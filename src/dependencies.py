"""Extract changed registry dependency specs from complete file snapshots."""

import json
from pathlib import PurePosixPath
import re
import tomllib

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name


MAX_DEPENDENCY_BYTES = 256 * 1024
_NPM_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
_LOCKS = {"package-lock.json", "npm-shrinkwrap.json", "poetry.lock", "uv.lock", "pipfile.lock"}
_EXTRA = {"cargo.toml", "cargo.lock", "go.mod", "go.sum", "composer.json", "composer.lock"}
_UNSUPPORTED = {"yarn.lock", "pnpm-lock.yaml", "pom.xml", "build.gradle", "build.gradle.kts",
                "gemfile", "gemfile.lock", "pipfile", "setup.py", "setup.cfg", "packages.config"}


class DependencyParseError(ValueError):
    pass


def is_dependency_file(path: str) -> bool:
    parts = PurePosixPath(path.lower()).parts
    name = parts[-1] if parts else ""
    return name in _LOCKS | _EXTRA | _UNSUPPORTED | {"package.json", "pyproject.toml"} or name.endswith((".csproj", ".fsproj")) or (
        name.endswith(".txt") and (name.startswith("requirements") or "requirements" in parts[:-1])
    )


def _mapping(value):
    if not isinstance(value, dict):
        raise DependencyParseError("Expected a dependency object.")
    return value


def _list(value):
    if not isinstance(value, list):
        raise DependencyParseError("Expected a dependency list.")
    return value


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DependencyParseError("Duplicate JSON dependency keys.")
        result[key] = value
    return result


def _spec(value):
    if not isinstance(value, str) or not value.strip():
        raise DependencyParseError("Dependency version must be a nonempty string.")
    value = value.strip()
    # Keep only registry names and specs. Paths, URLs, credentials, git sources,
    # workspace links, and opaque protocols must never enter external queries.
    if any(char in value for char in ":/@\\") or value in (".", ".."):
        return None
    if len(value) > 2048 or not re.fullmatch(r"[A-Za-z0-9.*+^~<>=!|,\-_ ;\[\]()'\"]+", value):
        raise DependencyParseError("Unsupported dependency version syntax.")
    return value


def _add(result, ecosystem, name, spec):
    if not isinstance(name, str):
        raise DependencyParseError("Dependency name must be a string.")
    if ecosystem == "pypi":
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", name):
            raise DependencyParseError("Invalid Python dependency name.")
        name = str(canonicalize_name(name))
    elif ecosystem == "npm" and (not re.fullmatch(r"(?:@[a-z0-9._-]+/)?[a-z0-9._-]+", name) or len(name) > 214):
        raise DependencyParseError("Invalid npm dependency name.")
    elif ecosystem not in {"npm", "pypi"} and (len(name) > 214 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", name)):
        raise DependencyParseError("Invalid registry dependency name.")
    safe = _spec(spec)
    if safe is not None:
        result.setdefault(name, set()).add(safe)


def _npm_add(result, name, value):
    if isinstance(value, str) and value.startswith("npm:"):
        alias = re.fullmatch(r"npm:((?:@[^/@]+/)?[^/@]+)@(.+)", value)
        if not alias:
            raise DependencyParseError("Invalid npm alias dependency.")
        name, value = alias.groups()
    _add(result, "npm", name, value)


def _npm(data, locked):
    result = {}
    if not locked:
        for section in _NPM_SECTIONS:
            for name, spec in _mapping(data.get(section, {})).items():
                _npm_add(result, name, spec)
        return result
    version = data.get("lockfileVersion")
    if type(version) is not int or version not in (1, 2, 3):
        raise DependencyParseError("Unsupported npm lockfile version.")
    packages = _mapping(data.get("packages")) if version >= 2 else {}
    non_registry = set()
    for entry in packages.values():
        for section in _NPM_SECTIONS:
            for name, spec in _mapping(_mapping(entry).get(section, {})).items():
                if isinstance(spec, str) and not spec.startswith("npm:") and _spec(spec) is None:
                    non_registry.add(name)

    def add_entry(name, entry):
        entry = _mapping(entry)
        if entry.get("link") or name in non_registry:
            return
        resolved = entry.get("resolved", "")
        if not isinstance(resolved, str):
            raise DependencyParseError("Invalid npm resolved source.")
        if resolved.startswith(("git", "file:", "link:", "../", "./", "/")):
            return
        _npm_add(result, entry.get("name", name), entry.get("version"))

    if version >= 2:
        for location, entry in packages.items():
            if location.startswith("node_modules/") or "/node_modules/" in location:
                add_entry(location.rsplit("node_modules/", 1)[1], entry)
    else:
        def walk(entries):
            for name, entry in _mapping(entries).items():
                add_entry(name, entry)
                walk(_mapping(entry).get("dependencies", {}))
        walk(data.get("dependencies", {}))
    return result


def _requirement(result, text):
    if not isinstance(text, str):
        raise DependencyParseError("Python dependency must be a PEP 508 string.")
    try:
        req = Requirement(text)
    except InvalidRequirement:
        raise DependencyParseError("Invalid PEP 508 dependency.") from None
    if req.url:
        return
    extras = f"[{','.join(sorted({canonicalize_name(extra) for extra in req.extras}))}]" if req.extras else ""
    marker = f"; {req.marker}" if req.marker else ""
    _add(result, "pypi", req.name, extras + (str(req.specifier) or "*") + marker)


def _requirements(text):
    result = {}
    text = re.sub(r"\\\r?\n", " ", text)
    for line in text.splitlines():
        line = re.split(r"\s+#", line, maxsplit=1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r ", "-c ", "-e ", "--requirement", "--constraint", "--editable",
                            "--index-url", "--extra-index-url", "--find-links", "--trusted-host",
                            "--no-index", "--only-binary", "--no-binary", "--prefer-binary", "--pre")):
            continue
        if re.match(r"(?:[A-Za-z][A-Za-z0-9+.-]*://|git\+|\.?\.?/|~/|[A-Za-z]:\\)", line):
            continue
        if re.fullmatch(r"[^ ;]+\.(?:whl|zip|tar\.gz|tgz)", line, re.IGNORECASE):
            continue
        line = re.sub(r"\s+--hash(?:=|\s+)\S+", "", line).strip()
        _requirement(result, line)
    return result


