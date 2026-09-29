'use strict';

const ProofRunGraph = (() => {
  const cache = new Map();
  const ns = 'http://www.w3.org/2000/svg';
  const ranks = { run: 0, revision: 0, contract: 0, case: 0, package: 0, environment: 0,
    finding: 1, repair: 2, verification: 3, test: 3, requirement: 3, evidence: 3, artifact: 3 };
  function element(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = text;
    return item;
  }
  function svgElement(tag, attrs, text) {
    const item = document.createElementNS(ns, tag);
    for (const [key, value] of Object.entries(attrs)) item.setAttribute(key, value);
    if (text !== undefined) item.textContent = text;
    return item;
  }
  function checked(data) {
    const text = (value, max) => typeof value === 'string' && value.length <= max;
    if (!data || data.schema_version !== 'proofrun.evidence-graph.v1' || data.provider !== 'neo4j'
        || data.kind !== 'dashboard' || !['ready', 'pending', 'unavailable'].includes(data.status)
        || !text(data.run_id, 256) || !Array.isArray(data.nodes) || data.nodes.length > 80
        || !Array.isArray(data.edges) || data.edges.length > 120 || !text(data.message, 500)) throw Error('Invalid graph');
    const ids = new Set();
    for (const item of data.nodes) {
      if (!text(item.id, 256) || ids.has(item.id) || !Object.hasOwn(ranks, item.kind) || !text(item.label, 120)
          || (item.status !== undefined && !text(item.status, 64)) || (item.detail !== undefined && !text(item.detail, 500))) throw Error('Invalid node');
      ids.add(item.id);
    }
    const edges = new Set();
    for (const edge of data.edges) {
      if (!text(edge.id, 256) || edges.has(edge.id) || !ids.has(edge.source) || !ids.has(edge.target) || !text(edge.label, 120)) throw Error('Invalid edge');
      edges.add(edge.id);
    }
    if (data.status === 'ready' ? !/^[a-f0-9]{64}$/.test(data.graph_id) || !ids.size : ids.size || edges.size) throw Error('Invalid graph state');
    return data;
  }
  function draw(host, data) {
    const columns = [[], [], [], []];
    for (const item of data.nodes) columns[ranks[item.kind]].push(item);
    columns.forEach(column => column.sort((a, b) => a.kind.localeCompare(b.kind) || a.label.localeCompare(b.label)));
    const positions = new Map();
    const height = 58 + Math.max(...columns.map(column => column.length)) * 86;
    const scroll = element('div', 'graph-scroll');
    scroll.tabIndex = 0;
    scroll.setAttribute('aria-label', 'Evidence graph; scroll horizontally for all four columns');
    const svg = svgElement('svg', { viewBox: `0 0 1000 ${height}`, role: 'group', 'aria-label': 'Source, finding, fix and evidence relationships' });
    svg.append(svgElement('title', {}, 'Select a node to inspect its evidence and relationships'));
    const defs = svgElement('defs', {});
    const markerId = 'graph-arrow-' + data.graph_id.slice(0, 16);
    const marker = svgElement('marker', { id: markerId, viewBox: '0 0 8 8', refX: 7, refY: 4, markerWidth: 6, markerHeight: 6, orient: 'auto-start-reverse' });
    marker.append(svgElement('path', { d: 'M0 0 L8 4 L0 8', class: 'graph-arrow' }));
    defs.append(marker); svg.append(defs);
    const details = element('div', 'graph-details', 'Select a node to inspect its status, fingerprints and relationships.');
    details.setAttribute('aria-live', 'polite');
    for (const [column, items] of columns.entries()) {
      svg.append(svgElement('text', { x: 18 + column * 250, y: 25, class: 'graph-column' }, ['SOURCE', 'FINDING', 'FIX', 'EVIDENCE'][column]));
      items.forEach((item, row) => positions.set(item.id, { x: 12 + column * 250, y: 42 + row * 86 }));
    }
    const lines = svgElement('g', { class: 'graph-lines' });
    for (const edge of data.edges) {
      const from = positions.get(edge.source), to = positions.get(edge.target);
      const sameColumn = from.x === to.x;
      const x1 = from.x + 222, y1 = from.y + 32, x2 = sameColumn ? to.x + 222 : to.x, y2 = to.y + 32;
      const bend = sameColumn ? x1 + 16 : (x1 + x2) / 2;
      const path = svgElement('path', { d: `M${x1},${y1} C${bend},${y1} ${bend},${y2} ${x2},${y2}`, 'marker-end': `url(#${markerId})` });
      path.append(svgElement('title', {}, edge.label.replaceAll('_', ' ')));
      lines.append(path);
    }
    svg.append(lines);
    const buttons = [];
    for (const item of data.nodes) {
      const { x, y } = positions.get(item.id);
      const label = item.label.length > 29 ? item.label.slice(0, 27) + '…' : item.label;
      const status = (item.status || item.kind).replaceAll('_', ' ');
      const good = ['pass', 'passed', 'verified', 'preserved'].includes(item.status);
      const bad = ['fail', 'failed', 'rejected', 'regression', 'confirmed_break'].includes(item.status);
      const button = svgElement('g', { class: `graph-node ${good ? 'good' : bad ? 'bad' : ''}`, tabindex: '0', role: 'button', 'aria-pressed': 'false', 'aria-label': `${item.label}, ${status}` });
      button.append(svgElement('title', {}, item.label), svgElement('rect', { x, y, width: 222, height: 64, rx: 8 }),
        svgElement('text', { x: x + 12, y: y + 25, class: 'graph-node-label' }, label),
        svgElement('text', { x: x + 12, y: y + 47, class: 'graph-node-status' }, status));
      const select = () => {
        buttons.forEach(other => other.setAttribute('aria-pressed', String(other === button)));
        details.replaceChildren(element('strong', '', item.label), element('p', '', `Status: ${status}`));
        if (item.detail) details.append(element('p', 'graph-fingerprint', item.detail));
        const list = element('ul');
        for (const edge of data.edges.filter(edge => edge.source === item.id || edge.target === item.id)) {
          const source = data.nodes.find(node => node.id === edge.source), target = data.nodes.find(node => node.id === edge.target);
          list.append(element('li', '', `${source.label} → ${edge.label.toLowerCase().replaceAll('_', ' ')} → ${target.label}`));
        }
        details.append(list);
      };
      button.addEventListener('click', select);
      button.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(); } });
      buttons.push(button); svg.append(button);
    }
    scroll.append(svg); host.append(scroll, details);
  }
  async function mount(host, url, signature, force = false) {
    const key = url + signature;
    if (!force && host.graphKey === key) return;
    host.graphKey = key;
    const generation = (host.graphGeneration || 0) + 1;
    host.graphGeneration = generation;
    host.replaceChildren();
    const heading = element('div', 'graph-heading');
    heading.append(element('h4', '', 'Fix evidence graph'), element('span', 'graph-provider', 'Neo4j'));
    const retry = element('button', 'text-button', 'Refresh graph');
    retry.type = 'button';
    retry.addEventListener('click', () => mount(host, url, signature, true));
    heading.append(retry);
    const message = element('p', 'graph-message', 'Loading saved evidence…');
    message.setAttribute('role', 'status');
    host.append(heading, message);
    retry.disabled = true;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
      let data = !force && cache.get(key);
      if (!data) {
        const response = await fetch(url, { signal: controller.signal, headers: { Accept: 'application/json' } });
        if (!response.ok) throw Error('Unavailable graph');
        data = checked(await response.json());
        if (data.status === 'ready') { cache.set(key, data); if (cache.size > 20) cache.delete(cache.keys().next().value); }
      }
      if (host.graphGeneration !== generation || !host.isConnected) return;
      message.textContent = data.message || (data.status === 'ready' ? 'Saved relationships for this fix. Select a node to inspect evidence.' : 'Graph evidence is not available yet.');
      if (data.status === 'ready') draw(host, data);
    } catch {
      if (host.graphGeneration === generation && host.isConnected) message.textContent = 'Neo4j graph unavailable. Saved test results remain available above. Retry to reconnect.';
    } finally {
      clearTimeout(timer);
      if (host.graphGeneration === generation) retry.disabled = false;
    }
  }
  return { mount };
})();
