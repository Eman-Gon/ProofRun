"""Loopback dashboard for prepared comparisons and public repository inspection."""

import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from dotenv import dotenv_values

from .public_repo import PublicRepoError, inspect_public_repo, list_public_repositories, normalize_owner, normalize_repository
from .result_explanation import explain_public_result
from .demo_ready import load_demo_ready


ROOT = Path(__file__).resolve().parents[1]
CASE_IDS = ("gpu-energy-pandas", "pydantic")
OUTPUT_LIMIT = 32_000
RUN_TIMEOUT = 180
LIVE_RUN_TIMEOUT = 420
INTERRUPT_GRACE = 20
KILL_GRACE = 5
STATIC = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"),
          "/styles.css": ("styles.css", "text/css"), "/app.js": ("app.js", "text/javascript")}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _repository_owner(root):
    repository = os.getenv("TARGET_REPO")
    if repository is None:
        try:
            path = root / ".env"
            if path.stat().st_size <= 64_000:
                repository = dotenv_values(path, interpolate=False).get("TARGET_REPO")
        except (OSError, UnicodeError):
            pass
    try:
        return normalize_repository(repository).split("/")[0]
    except PublicRepoError:
        # Match the account used by the existing prepared repository comparison.
        return "Eman-Gon"


def _text(value, limit=OUTPUT_LIMIT):
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value)[-limit:]


def _read(path):
    try:
        if path.stat().st_size > 2_000_000:
            return None
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, RecursionError):
        return None


def _file_text(path):
    try:
        return _text(path.read_text(), 24_000) if path.stat().st_size <= 24_000 else ""
    except (OSError, UnicodeError):
        return ""


def _memory_backend(value):
    backend = value.get("backend", "local")
    return backend if backend in ("local", "cloud", "none") else "unknown"


def _cloud_graph(value):
    dataset_id = value.get("dataset_id")
    graph = value.get("graph_summary")
    if (not isinstance(dataset_id, str)
            or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", dataset_id)
            or not isinstance(graph, dict) or graph.get("dataset_id") != dataset_id
            or any(type(graph.get(key)) is not int or graph[key] < 0 for key in ("num_nodes", "num_edges"))):
        return None
    return {"datasetId": dataset_id, "numNodes": graph["num_nodes"], "numEdges": graph["num_edges"]}


def _matching_cloud_retrieval(value):
    evidence, digest = value.get("evidence"), value.get("evidence_sha256")
    excerpt = value.get("retrieved_excerpt")
    if (not isinstance(evidence, dict) or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest) or not isinstance(excerpt, str)
            or not re.fullmatch(r"secondlook_compatibility_[0-9a-f]{32}", str(value.get("dataset", "")))):
        return False
    try:
        body = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return False
    frame = f"SECONDLOOK_VERIFIED_EVIDENCE_V1 {digest}\n{body}\nSECONDLOOK_VERIFIED_EVIDENCE_END {digest}"
    return hashlib.sha256(body.encode()).hexdigest() == digest and frame in excerpt


def _verified_cloud(value, graph):
    return bool(graph and graph["numNodes"] > 0 and graph["numEdges"] > 0 and _matching_cloud_retrieval(value))


