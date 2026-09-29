"""Exercise installed SDK adapters without credentials or provider requests."""

import importlib.util
import os
import subprocess
import sys
import unittest


SDK_AVAILABLE = all(importlib.util.find_spec(name) for name in ("strands", "litellm", "cognee"))


@unittest.skipUnless(SDK_AVAILABLE, "Install requirements.txt for SDK adapter smoke tests")
class SDKIntegrationTests(unittest.TestCase):
    def run_isolated(self, code):
        # Cognee caches settings on import; each check needs a fresh process.
        env = dict(os.environ, LITELLM_LOCAL_MODEL_COST_MAP="True", TELEMETRY_DISABLED="true",
                   GROQ_API_KEY="fake-key-no-network")
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_strands_adapter_makes_one_completion(self):
        self.run_isolated('''
import json
from unittest.mock import AsyncMock, patch
from litellm import ModelResponse
from src.github_fetch import Commit, ChangedFile
from src.judge import judge_commit

reply = {"flag": "no", "reason": "The change includes relevant tests.", "criteria_hit": []}
completion = AsyncMock(return_value=ModelResponse(
    model="groq/openai/gpt-oss-120b",
    choices=[{"finish_reason": "stop", "index": 0,
              "message": {"role": "assistant", "content": json.dumps(reply)}}],
    usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
))
commit = Commit("a" * 40, "Ada", "2026-09-21T00:00:00Z", "fix: validate time range",
                [ChangedFile("src/parser.py", "modified", 1, 1)], 1, 1, ["b" * 40])
with patch("litellm.acompletion", completion):
    result = judge_commit(commit, "a diff", "A baseline", ["tests/test_parser.py"],
                          "fake-key-no-network")
assert result.model_dump() == dict(reply, dependency_notes=[])
assert completion.await_count == 1
args = completion.await_args.kwargs
assert args["model"] == "groq/openai/gpt-oss-120b"
assert args["response_format"] == {"type": "json_object"}
assert args["stream"] is False
assert args["num_retries"] == args["max_retries"] == 0
''')

    def test_real_cognee_adapter_uses_groq_native_schema_and_local_embeddings(self):
        self.run_isolated('''
import asyncio
import json
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, patch
from src.memory import Memory

with tempfile.TemporaryDirectory(prefix="commit-watch-sdk-") as temp:
    Memory("Eman-Gon/fp-multimodel", Path(temp))._cognee(require_key=True)
    from cognee.infrastructure.llm.config import get_llm_context_config
    from cognee.infrastructure.llm.structured_output_framework.litellm_native.get_native_client import get_native_client
    from cognee.infrastructure.databases.vector.embeddings.config import get_embedding_context_config, resolve_embedding_defaults
    from litellm import ModelResponse
    from pydantic import BaseModel

    class Extracted(BaseModel):
        summary: str

    cfg = get_llm_context_config()
    assert cfg.llm_provider == "custom"
    assert cfg.structured_output_framework == "litellm_native"
    assert resolve_embedding_defaults(get_embedding_context_config(), cfg) == (
        "fastembed", "BAAI/bge-small-en-v1.5", 384,
    )
    client = get_native_client()
    completion = AsyncMock(return_value=ModelResponse(
        model=client.model,
        choices=[{"finish_reason": "stop", "index": 0,
                  "message": {"role": "assistant", "content": json.dumps({"summary": "Baseline"})}}],
    ))
    with patch("litellm.acompletion", completion):
        result = asyncio.run(client.acreate_structured_output("commit", "Extract baseline", Extracted))
    assert result.summary == "Baseline"
    assert completion.await_count == 1
    args = completion.await_args.kwargs
    assert args["model"] == "groq/openai/gpt-oss-120b"
    assert args["response_format"] is Extracted
    assert args["messages"][0]["content"] == "Extract baseline"
''')
