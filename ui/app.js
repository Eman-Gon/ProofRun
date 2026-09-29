'use strict';

const $ = (id) => document.getElementById(id);
let state = { cases: [], history: [], activeRun: null, repositoryScans: [], activeScan: null, demoReady: [] };
let selectedId = 'gpu-energy-pandas';
try { selectedId = localStorage.getItem('secondlook-selection') || selectedId; } catch { /* Storage is optional. */ }
let lastCaseSignature = '';
let lastScanSignature = '';
let lastProjectSignature = '';
let lastHistorySignature = '';
let lastRunSignature = '';
let lastDemoSignature = '';
let fetching = false;
let submitting = false;
let submittingRepository = false;
let submittingPr = false;
let repositorySource = 'mine';
let ownRepositories = [];
let repositoriesLoading = false;
let repositoriesRequested = false;
let repositoryOwner = '';
let repositoryListMessage = '';
let repositoryListError = '';
let pendingScan = null;
let pendingScanSequence = 0;
let refreshSequence = 0;
let connected = false;
let toastTimer;
const seenRuns = new Map();
const seenScans = new Map();
let initialized = false;

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function selectedCase() { return state.cases.find((item) => item.id === selectedId); }
function scanId(scan) { return `scan:${scan.id}`; }
function selectedScan() {
  return state.repositoryScans.find((scan) => scanId(scan) === selectedId)
    || state.demoReady.find((entry) => entry.scan && scanId(entry.scan) === selectedId)?.scan;
}
function isDemoRoute() { return location.hash === '#demo-ready' || location.hash.startsWith('#demo-ready/'); }
function demoEntry() {
  return location.hash.startsWith('#demo-ready/')
    ? state.demoReady.find((entry) => entry.id === location.hash.slice('#demo-ready/'.length)) : null;
}
function saveSelection() {
  try { localStorage.setItem('secondlook-selection', selectedId); } catch { /* Storage is optional. */ }
}
function caseRuns() { return state.history.filter((run) => run.caseId === selectedId).sort((a, b) => String(b.startedAt).localeCompare(String(a.startedAt))); }
function setLink(element, value) {
  let url;
  try {
    const parsed = new URL(value);
    if (parsed.protocol === 'https:' && !parsed.username && !parsed.password) url = parsed.href;
  } catch { /* Unavailable links stay hidden. */ }
  element.hidden = !url;
  if (url) element.href = url;
  else element.removeAttribute('href');
}
function dateLabel(value) {
  if (!value) return 'No saved comparison';
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return 'Saved comparison';
  return date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}
function toast(message, error = false) {
  clearTimeout(toastTimer);
  $('toast').textContent = message;
  $('toast').className = `toast${error ? ' error' : ''}`;
  $('toast').hidden = false;
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, error ? 6500 : 4000);
}
async function requestJson(path, options = {}, timeoutMs = 10000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, { ...options, signal: controller.signal });
    const result = await response.json();
    return { response, result };
  } catch (error) {
    if (error.name === 'AbortError') throw new Error(`The local dashboard did not respond within ${timeoutMs / 1000} seconds.`);
    if (error instanceof SyntaxError) throw new Error('The local dashboard returned an unreadable response.');
    throw error;
  } finally { clearTimeout(timer); }
}
function statusNode(result) {
  const status = result?.status || 'unknown';
  const names = { pass: 'Pass', fail: 'Fail', error: 'Error', timeout: 'Timed out', unknown: 'Not run' };
  const element = node('span', `test-status ${Object.hasOwn(names, status) ? status : 'unknown'}`);
  const symbol = node('span', 'symbol', status === 'pass' ? '✓' : status === 'unknown' ? '–' : '×');
  symbol.setAttribute('aria-hidden', 'true');
  element.append(symbol, node('span', '', names[status] || 'Unknown'));
  return element;
}
function hasVerifiedFix(item) {
  const check = (item.checks || []).find((candidate) => /fix/i.test(`${candidate.id} ${candidate.label}`));
  return check?.before?.status === 'pass' && check?.after?.status === 'pass';
}
function runName() { return 'Offline comparison'; }

