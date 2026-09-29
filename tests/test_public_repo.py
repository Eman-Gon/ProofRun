"""Network-free public inspection safety and detection checks."""

import io
import json
import tarfile
from threading import Event
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from src import public_repo as public

SHA = "a" * 40


def archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        for path, content in entries.items():
            info = tarfile.TarInfo(path)
            if isinstance(content, tuple):
                info.type, info.linkname = content
                tar.addfile(info)
            else:
                data = content.encode() if isinstance(content, str) else content
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return output.getvalue()


def read(entries):
    warnings = []
    files = public._archive_files(archive(entries), public._Budget(), warnings)
    return files, warnings


def scan(files):
    warnings = []
    dependencies, findings = public._scan_files(files, public._Budget(), warnings)
    return dependencies, findings, warnings


class PublicRepoTests(unittest.TestCase):
    def test_repository_listing_paginates_and_returns_only_safe_public_metadata(self):
        entries = [
            {"full_name": "Owner/a", "private": False, "description": "hello\nworld", "html_url": "https://evil.example"},
            {"full_name": "Owner/b", "private": False, "description": None},
            {"full_name": "Owner/c", "private": False, "description": "c"},
        ]
        with patch.object(public, "REPOSITORY_PAGE_SIZE", 2), patch.object(public, "_fetch", side_effect=[
                json.dumps(entries[:2]).encode(), json.dumps(entries[2:]).encode()]) as fetch:
            result = public.list_public_repositories(" owner ")
        self.assertEqual(result["owner"], "owner")
        self.assertEqual([row["fullName"] for row in result["repositories"]], ["Owner/a", "Owner/b", "Owner/c"])
        self.assertEqual(result["repositories"][0], {"fullName": "Owner/a", "url": "https://github.com/Owner/a", "description": "helloworld"})
        self.assertFalse(result["truncated"])
        self.assertIsNone(result["warning"])
        self.assertTrue(fetch.call_args_list[0].args[0].endswith("per_page=2&page=1"))
        self.assertTrue(fetch.call_args_list[1].args[0].endswith("per_page=2&page=2"))
        self.assertLessEqual(fetch.call_args.args[2].timeout(), public.REPOSITORY_LIST_SECONDS)

    def test_repository_listing_probes_beyond_cap_for_truthful_truncation(self):
        entries = [{"full_name": f"owner/repo{i}", "private": False} for i in range(5)]
        for extra, truncated in (([], False), (entries[4:], True)):
            with self.subTest(truncated=truncated), patch.object(public, "REPOSITORY_PAGE_SIZE", 2), \
                    patch.object(public, "MAX_REPOSITORIES", 4), patch.object(public, "_fetch", side_effect=[
                        json.dumps(entries[:2]).encode(), json.dumps(entries[2:4]).encode(), json.dumps(extra).encode()]) as fetch:
                result = public.list_public_repositories("owner")
            self.assertEqual(len(result["repositories"]), 4)
            self.assertEqual(result["truncated"], truncated)
            self.assertEqual(bool(result["warning"]), truncated)
            self.assertEqual(fetch.call_count, 3)

    def test_repository_listing_empty_and_failed_pagination_are_distinct(self):
        with patch.object(public, "_fetch", return_value=b"[]"):
            self.assertEqual(public.list_public_repositories("owner")["repositories"], [])
        with patch.object(public, "REPOSITORY_PAGE_SIZE", 1), patch.object(public, "_fetch", side_effect=[
                b'[{"full_name":"owner/repo","private":false}]', public.PublicRepoError("rate limit")]):
            with self.assertRaisesRegex(public.PublicRepoError, "rate limit"):
                public.list_public_repositories("owner")

    def test_repository_listing_rejects_bad_owner_before_fetch(self):
        with patch.object(public, "_fetch") as fetch:
            for owner in (None, "", "a/b", "https://github.com/owner", "-owner", "owner-", "x" * 40, "a?token=x"):
                with self.subTest(owner=owner), self.assertRaises(public.PublicRepoError):
                    public.list_public_repositories(owner)
            fetch.assert_not_called()

    def test_repository_listing_rejects_private_or_malformed_remote_data(self):
        responses = [b"{}", b"invalid", b"[null]", b'[{"full_name":"owner/private","private":true}]',
                     b'[{"full_name":"owner/repo","private":0}]', b'[{"full_name":"other/repo","private":false}]',
                     b'[{"full_name":"owner/repo/extra","private":false}]', b"[" * 1500 + b"0" + b"]" * 1500]
        for body in responses:
            with self.subTest(body=body[:80]), patch.object(public, "_fetch", return_value=body):
                with self.assertRaisesRegex(public.PublicRepoError, "unreadable repository listings"):
                    public.list_public_repositories("owner")

    def test_repository_listing_uses_specific_timeout_and_account_not_found_errors(self):
        with patch.object(public, "REPOSITORY_LIST_SECONDS", -1), self.assertRaisesRegex(public.PublicRepoError, "Loading repositories exceeded"):
            public.list_public_repositories("owner")
        class Opener:
            def open(self, *args, **kwargs):
                raise HTTPError("https://api.github.com/users/missing/repos", 404, "unsafe-body", {}, None)
        with patch.object(public, "build_opener", return_value=Opener()):
            with self.assertRaisesRegex(public.PublicRepoError, "GitHub account not found"):
                public.list_public_repositories("missing")

    def test_normalize(self):
        for value in ["Eman-Gon/repo", "https://github.com/Eman-Gon/repo", "https://github.com/Eman-Gon/repo.git/", " Eman-Gon/repo.git "]:
            with self.subTest(value=value):
                self.assertEqual(public.normalize_repository(value), "Eman-Gon/repo")

    def test_reject_invalid_repository(self):
        for value in ["https://github.com/u/r/tree/main", "https://github.com/user:pass@github.com/u/r", "http://github.com/u/r", "https://evil.test/u/r", "https://github.com:443/u/r", "https://github.com/u/r?token=x", "https://github.com/u/r#main", "https://github.com/u/r?", "u/..", "u/r/", "u/r\n/x", "https://github.com/u/%2e%2e", "u/r.git/", "https://[invalid", None]:
            with self.subTest(value=value), self.assertRaises(public.PublicRepoError):
                public.normalize_repository(value)

    def test_archive_ignores_paths_links_generated_and_binary(self):
        files, warnings = read({
            "snapshot/src/main.py": "import pandas as pd\n",
            "snapshot/../outside.py": "secret = 1",
            "/outside.py": "secret = 1",
            "snapshot/link.py": (tarfile.SYMTYPE, "/etc/passwd"),
            "snapshot/hard.py": (tarfile.LNKTYPE, "snapshot/src/main.py"),
            "snapshot/node_modules/pkg/index.js": "bad()",
            "snapshot/.env": "SECRET=never-read",
            "snapshot/binary.py": b"\x00\xff",
            "other/foreign.py": "bad()",
        })
        self.assertEqual(files, {"src/main.py": "import pandas as pd\n"})
        self.assertTrue(any("Unsafe" in warning for warning in warnings))
        self.assertTrue(any("links" in warning for warning in warnings))

    def test_regular_directory_entry_does_not_trigger_unsafe_warning(self):
        files, warnings = read({"snapshot/": (tarfile.DIRTYPE, ""), "snapshot/main.py": "x = 1"})
        self.assertEqual(files, {"main.py": "x = 1"})
        self.assertFalse(warnings)

    def test_source_size_and_file_count_caps(self):
        with patch.object(public, "MAX_SOURCE_BYTES", 20), patch.object(public, "MAX_FILES", 1):
            files, warnings = read({"snapshot/big.py": "x" * 21, "snapshot/a.py": "x=1", "snapshot/b.py": "x=2"})
        self.assertEqual(files, {"a.py": "x=1"})
        self.assertEqual(len(warnings), 2)

    def test_total_text_and_member_caps(self):
        with patch.object(public, "MAX_TEXT_BYTES", 4):
            files, warnings = read({"snapshot/a.py": "x=1", "snapshot/b.py": "x=2"})
        self.assertEqual(len(files), 1)
        self.assertTrue(warnings)
        with patch.object(public, "MAX_MEMBERS", 1):
            files, warnings = read({"snapshot/a.py": "x=1", "snapshot/b.py": "x=2"})
        self.assertEqual(len(files), 1)
        self.assertTrue(any("entry limit" in warning for warning in warnings))

    def test_expansion_bomb_is_bounded(self):
        with patch.object(public, "MAX_EXPANDED_BYTES", 1024), self.assertRaisesRegex(public.PublicRepoError, "Expanded"):
            read({"snapshot/large.bin": b"0" * 20_000})

    def test_archive_download_cap_and_invalid_tar(self):
        with patch.object(public, "MAX_ARCHIVE_BYTES", 1), self.assertRaisesRegex(public.PublicRepoError, "size limit"):
            read({"snapshot/a.py": "x=1"})
        with self.assertRaisesRegex(public.PublicRepoError, "unreadable"):
            public._archive_files(b"not gzip", public._Budget(), [])

    def test_cancellation_and_deadline(self):
        event = Event()
        event.set()
        with self.assertRaisesRegex(public.PublicRepoError, "cancelled"):
            public._Budget(event).check()
        budget = public._Budget()
        budget.deadline = 0
        with self.assertRaisesRegex(public.PublicRepoError, "time limit"):
            budget.check()

    def test_manifest_versions_are_declared_specs_and_urls_omitted(self):
        dependencies, findings, warnings = scan({
            "requirements.txt": "pandas>=2; python_version >= '3.10'\npydantic==1.10.18\nprivate @ https://name:password@example.com/pkg.whl\n",
            "web/package.json": '{"version":"999", "dependencies":{"web-vitals":"^2.1.0","local":"file:../local"}}',
            "pyproject.toml": '[project]\ndependencies = ["httpx>=0.27"]\n',
        })
        self.assertEqual({row["name"] for row in dependencies}, {"pandas", "pydantic", "web-vitals", "httpx"})
        pandas = next(row for row in dependencies if row["name"] == "pandas")
        self.assertIn("python_version", pandas["version"])
        self.assertNotIn("password", json.dumps(dependencies))
        self.assertFalse(findings or warnings)

    def test_unsupported_and_malformed_manifests_are_explicit(self):
        files, warnings = read({"snapshot/yarn.lock": "opaque", "snapshot/package-lock.json": "{}"})
        self.assertEqual(files, {})
        self.assertTrue(any("Lockfiles" in warning for warning in warnings))
        deps, _, warnings = scan({"package.json": "broken", "pyproject.toml": '[tool.poetry.dependencies]\npython = "^3.11"\npandas = "*"\n'})
        self.assertFalse(deps)
        self.assertEqual(len(warnings), 2)

    def test_pandas_calls_require_real_import_not_name_or_comment(self):
        _, findings, _ = scan({"app.py": '''import pandas as pd
from pandas import timedelta_range as tr
pd.date_range("2024-01-01", periods=2, freq="H")
tr(start="0h", periods=2, freq="2H")
pd.date_range("2024-01-01", periods=2, freq="h")
# pd.date_range(freq="H")
''', "other.py": 'pd.date_range(freq="H")\n'})
        self.assertEqual([finding["line"] for finding in findings], [3, 4])
        self.assertEqual([finding["afterCode"] for finding in findings], ["freq='h'", "freq='2h'"])
        self.assertTrue(all(finding["status"] == "static_unverified" for finding in findings))

    def test_shadowed_pandas_alias_is_suppressed(self):
        for shadow in ["pd = object()", "def f(pd): pass", "import unrelated as pd", "def f():\n import unrelated as pd"]:
            with self.subTest(shadow=shadow):
                _, findings, _ = scan({"app.py": f'import pandas as pd\n{shadow}\npd.date_range(freq="H")\n'})
                self.assertFalse(findings)

    def test_pydantic_nullable_fields_require_actual_base_and_no_default(self):
        _, findings, _ = scan({"models.py": '''from pydantic import BaseModel as Model
from pydantic.v1 import BaseModel as OldModel
from typing import Optional, ClassVar
class Person(Model):
    nickname: Optional[str]
    middle: str | None
    explicit: Optional[str] = None
    required: Optional[str] = ...
    members: list[Optional[str]]
    constant: ClassVar[Optional[str]]
class Unrelated:
    nickname: Optional[str]
class Old(OldModel):
    nickname: Optional[str]
'''})
        self.assertEqual([finding["line"] for finding in findings], [5, 6])
        self.assertTrue(all(finding["afterCode"].endswith(" = None") for finding in findings))
        self.assertTrue(all("intentional" in finding["explanation"] for finding in findings))

    def test_javascript_explicit_imports_dynamic_require_and_comments(self):
        _, findings, _ = scan({"report.js": '''// import { getCLS } from 'web-vitals';
const example = "import { getLCP } from 'web-vitals'";
import { onCLS as getCLS } from 'web-vitals';
import { getFCP as measure } from 'web-vitals';
const { getFID } = require('web-vitals');
import('web-vitals').then(({getCLS, getTTFB}) => getCLS(console.log));
import { getLCP } from 'other-library';
'''})
        self.assertEqual([finding["line"] for finding in findings], [4, 5, 6])
        self.assertTrue(all(finding["afterCode"] == "" for finding in findings))
        self.assertTrue(all("not a blind rename" in finding["explanation"] for finding in findings))

    def test_bad_python_is_skipped_and_output_limits_apply(self):
        with patch.object(public, "MAX_DEPENDENCIES", 1), patch.object(public, "MAX_FINDINGS", 1):
            deps, findings, warnings = scan({"requirements.txt": "pandas\npydantic", "bad.py": "def broken(:", "app.py": 'import pandas as pd\npd.date_range(freq="H")\npd.date_range(freq="H")'})
        self.assertEqual(len(deps), 1)
        self.assertEqual(len(findings), 1)
        self.assertEqual(len(warnings), 3)

    def test_inspection_is_public_and_commit_pinned(self):
        urls, messages = [], []
        source = archive({"snapshot/app.py": 'import pandas as pd\npd.date_range(freq="H")'})
        def fetch(url, limit, budget):
            urls.append(url)
            if len(urls) == 1:
                return json.dumps({"private": False, "full_name": "Owner/Repo", "default_branch": "feature/main"}).encode()
            if len(urls) == 2:
                return json.dumps({"sha": SHA}).encode()
            return source
        with patch.object(public, "_fetch", fetch):
            result = public.inspect_public_repo("owner/repo", emit=messages.append)
        self.assertTrue(urls[1].endswith("/commits/feature%2Fmain"))
        self.assertEqual(urls[2], f"https://codeload.github.com/Owner/Repo/tar.gz/{SHA}")
        self.assertEqual(result["commit"], SHA)
        self.assertEqual(result["filesScanned"], 1)
        self.assertEqual(result["findings"][0]["status"], "static_unverified")
        self.assertIn("No findings does not establish", result["scope"])
        self.assertEqual(len(messages), 3)

    def test_private_repo_never_downloaded(self):
        with patch.object(public, "_json", return_value={"private": True}), patch.object(public, "_fetch", side_effect=AssertionError("must not download")):
            with self.assertRaisesRegex(public.PublicRepoError, "publicly visible"):
                public.inspect_public_repo("owner/repo")

    def test_redirect_and_request_host_restrictions(self):
        for url in ["http://api.github.com/x", "https://evil.test/x", "https://api.github.com.evil.test/x", "https://token@api.github.com/x", "https://api.github.com:443/x", "https://127.0.0.1/x", "https://[invalid"]:
            with self.subTest(url=url), self.assertRaises(public.PublicRepoError):
                public._safe_url(url)

    def test_http_errors_are_actionable_without_echoing_remote_body(self):
        class Opener:
            def open(self, *args, **kwargs):
                raise HTTPError("https://api.github.com/repos/o/r", 403, "unsafe-body", {}, None)
        with patch.object(public, "build_opener", return_value=Opener()):
            with self.assertRaisesRegex(public.PublicRepoError, "rate limit") as error:
                public._fetch("https://api.github.com/repos/o/r", 20, public._Budget())
        self.assertNotIn("unsafe-body", str(error.exception))

    def test_fetch_uses_bounded_single_reads_without_auth_or_proxy(self):
        observed = {}
        class Response:
            headers = {}
            fp = None
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def geturl(self): return "https://api.github.com/repos/o/r"
            def read(self, size): raise AssertionError("must use read1")
            def read1(self, size):
                observed["size"] = size
                return b""
        class Opener:
            def open(self, request, timeout):
                observed["headers"] = dict(request.header_items())
                observed["timeout"] = timeout
                return Response()
        def opener(*handlers):
            observed["proxies"] = handlers[0].proxies
            return Opener()
        with patch.object(public, "build_opener", opener):
            self.assertEqual(public._fetch("https://api.github.com/repos/o/r", 20, public._Budget()), b"")
        self.assertEqual(observed["proxies"], {})
        self.assertFalse(any(key.lower() == "authorization" for key in observed["headers"]))
        self.assertLessEqual(observed["timeout"], 15)
        self.assertLessEqual(observed["size"], 21)
