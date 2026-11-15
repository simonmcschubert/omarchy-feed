// Tests for the update script against real git repositories: an "origin"
// with releases and an installed checkout cloned from it. curl, setsid and
// notify-send are stand-ins that log their calls; a file named "server"
// stands for a Feed server listening on its port. Run: node --test
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { spawnSync } from 'node:child_process';
import { chmodSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const SCRIPT = new URL('../update', import.meta.url).pathname;

function sandbox(t) {
  const root = mkdtempSync(join(tmpdir(), 'feed-update-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));

  const bin = join(root, 'bin');
  const log = join(root, 'calls.log');
  const home = join(root, 'home');
  const app = join(root, 'app'); // the installed checkout
  const server = join(root, 'server');
  mkdirSync(bin);
  const stub = (name, body) => {
    writeFileSync(join(bin, name), `#!/bin/bash\n${body}\n`);
    chmodSync(join(bin, name), 0o755);
  };
  const logged = (name, body = '') => stub(name, `echo "${name} $*" >> "${log}"\n${body}`);
  // Answers for the server while the file "server" exists. A server that
  // does not stop leaves a file "stuck".
  logged('curl', `
out=/dev/stdout
args=("$@")
for ((i = 0; i < \${#args[@]}; i++)); do [[ \${args[i]} != -o ]] || out=\${args[i + 1]}; done
[[ -e "${server}" ]] || exit 7
case \${args[-1]} in
  */api/ping) echo '{"app": "omarchy-feed"}' > "$out" ;;
  */api/stop) [[ -e "${root}/stuck" ]] || rm "${server}"; echo '{"ok": true}' > "$out" ;;
esac`);
  // Starts the checkout's server, unless the checkout has a file "broken":
  // then the server fails as it starts.
  logged('setsid', `[[ -e "${app}/broken" ]] && exit 1; touch "${server}"`);
  logged('notify-send');
  // Short waits keep the tests quick; the server starts in the background.
  stub('sleep', 'exec /bin/sleep 0.01');

  const env = {
    ...process.env,
    PATH: `${bin}:${process.env.PATH}`,
    HOME: home,
    XDG_STATE_HOME: join(root, 'state'),
    GIT_CONFIG_GLOBAL: '/dev/null',
    GIT_CONFIG_NOSYSTEM: '1',
    GIT_AUTHOR_NAME: 'Test', GIT_AUTHOR_EMAIL: 'test@example.com',
    GIT_COMMITTER_NAME: 'Test', GIT_COMMITTER_EMAIL: 'test@example.com',
  };
  const git = (cwd, ...args) => {
    const r = spawnSync('git', args, { cwd, env, encoding: 'utf8' });
    assert.equal(r.status, 0, `git ${args.join(' ')}: ${r.stderr}`);
    return r.stdout.trim();
  };

  const origin = join(root, 'origin');
  mkdirSync(origin);
  git(origin, 'init', '--quiet', '--initial-branch=main');
  // Each commit carries the script, as every real release does.
  const commit = (message, files = {}) => {
    copyFileSync(SCRIPT, join(origin, 'update'));
    writeFileSync(join(origin, 'server.py'), '# server\n');
    writeFileSync(join(origin, 'omarchy-feed'), '# launcher\n');
    for (const [name, text] of Object.entries(files)) writeFileSync(join(origin, name), text);
    git(origin, 'add', '-A');
    git(origin, 'commit', '--quiet', '--allow-empty', '-m', message);
    return git(origin, 'rev-parse', 'HEAD');
  };
  const release = (version, files) => {
    commit(`Release ${version}`, files);
    git(origin, 'tag', '--annotate', `v${version}`, '--message', `v${version}`);
  };

  // The launcher entry, as setup has omarchy-webapp-install write it.
  const entry = (exec) => {
    const dir = join(home, '.local', 'share', 'applications');
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, 'Feed.desktop'), `[Desktop Entry]\nVersion=1.0\nName=Feed\nExec=${exec}\nType=Application\n`);
  };
  const install = (ref) => {
    git(root, 'clone', '--quiet', origin, app);
    if (ref) git(app, 'reset', '--quiet', '--hard', ref);
    entry(join(app, 'omarchy-feed'));
  };
  const open = () => writeFileSync(server, '');

  // stdin and stderr are pipes, as when the server runs it: no terminal.
  const run = (...args) => spawnSync(join(app, 'update'), args, { env, encoding: 'utf8' });
  const calls = () => (existsSync(log) ? readFileSync(log, 'utf8').trim().split('\n') : []);
  const count = (pattern) => calls().filter((call) => pattern.test(call)).length;
  const starts = () => count(/^setsid python3 .*\/app\/server\.py --port 47220$/);
  const stops = () => count(/^curl .* -X POST .*\/api\/stop$/);
  const running = () => existsSync(server);
  const head = () => git(app, 'rev-parse', 'HEAD');

  return { root, origin, app, git, commit, release, entry, install, open, run, calls, starts, stops, running, head };
}

test('check finds the newest release on origin', (t) => {
  const s = sandbox(t);
  s.commit('Initial');
  s.install();
  assert.equal(s.run('check').stdout, 'current\n', 'no releases yet');

  s.release('0.9.0');
  s.release('0.10.0');
  s.git(s.origin, 'tag', 'v2.0.0-rc.1'); // candidates and other tags are no releases
  s.git(s.origin, 'tag', 'latest');
  const r = s.run('check');
  assert.equal(r.status, 0, r.stderr);
  assert.equal(r.stdout, 'available 0.10.0\n');
});

test('check reads lightweight tags too', (t) => {
  const s = sandbox(t);
  s.commit('Initial');
  s.install();
  s.commit('Change');
  s.git(s.origin, 'tag', 'v0.2.0');
  assert.equal(s.run('check').stdout, 'available 0.2.0\n');
});

test('check says current at or past the newest release', (t) => {
  const s = sandbox(t);
  s.release('0.1.0');
  s.commit('Unreleased');
  s.install('v0.1.0');
  assert.equal(s.run('check').stdout, 'current\n');

  rmSync(s.app, { recursive: true });
  s.install(); // main, ahead of v0.1.0
  assert.equal(s.run('check').stdout, 'current\n');
});

test("check fails when origin can't be reached", (t) => {
  const s = sandbox(t);
  s.release('0.1.0');
  s.install();
  s.git(s.app, 'remote', 'set-url', 'origin', join(s.root, 'gone'));
  const r = s.run('check');
  assert.equal(r.status, 1);
  assert.match(r.stderr, /update: can't reach origin\n$/);
});

test('download fetches the release and changes no files', (t) => {
  const s = sandbox(t);
  s.release('0.1.0');
  s.install();
  s.release('0.2.0', { 'new.txt': 'new\n' });
  const before = s.head();

  const r = s.run('download', '0.2.0');
  assert.equal(r.status, 0, r.stderr);
  assert.equal(s.head(), before);
  assert.equal(existsSync(join(s.app, 'new.txt')), false);
  assert.equal(s.run('check').stdout, 'downloaded 0.2.0\n');
  assert.deepEqual(s.calls(), []);
});

test('install moves to the release, puts its server in place of the running one, and says so', (t) => {
  const s = sandbox(t);
  s.release('0.1.0');
  s.install();
  s.release('0.2.0', { 'new.txt': 'new\n' });
  s.commit('Unreleased', { 'unreleased.txt': 'not yet\n' });
  s.open();

  const r = s.run('install', '0.2.0'); // downloads first when needed
  assert.equal(r.status, 0, r.stderr);
  assert.equal(r.stdout, 'Feed 0.2.0 is installed.\n');
  assert.equal(s.head(), s.git(s.origin, 'rev-parse', 'v0.2.0^{commit}'));
  assert.equal(existsSync(join(s.app, 'new.txt')), true);
  assert.equal(existsSync(join(s.app, 'unreleased.txt')), false);
  assert.equal(s.git(s.app, 'branch', '--show-current'), 'main');
  assert.deepEqual([s.stops(), s.starts(), s.running()], [1, 1, true]);
  assert.equal(s.calls().at(-1), 'notify-send Feed updated Version 0.2.0 is installed.');
  assert.equal(s.run('check').stdout, 'current\n');
});

test('install with Feed closed starts the new server only to see it answer', (t) => {
  const s = sandbox(t);
  s.release('0.1.0');
  s.install();
  s.release('0.2.0');

  const r = s.run('install', '0.2.0');
  assert.equal(r.status, 0, r.stderr);
  assert.equal(s.head(), s.git(s.origin, 'rev-parse', 'v0.2.0^{commit}'));
  assert.deepEqual([s.starts(), s.stops(), s.running()], [1, 1, false]);
});

test('install goes back when the new server does not start', (t) => {
  const s = sandbox(t);
  s.release('0.1.0');
  s.install();
  s.release('0.2.0', { broken: 'x\n' });
  const before = s.head();
  s.open();

  const r = s.run('install', '0.2.0');
  assert.equal(r.status, 1);
  assert.match(r.stderr, /update: Feed 0\.2\.0 did not start, so Feed stays as it was\. See .*\/state\/omarchy-feed\/server\.log\n$/);
  assert.equal(s.head(), before);
  assert.equal(existsSync(join(s.app, 'broken')), false);
  // Started with the release, which failed, then with the old code again.
  assert.deepEqual([s.stops(), s.starts(), s.running()], [1, 2, true]);
  assert.match(s.calls().at(-1), /^notify-send Feed update failed Feed 0\.2\.0 did not start/);
});

test('install with Feed closed goes back and leaves nothing running', (t) => {
  const s = sandbox(t);
  s.release('0.1.0');
  s.install();
  s.release('0.2.0', { broken: 'x\n' });
  const before = s.head();

  const r = s.run('install', '0.2.0');
  assert.equal(r.status, 1);
  assert.match(r.stderr, /Feed 0\.2\.0 did not start, so Feed stays as it was/);
  assert.equal(s.head(), before);
  assert.deepEqual([s.starts(), s.running()], [2, false]);
});

test('install goes back when the running server does not stop', (t) => {
  const s = sandbox(t);
  s.release('0.1.0');
  s.install();
  s.release('0.2.0', { 'new.txt': 'new\n' });
  const before = s.head();
  s.open();
  writeFileSync(join(s.root, 'stuck'), '');

  const r = s.run('install', '0.2.0');
  assert.equal(r.status, 1);
  assert.match(r.stderr, /update: can't update to 0\.2\.0: the running Feed server did not stop\. Close Feed and try again\n$/);
  assert.equal(s.head(), before);
  assert.equal(existsSync(join(s.app, 'new.txt')), false);
  assert.equal(s.starts(), 0);
});

test("install refuses a checkout the launcher entry does not start", (t) => {
  const s = sandbox(t);
  s.release('0.1.0');
  s.install();
  s.release('0.2.0', { 'new.txt': 'new\n' });
  const before = s.head();
  s.open();

  for (const exec of [join(s.root, 'elsewhere', 'omarchy-feed'), 'omarchy-launch-webapp "http://localhost:47220/"']) {
    s.entry(exec);
    const r = s.run('install', '0.2.0');
    assert.equal(r.status, 1);
    assert.match(r.stderr, /Feed's launcher entry does not start Feed from .*\/app\. Set it up with .*\/app\/setup\n$/);
    assert.equal(s.head(), before);
  }
  rmSync(join(s.root, 'home'), { recursive: true }); // no entry at all
  assert.match(s.run('install', '0.2.0').stderr, /launcher entry does not start Feed/);
  assert.deepEqual([s.stops(), s.starts(), s.running()], [0, 0, true]);
});

test('install leaves a checkout with local changes alone', (t) => {
  const s = sandbox(t);
  s.release('0.1.0', { 'file.txt': 'one\n' });
  s.install();
  s.release('0.2.0', { 'file.txt': 'two\n' });
  writeFileSync(join(s.app, 'file.txt'), 'mine\n');
  const before = s.head();
  s.open();

  const r = s.run('install', '0.2.0');
  assert.equal(r.status, 1);
  assert.match(r.stderr, /local changes/);
  assert.equal(s.head(), before);
  assert.equal(readFileSync(join(s.app, 'file.txt'), 'utf8'), 'mine\n');
  assert.deepEqual([s.stops(), s.starts()], [0, 0]);
});

test('install keeps local changes to files the release does not touch', (t) => {
  const s = sandbox(t);
  s.release('0.1.0', { 'file.txt': 'one\n' });
  s.install();
  // Going back from this release would otherwise reset the whole checkout.
  s.release('0.2.0', { broken: 'x\n' });
  writeFileSync(join(s.app, 'file.txt'), 'mine\n');
  const before = s.head();

  const r = s.run('install', '0.2.0');
  assert.equal(r.status, 1);
  assert.match(r.stderr, /local changes/);
  assert.equal(s.head(), before);
  assert.equal(readFileSync(join(s.app, 'file.txt'), 'utf8'), 'mine\n');
});

test('install leaves a checkout with local commits alone', (t) => {
  const s = sandbox(t);
  s.release('0.1.0');
  s.install();
  s.release('0.2.0');
  writeFileSync(join(s.app, 'mine.txt'), 'mine\n');
  s.git(s.app, 'add', 'mine.txt');
  s.git(s.app, 'commit', '--quiet', '-m', 'Mine');
  const before = s.head();

  const r = s.run('install', '0.2.0');
  assert.equal(r.status, 1);
  assert.match(r.stderr, /local commits/);
  assert.equal(s.head(), before);
  assert.deepEqual([s.stops(), s.starts()], [0, 0]);
});

test('refuses a copy that is not its own checkout', (t) => {
  const s = sandbox(t);
  s.release('0.1.0');
  s.install();
  // A copy inside a repository that keeps the whole home directory.
  const nested = join(s.app, 'copy');
  mkdirSync(nested, { recursive: true });
  copyFileSync(SCRIPT, join(nested, 'update'));

  const r = spawnSync(join(nested, 'update'), ['check'], { encoding: 'utf8' });
  assert.equal(r.status, 1);
  assert.match(r.stderr, /not a git checkout/);
});

test('rejects versions that are not releases', (t) => {
  const s = sandbox(t);
  s.release('0.1.0');
  s.install();
  for (const version of ['', 'v0.2.0', '0.2', '0.2.0-rc.1', '0.2.0;true']) {
    const r = s.run('install', version);
    assert.equal(r.status, 1, version);
    assert.match(r.stderr, /not a release version/);
  }
  assert.deepEqual(s.calls(), []);
});

test('version names the installed release', (t) => {
  const s = sandbox(t);
  s.commit('Initial');
  s.install();
  assert.equal(s.run('version').stdout, '', 'before the first release');

  s.release('0.1.0');
  s.git(s.origin, 'tag', 'v0.2.0-rc.1'); // a candidate is no release
  rmSync(s.app, { recursive: true });
  s.install();
  assert.equal(s.run('version').stdout, '0.1.0\n');

  s.commit('Unreleased');
  rmSync(s.app, { recursive: true });
  s.install();
  assert.match(s.run('version').stdout, /^0\.1\.0-1-g[0-9a-f]+\n$/);
});
