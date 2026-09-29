"""Small, explicit configuration shared by the two CLI commands."""

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv

from .github_fetch import validate_repo


@dataclass(frozen=True)
class Settings:
    repo: str
    github_token: str
    groq_key: str
    state_dir: Path
    sandbox_image: str
    test_command: str
    test_timeout: int
    baseline_count: int
    brightdata_key: str = ""

    @classmethod
    def load(cls):
        load_dotenv(Path.cwd() / ".env", override=False)
        repo = os.getenv("TARGET_REPO", "").strip()
        if not repo or repo == "owner/name":
            raise ValueError("Set TARGET_REPO=owner/name in .env first.")
        validate_repo(repo)
        key = os.getenv("GROQ_API_KEY", "").strip()
        if not key:
            raise ValueError("Set GROQ_API_KEY in .env first; get a key at https://console.groq.com/keys.")
        timeout = int(os.getenv("TEST_TIMEOUT", "60"))
        count = int(os.getenv("BASELINE_COUNT", "40"))
        if not 1 <= timeout <= 60:
            raise ValueError("TEST_TIMEOUT must be between 1 and 60 seconds.")
        if not 30 <= count <= 50:
            raise ValueError("BASELINE_COUNT must be between 30 and 50.")
        image = os.getenv("SANDBOX_IMAGE", "commit-watch-sandbox:latest").strip()
        command = os.getenv("TEST_COMMAND", "python -m pytest -q -p no:cacheprovider").strip()
        if not image or image.startswith("-") or not command:
            raise ValueError("SANDBOX_IMAGE and TEST_COMMAND must be nonempty and valid.")
        return cls(
            repo=repo, github_token=os.getenv("GITHUB_TOKEN", "").strip(), groq_key=key,
            state_dir=Path.cwd() / ".commit-watch", sandbox_image=image,
            test_command=command, test_timeout=timeout, baseline_count=count,
            brightdata_key=os.getenv("BRIGHTDATA_API_KEY", "").strip(),
        )
