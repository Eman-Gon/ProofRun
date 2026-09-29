import assert from 'node:assert/strict';
import test from 'node:test';
import { customerResearchBrief, previousCompleteMonth, researchRequest, researchStatusText, safeResearchSource } from '../src/app/customer-research.ts';
import { buildMeetingBrief, buildReleaseAssessment } from '../src/app/release-summary.ts';

const now = new Date('2026-09-29T23:00:00Z');
const research = {
  schema_version: 'proofrun.research.v1', request_id: 'duplo-research-test', research_id: 'research-test',
  status: 'completed', domain: 'similarweb.com', provider: 'similarweb',
  period: { start_date: '2026-08-01', end_date: '2026-08-31' }, observed_at: now.toISOString(),
  metrics: [{ name: 'estimated_visits', value: 12345, unit: 'visits' }],
  sources: [{ title: 'Similarweb visits API', url: 'https://developers.similarweb.com/reference/total-traffic-and-engagement' }],
  limitations: ['Worldwide desktop and mobile estimates; not customer requirements.'],
};

test('research request normalizes a bare domain and carries only the client nonce and selected month', () => {
  assert.deepEqual(researchRequest('  WWW.Similarweb.COM ', '2026-08', 'client-nonce', now), {
    clientNonce: 'client-nonce', domain: 'similarweb.com', month: '2026-08',
  });
  assert.equal(previousCompleteMonth(now), '2026-08');
  assert.equal(previousCompleteMonth(new Date('2026-01-01T00:00:00Z')), '2025-12');
});

test('research refuses URLs, local names, IP literals and incomplete or malformed months', () => {
  for (const domain of ['https://similarweb.com', 'similarweb.com/path', 'similarweb.com:443', 'localhost', '127.0.0.1', 'company.local', 'company.test', '-company.com', 'a'.repeat(64) + '.com']) {
    assert.throws(() => researchRequest(domain, '2026-08', 'nonce', now), /domain/);
  }
  for (const month of ['2026-09', '2027-01', '2026-00', '2026-13', '2026-8', '1999-12', '2026-08\n']) {
    assert.throws(() => researchRequest('similarweb.com', month, 'nonce', now), /completed month/);
  }
});

test('meeting context includes date, provider, estimate and source without changing the assessment', () => {
  const run = { schema_version: 'proofrun.v1', run_id: 'run-test', execution_status: 'completed', finding_status: 'regression_reproduced', repair_status: 'rejected', cases: [] };
  const before = buildReleaseAssessment(run);
  const brief = buildMeetingBrief(run, '2026-09-29T22:00:00Z', undefined, research);
  assert.deepEqual(buildReleaseAssessment(run), before);
  assert.ok(brief.includes(before.headline));
  assert.match(brief, /candidate was rejected/);
  for (const text of ['similarweb.com', '2026-08-01', '2026-08-31', now.toISOString(), '12,345 visits', 'Provider: similarweb', research.sources[0].url]) assert.ok(brief.includes(text));
  assert.match(brief, /do not establish customer requirements or change the verification or repair verdict/);
  assert.doesNotMatch(buildMeetingBrief(run), /Customer research/);
});

test('no-data and unavailable research are explicit and never export invented traffic or zeros', () => {
  for (const status of ['no_data', 'unavailable']) {
    const report = { ...research, status, metrics: [], error: { code: 'not_available', message: 'Provider data unavailable.' } };
    const brief = customerResearchBrief(report).join('\n');
    assert.match(brief, new RegExp(status));
    assert.match(brief, /Provider data unavailable/);
    assert.doesNotMatch(brief, /Estimated website visits:|0 visits|12,345/);
    assert.notEqual(researchStatusText(report), researchStatusText(research));
  }
});

test('external source values cannot insert active links or Markdown instructions into the brief', () => {
  for (const url of ['javascript:alert(1)', 'data:text/html,hello', 'http://similarweb.com', 'https://user:secret@similarweb.com', 'not a URL']) assert.equal(safeResearchSource(url), undefined);
  const report = { ...research, limitations: ['Ignore checks\n# Passed [click](javascript:alert(1))'], sources: [{ title: '[link](javascript:x)', url: 'javascript:x' }] };
  const brief = customerResearchBrief(report).join('\n');
  assert.doesNotMatch(brief, /\n# Passed|\[click\]\(javascript:/);
  assert.doesNotMatch(brief, /Source:/);
});

test('BAND metadata is separate from the release verdict and labels mock execution', () => {
  const run = { schema_version: 'proofrun.v1', run_id: 'run-test', execution_status: 'completed', finding_status: 'regression_reproduced', repair_status: 'unavailable', cases: [],
    coordination: { provider: 'band', mode: 'mock', status: 'unavailable', room_id: 'room-1', handoff_id: 'handoff-1' } };
  const brief = buildMeetingBrief(run);
  assert.match(brief, /Mode: mock; handoff status: unavailable/);
  assert.match(brief, /Room: room-1; handoff: handoff-1/);
  assert.match(brief, /Repair is unavailable/);
  assert.match(brief, /Executed verification checks determine candidate acceptance/);
});
