// Feed: the three views (Feed, Reader, Settings) and the sync. The server
// fetches; this page cleans each new article (clean.mjs) and posts it back.
import { cleanArticle } from './clean.mjs';
import { startUpdates } from './update.mjs';

const state = {
  sources: [],
  items: [],
  dataDir: '',
  keepReadDays: 30,
  view: 'feed', // feed | reader | settings
  show: 'unread', // unread | all
  source: 0, // 0: all sources
  selected: 0,
  reader: null, // the open article, with its content
  feedScroll: 0,
  syncing: false,
  adding: false,
  status: '',
};

const $ = (id) => document.getElementById(id);
const main = $('main');
let updates = null; // the update button (update.mjs)

// --- server -------------------------------------------------------------------------

async function api(method, path, body) {
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    // Unless it restarts with a release: then the page reloads from the new one.
    if (!updates?.restarting()) banner("Feed's server has stopped. Close this window and open Feed again.");
    throw new Error('The server has stopped');
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

async function load() {
  const data = await api('GET', '/api/state');
  state.sources = data.sources;
  state.items = data.items;
  state.dataDir = data.dataDir;
  state.keepReadDays = data.keepReadDays;
  if (state.source && !state.sources.some((s) => s.id === state.source)) state.source = 0;
}

// Keeps the server running while this window is open; it stops soon after
// the window closes.
setInterval(() => api('GET', '/api/ping').catch(() => {}), 30_000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) api('GET', '/api/ping').catch(() => {});
});
addEventListener('pagehide', () => navigator.sendBeacon('/api/bye'));

// --- sync -----------------------------------------------------------------------------

async function sync() {
  if (state.syncing) return;
  state.syncing = true;
  setStatus('Syncing…');
  try {
    await api('POST', '/api/sync');
    await load();
    render();
    await cleanPending();
    await load();
    await refreshReader();
    setStatus(updated());
  } catch (error) {
    setStatus(error.message);
  } finally {
    state.syncing = false;
    render();
  }
}

// An article opened while it was still being saved shows its text once
// the sync has saved it.
async function refreshReader() {
  if (state.view !== 'reader' || state.reader?.state === 'ready') return;
  const open = state.reader;
  const item = state.items.find((i) => i.id === open.id);
  if (!item || item.state === open.state) return;
  const fresh = item.state === 'ready' ? await api('GET', `/api/items/${item.id}`) : item;
  // Unless another article was opened in the meantime.
  if (state.view === 'reader' && state.reader === open) state.reader = fresh;
}

// Cleans the articles the server downloaded, three at a time.
async function cleanPending() {
  const { items } = await api('GET', '/api/pending');
  if (!items.length) return;
  let done = 0;
  const queue = [...items];
  const work = async () => {
    while (queue.length) {
      const item = queue.shift();
      let body;
      try {
        const raw = await api('GET', `/api/raw/${item.id}`);
        const doc = new DOMParser().parseFromString(raw.html, 'text/html');
        body = cleanArticle(doc, item);
        if (!body.html.trim()) body = { error: 'No article text found' };
      } catch (error) {
        body = { error: `Cleanup failed: ${error.message}` };
      }
      await api('POST', `/api/items/${item.id}/content`, body).catch(() => {});
      setStatus(`Saving articles ${++done}/${items.length}`);
    }
  };
  await Promise.all([work(), work(), work()]);
}

// --- actions --------------------------------------------------------------------------

function visible() {
  return state.items.filter((i) => (state.show === 'all' || !i.read_at) && (!state.source || i.source_id === state.source));
}

function sourceOf(item) {
  return state.sources.find((s) => s.id === item.source_id);
}

async function openItem(item) {
  if (!item) return;
  if (item.kind === 'video') {
    await openUrl(item.url);
    return;
  }
  state.feedScroll = scrollY;
  let full = item;
  if (item.state === 'ready') {
    try {
      full = await api('GET', `/api/items/${item.id}`);
    } catch (error) {
      toast(error.message);
      return;
    }
  }
  state.reader = full;
  state.view = 'reader';
  render();
  scrollTo(0, 0);
}

async function openUrl(url) {
  try {
    const { opened_in } = await api('POST', '/api/open', { url });
    toast(`Opening in ${opened_in || 'the browser'}…`);
  } catch (error) {
    toast(error.message);
  }
}

async function setRead(item, read) {
  try {
    await api('POST', `/api/items/${item.id}/read`, { read });
  } catch (error) {
    toast(error.message);
    return;
  }
  const stamp = read ? Math.floor(Date.now() / 1000) : null;
  for (const i of [item, state.items.find((x) => x.id === item.id), state.reader?.id === item.id && state.reader]) {
    if (i) i.read_at = stamp;
  }
  render();
}

