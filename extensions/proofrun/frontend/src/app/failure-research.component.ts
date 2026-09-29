import { CommonModule } from '@angular/common';
import { Component, EventEmitter, Input, OnChanges, OnDestroy, Output, signal } from '@angular/core';
import { inject } from '@angular/core';
import { defer, Subscription, timeout } from 'rxjs';
import { extractErrorMessage } from '@duplocloud-internal/ng-common-lib';
import { ProofRunService } from './proofrun.service';
import { ReleaseInvestigationService } from './release-investigation.service';
import {
  FAILURE_QUERY_LIMIT, FailureResearch, FailureResearchKind, FailureResearchRequests,
  failureResearchBrief, readFailureResearch, safeFailureSource,
} from './failure-research';

@Component({
  selector: 'proofrun-failure-research',
  standalone: true,
  imports: [CommonModule],
  styleUrl: './failure-research.component.scss',
  template: `
    <section class="failure-research">
      <button type="button" class="research-toggle" [attr.aria-expanded]="expanded()" (click)="expanded.set(!expanded())">
        {{ expanded() ? 'Hide failure research' : 'Research this failure' }}
      </button>
      @if (expanded()) {
        <div class="research-content">
          <h3>Has anyone else hit this problem?</h3>
          <p class="muted">Search related GitHub issues, official documentation and release notes using OpenRouter web search.</p>
          <p><strong>Recorded finding:</strong> {{ findingTitle }}</p>
          <form (submit)="$event.preventDefault(); search(false)">
            <label>Review the query sent to search
              <textarea rows="4" [value]="query()" (input)="changeQuery($any($event.target).value)"
                [attr.maxlength]="queryLimit" autocomplete="off" spellcheck="false"
                placeholder="Package, exact versions and a public error message. Leave out customer data, private source code and credentials."></textarea>
            </label>
            <p class="muted">Only this query is sent externally. Check it before searching. Searches may use OpenRouter credits; refreshing the run does not start research.</p>
            <div class="actions">
              <button type="submit" [disabled]="pending() || !query().trim()">
                {{ pending() ? 'Researching failure…' : error() ? 'Retry this request' : 'Search related issues and docs' }}
              </button>
              @if (hasRequested()) {
                <button type="button" class="secondary" (click)="search(true)" [disabled]="pending() || !query().trim()">Start a new search · may use credits</button>
              }
            </div>
          </form>
          @if (error()) { <p class="error" role="alert">{{ error() }}</p> }
          @if (report(); as research) {
            <div class="research-result" aria-live="polite">
              <div class="advisory">Research is unverified. It does not change the recorded finding or repair verdict.</div>
              <p class="muted">Retrieved {{ research.observed_at | date:'medium' }} · {{ research.status.replaceAll('_', ' ') }}</p>
              @if (research.status === 'no_sources') {
                <p><strong>No usable sources were found.</strong> This does not establish that the issue is new or that a repair is available.</p>
              }
              @if (research.status === 'unavailable') { <p><strong>Failure research is unavailable.</strong> Your recorded finding is unchanged.</p> }
              @if (research.error) { <p>{{ research.error.message }}</p> }
              @if (research.summary) { <h4>Research summary</h4><p class="external-text">{{ research.summary }}</p> }
              @if (research.sources.length) {
                <h4>Related reports and documentation</h4>
                <ol class="sources">@for (source of research.sources; track source.id) {
                  <li>
                    @if (safeSource(source.url); as url) { <a [href]="url" target="_blank" rel="noopener noreferrer">{{ source.title }}</a> }
                    @else { <span>{{ source.title }} · source link unavailable</span> }
                    <small>{{ source.id }}</small>
                    @if (source.excerpt) { <p class="external-text">{{ source.excerpt }}</p> }
                  </li>
                }</ol>
              }
              @if (research.suggested_fixes.length) {
                <h4>Suggested fixes · not tested</h4>
                <ul>@for (fix of research.suggested_fixes; track $index) {
                  <li><p class="external-text">{{ fix.description }}</p><small>Sources: {{ fix.source_ids.join(', ') }}</small></li>
                }</ul>
              }
              @if (research.limitations.length) { <ul class="muted">@for (limit of research.limitations; track $index) { <li>{{ limit }}</li> }</ul> }
              <div class="actions">
                <button type="button" class="secondary" (click)="download()">Download research brief</button>
                @if (research.status === 'completed' && repairAvailable) {
                  <button type="button" (click)="requestRepair()" [disabled]="repairBusy">{{ repairBusy ? 'Starting repair check…' : 'Try a repair with this research' }}</button>
                }
              </div>
              @if (research.status === 'completed') {
                <p class="muted">{{ repairAvailable ? 'Starts a fresh run with this research as advisory context. The original tests and requirements still decide whether the repair passes. Nothing is deployed.' : repairUnavailableReason }}</p>
              }
              <details><summary>Research identity and provider</summary><pre>{{ {research_id: research.research_id, context: research.context, provenance: research.provenance} | json }}</pre></details>
            </div>
          }
        </div>
      }
    </section>
  `,
})
export class FailureResearchComponent implements OnChanges, OnDestroy {
  @Input() kind: FailureResearchKind = 'fixture';
  @Input() resourceId = '';
  @Input() runId = '';
  @Input() findingId = 'regression';
  @Input() findingTitle = '';
  @Input() initialQuery = '';
  @Input() repairAvailable = false;
  @Input() repairUnavailableReason = 'Repair is not enabled for this product. Download the research brief for engineering review.';
  @Input() repairBusy = false;
  @Output() repairRequested = new EventEmitter<FailureResearch>();
  private readonly fixtureService = inject(ProofRunService);
  private readonly releaseService = inject(ReleaseInvestigationService);
  private readonly requests = new FailureResearchRequests();
  private subscription?: Subscription;
  private scope = '';
  private generation = 0;
  protected readonly expanded = signal(false);
  protected readonly query = signal('');
  protected readonly pending = signal(false);
  protected readonly hasRequested = signal(false);
  protected readonly error = signal('');
  protected readonly report = signal<FailureResearch | undefined>(undefined);
  protected readonly queryLimit = FAILURE_QUERY_LIMIT;
  protected readonly safeSource = safeFailureSource;

