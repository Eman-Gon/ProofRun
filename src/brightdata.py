"""Two deterministic Bright Data MCP calls for a sourced dependency excerpt.

Hosted authentication and schemas were checked against Bright Data's remote MCP
docs and brightdata/brightdata-mcp/server.js. The transport targets MCP 2.x;
Strands' MCPClient supplies the synchronous bridge, without constructing an LLM.
"""

from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
import importlib
import json
import logging
import re
import threading
from urllib.parse import urlencode, urlsplit, urlunsplit
import uuid


TOOLS = ("search_engine", "scrape_as_markdown")
TOOL_TIMEOUT = 30
MAX_RESULT_BYTES = 256_000
MAX_EXCERPT_CHARS = 6_000
_HOSTED_ENVELOPE = re.compile(
    r"\ASECURITY NOTICE: the content between the markers below \(id (?P<id>[0-9a-f]{32})\)"
    r"[^\r\n]*\n=====UNTRUSTED_(?P=id)_BEGIN=====\n"
    r"(?P<body>[\s\S]*)\n=====UNTRUSTED_(?P=id)_END=====\Z"
)
_LOG_LOCK = threading.RLock()


class BrightDataError(RuntimeError):
    pass


@contextmanager
def _private_transport_logs():
    # The documented endpoint carries its token in the URL. MCP/HTTP exception
    # logs include that URL, so keep this synchronous connection out of logging.
    # Restore the application's previous setting even on connection failure.
    with _LOG_LOCK:
        previous = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            yield
        finally:
            logging.disable(previous)


def _make_client(api_key: str, *, timeout: int = TOOL_TIMEOUT):
    mcp = importlib.import_module("mcp.client.streamable_http")
    httpx = importlib.import_module("httpx2")
    strands = importlib.import_module("strands.tools.mcp")
    # The hosted tools query can omit scrape_as_markdown from discovery. Use the
    # default endpoint and keep the allowlist in Strands and _call instead.
    endpoint = "https://mcp.brightdata.com/mcp?" + urlencode({"token": api_key})

    @asynccontextmanager
    async def transport():
        # No redirects: a redirect must never carry the query token elsewhere.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10), follow_redirects=False,
        ) as client:
            async with mcp.streamable_http_client(endpoint, http_client=client) as streams:
                yield streams

    return strands.MCPClient(
        transport, startup_timeout=15, tool_filters={"allowed": list(TOOLS)},
        application_name="commit-watch", continue_on_error=False,
    )


def _call(client, name: str, arguments: dict, *, timeout: int = TOOL_TIMEOUT) -> dict:
    if name not in TOOLS:
        raise BrightDataError("Only dependency search and page reading are allowed.")
    cancelled = threading.Event()
    timer = threading.Timer(timeout, cancelled.set)
    timer.daemon = True
    timer.start()
    try:
        result = client.call_tool_sync(
            tool_use_id=uuid.uuid4().hex, name=name, arguments=arguments,
            read_timeout_seconds=timedelta(seconds=timeout), cancel_signal=cancelled,
        )
    finally:
        timer.cancel()
    if cancelled.is_set() or (isinstance(result, dict) and result.get("cancelled")):
        raise BrightDataError("Bright Data timed out; retry dependency context later.")
    if not isinstance(result, dict) or result.get("status") != "success" or result.get("isError"):
        raise BrightDataError("Bright Data could not read dependency context; check the API key, quota, and access.")
    return result


def _text(result: dict) -> str:
    blocks = result.get("content")
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 8:
        raise BrightDataError("Bright Data returned missing or unsupported text content.")
    parts, total = [], 0
    for block in blocks:
        value = block.get("text") if isinstance(block, dict) else None
        if not isinstance(value, str):
            raise BrightDataError("Bright Data returned unsupported non-text content.")
        total += len(value.encode("utf-8"))
        if total > MAX_RESULT_BYTES:
            raise BrightDataError("Bright Data returned too much content; narrow the dependency version.")
        parts.append(value)
    text = "\n".join(parts).strip()
    if text.startswith("SECURITY NOTICE:"):
        # The hosted service wraps JSON and Markdown in this envelope. Decode
        # only a complete matching frame; its body remains untrusted evidence.
        envelope = _HOSTED_ENVELOPE.fullmatch(text)
        if envelope is None:
            raise BrightDataError("Bright Data returned an incomplete or unsupported source envelope.")
        text = envelope.group("body").strip()
    if not text:
        raise BrightDataError("Bright Data returned an empty page or search result.")
    return text


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.casefold())


