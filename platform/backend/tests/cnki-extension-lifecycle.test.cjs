const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../app/assets/cnki-extension/background.js'), 'utf8')
  .replace('import {cnkiPage} from "./cnki-page.js";', 'const cnkiPage = () => {};');
function extension({paired = true, blocked = false} = {}) {
  const state = {server: 'https://platform.invalid', token: paired ? 'test' : '', tabId: 1};
  const alarms = new Set(['cnki-poll']);
  const requests = [], handlers = {};
  const register = name => ({addListener(fn) {handlers[name] = fn;}});
  const runtime = {id: 'test', getURL: () => 'chrome-extension://test/popup.html',
    onInstalled: register('installed'), onStartup: register('startup'), onMessage: register('message')};
  const chrome = {runtime, alarms: {clear: async n => alarms.delete(n)},
    storage: {local: {get: async () => ({...state}), set: async v => Object.assign(state, v)}},
    tabs: {onRemoved: register('removed'), query: async () => [], get: async () => ({status: 'complete', url: 'https://kns.cnki.net/'})},
    action: {setBadgeText() {}, setBadgeBackgroundColor() {}},
    scripting: {registerContentScripts() {}, unregisterContentScripts: async () => {},
      executeScript: async () => [{result: {captcha: blocked, rows: [], total: 0, hasNext: false,
        page: 1, field: 'SU', query: 'topic', resource: 'CROSSDB'}}]}};
  const fetch = async (url, {body}) => {
    const route = new URL(url).pathname.replace('/api/v1/cnki', '');
    requests.push([route, JSON.parse(body)]);
    const result = route === '/worker/poll' ? {job: {id: 'job', lease: 'lease', page: 1, field: 'SU', query: 'topic'}} :
      route === '/worker/cache' ? {papers: []} : route === '/pair' ? {token: 'fresh', device_id: 'device'} : {};
    return {ok: true, json: async () => result};
  };
  vm.runInNewContext(source, {chrome, fetch, URL, AbortSignal}); // No wall-clock timer is needed.
  const send = m => new Promise(resolve => {
    handlers.message(m, {id: 'test', url: runtime.getURL('popup.html')}, resolve);
  });
  return {requests, state, alarms, handlers, send};
}

test('upgrade and browser startup clear the old alarm without contacting either service', async () => {
  const {alarms, requests, handlers} = extension();
  await handlers.installed({reason: 'update'});
  await handlers.startup();
  assert.equal(alarms.size, 0);
  assert.deepEqual(requests, []);
});

test('pairing stays idle; an explicit wake runs one search and does not schedule the next', async () => {
  const {state, requests, send} = extension({paired: false});
  assert.equal((await send({type: 'pair', code: 'test', server: 'https://platform.invalid'})).ok, true);
  assert.deepEqual(requests.map(([route]) => route), ['/pair']);
  assert.equal((await send({type: 'wake'})).ok, true);
  assert.equal(state.state, 'idle');
  assert.equal(requests.filter(([route]) => route === '/worker/poll').length, 1);
  assert.equal(requests.at(-1)[1].finished, true);
});

test('an explicit popup run that encounters verification does not retry on browser restart', async () => {
  const {state, requests, handlers, send} = extension({blocked: true});
  assert.equal((await send({type: 'resume'})).ok, true);
  assert.equal(state.state, 'waiting_for_verification');
  const before = requests.length;
  await handlers.startup();
  assert.equal(requests.length, before);
});
