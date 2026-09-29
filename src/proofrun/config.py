"""Explicit server configuration; secrets never participate in repr or artifacts.

Environment loading is intentional: entry points may load an ignored .env, but
importing this module never scans files or mutates the process environment.
"""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Mapping


class ConfigurationError(ValueError):
    """Safe configuration error, containing setting names but never values."""


def _value(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not isinstance(value, str) or any(c in value for c in "\r\n\x00"):
        raise ConfigurationError(f"{name} must be a single-line string.")
    return value.strip()


@dataclass(frozen=True)
class RepairConfig:
    api_key: str = field(repr=False)
    model: str
    timeout_seconds: int = 45
    max_tokens: int = 4096

    def __post_init__(self):
        if (not isinstance(self.api_key, str) or not self.api_key or len(self.api_key) > 512
                or not self.api_key.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in self.api_key)):
            raise ConfigurationError("Set OPENROUTER_API_KEY in server-side secret configuration.")
        if (not isinstance(self.model, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.:/-]{0,180}", self.model)
                or self.model in {"openrouter/auto", "openrouter/free"}):
            raise ConfigurationError("Set PROOFRUN_MODEL to an explicit OpenRouter model ID.")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 60:
            raise ConfigurationError("Repair timeout must be an integer from 1 to 60 seconds.")
        if type(self.max_tokens) is not int or not 256 <= self.max_tokens <= 4096:
            raise ConfigurationError("Repair output token limit must be from 256 to 4096.")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None):
        env = os.environ if environ is None else environ
        return cls(api_key=_value(env, "OPENROUTER_API_KEY"), model=_value(env, "PROOFRUN_MODEL"))


@dataclass(frozen=True)
class WorkerConfig:
    token: str = field(repr=False)
    artifact_dir: Path
    execution_target: str = "local"
    worker_id: str = "local-worker"

    def __post_init__(self):
        if (not isinstance(self.token, str) or len(self.token) < 32 or len(self.token) > 512
                or not self.token.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in self.token)):
            raise ConfigurationError("PROOFRUN_WORKER_TOKEN must be a secret of 32 to 512 characters without whitespace.")
        if not isinstance(self.execution_target, str) or self.execution_target not in {"local", "crusoe"}:
            raise ConfigurationError("PROOFRUN_EXECUTION_TARGET must be local or crusoe.")
        if not isinstance(self.worker_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", self.worker_id):
            raise ConfigurationError("PROOFRUN_WORKER_ID must be a safe host identifier.")
        if self.execution_target == "crusoe" and self.worker_id == "local-worker":
            raise ConfigurationError("Set PROOFRUN_WORKER_ID to the recorded Crusoe VM identifier.")
        object.__setattr__(self, "artifact_dir", Path(self.artifact_dir).expanduser().resolve())

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None):
        env = os.environ if environ is None else environ
        return cls(
            token=_value(env, "PROOFRUN_WORKER_TOKEN"),
            artifact_dir=Path(_value(env, "PROOFRUN_ARTIFACT_DIR") or ".commit-watch/proofrun"),
            execution_target=_value(env, "PROOFRUN_EXECUTION_TARGET") or "local",
            worker_id=_value(env, "PROOFRUN_WORKER_ID") or "local-worker",
        )
