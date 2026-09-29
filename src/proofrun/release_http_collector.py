"""Owned HTTP collector entrypoint: no application source, model code or credentials.

Mounted read-only in a trusted Python sidecar sharing only the application's
loopback network namespace. Application stdout is never a proof channel.
"""
from __future__ import annotations

import json
import math
import sys
import time

try:
    from . import release_runtime as runtime
except ImportError:  # Standalone files are mounted at /collector in Docker.
    import release_runtime as runtime


MAX_INPUT = runtime.MAX_STEPS * (runtime.MAX_BODY + 512) + 4096


def collect(plan: dict) -> dict:
    result = {"execution_status": "setup_failed", "observations": [], "complete": False}
    try:
        if not isinstance(plan, dict) or set(plan) != {"port", "health_path", "startup_seconds", "deadline_seconds", "steps"}:
            raise runtime.RuntimeFailure("Invalid collection plan.")
        if type(plan["port"]) is not int or not 1024 <= plan["port"] <= 65535:
            raise runtime.RuntimeFailure("Invalid collection port.")
        for key, upper in (("startup_seconds", 60), ("deadline_seconds", 600)):
            value = plan[key]
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= upper:
                raise runtime.RuntimeFailure("Invalid collection time limit.")
        steps = runtime._steps(plan["steps"])
        health_path = runtime._path(plan["health_path"])
        deadline = time.monotonic() + plan["deadline_seconds"]
        startup = min(deadline, time.monotonic() + plan["startup_seconds"])
        base_url = f"http://127.0.0.1:{plan['port']}"
        while True:
            runtime._remaining(startup)
            try:
                health = runtime._http(base_url, {"method": "GET", "path": health_path}, startup)
                if 200 <= health["status"] < 300:
                    break
            except runtime.DeadlineExceeded:
                raise
            except runtime.HTTPUnavailable:
                pass
            time.sleep(min(.1, runtime._remaining(startup)))
        for step in steps:
            result["observations"].append(runtime._http(base_url, step, deadline))
        result.update(execution_status="completed", complete=True)
    except runtime.RuntimeFailure as exc:
        runtime._failure(result, exc)
    return result


def main() -> int:
    try:
        raw = sys.stdin.buffer.readline(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT or not raw.endswith(b"\n"):
            raise ValueError("Collection plan too large")
        result = collect(json.loads(raw))
    except (ValueError, TypeError, RecursionError):
        result = {"execution_status": "setup_failed", "observations": [], "complete": False,
                  "error": "Invalid collection input."}
    print(json.dumps(result, allow_nan=False), flush=True)
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