async function markAllRead() {
  const ids = visible().filter((i) => !i.read_at).map((i) => i.id);
  if (!ids.length || !confirm(`Mark ${ids.length} ${ids.length === 1 ? 'item' : 'items'} as read?`)) return;
  try {
    await api('POST', '/api/read', { ids });
    await load();
  } catch (error) {
    toast(error.message);
  }
  render();
}

async function addSource(address) {
  state.adding = true;
  render();
  try {
    const result = await api('POST', '/api/sources', { url: address });
    await cleanPending();
    await load();
    setStatus(updated());
    toast(`Added ${result.source.title}`);
    state.adding = false;
    render();
    return true;
  } catch (error) {
    state.adding = false;
    render();
    toast(error.message, 6000);
    return false;
  }
}

async function removeSource(source) {
  if (!confirm(`Remove ${source.title} and its items?`)) return;
  try {
    await api('DELETE', `/api/sources/${source.id}`);
    await load();
  } catch (error) {
    toast(error.message);
  }
  render();
}

function show(view) {
  if (view === 'feed' && state.view === 'reader') {
    state.view = 'feed';
    render();
    scrollTo(0, state.feedScroll);
    return;
  }
  state.view = view;
  render();
  scrollTo(0, 0);
}

// --- rendering ------------------------------------------------------------------------

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else if (key === 'class') el.className = value;
    else el.setAttribute(key, value === true ? '' : value);
  }
  el.append(...children.flat().filter((c) => c != null && c !== false));
  return el;
}

function render() {
  const unread = state.items.filter((i) => !i.read_at).length;
  document.title = unread ? `Feed (${unread})` : 'Feed';
  $('nav').replaceChildren(
    h('button', { class: state.view !== 'settings' ? 'tab active' : 'tab', onclick: () => show('feed'), title: 'Ctrl+1' }, 'Feed', unread ? h('span', { class: 'count' }, String(unread)) : null),
    h('button', { class: state.view === 'settings' ? 'tab active' : 'tab', onclick: () => show('settings'), title: 'Ctrl+2' }, 'Settings'),
  );
  $('sync').disabled = state.syncing;
  $('status').textContent = state.status;
  if (state.view === 'feed') renderFeed();
  else if (state.view === 'reader') renderReader();
  else renderSettings();
}

// "Updated 10:42", and how many sources failed in their last sync.
function updated() {
  const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const failed = state.sources.filter((s) => s.error).length;
  return `Updated ${time}` + (failed ? ` · ${failed} ${failed === 1 ? 'source' : 'sources'} failed` : '');
}

function setStatus(text) {
  state.status = text;
  $('status').textContent = text;
}

function renderFeed() {
  const items = visible();
  state.selected = Math.max(0, Math.min(state.selected, items.length - 1));
  const unreadHere = items.filter((i) => !i.read_at).length;
  const toolbar = h(
    'div',
    { class: 'toolbar' },
    h('div', { class: 'switch' },
      h('button', { class: state.show === 'unread' ? 'on' : '', onclick: () => { state.show = 'unread'; render(); }, title: 'u' }, 'Unread'),
      h('button', { class: state.show === 'all' ? 'on' : '', onclick: () => { state.show = 'all'; render(); }, title: 'u' }, 'All'),
    ),
    state.sources.length > 1
      ? h('select', { 'aria-label': 'Source', onchange: (e) => { state.source = Number(e.target.value); state.selected = 0; render(); } },
          h('option', { value: '0' }, 'All sources'),
          state.sources.map((s) => h('option', { value: String(s.id), selected: s.id === state.source }, s.title)))
      : null,
    h('span', { class: 'grow' }),
    h('button', { onclick: markAllRead, disabled: !unreadHere }, 'Mark all read'),
  );

  let body;
  if (!state.sources.length) {
    body = h('div', { class: 'empty' },
      h('p', {}, 'No sources yet.'),
      h('p', { class: 'muted' }, 'Add a YouTube channel, a Substack or any site with an RSS feed in ', h('a', { href: '#', onclick: (e) => { e.preventDefault(); show('settings'); } }, 'Settings'), ' (Ctrl+2).'));
  } else if (!items.length) {
    body = h('div', { class: 'empty' }, h('p', {}, state.show === 'unread' ? 'Nothing unread.' : 'No items yet.'),
      h('p', { class: 'muted' }, state.syncing ? 'Syncing…' : 'Press r to sync.'));
  } else {
    body = h('ul', { class: 'items' }, items.map((item, index) => feedRow(item, index)));
  }
  main.replaceChildren(toolbar, body, h('p', { class: 'hint' }, 'j/k move · enter open · m read · u unread/all · r sync'));
  main.querySelector('.items .selected')?.scrollIntoView({ block: 'nearest' });
}