function renderProjects() {
  const signature = JSON.stringify([state.cases.map(({ id, repository, kind }) => ({ id, repository, kind })),
    state.repositoryScans.map(({ id, repository, status, startedAt }) => ({ id, repository, status, startedAt })),
    state.demoReady.map(({ id, scan, available }) => ({ id, scanId: scan?.id, available }))]);
  if (signature !== lastProjectSignature) {
    lastProjectSignature = signature;
    $('project-select').replaceChildren();
    const prepared = node('optgroup');
    prepared.label = 'Prepared comparisons';
    for (const item of state.cases) {
      const option = node('option', '', item.repository);
      option.value = item.id;
      prepared.append(option);
    }
    if (prepared.children.length) $('project-select').append(prepared);
    const repositories = node('optgroup');
    repositories.label = 'Public repository scans';
    for (const scan of state.repositoryScans) {
      const suffix = scan.status === 'running' ? ' · checking…' : scan.status === 'failed' ? ' · failed' : ` · ${dateLabel(scan.startedAt)}`;
      const option = node('option', '', scan.repository + suffix);
      option.value = scanId(scan);
      repositories.append(option);
    }
    if (repositories.children.length) $('project-select').append(repositories);
    const demos = node('optgroup');
    demos.label = 'Saved examples · saved source scans';
    for (const entry of state.demoReady) {
      if (!entry.available || !entry.scan) continue;
      const option = node('option', '', entry.repository);
      option.value = scanId(entry.scan);
      demos.append(option);
    }
    if (demos.children.length) $('project-select').append(demos);
  }
  $('project-select').value = selectedId || '';
  $('project-select').disabled = !state.cases.length && !state.repositoryScans.length && !state.demoReady.some((entry) => entry.scan);
}

function renderDemoReady() {
  const signature = JSON.stringify([state.demoReady, state.cases.map(({ id, checkedAt, fromVersion, toVersion }) => ({ id, checkedAt, fromVersion, toVersion }))]);
  if (signature === lastDemoSignature) return;
  lastDemoSignature = signature;
  $('demo-nav-count').textContent = state.demoReady.length || '–';
  $('demo-ready-list').replaceChildren();
  if (!state.demoReady.length) {
    $('demo-ready-list').append(node('p', 'muted', 'The saved demo collection is unavailable. Reconnect to the local dashboard to try again.'));
    return;
  }
  state.demoReady.forEach((entry, index) => {
    const item = entry.caseId ? state.cases.find((candidate) => candidate.id === entry.caseId) : null;
    const result = entry.scan?.result;
    const article = node('article', `demo-card${index === 0 ? ' featured' : ''}`);
    const top = node('div', 'demo-card-top');
    top.append(node('span', 'demo-card-number', `${String(index + 1).padStart(2, '0')}${index === 0 ? ' / START HERE' : ''}`),
      node('span', `result-badge${item ? ' measured' : result?.findings?.length ? '' : ' neutral'}`, entry.evidenceLabel));
    article.append(top, node('h3', '', entry.title), node('p', 'demo-card-repository', entry.repository), node('p', 'demo-card-summary', entry.summary));
    const stats = node('div', 'demo-card-stats');
    if (item) {
      stats.append(node('span', '', `${item.package} ${item.fromVersion} → ${item.toVersion}`), node('span', '', 'Fix verified on both versions'));
    } else if (result) {
      stats.append(node('span', '', `${result.filesScanned} files`), node('span', '', `${result.dependencies.length} declarations`),
        node('span', '', `${result.findings.length} rule ${result.findings.length === 1 ? 'match' : 'matches'}`));
    }
    article.append(stats);
    const footer = node('div', 'demo-card-footer');
    footer.append(node('span', '', entry.available ? `Saved ${dateLabel(item?.checkedAt || result?.checkedAt)}` : 'Evidence unavailable'));
    if (entry.available) {
      const link = node('a', 'demo-open', 'Open demo →');
      link.href = '#demo-ready/' + entry.id;
      link.setAttribute('aria-label', `Open ${entry.title} demo`);
      footer.append(link);
    } else {
      article.append(node('p', 'demo-unavailable', entry.unavailableReason || 'Saved evidence is unavailable.'));
    }
    article.append(footer);
    $('demo-ready-list').append(article);
  });
}

function renderNavigation() {
  const demo = isDemoRoute();
  const entry = demoEntry();
  const detail = demo && entry?.available;
  $('demo-ready').hidden = !demo || Boolean(detail);
  $('workspace-content').hidden = demo && !detail;
  $('repository-form').hidden = demo;
  $('demo-selection').hidden = !detail;
  $('demo-selection-note').textContent = detail ? `${entry.evidenceLabel} · Saved evidence` : '';
  document.querySelector('.project-field').hidden = demo;
  document.querySelector('.project-controls').hidden = demo && !entry?.caseId;
  $('run-note').hidden = demo && !entry?.caseId;
  for (const [id, active] of [['nav-workspace', !demo], ['nav-demo-ready', demo]]) {
    if (active) $(id).setAttribute('aria-current', 'page');
    else $(id).removeAttribute('aria-current');
  }
  document.title = demo ? `${detail ? entry.title + ' · ' : ''}Saved examples — Hackday Idea` : 'Hackday Idea — dependency checks';
}

