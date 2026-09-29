import type { RunSummary } from './proofrun.service';
import { customerResearchBrief } from './customer-research.ts';
import type { CustomerResearch } from './customer-research';

export interface ReleaseAssessment {
  tone: 'neutral' | 'warning' | 'success';
  headline: string;
  workflow: string;
  evidence: string;
  repair: string;
  deployment: string;
  nextStep: string;
}

// Presentation for the registered fixture, not another worker verdict or a staging check.
const CASE_IDS = [
  'nickname_omitted', 'nickname_null', 'nickname_string',
  'nickname_object_rejected', 'required_name_rejected',
  'test_existing.TestExisting.test_explicit_nickname',
  'test_existing.TestExisting.test_explicit_none',
];
const DEPLOYMENT = 'Staging has not been checked by this run. This run does not deploy a repair.';

function stageCases(run: RunSummary, stage: string): NonNullable<RunSummary['cases']> | undefined {
  if (!Array.isArray(run.cases)) return undefined;
  const rows = run.cases.filter(row => row && row.stage === stage);
  if (rows.length !== CASE_IDS.length || new Set(rows.map(row => row.id)).size !== CASE_IDS.length
      || rows.some(row => !CASE_IDS.includes(row.id ?? ''))) return undefined;
  return rows;
}

function stagePassed(run: RunSummary, stage: string): boolean {
  return stageCases(run, stage)?.every(row => row.status === 'passed') === true;
}

function hasReproducedBreak(run: RunSummary): boolean {
  const updated = stageCases(run, 'updated');
  return stagePassed(run, 'baseline') && updated?.every(row => row.id === 'nickname_omitted'
    ? ['failed', 'error'].includes(row.status ?? '') : row.status === 'passed') === true;
}

function repairDescription(run: RunSummary): string {
  switch (run.repair_status) {
    case 'not_requested': return 'Repair was not requested.';
    case 'pending': return 'Waiting for a repair proposal; no candidate has been accepted.';
    case 'proposed': return 'A candidate was proposed; independent verification has not accepted it yet.';
    case 'rejected': return 'The candidate was rejected by verification. The original release finding still stands.';
    case 'unavailable': return 'Repair is unavailable. No candidate has been accepted.';
    case 'verified': {
      const candidate = run.bindings?.['candidate_sha256'];
      if (run.execution_status !== 'completed' || typeof candidate !== 'string'
          || !/^[a-f0-9]{64}$/.test(candidate)
          || !stagePassed(run, 'repaired_baseline') || !stagePassed(run, 'repaired_updated')) {
        return 'The worker reports a verified candidate, but its complete verification records are unavailable here. Review the bound evidence.';
      }
      const mode = run.proposal?.mode;
      const generated = mode === 'live' && run.proposal?.gateway === 'openrouter'
        && typeof run.proposal?.model === 'string' && run.proposal.model.trim().length > 0;
      const origin = mode === 'prepared' ? 'Prepared candidate' : generated ? 'Generated candidate' : 'Candidate';
      return `${origin} passes the original tests and approved controls in both test environments; awaiting engineering review.`;
    }
    default: return 'No recognized repair result is available.';
  }
}

export function buildReleaseAssessment(run?: RunSummary, bridgeError?: string): ReleaseAssessment {
  const assessment: ReleaseAssessment = {
    tone: 'neutral',
    headline: 'Client workflow: not checked',
    workflow: 'Importing customer records, including records without nicknames.',
    evidence: 'Start a check to run the approved synthetic examples against the baseline and updated fixture.',
    repair: 'An optional repair attempt follows a reproduced break.',
    deployment: DEPLOYMENT,
    nextStep: 'Check this release before deciding which environment to use for the client demonstration.',
  };
  if (!run || run.schema_version !== 'proofrun.v1') {
    if (bridgeError) {
      assessment.tone = 'warning';
      assessment.headline = 'Release check unavailable';
      assessment.evidence = 'The portal could not obtain a recognized worker result.';
      assessment.nextStep = 'Resolve the connection or setup problem and collect a measured result.';
    }
    return assessment;
  }
  assessment.repair = repairDescription(run);
  if (run.finding_status === 'regression_reproduced') {
    assessment.tone = 'warning';
    if (hasReproducedBreak(run)) {
      assessment.headline = 'Client workflow: blocked in the tested update';
      assessment.evidence = 'The same approved record without a nickname passes on the baseline and fails on the update. The remaining controls and original tests pass.';
      assessment.nextStep = 'Use a separately verified baseline environment for the meeting, or review the candidate and validate it in staging first.';
    } else {
      assessment.headline = 'Worker reports a break: review the evidence';
      assessment.evidence = 'The worker reports a reproduced regression, but the complete, consistent baseline/update case records are unavailable here.';
      assessment.nextStep = 'Inspect the bound comparison report before making a release decision.';
    }
  } else if (run.execution_status === 'queued' || run.execution_status === 'running') {
    assessment.headline = 'Checking the client workflow';
    assessment.evidence = 'The worker has not yet established a result for the approved examples.';
    assessment.nextStep = 'Wait for the measured comparison before making a release decision.';
  } else if (run.execution_status === 'completed' && run.finding_status === 'no_difference_observed'
      && stagePassed(run, 'baseline') && stagePassed(run, 'updated')) {
    assessment.tone = 'success';
    assessment.headline = 'Approved examples pass in the tested update';
    assessment.evidence = 'All five approved examples and both original tests pass in both fixture environments. This result covers those checks only.';
    assessment.nextStep = 'Validate the actual client workflow in staging before the demonstration.';
  } else {
    assessment.tone = 'warning';
    assessment.headline = 'Release check incomplete';
    assessment.evidence = 'The recorded execution does not establish a complete passing comparison or a supported reproduced break.';
    assessment.nextStep = 'Review the execution details and rerun after resolving missing setup or evidence.';
  }
  if (bridgeError) {
    assessment.tone = 'warning';
    assessment.evidence += ' The latest portal refresh failed; this summary describes the last recorded worker result.';
    assessment.nextStep = 'Refresh the worker result and check its observation time before relying on this recorded evidence.';
  }
  return assessment;
}

