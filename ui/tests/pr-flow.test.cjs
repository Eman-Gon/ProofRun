const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Exercise the publishing boundary without GitHub access or a browser dependency.
const source = fs.readFileSync(path.join(__dirname, '../app.js'), 'utf8')
  .split("$('project-select').addEventListener('change'")[0];

function harness(respond) {
  const elements = new Map();
  const requests = [];
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      textContent: '', hidden: true, open: false, disabled: false,
      setAttribute() {}, focus() {}, showModal() { this.open = true; }, close() { this.open = false; },
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    document: { getElementById: element }, TextEncoder, AbortController, URL,
    setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {},
    fetch: async (url, options) => {
      requests.push({ url, payload: JSON.parse(options.body) });
      return respond();
    },
  });
  vm.runInContext(source, context);
  vm.runInContext(`
    connected = true;
    renderCase = () => {};
    renderRepository = () => {};
    state.csrfToken = 'test-token';
    state.repositoryScans = [{ id: 'review-1', status: 'completed', repository: 'example/typescript-app', result: {
      repository: 'example/typescript-app', commit: 'a'.repeat(40), findings: [{
        id: 'agent-review:new-bug', origin: 'agent', title: 'Handle empty input', file: 'src/parse.ts',
        beforeCode: 'return rows[0].id;', afterCode: 'return rows[0]?.id ?? null;'
      }]
    }}];
    selectedId = 'scan:review-1';
  `, context);
  return { context, requests, element, run: (code) => vm.runInContext(code, context) };
}

test('a generic finding opens a truthful preview without publishing', () => {
  const h = harness(() => { throw new Error('Unexpected publication'); });
  h.run('createPublicPullRequest(state.repositoryScans[0], 0)');
  assert.equal(h.element('pr-dialog').open, true);
  assert.match(h.element('pr-dialog-summary').textContent, /example\/typescript-app/);
  assert.match(h.element('pr-dialog-evidence').textContent, /tests have not been run/);
  assert.match(h.element('pr-dialog-patch').textContent, /rows\[0\]\?\.id/);
  assert.equal(h.requests.length, 0);
});

test('explicit submission publishes the previewed finding despite a selection change and prevents duplicate requests', async () => {
  let finish;
  const h = harness(() => new Promise((resolve) => { finish = resolve; }));
  h.run('createPublicPullRequest(state.repositoryScans[0], 0)');
  h.run("selectedId = 'different-result'");
  const pending = h.run('publishPullRequest()');
  await h.run('publishPullRequest()');
  assert.equal(h.requests.length, 1);
  assert.deepEqual(h.requests[0], { url: '/api/pull-requests', payload: { scanId: 'review-1', findingIndex: 0 } });
  assert.equal(h.element('pr-dialog-create').disabled, true);
  finish({ ok: true, json: async () => ({ pullRequest: { number: 12, url: 'https://github.com/example/typescript-app/pull/12' } }) });
  await pending;
  assert.equal(h.element('pr-dialog').open, false);
  assert.equal(h.run('state.repositoryScans[0].pullRequests[0].number'), 12);
});

test('server rejection remains visible in the preview without losing the patch', async () => {
  const h = harness(() => ({ ok: false, json: async () => ({ error: 'Source changed; run a fresh review.' }) }));
  h.run('createPublicPullRequest(state.repositoryScans[0], 0)');
  const patch = h.element('pr-dialog-patch').textContent;
  await h.run('publishPullRequest()');
  assert.equal(h.element('pr-dialog').open, true);
  assert.equal(h.element('pr-dialog-error').hidden, false);
  assert.match(h.element('pr-dialog-error').textContent, /Source changed/);
  assert.equal(h.element('pr-dialog-patch').textContent, patch);
  assert.equal(h.element('pr-dialog-create').disabled, false);
});

test('opening the scan report exposes existing evidence without a network request', () => {
  const h = harness(() => { throw new Error('Unexpected network request'); });
  h.run('openReport()');
  assert.equal(h.element('report-dialog').open, true);
  assert.match(h.element('report-dialog-scope').textContent, /tests were not executed/);
  const report = JSON.parse(h.element('report-dialog-content').textContent);
  assert.equal(report.scan.result.findings[0].file, 'src/parse.ts');
  assert.equal(h.requests.length, 0);
});
