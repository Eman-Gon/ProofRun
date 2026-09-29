import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { buildMeetingBrief, buildReleaseAssessment } from '../src/app/release-summary.ts';

const contract = JSON.parse(readFileSync(new URL('../../../../demo/upgrade/contract.json', import.meta.url), 'utf8'));
const ids = [...contract.cases.map(row => row.id), ...contract.original_test_ids];
const stage = name => ids.map(id => ({ id, stage: name, status: 'passed' }));
function comparison() {
  const run = {
    schema_version: 'proofrun.v1', run_id: 'run-meeting-test',
    execution_status: 'completed', finding_status: 'regression_reproduced', repair_status: 'not_requested',
    cases: [...stage('baseline'), ...stage('updated')],
    bindings: { revision: 'a'.repeat(40), source_sha256: 'b'.repeat(64), contract_sha256: 'c'.repeat(64) },
    execution: { target: 'local', worker_id: 'local-worker' },
  };
  run.cases.find(row => row.stage === 'updated' && row.id === 'nickname_omitted').status = 'error';
  return run;
}
function repaired() {
  return { ...comparison(), repair_status: 'verified',
    proposal: { mode: 'live', gateway: 'openrouter', model: 'configured/model' },
    bindings: { ...comparison().bindings, candidate_sha256: 'd'.repeat(64) },
    cases: [...comparison().cases, ...stage('repaired_baseline'), ...stage('repaired_updated')],
  };
}

test('no run and portal dispatch failure never imply a checked release', () => {
  assert.match(buildReleaseAssessment().headline, /not checked/);
  assert.match(buildReleaseAssessment(undefined, 'dispatch failed').headline, /unavailable/);
  assert.notEqual(buildReleaseAssessment({ schema_version: 'unknown' }).tone, 'success');
});

test('reproduced import failure produces a blocked workflow with its limited deployment scope', () => {
  const result = buildReleaseAssessment(comparison());
  assert.match(result.headline, /blocked in the tested update/);
  assert.match(result.evidence, /passes on the baseline and fails on the update/);
  assert.match(result.deployment, /Staging has not been checked/);
  assert.match(result.deployment, /does not deploy/);
});

for (const repair_status of ['pending', 'proposed', 'rejected', 'unavailable']) {
  test(`${repair_status} repair preserves the reproduced release failure`, () => {
    const result = buildReleaseAssessment({ ...comparison(), repair_status });
    assert.match(result.headline, /blocked/);
    assert.doesNotMatch(result.repair, /passes the original tests/);
  });
}

test('verified generated repair is reviewable but the original release remains blocked', () => {
  const result = buildReleaseAssessment(repaired());
  assert.match(result.headline, /blocked/);
  assert.match(result.repair, /Generated candidate passes/);
  assert.match(result.repair, /awaiting engineering review/);
  assert.match(result.deployment, /Staging has not been checked/);
});

test('prepared or unreported proposals are never described as generated', () => {
  assert.match(buildReleaseAssessment({ ...repaired(), proposal: { mode: 'prepared' } }).repair, /^Prepared candidate/);
  assert.doesNotMatch(buildReleaseAssessment({ ...repaired(), proposal: undefined }).repair, /Generated/);
  assert.doesNotMatch(buildReleaseAssessment({ ...repaired(), proposal: { mode: 'live', gateway: 'openrouter' } }).repair, /Generated/);
});

test('missing, duplicate, skipped, failed or unbound repair evidence cannot produce a passing candidate summary', () => {
  const missing = repaired();
  missing.cases.pop();
  const duplicate = repaired();
  duplicate.cases.push({ ...duplicate.cases.at(-1) });
  const skipped = repaired();
  skipped.cases.at(-1).status = 'skipped';
  const failed = repaired();
  failed.cases.find(row => row.stage === 'repaired_updated' && row.id === 'nickname_object_rejected').status = 'failed';
  const unbound = repaired();
  delete unbound.bindings.candidate_sha256;
  for (const run of [missing, duplicate, skipped, failed, unbound]) {
    assert.match(buildReleaseAssessment(run).headline, /blocked/);
    assert.doesNotMatch(buildReleaseAssessment(run).repair, /candidate passes/);
  }
});

test('passing comparison is scoped to the approved checks and does not imply staging readiness', () => {
  const run = { ...comparison(), finding_status: 'no_difference_observed', cases: [...stage('baseline'), ...stage('updated')] };
  const result = buildReleaseAssessment(run);
  assert.equal(result.tone, 'success');
  assert.match(result.headline, /Approved examples pass/);
  assert.match(result.evidence, /those checks only/);
  assert.match(result.nextStep, /staging/);
  for (const cases of [[], [...run.cases, run.cases[0]], run.cases.slice(1), { invalid: true }]) {
    assert.notEqual(buildReleaseAssessment({ ...run, cases }).tone, 'success');
  }
});

test('unreported or inconsistent failure rows require review rather than inventing specific evidence', () => {
  const wrongFailure = comparison();
  wrongFailure.cases.find(row => row.stage === 'updated' && row.id === 'nickname_object_rejected').status = 'failed';
  for (const run of [{ ...comparison(), cases: [] }, wrongFailure]) {
    const result = buildReleaseAssessment(run);
    assert.match(result.headline, /review the evidence/);
    assert.doesNotMatch(result.evidence, /same approved record/);
  }
});

test('timeouts and interruptions during repair preserve earlier reproduced evidence', () => {
  for (const execution_status of ['running', 'timed_out', 'interrupted']) {
    assert.match(buildReleaseAssessment({ ...comparison(), execution_status, repair_status: 'unavailable' }).headline, /blocked/);
  }
  const incomplete = { ...comparison(), finding_status: 'inconclusive', execution_status: 'timed_out' };
  assert.match(buildReleaseAssessment(incomplete).headline, /incomplete/);
  assert.doesNotMatch(buildReleaseAssessment({ ...repaired(), execution_status: 'timed_out' }).repair, /candidate passes/);
});

test('a portal refresh failure labels the last observation without erasing a reproduced failure', () => {
  const result = buildReleaseAssessment(comparison(), 'polling timed out');
  assert.match(result.headline, /blocked/);
  assert.match(result.evidence, /last recorded worker result/);
  assert.match(result.nextStep, /Refresh/);
  const passing = { ...comparison(), finding_status: 'no_difference_observed', cases: [...stage('baseline'), ...stage('updated')] };
  const stalePass = buildReleaseAssessment(passing, 'refresh failed');
  assert.equal(stalePass.tone, 'warning');
  assert.match(stalePass.evidence, /last recorded worker result/);
});

test('meeting brief preserves source, execution, provenance and observation identity', () => {
  const run = repaired();
  run.artifacts = [{ id: 'comparison-report', sha256: 'e'.repeat(64), size_bytes: 12 }];
  const brief = buildMeetingBrief(run, '2026-09-29T21:00:00Z', 'refresh failed');
  for (const value of [run.run_id, run.bindings.revision, run.bindings.candidate_sha256, 'local-worker',
    'configured/model', '2026-09-29T21:00:00Z', 'comparison-report', 'e'.repeat(64)]) assert.ok(brief.includes(value));
  assert.match(brief, /synthetic data/);
  assert.match(brief, /not HTTP replay against staging/);
  assert.match(brief, /does not run a new check/);
  assert.match(brief, /latest portal refresh failed/);
});