export function buildMeetingBrief(run: RunSummary, observedAt?: string, bridgeError?: string, research?: CustomerResearch): string {
  const assessment = buildReleaseAssessment(run, bridgeError);
  const text = (value: unknown): string => typeof value === 'string' && value.length > 0 ? value : 'unreported';
  const lines = [
    '# ProofRun — client meeting brief',
    '',
    `## ${assessment.headline}`,
    '',
    `**Affected workflow:** ${assessment.workflow}`,
    '',
    `**Evidence:** ${assessment.evidence}`,
    '',
    `**Repair:** ${assessment.repair}`,
    '',
    `**Deployment status:** ${assessment.deployment}`,
    '',
    `**Next step:** ${assessment.nextStep}`,
    '',
    'Scope: customer-nickname-v1; approved synthetic data; direct application-function checks in Docker, not HTTP replay against staging.',
    '',
    'This brief exports a stored worker result; downloading it does not run a new check.',
    '',
    '## Recorded evidence',
    '',
    '- Test environments: Pydantic 1.10.18 baseline / 2.8.2 update.',
    `- Run: ${text(run.run_id)}`,
    `- Last observed by the portal: ${text(observedAt)}`,
    `- Execution / finding / repair: ${text(run.execution_status)} / ${text(run.finding_status)} / ${text(run.repair_status)}`,
    `- Execution target: ${text(run.execution?.target)}; worker: ${text(run.execution?.worker_id)}`,
    `- Proposal mode: ${text(run.proposal?.mode)}; gateway: ${text(run.proposal?.gateway)}; model: ${text(run.proposal?.model)}`,
    `- Source revision: ${text(run.bindings?.['revision'])}`,
    `- Source SHA-256: ${text(run.bindings?.['source_sha256'])}`,
    `- Contract SHA-256: ${text(run.bindings?.['contract_sha256'])}`,
    `- Candidate SHA-256: ${text(run.bindings?.['candidate_sha256'])}`,
    `- Environment manifest SHA-256: ${text(run.bindings?.['environment_manifest_sha256'])}`,
  ];
  if (Array.isArray(run.limitations) && run.limitations.length) {
    lines.push('', '## Recorded limitations', '', ...run.limitations.filter(item => typeof item === 'string').map(item => `- ${item}`));
  }
  if (Array.isArray(run.artifacts) && run.artifacts.length) {
    lines.push('', '## Evidence references', '', 'Retrieve these artifacts through the authenticated portal.', '',
      ...run.artifacts.filter(item => item && typeof item.id === 'string')
        .map(item => `- ${item.id}: SHA-256 ${text(item.sha256)}`));
  }
  if (run.coordination?.provider === 'band') {
    lines.push('', '## Repair coordination — BAND', '',
      `- Mode: ${text(run.coordination.mode)}; handoff status: ${text(run.coordination.status)}`,
      `- Room: ${text(run.coordination.room_id)}; handoff: ${text(run.coordination.handoff_id)}`,
      '- The room carries the proposer/verifier handoff. Executed verification checks determine candidate acceptance.');
  }
  if (research) lines.push(...customerResearchBrief(research));
  return lines.join('\n') + '\n';
}