function selectResult(id) {
  selectedId = id;
  saveSelection();
  if (selectedScan()) {
    const repository = selectedScan().repository;
    $('repository-input').value = repository;
    if (repositorySource === 'mine' && ownRepositories.some((repo) => repo.fullName === repository)) {
      $('my-repository-select').value = repository;
      clearRepositoryError();
      updateRepositoryControls();
    } else setRepositorySource('public');
  }
  lastCaseSignature = '';
  lastScanSignature = '';
  $('evidence-list').replaceChildren();
  for (const id of ['test-details', 'source-details', 'history-details', 'repository-dependencies', 'repository-scope-details', 'repository-output-details']) $(id).open = false;
  renderProjects();
  renderCase();
  renderRepository();
  updateRunPanel();
  renderHistory();
  renderNavigation();
}

function navigate() {
  const entry = demoEntry();
  if (entry?.available) selectResult(entry.caseId || scanId(entry.scan));
  else renderNavigation();
  if (!isDemoRoute() && connected && !repositoriesRequested) loadOwnRepositories();
  if (initialized) $('main').focus({ preventScroll: true });
}

function sourceFileUrl(result, finding) {
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(result.repository || '') || !/^[a-f0-9]{40}$/i.test(result.commit || '')) return null;
  const parts = typeof finding.file === 'string' ? finding.file.split('/') : [];
  if (!parts.length || parts.some((part) => !part || part === '.' || part === '..')) return null;
  const line = Number.isInteger(finding.line) && finding.line > 0 ? `#L${finding.line}` : '';
  return `https://github.com/${result.repository}/blob/${result.commit}/${parts.map(encodeURIComponent).join('/')}${line}`;
}

function renderRepository() {
  const scan = selectedScan();
  $('repository-content').hidden = !scan;
  if (!scan) return;
  const signature = JSON.stringify(scan);
  if (signature === lastScanSignature) return;
  lastScanSignature = signature;
  const result = scan.status === 'completed' && scan.result ? scan.result : {};
  const findings = Array.isArray(result.findings) ? result.findings : [];
  const dependencies = Array.isArray(result.dependencies) ? result.dependencies : [];
  const running = scan.status === 'running';
  const failed = scan.status === 'failed';
  $('repository-title').textContent = scan.repository;
  $('repository-badge').textContent = running ? 'Checking repository…' : failed ? 'Scan failed'
    : findings.length ? 'Potential upgrade issue' : 'No supported patterns found';
  $('repository-badge').classList.toggle('neutral', running || failed || !findings.length);
  $('repository-summary').textContent = running ? 'Reading public source files and dependency manifests…'
    : failed ? scan.error || 'The repository scan could not finish. Check the repository address and try again.'
      : findings.length ? `${findings.length} potential upgrade ${findings.length === 1 ? 'issue needs' : 'issues need'} review. Check the affected code and upstream documentation below.`
        : 'No supported patterns found in the files checked. Other incompatibilities may still exist.';
  $('repository-saved-status').textContent = running ? 'Scan in progress' : failed ? 'No completed result for this scan'
    : `Checked ${dateLabel(result.checkedAt || scan.finishedAt)}`;
  $('repository-download-button').disabled = running || failed || !scan.result;
  const explanation = result.explanation;
  const hasExplanation = !running && !failed && explanation && typeof explanation.summary === 'string';
  $('repository-explanation').hidden = !hasExplanation;
  $('repository-boundary').hidden = Boolean(hasExplanation);
  for (const [id, field] of [['summary', 'summary'], ['next', 'nextStep'], ['checked', 'checked'], ['limits', 'limits']]) {
    $('repository-explanation-' + id).textContent = hasExplanation && typeof explanation[field] === 'string' ? explanation[field] : '';
  }
  $('repository-findings').replaceChildren();
  for (const [findingIndex, finding] of findings.entries()) {
    const article = node('article', 'repository-finding');
    article.append(node('span', 'result-badge', 'Potential upgrade issue'), node('h3', '', finding.title || finding.package),
      node('p', '', finding.explanation || 'Review this usage before changing dependency versions.'));
    const diff = node('div', 'diff-card');
    const heading = node('div', 'diff-header');
    const location = `${finding.file || 'Source file'}${Number.isInteger(finding.line) && finding.line > 0 ? `:${finding.line}` : ''}`;
    const locationUrl = sourceFileUrl(result, finding);
    const locationNode = node(locationUrl ? 'a' : 'span', '', location);
    if (locationUrl) {
      locationNode.target = '_blank';
      locationNode.rel = 'noopener noreferrer';
      setLink(locationNode, locationUrl);
    }
    heading.append(locationNode, node('span', '', finding.package || ''));
    diff.append(heading);
    for (const [kind, code, symbol] of [['removed', finding.beforeCode, '−'], ['added', finding.afterCode, '+']]) {
      if (typeof code !== 'string' || !code) continue;
      const line = node('div', `diff-line ${kind}`);
      const marker = node('span', '', symbol);
      marker.setAttribute('aria-label', kind === 'removed' ? 'Current code' : 'Suggested code');
      line.append(marker, node('code', '', code));
      diff.append(line);
    }
    article.append(diff, node('p', 'fix-note', 'Suggested change; not applied or tested.'));
    const source = node('a', 'finding-source', 'Upstream documentation ↗');
    source.target = '_blank';
    source.rel = 'noopener noreferrer';
    setLink(source, finding.sourceUrl);
    article.append(source);
    $('repository-findings').append(article);
    const graph = node('section', 'evidence-graph');
    graph.setAttribute('aria-label', 'Suggested fix evidence graph');
    article.append(graph);
    ProofRunGraph.mount(graph, `/api/graph?scanId=${encodeURIComponent(scan.id)}&findingIndex=${findingIndex}`, signature + findingIndex);
  }
  $('repository-dependencies').hidden = running || failed;
  $('repository-dependencies-label').textContent = `Dependencies (${dependencies.length})`;
  $('repository-dependency-list').replaceChildren();
  if (dependencies.length) {
    const table = node('table', 'dependency-table');
    const head = node('thead');
    const row = node('tr');
    for (const title of ['Dependency', 'Declared version', 'Ecosystem']) {
      const cell = node('th', '', title);
      cell.scope = 'col';
      row.append(cell);
    }
    head.append(row);
    const body = node('tbody');
    for (const dependency of dependencies) {
      const row = node('tr');
      const name = node('td', '', dependency.name);
      name.append(node('span', 'dependency-file', dependency.file));
      row.append(name, node('td', '', dependency.version || 'Not specified'), node('td', '', dependency.ecosystem));
      body.append(row);
    }
    table.append(head, body);
    $('repository-dependency-list').append(table);
  } else $('repository-dependency-list').append(node('p', 'muted', 'No dependencies were identified in the supported manifests checked.'));
  $('repository-scope').textContent = result.scope || 'A limited scan of public source files and dependency manifests for supported upgrade patterns.';
  const meta = [];
  if (Number.isInteger(result.filesScanned)) meta.push(`${result.filesScanned} files checked`);
  if (result.commit) meta.push(`Commit ${String(result.commit).slice(0, 12)}`);
  $('repository-meta').textContent = meta.join(' · ');
  $('repository-warnings').replaceChildren();
  for (const warning of Array.isArray(result.warnings) ? result.warnings : []) $('repository-warnings').append(node('li', '', warning));
  setLink($('repository-link'), result.repoUrl);
  $('repository-output-details').hidden = !scan.output;
  $('repository-output').textContent = scan.output || '';
}

