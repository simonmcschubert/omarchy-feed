#!/usr/bin/env python3
"""Feed: serves the feed reader, syncs its sources and keeps its data.

Runs only while Feed is open: the launcher starts it, and it stops by itself
when the window closes (or after IDLE_SECONDS without a request). It syncs
only when the page asks: on opening and with the Sync button.

Listens on 127.0.0.1 only. Data: ~/.local/state/omarchy-feed/ (feed.db and
the images of saved articles), in a directory only the owner can open.
Updates come from the update script next to this file.

Python standard library only (3.11 or newer).
"""

import argparse
import concurrent.futures
import email.utils
import html
import html.parser
import http.client
import http.server
import ipaddress
import json
import os
import queue
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PORT = 47220
APP = "omarchy-feed"
APP_DIR = Path(__file__).resolve().parent / "app"
UPDATE_SCRIPT = Path(__file__).resolve().parent / "update"

IDLE_SECONDS = 300  # stop when no window has asked anything for this long
BYE_SECONDS = 5  # stop this soon after the window says it is closing
FIRST_SYNC_ITEMS = 3  # a new source starts with this many unread items
MAX_ENTRIES = 30  # newest entries looked at per source and sync
KEEP_READ_DAYS = 30  # read items are deleted this long after being marked read
MAX_ATTEMPTS = 3  # downloads of an article page before it counts as failed
FULL_TEXT_CHARS = 1000  # feed content at least this long is used as the article
WORKERS = 6
TIMEOUT = 15
MAX_FEED = 10 * 1024 * 1024
MAX_PAGE = 8 * 1024 * 1024
MAX_IMAGE = 8 * 1024 * 1024
MAX_IMAGES = 60
MAX_BODY = 20 * 1024 * 1024
# Seconds: the first check for an update after the server starts, and then.
FIRST_CHECK = 15
CHECK_EVERY = 4 * 60 * 60
# Which updates Feed offers: releases, or previews of main (update script).
CHANNELS = ("stable", "preview")

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
IMAGE_TYPES = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "avif": "image/avif",
}
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
X_HOSTS = {"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
FEED_PATHS = ("/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml")

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.mjs": ("app.mjs", "text/javascript; charset=utf-8"),
    "/clean.mjs": ("clean.mjs", "text/javascript; charset=utf-8"),
    "/update.mjs": ("update.mjs", "text/javascript; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    # The reader's typefaces: variable fonts, Latin subset (licenses next to them).
    "/fonts/source-serif-4.woff2": ("fonts/source-serif-4.woff2", "font/woff2"),
    "/fonts/source-serif-4-italic.woff2": ("fonts/source-serif-4-italic.woff2", "font/woff2"),
    "/fonts/inter.woff2": ("fonts/inter.woff2", "font/woff2"),
    "/fonts/inter-italic.woff2": ("fonts/inter-italic.woff2", "font/woff2"),
    "/fonts/literata.woff2": ("fonts/literata.woff2", "font/woff2"),
    "/fonts/literata-italic.woff2": ("fonts/literata-italic.woff2", "font/woff2"),
}

# How the reader sets articles (Settings > Reader); style.css has the rules.
READER_STYLES = ("editorial", "modern", "book", "system")
DEFAULT_READER_STYLE = "editorial"

# The page loads nothing from outside this server: articles are read from
# local copies, and the browser refuses anything else.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

# Tokyo Night, Omarchy's default theme: used when no theme file is found.
DEFAULT_THEME = {
    "background": "#1a1b26",
    "lighter_background": "#24283b",
    "dark_background": "#13141c",
    "foreground": "#a9b1d6",
    "bright_foreground": "#c0caf5",
    "dark_foreground": "#565f89",
    "muted": "#414868",
    "selection": "#292e42",
    "accent": "#7aa2f7",
    "red": "#f7768e",
    "green": "#9ece6a",
    "yellow": "#e0af68",
}

# CSS variable -> colors.toml keys to try, in order (as omarchy-theme-color).
THEME_VARIABLES = {
    "bg": ["background", "bg", "color0"],
    "surface": ["lighter_background", "lighter_bg", "background", "bg", "color0"],
    "sunken": ["dark_background", "dark_bg", "background", "bg", "color0"],
    "fg": ["foreground", "fg", "color7"],
    "fg-strong": ["bright_foreground", "bright_fg", "color15", "foreground", "fg", "color7"],
    "fg-muted": ["dark_foreground", "dark_fg", "color8", "foreground", "fg", "color7"],
    "border": ["muted", "color8", "dark_foreground", "dark_fg"],
    "selection": ["selection", "selection_background", "color8"],
    "accent": ["accent", "blue", "color4"],
    "red": ["red", "color1"],
    "green": ["green", "color2"],
    "yellow": ["yellow", "color3"],
}

HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,          -- feed | youtube | sitemap
  platform TEXT,               -- substack | beehiiv | youtube | NULL
  site TEXT NOT NULL,          -- host without www., picks the cleanup rules
  title TEXT NOT NULL,
  url TEXT NOT NULL,           -- the page the source stands for
  fetch_url TEXT NOT NULL UNIQUE,
  added_at INTEGER NOT NULL,
  synced_at INTEGER,
  error TEXT
);
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reused: image addresses contain it
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  kind TEXT NOT NULL,          -- article | video
  state TEXT NOT NULL,         -- seen | video | fetch | clean | ready | failed
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  published INTEGER,
  added_at INTEGER NOT NULL,
  read_at INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0,
  raw TEXT,                    -- downloaded HTML waiting for cleanup
  raw_is_page INTEGER,         -- 1 web page, 0 feed content
  content TEXT,                -- cleaned HTML with local images
  removed TEXT,                -- JSON: what the cleanup took out
  error TEXT,
  UNIQUE (source_id, key)
);
CREATE INDEX IF NOT EXISTS items_state ON items(state);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


class FetchError(Exception):
    pass


@dataclass
class Entry:
    key: str
    title: str
    url: str
    published: int | None
    kind: str = "article"
    content: str | None = None


# --- fetching ----------------------------------------------------------------

# Tests serve their pages from 127.0.0.1; everything else may only reach
# public addresses, so a feed cannot point the server at the local network.
allow_private_hosts = False


def check_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError(f"Not a web address: {url}")


