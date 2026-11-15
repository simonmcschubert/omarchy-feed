// The update button in the top bar. The server looks for new releases of
// Feed (server.py, Updater); this shows what it found, and asks it to check,
// download and install. The button is always there: a circle of arrows that
// checks for updates when clicked. When there is a release it becomes
// Update, then Restart to update.

const POLL = 60 * 1000; // ms between asking the server for news
const RESTART_POLL = 1000; // ms between asks while the server restarts
const RESTART_TRIES = 120; // then give up waiting for it
const CONFIRM = 4000; // ms that "Up to date" shows after a check

// What the button shows: icon, text, tone (accent, red or none), tip, and
// busy (the icon turns). server is GET /api/update (null before the first
// answer); asked is the last check the button asked for.
export function updateButton(server, asked) {
  const s = server ?? { status: '', version: '', error: '', installed: '', checking: false };
  const name = `Feed ${s.version}`;
  switch (s.status) {
    case 'available':
      return { icon: 'download', text: 'Update', tone: 'accent', tip: `${name} is available. Click to download it.` };
    case 'downloading':
      return { icon: 'download', text: 'Downloading', tone: 'accent', tip: `Downloading ${name}.` };
    case 'downloaded':
      return { icon: 'restart', text: 'Restart to update', tone: 'accent',
        tip: `${name} is downloaded. Click to restart Feed with it. Your sources and articles stay as they are.` };
    case 'installing':
      return { icon: 'restart', text: 'Updating', tone: 'accent', tip: `Installing ${name} and restarting Feed.`, busy: true };
    case 'failed':
      return { icon: 'alert', text: 'Update failed', tone: 'red', tip: `${sentence(s.error || `can't update to ${name}`)} Click to try again.` };
  }
  const installed = s.installed ? `Feed ${s.installed}` : 'Feed';
  if (asked.checking || s.checking) return { icon: 'refresh', text: '', tone: '', tip: 'Checking for updates…', busy: true };
  if (asked.error) return { icon: 'alert', text: "Can't check", tone: 'red', tip: `Can't check for updates: ${asked.error}. Click to try again.` };
  if (asked.current) return { icon: 'refresh', text: 'Up to date', tone: '', tip: `${installed} is up to date.` };
  return { icon: 'refresh', text: '', tone: '', tip: `${s.installed ? `${installed} is installed. ` : ''}Click to check for updates.` };
}

function sentence(text) {
  const trimmed = text.trim().replace(/\.$/, '');
  return `${trimmed.charAt(0).toUpperCase()}${trimmed.slice(1)}.`;
}

// 24×24, drawn with a 2px stroke in the text color.
const ICONS = {
  refresh: ['M4 12A8 8 0 0 1 18.93 8', 'M19 3v5h-5', 'M20 12A8 8 0 0 1 5.07 16', 'M5 21v-5h5'],
  download: ['M12 4v11', 'M7 10l5 5 5-5', 'M5 20h14'],
  restart: ['M20 12A8 8 0 1 1 18.93 8', 'M19 3v5h-5'],
  alert: ['M12 4a8 8 0 1 0 0 16a8 8 0 1 0 0-16', 'M12 8v5', 'M12 16.5v.01'],
};

function icon(name) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  for (const d of ICONS[name]) {
    const path = document.createElementNS(ns, 'path');
    path.setAttribute('d', d);
    svg.append(path);
  }
  return svg;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Shows the button in root. busy() tells whether the page is syncing or
// adding a source: a restart waits for it. Returns restarting(), which
// tells whether the server is away because it restarts with a release.
export function startUpdates(root, { busy }) {
  let server = null;
  const asked = { checking: false, error: '', current: false };
  let confirmed = null;
  let restarting = false;

  async function call(method, body) {
    try {
      const response = await fetch('/api/update', {
        method,
        headers: body ? { 'Content-Type': 'application/json' } : {},
        body: body ? JSON.stringify(body) : undefined,
      });
      const json = await response.json();
      return response.ok ? { json } : { error: json?.error ?? `HTTP ${response.status}` };
    } catch {
      return { error: "Feed's server does not answer" };
    }
  }

  async function reload() {
    restarting = true;
    while (busy()) await sleep(100);
    location.reload();
  }

  function render() {
    const b = updateButton(server, asked);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = ['update-button', b.tone, b.busy ? 'busy' : ''].filter(Boolean).join(' ');
    button.title = b.tip;
    button.setAttribute('aria-label', b.text ? `${b.text}. ${b.tip}` : b.tip);
    if (b.busy) button.setAttribute('aria-busy', 'true');
    button.append(icon(b.icon));
    if (b.text) button.append(Object.assign(document.createElement('span'), { textContent: b.text }));
    button.addEventListener('click', act);
    const hadFocus = root.contains(document.activeElement);
    root.replaceChildren(button);
    if (hadFocus) button.focus();
  }

  async function refresh() {
    if (restarting) return;
    const { json } = await call('GET');
    if (!json || restarting) return;
    if (server && json.boot !== server.boot && json.installed !== server.installed) {
      // A terminal installed a release: this page is old.
      await reload();
      return;
    }
    server = json;
    render();
  }

  async function check() {
    if (asked.checking) return;
    Object.assign(asked, { checking: true, error: '', current: false });
    clearTimeout(confirmed);
    render();
    const { json, error } = await call('POST', { action: 'check' });
    asked.checking = false;
    if (json) server = json;
    asked.error = error ?? json.checkError;
    if (!asked.error && server.status === '') {
      asked.current = true;
      confirmed = setTimeout(() => {
        asked.current = false;
        render();
      }, CONFIRM);
    }
    render();
  }

  async function download() {
    server = { ...server, status: 'downloading' };
    render();
    const { json, error } = await call('POST', { action: 'download' });
    server = json ?? { ...server, status: 'failed', error: `can't download Feed ${server.version}: ${error}` };
    render();
  }

  // Installing stops the server and starts the new one; the page reloads
  // from it once it answers.
  async function install() {
    restarting = true;
    const boot = server.boot;
    server = { ...server, status: 'installing' };
    render();
    while (busy()) await sleep(100);
    const { json } = await call('POST', { action: 'install' });
    if (json) server = json;
    let misses = 0;
    while (server.status === 'installing') {
      await sleep(RESTART_POLL);
      const { json } = await call('GET');
      if (json?.boot && json.boot !== boot) {
        await reload();
        return;
      }
      if (json) {
        // Still the old server: the install is under way, or failed before
        // the restart, and then the button says why.
        server = json;
        render();
      } else if (++misses >= RESTART_TRIES) {
        server = { ...server, status: 'failed', error: 'Feed did not start again. See ~/.local/state/omarchy-feed/server.log' };
        render();
      }
    }
    restarting = false;
  }

  function act() {
    const status = server?.status ?? '';
    if (status === '') check();
    else if (status === 'available' || status === 'failed') download();
    else if (status === 'downloaded') install();
  }

  render();
  refresh();
  setInterval(() => {
    if (!document.hidden) refresh();
  }, POLL);
  addEventListener('focus', refresh);
  return { restarting: () => restarting };
}
