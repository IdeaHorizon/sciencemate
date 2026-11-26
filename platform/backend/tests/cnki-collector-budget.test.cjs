const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('a mixed result set cannot make a category search turn pages to fill its quota', async () => {
  // Simulate CROSSDB, whose rows mostly belong to other categories. The server
  // accepts no matches here, even though every page has a "next" link.
  let page = 1;
  const titles = new Set(), completedPages = [];
  const config = {server: 'http://localhost', tabId: 1};
  const pageState = () => ({
    query: 'topic', field: 'SU', resource: 'CROSSDB', page, hasNext: true, total: 900,
    rows: Array.from({length: 20}, (_, i) => ({title: `${page}/${i + 1}`, year: '2026'})),
  });
  const event = {addListener() {}};
  const chrome = {
    storage: {local: {get: async () => config, set: async (p) => Object.assign(config, p)}},
    alarms: {get: async () => ({}), onAlarm: event},
    runtime: {onStartup: event, onInstalled: event, onMessage: event},
    action: {setBadgeText() {}, setBadgeBackgroundColor() {}},
    tabs: {get: async () => ({url: 'https://kns.cnki.net/', id: 1, status: 'complete'}), onRemoved: event},
    scripting: {executeScript: async ({args: [action]}) => {
      if (action === 'next') assert.ok(++page <= 2, 'requested a page beyond the search budget');
      else assert.equal(action, 'state');
      return [{result: pageState()}];
    }},
  };
  const fetch = async (url, {body}) => {
    const data = JSON.parse(body);
    if (url.endsWith('/cache')) return {ok: true, json: async () => ({papers: data})};
    if (url.endsWith('/batch')) {
      for (const paper of data.papers) titles.add(paper.title);
      if (data.page_complete) completedPages.push([data.page, data.finished]);
    }
    return {ok: true, json: async () => ({count: 0})};
  };
  const background = fs.readFileSync(path.join(__dirname, '../app/assets/cnki-extension/background.js'), 'utf8')
    .replace('import {cnkiPage} from "./cnki-page.js";', 'const cnkiPage = () => {};');
  const job = {id: 'job', query: 'topic', field: 'SU', document_kind: 'other', limit: 21, page: 1};
  await vm.runInNewContext(background + '\ncollect(job)', {chrome, fetch, URL, AbortSignal, job});
  assert.deepEqual(completedPages, [[1, false], [2, true]]);
  assert.equal(titles.size, 21);
  assert.equal(config.currentJob, null);
});
