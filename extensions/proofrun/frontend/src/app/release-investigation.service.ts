import { Injectable, inject } from '@angular/core';
import { Observable, defer, map } from 'rxjs';

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
    evidence: Record<string, unknown>;
    repair?: { status: string; [key: string]: unknown };
  }[];
  tests?: { baseline?: { test_status: string }; candidate?: { test_status: string } };
  agent?: { status: string; summary?: string };
  staging?: { status: string; [key: string]: unknown };
  limitations?: string[];
  artifacts?: unknown[];
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
            || typeof finding.requirement_id !== 'string' || typeof finding.status !== 'string')
          || (run.result.limitations !== undefined && !Array.isArray(run.result.limitations))))) {
      throw new Error('The server returned an unsupported investigation result. No release recommendation was inferred.');
    }
    return run;
  }
}
