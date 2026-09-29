import { CommonModule } from '@angular/common';
import { Component, DoCheck, Input, OnDestroy, computed, inject, signal } from '@angular/core';
import { Subscription } from 'rxjs';
import { EvidenceGraph, evidenceStatusTone, graphForFinding, layoutEvidenceGraph } from './evidence-graph';
import { ProofRunService } from './proofrun.service';
import { ReleaseInvestigationService } from './release-investigation.service';

let nextGraphId = 0;

@Component({
  selector: 'proofrun-evidence-graph',
  standalone: true,
  imports: [CommonModule],
  styleUrl: './evidence-graph.component.scss',
  template: `
    <section class="evidence-graph" [attr.aria-labelledby]="headingId" [attr.aria-busy]="loading()">
      <header class="graph-header">
        <div><p class="eyebrow">Connected evidence · Neo4j</p><h3 [id]="headingId">Fix evidence graph</h3></div>
        <button class="refresh" type="button" (click)="reload()" [disabled]="loading() || !runId">
          {{ loading() ? 'Loading…' : error() || graph()?.status === 'unavailable' ? 'Retry graph' : 'Refresh graph' }}
        </button>
      </header>
      <p class="graph-help">Trace the recorded source, finding, repair and checks. Select a node to inspect its evidence.</p>
      @if (loading()) {
        <p class="graph-state" role="status">Loading this run’s Neo4j evidence…</p>
      } @else if (error()) {
        <p class="graph-state unavailable" role="status"><strong>Graph unavailable.</strong> {{ error() }}</p>
      } @else if (!runId) {
        <p class="graph-state" role="status">The graph will appear when the worker records this run.</p>
      } @else if (graph(); as current) {
        @if (current.status !== 'ready') {
          <p class="graph-state" [class.unavailable]="current.status === 'unavailable'" role="status">
            <strong>{{ current.status === 'pending' ? 'Graph pending.' : 'Graph unavailable.' }}</strong>
            {{ current.message || 'No graph evidence is available for this run yet.' }}
          </p>
        } @else if (!diagram().nodes.length) {
          <p class="graph-state" role="status">No graph nodes have been recorded for {{ findingId ? 'this finding' : 'this run' }} yet.</p>
        } @else {
          <div class="graph-scroll" tabindex="0" role="group" aria-label="Evidence diagram. Scroll to explore; tab to select nodes.">
            <div class="graph-canvas" [style.width.px]="diagram().width" [style.height.px]="diagram().height">
              <svg aria-hidden="true" [attr.width]="diagram().width" [attr.height]="diagram().height">
                <defs><marker [id]="arrowId" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
                  <path d="M 0 0 L 10 5 L 0 10 z" fill="#7890a8"></path>
                </marker></defs>
                @for (edge of diagram().edges; track edge.id) {
                  <path [attr.d]="edge.path" [attr.marker-end]="'url(#' + arrowId + ')'" class="graph-edge"
                    [class.active-edge]="edge.source === selectedId() || edge.target === selectedId()"></path>
                }
              </svg>
              @for (column of diagram().columns; track column.label) {
                <span class="column-label" [style.left.px]="column.x">{{ column.label }}</span>
              }
              @for (node of diagram().nodes; track node.id) {
                <button type="button" class="graph-node" [class.selected]="selectedId() === node.id"
                  [attr.data-tone]="tone(node.kind, node.status)" [style.left.px]="node.x" [style.top.px]="node.y"
                  [style.width.px]="node.width" [style.height.px]="node.height" [attr.aria-pressed]="selectedId() === node.id"
                  [attr.aria-label]="label(node.kind) + ': ' + node.label + '. ' + label(node.status)"
                  [attr.title]="node.label" (click)="selectedId.set(node.id)">
                  <span class="node-kind">{{ label(node.kind) }}</span><strong>{{ node.label }}</strong>
                  <span class="node-status">{{ label(node.status) }}</span>
                </button>
              }
            </div>
          </div>
          <p class="scroll-help">Scroll to explore the diagram · Node status comes from recorded evidence.</p>
          @if (selectedNode(); as node) {
            <div class="node-detail" aria-live="polite">
              <div class="detail-heading"><strong>{{ node.label }}</strong><span [attr.data-tone]="tone(node.kind, node.status)">{{ label(node.status) }}</span></div>
              @if (node.detail) { <p>{{ node.detail }}</p> }
              <dl><div><dt>Type</dt><dd>{{ label(node.kind) }}</dd></div><div><dt>Node</dt><dd><code>{{ node.id }}</code></dd></div></dl>
              @if (connections().length) {
                <ul class="connections" aria-label="Recorded relationships">
                  @for (connection of connections(); track connection.id) { <li>{{ connection.text }}</li> }
                </ul>
              }
            </div>
          }
          <p class="graph-caption">Observed {{ current.observed_at | date:'medium' }} · Snapshot <code>{{ current.graph_id?.slice(0, 12) }}</code></p>
        }
      }
      <p class="graph-footnote">The graph shows relationships. Executed verification determines whether a fix passes.</p>
    </section>
  `,
})
export class EvidenceGraphComponent implements DoCheck, OnDestroy {
  @Input() kind: 'fixture' | 'release' = 'fixture';
  @Input() resourceId = '';
  @Input() runId = '';
  @Input() findingId = '';
  @Input() revision = '';
  private readonly fixture = inject(ProofRunService);
  private readonly release = inject(ReleaseInvestigationService);
  protected readonly headingId = `evidence-graph-${++nextGraphId}`;
  protected readonly arrowId = `${this.headingId}-arrow`;
  protected readonly graph = signal<EvidenceGraph | undefined>(undefined);
  protected readonly loading = signal(false);
  protected readonly error = signal('');
  protected readonly selectedId = signal('');
  private readonly scopedGraph = signal<EvidenceGraph | undefined>(undefined);
  protected readonly diagram = computed(() => layoutEvidenceGraph(this.scopedGraph() ?? { nodes: [], edges: [] } as unknown as EvidenceGraph));
  protected readonly selectedNode = computed(() => this.scopedGraph()?.nodes.find(node => node.id === this.selectedId()));
  protected readonly connections = computed(() => {
    const graph = this.scopedGraph(), selected = this.selectedId();
    if (!graph || !selected) return [];
    const names = new Map(graph.nodes.map(node => [node.id, node.label]));
    return graph.edges.filter(edge => edge.source === selected || edge.target === selected)
      .map(edge => ({ id: edge.id, text: `${names.get(edge.source)} → ${edge.label.replace(/_/g, ' ').toLowerCase()} → ${names.get(edge.target)}` }));
  });
  protected readonly tone = evidenceStatusTone;
  private identity = '';
  private request?: Subscription;

