import gzip
import http.client
import http.server
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
LONG = "<p>" + "A paragraph of the article that is long enough to count as full text. " * 20 + "</p>"


def rss(items, title="Example Letter"):
    entries = "".join(
        f"<item><title>{t}</title><link>{{web}}/p/{slug}</link><guid>{{web}}/p/{slug}</guid>"
        f"<pubDate>{date}</pubDate><content:encoded><![CDATA[{body}]]></content:encoded></item>"
        for t, slug, date, body in items
    )
    return (
        '<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel><title>{title}</title>{entries}</channel></rss>"
    )


FULL = LONG + '<img src="https://substackcdn.com/image.png">'
FIVE = [
    ("Fifth", "five", "Fri, 13 Nov 2026 10:00:00 GMT", FULL),
    ("Fourth", "four", "Thu, 12 Nov 2026 10:00:00 GMT", FULL),
    ("Third", "three", "Wed, 11 Nov 2026 10:00:00 GMT", FULL),
    ("Second", "two", "Tue, 10 Nov 2026 10:00:00 GMT", FULL),
    ("First", "one", "Mon, 09 Nov 2026 10:00:00 GMT", FULL),
]

YOUTUBE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
 <title>Videos</title>
 <author><name>Some Channel</name></author>
 <entry><id>yt:video:aaa</id><yt:videoId>aaa</yt:videoId><title>A long video</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=aaa"/><published>2026-11-13T17:00:00+00:00</published></entry>
 <entry><id>yt:video:bbb</id><yt:videoId>bbb</yt:videoId><title>A short</title>
  <link rel="alternate" href="https://www.youtube.com/shorts/bbb"/><published>2026-11-12T17:00:00+00:00</published></entry>