function feedRow(item, index) {
  const source = sourceOf(item);
  const flag = item.kind === 'video' ? null
    : item.state === 'failed' ? h('span', { class: 'flag bad', title: item.error || '' }, 'not saved')
    : item.state !== 'ready' ? h('span', { class: 'flag' }, 'saving')
    : null;
  return h(
    'li',
    {
      class: ['item', item.read_at ? 'read' : '', index === state.selected ? 'selected' : ''].join(' ').trim(),
      onclick: () => { state.selected = index; openItem(item); },
    },
    h('span', { class: item.kind === 'video' ? 'kind video' : 'kind', 'aria-label': item.kind }, item.kind === 'video' ? '▶' : '¶'),
    h('span', { class: 'title' }, item.title),
    flag,
    h('span', { class: 'meta' }, `${source ? source.title : ''} · ${when(item.published || item.added_at)}`),
  );
}

function renderReader() {
  const item = state.reader;
  const source = sourceOf(item);
  const bar = h('div', { class: 'toolbar reader-bar' },
    h('button', { onclick: () => show('feed'), title: 'Esc' }, '← Feed'),
    h('span', { class: 'grow' }),
    h('button', { onclick: () => setRead(item, !item.read_at), title: 'm' }, item.read_at ? 'Mark unread' : 'Mark read'),
    h('button', { onclick: () => openUrl(item.url), title: 'o' }, 'Open original'),
  );
  const words = item.content ? item.content.replace(/<[^>]+>/g, ' ').split(/\s+/).filter(Boolean).length : 0;
  const meta = [source?.title, when(item.published || item.added_at, true), words ? `${Math.max(1, Math.round(words / 230))} min read` : null].filter(Boolean).join(' · ');
  const article = h('article', { class: 'reader' },
    h('p', { class: 'meta' }, meta),
    h('h1', {}, item.title),
    removedLine(item.removed));
  if (item.state === 'ready') {
    const content = h('div', { class: 'content' });
    content.innerHTML = item.content; // cleaned by clean.mjs; images are local copies
    content.addEventListener('click', (event) => {
      const link = event.target.closest('a[href]');
      if (!link) return;
      event.preventDefault();
      openUrl(link.href);
    });
    article.append(content);
  } else if (item.state === 'failed') {
    article.append(h('p', { class: 'notice bad' }, `This article could not be saved: ${item.error || 'unknown error'}.`));
  } else {
    article.append(h('p', { class: 'notice' }, 'This article is saved during the next sync (r).'));
  }
  main.replaceChildren(bar, article);
}

const REMOVED_NAMES = {
  ads: ['ad', 'ads'],
  promos: ['promotion', 'promotions'],
  polls: ['poll', 'polls'],
  embeds: ['embed', 'embeds'],
  'header image': ['header image', 'header images'],
};

function removedLine(removed) {
  const parts = Object.entries(removed || {}).filter(([, n]) => n > 0)
    .map(([what, n]) => { const [one, many] = REMOVED_NAMES[what] || [what, what]; return n === 1 && what === 'header image' ? one : `${n} ${n === 1 ? one : many}`; });
  return parts.length ? h('p', { class: 'removed' }, `Removed: ${parts.join(', ')}`) : null;
}

function renderSettings() {
  const input = h('input', { type: 'text', name: 'url', placeholder: 'https://… a YouTube channel, a Substack or a site with an RSS feed', 'aria-label': 'Address', disabled: state.adding, autocomplete: 'off', spellcheck: 'false' });
  const form = h('form', { class: 'add', onsubmit: async (e) => {
    e.preventDefault();
    const value = input.value.trim();
    if (value && (await addSource(value))) input.value = '';
  } },
    input,
    h('button', { type: 'submit', disabled: state.adding }, state.adding ? 'Adding…' : 'Add'));

  const sources = state.sources.length
    ? h('ul', { class: 'sources' }, state.sources.map((s) => h('li', {},
        h('div', { class: 'grow' },
          h('div', { class: 'name' }, s.title),
          h('div', { class: s.error ? 'meta bad' : 'meta' },
            [s.kind === 'youtube' ? 'YouTube' : s.site, KIND_NAMES[s.kind],
              s.error ? `failed: ${s.error}` : s.synced_at ? `synced ${when(s.synced_at, true)}` : 'not synced yet'].join(' · '))),
        h('button', { onclick: () => removeSource(s) }, 'Remove'))))
    : h('p', { class: 'muted' }, 'No sources yet.');

  main.replaceChildren(h('div', { class: 'settings' },
    h('h2', {}, 'Sources'),
    sources,
    form,
    h('p', { class: 'muted small' }, 'A new source starts with its three newest items. Videos open in the YouTube app; articles are saved with their images and read here, without ads.'),
    h('h2', {}, 'Data'),
    h('p', {}, 'Saved in ', h('code', {}, state.dataDir), `. Read items are deleted ${state.keepReadDays} days after they were marked read.`),
    h('h2', {}, 'Keys'),
    h('table', { class: 'keys' }, KEYS.map(([key, what]) => h('tr', {}, h('td', {}, h('kbd', {}, key)), h('td', {}, what)))),
  ));
  if (!state.sources.length && !state.adding) input.focus();
}

