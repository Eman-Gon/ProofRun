#!/usr/bin/env python3
"""Initialize local licensed portal access using the official DevKit API sequence.

This does not start containers, obtain/change a license, configure an LLM, or
declare the full DevKit setup complete. Secrets are read from the sibling .env
and never included in console messages or HTTP error descriptions.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
PERSISTED_KEYS = {
    "DUPLO_ADMIN_TOKEN",
    "EXTENSION_DEV_WORKSPACE_ID",
    "EXTENSION_DEV_PERMSET_ID",
    "EXTENSION_DEV_PERMSETGROUP_ID",
}


class BootstrapError(Exception):
    """An error whose message is safe to print without secrets or response bodies."""


class ApiStatusError(BootstrapError):
    def __init__(self, operation: str, status: int):
        self.status = status
        super().__init__(f"{operation} failed (HTTP {status}); no response body was logged.")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a password or bearer credential through a redirect.
        return None


class PrivateEnv:
    def __init__(self, path: Path):
        self.path = path
        if path.is_symlink() or not path.is_file():
            raise BootstrapError("The DevKit directory must contain a regular private .env file.")

    def values(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            # Match the official line-based getenv helper: first exact key wins.
            result.setdefault(key, value)
        return result

    def text(self) -> str:
        with self.path.open(encoding="utf-8", newline="") as handle:
            return handle.read()

    def save(self, key: str, value: str) -> None:
        if key not in PERSISTED_KEYS or not value or any(c in value for c in "\r\n\0"):
            raise BootstrapError("Refused an invalid local configuration update.")
        if self.path.is_symlink():
            raise BootstrapError("Refused to replace a symbolic-link configuration file.")
        original = self.text()
        lines = original.splitlines(keepends=True)
        found = False
        updated: list[str] = []
        for line in lines:
            if line.startswith(key + "="):
                ending = "\r\n" if line.endswith("\r\n") else "\n"
                updated.append(f"{key}={value}{ending}")
                found = True
            else:
                updated.append(line)
        if not found:
            if updated and not updated[-1].endswith(("\r", "\n")):
                updated[-1] += "\n"
            updated.append(f"{key}={value}\n")

        # Re-read on each update so unrelated changes made between API steps are preserved.
        # Write the token immediately after minting it; later failures can reuse it on retry.
        fd, temp_name = tempfile.mkstemp(prefix=".proofrun-bootstrap-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write("".join(updated))
                handle.flush()
                os.fsync(handle.fileno())
            if self.text() != original:
                raise BootstrapError("Local configuration changed during the update; retry after the other writer finishes.")
            os.replace(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


class StudioApi:
    def __init__(self, port: int):
        if not 1 <= port <= 65535:
            raise BootstrapError("STUDIO_PORT must be between 1 and 65535.")
        self.origin = f"http://127.0.0.1:{port}"
        # This helper is local-only; do not send local credentials through environment proxies.
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(
        self,
        method: str,
        route: str,
        operation: str,
        *,
        token: str | None = None,
        body: dict[str, Any] | None = None,
        json_response: bool = True,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if token:
            if any(c in token for c in "\r\n\0"):
                raise BootstrapError("The configured authentication token has an invalid format.")
            headers["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.origin + route, method=method, headers=headers, data=data)
        try:
            with self.opener.open(request, timeout=15) as response:
                if not 200 <= response.status < 300:
                    raise ApiStatusError(operation, response.status)
                if not json_response:
                    return None
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            error.close()
            raise ApiStatusError(operation, error.code) from None
        except (URLError, TimeoutError, OSError):
            raise BootstrapError(f"{operation} could not reach the local studio; check /healthz and retry.") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise BootstrapError(f"{operation} returned an oversized response.")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise BootstrapError(f"{operation} returned invalid JSON; no response body was logged.") from None


def unwrap(value: Any) -> Any:
    return value.get("data", value) if isinstance(value, dict) else value


def record(value: Any, operation: str) -> dict[str, Any]:
    result = unwrap(value)
    if not isinstance(result, dict):
        raise BootstrapError(f"{operation} returned an unexpected record format.")
    return result


def record_id(value: dict[str, Any], operation: str) -> str:
    identifier = value.get("id")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier):
        raise BootstrapError(f"{operation} did not return a valid record ID.")
    return identifier


def ensure_record(
    api: StudioApi,
    env: PrivateEnv,
    token: str,
    collection: str,
    key: str,
    name: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    route = f"/v1/aiservicedesk/admin/data/{collection}"
    saved_id = env.values().get(key)
    if saved_id and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", saved_id):
        try:
            current = record(api.request("GET", route + "/" + quote(saved_id, safe=""), f"Read {collection}", token=token), collection)
            if current.get("name") == name:
                env.save(key, record_id(current, collection))
                print(f"Reused {name}.")
                return current
        except ApiStatusError as error:
            if error.status != 404:
                raise

    items = unwrap(api.request("GET", route, f"List {collection}", token=token))
    if isinstance(items, dict):
        items = items.get("items")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise BootstrapError(f"List {collection} returned an unexpected format.")
    matches = [item for item in items if item.get("name") == name]
    if len(matches) > 1:
        raise BootstrapError(f"More than one {name} record exists; review the portal before retrying.")
    if matches:
        current = matches[0]
        action = "Reused"
    else:
        current = record(api.request("POST", route, f"Create {collection}", token=token, body=body), collection)
        action = "Created"
    identifier = record_id(current, collection)
    env.save(key, identifier)
    # List/create responses may be abbreviated. Check access against the full stored record.
    current = record(api.request("GET", route + "/" + quote(identifier, safe=""), f"Read {collection}", token=token), collection)
    if record_id(current, collection) != identifier or current.get("name") != name:
        raise BootstrapError(f"Read {collection} returned an inconsistent record identity.")
    print(f"{action} {name}.")
    return current


def bootstrap(devkit_dir: Path) -> None:
    env = PrivateEnv(devkit_dir / ".env")
    values = env.values()
    required = ("Authentication__LocalAdminEmail", "Authentication__LocalAdminPassword", "Licensing__Token")
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise BootstrapError("Complete licensed local portal configuration first; missing setting names: " + ", ".join(missing))
    try:
        port = int(values.get("STUDIO_PORT") or "60021")
    except ValueError:
        raise BootstrapError("STUDIO_PORT is not a valid integer.") from None
    api = StudioApi(port)
    api.request("GET", "/healthz", "Studio health check", json_response=False)

    token = values.get("DUPLO_ADMIN_TOKEN")
    if token:
        try:
            api.request("GET", "/v1/aiservicedesk/admin/extensions", "Validate admin API token", token=token, json_response=False)
        except ApiStatusError as error:
            if error.status not in (401, 403):
                raise
            token = None
    if token:
        print("Reused the existing admin API token.")
    else:
        login = record(api.request("POST", "/api/Account/PasswordLogin", "Portal login", body={
            "username": values["Authentication__LocalAdminEmail"],
            "password": values["Authentication__LocalAdminPassword"],
        }), "Portal login")
        session = login.get("access_token")
        if not isinstance(session, str) or not session:
            raise BootstrapError("Portal login did not return an access token.")
        minted = record(api.request("POST", "/v1/aiservicedesk/user/data/apitokens", "Create admin API token", token=session,
            body={"name": "dev-kit-admin", "expiresAt": None}), "Create admin API token")
        token = minted.get("plainToken")
        if not isinstance(token, str) or not token:
            raise BootstrapError("Token creation did not return a usable token; no response body was logged.")
        # Never revoke another token. A token-cap rejection is reported without destructive cleanup.
        env.save("DUPLO_ADMIN_TOKEN", token)
        print("Created and privately stored the admin API token.")

    workspace = ensure_record(api, env, token, "workspaces", "EXTENSION_DEV_WORKSPACE_ID", "extension-dev", {"name": "extension-dev"})
    workspace_id = record_id(workspace, "Workspace")
    permission = ensure_record(api, env, token, "permissionset", "EXTENSION_DEV_PERMSET_ID", "extension-dev-access", {
        "name": "extension-dev-access", "allowedWorkspaces": [{"workspaceId": workspace_id}],
    })
    allowed = permission.get("allowedWorkspaces")
    if not isinstance(allowed, list) or not any(isinstance(entry, dict) and entry.get("workspaceId") == workspace_id for entry in allowed):
        raise BootstrapError("The existing extension-dev-access record does not grant the expected workspace; review portal access settings.")
    permission_id = record_id(permission, "Permission set")
    email = values["Authentication__LocalAdminEmail"]
    group = ensure_record(api, env, token, "permissionsetgroup", "EXTENSION_DEV_PERMSETGROUP_ID", "extension-dev-group", {
        "name": "extension-dev-group", "permissionSets": [permission_id], "userStringHandle": [email],
    })
    members = group.get("userStringHandle")
    grants = group.get("permissionSets")
    if not isinstance(grants, list) or permission_id not in grants or not isinstance(members, list) or not any(isinstance(member, str) and member.casefold() == email.casefold() for member in members):
        raise BootstrapError("The existing extension-dev-group record does not grant this user's expected access; review portal access settings.")
    print("Local portal access initialized. Re-login if workspace access was previously cached.")
    print("This helper does not configure the model/agent or complete the full DevKit setup.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--devkit-dir", type=Path,
        default=Path(__file__).resolve().parents[4] / "proofrun-duplocloud-devkit",
        help="Official sibling DevKit checkout containing the existing private .env")
    args = parser.parse_args()
    try:
        bootstrap(args.devkit_dir.resolve())
    except BootstrapError as error:
        print(f"Portal bootstrap stopped: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("Portal bootstrap stopped: local configuration could not be read or saved.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