def _python_manifest(data):
    result = {}
    project = _mapping(data.get("project", {}))
    dependencies = list(_list(project.get("dependencies", [])))
    for group in _mapping(project.get("optional-dependencies", {})).values():
        dependencies.extend(_list(group))
    if "dependencies" in project.get("dynamic", []):
        raise DependencyParseError("Dynamic Python dependencies require a resolved lockfile.")
    poetry = _mapping(_mapping(data.get("tool", {})).get("poetry", {}))
    if poetry.get("dependencies") or poetry.get("group") or poetry.get("dev-dependencies"):
        raise DependencyParseError("Poetry manifest dependencies are unsupported; use poetry.lock.")
    for requirement in dependencies:
        _requirement(result, requirement)
    return result


def _python_lock(data, kind):
    result = {}
    if kind == "pipfile.lock":
        if not any(group in data for group in ("default", "develop")):
            raise DependencyParseError("Missing Pipfile lock dependency sections.")
        for group in ("default", "develop"):
            for name, entry in _mapping(data.get(group, {})).items():
                entry = _mapping(entry)
                if any(key in entry for key in ("git", "path", "file", "uri", "url", "editable")):
                    continue
                extras = _list(entry.get("extras", []))
                if any(not isinstance(extra, str) for extra in extras):
                    raise DependencyParseError("Invalid Python dependency extras.")
                marker = entry.get("markers")
                text = name + (f"[{','.join(extras)}]" if extras else "") + entry.get("version", "")
                _requirement(result, text + (f"; {marker}" if marker else ""))
        return result
    for entry in _list(data.get("package")):
        entry = _mapping(entry)
        source = _mapping(entry.get("source", {}))
        if any(key in source for key in ("git", "url", "path", "directory", "editable", "virtual")):
            # Poetry registry sources have a URL alongside type=legacy. It is
            # safe to retain the version while discarding all source fields.
            if not (kind == "poetry.lock" and source.get("type") in ("legacy", "repository")):
                continue
        if source.get("type") in ("git", "file", "directory", "url"):
            continue
        markers = entry.get("resolution-markers", []) if kind == "uv.lock" else []
        markers = _list(markers)
        if entry.get("markers"):
            markers = markers + [entry["markers"]]
        for marker in markers or [None]:
            version = entry.get("version")
            if not isinstance(version, str) or not version:
                raise DependencyParseError("Missing locked Python dependency version.")
            _requirement(result, str(entry.get("name", "")) + "==" + version + (f"; {marker}" if marker else ""))
    return result


