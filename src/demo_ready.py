"""Offline, curated repository examples, independent of rotating scan history."""

from datetime import datetime
import json
from pathlib import Path
import re
import stat

from .result_explanation import explain_public_result

MAX_EVIDENCE_BYTES = 1024 * 1024
_ENTRIES = (
    ("gpu-energy-pandas", "Eman-Gon/gpu-energy-recommender", "gpu-energy-recommender.json",
     "Hourly data collection with a pandas migration pattern to review."),
    ("scam-killer", "Eman-Gon/scam_killer", "scam-killer.json",
     "A saved source scan for reviewing dependency declarations and migration findings."),
    ("gauntlet", "Eman-Gon/Gauntlet", "gauntlet.json",
     "A larger application showing dependency declarations and the limits of static checks."),
    ("agent-with-a-brain", "sandhya-subramani/Agent-with-a-Brain", "agent-with-a-brain.json",
     "An agent application showing dependencies that need checks beyond the supported migration rules."),
    ("flask", "pallets/flask", "flask.json",
     "A Python framework example for reviewing scan coverage and dependency declarations."),
)


def _text(value, limit=10000, *, empty=False):
    return isinstance(value, str) and len(value) <= limit and (empty or bool(value.strip()))


def _valid_result(result, repository):
    if not isinstance(result, dict):
        return False
    if not _text(result.get("repository"), 200) or result["repository"].lower() != repository.lower():
        return False
    if not _text(result.get("repoUrl"), 300) or result["repoUrl"].lower() != ("https://github.com/" + repository).lower():
        return False
    if not isinstance(result.get("commit"), str) or not re.fullmatch(r"[0-9a-fA-F]{40}", result["commit"]):
        return False
    checked_at = result.get("checkedAt")
    if not _text(checked_at, 80):
        return False
    try:
        if datetime.fromisoformat(checked_at.replace("Z", "+00:00")).utcoffset() is None:
            return False
    except (ValueError, OverflowError):
        return False
    count = result.get("filesScanned")
    if type(count) is not int or not 0 < count <= 300 or not _text(result.get("scope")):
        return False
    dependencies, findings, warnings = (result.get(key) for key in ("dependencies", "findings", "warnings"))
    if not isinstance(dependencies, list) or len(dependencies) > 500:
        return False
    for dependency in dependencies:
        if (not isinstance(dependency, dict)
                or not all(_text(dependency.get(key), 1024) for key in ("name", "ecosystem", "file"))
                or not (dependency.get("version") is None or _text(dependency["version"], 1024, empty=True))):
            return False
    if not isinstance(findings, list) or len(findings) > 50:
        return False
    for finding in findings:
        if (not isinstance(finding, dict) or finding.get("status") != "static_unverified"
                or type(finding.get("line")) is not int or finding["line"] < 1
                or not all(_text(finding.get(key)) for key in ("id", "title", "package", "file", "explanation", "sourceUrl"))
                or not all(_text(finding.get(key), empty=True) for key in ("beforeCode", "afterCode"))):
            return False
    return (isinstance(warnings, list) and len(warnings) <= 30
            and all(_text(warning) for warning in warnings)
            and result.get("status") in (None, "static_unverified"))


def _read_result(root, filename, repository):
    path = Path(root).resolve() / "demo" / "ready" / filename
    try:
        # Fixed filenames and an unredirected path keep evidence within this tree.
        if path.resolve() != path or not stat.S_ISREG(path.stat().st_mode):
            return None, "Saved source evidence must be a regular file inside demo/ready."
        with path.open("rb") as stream:
            data = stream.read(MAX_EVIDENCE_BYTES + 1)
        if len(data) > MAX_EVIDENCE_BYTES:
            return None, "Saved source evidence exceeds the size limit."
        result = json.loads(data)
    except FileNotFoundError:
        return None, "Saved source evidence is missing."
    except (OSError, ValueError, UnicodeError, RecursionError, RuntimeError):
        return None, "Saved source evidence could not be read."
    if not _valid_result(result, repository):
        return None, "Saved source evidence is invalid or does not match this repository."
    # Generate explanations from validated evidence instead of trusting stale copy.
    result["explanation"] = explain_public_result(result)
    return result, None


def _summary(entry_id, result):
    packages = {(row["ecosystem"], row["name"].lower().replace("_", "-")) for row in result["dependencies"]}
    rules = {row["id"].split(":", 1)[0] for row in result["findings"]}
    if entry_id == "gpu-energy-pandas" and "pandas-hour" in rules:
        return "Hourly data collection uses the pandas frequency alias flagged by the scan. The suggested change is untested in this source scan."
    if entry_id == "scam-killer" and "web-vitals-exports" in rules:
        return "Legacy web-vitals imports make a compact JavaScript migration review. The suggested migration is untested."
    if entry_id == "gauntlet" and ("npm", "next") in packages and any(ecosystem == "pypi" for ecosystem, _ in packages):
        summary = "A full-stack Python and Next.js dependency inventory."
    elif entry_id == "agent-with-a-brain" and {("pypi", "cognee"), ("pypi", "strands-agents")} <= packages:
        summary = "An agent using Cognee and Strands, whose APIs need checks beyond the three supported migration rules."
    elif entry_id == "flask" and ("pypi", "flask") in packages:
        summary = "An established Python framework showing source coverage and dependency declarations."
    else:
        summary = f"{result['filesScanned']} source or manifest files and {len(result['dependencies'])} dependency declarations ready for review."
    if not result["findings"]:
        return summary + " No rule matches; compatibility is unverified."
    return summary + " Static findings are suggestions that have not been tested."


def load_demo_ready(root, cases):
    """Return five curated cards using saved evidence; never run or fetch a scan.

    A confirmed GPU comparison links to its existing case. Every other available
    card owns a saved static scan; these are not inserted into session history.
    """
    confirmed_gpu = next((case for case in (cases or []) if isinstance(case, dict)
                          and case.get("id") == "gpu-energy-pandas"
                          and case.get("repository") == "Eman-Gon/gpu-energy-recommender"
                          and case.get("status") == "confirmed_break"), None)
    entries = []
    for entry_id, repository, filename, summary in _ENTRIES:
        entry = {"id": entry_id, "title": repository.split("/", 1)[1], "repository": repository,
                 "summary": summary, "evidenceLabel": "Saved source scan", "available": False,
                 "unavailableReason": None, "caseId": None, "scan": None}
        if entry_id == "gpu-energy-pandas" and confirmed_gpu is not None:
            entry.update(available=True, caseId=entry_id, evidenceLabel="Measured comparison",
                         summary="Repository pandas change with a verified fix: hourly collection fails on pandas 3, and lowercase h passes on both tested versions.")
        else:
            result, error = _read_result(root, filename, repository)
            entry["unavailableReason"] = error
            if result is not None:
                entry.update(available=True, summary=_summary(entry_id, result),
                             evidenceLabel="Static finding" if result["findings"] else "Saved source scan",
                             scan={"id": "demo-" + entry_id, "repository": repository, "status": "completed",
                                   "startedAt": result["checkedAt"], "finishedAt": result["checkedAt"],
                                   "error": None, "output": "", "result": result})
        entries.append(entry)
    return entries
