"""Plain terminal evidence: keep model judgment and executed tests distinct."""

import re
import unicodedata

from .github_fetch import Commit
from .judge import CRITERIA, Judgment
from .sandbox import SandboxResult


def terminal_text(value: str) -> str:
    # Commit messages and test output must not be able to clear/rewrite a terminal.
    value = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", value)
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    return "".join(c for c in value if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cf"))


def render_report(repo: str, commit: Commit, judgment: Judgment, tests: SandboxResult,
                  baseline_count: int, baseline_head: str, command: str,
                  dependency_context: dict | None = None) -> str:
    message = terminal_text(commit.message).splitlines()
    criteria = ", ".join(CRITERIA[hit] for hit in judgment.criteria_hit) or "none"
    exit_text = "not available" if tests.exit_code is None else str(tests.exit_code)
    lines = [
        "=" * 72,
        f"COMMIT-WATCH  {repo}",
        f"Commit    {commit.sha}",
        f"Author    {terminal_text(commit.author)[:80]}  |  {terminal_text(commit.date)}",
        f"Message   {message[0][:160] if message else '(empty)'}",
        f"Change    {len(commit.files)} files  +{commit.additions}/-{commit.deletions}",
        f"Baseline  {baseline_count} commits through {baseline_head[:12]}",
        "-" * 72,
        f"Flag: {judgment.flag.upper()}",
        f"Reason: {terminal_text(judgment.reason)}",
        f"Criteria: {criteria}",
    ]
    if dependency_context:
        context = dependency_context
        lines.extend(["", f"Upstream context: {terminal_text(context['status'])}"])
        for change in context["changes"][:10]:
            lines.append("  " + terminal_text(
                f"{change['ecosystem']} {change['package']}: {change['before'] or '(added)'}"
                f" -> {change['version'] or '(removed)'}"))
        if len(context["changes"]) > 10:
            lines.append(f"  ... {len(context['changes']) - 10} more dependency changes")
        sources = {s["source_id"]: s for s in context["candidate_sources"]}
        for note in judgment.dependency_notes:
            source = sources[note.source_id]
            lines.append("  " + terminal_text(f"{source['package']} {source['version']}: {note.summary}"))
            lines.append("    Evidence: " + terminal_text(note.evidence_quote))
            lines.append("    Source: " + terminal_text(source["source_url"]))
            lines.append("    Fetched: " + terminal_text(source["fetched_at"]))
        if context["candidate_sources"] and not judgment.dependency_notes and context.get("used_for_judgment", True):
            lines.append("  No relevant upstream finding selected from the retrieved sources.")
        if context["recalled_context"]:
            lines.append("  Prior sourced notes recalled for matching package versions (within 7 days).")
        if judgment.dependency_notes:
            lines.append("  Memory: " + terminal_text(context["memory_status"]))
        lines.extend("  Note: " + terminal_text(warning) for warning in context["warnings"])
    lines.extend([
        "", f"Tests: {tests.status.upper()}  |  exit {exit_text}  |  {tests.duration_seconds:.2f}s",
        f"Command: {terminal_text(command)}", "Sandbox output (last 30 lines):",
    ])
    output = terminal_text(tests.output_tail).splitlines()[-30:]
    lines.extend(f"  {line}" for line in output or ["(no output)"])
    if tests.status in ("error", "timeout"):
        lines.append("Review incomplete: sandbox did not finish successfully.")
    lines.append("=" * 72)
    return "\n".join(lines)


def exit_code(judgment: Judgment, tests: SandboxResult) -> int:
    if tests.status in ("error", "timeout"):
        return 2
    return 1 if judgment.flag == "yes" or tests.status == "fail" else 0
