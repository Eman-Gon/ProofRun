import type { RunSummary } from './proofrun.service';

export type FailureResearchKind = 'fixture' | 'release';
export interface FailureResearchScope { kind: FailureResearchKind; runId: string; findingId: string }
export interface FailureResearchRequest { requestId: string; query: string }
export interface FailureResearch {
  schema_version: 'proofrun.failure-research.v1';
  research_id: string;
  request_id: string;
  context: { kind: FailureResearchKind; run_id: string; finding_id: string; evidence_sha256: string; [key: string]: unknown };
  query: string;
  status: 'completed' | 'no_sources' | 'unavailable';
  summary: string;
  sources: { id: string; title: string; url: string; excerpt: string }[];
  suggested_fixes: { description: string; source_ids: string[] }[];
  observed_at: string;
  provenance: Record<string, unknown>;
  error?: { code: string; message: string } | null;
  limitations: string[];
}

export const FAILURE_QUERY_LIMIT = 1200;

export function normalizedFailureQuery(value: string): string {
  const query = value.trim();
  if (!query || query.length > FAILURE_QUERY_LIMIT || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(query)) {
    throw new Error(`Enter a search query of 1–${FAILURE_QUERY_LIMIT} characters without control characters.`);
  }
  return query;
}

// Keep the same paid-request identity after an ambiguous transport failure, even
// if the query is edited and restored. Only an explicit new search renews it.
export class FailureResearchRequests {
  private requests = new Map<string, FailureResearchRequest>();
  request(scope: string, value: string, refresh: boolean, nonce: () => string): FailureResearchRequest {
    const query = normalizedFailureQuery(value);
    const key = JSON.stringify([scope, query]);
    const previous = this.requests.get(key);
    if (previous && !refresh) return previous;
    const request = { requestId: nonce(), query };
    this.requests.set(key, request);
    return request;
  }
}

export function safeFailureSource(value: string): string | undefined {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : undefined;
  } catch { return undefined; }
}

const text = (value: unknown, max = 24000): value is string => typeof value === 'string' && value.length <= max;

export function readFailureResearch(value: unknown, scope: FailureResearchScope, query: string): FailureResearch {
  const report = value as FailureResearch;
  const invalid = () => new Error('The server returned unsupported research or research for a different failure. No advice was accepted.');
  if (!report || report.schema_version !== 'proofrun.failure-research.v1'
      || !text(report.research_id, 256) || !report.research_id || !text(report.request_id, 256)
      || !report.context || report.context.kind !== scope.kind || report.context.run_id !== scope.runId
      || report.context.finding_id !== scope.findingId || !/^[a-f0-9]{64}$/.test(report.context.evidence_sha256)
      || report.query !== query || !['completed', 'no_sources', 'unavailable'].includes(report.status)
      || !text(report.summary) || !text(report.observed_at, 100) || !Number.isFinite(Date.parse(report.observed_at))
      || !report.provenance || typeof report.provenance !== 'object' || Array.isArray(report.provenance)
      || !Array.isArray(report.sources) || report.sources.length > 5
      || report.sources.some(source => !source || !text(source.id, 256) || !source.id
        || !text(source.title, 2000) || !text(source.url, 8000) || !safeFailureSource(source.url) || !text(source.excerpt))
      || new Set(report.sources.map(source => source.id)).size !== report.sources.length
      || !Array.isArray(report.suggested_fixes) || report.suggested_fixes.length > 3
      || report.suggested_fixes.some(fix => !fix || !text(fix.description) || !Array.isArray(fix.source_ids)
        || !fix.source_ids.length || fix.source_ids.some(id => !report.sources.some(source => source.id === id)))
      || !Array.isArray(report.limitations) || report.limitations.length > 50 || report.limitations.some(limit => !text(limit))
      || (report.error != null && (!text(report.error.code, 256) || !text(report.error.message)))) throw invalid();
  if (report.status === 'completed' && !report.sources.some(source => safeFailureSource(source.url))) throw invalid();
  if (report.status !== 'completed' && (report.sources.length || report.suggested_fixes.length)) throw invalid();
  return report;
}

export function fixtureFailureQuery(run?: RunSummary): string {
  if (run?.case_id !== 'customer-nickname-v1' || run.finding_status !== 'regression_reproduced'
      || run.environments?.baseline?.observed_version !== '1.10.18'
      || run.environments?.updated?.observed_version !== '2.8.2' || !Array.isArray(run.cases)) return '';
  const before = run.cases.filter(row => row?.id === 'nickname_omitted' && row.stage === 'baseline');
  const after = run.cases.filter(row => row?.id === 'nickname_omitted' && row.stage === 'updated');
  if (before.length !== 1 || before[0].status !== 'passed' || after.length !== 1
      || !['failed', 'error'].includes(after[0].status ?? '') || typeof after[0]['detail'] !== 'string'
      || !/ValidationError/.test(after[0]['detail']) || !/Field required|type=missing/.test(after[0]['detail'])) return '';
  // Deliberately use a fixed, public package/error description, never the raw
  // traceback, input body, customer name, source code, or filesystem paths.
  return 'Pydantic 1.10.18 to 2.8.2 upgrade: ValidationError Field required when an Optional field is omitted. Related GitHub issues, official migration documentation and release notes.';
}

export function failureResearchBrief(report: FailureResearch): string {
  // Plain text is intentional: external text cannot introduce active Markdown.
  return [
    'ProofRun — failure research',
    'Unverified research. Only a fresh test run can establish whether a proposed repair works.',
    `Run: ${report.context.run_id}; finding: ${report.context.finding_id}`,
    `Research: ${report.research_id}; retrieved: ${report.observed_at}; status: ${report.status}`,
    `Evidence SHA-256: ${report.context.evidence_sha256}`,
    '', 'Search query sent to OpenRouter web search:', report.query,
    '', 'Research summary (unverified):', report.summary,
    ...(report.error ? [`Availability: ${report.error.message}`] : []),
    '', 'Sources:',
    ...report.sources.map(source => `${source.id}: ${source.title}\n${safeFailureSource(source.url) ?? '[Unsafe source link omitted]'}\n${source.excerpt}`),
    '', 'Suggested fixes — not tested:',
    ...report.suggested_fixes.map(fix => `${fix.description}\nSources: ${fix.source_ids.join(', ')}`),
    '', 'Limitations:', ...report.limitations,
  ].join('\n') + '\n';
}