function repositoryField() {
  return $(repositorySource === 'mine' ? 'my-repository-select' : 'repository-input');
}

function clearRepositoryError() {
  $('repository-error').hidden = true;
  $('repository-input').removeAttribute('aria-invalid');
  $('my-repository-select').removeAttribute('aria-invalid');
}

function setRepositorySource(source) {
  repositorySource = source;
  $('source-mine').checked = source === 'mine';
  $('source-public').checked = source === 'public';
  clearRepositoryError();
  updateRepositoryControls();
}

function showRepositoryAccountEditor(show) {
  $('repository-account-editor').hidden = !show;
  $('change-repository-account').setAttribute('aria-expanded', String(show));
  $('change-repository-account').textContent = show ? 'Done' : 'Change account';
}

async function loadOwnRepositories(owner) {
  if (repositoriesLoading) return;
  if (typeof owner === 'string' && !/^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$/.test(owner)) {
    repositoryListError = 'Enter a GitHub username to load its public repositories.';
    updateRepositoryControls();
    $('repository-owner').focus();
    return;
  }
  repositoriesRequested = true;
  repositoriesLoading = true;
  repositoryListError = '';
  repositoryListMessage = '';
  ownRepositories = [];
  $('my-repository-select').replaceChildren(node('option', '', 'Loading repositories…'));
  updateRepositoryControls();
  try {
    const path = '/api/repositories' + (owner ? `?owner=${encodeURIComponent(owner)}` : '');
    const { response, result } = await requestJson(path, { cache: 'no-store' }, 20000);
    if (typeof result.owner === 'string') {
      repositoryOwner = result.owner;
      $('repository-owner').value = result.owner;
    }
    if (!response.ok) throw new Error(result.error || 'Unable to load your repositories.');
    if (!Array.isArray(result.repositories)) throw new Error('The dashboard returned an unreadable repository list.');
    ownRepositories = result.repositories.filter((repo) => typeof repo?.fullName === 'string' && repo.fullName);
    const placeholder = node('option', '', ownRepositories.length ? 'Choose a repository…' : 'No public repositories found');
    placeholder.value = '';
    $('my-repository-select').replaceChildren(placeholder);
    for (const repo of ownRepositories) {
      const option = node('option', '', repo.fullName);
      option.value = repo.fullName;
      $('my-repository-select').append(option);
    }
    repositoryListMessage = result.warning || (result.truncated
      ? 'Showing a limited list. Use Public repository to enter a repository that is missing.'
      : ownRepositories.length ? '' : 'This account has no public repositories to list. Try another account or enter a public repository URL.');
  } catch (error) {
    repositoryListError = `${error.message} You can retry here or use Public repository.`;
    const placeholder = node('option', '', 'Repository list unavailable');
    placeholder.value = '';
    $('my-repository-select').replaceChildren(placeholder);
    showRepositoryAccountEditor(true);
  } finally {
    repositoriesLoading = false;
    updateRepositoryControls();
  }
}

