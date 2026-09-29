export type EvidenceGraphKind = 'fixture' | 'release' | 'dashboard';
export interface EvidenceNode {
  id: string;
  kind: string;
  label: string;
  status?: string;
  detail?: string;
  finding_id?: string;
}
export interface EvidenceEdge { id: string; source: string; target: string; label: string }
export interface EvidenceGraph {
  schema_version: 'proofrun.evidence-graph.v1';
  provider: 'neo4j';
  status: 'ready' | 'pending' | 'unavailable';
  kind: EvidenceGraphKind;
  run_id: string;
  graph_id: string | null;
  observed_at: string;
  nodes: EvidenceNode[];
  edges: EvidenceEdge[];
  message?: string;
}

const text = (value: unknown, max: number, empty = false): value is string =>
  typeof value === 'string' && (empty || value.length > 0) && value.length <= max && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value);

/** Validate the entire response before rendering any node or trusting its scope. */
export function readEvidenceGraph(value: unknown, kind: EvidenceGraphKind, runId: string): EvidenceGraph {
  const graph = value as EvidenceGraph;
  const invalid = () => new Error('The graph response did not match this run or its supported evidence format.');
  if (!graph || graph.schema_version !== 'proofrun.evidence-graph.v1' || graph.provider !== 'neo4j'
      || graph.kind !== kind || graph.run_id !== runId || !text(graph.run_id, 256)
      || !['ready', 'pending', 'unavailable'].includes(graph.status)
      || (graph.status === 'ready' ? typeof graph.graph_id !== 'string' || !/^[a-f0-9]{64}$/.test(graph.graph_id) : graph.graph_id !== null)
      || !text(graph.observed_at, 64) || !Number.isFinite(Date.parse(graph.observed_at))
      || (graph.message !== undefined && !text(graph.message, 500, true))
      || !Array.isArray(graph.nodes) || graph.nodes.length > 80
      || !Array.isArray(graph.edges) || graph.edges.length > 120) throw invalid();
  const ids = new Set<string>();
  for (const node of graph.nodes) {
    if (!node || !text(node.id, 256) || ids.has(node.id) || !text(node.kind, 64)
        || !text(node.label, 160) || (node.status !== undefined && !text(node.status, 64, true))
        || (node.detail !== undefined && !text(node.detail, 500, true))
        || (node.finding_id !== undefined && !text(node.finding_id, 256))) throw invalid();
    ids.add(node.id);
  }
  const edgeIds = new Set<string>();
  for (const edge of graph.edges) {
    if (!edge || !text(edge.id, 256) || edgeIds.has(edge.id) || !ids.has(edge.source)
        || !ids.has(edge.target) || !text(edge.label, 160)) throw invalid();
    edgeIds.add(edge.id);
  }
  if (graph.status !== 'ready' && (graph.nodes.length || graph.edges.length)) throw invalid();
  return graph;
}

/** A finding never inherits another finding's repair or verification branch. */
export function graphForFinding(graph: EvidenceGraph, findingId?: string): EvidenceGraph {
  if (!findingId) return graph;
  const branch = graph.nodes.filter(node => node.finding_id === findingId);
  if (!branch.length) return { ...graph, nodes: [], edges: [] };
  const sharedKinds = new Set(['run', 'revision', 'contract', 'package', 'version', 'requirement', 'environment']);
  const allowedAncestors = new Set(graph.nodes.filter(node => !node.finding_id && sharedKinds.has(node.kind)).map(node => node.id));
  const sharedSourceKinds = new Set(['run', 'revision', 'contract', 'package', 'version', 'environment']);
  const ids = new Set(graph.nodes.filter(node => node.finding_id === findingId
    || (!node.finding_id && sharedSourceKinds.has(node.kind))).map(node => node.id));
  let changed = true;
  while (changed) {
    changed = false;
    for (const edge of graph.edges) {
      if (ids.has(edge.target) && allowedAncestors.has(edge.source) && !ids.has(edge.source)) {
        ids.add(edge.source); changed = true;
      }
    }
  }
  const nodes = graph.nodes.filter(node => ids.has(node.id));
  return { ...graph, nodes, edges: graph.edges.filter(edge => ids.has(edge.source) && ids.has(edge.target)) };
}

