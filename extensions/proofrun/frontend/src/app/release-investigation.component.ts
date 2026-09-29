import { CommonModule } from '@angular/common';
import { Component, DestroyRef, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router } from '@angular/router';
import { EMPTY, Subscription, catchError, exhaustMap, takeWhile, timeout, timer } from 'rxjs';
import { extractErrorMessage } from '@duplocloud-internal/ng-common-lib';
import {
  ReleaseInvestigation, ReleaseInvestigationRequest, ReleaseInvestigationService, ReleaseTarget,
} from './release-investigation.service';

@Component({
  selector: 'proofrun-release-investigation',
  standalone: true,
  imports: [CommonModule, FormsModule],
  styleUrl: './release-investigation.component.scss',
  template: `
    <main class="release-page">
      <header class="page-header">
        <div><p class="eyebrow">ProofRun · Release investigation</p>
          <h1>Check the release before the meeting</h1>
          <p>Investigate two exact commits against the requirements configured for this product.</p>
        </div>
        <a [href]="fixtureUrl()">Prepared fixture checks ↗</a>
      </header>

      <div class="scope-note">
        <strong>A bounded investigation, with a 5–10 minute budget.</strong>
        <p>Supports configured repositories and HTTP workflows. Results cover the requirements actually exercised;
          they do not establish that the whole product is bug-free. A verified repair and a staging check are separate outcomes.</p>
      </div>

      <div class="release-layout">
        <section class="panel setup-panel" aria-labelledby="setup-title">
          <h2 id="setup-title">Release to investigate</h2>
          @if (loadingTargets()) { <p class="muted" role="status">Loading configured products…</p> }
          @if (configurationError()) {
            <p class="error" role="alert">{{ configurationError() }}</p>
            <button type="button" class="secondary" (click)="loadTargets()" [disabled]="loadingTargets()">Retry configuration</button>
          }
          @if (!loadingTargets() && !configurationError() && !targets().length) {
            <p class="muted">No release targets are configured for this workspace. Configure a supported repository and its requirements before starting.</p>
          }
          <form (ngSubmit)="start()" novalidate>
            <label for="release-target">Product</label>
            <select id="release-target" name="target" [(ngModel)]="targetId" (ngModelChange)="targetChanged()" [disabled]="submitting() || !targets().length">
              <option value="">Select a configured product</option>
              @for (target of targets(); track target.id) { <option [value]="target.id">{{ target.name }}</option> }
            </select>
            <label for="baseline-revision">Baseline commit</label>
            <input id="baseline-revision" name="baseline" [(ngModel)]="baselineRevision" placeholder="Full 40-character commit SHA" autocomplete="off" spellcheck="false" [disabled]="submitting()" aria-describedby="commit-help">
            <label for="candidate-revision">Candidate release commit</label>
            <input id="candidate-revision" name="candidate" [(ngModel)]="candidateRevision" placeholder="Full 40-character commit SHA" autocomplete="off" spellcheck="false" [disabled]="submitting()" aria-describedby="commit-help">
            <p id="commit-help" class="field-help">Use exact commits from the configured repository. Branch names and abbreviated SHAs are not accepted.</p>
            <label for="release-benefit">What should this release improve?</label>
            <textarea id="release-benefit" name="benefit" [(ngModel)]="benefit" rows="3" placeholder="Describe the benefit you need for the client workflow." [disabled]="submitting()"></textarea>
            <label for="release-budget">Investigation budget</label>
            <select id="release-budget" name="budget" [(ngModel)]="budgetSeconds" [disabled]="submitting()">
              <option [ngValue]="300">5 minutes</option><option [ngValue]="600">10 minutes</option>
            </select>
            <p class="field-help">The worker may finish without enough evidence. Reaching the budget does not count as a pass.</p>
            <label class="check-label"><input type="checkbox" name="repair" [(ngModel)]="repair" [disabled]="submitting() || !selectedTarget()?.repair_enabled">
              Try a repair when supported</label>
            @if (!selectedTarget()?.repair_enabled) { <p class="field-help">Repair is not enabled for the selected product.</p> }
            <details class="event-details"><summary>Event reference (optional)</summary>
              <label for="release-event">Event ID</label>
              <input id="release-event" name="event" [(ngModel)]="eventId" autocomplete="off" [disabled]="submitting()">
            </details>
            @if (submissionError()) { <p class="error" role="alert">{{ submissionError() }}</p> }
            <button type="submit" class="primary" [disabled]="submitting() || !selectedTarget() || loadingTargets()">
              {{ submitting() ? 'Starting investigation…' : 'Investigate this release' }}
            </button>
          </form>
          @if (selectedTarget(); as target) {
            <div class="requirements"><h3>Configured requirements</h3>
              @if (!target.requirements.length) { <p class="muted">No requirements were reported for this product.</p> }
              <ul>@for (requirement of target.requirements; track requirement.id) {
                <li>{{ requirement.description }} <code>{{ requirement.id }}</code></li>
              }</ul>
              <p class="field-help">{{ target.staging_configured ? 'Staging is configured. Its measured result is reported separately below.' : 'No staging target is configured for this product.' }}</p>
            </div>
          }
        </section>

        <section class="panel result-panel" aria-labelledby="results-title" aria-live="polite">
          <div class="section-heading"><h2 id="results-title">Investigation evidence</h2>
            @if (selectedId()) { <button type="button" class="secondary" (click)="refresh()">Refresh result</button> }
          </div>
          @if (observationError()) {
            <p class="error" role="alert">{{ observationError() }}</p>
            <p class="muted">{{ run() ? 'The evidence below is the last saved observation. Refresh before relying on its current status.' : 'No measured result is available. A connection failure is not a release verdict.' }}</p>
          }
          @if (pollingStopped()) { <p class="notice">{{ pollingStopped() }}</p> }
          @if (run(); as current) {
            <div class="run-state"><span class="status">{{ label(current.status) }}</span>
              <span class="muted">{{ observing() ? 'Refreshing every 3 seconds' : 'Saved observation' }}</span>
            </div>
            <p class="identity">Run <code>{{ current.id }}</code></p>
            <p class="muted">Last observed {{ observedAt() | date:'medium' }} · Worker updated {{ current.updated_at ? (current.updated_at | date:'medium') : 'unreported' }}</p>
            @if (current.error) { <p class="error">{{ current.error }}</p> }
            <div class="decision" [class.caution]="current.status !== 'completed' || current.result?.recommendation !== 'update' || observationError() || pollingStopped()">
              <p class="eyebrow">{{ current.status === 'completed' ? 'Recorded worker recommendation' : 'Current assessment' }}</p>
              <h3>{{ recommendation(current) }}</h3>
              @if (current.result?.summary) { <p>{{ current.result?.summary }}</p> }
              @if (current.status === 'queued' || current.status === 'running') {
                <p>The investigation is still in progress. Partial evidence does not establish a final release decision.</p>
              }
              @if (current.status === 'failed') { <p>The investigation failed. Review any retained evidence before deciding whether to rerun.</p> }
              <p class="field-help">This recommendation applies to the submitted revisions and recorded checks. It does not deploy the release or establish staging readiness.</p>
            </div>
            <dl class="release-identity">
              <div><dt>Product</dt><dd>{{ current.request.target_id }}</dd></div>
              <div><dt>Baseline</dt><dd><code>{{ current.request.baseline_revision }}</code></dd></div>
              <div><dt>Candidate</dt><dd><code>{{ current.request.candidate_revision }}</code></dd></div>
              <div><dt>Intended benefit</dt><dd>{{ current.request.benefit }}</dd></div>
              <div><dt>Budget</dt><dd>{{ current.request.budget_seconds / 60 }} minutes</dd></div>
            </dl>
            @if (current.result; as result) {
              <h3>Coverage and execution</h3>
              <dl class="execution-grid">
                <div><dt>Requirements exercised</dt><dd>{{ coverage(result.coverage) }}</dd></div>
                <div><dt>Baseline tests</dt><dd>{{ label(result.tests?.baseline?.test_status) }}</dd></div>
                <div><dt>Candidate tests</dt><dd>{{ label(result.tests?.candidate?.test_status) }}</dd></div>
                <div><dt>Investigation agent</dt><dd>{{ label(result.agent?.status) }}</dd></div>
                <div><dt>Staging</dt><dd>{{ label(result.staging?.status) }}</dd></div>
              </dl>
              <p class="field-help">An exercised requirement may still fail. Unreported or untested staging is not a passing staging check.</p>
              @if (result.agent?.summary) { <p>{{ result.agent?.summary }}</p> }
              <h3>Findings</h3>
              @if (!result.findings?.length) {
                <p class="muted">No findings were recorded. Check the coverage, execution status and limitations before interpreting this result.</p>
              }
              @for (finding of result.findings; track $index) {
                <article class="finding">
                  <div class="section-heading"><h4>{{ finding.title }}</h4><span class="status">{{ label(finding.status) }}</span></div>
                  <p class="muted">Requirement <code>{{ finding.requirement_id }}</code></p>
                  @if (finding.repair) {
                    <p><strong>Repair:</strong> {{ label(finding.repair.status) }}</p>
                    <p class="field-help">A repair result does not change what was observed in the original candidate commit.</p>
                  }
                  <details><summary>Finding evidence</summary><pre>{{ finding.evidence | json }}</pre></details>
                  @if (finding.repair) { <details><summary>Repair evidence</summary><pre>{{ finding.repair | json }}</pre></details> }
                </article>
              }
              <h3>Limitations</h3>
              @if (!result.limitations?.length) { <p class="muted">No additional limitations were reported. Scope remains limited to the recorded checks.</p> }
              <ul>@for (limitation of result.limitations; track $index) { <li>{{ limitation }}</li> }</ul>
              <details><summary>Recorded result and artifact references</summary><pre>{{ result | json }}</pre></details>
            }
          } @else if (!observationError()) {
            <div class="empty-state"><h3>{{ selectedId() ? 'Loading the recorded investigation…' : 'No release has been investigated here yet' }}</h3>
              <p>Select a configured product, enter both commits, and start the bounded check. Results will appear here as the worker reports them.</p>
            </div>
          }
        </section>
      </div>
    </main>
  `,
})
export class ReleaseInvestigationComponent implements OnInit {
  private readonly service = inject(ReleaseInvestigationService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);
  protected readonly targets = signal<ReleaseTarget[]>([]);
  protected readonly loadingTargets = signal(false);
  protected readonly configurationError = signal('');
  protected readonly submissionError = signal('');
  protected readonly observationError = signal('');
  protected readonly pollingStopped = signal('');
  protected readonly submitting = signal(false);
  protected readonly observing = signal(false);
  protected readonly selectedId = signal('');
  protected readonly run = signal<ReleaseInvestigation | undefined>(undefined);
  protected readonly observedAt = signal<string | undefined>(undefined);
  protected targetId = '';
  protected baselineRevision = '';
  protected candidateRevision = '';
  protected benefit = '';
  protected budgetSeconds = 300;
  protected repair = false;
  protected eventId = '';
  private poll?: Subscription;
  private params?: Subscription;
  private configuration?: Subscription;
  private submission?: Subscription;

