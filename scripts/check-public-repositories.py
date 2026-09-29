#!/usr/bin/env python3
"""Read-only smoke matrix for public repository review; never runs upstream code.

Without --env-file, only the caller's existing environment is used. Use
--env-file .env to include live model review when configured. This can incur
the configured provider's normal request costs. Reports contain public source
findings and model provenance, never environment values or credentials.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_REPOSITORIES = (
    "pallets/flask",              # Python, pyproject.toml
    "sindresorhus/is",            # TypeScript, package.json
    "spf13/cobra",                # Go, go.mod
    "serde-rs/json",              # Rust, Cargo.toml
    "octocat/Hello-World",        # README-only, no dependency assumptions
)


def check(repository: str, require_agent: bool = False) -> dict:
    from src.public_repo import PublicRepoError, inspect_public_repo

    started = time.monotonic()
    progress = []
    try:
        result = inspect_public_repo(repository, emit=progress.append)
    except PublicRepoError as error:
        return {"repository": repository, "status": "blocked", "error": str(error),
                "durationSeconds": round(time.monotonic() - started, 2), "progress": progress}
    except Exception as error:
        # Provider or network exception messages can contain credentials/URLs.
        return {"repository": repository, "status": "failed", "errorType": type(error).__name__,
                "durationSeconds": round(time.monotonic() - started, 2), "progress": progress}

    dependencies = result.get("dependencies", [])
    findings = result.get("findings", [])
    checks = {
        "commitPinned": bool(re.fullmatch(r"[a-fA-F0-9]{40}", result.get("commit", ""))),
        "repositoryMatches": result.get("repository", "").lower() == repository.lower(),
        "fileCountKnown": type(result.get("filesScanned")) is int,
        "dependenciesHaveSource": isinstance(dependencies, list) and all(
            isinstance(row, dict) and all(isinstance(row.get(key), str) and row[key]
                                         for key in ("name", "ecosystem", "file"))
            for row in dependencies),
        "findingsHaveSource": isinstance(findings, list) and all(
            isinstance(row, dict) and isinstance(row.get("file"), str) and row["file"]
            and type(row.get("line")) is int and row["line"] > 0 for row in findings),
        "explainsLimits": isinstance(result.get("explanation"), dict)
                          and bool(result["explanation"].get("limits")),
    }
    review = result.get("review", {})
    if require_agent:
        checks["agentCompleted"] = review.get("status") == "completed"
    return {"repository": repository, "status": "completed" if all(checks.values()) else "failed",
            "durationSeconds": round(time.monotonic() - started, 2), "checks": checks,
            "progress": progress, "result": result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New JSON evidence file.")
    parser.add_argument("--repository", action="append", help="Public owner/repo; repeat up to five times.")
    parser.add_argument("--env-file", type=Path, help="Explicit configuration file for live model review.")
    parser.add_argument("--require-agent", action="store_true",
                        help="Also require the configured agent review to finish successfully.")
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2,
                        help="At most two repository reviews concurrently.")
    args = parser.parse_args()
    repositories = args.repository or list(DEFAULT_REPOSITORIES)
    if not 1 <= len(repositories) <= 5:
        parser.error("Select between one and five public repositories.")
    if args.output.exists():
        parser.error("Choose a new output file to preserve previous evidence.")
    if args.env_file:
        from dotenv import load_dotenv
        if not args.env_file.is_file():
            parser.error("The selected environment file does not exist.")
        load_dotenv(args.env_file, override=False)

    started = time.monotonic()
    report = {"checkedAt": datetime.now(timezone.utc).isoformat(),
              "scope": "Public pinned-source inspection and optional model review. No upstream code, tests, installs, or PR publication.",
              "repositories": repositories, "results": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for row in executor.map(lambda repository: check(repository, args.require_agent), repositories):
            report["results"].append(row)
            report["durationSeconds"] = round(time.monotonic() - started, 2)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            result = row.get("result", {})
            print(json.dumps({"repository": row["repository"], "status": row["status"],
                              "files": result.get("filesScanned"),
                              "dependencies": len(result.get("dependencies", [])),
                              "findings": len(result.get("findings", [])),
                              "agent": result.get("review", {}).get("status"),
                              "durationSeconds": row["durationSeconds"]}), flush=True)
    return 0 if all(row["status"] == "completed" for row in report["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