  ngOnChanges(): void {
    const scope = JSON.stringify([this.kind, this.resourceId, this.runId, this.findingId]);
    if (scope === this.scope) return;
    this.scope = scope;
    this.cancel();
    this.expanded.set(false);
    this.query.set(this.initialQuery);
    this.hasRequested.set(false);
    this.error.set('');
    this.report.set(undefined);
  }

  ngOnDestroy(): void { this.cancel(); }

  protected changeQuery(value: string): void {
    if (value === this.query()) return;
    this.cancel();
    this.query.set(value);
    this.error.set('');
    this.report.set(undefined);
  }

  private cancel(): void {
    this.generation++;
    this.subscription?.unsubscribe();
    this.pending.set(false);
  }

  protected search(refresh: boolean): void {
    if (this.pending()) return;
    let request;
    try {
      request = this.requests.request(this.scope, this.query(), refresh, () => crypto.randomUUID());
    } catch (error) { this.error.set((error as Error).message); return; }
    const scope = { kind: this.kind, runId: this.runId, findingId: this.findingId };
    const generation = ++this.generation;
    this.pending.set(true);
    this.hasRequested.set(true);
    this.error.set('');
    this.report.set(undefined);
    this.subscription = defer(() => this.kind === 'fixture'
      ? this.fixtureService.failureResearch(this.resourceId, request.requestId, request.query)
      : this.releaseService.failureResearch(this.runId, this.findingId, request.requestId, request.query)
    ).pipe(timeout(120000)).subscribe({
      next: value => {
        if (generation !== this.generation) return;
        try { this.report.set(readFailureResearch(value, scope, request.query)); }
        catch (error) { this.error.set((error as Error).message); }
        this.pending.set(false);
      },
      error: error => {
        if (generation !== this.generation) return;
        this.pending.set(false);
        const message = error?.name === 'TimeoutError' ? 'The research request timed out.'
          : extractErrorMessage(error) || 'Research could not be retrieved.';
        this.error.set(`${message} Retry reuses this request; start a new search only when you want another provider request.`);
      },
    });
  }

  protected requestRepair(): void {
    const report = this.report();
    if (report?.status === 'completed' && this.repairAvailable && !this.repairBusy) this.repairRequested.emit(report);
  }

  protected download(): void {
    const report = this.report();
    if (!report) return;
    const url = URL.createObjectURL(new Blob([failureResearchBrief(report)], { type: 'text/plain;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `proofrun-failure-research-${report.research_id.replace(/[^a-zA-Z0-9_-]/g, '_')}.txt`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}
