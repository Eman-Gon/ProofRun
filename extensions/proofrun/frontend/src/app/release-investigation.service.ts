import { Injectable, inject } from '@angular/core';
import { Observable, defer, map, shareReplay, timeout } from 'rxjs';
import { EvidenceGraph, EvidenceGraphCache, readEvidenceGraph } from './evidence-graph';
import type { FailureResearch } from './failure-research';

export interface ReleaseTarget {
  id: string;
  name: string;
  requirements: { id: string; description: string }[];
  repair_enabled: boolean;
  staging_configured: boolean;
}

export interface ReleaseInvestigationRequest {
  target_id: string;
  baseline_revision: string;
  candidate_revision: string;
  benefit: string;
  budget_seconds: number;
  repair: boolean;
  event_id?: string;
  failure_research_id?: string;
}

export interface ReleaseInvestigationResult {
  recommendation: 'update' | 'skip' | 'postpone';
  summary: string;
  coverage?: { requirements_total: number; requirements_exercised: number };
  revisions?: { baseline: string; candidate: string };
  findings?: {
    id: string;
    title: string;
    requirement_id: string;
    status: 'confirmed' | 'inconclusive';
    hypothesis?: string;
    cause_status?: string;
    evidence: Record<string, unknown>;
    repair?: { status: string; [key: string]: unknown };
  }[];
  tests?: { baseline?: { test_status: string }; candidate?: { test_status: string } };
  agent?: { status: string; summary?: string };
  staging?: { status: string; [key: string]: unknown };
  limitations?: string[];
  artifacts?: { id: string; sha256: string; bytes: number }[];
}

export interface ReleaseInvestigation {
  id: string;
  status: 'queued' | 'running' | 'completed' | 'failed';
  request: ReleaseInvestigationRequest;
  result?: ReleaseInvestigationResult;
  error?: string;
  created_at?: string;
  updated_at?: string;
}

@Injectable({ providedIn: 'root' })
export class ReleaseInvestigationService {
  private readonly http = inject<any>('REMOTE_DuploHttpClient' as any);
  private readonly session = inject<any>('REMOTE_UserSession' as any);
  private readonly graphs = new EvidenceGraphCache<Observable<EvidenceGraph>>();

  private base(): string {
    const workspace = this.session?.tenant?.TenantId;
    if (!workspace) throw new Error('Select a DuploCloud workspace before investigating a release.');
    return `/v1/aiservicedesk/user/data/workspaces/${encodeURIComponent(workspace)}/environment/extensions/releaseinvestigations`;
  }

  private unwrap(response: any): any {
    return response?.data !== undefined ? response.data : response;
  }

  targets(): Observable<ReleaseTarget[]> {
    return defer(() => this.http.get(`${this.base()}/targets`)).pipe(map(response => {
      const body = this.unwrap(response);
      if (!Array.isArray(body?.targets) || body.targets.some((target: ReleaseTarget) =>
        !target || typeof target.id !== 'string' || typeof target.name !== 'string'
        || typeof target.repair_enabled !== 'boolean' || typeof target.staging_configured !== 'boolean'
        || !Array.isArray(target.requirements) || target.requirements.some(requirement =>
          !requirement || typeof requirement.id !== 'string' || typeof requirement.description !== 'string'))) {
        throw new Error('The server returned an unsupported target configuration. No release check was started.');
      }
      return body.targets;
    }));
  }

  submit(request: ReleaseInvestigationRequest): Observable<ReleaseInvestigation> {
    return defer(() => this.http.post(this.base(), request)).pipe(map(response => this.readRun(response)));
  }

  get(id: string): Observable<ReleaseInvestigation> {
    return defer(() => this.http.get(`${this.base()}/${encodeURIComponent(id)}`)).pipe(map(response => this.readRun(response)));
  }

  graphScope(runId: string): string { return `${this.base()}/${encodeURIComponent(runId)}/graph`; }

  graph(runId: string, revision: string, refresh = false): Observable<EvidenceGraph> {
    const scope = this.graphScope(runId);
    return this.graphs.get(scope, runId, revision, () => defer(() => this.http.get(scope)).pipe(
      timeout(20000), map(response => readEvidenceGraph(this.unwrap(response), 'release', runId)),
      shareReplay({ bufferSize: 1, refCount: true }),
    ), refresh);
  }

  failureResearch(runId: string, findingId: string, requestId: string, query: string): Observable<FailureResearch> {
    return defer(() => this.http.post(`${this.base()}/${encodeURIComponent(runId)}/failure-research`, {
      request_id: requestId, query, finding_id: findingId,
    })).pipe(map(response => this.unwrap(response)));
  }

  artifact(runId: string, artifactId: string): Observable<{ fileName: string; base64: string }> {
    return defer(() => {
      if (!/^release-[a-f0-9]{40}$/.test(runId) || artifactId.length > 128 || !/^[a-z0-9-]+\.(json|diff)$/.test(artifactId)) {
        throw new Error('Select an evidence file listed for this investigation.');
      }
      return this.http.get(`${this.base()}/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}`);
    }).pipe(map(response => {
      const artifact = this.unwrap(response);
      const maxBase64Length = Math.ceil(16 * 1024 * 1024 / 3) * 4;
      if (!artifact || artifact.fileName !== artifactId || typeof artifact.base64 !== 'string'
          || artifact.base64.length > maxBase64Length || artifact.base64.length % 4 !== 0) {
        throw new Error('The evidence download response is invalid or exceeds the 16 MiB limit.');
      }
      return { fileName: artifact.fileName, base64: artifact.base64 };
    }));
  }

  private readRun(response: unknown): ReleaseInvestigation {
    const run = this.unwrap(response);
    if (!run || typeof run.id !== 'string' || !['queued', 'running', 'completed', 'failed'].includes(run.status)
        || !run.request || typeof run.request !== 'object'
        || typeof run.request.target_id !== 'string' || typeof run.request.baseline_revision !== 'string'
        || typeof run.request.candidate_revision !== 'string' || typeof run.request.benefit !== 'string'
        || !Number.isInteger(run.request.budget_seconds)
        || (run.result && (typeof run.result !== 'object'
          || (run.result.findings !== undefined && !Array.isArray(run.result.findings))
          || run.result.findings?.some((finding: any) => !finding || typeof finding.title !== 'string'
            || typeof finding.requirement_id !== 'string' || typeof finding.status !== 'string'
            || (finding.hypothesis !== undefined && typeof finding.hypothesis !== 'string')
            || (finding.cause_status !== undefined && typeof finding.cause_status !== 'string'))
          || (run.result.artifacts !== undefined && (!Array.isArray(run.result.artifacts)
            || run.result.artifacts.some((artifact: any) => !artifact || typeof artifact.id !== 'string'
              || artifact.id.length > 128 || !/^[a-z0-9-]+\.(json|diff)$/.test(artifact.id)
              || typeof artifact.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(artifact.sha256)
              || !Number.isInteger(artifact.bytes) || artifact.bytes < 0 || artifact.bytes > 16 * 1024 * 1024)))
          || (run.result.limitations !== undefined && !Array.isArray(run.result.limitations))))) {
      throw new Error('The server returned an unsupported investigation result. No release recommendation was inferred.');
    }
    return run;
  }
}