const KIND_NAMES = { feed: 'RSS feed', youtube: 'channel feed', sitemap: 'site map (no feed)' };
const KEYS = [
  ['Ctrl+1 / Ctrl+2', 'Feed / Settings'],
  ['j / k', 'Next / previous item (also ↓ / ↑)'],
  ['Enter / o', 'Open the item (videos open in YouTube)'],
  ['m', 'Mark read or unread'],
  ['u', 'Unread or all items'],
  ['r', 'Sync'],
  ['Esc', 'Back to the feed'],
  ['o (reader)', 'Open the original in the browser'],
];

function when(seconds, long = false) {
  if (!seconds) return '';
  const date = new Date(seconds * 1000);
  const ago = (Date.now() - date) / 1000;
  if (ago >= 0 && ago < 3600) return `${Math.max(1, Math.round(ago / 60))} min${long ? ' ago' : ''}`;
  if (ago >= 0 && ago < 86400) return `${Math.round(ago / 3600)} h${long ? ' ago' : ''}`;
  if (ago >= 0 && ago < 7 * 86400) return `${Math.round(ago / 86400)} d${long ? ' ago' : ''}`;
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return date.toLocaleDateString([], { day: 'numeric', month: 'short', year: sameYear ? undefined : 'numeric' });
}

let toastTimer;
function toast(text, ms = 2500) {
  const el = $('toast');
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ms);
}

function banner(text) {
  $('banner').textContent = text;
  $('banner').hidden = false;
}

// --- keys -----------------------------------------------------------------------------

addEventListener('keydown', (event) => {
  if (event.ctrlKey && !event.altKey && !event.metaKey && !event.shiftKey && (event.key === '1' || event.key === '2')) {
    event.preventDefault();
    show(event.key === '1' ? 'feed' : 'settings');
    return;
  }
  if (event.ctrlKey || event.altKey || event.metaKey || event.target.closest('input, select, textarea')) return;
  const key = event.key;
  if (key === 'r') return void sync();
  if (state.view === 'feed') {
    const items = visible();
    const item = items[state.selected];
    if (key === 'j' || key === 'ArrowDown') moveSelection(1, items.length);
    else if (key === 'k' || key === 'ArrowUp') moveSelection(-1, items.length);
    else if (key === 'Home') moveSelection(-Infinity, items.length);
    else if (key === 'End') moveSelection(Infinity, items.length);
    else if ((key === 'Enter' || key === 'o') && item) openItem(item);
    else if (key === 'm' && item) setRead(item, !item.read_at);
    else if (key === 'u') { state.show = state.show === 'unread' ? 'all' : 'unread'; render(); }
    else return;
    event.preventDefault();
  } else if (state.view === 'reader') {
    const item = state.reader;
    if (key === 'Escape') show('feed');
    else if (key === 'm') setRead(item, !item.read_at);
    else if (key === 'o') openUrl(item.url);
    else if (key === 'j') scrollBy({ top: 120 });
    else if (key === 'k') scrollBy({ top: -120 });
    else return;
    event.preventDefault();
  } else if (key === 'Escape') {
    show('feed');
  }
});

function moveSelection(step, length) {
  if (!length) return;
  state.selected = Math.max(0, Math.min(length - 1, state.selected + step));
  render();
}

$('sync').addEventListener('click', () => sync());

// --- start ----------------------------------------------------------------------------

updates = startUpdates($('update'), { busy: () => state.syncing || state.adding });

(async () => {
  try {
    await load();
  } catch (error) {
    main.replaceChildren(h('p', { class: 'pad bad' }, error.message));
    return;
  }
  render();
  sync();
})();