def _integrations(report):
    report = report or {}
    source = report.get("source") if isinstance(report.get("source"), dict) else {}
    stored = report.get("memory_verification") if isinstance(report.get("memory_verification"), dict) else {}
    prior = report.get("prior_memory") if isinstance(report.get("prior_memory"), dict) else {}
    memory_backend = _memory_backend(stored)
    memory_label = {"cloud": "Cognee Cloud", "local": "Cognee local", "none": "Cognee", "unknown": "Cognee (unknown backend)"}[memory_backend]
    graph = _cloud_graph(stored) if memory_backend == "cloud" else None
    bright_status = "not_run"
    if source.get("provider") == "brightdata" and re.fullmatch(r"[0-9a-f]{64}", str(source.get("content_sha256", ""))):
        bright_status = "verified"
    elif source.get("provider") == "direct_https" or _text(source.get("provenance")).startswith("direct HTTPS"):
        bright_status = "fallback"
    memory_status = "not_run"
    if stored.get("status") == "stored_and_retrieved":
        matches = bool(stored.get("retrieved_excerpt") and re.fullmatch(r"secondlook_compatibility_[0-9a-f]{32}", str(stored.get("dataset", ""))))
        memory_status = "verified" if matches and (memory_backend == "local" or (memory_backend == "cloud" and _verified_cloud(stored, graph))) else "failed"
    elif stored.get("status") == "failed":
        memory_status = "failed"
    elif str(report.get("memory", "")).startswith("secondlook_compatibility_"):
        memory_status = "stored_only"
    matches = prior.get("matches") if isinstance(prior.get("matches"), list) else []
    recalled = next((item for item in matches if isinstance(item, dict) and item.get("retrieved_excerpt")), {})
    prior_status = "recalled" if prior.get("status") == "recalled" and recalled else "not_found" if prior.get("status") == "not_found" else "failed" if prior.get("status") == "failed" else "not_run"
    prior_backend = _memory_backend(recalled if "backend" in recalled else prior)
    prior_label = {"cloud": "Cognee Cloud", "local": "Cognee local", "none": "Cognee", "unknown": "Cognee (unknown backend)"}[prior_backend]
    prior_graph = _cloud_graph(recalled) if prior_backend == "cloud" else None
    if prior.get("status") == "recalled" and (prior_backend in ("unknown", "none") or (prior_backend == "cloud" and not _verified_cloud(recalled, prior_graph))):
        prior_status = "failed"
    return {
        "brightData": {"status": bright_status,
                       "detail": {"verified": "Bright Data fetched the source and its supporting quote was verified.", "fallback": "Source came from direct HTTPS; Bright Data was not successful in this run.", "not_run": "No verified Bright Data source fetch in this comparison."}[bright_status],
                       "sourceUrl": _text(source.get("source_url"), 500) or None, "checkedAt": _text(source.get("fetched_at"), 80) or None,
                       "quote": _text(source.get("evidence_quote"), 400), "contentSha256": _text(source.get("content_sha256"), 64)},
        "cognee": {"status": memory_status,
                   "backend": memory_backend,
                   "detail": {"verified": f"Finding stored and processed in {memory_label}, then retrieved by an actual search with matching evidence hash." + (" Its Cloud graph has verified nodes and edges." if graph else ""), "stored_only": f"Earlier run stored this finding in {memory_label}; retrieval was not verified.", "failed": f"{memory_label} storage, retrieval, or graph verification did not complete successfully.", "not_run": f"{memory_label} was not verified in this comparison."}[memory_status],
                   "dataset": _text(stored.get("dataset"), 100) or None,
                   "datasetId": (_text(stored.get("dataset_id"), 36) or None) if memory_backend == "cloud" else None,
                   "graphSummary": graph, "graphUrl": "https://platform.cognee.ai/knowledge-graph" if memory_backend == "cloud" else None,
                   "excerpt": _text(stored.get("retrieved_excerpt"), 6000), "checkedAt": _text(stored.get("retrieved_at"), 80) or None},
        "priorMemory": {"status": prior_status,
                        "backend": prior_backend,
                        "detail": {"recalled": f"A previous matching finding was retrieved from {prior_label} and supplied to the model before this comparison.", "not_found": f"No previous verified {prior_label} memory matches this app and version pair yet.", "failed": f"Prior {prior_label} lookup or evidence verification failed; recall is not verified.", "not_run": f"Prior {prior_label} memory was not queried."}[prior_status],
                        "dataset": _text(recalled.get("dataset"), 100) or None,
                        "datasetId": (_text(recalled.get("dataset_id"), 36) or None) if prior_backend == "cloud" else None,
                        "graphSummary": prior_graph, "graphUrl": "https://platform.cognee.ai/knowledge-graph" if prior_backend == "cloud" else None,
                        "excerpt": _text(recalled.get("retrieved_excerpt"), 6000)},
    }