  ngOnInit(): void {
    this.loadTargets();
    this.params = this.route.paramMap.subscribe(params => {
      const id = params.get('id');
      if (id) this.observe(id);
      else {
        this.poll?.unsubscribe();
        this.selectedId.set('');
        this.run.set(undefined);
        this.observing.set(false);
        this.observationError.set('');
        this.pollingStopped.set('');
      }
    });
    this.destroyRef.onDestroy(() => {
      this.poll?.unsubscribe(); this.params?.unsubscribe();
      this.configuration?.unsubscribe(); this.submission?.unsubscribe();
    });
  }

  protected selectedTarget(): ReleaseTarget | undefined {
    return this.targets().find(target => target.id === this.targetId);
  }

  protected targetChanged(): void {
    if (!this.selectedTarget()?.repair_enabled) this.repair = false;
  }

  protected fixtureUrl(): string {
    return this.router.serializeUrl(this.router.createUrlTree(
      this.route.snapshot.paramMap.has('id') ? ['../..'] : ['..'], { relativeTo: this.route },
    ));
  }

  protected loadTargets(): void {
    this.configuration?.unsubscribe();
    this.loadingTargets.set(true);
    this.configurationError.set('');
    this.configuration = this.service.targets().pipe(timeout(20000)).subscribe({
      next: targets => {
        this.targets.set(targets);
        if (!targets.some(target => target.id === this.targetId)) this.targetId = targets.length === 1 ? targets[0].id : '';
        this.targetChanged();
        this.loadingTargets.set(false);
      },
      error: error => { this.loadingTargets.set(false); this.configurationError.set(this.errorText(error)); },
    });
  }

