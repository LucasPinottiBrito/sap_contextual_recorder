from __future__ import annotations

import unittest
from unittest.mock import patch

from main import _parse_arguments, read_selection


class CliSelectionTests(unittest.TestCase):
    @patch("builtins.input", return_value="1")
    def test_accepts_a_valid_list_index(self, _: object) -> None:
        self.assertEqual(1, read_selection(3))

    @patch("builtins.input", return_value="abc")
    def test_rejects_non_numeric_input(self, _: object) -> None:
        with self.assertRaises(ValueError):
            read_selection(3)

    @patch("builtins.input", return_value="3")
    def test_rejects_out_of_range_input(self, _: object) -> None:
        with self.assertRaises(ValueError):
            read_selection(3)

    def test_parses_inspect_json_mode(self) -> None:
        self.assertEqual(("inspect", True), _parse_arguments(["inspect", "--json"]))

    def test_rejects_json_flag_outside_inspect(self) -> None:
        self.assertEqual(("invalid", False), _parse_arguments(["monitor", "--json"]))

    def test_parses_record_mode(self) -> None:
        self.assertEqual(("record", False), _parse_arguments(["record"]))


if __name__ == "__main__":
    unittest.main()