def _cell(item, *, gpu=False):
    if not isinstance(item, dict):
        return None
    code = item.get("exit_code")
    status = item.get("status")
    if gpu:
        status = "pass" if type(code) is int and code == 0 else "fail" if type(code) is int and code == 1 else "error"
        if code is None and item.get("output") == "TIMEOUT":
            status = "timeout"
    output = item.get("output" if gpu else "output_tail")
    if (not isinstance(status, str) or status not in {"pass", "fail", "error", "timeout"} or not isinstance(output, str)
            or (status in {"pass", "fail"} and (type(code) is not int or code != (0 if status == "pass" else 1)))):
        status = "error"
    duration = item.get("duration_seconds")
    # Compare before conversion: math.isfinite converts integers to floats and
    # can overflow on a malformed report. This also excludes bool, NaN and infinity.
    duration = duration if type(duration) in {int, float} and 0 <= duration <= RUN_TIMEOUT else None
    return {"status": status, "output": _text(output), "duration": duration}


class Dashboard:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.jobs = []
        self.stopping = threading.Event()
        self.worker = None
        self.repository_scans = []
        self.repository_worker = None
        self.repository_owner = _repository_owner(self.root)

    def report_path(self, case_id):
        if case_id == "gpu-energy-pandas":
            return self.root / ".commit-watch/repo-audit/gpu-energy-pandas/results.json"
        reports = sorted((self.root / ".commit-watch/upgrade-demo").glob("*/report.json"))
        return reports[-1] if reports else None

    def _signature(self, case_id):
        path = self.report_path(case_id)
        try:
            return (str(path), path.stat().st_mtime_ns, path.stat().st_size) if path else None
        except OSError:
            return None

    def case(self, case_id):
        gpu = case_id == "gpu-energy-pandas"
        path = self.report_path(case_id)
        report = _read(path) if path else None
        exists = path is not None and path.exists()
        case = {
            "id": case_id, "repository": "Eman-Gon/gpu-energy-recommender" if gpu else "Customer import demo",
            "title": "Hourly data collection" if gpu else "An optional field becomes required",
            "kind": "repository" if gpu else "fixture", "package": "pandas" if gpu else "pydantic",
            "fromVersion": "2.3.3" if gpu else "1.10.18", "toVersion": "3.0.0" if gpu else "2.8.2",
            "status": "inconclusive" if exists else "not_run", "summary": "No readable comparison evidence yet." if exists else "This comparison has not run yet.",
            "sourceUrl": "https://pandas.pydata.org/docs/whatsnew/v3.0.0.html" if gpu else "https://raw.githubusercontent.com/pydantic/pydantic/v2.8.2/docs/migration.md",
            "repoUrl": "https://github.com/Eman-Gon/gpu-energy-recommender" if gpu else None,
            "commit": "7a802eace3a3775acc5ea4af6c266079e2599eb3" if gpu else None,
            "filePath": "eda/data_collection.py" if gpu else "demo/upgrade/app.py", "lineNumbers": [22, 63] if gpu else [10],
            "beforeCode": "date_range = pd.date_range(start=start_date, end=end_date, freq='H')" if gpu else "nickname: Optional[str]",
            "afterCode": "date_range = pd.date_range(start=start_date, end=end_date, freq='h')" if gpu else "nickname: Optional[str] = None",
            "explanation": "Pandas 3.0 removes the uppercase H frequency alias. Lowercase h preserves hourly frequency." if gpu else "In Pydantic V2, Optional permits null but does not make a field omittable. An explicit None default preserves omission behavior.",
            "scope": "Two actual repository functions, using one day of synthetic data. The repository has no manifest proving its deployed pandas version." if gpu else "One self-contained customer-import fixture and this version pair; not a result from a user repository.",
            "provenance": "No measured evidence loaded.", "checkedAt": None, "checks": [], "patch": "",
            "canRun": False, "unavailableReason": None,
            "liveSupported": not gpu, "integrations": _integrations(report) if not gpu else _integrations(None),
        }
        names = [("original", "Original code"), ("fixed", "With suggested fix")] if gpu else [
            ("existing", "Existing tests"), ("probe", "Targeted check"), ("fixed", "With suggested fix")]
        results = report.get("results") if report else None
        valid_versions = True
        if gpu:
            results = results if isinstance(results, list) else []
            for key, label in names:
                cells = []
                for version in (case["fromVersion"], case["toVersion"]):
                    matches = [item for item in results if isinstance(item, dict) and item.get("variant") == key and item.get("pandas") == version]
                    cells.append(_cell(matches[0], gpu=True) if len(matches) == 1 else None)
                case["checks"].append({"id": key, "label": label, "before": cells[0], "after": cells[1]})
            valid_versions = len(results) == 4 and bool(report) and report.get("commit") == case["commit"] and report.get("repository") == case["repository"] and report.get("source_path") == case["filePath"]
        else:
            results = results if isinstance(results, dict) else {}
            for key, label in names:
                cells = [_cell(results.get(key + suffix)) for suffix in ("_old", "_new")]
                for cell, version in zip(cells, (case["fromVersion"], case["toVersion"])):
                    if cell and re.findall(r"^SECONDLOOK_DEPENDENCY_VERSION=([^\s]+)$", cell["output"], re.M) != [version]:
                        valid_versions = False
                case["checks"].append({"id": key, "label": label, "before": cells[0], "after": cells[1]})
            upgrade = report.get("upgrade", {}) if report else {}
            valid_versions = (valid_versions and bool(report) and report.get("status") == "confirmed_break"
                              and set(results) == {key + suffix for key, _ in names for suffix in ("_old", "_new")}
                              and upgrade == {"ecosystem": "pypi", "package": "pydantic", "before": "==1.10.18", "version": "==2.8.2"})
        expected = [["pass", "fail"], ["pass", "pass"]] if gpu else [["pass", "pass"], ["pass", "fail"], ["pass", "pass"]]
        observed = [[row[side]["status"] if row[side] else None for side in ("before", "after")] for row in case["checks"]]
        failure = case["checks"][0 if gpu else 1]["after"]
        terms = ("ValueError", "Invalid frequency: H") if gpu else ("ValidationError", "nickname", "Field required")
        if valid_versions and observed == expected and failure and all(term in failure["output"] for term in terms):
            case["status"] = "confirmed_break"
            if gpu:
                case["title"] = "Hourly data generation breaks on pandas 3"
            case["summary"] = "Both repository functions fail after the pandas upgrade. The lowercase h fix passes on both versions." if gpu else "Existing tests pass on both versions. The missing-nickname check exposes a break, and the explicit default fixes it on both."
        elif report:
            case["summary"] = "The stored comparison does not establish the expected upgrade break and verified fix."
        if report:
            if gpu:
                case["checkedAt"] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                case["provenance"] = "Actual repository functions; stored Docker test results. Timestamp is the evidence file modification time."
            else:
                case["checkedAt"] = _text(report.get("checked_at"), 80) or None
                source = report.get("source") if isinstance(report.get("source"), dict) else {}
                if isinstance(source.get("source_url"), str):
                    case["sourceUrl"] = source["source_url"]
                case["provenance"] = "; ".join(filter(None, [_text(source.get("provenance"), 300), _text(report.get("probe_provenance"), 300)])) or "Stored fixture test results; source provenance unavailable."
            case["patch"] = _file_text(path.parent / "suggested-fix.patch")
        required = [".commit-watch/repo-audit/gpu-energy-pandas/" + name for name in ("reproduce.py", "data_collection.py", "fixed_data_collection.py", "test_collection.py")] if gpu else ["src/main.py", "demo/upgrade/app.py", "demo/upgrade/fixed_app.py", "demo/upgrade/requirements-old.txt", "demo/upgrade/requirements-new.txt"]
        if not all((self.root / name).is_file() for name in required):
            case["unavailableReason"] = "The local reproduction files are missing."
        elif not shutil.which("docker"):
            case["unavailableReason"] = "Docker is not installed or is not on PATH."
        else:
            case["canRun"] = True
        return case

    def state(self):
        cases = [self.case(case_id) for case_id in CASE_IDS]
        with self.lock:
            jobs = [dict(job) for job in self.jobs]
            scans = [dict(scan) for scan in self.repository_scans]
        for scan in scans:
            result = scan.get("result")
            if scan.get("status") == "completed" and isinstance(result, dict) and "explanation" not in result:
                scan["result"] = {**result, "explanation": explain_public_result(result)}
        for case in cases:
            latest = next((job for job in jobs if job["caseId"] == case["id"]), None)
            if latest and latest["status"] == "failed":
                if latest.get("mode") == "live" and latest.get("freshReport") and case["status"] == "confirmed_break":
                    case["summary"] += " The required live integrations did not all succeed; inspect their evidence below."
                    continue
                case.update(status="inconclusive", summary="The latest rerun failed. Earlier stored results do not establish the outcome of this run.", checkedAt=None)
                if latest.get("mode") == "live":
                    case["integrations"] = _integrations(None)
                    for key in ("brightData", "cognee"):
                        case["integrations"][key].update(status="failed", detail="The latest live investigation failed; no new verified integration evidence is available.", excerpt="")
                if case["id"] == "gpu-energy-pandas":
                    case["title"] = "Hourly data collection"
                for row in case["checks"]:
                    row.update(before=None, after=None)
        return {"csrfToken": self.token, "cases": cases, "repositoryOwner": self.repository_owner,
                "demoReady": load_demo_ready(self.root, cases),
                "activeRun": next((job for job in jobs if job["status"] == "running"), None),
                "history": [job for job in jobs if job["status"] != "running"],
                "repositoryScans": scans,
                "activeScan": next((scan for scan in scans if scan["status"] == "running"), None)}

    def repositories(self, owner=None):
        try:
            owner = normalize_owner(self.repository_owner if owner is None else owner)
        except PublicRepoError as exc:
            return 400, {"owner": None, "error": str(exc)}
        try:
            return 200, list_public_repositories(owner, cancelled=self.stopping)
        except Exception as exc:
            message = str(exc) if isinstance(exc, PublicRepoError) else "Could not load public repositories. Please retry."
            return 502, {"owner": owner, "error": _text(message, 1000)}

    def inspect_repository(self, value):
        try:
            repository = normalize_repository(value)
        except PublicRepoError as exc:
            return 400, {"error": str(exc)}
        with self.lock:
            if self.stopping.is_set() or any(scan["status"] == "running" for scan in self.repository_scans):
                return 409, {"error": "A repository check is already running or the server is stopping."}
            scan = {"id": uuid4().hex, "repository": repository, "status": "running",
                    "startedAt": _now(), "finishedAt": None, "error": None, "result": None, "output": ""}
            self.repository_scans.insert(0, scan)
            del self.repository_scans[10:]
            self.repository_worker = threading.Thread(target=self._inspect_repository, args=(scan,), daemon=True)
            self.repository_worker.start()
            return 202, {"scan": dict(scan)}

    def _inspect_repository(self, scan):
        def emit(message):
            with self.lock:
                scan["output"] = _text(scan["output"] + str(message) + "\n", 4000)

        try:
            result = inspect_public_repo(scan["repository"], emit=emit, cancelled=self.stopping)
            with self.lock:
                scan.update(status="completed", result=result, finishedAt=_now())
        except Exception as exc:
            # Provider errors are intentionally user-readable; unexpected exceptions
            # must not expose URLs, credentials, environment values, or tracebacks.
            message = str(exc) if isinstance(exc, PublicRepoError) else "Repository inspection could not complete. Please try again."
            with self.lock:
                scan.update(status="failed", error=_text(message, 1000), finishedAt=_now())

    def start(self, case_id, mode="offline"):
        if case_id not in CASE_IDS:
            return 400, {"error": "Unknown caseId."}
        if mode not in {"offline", "live"} or (mode == "live" and case_id != "pydantic"):
            return 400, {"error": "Live integration runs are available for the Pydantic case only."}
        with self.lock:
            if self.stopping.is_set() or any(job["status"] == "running" for job in self.jobs):
                return 409, {"error": "A comparison is already running or the server is stopping."}
            case = self.case(case_id)
            if not case["canRun"]:
                return 409, {"error": case["unavailableReason"]}
            job = {"id": uuid4().hex, "caseId": case_id, "status": "running", "startedAt": _now(),
                   "finishedAt": None, "output": "", "exitCode": None, "mode": mode}
            self.jobs.insert(0, job)
            del self.jobs[20:]
            before = self._signature(case_id)
            self.worker = threading.Thread(target=self._work, args=(job, before), daemon=True)
            self.worker.start()
            return 202, {"run": dict(job)}

    def _append(self, job, output):
        with self.lock:
            job["output"] = _text(job["output"] + output)

    def _work(self, job, before):
        gpu = job["caseId"] == "gpu-energy-pandas"
        command = [sys.executable, str(self.root / ".commit-watch/repo-audit/gpu-energy-pandas/reproduce.py")] if gpu else [sys.executable, "-m", "src.main", "upgrade-demo", "--offline", "--no-memory"]
        if job.get("mode") == "live":
            command = [sys.executable, "-m", "src.main", "upgrade-demo", "--require-integrations"]
        code = None
        succeeded = False
        try:
            code = self._execute(command, lambda output: self._append(job, output))
            fresh = self._signature(job["caseId"]) != before
            job["freshReport"] = fresh
            succeeded = code == (0 if gpu else 1) and fresh and self.case(job["caseId"])["status"] == "confirmed_break"
            if succeeded and job.get("mode") == "live":
                evidence = self.case(job["caseId"])["integrations"]
                succeeded = all(evidence[key]["status"] == "verified" for key in ("brightData", "cognee"))
            if not fresh:
                self._append(job, "\nNo new evidence report was produced.\n")
        except Exception as exc:
            self._append(job, "\nComparison failed: " + type(exc).__name__ + ".\n")
        finally:
            with self.lock:
                job.update(status="completed" if succeeded else "failed", exitCode=code, finishedAt=_now())

    def _execute(self, command, emit):
        # Do not inherit API keys, custom provider endpoints, or shell startup code.
        env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL") if key in os.environ}
        env.update(PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1", TELEMETRY_DISABLED="true", LOG_LEVEL="ERROR")
        with subprocess.Popen(command, cwd=self.root, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, start_new_session=True) as process:
            limit = LIVE_RUN_TIMEOUT if "--require-integrations" in command else RUN_TIMEOUT
            deadline = time.monotonic() + limit
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map() or process.poll() is None:
                    if self.stopping.is_set() or time.monotonic() >= deadline:
                        emit(f"\nRun cancelled or exceeded the {limit}-second limit.\n")
                        # SIGINT lets both reviewed Python runners execute container cleanup.
                        try:
                            os.killpg(process.pid, signal.SIGINT)
                            process.wait(timeout=INTERRUPT_GRACE)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=KILL_GRACE)
                        return None
                    for key, _ in selector.select(timeout=0.2):
                        chunk = key.fileobj.read1(4096)
                        if chunk:
                            emit(chunk.decode("utf-8", errors="replace"))
                        else:
                            selector.unregister(key.fileobj)
            return process.wait(timeout=5)

    def close(self):
        self.stopping.set()
        if self.worker:
            self.worker.join(timeout=INTERRUPT_GRACE + KILL_GRACE + 1)
        if self.repository_worker:
            self.repository_worker.join(timeout=20)


