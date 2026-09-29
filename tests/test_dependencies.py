import json
import unittest

from src.dependencies import DependencyParseError, MAX_DEPENDENCY_BYTES, changed_dependencies, is_dependency_file


class DependencyTests(unittest.TestCase):
    def test_recognizes_only_dependency_filenames(self):
        for path in ("frontend/package.json", "npm-shrinkwrap.json", "requirements-dev.txt",
                     "backend/requirements/base.txt", "pyproject.toml", "poetry.lock", "uv.lock",
                     "Pipfile.lock", "pnpm-lock.yaml", "yarn.lock"):
            self.assertTrue(is_dependency_file(path), path)
        for path in ("README.md", "package-notes.json", "requirements.md", "src/version.py"):
            self.assertFalse(is_dependency_file(path), path)

    def test_npm_ignores_app_version_scripts_and_order(self):
        before = json.dumps({"name": "my-app", "version": "1", "scripts": {"test": "a"},
                             "dependencies": {"react": "^19"}, "devDependencies": {"react": "^19"}})
        after = json.dumps({"name": "new-app", "version": "2", "scripts": {"test": "b"},
                            "peerDependencies": {"react": "^19"}})
        self.assertEqual(changed_dependencies("package.json", before, after), [])

    def test_added_removed_and_changed_specs(self):
        before = '{"dependencies":{"react":"^18","gone":"1"}}'
        after = '{"dependencies":{"react":"^19","new":"2"},"devDependencies":{"react":"^19"}}'
        self.assertEqual(changed_dependencies("package.json", before, after), [
            {"ecosystem": "npm", "package": "gone", "before": "1", "version": None},
            {"ecosystem": "npm", "package": "new", "before": None, "version": "2"},
            {"ecosystem": "npm", "package": "react", "before": "^18", "version": "^19"},
        ])

    def test_npm_lock_transitive_changes_all_versions(self):
        for version in (1, 2, 3):
            def lock(v):
                entry = {"version": v, "resolved": "https://user:secret@registry.example/x.tgz"}
                return json.dumps({"lockfileVersion": version, **(
                    {"dependencies": {"parent": {"version": "1", "dependencies": {"child": entry}}}}
                    if version == 1 else {"packages": {"": {"version": "9"},
                        "node_modules/parent": {"version": "1"},
                        "node_modules/parent/node_modules/child": entry}})})
            changes = changed_dependencies("package-lock.json", lock("1.0.0"), lock("1.0.1"))
            self.assertEqual(changes, [{"ecosystem": "npm", "package": "child", "before": "1.0.0", "version": "1.0.1"}])
            self.assertNotIn("secret", str(changes))

    def test_requirements_normalization_markers_extras_and_hashes(self):
        before = 'Requests[security]>=2; python_version < "3.12"\n'
        after = 'requests[security]>=2; python_version < "3.13" ' + '\\\n' + '    --hash=sha256:123 # locked\n'
        change, = changed_dependencies("requirements/base.txt", before, after)
        self.assertEqual(change["package"], "requests")
        self.assertEqual(change["before"], '[security]>=2; python_version < "3.12"')
        self.assertEqual(change["version"], '[security]>=2; python_version < "3.13"')
        self.assertEqual(changed_dependencies("requirements.txt", "Some_Pkg==1\n", "some-pkg==1\n"), [])
        self.assertEqual(changed_dependencies("requirements.txt", "foo[DEV_TEST]==1\n", "foo[dev-test]==1\n"), [])

    def test_registry_specs_aggregate_across_markers(self):
        before = 'foo==1; python_version < "3.12"\nfoo==2; python_version >= "3.12"\n'
        after = before.replace("foo==2", "foo==3")
        change, = changed_dependencies("requirements.txt", before, after)
        self.assertIn('==1; python_version < "3.12"', change["version"])
        self.assertIn('==3; python_version >= "3.12"', change["version"])

    def test_pep621_optional_dependencies_only(self):
        before = '[project]\nname="app"\nversion="1"\ndependencies=["foo>=1"]\n[project.optional-dependencies]\ntest=["pytest==8"]\n'
        self.assertEqual(changed_dependencies("pyproject.toml", before, before.replace('version="1"', 'version="2"')), [])
        change, = changed_dependencies("pyproject.toml", before, before.replace("pytest==8", "pytest==9"))
        self.assertEqual((change["package"], change["before"], change["version"]), ("pytest", "==8", "==9"))

    def test_python_locks_include_transitive_versions_and_skip_local_sources(self):
        for path in ("uv.lock", "poetry.lock"):
            before = '[[package]]\nname="Some_Pkg"\nversion="1.0"\n[[package]]\nname="my-app"\nversion="0.1"\n[package.source]\neditable="."\n'
            change, = changed_dependencies(path, before, before.replace('version="1.0"', 'version="1.1"'))
            self.assertEqual(change, {"ecosystem": "pypi", "package": "some-pkg", "before": "==1.0", "version": "==1.1"})

    def test_pipfile_lock_preserves_marker_and_extras(self):
        data = json.dumps({"default": {"Requests": {"version": "==2", "markers": "python_version >= '3.10'", "extras": ["security"]}}})
        change, = changed_dependencies("Pipfile.lock", None, data)
        self.assertEqual(change["version"], '[security]==2; python_version >= "3.10"')

    def test_uv_lock_resolution_markers_preserved(self):
        text = '[[package]]\nname="foo"\nversion="1"\nresolution-markers=["python_version < \'3.12\'"]\n'
        change, = changed_dependencies("uv.lock", None, text)
        self.assertEqual(change["version"], '==1; python_version < "3.12"')

    def test_no_external_addresses_or_local_paths_are_exported(self):
        npm = json.dumps({"dependencies": {"a": "file:../private", "b": "git+https://user:secret@host/repo",
                                         "c": "https://user:secret@host/pkg.tgz", "d": "workspace:*"}})
        self.assertEqual(changed_dependencies("package.json", None, npm), [])
        python = 'foo @ https://user:secret@host/a.whl\nbar @ file:///private/bar\n-e ../local\n--index-url https://user:secret@registry\n-r other.txt\nprivate-1-py3-none-any.whl\n'
        self.assertEqual(changed_dependencies("requirements.txt", None, python), [])
        lock = json.dumps({"lockfileVersion": 3, "packages": {
            "": {"dependencies": {"foo": "https://user:secret@host/foo.tgz"}},
            "node_modules/foo": {"version": "1", "resolved": "https://user:secret@host/foo.tgz"},
        }})
        self.assertEqual(changed_dependencies("package-lock.json", None, lock), [])

    def test_npm_alias_uses_actual_registry_package(self):
        change, = changed_dependencies("package.json", None, '{"dependencies":{"alias":"npm:@scope/actual@^2"}}')
        self.assertEqual((change["package"], change["version"]), ("@scope/actual", "^2"))

    def test_unsupported_malformed_and_oversized_fail_explicitly(self):
        for path, text in (("yarn.lock", ""), ("pnpm-lock.yaml", ""), ("package.json", "{"),
                           ("package.json", '{"dependencies": []}'),
                           ("package.json", '{"dependencies":{"foo":"1","foo":"2"}}'),
                           ("pyproject.toml", "[invalid"), ("requirements.txt", "not a dependency ???"),
                           ("uv.lock", '[[package]]\nname="foo"'),
                           ("uv.lock", ""), ("Pipfile.lock", "{}"),
                           ("requirements.txt", "\ud800"),
                           ("requirements.txt", "x" * (MAX_DEPENDENCY_BYTES + 1))):
            with self.subTest(path=path), self.assertRaises(DependencyParseError):
                changed_dependencies(path, None, text)


if __name__ == "__main__":
    unittest.main()