</feed>"""


class Web:
    """A stand-in for the internet: path -> (status, content type, body)."""

    def __init__(self):
        self.pages = {}
        web = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                status, content_type, body = web.pages.get(self.path, (404, "text/html", b"not found"))
                if isinstance(body, str):
                    body = body.replace("{web}", web.url).encode()
                self.send_response(status)
                if body.startswith(b"\x1f\x8b"):
                    self.send_header("Content-Encoding", "gzip")
                if status in (301, 302):
                    self.send_header("Location", body.decode())
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FeedTest(unittest.TestCase):
    def setUp(self):
        server.allow_private_hosts = True
        self.addCleanup(setattr, server, "allow_private_hosts", False)
        self.web = Web()
        self.addCleanup(self.web.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.opened = []
        self.start()

    def start(self, idle=0):
        self.server = server.FeedServer(0, Path(self.tmp.name) / "data", Path(self.tmp.name) / "colors.toml", idle=idle, opener=self.opened.append)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.stopped.set()
        self.server.server_close()
        self.server.store.close()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        all_headers = {"Host": f"localhost:{self.port}"}
        if method != "GET":
            all_headers["Origin"] = f"http://localhost:{self.port}"
        if body is not None:
            all_headers["Content-Type"] = "application/json"
            body = json.dumps(body).encode()
        all_headers.update(headers or {})
        conn.request(method, path, body=body, headers=all_headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response, data

    def api(self, method, path, body=None, status=200):
        response, data = self.request(method, path, body)
        self.assertEqual(response.status, status, data)
        return json.loads(data)

    def items(self):
        return self.api("GET", "/api/state")["items"]

    def clean_all(self, content=lambda item: f"<p>{item['title']}</p>"):
        """Stands in for the page: posts back cleaned HTML for every pending item."""
        for item in self.api("GET", "/api/pending")["items"]:
            self.api("POST", f"/api/items/{item['id']}/content", {"html": content(item), "removed": {}})

    def substack(self, items=FIVE):
        self.web.pages["/blog/"] = (200, "text/html", '<html><head><title>Letter</title><link rel="alternate" type="application/rss+xml" href="/blog/feed"></head><body>substackcdn.com</body></html>')
        self.web.pages["/blog/feed"] = (200, "application/xml", rss(items))

    # --- the page and its protections

    def test_serves_the_page_with_a_policy_that_blocks_outside_content(self):
        response, data = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        policy = response.getheader("Content-Security-Policy")
        self.assertIn("default-src 'self'", policy)
        self.assertIn("img-src 'self';", policy)
        self.assertIn(b"/app.mjs", data)

    def test_refuses_other_hosts_and_writes_from_other_sites(self):
        response, _ = self.request("GET", "/api/state", headers={"Host": "evil.example"})
        self.assertEqual(response.status, 403)
        response, _ = self.request("POST", "/api/read", {"ids": []}, headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status, 403)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/sync", headers={"Host": f"localhost:{self.port}"})  # no Origin
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()

    def test_fetching_refuses_local_addresses_outside_tests(self):
        self.web.pages["/feed"] = (200, "application/xml", rss(FIVE))
        server.allow_private_hosts = False
        for url in (self.web.url + "/feed", f"http://localhost:{self.port}/api/ping", "http://10.0.0.1/", "ftp://example.com/", "file:///etc/passwd"):
            with self.assertRaises(server.FetchError, msg=url):
                server.fetch(url, 1000)

    def test_the_address_is_checked_when_connecting(self):
        # No separate look-up before the request: the answer the connection
        # uses is the one checked, so a host name cannot switch to a local
        # address in between.
        port = int(self.web.url.rsplit(":", 1)[1])
        real = server.socket.getaddrinfo
        lookups = []

        def local(host, *args, **kwargs):
            if host == "rebind.example":
                lookups.append(host)
                return [(server.socket.AF_INET, server.socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
            return real(host, *args, **kwargs)

        server.socket.getaddrinfo = local
        self.addCleanup(setattr, server.socket, "getaddrinfo", real)
        server.allow_private_hosts = False
        with self.assertRaisesRegex(server.FetchError, "not a public address"):
            server.fetch(f"http://rebind.example:{port}/feed", 1000)
        self.assertEqual(lookups, ["rebind.example"])

    # --- sources and sync

    def test_a_new_feed_shows_its_three_newest_items_and_later_only_new_ones(self):
        self.substack()
        result = self.api("POST", "/api/sources", {"url": self.web.url + "/blog/"})
        self.assertEqual(result["source"]["kind"], "feed")
        self.assertEqual(result["source"]["platform"], "substack")
        self.assertEqual(result["source"]["title"], "Example Letter")
        self.assertEqual([i["title"] for i in self.items()], ["Fifth", "Fourth", "Third"])
        self.assertEqual(result["pending"], 3, "full text from the feed waits for cleanup")

        self.web.pages["/blog/feed"] = (200, "application/xml", rss([("Sixth", "six", "Sat, 14 Nov 2026 10:00:00 GMT", FULL)] + FIVE))
        result = self.api("POST", "/api/sync")
        self.assertEqual(result["added"], 1)
        self.assertEqual([i["title"] for i in self.items()], ["Sixth", "Fifth", "Fourth", "Third"])

        response, _ = self.request("POST", "/api/sources", {"url": self.web.url + "/blog/feed"})
        self.assertEqual(response.status, 502, "the same feed twice is refused")

    def test_short_feed_entries_download_the_page_without_scripts_and_styles(self):
        self.web.pages["/feed"] = (200, "application/rss+xml", rss([("Teaser", "teaser", "Fri, 13 Nov 2026 10:00:00 GMT", "<p>Only a teaser</p>")]))
        self.web.pages["/p/teaser"] = (200, "text/html; charset=utf-8", '<html><head><script>x()</script><style>p{}</style></head><body><p style="color:red">Whole article</p></body></html>')
        self.api("POST", "/api/sources", {"url": self.web.url + "/feed"})
        pending = self.api("GET", "/api/pending")["items"]
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0]["page"])
        raw = self.api("GET", f"/api/raw/{pending[0]['id']}")["html"]
        self.assertIn("Whole article", raw)
        self.assertNotIn("x()", raw)
        self.assertNotIn("p{}", raw)
        self.assertNotIn("color:red", raw)

    def test_an_article_that_moved_keeps_its_new_address_for_cleanup(self):
        self.web.pages["/feed"] = (200, "application/rss+xml", rss([("Moved", "moved", "Fri, 13 Nov 2026 10:00:00 GMT", "<p>Teaser</p>")]))
        self.web.pages["/p/moved"] = (301, "text/html", "/p/moved/")
        self.web.pages["/p/moved/"] = (200, "text/html", '<html><body><p>Article</p><img src="chart.png"></body></html>')
        self.api("POST", "/api/sources", {"url": self.web.url + "/feed"})
        pending = self.api("GET", "/api/pending")["items"]
        self.assertEqual(pending[0]["url"], self.web.url + "/p/moved/", "relative images resolve against this address")

    def test_article_addresses_with_non_ascii_characters(self):
        self.web.pages["/feed"] = (200, "application/rss+xml", rss([("Café", "café", "Fri, 13 Nov 2026 10:00:00 GMT", "<p>Teaser</p>")]))
        self.web.pages["/p/caf%C3%A9"] = (200, "text/html", "<html><body><p>Article</p></body></html>")
        self.api("POST", "/api/sources", {"url": self.web.url + "/feed"})
        self.assertEqual(self.api("POST", "/api/sync")["errors"], {})
        pending = self.api("GET", "/api/pending")["items"]
        self.assertIn("Article", self.api("GET", f"/api/raw/{pending[0]['id']}")["html"])

    def test_a_beehiiv_site_without_feed_uses_its_sitemap(self):
        self.web.pages["/"] = (200, "text/html", '<html><head><meta property="og:site_name" content="Letter AI"></head><body>beehiiv</body></html>')
        self.web.pages["/sitemap.xml"] = (
            200,
            "application/xml",
            '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>{web}/about</loc><lastmod>2026-11-14</lastmod></url>"
            "<url><loc>{web}/p/older-post</loc><lastmod>2026-11-12</lastmod></url>"
            "<url><loc>{web}/p/newest-post</loc><lastmod>2026-11-14</lastmod></url>"
            "</urlset>",
        )
        self.web.pages["/p/newest-post"] = (200, "text/html", '<html><head><meta property="og:title" content="The Newest Post"></head><body><p>Text</p></body></html>')
        self.web.pages["/p/older-post"] = (200, "text/html", "<html><head><title>Older &amp; wiser</title></head><body><p>Text</p></body></html>")
        result = self.api("POST", "/api/sources", {"url": self.web.url + "/"})
        self.assertEqual(result["source"]["kind"], "sitemap")
        self.assertEqual(result["source"]["title"], "Letter AI")
        self.assertEqual([i["title"] for i in self.items()], ["The Newest Post", "Older & wiser"])

    def test_no_feed_found(self):
        self.web.pages["/"] = (200, "text/html", "<html><body>Just a page</body></html>")
        response, data = self.request("POST", "/api/sources", {"url": self.web.url + "/"})
        self.assertEqual(response.status, 502)
        self.assertIn("No feed found", json.loads(data)["error"])

    def test_a_failing_source_keeps_its_items_and_shows_the_error(self):
        self.substack()
        self.api("POST", "/api/sources", {"url": self.web.url + "/blog/"})
        del self.web.pages["/blog/feed"]
        result = self.api("POST", "/api/sync")
        self.assertIn("HTTP 404", list(result["errors"].values())[0])
        state = self.api("GET", "/api/state")
        self.assertIn("HTTP 404", state["sources"][0]["error"])
        self.assertEqual(len(state["items"]), 3)

    def test_youtube_feeds_and_channel_pages(self):
        title, entries = server.parse_feed(YOUTUBE_FEED.encode(), "https://www.youtube.com/feeds/videos.xml")
        self.assertEqual(title, "Some Channel")
        self.assertEqual([(e.title, e.kind, e.url) for e in entries], [("A long video", "video", "https://www.youtube.com/watch?v=aaa")])
        page = '<html><head><link rel="canonical" href="https://www.youtube.com/channel/UCbRP3c757lWg9M-U7TyEkXA"></head><body>"channelId":"UCK_m2976Yvbx-TyDLw7n1WA"</body></html>'
        self.assertEqual(server.youtube_channel_id(page), "UCbRP3c757lWg9M-U7TyEkXA")
        self.assertTrue(server.is_youtube_video("https://www.youtube.com/watch?v=aaa"))
        self.assertFalse(server.is_youtube_video("https://www.youtube.com/@theo"))
        self.assertFalse(server.is_youtube_video("https://example.com/watch"))

    def test_atom_xhtml_content_keeps_plain_html_tags(self):
        feed = (
            '<feed xmlns="http://www.w3.org/2005/Atom"><title>Blog</title><entry><id>1</id><title>Post</title>'
            '<link href="https://blog.example/1"/><updated>2026-11-13T10:00:00Z</updated>'
            '<content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>Text with <a href="https://x.example/">a link</a></p>'
            '<img src="https://blog.example/a.png"/></div></content></entry></feed>'
        )
        _, entries = server.parse_feed(feed.encode(), "https://blog.example/feed")
        self.assertEqual(entries[0].content, '<div><p>Text with <a href="https://x.example/">a link</a></p><img src="https://blog.example/a.png" /></div>')

    def test_removing_a_source_during_a_sync_waits_for_the_sync(self):
        self.substack()
        source = self.api("POST", "/api/sources", {"url": self.web.url + "/blog/"})["source"]
        started, go_on = threading.Event(), threading.Event()
        real = server.fetch_entries

        def slow(source):
            started.set()
            go_on.wait(5)
            return real(source)

        server.fetch_entries = slow
        self.addCleanup(setattr, server, "fetch_entries", real)
        results = {}
        sync = threading.Thread(target=lambda: results.setdefault("sync", self.request("POST", "/api/sync")[0].status))
        sync.start()
        self.assertTrue(started.wait(5))
        delete = threading.Thread(target=lambda: results.setdefault("delete", self.request("DELETE", f"/api/sources/{source['id']}")[0].status))
        delete.start()
        time.sleep(0.3)
        go_on.set()
        sync.join(10)
        delete.join(10)
        self.assertEqual(results, {"sync": 200, "delete": 200})
        state = self.api("GET", "/api/state")
        self.assertEqual((state["sources"], state["items"]), ([], []))

    def test_compressed_responses_are_limited_by_their_unpacked_size(self):
        self.web.pages["/small.gz"] = (200, "text/html", gzip.compress(b"<p>hello</p>"))
        self.web.pages["/bomb.gz"] = (200, "text/html", gzip.compress(b"0" * 5_000_000))
        self.assertEqual(server.fetch(self.web.url + "/small.gz", 1000)[2], b"<p>hello</p>")
        with self.assertRaisesRegex(server.FetchError, "Too large"):
            server.fetch(self.web.url + "/bomb.gz", 1_000_000)

    def test_item_numbers_are_not_reused_after_a_source_is_removed(self):
        # Image addresses contain the item number and may be cached.
        self.substack()
        source = self.api("POST", "/api/sources", {"url": self.web.url + "/blog/"})["source"]
        before = max(i["id"] for i in self.items())
        self.api("DELETE", f"/api/sources/{source['id']}")
        self.api("POST", "/api/sources", {"url": self.web.url + "/blog/"})
        self.assertGreater(min(i["id"] for i in self.items()), before)

    # --- articles

    def test_saved_articles_keep_only_local_copies_of_their_images(self):
        self.substack()
        self.web.pages["/a.png"] = (200, "image/png", PNG)
        self.web.pages["/fake.png"] = (200, "image/png", b"<svg onload=alert(1)>")
        self.api("POST", "/api/sources", {"url": self.web.url + "/blog/"})
        content = (
            f'<p>Text</p><img src="{self.web.url}/a.png" alt="A &quot;chart&quot;">'
            f'<img src="{self.web.url}/missing.png"><img src="{self.web.url}/fake.png"><img src="data:image/png;base64,AA">'
        )
        self.clean_all(lambda item: content)
        item = self.items()[0]
        saved = self.api("GET", f"/api/items/{item['id']}")
        self.assertEqual(saved["state"], "ready")
        self.assertEqual(saved["content"], f'<p>Text</p><img src="/img/{item["id"]}/0.png" alt="A &quot;chart&quot;" loading="lazy">')
        response, data = self.request("GET", f"/img/{item['id']}/0.png")
        self.assertEqual((response.status, response.getheader("Content-Type"), data), (200, "image/png", PNG))
        self.assertEqual(self.api("GET", "/api/pending")["items"], [])

    def test_a_cleanup_error_marks_the_article_failed(self):
        self.substack()
        self.api("POST", "/api/sources", {"url": self.web.url + "/blog/"})
        item = self.api("GET", "/api/pending")["items"][0]
        self.api("POST", f"/api/items/{item['id']}/content", {"error": "No article text found"})
        saved = self.api("GET", f"/api/items/{item['id']}")
        self.assertEqual((saved["state"], saved["error"]), ("failed", "No article text found"))

    def test_read_unread_and_mark_all(self):
        self.substack()
        self.api("POST", "/api/sources", {"url": self.web.url + "/blog/"})
        first, second, third = self.items()
        self.api("POST", f"/api/items/{first['id']}/read", {"read": True})
        self.assertIsNotNone(self.items()[0]["read_at"])
        self.api("POST", f"/api/items/{first['id']}/read", {"read": False})
        self.assertIsNone(self.items()[0]["read_at"])
        self.api("POST", "/api/read", {"ids": [second["id"], third["id"]]})
        self.assertEqual([i["read_at"] is not None for i in self.items()], [False, True, True])

    def test_old_read_items_are_deleted_with_their_images_and_not_added_again(self):
        self.substack()
        self.web.pages["/a.png"] = (200, "image/png", PNG)
        self.api("POST", "/api/sources", {"url": self.web.url + "/blog/"})
        self.clean_all(lambda item: f'<p>Text</p><img src="{self.web.url}/a.png">')
        item = self.items()[0]
        images = self.server.store.images / str(item["id"])
        self.assertTrue(images.exists())
        self.server.store.run("UPDATE items SET read_at = ? WHERE id = ?", (int(time.time()) - 31 * 86400, item["id"]))
        self.api("POST", "/api/sync")
        self.assertNotIn(item["id"], [i["id"] for i in self.items()])
        self.assertFalse(images.exists())
        self.assertEqual(self.api("POST", "/api/sync")["added"], 0)

    def test_removing_a_source_removes_its_items_and_images(self):
        self.substack()
        self.web.pages["/a.png"] = (200, "image/png", PNG)
        source = self.api("POST", "/api/sources", {"url": self.web.url + "/blog/"})["source"]
        self.clean_all(lambda item: f'<img src="{self.web.url}/a.png"><p>Text</p>')
        self.assertTrue(any(self.server.store.images.iterdir()))
        self.api("DELETE", f"/api/sources/{source['id']}")
        state = self.api("GET", "/api/state")
        self.assertEqual((state["sources"], state["items"]), ([], []))
        self.assertFalse(any(self.server.store.images.iterdir()))

    def test_opens_only_web_and_email_addresses(self):
        self.api("POST", "/api/open", {"url": "https://www.youtube.com/watch?v=aaa"})
        self.api("POST", "/api/open", {"url": "mailto:team@example.com"})
        self.assertEqual(self.opened, ["https://www.youtube.com/watch?v=aaa", "mailto:team@example.com"])
        for url in ("javascript:alert(1)", "file:///etc/passwd", "https://x.example/ --flag", "mailto:a@b.example --flag"):
            self.api("POST", "/api/open", {"url": url}, status=400)
        self.assertEqual(len(self.opened), 2)

    def test_x_links_open_in_the_x_app(self):
        def opened(url):
            with mock.patch.object(server.subprocess, "Popen") as popen:
                where = server.open_url(url)
            return popen.call_args.args[0], where

        for url in ("https://x.com/someone/status/123", "https://www.x.com/someone", "https://twitter.com/someone/status/123"):
            self.assertEqual(opened(url), (["omarchy-launch-webapp", url], "X"))
        for url in ("https://x.com.example/someone", "https://notx.com/", "https://help.x.com/en"):
            self.assertEqual(opened(url)[1], "the browser", url)
        self.assertEqual(opened("https://www.youtube.com/watch?v=aaa"), (["omarchy-launch-webapp", "https://www.youtube.com/watch?v=aaa"], "YouTube"))
        self.assertEqual(opened("mailto:team@example.com"), (["xdg-open", "mailto:team@example.com"], "the mail app"))

    # --- running only while the window is open

    def test_stops_soon_after_the_window_says_goodbye(self):
        self.stop()
        bye = server.BYE_SECONDS
        server.BYE_SECONDS = 0.2
        self.addCleanup(setattr, server, "BYE_SECONDS", bye)
        self.start(idle=60)
        threading.Thread(target=self.server.watch, daemon=True).start()
        self.api("POST", "/api/bye")
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())

    def test_setup_stops_the_server_at_once_even_with_a_window_open(self):
        self.stop()
        self.start(idle=60)
        threading.Thread(target=self.server.watch, daemon=True).start()
        self.api("POST", "/api/stop")
        try:
            self.request("GET", "/api/ping")  # the open window's heartbeat
        except OSError:
            pass
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())

    def test_stops_when_no_window_asks_anything(self):
        self.stop()
        self.start(idle=1)
        threading.Thread(target=self.server.watch, daemon=True).start()
        self.api("GET", "/api/ping")
        self.thread.join(6)
        self.assertFalse(self.thread.is_alive())

    def test_theme_colors_come_from_omarchy(self):
        (Path(self.tmp.name) / "colors.toml").write_text('background = "#ffffff"\naccent = "#123456"\n')
        response, data = self.request("GET", "/theme.css")
        css = data.decode()
        self.assertIn("--accent: #123456;", css)
        self.assertIn("color-scheme: light;", css)


if __name__ == "__main__":
    unittest.main()
