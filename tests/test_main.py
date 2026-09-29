"""CLI dispatch preserves comparison behavior without retired services."""
from contextlib import redirect_stderr
from io import StringIO
import unittest
from unittest.mock import patch

from src.main import main, parser
from src.upgrade_demo import UpgradeError


class MainTests(unittest.TestCase):
    def test_offline_and_prepare_options_reach_comparison(self):
        for options in (["--offline"], ["--prepare", "--rebuild"]):
            with self.subTest(options=options), patch("src.upgrade_demo.run_demo", return_value=1) as run:
                self.assertEqual(main(["upgrade-demo", *options]), 1)
                run.assert_called_once_with(offline="--offline" in options,
                                            prepare="--prepare" in options, rebuild="--rebuild" in options)

    def test_retired_commands_and_flags_are_rejected(self):
        for args in (["ingest"], ["check", "a" * 40], ["upgrade-demo", "--require-integrations"],
                     ["upgrade-demo", "--no-memory"]):
            with self.subTest(args=args), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
                parser().parse_args(args)
            self.assertEqual(error.exception.code, 2)

    def test_failed_comparison_has_readable_error(self):
        with patch("src.upgrade_demo.run_demo", side_effect=UpgradeError("Missing image")), redirect_stderr(StringIO()) as out:
            self.assertEqual(main(["upgrade-demo", "--offline"]), 2)
        self.assertIn("Missing image", out.getvalue())

    def test_unexpected_provider_error_does_not_expose_details(self):
        with patch("src.upgrade_demo.run_demo", side_effect=RuntimeError("private token")), redirect_stderr(StringIO()) as out:
            self.assertEqual(main(["upgrade-demo"]), 2)
        self.assertNotIn("private token", out.getvalue())