def _cargo(data, locked=False):
    result = {}
    if locked:
        for entry in _list(data.get("package")):
            entry = _mapping(entry)
            if str(entry.get("source", "")).startswith("registry+"):
                _add(result, "cargo", entry.get("name"), entry.get("version"))
        return result

    def groups(table):
        for group in ("dependencies", "dev-dependencies", "build-dependencies"):
            for name, entry in _mapping(table.get(group, {})).items():
                if isinstance(entry, str):
                    _add(result, "cargo", name, entry)
                else:
                    entry = _mapping(entry)
                    if not any(key in entry for key in ("path", "git", "workspace")):
                        _add(result, "cargo", entry.get("package", name), entry.get("version", "*"))
    groups(data)
    groups(_mapping(data.get("workspace", {})))
    for table in _mapping(data.get("target", {})).values():
        groups(_mapping(table))
    return result


def _go(text, locked=False):
    result, in_require, replaced = {}, False, set()
    if locked:
        for line in text.splitlines():
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) != 3 or not parts[2].startswith("h1:"):
                raise DependencyParseError("Malformed Go checksum entry.")
            _add(result, "golang", parts[0], parts[1].removesuffix("/go.mod"))
        return result
    for line in text.splitlines():
        line = line.split("//", 1)[0].strip()
        if "=>" in line:
            # A replaced module's require version is not its selected source.
            left = line.split("=>", 1)[0].removeprefix("replace ").split()
            if left:
                replaced.add(left[0])
        if line.startswith("require "):
            line = line[len("require "):].strip()
            if line == "(":
                in_require = True
                continue
        elif not in_require:
            continue
        if line == ")":
            in_require = False
            continue
        if line:
            parts = line.split()
            if len(parts) != 2:
                raise DependencyParseError("Malformed Go require declaration.")
            _add(result, "golang", parts[0], parts[1])
    if in_require:
        raise DependencyParseError("Unclosed Go require block.")
    return {name: specs for name, specs in result.items() if name not in replaced}


def _composer(data, locked=False):
    result = {}
    if locked:
        for group in ("packages", "packages-dev"):
            for entry in _list(data.get(group, [])):
                entry = _mapping(entry)
                _add(result, "packagist", entry.get("name"), entry.get("version"))
    else:
        for group in ("require", "require-dev"):
            for name, version in _mapping(data.get(group, {})).items():
                if "/" in name:  # Runtime and ext-* requirements are not packages.
                    _add(result, "packagist", name, version)
    return result


def _snapshot(path, text):
    if text is None:
        return {}
    name = PurePosixPath(path.lower()).name
    try:
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_DEPENDENCY_BYTES:
            raise DependencyParseError("Dependency file exceeds the 256 KB parsing limit.")
        if name in {"go.mod", "go.sum"}:
            return _go(text, name == "go.sum")
        if name in {"cargo.toml", "cargo.lock"}:
            return _cargo(tomllib.loads(text), name == "cargo.lock")
        if name in {"composer.json", "composer.lock"}:
            return _composer(_mapping(json.loads(text, object_pairs_hook=_json_object)), name == "composer.lock")
        if name.endswith(".json") or name == "pipfile.lock":
            data = _mapping(json.loads(text, object_pairs_hook=_json_object))
            return _python_lock(data, name) if name == "pipfile.lock" else _npm(data, name != "package.json")
        if name in ("pyproject.toml", "uv.lock", "poetry.lock"):
            data = tomllib.loads(text)
            return _python_manifest(data) if name == "pyproject.toml" else _python_lock(data, name)
        return _requirements(text)
    except DependencyParseError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise DependencyParseError("Malformed dependency file.") from None


def changed_dependencies(path: str, before: str | None, after: str | None) -> list[dict]:
    """Return only changed package specs, with no source or registry addresses."""
    if not is_dependency_file(path):
        raise DependencyParseError("Unsupported dependency filename.")
    name = PurePosixPath(path.lower()).name
    if name in _UNSUPPORTED or name.endswith((".csproj", ".fsproj")):
        raise DependencyParseError(f"{name} dependency parsing is not supported.")
    old, new = _snapshot(path, before), _snapshot(path, after)
    ecosystem = ("npm" if name in ("package.json", "package-lock.json", "npm-shrinkwrap.json") else
                 "cargo" if name.startswith("cargo.") else "golang" if name in {"go.mod", "go.sum"} else
                 "packagist" if name.startswith("composer.") else "pypi")
    return [{"ecosystem": ecosystem, "package": package,
             "before": " || ".join(sorted(old[package])) if package in old else None,
             "version": " || ".join(sorted(new[package])) if package in new else None}
            for package in sorted(old.keys() | new.keys()) if old.get(package) != new.get(package)]