export interface PositionedNode extends EvidenceNode { x: number; y: number; width: number; height: number }
export interface GraphLayout {
  nodes: PositionedNode[];
  edges: (EvidenceEdge & { path: string })[];
  columns: { x: number; label: string }[];
  width: number;
  height: number;
}

const stages = ['Source', 'Finding', 'Repair', 'Verification', 'Evidence'];
const rank = (kind: string): number => ({ run: 0, revision: 0, contract: 0, package: 0, version: 0, requirement: 0, environment: 0,
  finding: 1, repair: 2, verification: 3, case: 3, test: 3, artifact: 4, evidence: 4 } as Record<string, number>)[kind] ?? 4;

/** Deterministic bounded stage layout; paths always connect recorded endpoints. */
export function layoutEvidenceGraph(graph: EvidenceGraph): GraphLayout {
  const width = 150, height = 88, gap = 38, rowGap = 22, inset = 18, top = 36;
  const ranks = [...new Set(graph.nodes.map(node => rank(node.kind)))].sort((a, b) => a - b);
  const rows = new Map<number, number>();
  const nodes = [...graph.nodes].sort((a, b) => rank(a.kind) - rank(b.kind)).map(node => {
    const stage = rank(node.kind), row = rows.get(stage) ?? 0;
    rows.set(stage, row + 1);
    return { ...node, x: inset + ranks.indexOf(stage) * (width + gap), y: top + row * (height + rowGap), width, height };
  });
  const byId = new Map(nodes.map(node => [node.id, node]));
  const edges = graph.edges.flatMap(edge => {
    const source = byId.get(edge.source), target = byId.get(edge.target);
    if (!source || !target) return [];
    const sx = source.x + width, sy = source.y + height / 2, ty = target.y + height / 2;
    const path = target.x > source.x
      ? `M ${sx} ${sy} C ${sx + gap / 2} ${sy}, ${target.x - gap / 2} ${ty}, ${target.x - 4} ${ty}`
      : `M ${sx} ${sy} C ${sx + gap / 2} ${sy}, ${target.x + width + gap / 2} ${ty}, ${target.x + width + 4} ${ty}`;
    return [{ ...edge, path }];
  });
  return { nodes, edges, columns: ranks.map((stage, index) => ({ x: inset + index * (width + gap), label: stages[stage] })),
    width: Math.max(1, ranks.length) * (width + gap) - gap + inset * 2,
    height: top + Math.max(1, ...rows.values()) * (height + rowGap) - rowGap + inset };
}

export function evidenceStatusTone(kind: string, status?: string): 'positive' | 'negative' | 'pending' | 'neutral' {
  if (['failed', 'fail', 'error', 'rejected', 'regression_reproduced', 'confirmed_break', 'regression', 'setup_failed', 'timed_out', 'timeout'].includes(status ?? '')) return 'negative';
  if (['queued', 'running', 'pending', 'inconclusive', 'unavailable', 'not_run', 'not_attempted', 'verification_stale', 'unverified', 'interrupted'].includes(status ?? '')
      || (kind === 'finding' && status === 'confirmed')) return 'pending';
  if (['passed', 'pass', 'verified', 'verified_candidate', 'accepted', 'completed'].includes(status ?? '')) return 'positive';
  return 'neutral';
}

/** Shared in-flight and completed values, scoped to workspace, run and recorded snapshot. */
export class EvidenceGraphCache<T> {
  private entries = new Map<string, T>();
  get(scope: string, runId: string, revision: string, create: () => T, refresh = false): T {
    const key = JSON.stringify([scope, runId, revision]);
    if (refresh) this.entries.delete(key);
    const existing = this.entries.get(key);
    if (existing !== undefined) return existing;
    const value = create();
    this.entries.set(key, value);
    while (this.entries.size > 24) this.entries.delete(this.entries.keys().next().value!);
    return value;
  }
}
