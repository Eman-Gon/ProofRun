"""Existing coverage explicitly provides the nullable nickname field."""

import unittest

from app import import_row


class TestExisting(unittest.TestCase):
    def test_explicit_nickname(self):
        self.assertEqual(
            import_row({"name": "Ada", "nickname": "Countess"}),
            {"name": "Ada", "nickname": "Countess"},
        )

    def test_explicit_none(self):
        self.assertEqual(
            import_row({"name": "Ada", "nickname": None}),
            {"name": "Ada", "nickname": None},
        )


if __name__ == "__main__":
    unittest.main()
