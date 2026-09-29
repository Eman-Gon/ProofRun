import { Component, DestroyRef, OnInit, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { catchError, EMPTY, exhaustMap, Subscription, timer } from 'rxjs';
import { extractErrorMessage } from '@duplocloud-internal/ng-common-lib';
import { ProofRunResource, ProofRunService, RunSummary } from './proofrun.service';

@Component({
  selector: 'proofrun-verification',
  standalone: true,
  imports: [CommonModule],
  styleUrl: './proofrun.component.scss',
  template: `
    <main class="proofrun-page">
      <header>
        <div>
          <div class="eyebrow">Approved customer-input verification</div>
          <h1>ProofRun</h1>
          <p>Compare the same customer import across Pydantic 1.10.18 and 2.8.2.</p>
        </div>
        <div class="run-options">
          <label><input type="checkbox" [checked]="requestRepair()" (change)="requestRepair.set($any($event.target).checked)" [disabled]="starting()"> Request generated repair (up to 2 attempts)</label>
          <button type="button" class="run-button" (click)="start()" [disabled]="starting()">
            {{ starting() ? 'Creating verification…' : 'Run verification' }}
          </button>
        </div>
      </header>

      <div class="fixture-note">
        <strong>customer-nickname-v1 · Synthetic input</strong>
        <span>Missing nickname is permitted; invalid objects and a missing required name must be rejected.</span>
        <span>Comparison runs by default. Generated repair requires a configured model; an unavailable model never falls back to a prepared fix.</span>
      </div>
      @if (error()) { <p class="error" role="alert">{{ error() }}</p> }

      <div class="layout">
        <aside aria-label="Verification history">
          <div class="section-title"><h2>Runs</h2><button type="button" (click)="loadList()">Refresh</button></div>
          @if (!runs().length) { <p class="muted">No runs loaded. Run verification to collect measured results.</p> }
          @for (run of runs(); track run.id) {
            <button type="button" class="history-item" [class.selected]="selected()?.id === run.id" (click)="select(run.id)">
              <span>{{ run.name }}</span><small>{{ run.createdAt | date:'medium' }} · {{ run.status }}</small>
            </button>
          }
        </aside>

        <section class="results" aria-live="polite">
          @if (selected(); as resource) {
            <div class="section-title"><h2>Worker-reported results</h2><button type="button" (click)="refreshSelected()">Refresh results</button></div>
            <p class="muted">Portal resource: {{ resource.status }}</p>
            <p class="muted">{{ resource.name }}<br>Resource ID: <code>{{ resource.id }}</code></p>
            @if (resource.result?.bridgeError) { <p class="error">{{ resource.result?.bridgeError }}</p> }
            @if (summary(); as run) {
              <div class="states">
                <div><span>Execution</span><strong [class.bad]="failedExecution(run.execution_status)">{{ label(run.execution_status) }}</strong></div>
                <div><span>Finding</span><strong [class.warning]="run.finding_status === 'regression_reproduced'">{{ label(run.finding_status) }}</strong></div>
                <div><span>Repair</span><strong>{{ label(run.repair_status) }}</strong></div>
              </div>
              <p class="muted">Completion describes workflow execution; the finding and repair verdicts are separate.</p>
              <div class="provenance">
                <span>Execution target: <strong>{{ run.execution?.target || 'unreported' }}</strong></span>
                <span>Proposal mode: <strong>{{ run.proposal?.mode || 'none reported' }}</strong></span>
                <span>Worker: <code>{{ run.execution?.worker_id || 'unreported' }}</code></span>
                <span>Run: <code>{{ run.run_id }}</code></span>
              </div>
              <p class="muted">Stored worker snapshot, last observed {{ resource.result?.lastObservedAt | date:'medium' }}.
                @if (active()) { <span>Polling every 3 seconds.</span> }
                Local execution does not establish Crusoe hosting. A prepared proposal does not establish generated repair.
              </p>

              <h3>Executed cases</h3>
              @if (!run.cases?.length) { <p class="muted">No measured case records have been reported yet.</p> }
              @else {
                <div class="table-scroll"><table>
                  <thead><tr><th>Case</th><th>Stage</th><th>Observed status</th><th>Evidence</th></tr></thead>
                  <tbody>@for (case of run.cases; track $index) {
                    <tr><td><code>{{ case.id || 'unreported' }}</code></td><td>{{ case.stage || 'unreported' }}</td>
                      <td [class.bad]="case.status === 'failed'">{{ case.status || 'unreported' }}</td>
                      <td><details><summary>Case data</summary><pre>{{ case | json }}</pre></details></td></tr>
                  }</tbody>
                </table></div>
              }

              <h3>Bound evidence</h3>
              @if (!run.artifacts?.length) { <p class="muted">Artifacts will appear when the worker records them.</p> }
              @for (artifact of run.artifacts; track artifact.id) {
                <div class="artifact">
                  <button type="button" (click)="download(resource.id, artifact.id)" [disabled]="downloading() === artifact.id">
                    {{ downloading() === artifact.id ? 'Checking and downloading…' : artifact.id }}
                  </button>
                  <span>{{ artifact.size_bytes | number }} bytes · SHA-256 <code>{{ artifact.sha256 }}</code></span>
                </div>
              }
              <details class="binding-details"><summary>Source, environment and proposal bindings</summary>
                <pre>{{ {bindings: run.bindings, execution: run.execution, proposal: run.proposal} | json }}</pre>
              </details>
              @if (run.limitations?.length) {
                <div class="limitations"><h3>Scope and limitations</h3><ul>@for (limit of run.limitations; track $index) { <li>{{ limit }}</li> }</ul></div>
              }
            } @else {
              <p>{{ resource.subStatus || 'Waiting for the DuploCloud worker to dispatch this resource.' }}</p>
              <p class="muted">No worker verdict is available yet. A portal lifecycle state is not a verification result.</p>
            }
          } @else {
            <div class="empty"><h2>Verify the customer case</h2><p>Start a fresh run or select an existing resource to inspect its recorded evidence.</p></div>
          }
        </section>
      </div>
    </main>
  `,
})
export class ProofRunComponent implements OnInit {
  private readonly service = inject(ProofRunService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);
  protected readonly runs = signal<ProofRunResource[]>([]);
  protected readonly selected = signal<ProofRunResource | undefined>(undefined);
  protected readonly starting = signal(false);
  protected readonly requestRepair = signal(false);
  protected readonly downloading = signal('');
  protected readonly error = signal('');
  private poll?: Subscription;
  private params?: Subscription;
  protected readonly summary = computed<RunSummary | undefined>(() => {
    try {
      const raw = this.selected()?.result?.runJson;
      if (!raw) return undefined;
      const run = JSON.parse(raw);
      return run?.schema_version === 'proofrun.v1' ? run : undefined;
    } catch { return undefined; }
  });
  protected readonly active = computed(() => {
    const run = this.summary();
    return run ? ['queued', 'running'].includes(run.execution_status)
      : ['New', 'Updated', 'Processing'].includes(this.selected()?.status ?? '');
  });

  ngOnInit(): void {
    this.loadList();
    this.params = this.route.params.subscribe(params => { if (params['id']) this.observe(params['id']); });
    this.destroyRef.onDestroy(() => { this.poll?.unsubscribe(); this.params?.unsubscribe(); });
  }

  protected loadList(): void {
    this.service.list().subscribe({
      next: list => this.runs.set([...list].sort((a, b) => (b.createdAt ?? '').localeCompare(a.createdAt ?? ''))),
      error: err => this.error.set(extractErrorMessage(err)),
    });
  }

  protected start(): void {
    this.starting.set(true);
    this.error.set('');
    this.service.run(this.requestRepair()).subscribe({
      next: resource => {
        this.starting.set(false);
        this.loadList();
        this.select(resource.id);
      },
      error: err => { this.starting.set(false); this.error.set(extractErrorMessage(err)); },
    });
  }

  protected select(id: string): void {
    const fromView = !!this.route.snapshot.params['id'];
    this.router.navigate([...(fromView ? ['../..'] : []), 'view', id], { relativeTo: this.route });
  }

  protected refreshSelected(): void {
    const id = this.selected()?.id;
    if (id) this.observe(id);
  }

  private observe(id: string): void {
    this.poll?.unsubscribe();
    this.selected.set(undefined);
    this.error.set('');
    this.poll = timer(0, 3000).pipe(exhaustMap(() => this.service.get(id).pipe(
      catchError(err => { this.error.set(extractErrorMessage(err)); return EMPTY; }),
    ))).subscribe(resource => {
      this.selected.set(resource);
      this.error.set('');
      if (!this.active()) this.poll?.unsubscribe();
    });
  }

  protected download(resourceId: string, artifactId: string): void {
    this.downloading.set(artifactId);
    this.service.artifact(resourceId, artifactId).subscribe({
      next: artifact => {
        try {
          const bytes = Uint8Array.from(atob(artifact.base64), c => c.charCodeAt(0));
          const url = URL.createObjectURL(new Blob([bytes], { type: 'application/octet-stream' }));
          const link = document.createElement('a');
          link.href = url;
          link.download = artifact.fileName;
          link.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch { this.error.set('The artifact response could not be decoded.'); }
        this.downloading.set('');
      },
      error: err => { this.downloading.set(''); this.error.set(extractErrorMessage(err)); },
    });
  }

  protected label(status: string): string { return status.replaceAll('_', ' '); }
  protected failedExecution(status: string): boolean { return ['setup_failed', 'timed_out', 'interrupted'].includes(status); }
}