  protected start(): void {
    if (this.submitting()) return;
    this.submissionError.set('');
    const target = this.selectedTarget();
    const baseline = this.baselineRevision.trim();
    const candidate = this.candidateRevision.trim();
    if (!target || !/^[a-fA-F0-9]{40}$/.test(baseline) || !/^[a-fA-F0-9]{40}$/.test(candidate)
        || !this.benefit.trim() || ![300, 600].includes(this.budgetSeconds)) {
      this.submissionError.set('Select a configured product, provide two full 40-character commit SHAs, describe the intended benefit, and choose a 5- or 10-minute budget.');
      return;
    }
    const request: ReleaseInvestigationRequest = {
      target_id: target.id, baseline_revision: baseline.toLowerCase(), candidate_revision: candidate.toLowerCase(),
      benefit: this.benefit.trim(), budget_seconds: this.budgetSeconds, repair: this.repair && target.repair_enabled,
      ...(this.eventId.trim() ? { event_id: this.eventId.trim() } : {}),
    };
    this.submitting.set(true);
    this.submission = this.service.submit(request).pipe(timeout(30000)).subscribe({
      next: run => {
        this.submitting.set(false);
        this.poll?.unsubscribe();
        this.run.set(run);
        this.selectedId.set(run.id);
        this.observedAt.set(new Date().toISOString());
        this.observationError.set('');
        this.pollingStopped.set('');
        const segments = this.route.snapshot.paramMap.has('id') ? ['../', run.id] : [run.id];
        this.router.navigate(segments, { relativeTo: this.route }).then(navigated => {
          if (!navigated) this.observe(run.id);
        }).catch(() => this.observe(run.id));
      },
      error: error => {
        this.submitting.set(false);
        this.submissionError.set(`${this.errorText(error)} The submission outcome is unknown if the request reached the worker; no successful run was inferred.`);
      },
    });
  }

