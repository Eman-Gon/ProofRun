import { Injectable, inject } from '@angular/core';
import { Observable, defer, map, shareReplay, timeout } from 'rxjs';
import { EvidenceGraph, EvidenceGraphCache, readEvidenceGraph } from './evidence-graph';
import type { CustomerResearch, CustomerResearchRequest } from './customer-research';
import type { FailureResearch } from './failure-research';

const REST_SEGMENT = 'extensions/proofruns';

export interface RunSummary {
  schema_version: string;
  run_id: string;
  case_id?: string;
  execution_status: string;
  finding_status: string;
  repair_status: string;
  bindings?: Record<string, unknown>;
  execution?: { target?: string; worker_id?: string; [key: string]: unknown };
  proposal?: { mode?: string; gateway?: string; model?: string; [key: string]: unknown };
  coordination?: { provider: 'band'; mode: 'live' | 'mock'; status: string; room_id?: string; handoff_id?: string; [key: string]: unknown };
  cases?: { id?: string; stage?: string; status?: string; [key: string]: unknown }[];
  environments?: Record<string, { observed_version?: string; [key: string]: unknown }>;
  artifacts?: { id: string; sha256: string; size_bytes: number }[];
  attempts?: { attempt: number; repair_status?: string; candidate_sha256?: string }[];
  verification?: Record<string, unknown>;
  limitations?: string[];
}

export interface ProofRunResource {
  id: string;
  name: string;
  status: string;
  subStatus?: string;
  createdAt?: string;
  result?: {
    runId?: string;
    runJson?: string;
    bridgeError?: string;
    lastObservedAt?: string;
  };
}

@Injectable({ providedIn: 'root' })
export class ProofRunService {
  private readonly http = inject<any>('REMOTE_DuploHttpClient' as any);
  private readonly session = inject<any>('REMOTE_UserSession' as any);
  private readonly graphs = new EvidenceGraphCache<Observable<EvidenceGraph>>();
  private base(): string {
    const workspace = this.session?.tenant?.TenantId;
    if (!workspace) throw new Error('Select a DuploCloud workspace before running verification.');
    return `/v1/aiservicedesk/user/data/workspaces/${encodeURIComponent(workspace)}/environment/${REST_SEGMENT}`;
  }
  private unwrap = (r: any) => r?.data !== undefined ? r.data : r;

  list(): Observable<ProofRunResource[]> {
    return this.http.get(this.base()).pipe(map((r: any) => {
      const data = this.unwrap(r);
      return data?.items ?? data ?? [];
    }));
  }

  get(id: string): Observable<ProofRunResource> {
    return this.http.get(`${this.base()}/${encodeURIComponent(id)}`).pipe(map(this.unwrap));
  }

  run(enableRepair: boolean, failureResearchId?: string): Observable<ProofRunResource> {
    // A fresh resource is a fresh run. The backend derives retry identity from its stored resource ID.
    const name = `verification-${Date.now()}-${crypto.randomUUID().slice(0, 8)}`;
    return this.http.post(this.base(), { name, spec: { caseId: 'customer-nickname-v1', enableRepair,
      ...(failureResearchId ? { failureResearchId } : {}),
    } }).pipe(map(this.unwrap));
  }

  artifact(resourceId: string, artifactId: string): Observable<{ fileName: string; base64: string }> {
    return this.http.get(`${this.base()}/${encodeURIComponent(resourceId)}/artifacts/${encodeURIComponent(artifactId)}`)
      .pipe(map(this.unwrap));
  }

  researchScope(resourceId: string): string {
    return `${this.base()}/${encodeURIComponent(resourceId)}`;
  }

  graphScope(resourceId: string): string { return `${this.researchScope(resourceId)}/graph`; }

  graph(resourceId: string, runId: string, revision: string, refresh = false): Observable<EvidenceGraph> {
    const scope = this.graphScope(resourceId);
    return this.graphs.get(scope, runId, revision, () => defer(() => this.http.get(scope)).pipe(
      timeout(20000), map(response => readEvidenceGraph(this.unwrap(response), 'fixture', runId)),
      shareReplay({ bufferSize: 1, refCount: true }),
    ), refresh);
  }

  research(resourceId: string, request: CustomerResearchRequest): Observable<CustomerResearch> {
    return this.http.post(`${this.researchScope(resourceId)}/research`, request).pipe(map(this.unwrap));
  }

  failureResearch(resourceId: string, clientNonce: string, query: string): Observable<FailureResearch> {
    return this.http.post(`${this.researchScope(resourceId)}/failure-research`, { clientNonce, query }).pipe(map(this.unwrap));
  }
}
