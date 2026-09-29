"""Probe the omitted-field behavior described by the migration guide."""

import unittest

from app import import_row


class TestUpgrade(unittest.TestCase):
    def test_missing_nickname(self):
        self.assertEqual(
            import_row({"name": "Ada"}),
            {"name": "Ada", "nickname": None},
        )


if __name__ == "__main__":
    unittest.main()