function updateRepositoryControls() {
  const mine = repositorySource === 'mine';
  $('my-repositories-panel').hidden = !mine;
  $('public-repository-panel').hidden = mine;
  $('repository-account').hidden = !mine;
  $('repository-account-editor').hidden = !mine || $('change-repository-account').getAttribute('aria-expanded') !== 'true';
  $('repository-owner').disabled = !mine || repositoriesLoading;
  $('load-repositories-button').disabled = repositoriesLoading;
  $('load-repositories-button').textContent = repositoriesLoading ? 'Loading…' : 'Load repositories';
  $('repository-account-label').textContent = repositoriesLoading ? 'Loading your public repositories…'
    : repositoryOwner ? `Public repositories from ${repositoryOwner}` : 'Choose your GitHub account';
  $('my-repository-select').disabled = !mine || repositoriesLoading || !ownRepositories.length;
  $('my-repository-select').required = mine;
  $('repository-input').disabled = mine;
  $('repository-input').required = !mine;
  $('repository-list-error').hidden = !mine || !repositoryListError;
  $('repository-list-error').textContent = repositoryListError;
  $('repository-list-status').hidden = !mine || !repositoryListMessage;
  $('repository-list-status').textContent = repositoryListMessage;
  $('repository-note').textContent = mine
    ? 'Choose one of your public repositories to check for supported dependency upgrade patterns. Repository code is not executed.'
    : 'Enter any public GitHub repository to check for supported dependency upgrade patterns. Repository code is not executed.';
  const active = state.activeScan?.status === 'running' ? state.activeScan : null;
  $('repository-button').disabled = !connected || submittingRepository || Boolean(active)
    || (mine && (repositoriesLoading || !ownRepositories.length || !$('my-repository-select').value));
  $('repository-button').textContent = submittingRepository || active ? 'Checking…' : 'Check repository';
  $('repository-status').hidden = !active;
  $('repository-status').textContent = active ? `Checking ${active.repository}. You can view saved results while it runs.` : '';
}

function renderCase() {
  const item = selectedCase();
  $('loading').hidden = true;
  $('case-content').hidden = !item;
  if (!item) return;
  const signature = JSON.stringify(item);
  if (signature === lastCaseSignature) return;
  lastCaseSignature = signature;
  const confirmed = item.status === 'confirmed_break';
  $('case-kind').textContent = item.kind === 'fixture' ? 'Demo fixture' : 'Repository check';
  $('case-title').textContent = item.status === 'inconclusive' ? 'Comparison incomplete' : item.title;
  $('case-summary').textContent = item.summary;
  $('result-badge').textContent = confirmed ? 'Break confirmed' : item.status === 'inconclusive' ? 'Inconclusive' : 'Not run yet';
  $('result-badge').classList.toggle('neutral', !confirmed);
  $('saved-status').textContent = item.checkedAt ? `Last checked ${dateLabel(item.checkedAt)}` : 'No completed comparison yet';
  $('table-old').textContent = `${item.package} ${item.fromVersion}`;
  $('table-new').textContent = `${item.package} ${item.toVersion}`;
  $('comparison-body').replaceChildren();
  for (const check of item.checks || []) {
    const row = node('tr');
    const heading = node('th', '', check.label);
    heading.scope = 'row';
    row.append(heading);
    for (const side of ['before', 'after']) {
      const cell = node('td', check[side]?.status === 'fail' ? 'failed-cell' : '');
      cell.append(statusNode(check[side]));
      row.append(cell);
    }
    $('comparison-body').append(row);
  }
  if (!item.checks?.length) {
    const row = node('tr');
    const cell = node('td', '', 'Run a comparison to collect test results.');
    cell.colSpan = 3;
    row.append(cell);
    $('comparison-body').append(row);
  }
  $('before-code').textContent = item.beforeCode || 'No source excerpt available';
  $('after-code').textContent = item.afterCode || 'No proposed change available';
  $('fix-path').textContent = item.filePath || 'Source file';
  $('fix-lines').textContent = item.lineNumbers?.length ? `line${item.lineNumbers.length > 1 ? 's' : ''} ${item.lineNumbers.join(', ')}` : '';
  $('fix-description').textContent = item.explanation || '';
  $('fix-verification').textContent = hasVerifiedFix(item)
    ? '✓ Fix passes on both versions. Your repository is unchanged.'
    : 'Fix has not been verified in this comparison. Your repository is unchanged.';
  $('copy-patch-button').disabled = !item.patch;
  const publication = item.pullRequest || {};
  const published = publication.result;
  $('create-pr-button').hidden = !publication.eligible || Boolean(published);
  $('create-pr-button').disabled = submittingPr;
  $('create-pr-button').textContent = submittingPr ? 'Creating draft PR…' : 'Create draft PR';
  $('create-pr-button').title = publication.reason || '';
  $('pr-status').hidden = !published;
  if (published) {
    $('pr-link').textContent = `Draft PR #${published.number} created ↗`;
    setLink($('pr-link'), published.url);
  } else {
    $('pr-link').textContent = '';
    $('pr-link').removeAttribute('href');
  }
  $('scope-copy').textContent = item.scope;
  $('provenance-copy').textContent = item.provenance;
  $('repo-meta').textContent = item.commit ? `Checked commit ${item.commit.slice(0, 7)}` : 'Prepared demo application';
  setLink($('source-link'), item.sourceUrl);
  setLink($('repo-link'), item.repoUrl);
  renderEvidence(item);
  ProofRunGraph.mount($('fix-graph'), `/api/graph?caseId=${encodeURIComponent(item.id)}`, signature);
}

