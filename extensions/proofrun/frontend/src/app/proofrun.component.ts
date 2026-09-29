import { Component, DestroyRef, OnInit, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { catchError, EMPTY, exhaustMap, Subscription, timer } from 'rxjs';
import { extractErrorMessage } from '@duplocloud-internal/ng-common-lib';
import { ProofRunResource, ProofRunService, RunSummary } from './proofrun.service';
import { buildMeetingBrief, buildReleaseAssessment } from './release-summary';
import { CustomerResearch, CustomerResearchRequest, metricLabel, previousCompleteMonth, researchRequest, researchStatusText, safeResearchSource } from './customer-research';
import { FailureResearchComponent } from './failure-research.component';
import { FailureResearch, fixtureFailureQuery } from './failure-research';

interface ResearchSession {
  pending: boolean;
  request?: CustomerResearchRequest;
  result?: CustomerResearch;
  error?: string;
}

@Component({
  selector: 'proofrun-verification',
  standalone: true,
  imports: [CommonModule, FailureResearchComponent],
  styleUrl: './proofrun.component.scss',
  template: `
    <main class="proofrun-page">
      <header>
        <div>
          <div class="eyebrow">Release check · Customer import</div>
          <h1>ProofRun</h1>
          <p>Client meeting in 20 minutes. Check the import behavior you plan to demonstrate.</p>
          <button type="button" (click)="openReleaseInvestigation()">Investigate a repository release →</button>
        </div>
        <div class="run-options">
          <button type="button" class="run-button" (click)="start()" [disabled]="starting()">
            {{ starting() ? 'Starting release check…' : 'Check this release' }}
          </button>
          <label><input type="checkbox" [checked]="requestRepair()" (change)="requestRepair.set($any($event.target).checked)" [disabled]="starting()"> Try a repair after a reproduced break</label>
          <span class="muted">Optional · requires a configured model · up to 2 attempts</span>
        </div>
      </header>

      <section class="fixture-note" aria-labelledby="release-scope">
        <div class="section-title"><h2 id="release-scope">The release being checked</h2><span class="scope-badge">Registered synthetic fixture</span></div>
        <p>Customer import · Pydantic <strong>1.10.18 → 2.8.2</strong></p>
        <p>Runs the checked-in application functions in Docker against the approved examples below.
          <strong>Staging API not checked. Nothing is deployed.</strong></p>
        <div class="approved-examples" aria-label="Approved synthetic customer examples">
          @for (example of approvedExamples; track example.id) {
            <div class="example">
              <strong>{{ example.name }}</strong>
              <code>{{ example.input }}</code>
              <span>{{ example.expected }}</span>
            </div>
          }
        </div>
        <details class="fixture-details"><summary>Case identity and repair scope</summary>
          <p>Registered case: <code>customer-nickname-v1</code> · Application: <code>demo/upgrade/app.py</code></p>
          <p>These synthetic requirements are fixed before the run. A repair can change the allowed application copy;
            acceptance tests and dependency pins remain fixed. An unavailable model never falls back to a prepared fix.</p>
        </details>
      </section>
      @if (error()) { <p class="error" role="alert">{{ error() }}</p> }

      <div class="layout">
        <aside aria-label="Verification history">
          <div class="section-title"><h2>Runs</h2><button type="button" (click)="loadList()">Refresh</button></div>
          @if (!runs().length) { <p class="muted">No runs loaded. Check this release to collect measured results.</p> }
          @for (run of runs(); track run.id) {
            <button type="button" class="history-item" [class.selected]="selected()?.id === run.id" (click)="select(run.id)">
              <span>{{ run.name }}</span><small>{{ run.createdAt | date:'medium' }} · {{ run.status }}</small>
            </button>
          }
        </aside>

        <section class="results" aria-live="polite">
          <div class="assessment" [class.assessment-warning]="assessment().tone === 'warning'" [class.assessment-success]="assessment().tone === 'success'">
            <div class="eyebrow">For the client meeting</div>
            <h2>{{ assessment().headline }}</h2>
            <p class="assessment-workflow">{{ assessment().workflow }}</p>
            <dl>
              <div><dt>Evidence</dt><dd>{{ assessment().evidence }}</dd></div>
              <div><dt>Repair</dt><dd>{{ assessment().repair }}</dd></div>
              <div><dt>Deployment</dt><dd>{{ assessment().deployment }}</dd></div>
            </dl>
            <div class="next-step"><strong>Next step</strong><p>{{ assessment().nextStep }}</p></div>
            @if (summary()) {
              <button type="button" class="brief-button" (click)="downloadBrief()">Download meeting brief</button>
              <p class="muted brief-note">Exports the stored assessment, run identity and reported evidence as Markdown.</p>
            }
          </div>
          @if (selected(); as resource) {
            <section class="customer-research" aria-labelledby="research-title">
              <div class="section-title"><h2 id="research-title">Customer context</h2><span class="scope-badge">Similarweb</span></div>
              <p class="muted">Look up estimated website visits for the client meeting. This context does not change the release or repair verdict.</p>
              <form class="research-form" (submit)="$event.preventDefault(); fetchResearch()">
                <label>Customer website domain
                  <input type="text" placeholder="similarweb.com" autocomplete="off" maxlength="253" [value]="researchDomain()"
                    (input)="researchDomain.set($any($event.target).value)" [disabled]="researchSession()?.pending">
                </label>
                <label>Completed month
                  <input type="month" min="2000-01" [max]="latestResearchMonth" [value]="researchMonth()"
                    (input)="researchMonth.set($any($event.target).value)" [disabled]="researchSession()?.pending">
                </label>
                <button type="submit" [disabled]="researchSession()?.pending">
                  {{ researchSession()?.pending ? 'Fetching customer context…' : researchSession()?.error ? 'Retry customer context' : 'Fetch customer context' }}
                </button>
              </form>
              <p class="muted">One explicit fetch for one month; may use Similarweb API credits. Refreshing verification does not fetch research.
                Keep the downloaded brief: context is held only for this page session.</p>
              @if (researchSession()?.error; as message) { <p class="error" role="alert">{{ message }}</p> }
              @if (researchSession()?.result; as research) {
                <div class="research-result">
                  <h3>{{ researchStatusText(research) }}</h3>
                  <p><strong>{{ research.domain }}</strong> · {{ research.period.start_date }} to {{ research.period.end_date }}</p>
                  <p class="muted">Provider: {{ research.provider }} · Retrieved {{ research.observed_at | date:'medium' }}</p>
                  @if (research.status === 'completed') {
                    @for (metric of research.metrics; track metric.name) {
                      <p class="research-metric"><span>{{ metricLabel(metric.name) }}</span><strong>{{ metric.value | number:'1.0-2' }} {{ metric.unit }}</strong></p>
                    }
                  }
                  @if (research.error) { <p>{{ research.error.message }}</p> }
                  @if (research.limitations.length) { <ul class="muted">@for (limit of research.limitations; track $index) { <li>{{ limit }}</li> }</ul> }
                  @for (source of research.sources; track $index) {
                    @if (safeResearchSource(source.url); as sourceUrl) { <a [href]="sourceUrl" target="_blank" rel="noopener noreferrer">{{ source.title }}</a> }
                  }
                </div>
              }
            </section>
            <div class="section-title"><h2>Recorded check results</h2><button type="button" (click)="refreshSelected()">Refresh results</button></div>
            @if (resource.result?.bridgeError) { <p class="error">{{ resource.result?.bridgeError }}</p> }
            @if (summary(); as run) {
              <div class="states">
                <div><span>Execution</span><strong [class.bad]="failedExecution(run.execution_status)">{{ label(run.execution_status) }}</strong></div>
                <div><span>Finding</span><strong [class.warning]="run.finding_status === 'regression_reproduced'">{{ label(run.finding_status) }}</strong></div>
                <div><span>Repair</span><strong>{{ label(run.repair_status) }}</strong></div>
              </div>
              <p class="muted">Completion describes workflow execution; the finding and repair verdicts are separate.</p>
              @if (run.finding_status === 'regression_reproduced') {
                <proofrun-failure-research kind="fixture" [resourceId]="resource.id" [runId]="run.run_id"
                  findingId="regression" findingTitle="The worker reproduced a regression in the registered customer import fixture."
                  [initialQuery]="failureQuery(run)" [repairAvailable]="run.repair_status !== 'unavailable'" [repairBusy]="starting()"
                  repairUnavailableReason="The worker reported that repair is unavailable. Restore the configured model before retrying; the research brief is available for engineering review."
                  (repairRequested)="repairWithResearch($event)"></proofrun-failure-research>
              }
              @if (run.coordination?.provider === 'band') {
                <div class="coordination">
                  <h3>BAND repair handoff</h3>
                  <p>Mode: <strong>{{ run.coordination?.mode }}</strong> · Handoff: <strong>{{ label(run.coordination?.status || 'unreported') }}</strong></p>
                  <p class="muted">The room carries the proposer/verifier handoff. Executed verification checks determine candidate acceptance.</p>
                  <p class="muted">Room: <code>{{ run.coordination?.room_id || 'unreported' }}</code> · Handoff: <code>{{ run.coordination?.handoff_id || 'unreported' }}</code></p>
                </div>
              }
              <p class="muted">Stored worker snapshot, last observed {{ resource.result?.lastObservedAt | date:'medium' }}.
                @if (active()) { <span>Polling every 3 seconds.</span> }
              </p>

              <h3>Executed cases</h3>
              @if (!run.cases?.length) { <p class="muted">No measured case records have been reported yet.</p> }
              @else {
                <div class="table-scroll"><table>
                  <thead><tr><th>Case</th><th>Stage</th><th>Observed status</th><th>Evidence</th></tr></thead>
                  <tbody>@for (case of run.cases; track $index) {
                    <tr><td>{{ caseLabel(case.id) }}</td><td>{{ stageLabel(case.stage) }}</td>
                      <td [class.bad]="case.status === 'failed' || case.status === 'error'">{{ case.status || 'unreported' }}</td>
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
                <div class="provenance">
                  <span>Execution target: <strong>{{ run.execution?.target || 'unreported' }}</strong></span>
                  <span>Proposal mode: <strong>{{ run.proposal?.mode || 'none reported' }}</strong></span>
                  <span>Worker: <code>{{ run.execution?.worker_id || 'unreported' }}</code></span>
                  <span>Run: <code>{{ run.run_id }}</code></span>
                </div>
                <p class="muted">Local execution does not establish Crusoe hosting. A prepared proposal does not establish generated repair.</p>
                <pre>{{ {bindings: run.bindings, execution: run.execution, proposal: run.proposal} | json }}</pre>
              </details>
              @if (run.limitations?.length) {
                <div class="limitations"><h3>Scope and limitations</h3><ul>@for (limit of run.limitations; track $index) { <li>{{ limit }}</li> }</ul></div>
              }
            } @else {
              <p>{{ resource.subStatus || 'Waiting for the DuploCloud worker to dispatch this resource.' }}</p>
              <p class="muted">No worker verdict is available yet. A portal lifecycle state is not a verification result.</p>
            }
            <details class="binding-details"><summary>Portal resource details</summary>
              <p class="muted">{{ resource.name }}<br>Resource ID: <code>{{ resource.id }}</code><br>Portal state: {{ resource.status }}</p>
            </details>
          } @else {
            <p class="muted">Start a fresh check or select a previous run to review its recorded results.</p>
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
  private readonly observationError = signal('');
  protected readonly researchDomain = signal('');
  protected readonly latestResearchMonth = previousCompleteMonth();
  protected readonly researchMonth = signal(this.latestResearchMonth);
  private readonly researchSessions = signal<Record<string, ResearchSession>>({});
  protected readonly researchSession = computed(() => {
    const id = this.selected()?.id;
    return id ? this.researchSessions()[this.service.researchScope(id)] : undefined;
  });
  protected readonly metricLabel = metricLabel;
  protected readonly researchStatusText = researchStatusText;
  protected readonly safeResearchSource = safeResearchSource;
  protected readonly failureQuery = fixtureFailureQuery;
  protected readonly approvedExamples = [
    { id: 'nickname_omitted', name: 'Nickname omitted', input: '{"name":"Grace"}', expected: 'Accept; nickname is null.' },
    { id: 'nickname_null', name: 'Nickname is null', input: '{"name":"Grace","nickname":null}', expected: 'Accept; keep null.' },
    { id: 'nickname_string', name: 'Nickname is text', input: '{"name":"Grace","nickname":"  Amazing Grace  "}', expected: 'Accept; preserve the text and spaces.' },
    { id: 'nickname_object_rejected', name: 'Nickname is an object', input: '{"name":"Grace","nickname":{"unexpected":"object"}}', expected: 'Reject invalid nickname.' },
    { id: 'required_name_rejected', name: 'Required name omitted', input: '{"nickname":"Grace"}', expected: 'Reject missing name.' },
  ];
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
  protected readonly assessment = computed(() => buildReleaseAssessment(
    this.summary(), this.selected()?.result?.bridgeError || this.observationError(),
  ));

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

  protected start(failureResearchId?: string): void {
    if (this.starting()) return;
    this.starting.set(true);
    this.error.set('');
    this.service.run(failureResearchId ? true : this.requestRepair(), failureResearchId).subscribe({
      next: resource => {
        this.starting.set(false);
        this.loadList();
        this.select(resource.id);
      },
      error: err => { this.starting.set(false); this.error.set(extractErrorMessage(err)); },
    });
  }

  protected repairWithResearch(research: FailureResearch): void {
    if (research.context.kind !== 'fixture' || research.context.run_id !== this.summary()?.run_id
        || research.context.finding_id !== 'regression' || research.status !== 'completed') return;
    this.start(research.research_id);
  }

  protected select(id: string): void {
    const fromView = !!this.route.snapshot.params['id'];
    this.router.navigate([...(fromView ? ['../..'] : []), 'view', id], { relativeTo: this.route });
  }

  protected openReleaseInvestigation(): void {
    const fromView = !!this.route.snapshot.params['id'];
    this.router.navigate([...(fromView ? ['../..'] : []), 'releases'], { relativeTo: this.route });
  }

  protected refreshSelected(): void {
    const id = this.selected()?.id;
    if (id) this.observe(id);
  }

  private observe(id: string): void {
    this.poll?.unsubscribe();
    if (this.selected()?.id !== id) this.selected.set(undefined);
    this.error.set('');
    this.observationError.set('');
    this.poll = timer(0, 3000).pipe(exhaustMap(() => this.service.get(id).pipe(
      catchError(err => {
        const message = extractErrorMessage(err);
        this.error.set(message);
        this.observationError.set(message);
        return EMPTY;
      }),
    ))).subscribe(resource => {
      this.selected.set(resource);
      this.error.set('');
      this.observationError.set('');
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

  protected downloadBrief(): void {
    const run = this.summary();
    if (!run) return;
    const resource = this.selected();
    const brief = buildMeetingBrief(run, resource?.result?.lastObservedAt, resource?.result?.bridgeError || this.observationError(), this.researchSession()?.result);
    const url = URL.createObjectURL(new Blob([brief], { type: 'text/markdown;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `proofrun-meeting-brief-${run.run_id.replace(/[^a-zA-Z0-9_-]/g, '_')}.md`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  protected fetchResearch(): void {
    const id = this.selected()?.id;
    if (!id) return;
    const scope = this.service.researchScope(id);
    const previous = this.researchSessions()[scope];
    if (previous?.pending) return;
    let request: CustomerResearchRequest;
    try {
      request = researchRequest(this.researchDomain(), this.researchMonth(), crypto.randomUUID());
    } catch (err) {
      this.researchSessions.update(sessions => ({ ...sessions, [scope]: { ...previous, pending: false, error: (err as Error).message } }));
      return;
    }
    // Retry an ambiguous transport outcome with the same nonce. A completed fetch is replaced only by another explicit action.
    if (previous?.request?.domain === request.domain && previous.request.month === request.month) request = previous.request;
    this.researchSessions.update(sessions => ({ ...sessions, [scope]: { ...previous, request, pending: true, error: undefined } }));
    this.service.research(id, request).subscribe({
      next: result => this.researchSessions.update(sessions => ({ ...sessions, [scope]: { pending: false, result } })),
      error: err => this.researchSessions.update(sessions => ({ ...sessions, [scope]: { ...sessions[scope], pending: false, error: extractErrorMessage(err) } })),
    });
  }

  protected caseLabel(id?: string): string {
    return this.approvedExamples.find(example => example.id === id)?.name
      ?? ({
        'test_existing.TestExisting.test_explicit_nickname': 'Existing test: explicit nickname',
        'test_existing.TestExisting.test_explicit_none': 'Existing test: null nickname',
      } as Record<string, string>)[id ?? ''] ?? id ?? 'Unreported case';
  }

  protected stageLabel(stage?: string): string {
    return ({
      baseline: 'Before update', updated: 'After update',
      repaired_baseline: 'Repair · before update', repaired_updated: 'Repair · after update',
    } as Record<string, string>)[stage ?? ''] ?? stage ?? 'Unreported stage';
  }

  protected label(status: string): string { return status.replaceAll('_', ' '); }
  protected failedExecution(status: string): boolean { return ['setup_failed', 'timed_out', 'interrupted'].includes(status); }
}
