import assert from 'node:assert/strict';
import test from 'node:test';
import {
  FAILURE_QUERY_LIMIT, FailureResearchRequests, failureResearchBrief,
  fixtureFailureQuery, normalizedFailureQuery, readFailureResearch, safeFailureSource,
} from '../src/app/failure-research.ts';

const scope = { kind: 'fixture', runId: 'run-original', findingId: 'regression' };
const query = 'Pydantic optional field missing after upgrade';
const report = {
  schema_version: 'proofrun.failure-research.v1', research_id: 'failure-research-one', request_id: 'gateway-derived-request',
  context: { kind: 'fixture', run_id: 'run-original', finding_id: 'regression', evidence_sha256: 'a'.repeat(64) },
  query, status: 'completed', summary: 'Review the required/optional migration note [source-1].',
  sources: [{ id: 'source-1', title: 'Migration guide', url: 'https://docs.pydantic.dev/latest/migration/', excerpt: 'Optional fields can be required.' }],
  suggested_fixes: [{ description: 'Check whether the field needs an explicit default.', source_ids: ['source-1'] }],
  observed_at: '2026-09-29T22:00:00Z', provenance: { provider: 'openrouter', mode: 'live' },
  error: null, limitations: ['Related reports do not prove the cause of this failure.'],
};

test('ambiguous retries preserve request identity; explicit refresh and different scopes create new identities', () => {
  const requests = new FailureResearchRequests();
  let n = 0;
  const nonce = () => `request-${++n}`;
  const original = requests.request('fixture/run-a/regression', ` ${query} `, false, nonce);
  assert.equal(requests.request('fixture/run-a/regression', query, false, nonce), original);
  requests.request('fixture/run-a/regression', 'Different question', false, nonce);
  assert.equal(requests.request('fixture/run-a/regression', query, false, nonce), original);
  assert.notEqual(requests.request('fixture/run-a/regression', query, true, nonce).requestId, original.requestId);
  assert.notEqual(requests.request('fixture/run-b/regression', query, false, nonce).requestId, original.requestId);
  assert.notEqual(requests.request('release/run-a/finding-2', query, false, nonce).requestId, original.requestId);
});

test('queries are explicit, bounded and trimmed without adding local evidence', () => {
  assert.equal(normalizedFailureQuery(`  ${query}  `), query);
  assert.equal(normalizedFailureQuery('a'.repeat(FAILURE_QUERY_LIMIT)).length, FAILURE_QUERY_LIMIT);
  for (const value of ['', '   ', 'a'.repeat(FAILURE_QUERY_LIMIT + 1), 'secret\u0000text']) {
    assert.throws(() => normalizedFailureQuery(value), /search query/);
  }
});

test('research accepts the gateway request identity but rejects other runs, findings, kinds and changed queries', () => {
  assert.equal(readFailureResearch(report, scope, query), report);
  for (const other of [
    { ...scope, runId: 'run-other' }, { ...scope, findingId: 'another-finding' }, { ...scope, kind: 'release' },
  ]) assert.throws(() => readFailureResearch(report, other, query), /different failure/);
  assert.throws(() => readFailureResearch(report, scope, 'Changed query'), /different failure/);
});

test('reports require dated bound evidence and actual citations; model text alone is never sourced research', () => {
  for (const value of [
    { ...report, schema_version: 'other' },
    { ...report, observed_at: 'yesterday' },
    { ...report, context: { ...report.context, evidence_sha256: '' } },
    { ...report, sources: [] },
    { ...report, sources: [report.sources[0], report.sources[0]] },
    { ...report, sources: [{ ...report.sources[0], url: 'javascript:alert(1)' }] },
    { ...report, sources: [...report.sources, { ...report.sources[0], id: 'source-2', url: 'data:text/plain,bad' }] },
    { ...report, sources: Array.from({ length: 6 }, (_, index) => ({ ...report.sources[0], id: `source-${index + 1}` })) },
    { ...report, suggested_fixes: [{ description: 'Uncited fix', source_ids: [] }] },
    { ...report, suggested_fixes: [{ description: 'Wrong citation', source_ids: ['source-invented'] }] },
  ]) assert.throws(() => readFailureResearch(value, scope, query), /unsupported research/);
});

test('unavailable and no-source responses remain explicit without suggested fixes', () => {
  for (const status of ['unavailable', 'no_sources']) {
    const value = { ...report, status, sources: [], suggested_fixes: [], summary: '', error: { code: 'not_available', message: 'Search unavailable.' } };
    assert.equal(readFailureResearch(value, scope, query).status, status);
    const brief = failureResearchBrief(value);
    assert.ok(brief.includes(`status: ${status}`));
    assert.ok(brief.includes('Search unavailable.'));
    assert.throws(() => readFailureResearch({ ...value, suggested_fixes: report.suggested_fixes }, scope, query));
    assert.throws(() => readFailureResearch({ ...value, sources: report.sources }, scope, query));
  }
});

test('fixture prefill requires the actual missing-field regression and measured package versions', () => {
  const run = {
    case_id: 'customer-nickname-v1', finding_status: 'regression_reproduced',
    environments: { baseline: { observed_version: '1.10.18' }, updated: { observed_version: '2.8.2' } },
    cases: [
      { id: 'nickname_omitted', stage: 'baseline', status: 'passed' },
      { id: 'nickname_omitted', stage: 'updated', status: 'error', detail: 'ValidationError Field required [type=missing] input: customer@example.com /private/source/app.py secret-token' },
    ],
  };
  const prefill = fixtureFailureQuery(run);
  assert.match(prefill, /Pydantic 1\.10\.18 to 2\.8\.2/);
  assert.match(prefill, /ValidationError/);
  assert.doesNotMatch(prefill, /customer@example|private|secret-token/);
  for (const value of [
    { ...run, case_id: 'another-case' }, { ...run, finding_status: 'inconclusive' },
    { ...run, environments: undefined },
    { ...run, cases: [run.cases[0], { ...run.cases[1], status: 'passed' }] },
    { ...run, cases: [{ ...run.cases[0], status: 'error' }, run.cases[1]] },
    { ...run, cases: [run.cases[0], { ...run.cases[1], detail: 'Network error' }] },
    { ...run, cases: [...run.cases, run.cases[1]] },
  ]) assert.equal(fixtureFailureQuery(value), '');
});

test('citation navigation accepts HTTPS only and the plain text brief records unverified scope', () => {
  assert.equal(safeFailureSource(report.sources[0].url), report.sources[0].url);
  for (const url of ['javascript:alert(1)', 'data:text/html,hello', 'http://example.com', 'https://user:secret@example.com', 'not a URL']) {
    assert.equal(safeFailureSource(url), undefined);
  }
  const brief = failureResearchBrief(report);
  for (const value of [report.context.run_id, report.research_id, report.observed_at, query, report.sources[0].url, 'Suggested fixes — not tested', 'Unverified research']) assert.ok(brief.includes(value));
  const unsafe = failureResearchBrief({ ...report, sources: [{ ...report.sources[0], url: 'javascript:alert(1)' }] });
  assert.doesNotMatch(unsafe, /javascript:alert/);
  assert.match(unsafe, /Unsafe source link omitted/);
});