function renderEvidence(item) {
  const openChecks = new Set([...$('evidence-list').querySelectorAll('details[open]')].map((details) => details.dataset.checkId));
  $('evidence-list').replaceChildren();
  for (const check of item.checks || []) {
    const details = node('details', 'evidence-group');
    details.dataset.checkId = check.id;
    details.open = openChecks.has(check.id);
    const summary = node('summary');
    summary.append(node('span', '', check.label), statusNode(check.after));
    const columns = node('div', 'evidence-columns');
    for (const [side, version] of [['before', item.fromVersion], ['after', item.toVersion]]) {
      const section = node('section', 'evidence-output');
      const heading = node('h4', '', `${item.package} ${version}`);
      heading.append(statusNode(check[side]));
      section.append(heading, node('pre', '', check[side]?.output || 'No captured output.'));
      columns.append(section);
    }
    details.append(summary, columns);
    $('evidence-list').append(details);
  }
  if (!item.checks?.length) $('evidence-list').append(node('p', 'muted', 'No test output yet.'));
}

function renderHistory() {
  const runs = caseRuns();
  const signature = JSON.stringify([selectedId, runs]);
  if (signature === lastHistorySignature) return;
  lastHistorySignature = signature;
  $('run-count').textContent = runs.length ? `(${runs.length})` : '';
  const openRuns = new Set([...$('history-list').querySelectorAll('details[open]')].map((details) => details.dataset.runId));
  $('history-list').replaceChildren();
  if (!runs.length) {
    $('history-list').append(node('p', 'muted', 'No runs for this project in this dashboard session.'));
    return;
  }
  for (const run of runs) {
    const details = node('details', 'history-item');
    details.dataset.runId = run.id;
    details.open = openRuns.has(run.id);
    const summary = node('summary');
    summary.append(node('span', '', `${runName(run)} · ${dateLabel(run.startedAt)}`));
    summary.append(node('span', `test-status ${run.status === 'failed' ? 'fail' : ''}`, run.status === 'completed' ? 'Completed' : run.status === 'failed' ? 'Run failed' : 'Running'));
    details.append(summary, node('pre', '', run.output || 'Waiting for output…'));
    $('history-list').append(details);
  }
}

