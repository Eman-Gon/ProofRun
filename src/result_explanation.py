"""Plain-language interpretation of bounded static scan evidence, without I/O."""

import re


_SUPPORTED = {"pandas": "pypi", "pydantic": "pypi", "web-vitals": "npm"}
_RULES = ("The current checks look for pandas uppercase hourly frequencies, Pydantic nullable fields "
          "without defaults, and legacy web-vitals imports.")


def _rows(value):
    return [row for row in value[:1000] if isinstance(row, dict)] if isinstance(value, list) else []


def _names(rows):
    result = []
    for row in rows:
        raw = row.get("name")
        if not isinstance(raw, str):
            continue
        name = re.sub(r"\[.*\]$", "", raw.strip().lower())
        if not re.fullmatch(r"(?:@[a-z0-9._-]+/)?[a-z0-9._-]{1,214}", name):
            continue
        name = re.sub(r"[-_.]+", "-", name) if not name.startswith("@") else name
        if name not in result:
            result.append(name)
    return result


def _join(items):
    if len(items) < 2:
        return "".join(items)
    if len(items) == 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _locations(findings):
    result = []
    for finding in findings:
        path, line = finding.get("file"), finding.get("line")
        if not isinstance(path, str) or not path or len(path) > 150 or any(ord(char) < 32 for char in path):
            continue
        location = f"{path}:{line}" if type(line) is int and line > 0 else path
        if location not in result:
            result.append(location)
        if len(result) == 2:
            break
    return _join(result)


def explain_public_result(result) -> dict:
    """Explain reported facts only; absence of a match never implies safety."""
    result = result if isinstance(result, dict) else {}
    dependencies = _rows(result.get("dependencies"))
    findings = _rows(result.get("findings"))
    names = _names(dependencies)
    supported = sorted({name for row in dependencies for name in _names([row])
                        if name in _SUPPORTED and row.get("ecosystem") == _SUPPORTED[name]})
    files = result.get("filesScanned")
    count_known = type(files) is int and files >= 0
    warnings = result.get("warnings")
    warnings = [item for item in warnings[:100] if isinstance(item, str)] if isinstance(warnings, list) else []
    partial = any(re.search(r"partial|skipped|limit (?:was )?reached|could not|incomplete|omitted", warning, re.I) for warning in warnings)
    open_constraints = any(isinstance(row.get("version"), str) and re.search(r">=|<=|[~^*]|(?<![=])>(?!=)|(?<![=])<(?!=)", row["version"]) for row in dependencies)

    if count_known and files:
        read = f"The {'partial ' if partial else ''}scan read {files} source or manifest file{'s' if files != 1 else ''}."
    elif count_known:
        read = "No eligible source or manifest files were scanned."
    else:
        read = "The report does not include a valid scanned-file count."

    if not count_known or files == 0:
        heading = "No scan coverage reported"
        summary = read + " This result cannot establish upgrade compatibility."
        if findings:
            summary += " Any attached findings need checking against a valid source scan."
    elif findings:
        number = len(findings)
        heading = f"{number} potential upgrade issue{'s' if number != 1 else ''} to review"
        summary = read + f" It reported {number} potential issue{'s' if number != 1 else ''}."
        locations = _locations(findings)
        if locations:
            summary += f" Start with {locations}."
        summary += " These are static suggestions; no code or tests were run, so failures and fixes have not been reproduced."
    elif names and not supported:
        heading = "These dependencies need broader checks"
        shown = _join(names[:4])
        extra = " and other packages" if len(names) > 4 else ""
        summary = read + f" It identified declarations for {shown}{extra}, but the current migration checks do not cover these packages."
        summary += " No matching issues were found; upgrade compatibility is still unknown."
    elif not names:
        heading = "Dependency coverage is unknown"
        summary = read + " No supported dependency declarations or matching migration patterns were identified."
        summary += " Imports can still be checked, but this result does not identify installed packages or establish upgrade compatibility."
    else:
        heading = "No matching migration patterns found"
        summary = read + f" It found no matches for the limited rules, including those for {_join(supported)}."
        summary += " Other API changes and packages remain unchecked; this is not evidence that an upgrade is compatible."

    if not (count_known and files and findings):
        summary += " No code or tests were run."
    checked = _RULES if count_known and files else "No eligible-file coverage is established. " + _RULES
    if partial:
        limits = "Limits or skipped data make this a partial report. "
    elif warnings:
        limits = "Review the scan warnings for coverage limits. "
    else:
        limits = ""
    if open_constraints:
        limits += "Open version constraints describe allowed versions, not the versions actually installed. "
    elif dependencies:
        limits += "Manifest declarations do not prove which versions are installed. "
    else:
        limits += "Installed dependency versions are unknown. "
    limits += "No repository code or tests were run."

    if not count_known or files == 0:
        next_step = "Check the scan warnings and choose a snapshot with supported source files before assessing an upgrade."
    elif {"cognee", "strands-agents"}.issubset(names):
        next_step = ("For this dependency set, exercise agent startup and memory write/recall with the current and proposed versions. "
                     "These are suggested tests; the scan did not inspect those behaviors.")
    elif findings:
        next_step = "Test the flagged code with the current and proposed dependency versions, then verify any suggested change on both."
    else:
        next_step = "Choose a proposed dependency version and test the affected code with both current and proposed versions."
    return {"heading": heading, "summary": summary, "checked": checked, "limits": limits, "nextStep": next_step}