def public_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, **kwargs):
    """socket.create_connection that refuses addresses that are not public.
    It checks the address it connects to, so a host name that resolves to a
    public address first and a local one later cannot get through."""
    host, port = address
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise FetchError(f"Cannot find {host}")
    error = None
    for family, kind, proto, _, sockaddr in infos:
        if not allow_private_hosts and not ipaddress.ip_address(sockaddr[0].split("%", 1)[0]).is_global:
            raise FetchError(f"{host} is not a public address")
        sock = socket.socket(family, kind, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            sock.connect(sockaddr)
            return sock
        except OSError as failure:
            error = failure
            sock.close()
    raise error or OSError(f"Cannot connect to {host}")


class PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = public_connection


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = public_connection


class PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(PublicHTTPConnection, req)


class PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(PublicHTTPSConnection, req, context=self._context)


class CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# No proxies from the environment: connections go straight to the checked address.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), PublicHTTPHandler(), PublicHTTPSHandler(), CheckedRedirects())


def fetch(url, limit, accept="*/*"):
    """GET url; returns (final url, content type, bytes). Raises FetchError."""
    # Non-ASCII characters in the path or query, as some feeds write them.
    url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")
    check_url(url)
    headers = {"User-Agent": USER_AGENT, "Accept": accept, "Accept-Language": "en"}
    if urllib.parse.urlsplit(url).hostname in YOUTUBE_HOSTS:
        headers["Cookie"] = "SOCS=CAI"  # skips Google's cookie consent page in the EU
    request = urllib.request.Request(url, headers=headers)
    try:
        with OPENER.open(request, timeout=TIMEOUT) as response:
            data = response.read(limit + 1)
            if len(data) > limit:
                raise FetchError(f"Too large: {url}")
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                # The limit counts the unpacked size, too.
                unpack = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
                data = unpack.decompress(data, limit + 1)
                if len(data) > limit or unpack.unconsumed_tail:
                    raise FetchError(f"Too large: {url}")
            return response.geturl(), response.headers.get("Content-Type", ""), data
    except urllib.error.HTTPError as error:
        error.close()
        raise FetchError(f"HTTP {error.code} from {url}")
    except urllib.error.URLError as error:
        reason = error.reason
        if isinstance(reason, FetchError):
            raise reason
        raise FetchError(f"Cannot reach {url}: {getattr(reason, 'strerror', None) or reason}")
    except (OSError, http.client.HTTPException, EOFError, zlib.error, ValueError) as error:
        raise FetchError(f"Cannot reach {url}: {error}")


def decode_html(data, content_type=""):
    match = re.search(r"charset=([\w-]+)", content_type, re.I)
    if not match:
        match = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", data[:4096], re.I)
    charset = match.group(1) if match else "utf-8"
    if isinstance(charset, bytes):
        charset = charset.decode("ascii", "replace")
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


class HeadInfo(html.parser.HTMLParser):
    """Title, meta tags and links from the head of a page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.links = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and key not in self.meta:
                self.meta[key] = a.get("content", "")
        elif tag == "link":
            self.links.append(a)
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def head_info(page):
    end = page.lower().find("</head>")
    parser = HeadInfo()
    parser.feed(page[: end if end > 0 else 300_000])
    return parser


def page_title(info):
    return clean_text(info.meta.get("og:title") or info.title)


def clean_text(value):
    """Plain text from a title that may hold tags or escaped entities."""
    value = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    return re.sub(r"\s+", " ", value).strip()


def site_of(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


# --- feeds ---------------------------------------------------------------------


def local_name(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def child(element, name):
    for c in element:
        if local_name(c.tag) == name:
            return c
    return None


def child_text(element, *names):
    for name in names:
        c = child(element, name)
        if c is not None and (c.text or "").strip():
            return c.text.strip()
    return ""


def parse_date(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())


def atom_content(entry):
    for name in ("content", "summary"):
        c = child(entry, name)
        if c is None:
            continue
        if c.get("type") == "xhtml":
            # Plain tag names: <html:p> would not be a paragraph to the HTML parser.
            for element in c.iter():
                element.tag = local_name(element.tag)
            return "".join(ET.tostring(x, encoding="unicode") for x in c)
        if (c.text or "").strip():
            # Atom content without type="html" is plain text.
            return html.escape(c.text) if c.get("type", "text") == "text" else c.text
    return None


def parse_feed(data, base_url):
    """(title, entries) from RSS 2.0, RSS 1.0 or Atom bytes."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise FetchError(f"Not a valid feed ({error})")
    kind = local_name(root.tag)
    entries = []
    if kind in ("rss", "rdf"):
        channel = child(root, "channel")
        title = child_text(channel, "title") if channel is not None else ""
        holder = channel if kind == "rss" and channel is not None else root
        for item in holder:
            if local_name(item.tag) != "item":
                continue
            guid = child_text(item, "guid")
            link = child_text(item, "link") or (guid if guid.startswith("http") else "")
            content = None
            for c in item:
                if local_name(c.tag) == "encoded" and (c.text or "").strip():
                    content = c.text
            entries.append(
                Entry(
                    key=guid or link,
                    title=clean_text(child_text(item, "title")),
                    url=urllib.parse.urljoin(base_url, link),
                    published=parse_date(child_text(item, "pubdate", "date")),
                    content=content or child_text(item, "description") or None,
                )
            )
    elif kind == "feed":
        title = child_text(root, "title")
        for entry in root:
            if local_name(entry.tag) != "entry":
                continue
            link = ""
            for c in entry:
                if local_name(c.tag) == "link" and c.get("rel", "alternate") == "alternate":
                    link = c.get("href", "")
                    break
            video_id = child_text(entry, "videoid")
            url = urllib.parse.urljoin(base_url, link)
            if "/shorts/" in url:
                continue  # YouTube Shorts
            entries.append(
                Entry(
                    key=child_text(entry, "id") or link,
                    title=clean_text(child_text(entry, "title")),
                    url=url,
                    published=parse_date(child_text(entry, "published", "updated")),
                    kind="video" if video_id else "article",
                    content=None if video_id else atom_content(entry),
                )
            )
        if not title or title in ("Videos", "Uploads"):
            author = child(root, "author")
            title = child_text(author, "name") if author is not None else title
    else:
        raise FetchError("Not a feed")
    entries = [e for e in entries if e.key and e.url.startswith(("http://", "https://"))]
    return clean_text(title), entries


def parse_sitemap(data):
    """Posts (paths starting with /p/, as beehiiv has them) from a sitemap."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise FetchError(f"Not a valid sitemap ({error})")
    entries = []
    for url in root:
        if local_name(url.tag) != "url":
            continue
        loc = child_text(url, "loc")
        if not urllib.parse.urlsplit(loc).path.startswith("/p/"):
            continue
        slug = urllib.parse.urlsplit(loc).path.rstrip("/").rsplit("/", 1)[-1]
        title = slug.replace("-", " ").strip().capitalize() or loc
        entries.append(Entry(key=loc, title=title, url=loc, published=parse_date(child_text(url, "lastmod"))))
    return entries


def newest(entries):
    return sorted(entries, key=lambda e: e.published or 0, reverse=True)[:MAX_ENTRIES]


def looks_like_feed(content_type, data):
    if re.search(r"(rss|atom|xml)", content_type, re.I):
        return True
    start = data[:500].lstrip().lower()
    return start.startswith(b"<?xml") or start.startswith(b"<rss") or start.startswith(b"<feed")


def platform_of(text):
    if "substackcdn.com" in text or "substack.com" in text:
        return "substack"
    if "beehiiv" in text:
        return "beehiiv"
    return None


# --- adding a source -------------------------------------------------------------


@dataclass
class NewSource:
    kind: str
    platform: str | None
    site: str
    title: str
    url: str
    fetch_url: str


def resolve_source(address):
    """Works out what to fetch for an address typed in Settings."""
    url = address.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("Enter a web address, such as https://example.com")
    if parts.hostname.lower() in YOUTUBE_HOSTS:
        return resolve_youtube(url)

    final, content_type, data = fetch(url, MAX_PAGE, "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    if looks_like_feed(content_type, data):
        try:
            title, _ = parse_feed(data, final)
            platform = platform_of(data.decode("utf-8", "replace"))
            return NewSource("feed", platform, site_of(final), title or site_of(final), final, final)
        except FetchError:
            pass  # an XHTML page, for example

    page = decode_html(data, content_type)
    info = head_info(page)
    platform = platform_of(page)
    name = clean_text(info.meta.get("og:site_name") or info.title) or site_of(final)
    candidates = []
    for link in info.links:
        rel = link.get("rel", "").lower().split()
        if "alternate" in rel and link.get("type", "").lower() in ("application/rss+xml", "application/atom+xml"):
            candidates.append(urllib.parse.urljoin(final, link.get("href", "")))
    origin = f"{urllib.parse.urlsplit(final).scheme}://{urllib.parse.urlsplit(final).netloc}"
    candidates += [origin + path for path in FEED_PATHS]
    for candidate in dict.fromkeys(candidates):
        try:
            feed_url, _, feed = fetch(candidate, MAX_FEED, "application/rss+xml,application/atom+xml,application/xml,text/xml")
            title, entries = parse_feed(feed, feed_url)
        except FetchError:
            continue
        if entries:
            return NewSource("feed", platform, site_of(final), title or name, final, feed_url)

    if platform == "beehiiv":
        try:
            sitemap_url, _, sitemap = fetch(origin + "/sitemap.xml", MAX_FEED, "application/xml,text/xml")
            if parse_sitemap(sitemap):
                return NewSource("sitemap", platform, site_of(final), name, final, sitemap_url)
        except FetchError:
            pass
    raise FetchError(f"No feed found at {final}")


CHANNEL_ID = re.compile(r"(UC[\w-]{22})")


def youtube_channel_id(page):
    info = head_info(page)
    for link in info.links:
        if "canonical" in link.get("rel", "").lower().split():
            match = re.search(r"/channel/" + CHANNEL_ID.pattern, link.get("href", ""))
            if match:
                return match.group(1)
    match = re.search(r'"externalId":"' + CHANNEL_ID.pattern, page)
    return match.group(1) if match else None


def resolve_youtube(url):
    match = re.search(r"/channel/" + CHANNEL_ID.pattern, url)
    channel_id = match.group(1) if match else None
    if not channel_id:
        _, content_type, data = fetch(url, MAX_PAGE, "text/html")
        channel_id = youtube_channel_id(decode_html(data, content_type))
        if not channel_id:
            raise FetchError("No YouTube channel found at this address")
    # The channel's uploads without Shorts and live streams; the full feed
    # as a fallback for channels without that list.
    feeds = [
        f"https://www.youtube.com/feeds/videos.xml?playlist_id=UULF{channel_id[2:]}",
        f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
    ]
    last_error = None
    for feed_url in feeds:
        try:
            _, _, data = fetch(feed_url, MAX_FEED, "application/atom+xml,application/xml")
            title, _ = parse_feed(data, feed_url)
        except FetchError as error:
            last_error = error
            continue
        return NewSource("youtube", "youtube", "youtube.com", title or "YouTube channel", f"https://www.youtube.com/channel/{channel_id}", feed_url)
    raise last_error


def fetch_entries(source):
    accept = "application/rss+xml,application/atom+xml,application/xml,text/xml"
    final, _, data = fetch(source["fetch_url"], MAX_FEED, accept)
    if source["kind"] == "sitemap":
        return newest(parse_sitemap(data))
    _, entries = parse_feed(data, final)
    return newest(entries)


# --- storage ---------------------------------------------------------------------


def state_dir():
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / APP


def now():
    return int(time.time())


class Store:
    """The SQLite file and the image directories. One connection; a lock
    keeps threads from using it at the same time."""

    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.images = self.dir / "images"
        self.images.mkdir(mode=0o700, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.dir / "feed.db", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self):
        with self.lock:
            self.db.close()

    def _query(self, sql, args=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args)]

    def _run(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    # Settings

    def reader_style(self):
        rows = self._query("SELECT value FROM settings WHERE key = 'reader_style'")
        style = rows[0]["value"] if rows else None
        return style if style in READER_STYLES else DEFAULT_READER_STYLE

    def set_reader_style(self, style):
        self._run("INSERT INTO settings (key, value) VALUES ('reader_style', ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (style,))

    # Sources

    def source(self, source_id):
        rows = self._query("SELECT * FROM sources WHERE id = ?", (source_id,))
        return rows[0] if rows else None

    def sources(self):
        return self._query("SELECT * FROM sources ORDER BY title COLLATE NOCASE")

    def add_source(self, new):
        with self.lock:
            if self._query("SELECT id FROM sources WHERE fetch_url = ?", (new.fetch_url,)):
                raise FetchError(f"{new.title} is already a source")
            cursor = self._run(
                "INSERT INTO sources (kind, platform, site, title, url, fetch_url, added_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new.kind, new.platform, new.site, new.title, new.url, new.fetch_url, now()),
            )
            return cursor.lastrowid

    def delete_source(self, source_id):
        with self.lock:
            ids = [r["id"] for r in self._query("SELECT id FROM items WHERE source_id = ?", (source_id,))]
            self._run("DELETE FROM sources WHERE id = ?", (source_id,))
        for item_id in ids:
            self.remove_images(item_id)

    def sync_failed(self, source_id, error):
        self._run("UPDATE sources SET error = ? WHERE id = ?", (error, source_id))

    def add_entries(self, source_id, entries):
        """Stores the entries of a source's sync not seen before and marks the
        source synced; returns how many show up as new. A source's first sync
        shows only its newest FIRST_SYNC_ITEMS; the others are kept as seen,
        so they never show up later."""
        added = 0
        with self.lock:
            first = not self._query("SELECT 1 FROM items WHERE source_id = ? LIMIT 1", (source_id,))
            for index, entry in enumerate(entries):
                if self._query("SELECT 1 FROM items WHERE source_id = ? AND key = ?", (source_id, entry.key)):
                    continue
                raw, raw_is_page = None, None
                if first and index >= FIRST_SYNC_ITEMS:
                    state = "seen"
                elif entry.kind == "video":
                    state = "video"
                elif entry.content and len(clean_text(entry.content)) >= FULL_TEXT_CHARS:
                    state, raw, raw_is_page = "clean", entry.content, 0
                else:
                    state = "fetch"
                self._run(
                    "INSERT INTO items (source_id, key, kind, state, title, url, published, added_at, raw, raw_is_page)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (source_id, entry.key, entry.kind, state, entry.title or entry.url, entry.url, entry.published, now(), raw, raw_is_page),
                )
                added += state != "seen"
            self._run("UPDATE sources SET synced_at = ?, error = NULL WHERE id = ?", (now(), source_id))
        return added

    # Items: an entry is stored as seen (never shown), video, or as an article
    # that goes fetch -> clean -> ready; fetch and clean may end in failed.
    # Read items go back to seen after KEEP_READ_DAYS, so they never return.

    def items(self):
        return self._query(
            "SELECT i.id, i.source_id, i.kind, i.state, i.title, i.url, i.published, i.added_at, i.read_at, i.error"
            " FROM items i WHERE i.state != 'seen'"
            " ORDER BY COALESCE(i.published, i.added_at) DESC, i.id DESC"
        )

    def item(self, item_id):
        rows = self._query(
            "SELECT i.id, i.source_id, i.kind, i.state, i.title, i.url, i.published, i.added_at, i.read_at,"
            " i.error, i.content, i.removed FROM items i WHERE i.id = ? AND i.state != 'seen'",
            (item_id,),
        )
        return rows[0] if rows else None

    def pages_to_fetch(self):
        """Articles whose page is to be downloaded, with their source's kind."""
        return self._query(
            "SELECT i.id, i.url, s.kind AS source_kind FROM items i JOIN sources s ON s.id = i.source_id"
            " WHERE i.state = 'fetch' AND i.attempts < ?",
            (MAX_ATTEMPTS,),
        )

    def page_fetched(self, item_id, url, page, title=None):
        """Keeps a downloaded page for cleanup. `url` is the address after
        redirects: the cleanup resolves relative links and images against it."""
        self._run(
            "UPDATE items SET state = 'clean', raw = ?, raw_is_page = 1, error = NULL, url = ?,"
            " title = COALESCE(NULLIF(?, ''), title) WHERE id = ? AND state = 'fetch'",
            (page, url, title, item_id),
        )

    def page_failed(self, item_id, error):
        """Counts a failed download; after MAX_ATTEMPTS the article failed."""
        self._run(
            "UPDATE items SET attempts = attempts + 1, error = ?,"
            " state = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE state END WHERE id = ? AND state = 'fetch'",
            (error, MAX_ATTEMPTS, item_id),
        )

    def pending(self):
        """Articles waiting for the page to clean them up, with what the
        cleanup needs to know."""
        rows = self._query(
            "SELECT i.id, i.title, i.url, i.raw_is_page, s.site, s.platform FROM items i"
            " JOIN sources s ON s.id = i.source_id WHERE i.state = 'clean' ORDER BY i.id"
        )
        return [{**r, "page": bool(r.pop("raw_is_page"))} for r in rows]

    def raw(self, item_id):
        """The HTML of an article waiting for cleanup; None for any other."""
        rows = self._query("SELECT raw FROM items WHERE id = ? AND state = 'clean'", (item_id,))
        return rows[0]["raw"] if rows else None

    def save_article(self, item_id, content, removed, images):
        """Saves a cleaned article with its images ({name: bytes}). False
        when it is no longer waiting for cleanup, as when its source was
        removed meanwhile; then nothing is saved."""
        with self.lock:
            if self.raw(item_id) is None:
                return False
            self.remove_images(item_id)
            if images:
                folder = self.images / str(int(item_id))
                folder.mkdir(mode=0o700, parents=True)
                for name, data in images.items():
                    (folder / name).write_bytes(data)
            self._run(
                "UPDATE items SET state = 'ready', raw = NULL, content = ?, removed = ?, error = NULL WHERE id = ?",
                (content, json.dumps(removed), item_id),
            )
        return True

    def cleanup_failed(self, item_id, error):
        """Marks an article waiting for cleanup failed; False for any other."""
        cursor = self._run("UPDATE items SET state = 'failed', raw = NULL, error = ? WHERE id = ? AND state = 'clean'", (error, item_id))
        return cursor.rowcount > 0

    def mark_read(self, item_ids, read):
        """Marks items read now, or unread. Items already read keep their time."""
        stamp = now()
        with self.lock:
            for item_id in item_ids:
                if read:
                    self._run("UPDATE items SET read_at = ? WHERE id = ? AND read_at IS NULL AND state != 'seen'", (stamp, item_id))
                else:
                    self._run("UPDATE items SET read_at = NULL WHERE id = ? AND state != 'seen'", (item_id,))

    def remove_images(self, item_id):
        shutil.rmtree(self.images / str(int(item_id)), ignore_errors=True)

    def prune(self, before):
        """Deletes items read before `before`; keeps the seen markers."""
        with self.lock:
            ids = [r["id"] for r in self._query("SELECT id FROM items WHERE read_at IS NOT NULL AND read_at < ? AND state != 'seen'", (before,))]
            for item_id in ids:
                # A small marker stays, so the entry is not added again.
                self._run("UPDATE items SET state = 'seen', raw = NULL, content = NULL, removed = NULL WHERE id = ?", (item_id,))
        for item_id in ids:
            self.remove_images(item_id)
        return len(ids)


# --- sync --------------------------------------------------------------------------


def sync(store, source_ids=None):
    """Fetches the sources, then the pages of new articles. Cleanup and
    images follow when the page posts each article back."""
    sources = store.sources()
    if source_ids is not None:
        sources = [s for s in sources if s["id"] in source_ids]
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(_try_entries, sources))
    added, errors = 0, {}
    for source, (entries, error) in zip(sources, results):
        if error:
            store.sync_failed(source["id"], error)
            errors[source["id"]] = error
            continue
        added += store.add_entries(source["id"], entries)

    todo = store.pages_to_fetch()
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        for item, (final, page, error) in zip(todo, pool.map(_try_page, todo)):
            if error:
                store.page_failed(item["id"], error)
                continue
            title = page_title(head_info(page)) if item["source_kind"] == "sitemap" else None
            store.page_fetched(item["id"], final, page, title)
    store.prune(now() - KEEP_READ_DAYS * 86400)
    return {"added": added, "errors": errors, "pending": len(store.pending())}