function updateRunPanel() {
  const item = selectedCase();
  $('run-button').hidden = Boolean(selectedScan());
  if (!item) {
    $('run-button').disabled = true;
    $('run-panel').hidden = true;
    $('run-note').textContent = selectedScan()?.status === 'running'
      ? 'Source scan in progress. Findings will appear below when it finishes.'
      : selectedScan() ? 'Viewing a saved source scan. Choose a repository above to check it again.' : '';
    return;
  }
  const active = state.activeRun?.status === 'running' ? state.activeRun : null;
  const caseRun = active?.caseId === item.id ? active : null;
  const unavailable = !connected || submitting || Boolean(active) || !item.canRun;
  $('run-button').disabled = unavailable;
  $('run-button').textContent = (submitting || caseRun) ? 'Comparing…' : 'Run comparison';
  $('run-note').textContent = !item.canRun ? (item.unavailableReason || 'This comparison is unavailable.')
    : caseRun || submitting ? 'Running in Docker. Saved results below will update when finished.'
      : active ? `${runName(active)} is running for another project.`
        : 'Viewing a prepared comparison. Run it again to refresh the measured test results in Docker.';
  const lastRun = caseRuns()[0];
  const shown = caseRun || (lastRun?.status === 'failed' ? lastRun : null);
  $('run-panel').hidden = !shown;
  if (!shown) return;
  const running = shown.status === 'running';
  $('run-panel').classList.toggle('finished', !running);
  const label = running ? 'Comparing both versions…'
    : `${runName(shown)} failed. Open the output for details.`;
  if ($('run-label').textContent !== label) $('run-label').textContent = label;
  const elapsed = Math.max(0, Math.floor((Date.now() - new Date(shown.startedAt).getTime()) / 1000));
  $('run-time').textContent = running && Number.isFinite(elapsed) ? `${elapsed}s` : '';
  const output = shown.output || 'Starting the comparison…';
  if ($('run-output').textContent !== output) $('run-output').textContent = output;
  const signature = `${shown.id}:${shown.status}`;
  if (signature !== lastRunSignature) {
    lastRunSignature = signature;
    $('run-output-disclosure').open = !running;
  }
}

async function refresh() {
  if (fetching) return;
  fetching = true;
  const requestSequence = ++refreshSequence;
  try {
    const { response, result: next } = await requestJson('/api/state', { cache: 'no-store' });
    if (!response.ok) throw new Error(`The local dashboard returned ${response.status}.`);
    if (!Array.isArray(next.cases) || !Array.isArray(next.history)) throw new Error('Unreadable dashboard response.');
    next.repositoryScans = Array.isArray(next.repositoryScans) ? next.repositoryScans : [];
    next.demoReady = Array.isArray(next.demoReady) ? next.demoReady : [];
    if (pendingScan) {
      if (next.repositoryScans.some((scan) => scan.id === pendingScan.id)) pendingScan = null;
      else if (requestSequence <= pendingScanSequence) {
        next.repositoryScans.unshift(pendingScan);
        next.activeScan = pendingScan;
      } else pendingScan = null;
    }
    state = next;
    connected = true;
    if (!repositoriesRequested) {
      repositoryOwner = next.repositoryOwner || '';
      $('repository-owner').value = repositoryOwner;
      if (!isDemoRoute()) loadOwnRepositories();
    }
    const demo = demoEntry();
    if (demo?.available) {
      const id = demo.caseId || scanId(demo.scan);
      if (selectedId !== id) selectResult(id);
    }
    if (!selectedCase() && !selectedScan()) selectedId = state.cases[0]?.id || (state.repositoryScans[0] ? scanId(state.repositoryScans[0]) : '');
    for (const run of state.history) {
      if (initialized && seenRuns.get(run.id) === 'running' && run.status !== 'running') {
        toast(run.status === 'completed'
          ? 'Comparison complete. Results updated.'
          : `${runName(run)} failed. Open the output for details.`, run.status === 'failed');
      }
      seenRuns.set(run.id, run.status);
    }
    for (const scan of state.repositoryScans) {
      if (initialized && seenScans.get(scan.id) === 'running' && scan.status !== 'running') {
        toast(scan.status === 'completed' ? `Source scan finished for ${scan.repository}. Review the findings and scope.`
          : `Scan failed for ${scan.repository}. Select its result for details.`, scan.status === 'failed');
      }
      seenScans.set(scan.id, scan.status);
    }
    initialized = true;
    $('connection-error').hidden = true;
    renderProjects();
    renderDemoReady();
    renderCase();
    renderRepository();
    updateRunPanel();
    updateRepositoryControls();
    renderHistory();
    renderNavigation();
  } catch (error) {
    connected = false;
    $('connection-error').textContent = `Unable to reach the local dashboard. ${error.message} Reconnecting…`;
    $('connection-error').hidden = false;
    $('loading').hidden = true;
    $('run-button').disabled = true;
    $('repository-button').disabled = true;
  } finally { fetching = false; }
}