  protected refresh(): void {
    if (this.selectedId()) this.observe(this.selectedId());
  }

  private observe(id: string): void {
    this.poll?.unsubscribe();
    if (this.run()?.id !== id) { this.run.set(undefined); this.observedAt.set(undefined); }
    this.selectedId.set(id);
    this.observationError.set('');
    this.pollingStopped.set('');
    this.observing.set(true);
    const budget = this.run()?.request?.budget_seconds;
    const seconds = Number.isInteger(budget) ? Math.min(600, Math.max(180, budget)) : 600;
    const stopAt = Date.now() + (seconds + 30) * 1000;
    this.poll = timer(0, 3000).pipe(
      takeWhile(() => Date.now() < stopAt),
      exhaustMap(() => this.service.get(id).pipe(timeout(20000), catchError(error => {
        this.observationError.set(this.errorText(error));
        return EMPTY;
      }))),
    ).subscribe({
      next: run => {
        if (run.id !== id) {
          this.observationError.set('The server returned a different investigation. The previous observation has been preserved.');
          return;
        }
        this.run.set(run);
        this.observedAt.set(new Date().toISOString());
        this.observationError.set('');
        if (run.status === 'completed' || run.status === 'failed') {
          this.observing.set(false);
          this.poll?.unsubscribe();
        }
      },
      complete: () => {
        this.observing.set(false);
        this.pollingStopped.set('Automatic refresh reached its observation limit. This does not stop the worker or establish a result. Refresh to retrieve its current status.');
      },
    });
  }

  protected recommendation(run: ReleaseInvestigation): string {
    if (run.status === 'queued') return 'Waiting for the worker';
    if (run.status === 'running') return 'Investigation in progress';
    if (run.status !== 'completed') return 'No final release recommendation';
    return ({ update: 'Update', skip: 'Skip this release', postpone: 'Postpone the decision' } as Record<string, string>)[run.result?.recommendation ?? '']
      ?? 'No final release recommendation';
  }

  protected coverage(value?: { requirements_total: number; requirements_exercised: number }): string {
    if (!value || !Number.isInteger(value.requirements_total) || !Number.isInteger(value.requirements_exercised)
        || value.requirements_total < 0 || value.requirements_exercised < 0 || value.requirements_exercised > value.requirements_total) return 'Unreported';
    return `${value.requirements_exercised} of ${value.requirements_total}`;
  }

  protected label(value?: string): string {
    return typeof value === 'string' && value ? value.replace(/_/g, ' ') : 'Unreported';
  }

  private errorText(error: unknown): string {
    if (error instanceof Error && error.name === 'TimeoutError') return 'The request timed out before a result was received.';
    return extractErrorMessage(error as Parameters<typeof extractErrorMessage>[0]) || 'The request could not be completed.';
  }
}
