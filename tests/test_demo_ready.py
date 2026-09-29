"""Curated demos load durable evidence without inventing execution results."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.demo_ready import load_demo_ready


REPOSITORIES = (
    ("gpu-energy-pandas", "Eman-Gon/gpu-energy-recommender", "gpu-energy-recommender.json"),
    ("scam-killer", "Eman-Gon/scam_killer", "scam-killer.json"),
    ("gauntlet", "Eman-Gon/Gauntlet", "gauntlet.json"),
    ("agent-with-a-brain", "sandhya-subramani/Agent-with-a-Brain", "agent-with-a-brain.json"),
    ("flask", "pallets/flask", "flask.json"),
)
SHA = "7a802eace3a3775acc5ea4af6c266079e2599eb3"


def evidence(repository, **changes):
    result = {"repository": repository, "repoUrl": "https://github.com/" + repository,
              "commit": SHA, "checkedAt": "2026-09-22T02:56:41.147495+00:00",
              "filesScanned": 4, "dependencies": [], "findings": [], "warnings": [],
              "scope": "Static, unverified source inspection. No code or tests were run.",
              "provenance": "Saved repository source snapshot."}
    result.update(changes)
    return result


def finding(**changes):
    row = {"id": "pandas-hour:app.py:1", "status": "static_unverified", "line": 1,
           "title": "Hourly alias", "package": "pandas", "file": "app.py",
           "explanation": "Review uppercase hour alias.", "sourceUrl": "https://pandas.pydata.org/",
           "beforeCode": "freq='H'", "afterCode": "freq='h'"}
    row.update(changes)
    return row


class DemoReadyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ready = self.root / "demo" / "ready"
        self.ready.mkdir(parents=True)
        for _, repository, filename in REPOSITORIES:
            self.write(filename, evidence(repository))

    def write(self, filename, result):
        (self.ready / filename).write_text(json.dumps(result), encoding="utf-8")

    def test_five_fixed_cards_survive_reload_without_session_history_or_network(self):
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Unexpected network")):
            first = load_demo_ready(self.root, [])
            second = load_demo_ready(Path(str(self.root)), [])
        self.assertEqual(first, second)
        self.assertEqual([entry["id"] for entry in first], [row[0] for row in REPOSITORIES])
        self.assertEqual(len(first), 5)
        for entry, (_, repository, _) in zip(first, REPOSITORIES):
            with self.subTest(repository=repository):
                self.assertTrue(entry["available"])
                self.assertEqual(entry["repository"], repository)
                self.assertEqual(entry["title"], repository.split("/")[1])
                self.assertIsNone(entry["caseId"])
                self.assertIsNone(entry["unavailableReason"])
                scan = entry["scan"]
                self.assertEqual(scan["id"], "demo-" + entry["id"])
                self.assertEqual(scan["status"], "completed")
                self.assertEqual(scan["result"]["commit"], SHA)
                self.assertEqual(scan["startedAt"], scan["finishedAt"])
                self.assertEqual(scan["finishedAt"], scan["result"]["checkedAt"])
                self.assertEqual(scan["result"]["scope"], evidence(repository)["scope"])
                self.assertEqual(scan["result"]["provenance"], evidence(repository)["provenance"])
                self.assertEqual(scan["output"], "")
                self.assertIsNone(scan["error"])

    def test_confirmed_gpu_uses_existing_case_even_without_static_snapshot(self):
        (self.ready / REPOSITORIES[0][2]).unlink()
        cases = [{"id": "gpu-energy-pandas", "repository": REPOSITORIES[0][1],
                  "status": "confirmed_break", "scope": "Two repository functions."}]
        original = deepcopy(cases)
        entry = load_demo_ready(self.root, cases)[0]
        self.assertTrue(entry["available"])
        self.assertEqual(entry["evidenceLabel"], "Measured comparison")
        self.assertEqual(entry["caseId"], "gpu-energy-pandas")
        self.assertIn("verified fix", entry["summary"])
        self.assertIsNone(entry["scan"])
        self.assertEqual(cases, original)

    def test_inconclusive_or_unrelated_cases_fall_back_without_changing_the_case(self):
        for status, repository in (("inconclusive", REPOSITORIES[0][1]), ("not_run", REPOSITORIES[0][1]),
                                   ("confirmed_break", "wrong/repository")):
            with self.subTest(status=status, repository=repository):
                cases = [{"id": "gpu-energy-pandas", "status": status, "repository": repository}]
                original = deepcopy(cases)
                entry = load_demo_ready(self.root, cases)[0]
                self.assertTrue(entry["available"])
                self.assertEqual(entry["evidenceLabel"], "Saved source scan")
                self.assertIsNone(entry["caseId"])
                self.assertNotIn("verified fix", entry["summary"])
                self.assertEqual(cases, original)

    def test_missing_static_evidence_is_unavailable_and_never_measured(self):
        (self.ready / "gpu-energy-recommender.json").unlink()
        entry = load_demo_ready(self.root, [{"id": "gpu-energy-pandas", "status": "inconclusive"}])[0]
        self.assertFalse(entry["available"])
        self.assertIn("missing", entry["unavailableReason"])
        self.assertIsNone(entry["scan"])
        self.assertIsNone(entry["caseId"])

    def test_malformed_and_mismatched_evidence_remains_unavailable(self):
        bad = [None, [], {}, evidence("other/repository"), {**evidence(REPOSITORIES[1][1]), "repository": None},
               evidence(REPOSITORIES[1][1], commit="7a802ea"), evidence(REPOSITORIES[1][1], repoUrl=None),
               evidence(REPOSITORIES[1][1], checkedAt="2026-09-22"),
               evidence(REPOSITORIES[1][1], filesScanned=True), evidence(REPOSITORIES[1][1], filesScanned=0),
               evidence(REPOSITORIES[1][1], dependencies=[None]), evidence(REPOSITORIES[1][1], findings=[None]),
               evidence(REPOSITORIES[1][1], warnings="warning"), evidence(REPOSITORIES[1][1], scope="")]
        for value in bad:
            with self.subTest(value=value):
                self.write("scam-killer.json", value)
                entry = load_demo_ready(self.root, [])[1]
                self.assertFalse(entry["available"])
                self.assertIsNone(entry["scan"])
                self.assertTrue(entry["unavailableReason"])
        (self.ready / "scam-killer.json").write_text("{broken", encoding="utf-8")
        self.assertFalse(load_demo_ready(self.root, [])[1]["available"])

    def test_static_findings_cannot_claim_measured_or_verified_status(self):
        repository = REPOSITORIES[1][1]
        self.write("scam-killer.json", evidence(repository, findings=[finding()]))
        entry = load_demo_ready(self.root, [])[1]
        self.assertEqual(entry["evidenceLabel"], "Static finding")
        self.assertEqual(entry["scan"]["result"]["findings"][0]["status"], "static_unverified")
        self.assertIn("have not been reproduced", entry["scan"]["result"]["explanation"]["summary"])
        for changes in ({"findings": [finding(status="confirmed_break")]}, {"status": "verified_compatible"}):
            self.write("scam-killer.json", evidence(repository, **changes))
            self.assertFalse(load_demo_ready(self.root, [])[1]["available"])

    def test_static_explanation_is_refreshed_from_evidence(self):
        self.write("gauntlet.json", evidence(REPOSITORIES[2][1], explanation={"summary": "Verified compatible"}))
        explanation = load_demo_ready(self.root, [])[2]["scan"]["result"]["explanation"]
        self.assertNotIn("Verified compatible", str(explanation))
        self.assertIn("does not identify installed packages or establish upgrade compatibility", explanation["summary"])

    def test_specific_summaries_only_describe_patterns_in_loaded_evidence(self):
        repository = REPOSITORIES[1][1]
        self.write("scam-killer.json", evidence(repository, findings=[finding(id="web-vitals-exports:app.js:1", package="web-vitals")]))
        self.assertIn("Legacy web-vitals imports", load_demo_ready(self.root, [])[1]["summary"])
        self.write("scam-killer.json", evidence(repository))
        entries = load_demo_ready(self.root, [])
        self.assertNotIn("web-vitals", entries[1]["summary"])
        self.assertNotIn("Next.js", entries[2]["summary"])
        self.assertNotIn("Cognee", entries[3]["summary"])
        self.assertIn("compatibility is unverified", entries[1]["summary"])

    def test_oversized_file_is_unavailable(self):
        with patch("src.demo_ready.MAX_EVIDENCE_BYTES", 10):
            entry = load_demo_ready(self.root, [])[0]
        self.assertFalse(entry["available"])
        self.assertIn("size limit", entry["unavailableReason"])

    def test_symlinks_cannot_redirect_evidence_outside_ready_directory(self):
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(evidence(REPOSITORIES[1][1])), encoding="utf-8")
        path = self.ready / "scam-killer.json"
        path.unlink()
        path.symlink_to(outside)
        entry = load_demo_ready(self.root, [])[1]
        self.assertFalse(entry["available"])
        self.assertIsNone(entry["scan"])

    def test_ready_directory_symlink_is_rejected(self):
        moved = self.root / "elsewhere"
        self.ready.rename(moved)
        self.ready.symlink_to(moved, target_is_directory=True)
        entries = load_demo_ready(self.root, [])
        self.assertTrue(all(not entry["available"] for entry in entries))

    def test_checked_in_examples_have_complete_commit_and_honest_static_evidence(self):
        root = Path(__file__).resolve().parents[1]
        entries = load_demo_ready(root, [])
        self.assertEqual(len(entries), 5)
        for entry in entries:
            with self.subTest(repository=entry["repository"]):
                self.assertTrue(entry["available"], entry["unavailableReason"])
                self.assertIn(entry["evidenceLabel"], ("Saved source scan", "Static finding"))
                self.assertRegex(entry["scan"]["result"]["commit"], r"^[0-9a-f]{40}$")
                self.assertIn("No repository code", entry["scan"]["result"]["scope"])
        self.assertEqual(entries[0]["scan"]["result"]["commit"], SHA)


if __name__ == "__main__":
    unittest.main()