def _version_query(version: str, ecosystem: str) -> str:
    # Search terms are separate from the caller's original dependency identity.
    # Reject URLs/control characters before discarding markers, not afterward.
    if (not isinstance(version, str) or len(version) > 300 or not version.strip()
            or re.search(r"[\x00-\x1f\x7f\\:]", version)):
        raise BrightDataError("Dependency context requires a bounded registry version or constraint, not a URL.")
    constraint = version.strip()
    if ecosystem == "pypi":
        constraint = re.sub(r"^\[[A-Za-z0-9_.-]+(?:\s*,\s*[A-Za-z0-9_.-]+)*\]\s*", "", constraint)
        constraint = constraint.split(";", 1)[0].strip()
    if not re.fullmatch(r"[A-Za-z0-9.*+!<>=~^|, -]{1,120}", constraint):
        raise BrightDataError("Dependency context requires a valid registry version or constraint.")
    tokens = list(dict.fromkeys(re.findall(r"\d+(?:\.\d+)*(?:[-+.]?[A-Za-z][A-Za-z0-9.-]*)?", constraint)))[:4]
    if not tokens:
        if constraint.casefold() in ("*", "latest", "next", "alpha", "beta", "canary"):
            return '"release notes"'
        raise BrightDataError("Dependency context could not identify useful release-version terms.")
    terms = [json.dumps(token) for token in tokens]
    return terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")"


def _source_url(value: object, package: str) -> str | None:
    """Restrict reads to candidate GitHub release/issue pages, never arbitrary URLs."""
    if not isinstance(value, str) or len(value) > 2_048 or re.search(r"[\s\\%\x00-\x1f\x7f]", value):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.hostname != "github.com" or url.username is not None
                or url.password is not None or url.port not in (None, 443) or url.query):
            return None
    except ValueError:
        return None
    parts = url.path.strip("/").split("/")
    if len(parts) < 3 or any(part in ("", ".", "..") for part in parts):
        return None
    owner, repository = parts[:2]
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in (owner, repository)):
        return None
    if package.startswith("@"):
        scope, name = package[1:].split("/")
        if _normalize(owner) != _normalize(scope) or _normalize(repository) != _normalize(name):
            return None
    elif _normalize(repository) != _normalize(package):
        return None
    is_release = parts[2] == "releases" and (len(parts) == 3 or (len(parts) >= 5 and parts[3] == "tag"))
    is_issue = len(parts) == 4 and parts[2] == "issues" and bool(re.fullmatch(r"[1-9][0-9]*", parts[3]))
    if not (is_release or is_issue):
        return None
    return urlunsplit(("https", "github.com", url.path, "", ""))


def _select_source(result: dict, package: str) -> tuple[str, str]:
    payload = result.get("structuredContent")
    if payload is None:
        try:
            payload = json.loads(_text(result))
        except (ValueError, TypeError):
            raise BrightDataError("Bright Data search returned malformed JSON; no page was scraped.") from None
    elif len(json.dumps(payload).encode("utf-8")) > MAX_RESULT_BYTES:
        raise BrightDataError("Bright Data returned too many search results.")
    organic = payload.get("organic") if isinstance(payload, dict) else None
    if not isinstance(organic, list):
        raise BrightDataError("Bright Data search did not return its expected organic results.")
    # First acceptable result, no pagination, no broadening the search, no retry.
    for item in organic[:10]:
        if not isinstance(item, dict):
            continue
        url = _source_url(item.get("link"), package)
        title = item.get("title")
        if url and isinstance(title, str) and title.strip():
            clean_title = re.sub(r"[\x00-\x1f\x7f]", " ", title).strip()[:240]
            return url, clean_title
    raise BrightDataError("No matching public GitHub release or issue source was found for this package.")


class BrightDataClient:
    def __init__(self, api_key: str):
        if not isinstance(api_key, str) or not api_key.strip():
            raise BrightDataError("Set BRIGHTDATA_API_KEY to enable dependency context.")
        self._api_key = api_key.strip()

    def fetch_context(self, package: str, ecosystem: str, version: str) -> dict:
        if ecosystem == "python":
            ecosystem = "pypi"
        pattern = r"(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9][A-Za-z0-9_.-]*" if ecosystem == "npm" else r"[A-Za-z0-9][A-Za-z0-9_.-]*"
        if (ecosystem not in ("pypi", "npm") or not isinstance(package, str)
                or len(package) > 160 or not re.fullmatch(pattern, package)):
            raise BrightDataError("Dependency context requires a valid PyPI or npm package name.")
        version_terms = _version_query(version, ecosystem)
        query = f'site:github.com "{package}" {version_terms} (inurl:releases OR inurl:issues)'
        try:
            with _private_transport_logs(), _make_client(self._api_key) as client:
                results = _call(client, "search_engine", {"query": query, "engine": "google"})
                url, title = _select_source(results, package)
                page = _text(_call(client, "scrape_as_markdown", {"url": url}))
                page = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", page)
                if not page.strip():
                    raise BrightDataError("Bright Data returned no readable dependency context.")
                excerpt = page[:MAX_EXCERPT_CHARS]
                if len(page) > MAX_EXCERPT_CHARS:
                    excerpt += "\n[Source excerpt truncated.]"
                return {"source_url": url, "title": title, "text": excerpt}
        except BrightDataError:
            raise
        except Exception:
            # Neither a transport URL containing token= nor server text belongs
            # in a user-visible exception or its chained traceback.
            raise BrightDataError(
                "Bright Data MCP connection failed; check BRIGHTDATA_API_KEY, connectivity, and quota."
            ) from None