def _try_entries(source):
    try:
        return fetch_entries(source), None
    except FetchError as error:
        return None, str(error)


def _try_page(item):
    try:
        final, content_type, data = fetch(item["url"], MAX_PAGE, "text/html,application/xhtml+xml")
        return final, decode_html(data, content_type), None
    except FetchError as error:
        return None, None, str(error)


def image_type(data):
    """File extension from an image's first bytes; None for anything else
    (SVG included, as it can hold scripts)."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return "avif"
    return None


SCRIPTS_AND_STYLES = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>|\sstyle=(\"[^\"]*\"|'[^']*')", re.I | re.S)


def strip_page(page):
    """Takes out scripts, style sheets and style attributes before the page
    parses the HTML: the cleanup drops them anyway, and the page's content
    security policy would report every inline style."""
    return SCRIPTS_AND_STYLES.sub("", page)


IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
ATTR = re.compile(r'\b(src|alt)="([^"]*)"', re.I)


def download_images(item_id, content):
    """Downloads the images an article shows and points it at the copies:
    returns the article and the images to save, as {name: bytes}. Images
    that cannot be downloaded are left out, so nothing loads from outside."""
    tags = IMG_TAG.findall(content)[:MAX_IMAGES]
    sources = []
    for tag in tags:
        attrs = {k.lower(): v for k, v in ATTR.findall(tag)}
        sources.append(html.unescape(attrs.get("src", "")))

    def download(numbered):
        number, url = numbered
        try:
            _, content_type, data = fetch(url, MAX_IMAGE, "image/avif,image/webp,image/png,image/jpeg,image/gif;q=0.9")
        except FetchError:
            return None
        extension = image_type(data)
        if not extension:
            return None
        return f"{number}.{extension}", data

    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        saved = dict(zip(tags, pool.map(download, enumerate(sources))))

    def replace(match):
        tag = match.group(0)
        image = saved.get(tag)
        if not image:
            return ""
        alt = {k.lower(): v for k, v in ATTR.findall(tag)}.get("alt", "")
        return f'<img src="/img/{int(item_id)}/{image[0]}" alt="{alt}" loading="lazy">'

    return IMG_TAG.sub(replace, content), dict(image for image in saved.values() if image)


# --- opening links -----------------------------------------------------------------


def is_youtube_video(url):
    parts = urllib.parse.urlsplit(url)
    return (parts.hostname or "").lower() in YOUTUBE_HOSTS and parts.path in ("/watch", "/live") or (parts.hostname or "").lower() == "youtu.be"


def is_x_link(url):
    return (urllib.parse.urlsplit(url).hostname or "").lower() in X_HOSTS


def web_app(url):
    """The web app a link opens in: its name and the hosts its windows are
    named after. None for a link the browser opens."""
    if is_youtube_video(url):
        return "YouTube", YOUTUBE_HOSTS | {"youtu.be"}
    if is_x_link(url):
        return "X", X_HOSTS
    return None


def launch(command):
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


class Hyprland:
    """Omarchy's window manager, through hyprctl. Omarchy 4.5 configures it in
    Lua, where a dispatch is a Lua expression; before, it took a dispatcher
    and its arguments. As Omarchy's own scripts do, each dispatch tries the
    Lua one first and the old one when Hyprland does not answer ok."""

    def __init__(self, run=subprocess.run):
        self.run = run

    @classmethod
    def here(cls):
        """Hyprland when Feed runs in it, otherwise None."""
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"):
            return cls()
        return None

    def get(self, what):
        """The clients (windows), monitors or workspaces, as JSON."""
        done = self.run(["hyprctl", what, "-j"], capture_output=True, text=True, timeout=5, check=True)
        return json.loads(done.stdout)

    def dispatch(self, lua, *old):
        for args in ([lua], list(old)):
            done = self.run(["hyprctl", "dispatch", *args], capture_output=True, text=True, timeout=5)
            if done.returncode == 0 and done.stdout.strip().startswith("ok"):
                return True
        return False

    def focus_window(self, address):
        self.dispatch(f'hl.dsp.focus({{ window = "address:{address}" }})', "focuswindow", f"address:{address}")

    def focus_workspace(self, number, workspace):
        # o.workspaces.focus is what Super + the number does in Omarchy 4.5.
        self.dispatch(f"o.workspaces.focus({number})", "workspace", str(workspace))

    def move_window(self, address, workspace):
        self.dispatch(
            f'hl.dsp.window.move({{ workspace = "{workspace}", follow = false, window = "address:{address}" }})',
            "movetoworkspacesilent",
            f"{workspace},address:{address}",
        )

    def close_window(self, address):
        self.dispatch(f'hl.dsp.window.close({{ window = "address:{address}" }})', "closewindow", f"address:{address}")


WEB_APP_SECONDS = 10  # how long a web app's new window may take to appear


def app_windows(clients, hosts):
    """The web app windows of these hosts, the last focused first. Chromium
    names an app window after the address it opened, as in
    chrome-www.youtube.com__watch-Default (see omarchy-webapp-window-class);
    browser windows are just chromium, so a YouTube tab never counts."""
    found = []
    for client in clients:
        host = client.get("class", "").lower().split("_", 1)[0].partition("-")[2]
        if host in hosts and client.get("mapped", True) and re.fullmatch(r"0x[0-9a-f]+", client.get("address", ""), re.I):
            found.append(client)
    return sorted(found, key=lambda client: client.get("focusHistoryID", 0))


def empty_workspace(monitors, workspaces):
    """The first workspace without windows on the monitor in use, as the
    number Super goes to it with and its id; None when all ten have windows.
    Omarchy 4.5 gives each monitor ten of its own (the first 1-10, the next
    11-20, and so on); before, there were just 1-10."""
    monitor = next((m for m in monitors if m.get("focused")), None)
    if not monitor:
        return None
    active = int(monitor["activeWorkspace"]["id"])
    first = (active - 1) // 10 * 10 if active > 0 else 0
    used = {int(w["id"]) for w in workspaces if w.get("windows")}
    for number in range(1, 11):
        if first + number not in used:
            return number, first + number
    return None


class LinkOpener:
    """Opens links where Omarchy shows them: videos in its YouTube web app,
    links to X in its X web app, email links in the mail app, everything else
    in the browser. In Hyprland, a web app link takes the place of what the
    app's window shows (see open_in_web_app). That waits for the new window,
    so it happens after the request, one link at a time, so two quick ones
    find each other's window."""

    def __init__(self, hyprland, launch=launch, which=shutil.which, wait=WEB_APP_SECONDS):
        self.hyprland = hyprland  # None outside Hyprland
        self.launch = launch
        self.which = which
        self.wait = wait
        self.links = queue.Queue()
        if hyprland:
            threading.Thread(target=self.work, daemon=True).start()

    def open(self, url):
        """Opens a link; returns where, for the page to say."""
        app = web_app(url)
        if app and self.hyprland:
            if not self.which("omarchy-launch-webapp"):
                raise FileNotFoundError("omarchy-launch-webapp not found")
            self.links.put((url, app[1]))
            return app[0]
        if app:
            command, where = ["omarchy-launch-webapp", url], app[0]
        elif url.lower().startswith("mailto:"):
            command, where = ["xdg-open", url], "the mail app"
        elif self.which("omarchy-launch-browser"):
            command, where = ["omarchy-launch-browser", url], "the browser"
        else:
            command, where = ["xdg-open", url], "the browser"
        self.launch(command)
        return where

    def close(self):
        """Waits for the links still waiting their turn, so they open when
        the server stops."""
        self.links.join()

    def work(self):
        while True:
            url, hosts = self.links.get()
            try:
                self.open_in_web_app(url, hosts)
            except Exception:
                sys.stderr.write(f"could not open {url}:\n{traceback.format_exc()}")
            finally:
                self.links.task_done()

    def open_in_web_app(self, url, hosts):
        """Opens a link in its web app's window, when one is open, or in a new
        window on the first empty workspace. A web app is a Chromium window,
        and nothing outside it can send it to another address, so the link
        opens in a new window where the old one is, which then closes."""
        old, workspace = None, 0  # the window it replaces, and the workspace for the new one
        try:
            clients = self.hyprland.get("clients")
            windows = app_windows(clients, hosts)
            if windows:
                old = windows[0]
                self.hyprland.focus_window(old["address"])
                workspace = int(old["workspace"]["id"])
            else:
                empty = empty_workspace(self.hyprland.get("monitors"), self.hyprland.get("workspaces"))
                if empty:
                    self.hyprland.focus_workspace(*empty)
                    workspace = empty[1]
            before = {client.get("address") for client in clients}
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            sys.stderr.write(f"hyprctl failed, opening {url} where Hyprland puts it: {error}\n")
            self.launch(["omarchy-launch-webapp", url])
            return
        self.launch(["omarchy-launch-webapp", url])
        try:
            deadline = time.monotonic() + self.wait
            while True:
                time.sleep(0.1)
                new = [c for c in app_windows(self.hyprland.get("clients"), hosts) if c["address"] not in before]
                if new:
                    break
                if time.monotonic() > deadline:
                    return  # no new window, so the old one stays
            # A window opens on the workspace in view when it appears, which is
            # another one when the user has moved on in the meantime.
            if workspace > 0 and int(new[0]["workspace"]["id"]) != workspace:
                self.hyprland.move_window(new[0]["address"], workspace)
            if old:
                self.hyprland.close_window(old["address"])
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            sys.stderr.write(f"hyprctl failed after opening {url}: {error}\n")