def make_server(root=ROOT, port=8765):
    dashboard = Dashboard(root)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, code, body, content_type="application/json", attachment=None):
            data = json.dumps(body).encode() if content_type == "application/json" else body
            self.send_response(code)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            if attachment:
                self.send_header("Content-Disposition", 'attachment; filename="' + attachment + '"')
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self, post=False):
            port = self.server.server_port
            host = self.headers.get("Host")
            origin = self.headers.get("Origin")
            if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                self._send(403, {"error": "Loopback Host required."})
                return False
            if (post or origin is not None) and origin != "http://" + host:
                self._send(403, {"error": "Same-origin requests required."})
                return False
            return True

        def do_GET(self):
            if not self._allowed():
                return
            url = urlsplit(self.path)
            if url.path == "/api/state":
                self._send(200, dashboard.state())
            elif url.path == "/api/repositories":
                query = parse_qs(url.query, keep_blank_values=True)
                if set(query) - {"owner"} or ("owner" in query and len(query["owner"]) != 1):
                    self._send(400, {"owner": None, "error": "Supply at most one GitHub username as owner."})
                    return
                code, body = dashboard.repositories(query.get("owner", [None])[0])
                self._send(code, body)
            elif url.path == "/api/report":
                query = parse_qs(url.query)
                if set(query) != {"caseId"} or len(query["caseId"]) != 1 or query["caseId"][0] not in CASE_IDS:
                    self._send(400, {"error": "A known caseId is required."})
                    return
                case_id = query["caseId"][0]
                case = next(case for case in dashboard.state()["cases"] if case["id"] == case_id)
                self._send(200, case, attachment=case_id + ".json")
            elif url.path in STATIC:
                filename, content_type = STATIC[url.path]
                asset = dashboard.root / "ui" / filename
                try:
                    if asset.resolve().parent != (dashboard.root / "ui").resolve() or asset.is_symlink():
                        raise OSError("Invalid asset")
                    self._send(200, asset.read_bytes(), content_type)
                except OSError:
                    self._send(404, {"error": "Asset not found."})
            else:
                self._send(404, {"error": "Not found."})

        def do_POST(self):
            if not self._allowed(post=True):
                return
            if self.path not in {"/api/runs", "/api/repositories"}:
                self._send(404, {"error": "Not found."})
                return
            token = self.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(token.encode("utf-8"), dashboard.token.encode("utf-8")):
                self._send(403, {"error": "Invalid CSRF token."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get_content_type() != "application/json" or not 0 < length <= 1024 or self.headers.get("Transfer-Encoding"):
                    raise ValueError
                self.connection.settimeout(5)
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError
                if self.path == "/api/repositories":
                    if set(payload) != {"repository"} or not isinstance(payload["repository"], str):
                        raise ValueError
                elif (set(payload) not in ({"caseId"}, {"caseId", "mode"})
                      or not isinstance(payload["caseId"], str) or not isinstance(payload.get("mode", "offline"), str)):
                    raise ValueError
            except (ValueError, OSError):
                field = "repository" if self.path == "/api/repositories" else "caseId and optional mode"
                self._send(400, {"error": "Expected a small JSON body with " + field + "."})
                return
            if self.path == "/api/repositories":
                code, body = dashboard.inspect_repository(payload["repository"])
            else:
                code, body = dashboard.start(payload["caseId"], payload.get("mode", "offline"))
            self._send(code, body)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.dashboard = dashboard
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    server = make_server(port=args.port)
    print(f"Hackday Idea dashboard: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.dashboard.close()
        server.server_close()


if __name__ == "__main__":
    main()
