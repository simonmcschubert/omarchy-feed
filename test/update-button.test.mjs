// What the update button shows (app/update.mjs). Run: node --test
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { channelNote, updateButton, updateName } from '../app/update.mjs';

const idle = { checking: false, error: '', current: false };
const server = (fields) => ({ status: '', version: '', error: '', installed: '0.1.0', channel: 'stable', checking: false, boot: 'b', ...fields });
const preview = '1a2b3c4d'.repeat(5);

test('the button checks for updates when there is nothing to offer', () => {
  assert.deepEqual(updateButton(server({}), idle), {
    icon: 'refresh', text: '', tone: '', tip: 'Feed 0.1.0 is installed. Click to check for updates.',
  });
  assert.equal(updateButton(server({ installed: '' }), idle).tip, 'Click to check for updates.', 'before the first release');
  assert.equal(updateButton(null, idle).tip, 'Click to check for updates.', 'before the server answers');
  assert.equal(updateButton(server({ checking: true }), idle).busy, true, "the timer's check turns it too");
});

test('the button says what the check it asked for found', () => {
  assert.deepEqual(updateButton(server({}), { ...idle, checking: true }), {
    icon: 'refresh', text: '', tone: '', tip: 'Checking for updates…', busy: true,
  });
  assert.deepEqual(updateButton(server({}), { ...idle, current: true }), {
    icon: 'refresh', text: 'Up to date', tone: '', tip: 'Feed 0.1.0 is up to date.',
  });
  assert.deepEqual(updateButton(server({}), { ...idle, error: "can't reach origin" }), {
    icon: 'alert', text: "Can't check", tone: 'red', tip: "Can't check for updates: can't reach origin. Click to try again.",
  });
});

test('the button offers a release step by step', () => {
  const steps = ['available', 'downloading', 'downloaded', 'installing'].map((status) => updateButton(server({ status, version: '0.2.0' }), idle));
  assert.deepEqual(steps.map((b) => b.text), ['Update', 'Downloading', 'Restart to update', 'Updating']);
  assert.deepEqual(steps.map((b) => b.tone), ['accent', 'accent', 'accent', 'accent']);
  assert.equal(steps[2].tip, 'Feed 0.2.0 is downloaded. Click to restart Feed with it. Your sources and articles stay as they are.');
  assert.equal(steps[3].busy, true);
});

test('a failed update says why as a sentence', () => {
  const failed = (error) => updateButton(server({ status: 'failed', version: '0.2.0', error }), idle);
  assert.equal(failed("can't download Feed 0.2.0").tip, "Can't download Feed 0.2.0. Click to try again.");
  assert.equal(failed("can't update to 0.2.0: /app has local changes").tip, "Can't update to 0.2.0: /app has local changes. Click to try again.");
  assert.equal(failed('').tip, "Can't update to Feed 0.2.0. Click to try again.");
  assert.equal(failed('x').tone, 'red');
});

test('a preview is named by its commit', () => {
  assert.equal(updateName('0.2.0'), '0.2.0');
  assert.equal(updateName(preview), 'preview 1a2b3c4');
  const offer = (status) => updateButton(server({ status, version: preview, channel: 'preview' }), idle).tip;
  assert.equal(offer('available'), 'Feed preview 1a2b3c4 is available. Click to download it.');
  assert.equal(offer('downloaded'), 'Feed preview 1a2b3c4 is downloaded. Click to restart Feed with it. Your sources and articles stay as they are.');
});

test('on Preview, the button says so', () => {
  const onPreview = server({ installed: '0.1.0-3-g1a2b3c4', channel: 'preview' });
  assert.equal(updateButton(onPreview, idle).tip, 'Feed 0.1.0-3-g1a2b3c4 (Preview) is installed. Click to check for updates.');
  assert.equal(updateButton(onPreview, { ...idle, current: true }).tip, 'Feed 0.1.0-3-g1a2b3c4 (Preview) is up to date.');
  assert.equal(updateButton(server({}), { ...idle, current: true }).tip, 'Feed 0.1.0 is up to date.', 'nothing added on Stable');
});

test("the channel choice says what is installed, or why it can't switch", () => {
  const calm = { busy: false, error: '' };
  assert.equal(channelNote(server({}), calm), 'Feed 0.1.0 is installed.');
  assert.equal(channelNote(server({ installed: '' }), calm), '', 'before the first release');
  assert.equal(channelNote(null, calm), '', 'before the server answers');
  assert.equal(channelNote(server({ channel: '' }), calm), "Feed can't update itself: its folder is not a git checkout.");
  assert.equal(channelNote(server({}), { busy: true, error: '' }), 'Switching…');
  assert.equal(channelNote(server({}), { busy: false, error: "can't change the channel while Feed updates; try again when it is done" }),
    "Can't change the channel while Feed updates; try again when it is done.");
});