async function startRepositoryScan(event) {
  event.preventDefault();
  if (!connected || submittingRepository || state.activeScan?.status === 'running') return;
  const field = repositoryField();
  if (field.disabled) return;
  const repository = field.value.trim();
  if (!repository) {
    field.focus();
    return;
  }
  submittingRepository = true;
  clearRepositoryError();
  updateRepositoryControls();
  try {
    const { response, result } = await requestJson('/api/repositories', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': state.csrfToken },
      body: JSON.stringify({ repository }),
    });
    if (!response.ok) throw new Error(result.error || 'Unable to start the repository scan.');
    if (!result.scan || typeof result.scan.id !== 'string') throw new Error('The dashboard did not return a scan. Refresh before trying again.');
    pendingScan = result.scan;
    pendingScanSequence = refreshSequence;
    state.repositoryScans = [result.scan, ...state.repositoryScans.filter((scan) => scan.id !== result.scan.id)];
    state.activeScan = result.scan;
    selectedId = scanId(result.scan);
    saveSelection();
    seenScans.set(result.scan.id, 'running');
    lastScanSignature = '';
    for (const id of ['repository-dependencies', 'repository-scope-details', 'repository-output-details']) $(id).open = false;
    renderProjects();
    renderCase();
    renderRepository();
    updateRunPanel();
    updateRepositoryControls();
    await refresh();
  } catch (error) {
    $('repository-error').textContent = error.message;
    $('repository-error').hidden = false;
    field.setAttribute('aria-invalid', 'true');
  } finally {
    submittingRepository = false;
    updateRepositoryControls();
  }
}

async function startRun() {
  const item = selectedCase();
  if (!item || !item.canRun || !connected || submitting || state.activeRun?.status === 'running') return;
  submitting = true;
  updateRunPanel();
  try {
    const { response, result } = await requestJson('/api/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': state.csrfToken },
      body: JSON.stringify({ caseId: item.id, mode: 'offline' }),
    });
    if (!response.ok) throw new Error(result.error || 'Unable to start the comparison.');
    if (result.run) {
      state.activeRun = result.run;
      seenRuns.set(result.run.id, 'running');
      updateRunPanel();
    }
    await refresh();
  } catch (error) { toast(error.message, true); }
  finally { submitting = false; updateRunPanel(); }
}

async function createDraftPullRequest() {
  const item = selectedCase();
  if (!item?.pullRequest?.eligible || submittingPr) return;
  const confirmed = window.confirm(
    'Create a new GitHub branch and draft pull request containing this verified fix?'
  );
  if (!confirmed) return;
  submittingPr = true;
  lastCaseSignature = '';
  renderCase();
  try {
    const { response, result } = await requestJson('/api/pull-requests', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': state.csrfToken },
      body: JSON.stringify({ caseId: item.id }),
    }, 45000);
    if (!response.ok) throw new Error(result.error || 'Unable to create the draft PR.');
    item.pullRequest.result = result.pullRequest;
    lastCaseSignature = '';
    renderCase();
    toast(`Draft PR #${result.pullRequest.number} created.`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    submittingPr = false;
    lastCaseSignature = '';
    renderCase();
  }
}

function downloadReport() {
  const item = selectedCase();
  const scan = selectedScan();
  if (!item && (!scan?.result || scan.status !== 'completed')) return;
  const report = item ? { exportedAt: new Date().toISOString(), investigation: item, runs: caseRuns() }
    : { exportedAt: new Date().toISOString(), type: 'static_source_scan', scan };
  const blob = new Blob([JSON.stringify(report, null, 2) + '\n'], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const anchor = node('a');
  anchor.href = url;
  anchor.download = `hackday-idea-${item ? item.id : 'repository-scan'}-report.json`;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast('Report exported.');
}

$('project-select').addEventListener('change', () => selectResult($('project-select').value));
window.addEventListener('hashchange', navigate);
document.querySelector('.skip-link').addEventListener('click', (event) => {
  event.preventDefault();
  $('main').focus();
  $('main').scrollIntoView();
});
$('repository-form').addEventListener('submit', startRepositoryScan);
$('source-mine').addEventListener('change', () => setRepositorySource('mine'));
$('source-public').addEventListener('change', () => setRepositorySource('public'));
$('change-repository-account').addEventListener('click', () => {
  const show = $('change-repository-account').getAttribute('aria-expanded') !== 'true';
  showRepositoryAccountEditor(show);
  if (show) $('repository-owner').focus();
});
$('load-repositories-button').addEventListener('click', () => loadOwnRepositories($('repository-owner').value.trim()));
$('repository-owner').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    event.preventDefault();
    loadOwnRepositories($('repository-owner').value.trim());
  }
});
$('my-repository-select').addEventListener('change', () => {
  clearRepositoryError();
  updateRepositoryControls();
});
$('repository-input').addEventListener('input', clearRepositoryError);
$('repository-download-button').addEventListener('click', downloadReport);
$('run-button').addEventListener('click', () => startRun());
$('download-button').addEventListener('click', downloadReport);
$('copy-patch-button').addEventListener('click', async () => {
  const patch = selectedCase()?.patch;
  if (!patch) return;
  try {
    await navigator.clipboard.writeText(patch);
    toast('Patch copied.');
  } catch { toast('Clipboard unavailable. Export the report to save the patch.', true); }
});
$('create-pr-button').addEventListener('click', createDraftPullRequest);
renderNavigation();
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 1200);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
