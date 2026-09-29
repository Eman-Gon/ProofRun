"""Engineer-approved controls, independent of the proposed repair and probe."""

import unittest

from pydantic import ValidationError

from app import import_row


class TestControls(unittest.TestCase):
    def test_nickname_omitted(self):
        self.assertEqual(import_row({"name": "Grace"}), {"name": "Grace", "nickname": None})

    def test_nickname_null(self):
        self.assertEqual(
            import_row({"name": "Grace", "nickname": None}),
            {"name": "Grace", "nickname": None},
        )

    def test_nickname_string_unchanged(self):
        self.assertEqual(
            import_row({"name": "Grace", "nickname": "  Amazing Grace  "}),
            {"name": "Grace", "nickname": "  Amazing Grace  "},
        )

    def test_nickname_object_rejected(self):
        with self.assertRaises(ValidationError):
            import_row({"name": "Grace", "nickname": {"unexpected": "object"}})

    def test_required_name_rejected(self):
        with self.assertRaises(ValidationError):
            import_row({"nickname": "Grace"})


if __name__ == "__main__":
    unittest.main()
