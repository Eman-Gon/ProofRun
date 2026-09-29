import assert from 'node:assert/strict';
import test from 'node:test';
import { defer, firstValueFrom, shareReplay } from 'rxjs';
import { EvidenceGraphCache, evidenceStatusTone, graphForFinding, layoutEvidenceGraph, readEvidenceGraph } from '../src/app/evidence-graph.ts';

const graph = () => ({
  schema_version: 'proofrun.evidence-graph.v1', provider: 'neo4j', status: 'ready', kind: 'release',
  run_id: 'release-123', graph_id: 'a'.repeat(64), observed_at: '2026-09-29T12:00:00Z',
  nodes: [
    { id: 'run', kind: 'run', label: 'This investigation', status: 'completed' },
    { id: 'revision', kind: 'revision', label: 'Candidate revision' },
    { id: 'req-a', kind: 'requirement', label: 'Requirement A' },
    { id: 'req-b', kind: 'requirement', label: 'Requirement B' },
    { id: 'finding-a', kind: 'finding', label: 'Finding A', status: 'confirmed', finding_id: 'a' },
    { id: 'repair-a', kind: 'repair', label: 'Repair A', status: 'verified', finding_id: 'a', detail: 'Recorded repair attempt.' },
    { id: 'test-a', kind: 'test', label: 'Test A', status: 'passed', finding_id: 'a' },
    { id: 'finding-b', kind: 'finding', label: 'Finding B', status: 'inconclusive', finding_id: 'b' },
    { id: 'repair-b', kind: 'repair', label: 'Repair B', status: 'rejected', finding_id: 'b' },
  ],
  edges: [
    { id: '1', source: 'run', target: 'revision', label: 'checks' },
    { id: '2', source: 'revision', target: 'req-a', label: 'requires' },
    { id: '3', source: 'revision', target: 'req-b', label: 'requires' },
    { id: '4', source: 'req-a', target: 'finding-a', label: 'observed' },
    { id: '5', source: 'req-b', target: 'finding-b', label: 'observed' },
    { id: '6', source: 'finding-a', target: 'repair-a', label: 'proposed' },
    { id: '7', source: 'repair-a', target: 'test-a', label: 'verified by' },
    { id: '8', source: 'finding-b', target: 'repair-b', label: 'proposed' },
  ],
});

test('graph validation binds the graph to the requested kind and run', () => {
  const value = graph();
  assert.equal(readEvidenceGraph(value, 'release', 'release-123'), value);
  for (const changes of [
    { run_id: 'other-run' }, { kind: 'fixture' }, { provider: 'unknown' },
    { schema_version: 'another-version' }, { graph_id: null }, { observed_at: 'not a date' },
  ]) assert.throws(() => readEvidenceGraph({ ...value, ...changes }, 'release', 'release-123'));
});

test('pending and unavailable never display invented nodes or a ready graph identity', () => {
  for (const status of ['pending', 'unavailable']) {
    const value = { ...graph(), status, graph_id: null, nodes: [], edges: [], message: 'No graph available.' };
    assert.equal(readEvidenceGraph(value, 'release', 'release-123').status, status);
    assert.throws(() => readEvidenceGraph({ ...value, nodes: graph().nodes }, 'release', 'release-123'));
    assert.throws(() => readEvidenceGraph({ ...value, graph_id: 'a'.repeat(64) }, 'release', 'release-123'));
  }
});

test('malformed, oversized, duplicate and dangling records are rejected before rendering', () => {
  const value = graph();
  const invalid = [
    { nodes: [...value.nodes, value.nodes[0]] },
    { edges: [...value.edges, value.edges[0]] },
    { edges: [{ ...value.edges[0], target: 'missing' }] },
    { nodes: [{ ...value.nodes[0], detail: { unsafe: 'unsupported nested object' } }], edges: [] },
    { nodes: [{ ...value.nodes[0], detail: 'x'.repeat(501) }], edges: [] },
    { nodes: [{ ...value.nodes[0], label: 'x'.repeat(161) }], edges: [] },
    { nodes: [{ ...value.nodes[0], label: 'bad\u0000label' }], edges: [] },
    { nodes: Array.from({ length: 81 }, (_, i) => ({ id: `${i}`, kind: 'run', label: 'run' })), edges: [] },
    { edges: Array.from({ length: 121 }, (_, i) => ({ ...value.edges[0], id: `${i}` })) },
  ];
  for (const changes of invalid) assert.throws(() => readEvidenceGraph({ ...value, ...changes }, 'release', 'release-123'));
});

test('each finding keeps its own repairs and ancestors without another finding or requirement', () => {
  const value = graph();
  const filtered = graphForFinding(value, 'a');
  assert.deepEqual(filtered.nodes.map(node => node.id), ['run', 'revision', 'req-a', 'finding-a', 'repair-a', 'test-a']);
  assert.deepEqual(filtered.edges.map(edge => edge.id), ['1', '2', '4', '6', '7']);
  assert.deepEqual(graphForFinding(value, 'unknown').nodes, []);
  assert.equal(graphForFinding(value), value);
  assert.equal(value.nodes.length, 9);
});

test('layout remains deterministic, bounded and non-overlapping while preserving all recorded edges', () => {
  const value = graphForFinding(graph(), 'a');
  const layout = layoutEvidenceGraph(value);
  assert.deepEqual(layoutEvidenceGraph(value), layout);
  assert.deepEqual(layout.columns.map(column => column.label), ['Source', 'Finding', 'Repair', 'Verification']);
  assert.equal(layout.edges.length, value.edges.length);
  for (const node of layout.nodes) {
    assert.ok(node.x >= 0 && node.y >= 0 && node.x + node.width <= layout.width && node.y + node.height <= layout.height);
    for (const other of layout.nodes) if (node.id !== other.id) {
      assert.ok(node.x + node.width <= other.x || other.x + other.width <= node.x
        || node.y + node.height <= other.y || other.y + other.height <= node.y);
    }
  }
  assert.ok(layout.edges.every(edge => /^M [\d .]+ C [\d .,]+$/.test(edge.path)));
});

test('confirmed findings do not acquire a passing color, and missing status stays unreported', () => {
  assert.equal(evidenceStatusTone('finding', 'confirmed'), 'pending');
  assert.equal(evidenceStatusTone('repair', 'rejected'), 'negative');
  assert.equal(evidenceStatusTone('verification', 'passed'), 'positive');
  assert.equal(evidenceStatusTone('repair', undefined), 'neutral');
});

test('one in-flight graph serves all finding panels; workspace, run, snapshot and retry are isolated', async () => {
  const cache = new EvidenceGraphCache();
  let requests = 0;
  const create = () => defer(async () => { requests++; return graph(); }).pipe(shareReplay({ bufferSize: 1, refCount: true }));
  const original = cache.get('workspace-one/run', 'run', 'snapshot', create);
  await Promise.all(Array.from({ length: 6 }, () => firstValueFrom(cache.get('workspace-one/run', 'run', 'snapshot', create))));
  assert.equal(requests, 1);
  assert.equal(cache.get('workspace-one/run', 'run', 'snapshot', create), original);
  for (const args of [
    ['workspace-two/run', 'run', 'snapshot'], ['workspace-one/run', 'new-run', 'snapshot'],
    ['workspace-one/run', 'run', 'new-snapshot'],
  ]) assert.notEqual(cache.get(...args, create), original);
  assert.notEqual(cache.get('workspace-one/run', 'run', 'snapshot', create, true), original);
});
