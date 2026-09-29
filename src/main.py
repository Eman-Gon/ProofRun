"""Dependency-upgrade demo plus the original manual commit-review commands."""

import argparse
import asyncio
import sys

from .config import Settings
from .dependency_context import collect_dependency_context, remember_selected_notes
from .github_fetch import GitHubClient, GitHubError, validate_sha
from .judge import JudgeError, judge_commit
from .memory import Memory, MemoryError
from .report import exit_code, render_report, terminal_text
from .sandbox import SandboxError, preflight, run_sandbox


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Hackday Idea: investigate dependency upgrades with reproducible tests.")
    commands = cli.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="Store 30–50 commits as the repo baseline.")
    ingest.add_argument("--count", type=int, help="Number of commits (30–50; default from .env).")
    ingest.add_argument("--ref", help="End baseline at this ref; use a parent of your demo commits.")
    check = commands.add_parser("check", help="Judge a new commit and run its tests in Docker.")
    check.add_argument("sha", help="7–40 character GitHub commit SHA.")
    upgrade = commands.add_parser("upgrade-demo", help="Compare Pydantic versions on one customer-import behavior.")
    mode = upgrade.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="Use curated source/probe data; requires cached Docker images.")
    mode.add_argument("--prepare", action="store_true", help="Only build the two demo images; downloads pinned dependencies without API keys.")
    upgrade.add_argument("--rebuild", action="store_true", help="Rebuild the two pinned dependency images.")
    upgrade.add_argument("--no-memory", action="store_true", help="Skip optional Cognee persistence of the verified finding.")
    upgrade.add_argument("--require-integrations", action="store_true", help="Require verified Bright Data sourcing and Cognee storage/retrieval; no source fallback.")
    return cli


def progress(message: str) -> None:
    print(terminal_text(message), file=sys.stderr, flush=True)


def execute(args, settings: Settings) -> int:
    github = GitHubClient(settings.repo, settings.github_token)
    memory = Memory(settings.repo, settings.state_dir)
    if args.command == "ingest":
        count = args.count if args.count is not None else settings.baseline_count
        if not 30 <= count <= 50:
            raise ValueError("Baseline must contain 30–50 commits.")
        progress(f"Fetching {count} commits from {settings.repo}...")
        commits = github.history(count=count, ref=args.ref)
        progress("Building local Cognee baseline (Groq extraction + local embeddings)...")
        try:
            manifest = asyncio.run(memory.ingest(commits))
        except MemoryError:
            raise
        except Exception as exc:
            raise MemoryError(
                "Cognee ingestion failed; check the Groq key, rate limits, and model download connectivity. "
                "The previous baseline remains active."
            ) from exc
        print(f"Baseline ready: {len(commits)} commits from {settings.repo}")
        print(f"Through: {manifest['head']}")
        print(f"Memory: {manifest['dataset']}")
        return 0

    # One reusable loop keeps Cognee's cached database clients usable across
    # recall/storage, while synchronous sandbox execution retains SIGINT cleanup.
    with asyncio.Runner() as runner:
        return check_commit(args, settings, github, memory, runner)


def check_commit(args, settings, github, memory, runner) -> int:
    sha = validate_sha(args.sha)
    manifest = memory.manifest()
    preflight(settings.sandbox_image)
    progress(f"Fetching commit {sha}...")
    commit = github.commit(sha)
    if commit.sha in manifest["shas"]:
        raise MemoryError("This commit is already in the baseline; ingest an earlier --ref first.")
    if not github.is_ancestor(manifest["head"], commit.sha):
        raise MemoryError("Baseline is not an ancestor of this commit; ingest an earlier --ref first.")
    diff = github.diff(commit.sha)
    paths = github.tree_paths(commit.parents[0] if commit.parents else commit.sha)
    progress("Retrieving baseline evidence from Cognee...")
    try:
        baseline = runner.run(memory.search(commit))
    except MemoryError:
        raise
    except Exception as exc:
        raise MemoryError("Cognee retrieval failed; check the local memory and embedding model.") from exc
    progress("Checking for changed dependencies and relevant upstream context...")
    try:
        context = runner.run(collect_dependency_context(github, commit, memory, settings.brightdata_key))
    except Exception:
        context = {"changes": [], "candidate_sources": [], "recalled_context": "",
                   "status": "unavailable", "memory_status": "not needed",
                   "warnings": ["Optional dependency context failed; the core review continues."]}
    progress("Judging against the four fixed criteria...")
    judgment = judge_commit(commit, diff, baseline, paths, settings.groq_key, dependency_context=context)
    progress(f"Running Docker tests (limit {settings.test_timeout}s)...")
    try:
        tests = run_sandbox(
            settings.repo, commit.sha, settings.sandbox_image, settings.test_command,
            timeout=settings.test_timeout, github_token=settings.github_token,
        )
    except SandboxError as exc:
        # Preserve a completed judgment if the sandbox fails to start/fetch.
        from .sandbox import SandboxResult
        tests = SandboxResult("error", None, str(exc), 0)
    if judgment.dependency_notes:
        progress("Remembering selected sourced dependency notes separately from the baseline...")
        runner.run(remember_selected_notes(memory, context, judgment))
    print(render_report(settings.repo, commit, judgment, tests, len(manifest["shas"]),
                        manifest["head"], settings.test_command, dependency_context=context))
    return exit_code(judgment, tests)


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "upgrade-demo":
            from .upgrade_demo import UpgradeError, run_demo
            try:
                return run_demo(offline=args.offline, rebuild=args.rebuild, remember=not args.no_memory,
                                prepare=args.prepare, require_integrations=args.require_integrations)
            except UpgradeError as exc:
                progress(f"Error: {exc}")
                return 2
        return execute(args, Settings.load())
    except (ValueError, GitHubError, MemoryError, JudgeError, SandboxError) as exc:
        progress(f"Error: {exc}")
        return 2
    except KeyboardInterrupt:
        progress("Interrupted; review incomplete.")
        return 130
    except Exception as exc:
        # Provider exception messages can embed request bodies or credentials.
        progress(f"Error: unexpected {type(exc).__name__}; review incomplete.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