# --- theme -------------------------------------------------------------------------


def theme_file():
    return Path.home() / ".local" / "state" / "omarchy" / "current" / "theme" / "colors.toml"


def luminance_is_light(color):
    value = color.lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return r + g + b > 382


def theme_css(path):
    """CSS variables from an Omarchy colors.toml; defaults for anything missing."""
    try:
        with open(path, "rb") as f:
            colors = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        colors = {}

    def pick(keys):
        for key in keys:
            value = colors.get(key)
            if isinstance(value, str) and HEX_COLOR.match(value.strip()):
                return value.strip()
        return None

    lines = [f"  --{variable}: {pick(keys) or DEFAULT_THEME[keys[0]]};" for variable, keys in THEME_VARIABLES.items()]
    mode = str(colors.get("mode") or colors.get("theme_type") or "").lower()
    if mode not in ("light", "dark"):
        if (Path(path).parent / "light.mode").exists():
            mode = "light"
        else:
            background = pick(THEME_VARIABLES["bg"])
            mode = "light" if background and luminance_is_light(background) else "dark"
    lines.append(f"  color-scheme: {mode};")
    # Light text on a dark background looks heavier: the reader's variable
    # fonts set it a little lighter there.
    lines.append(f"  --text-weight: {370 if mode == 'dark' else 400};")
    return ":root {\n" + "\n".join(lines) + "\n}\n"