  ngDoCheck(): void {
    // Workspace can change without destroying the route component. Include its full
    // proxy scope in the identity, and drop the previous graph before requesting.
    let scope = '';
    try { scope = this.kind === 'fixture' ? this.fixture.graphScope(this.resourceId) : this.release.graphScope(this.runId); }
    catch { scope = 'no-workspace'; }
    const identity = JSON.stringify([scope, this.kind, this.resourceId, this.runId, this.findingId, this.revision]);
    if (identity !== this.identity) {
      this.identity = identity;
      this.load(false);
    }
  }

  ngOnDestroy(): void { this.request?.unsubscribe(); }
  protected reload(): void { this.load(true); }
  protected label(value?: string): string { return value ? value.replace(/_/g, ' ') : 'Unreported'; }

  private load(refresh: boolean): void {
    this.request?.unsubscribe();
    this.graph.set(undefined); this.scopedGraph.set(undefined); this.selectedId.set(''); this.error.set('');
    this.loading.set(false);
    if (!this.runId || (this.kind === 'fixture' && !this.resourceId)) return;
    const identity = this.identity;
    this.loading.set(true);
    try {
      const request = this.kind === 'fixture'
        ? this.fixture.graph(this.resourceId, this.runId, this.revision, refresh)
        : this.release.graph(this.runId, this.revision, refresh);
      this.request = request.subscribe({
        next: graph => {
          if (this.identity !== identity) return;
          const scoped = graphForFinding(graph, this.findingId);
          this.graph.set(graph); this.scopedGraph.set(scoped);
          this.selectedId.set((scoped.nodes.find(node => node.kind === 'finding') ?? scoped.nodes[0])?.id ?? '');
          this.loading.set(false);
        },
        error: () => {
          if (this.identity !== identity) return;
          this.loading.set(false);
          this.error.set('This run’s Neo4j evidence could not be loaded or validated. Retry to retrieve the recorded graph.');
        },
      });
    } catch {
      this.loading.set(false);
      this.error.set('Select a workspace with a configured graph service, then retry.');
    }
  }
}