# --- updates -----------------------------------------------------------------------


def update_log():
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "omarchy-feed-update.log"


def reason(output):
    """The update script's reason for failing: its last line, after git's."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1].removeprefix("update: ") if lines else ""


class Updater:
    """Offers updates of Feed the way a desktop app offers them: it checks
    shortly after the server starts, every four hours while Feed is open, and
    when the update button asks; it downloads an update when asked, and
    installs it with a restart of the server. An update is a release, or on
    the preview channel the newest preview. The update script does the work;
    the page shows the state (app/update.mjs)."""

    def __init__(self, script, log):
        self.script = Path(script)
        self.log = Path(log)  # what the install says
        # Tells the page that the server restarted, after an install.
        self.boot = secrets.token_hex(8)
        self.lock = threading.Lock()  # guards the fields below
        self.work = threading.Lock()  # one run of the script at a time
        # "": nothing to offer (up to date, not checked yet, offline, or no
        # git checkout). Otherwise available, downloading, downloaded,
        # installing, or failed (download or install; error says why).
        self.status = ""
        # What status is about: a release X.Y.Z, or a preview's commit id.
        self.version = ""
        self.error = ""
        self.checking = False
        ok, out, _ = self.run("version")
        # The installed release: "" before the first or without a checkout.
        self.installed = out.strip() if ok else ""
        ok, out, _ = self.run("channel")
        # stable or preview; "" without a checkout.
        self.channel = out.strip() if ok else ""

    def run(self, *args, timeout=60):
        """(ok, stdout, reason) of a run of the update script."""
        try:
            result = subprocess.run(
                [str(self.script), *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return False, "", f"the update script did not finish within {timeout} seconds"
        except OSError as error:
            return False, "", f"can't run {self.script}: {error.strerror}"
        if result.returncode != 0:
            return False, result.stdout, reason(result.stderr) or "the update script failed"
        return True, result.stdout, ""

    def snapshot(self):
        with self.lock:
            return {
                "status": self.status,
                "version": self.version,
                "error": self.error,
                "installed": self.installed,
                "channel": self.channel,
                "checking": self.checking,
                "boot": self.boot,
            }

    def start(self):
        def checks():
            time.sleep(FIRST_CHECK)
            while True:
                self.check()
                time.sleep(CHECK_EVERY)

        threading.Thread(target=checks, daemon=True).start()

    def check(self, asked=False):
        """Looks for a newer update. Returns why the check failed, or "".
        A check on the timer keeps what an earlier one found when it fails
        (offline is no reason to hide an update), and leaves a failed update
        on show until the button is clicked."""
        with self.lock:
            if self.status == "installing" or (self.status == "failed" and not asked):
                return ""
        with self.work:
            with self.lock:
                self.checking = True
            ok, out, why = self.run("check")
            with self.lock:
                self.checking = False
                if not ok:
                    sys.stderr.write(f"update check failed: {why}\n")
                    return why
                if self.status in ("downloading", "installing"):
                    return ""  # what those find comes next
                words = out.split()
                if len(words) == 2 and words[0] in ("available", "downloaded"):
                    self.status, self.version = words
                else:
                    self.status, self.version = "", ""
                self.error = ""
        return ""

    def set_channel(self, channel):
        """Offers releases (stable) or previews from now on. Returns why it
        can't, or "". Check next: what was found before is forgotten."""
        busy = "can't change the channel while Feed updates; try again when it is done"
        with self.lock:
            if self.status in ("downloading", "installing"):
                return busy  # without waiting for the install to end
        with self.work:
            with self.lock:
                if self.status in ("downloading", "installing"):
                    return busy
            ok, _, why = self.run("channel", channel)
            if not ok:
                return why
            with self.lock:
                self.channel = channel
                # A download clicked meanwhile finishes for the channel before.
                if self.status not in ("downloading", "installing"):
                    self.status, self.version, self.error = "", "", ""
        return ""

    def download(self):
        with self.lock:
            if self.status not in ("available", "failed") or not self.version:
                return
            self.status, self.error = "downloading", ""
            version = self.version
        with self.work:
            ok, _, why = self.run("download", version, timeout=180)
        with self.lock:
            self.status = "downloaded" if ok else "failed"
            self.error = why

    def install(self):
        with self.lock:
            if self.status != "downloaded":
                return
            self.status, self.error = "installing", ""
            version = self.version
        threading.Thread(target=self.wait_for_install, args=(version,), daemon=True).start()

    def install_command(self, version):
        return [str(self.script), "install", version]

    def wait_for_install(self, version):
        """Runs the install, which stops this server and starts the new one.
        So it runs in a session of its own, and writes to the log, not to a
        pipe that ends with this server."""
        with self.work:
            try:
                with open(self.log, "w") as log:
                    process = subprocess.Popen(
                        self.install_command(version), stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True
                    )
                ok, why = process.wait(timeout=600) == 0, ""
            except subprocess.TimeoutExpired:
                ok, why = False, "the update did not finish within 10 minutes"
            except OSError as error:
                ok, why = False, f"can't start the update: {error.strerror}"
            try:
                why = reason(self.log.read_text()) or why
            except OSError:
                pass  # the script did not start
        # A successful install stops this server, so it is still here only
        # when the install failed, or when it restarted the server on Feed's
        # port while this is another one (./server.py from the same checkout).
        with self.lock:
            if ok:
                self.status, self.version = "", ""
            else:
                self.status, self.error = "failed", why or "the update script failed"
        if ok:
            self.check()


# --- server ------------------------------------------------------------------------


class FeedServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, data_dir, theme_path, idle=IDLE_SECONDS, opener=None, updater=None):
        self.store = Store(data_dir)
        self.theme_path = Path(theme_path)
        self.idle = idle
        self.opener = opener or LinkOpener(None).open
        self.updater = updater
        # The page as it was when this server started: a `git pull` while
        # Feed runs changes the files, not this server's code, and a page
        # newer than its server may not load.
        self.static = {path: (APP_DIR / name).read_bytes() for path, (name, _) in STATIC.items()}
        self.sync_lock = threading.Lock()
        self.activity = threading.Lock()
        self.last_request = time.monotonic()
        self.in_flight = 0
        self.closing = False
        self.stopped = threading.Event()
        super().__init__(("127.0.0.1", port), Handler)
        bound = self.server_address[1]
        self.hosts = {f"127.0.0.1:{bound}", f"localhost:{bound}"}
        self.origins = {f"http://{host}" for host in self.hosts}

    def begin(self, bye=False):
        with self.activity:
            self.in_flight += 1
            self.last_request = time.monotonic()
            self.closing = bye

    def end(self):
        with self.activity:
            self.in_flight -= 1
            self.last_request = time.monotonic()

    def watch(self):
        """Stops the server once the window is gone: soon after it says
        goodbye, or after `idle` seconds without a request."""
        if not self.idle:
            return
        while not self.stopped.wait(1):
            with self.activity:
                quiet = time.monotonic() - self.last_request
                limit = BYE_SECONDS if self.closing else self.idle
                done = self.in_flight == 0 and quiet > limit
            if done:
                self.shutdown()
                return


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Feed"
    sys_version = ""
    timeout = 30

    def log_message(self, format, *args):
        pass

    def log_error(self, format, *args):
        sys.stderr.write("%s\n" % (format % args))

    def send_body(self, status, body, content_type, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        extra = extra or {}
        if "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-store")
        for key, value in extra.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status, value):
        self.send_body(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def allowed(self, write):
        # The Host check stops other websites from reaching this server
        # through DNS rebinding; the Origin check stops cross-site writes.
        if self.headers.get("Host") not in self.server.hosts:
            self.send_json(403, {"error": "Unknown host"})
            return False
        origin = self.headers.get("Origin")
        if (origin is not None or write) and origin not in self.server.origins:
            self.send_json(403, {"error": "Unknown origin"})
            return False
        return True

    def read_json(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Send JSON")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise ValueError("Content-Length required")
        if length < 0 or length > MAX_BODY:
            raise ValueError("Too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Invalid JSON")
        if not isinstance(body, dict):
            raise ValueError("Send a JSON object")
        return body

    def route(self, method):
        path = urllib.parse.urlsplit(self.path).path
        self.server.begin(bye=path == "/api/bye")
        try:
            if not self.allowed(method != "GET"):
                return
            route = ROUTES.get((method, re.sub(r"/\d+(?=/|$)", "/#", path)))
            if route is not None:
                route(self, path, *(int(n) for n in re.findall(r"/(\d+)(?=/|$)", path)))
            elif method == "GET" and path.startswith("/img/"):
                self.get_image(path)
            elif method == "GET" and path in STATIC:
                self.get_static(path)
            else:
                self.send_json(404, {"error": "Not found"})
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except FetchError as error:
            self.send_json(502, {"error": str(error)})
        except Exception:
            # A bug. Without an answer, the page would take the server for stopped.
            try:
                self.log_error("%s", traceback.format_exc().rstrip())
            except OSError:
                pass  # a full disk, say
            self.send_json(500, {"error": "Something went wrong; server.log says what"})
        finally:
            self.server.end()

    def do_GET(self):
        self.route("GET")

    def do_HEAD(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")

    # GET

    def get_static(self, path):
        name, content_type = STATIC[path]
        extra = {"Content-Security-Policy": CSP} if name.endswith(".html") else None
        self.send_body(200, self.server.static[path], content_type, extra)

    def get_theme(self, path):
        self.send_body(200, theme_css(self.server.theme_path).encode(), "text/css; charset=utf-8")

    def get_ping(self, path):
        self.send_json(200, {"app": APP})

    def get_state(self, path):
        store = self.server.store
        self.send_json(
            200,
            {
                "sources": [{k: s[k] for k in ("id", "kind", "platform", "site", "title", "url", "synced_at", "error")} for s in store.sources()],
                "items": store.items(),
                "dataDir": str(store.dir),
                "keepReadDays": KEEP_READ_DAYS,
                "readerStyle": store.reader_style(),
                "syncing": self.server.sync_lock.locked(),
            },
        )

    def get_update(self, path):
        if self.server.updater is None:
            self.send_json(404, {"error": "Not found"})
            return
        self.send_json(200, self.server.updater.snapshot())

    def get_item(self, path, item_id):
        item = self.server.store.item(item_id)
        if item is None:
            self.send_json(404, {"error": "No such item"})
            return
        item["removed"] = json.loads(item["removed"]) if item["removed"] else None
        self.send_json(200, item)

    def get_pending(self, path):
        self.send_json(200, {"items": self.server.store.pending()})

    def get_raw(self, path, item_id):
        raw = self.server.store.raw(item_id)
        if raw is None:
            self.send_json(404, {"error": "Nothing to clean up"})
            return
        self.send_json(200, {"id": item_id, "html": strip_page(raw)})

    def get_image(self, path):
        match = re.fullmatch(r"/img/(\d+)/(\d+)\.(\w+)", path)
        if not match or match.group(3) not in IMAGE_TYPES:
            self.send_json(404, {"error": "Not found"})
            return
        file = self.server.store.images / match.group(1) / f"{match.group(2)}.{match.group(3)}"
        try:
            body = file.read_bytes()
        except OSError:
            self.send_json(404, {"error": "Not found"})
            return
        self.send_body(200, body, IMAGE_TYPES[match.group(3)], {"Cache-Control": "private, max-age=86400"})

    # POST and DELETE

    def post_bye(self, path):
        self.send_json(200, {"ok": True})

    def post_stop(self, path):
        """Stops the server now, whatever windows are open: setup and the
        update script do this, so the next start runs the new code."""
        self.send_json(200, {"ok": True})
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def post_update(self, path):
        updater = self.server.updater
        if updater is None:
            self.send_json(404, {"error": "Not found"})
            return
        body = self.read_json()
        action = body.get("action")
        if action == "check":
            why = updater.check(asked=True)
            self.send_json(200, {**updater.snapshot(), "checkError": why})
        elif action == "download":
            updater.download()
            self.send_json(200, updater.snapshot())
        elif action == "install":
            updater.install()
            self.send_json(200, updater.snapshot())
        elif action == "channel" and body.get("channel") in CHANNELS:
            why = updater.set_channel(body["channel"])
            if why:
                self.send_json(409, {"error": why})
                return
            why = updater.check(asked=True)
            self.send_json(200, {**updater.snapshot(), "checkError": why})
        else:
            raise ValueError(f"Expected {{action: check, download, install, or channel with channel: {' or '.join(CHANNELS)}}}")

    def post_sync(self, path):
        if not self.server.sync_lock.acquire(blocking=False):
            self.send_json(409, {"error": "A sync is already running"})
            return
        try:
            result = sync(self.server.store)
        finally:
            self.server.sync_lock.release()
        self.send_json(200, result)

    def post_source(self, path):
        body = self.read_json()
        address = body.get("url")
        if not isinstance(address, str) or not address.strip():
            raise ValueError("Enter an address")
        new = resolve_source(address)
        source_id = self.server.store.add_source(new)
        with self.server.sync_lock:
            result = sync(self.server.store, {source_id})
        source = self.server.store.source(source_id)
        self.send_json(200, {"source": {k: source[k] for k in ("id", "kind", "platform", "site", "title")}, **result})

    def delete_source(self, path, source_id):
        if self.server.store.source(source_id) is None:
            self.send_json(404, {"error": "No such source"})
            return
        with self.server.sync_lock:  # a running sync would add items to it
            self.server.store.delete_source(source_id)
        self.send_json(200, {"ok": True})

    def post_content(self, path, item_id):
        body = self.read_json()
        store = self.server.store
        if store.raw(item_id) is None:
            done = False
        elif isinstance(body.get("error"), str):
            done = store.cleanup_failed(item_id, body["error"][:500])
        else:
            content, removed = body.get("html"), body.get("removed")
            if not isinstance(content, str) or not isinstance(removed, dict):
                raise ValueError("Expected {html, removed}")
            content, images = download_images(item_id, content)
            done = store.save_article(item_id, content, removed, images)
        if not done:
            self.send_json(404, {"error": "Nothing to clean up"})
            return
        self.send_json(200, {"ok": True})

    def post_read(self, path, item_id):
        body = self.read_json()
        read = body.get("read")
        if not isinstance(read, bool):
            raise ValueError("Expected {read: true or false}")
        self.server.store.mark_read([item_id], read)
        self.send_json(200, {"ok": True})

    def post_read_all(self, path):
        ids = self.read_json().get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            raise ValueError("Expected {ids: [numbers]}")
        self.server.store.mark_read(ids, True)
        self.send_json(200, {"ok": True})

    def post_settings(self, path):
        style = self.read_json().get("readerStyle")
        if style not in READER_STYLES:
            raise ValueError(f"readerStyle is one of: {', '.join(READER_STYLES)}")
        self.server.store.set_reader_style(style)
        self.send_json(200, {"ok": True})

    def post_open(self, path):
        url = self.read_json().get("url")
        if not isinstance(url, str) or not re.match(r"^(https?://|mailto:)[^\s]+$", url, re.I):
            raise ValueError("Only web and email addresses can be opened")
        try:
            where = self.server.opener(url)
        except OSError as error:
            self.send_json(500, {"error": f"Could not open it: {error}"})
            return
        self.send_json(200, {"ok": True, "opened_in": where})


ROUTES = {
    ("GET", "/theme.css"): Handler.get_theme,
    ("GET", "/api/ping"): Handler.get_ping,
    ("GET", "/api/state"): Handler.get_state,
    ("GET", "/api/update"): Handler.get_update,
    ("GET", "/api/items/#"): Handler.get_item,
    ("GET", "/api/pending"): Handler.get_pending,
    ("GET", "/api/raw/#"): Handler.get_raw,
    ("POST", "/api/bye"): Handler.post_bye,
    ("POST", "/api/stop"): Handler.post_stop,
    ("POST", "/api/update"): Handler.post_update,
    ("POST", "/api/sync"): Handler.post_sync,
    ("POST", "/api/sources"): Handler.post_source,
    ("DELETE", "/api/sources/#"): Handler.delete_source,
    ("POST", "/api/items/#/content"): Handler.post_content,
    ("POST", "/api/items/#/read"): Handler.post_read,
    ("POST", "/api/read"): Handler.post_read_all,
    ("POST", "/api/open"): Handler.post_open,
    ("POST", "/api/settings"): Handler.post_settings,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--data", default=None, help="data directory (default: ~/.local/state/omarchy-feed)")
    parser.add_argument("--idle", type=int, default=IDLE_SECONDS, help="stop after this many seconds without a request; 0 never stops")
    args = parser.parse_args()
    data_dir = Path(args.data) if args.data else state_dir()
    updater = Updater(UPDATE_SCRIPT, update_log())
    links = LinkOpener(Hyprland.here())
    try:
        server = FeedServer(args.port, data_dir, theme_file(), idle=args.idle, opener=links.open, updater=updater)
    except OSError as error:
        sys.exit(f"feed: cannot listen on 127.0.0.1:{args.port}: {error.strerror}")
    updater.start()
    print(f"Feed on http://localhost:{server.server_address[1]}/ (data: {data_dir})", file=sys.stderr, flush=True)
    watcher = threading.Thread(target=server.watch, daemon=True)
    watcher.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stopped.set()
        server.server_close()
        links.close()
        server.store.close()


if __name__ == "__main__":
    main()
