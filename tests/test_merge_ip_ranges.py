import tempfile
import unittest
from pathlib import Path

import merge_Ban as mod


class TestMergeIPRanges(unittest.TestCase):
    def test_parse_supported_ipv4_forms(self):
        self.assertEqual(mod.parse_range("192.0.2.7"), mod.IPRange(4, 3221225991, 3221225991))
        self.assertEqual(mod.parse_range("192.0.2.7/24"), mod.IPRange(4, 3221225984, 3221226239))
        self.assertEqual(
            mod.parse_range("192.0.2.10 - 192.0.2.20"),
            mod.IPRange(4, 3221225994, 3221226004),
        )

    def test_parse_ipv6_and_mapped_ipv6(self):
        self.assertEqual(mod.format_range(mod.parse_range("2001:db8::/126")), "2001:db8::-2001:db8::3")
        self.assertEqual(
            mod.format_range(mod.parse_range("::ffff:192.0.2.1-::ffff:192.0.2.2")),
            "::ffff:192.0.2.1-::ffff:192.0.2.2",
        )

    def test_cidr_host_bits_are_normalized(self):
        self.assertEqual(mod.format_range(mod.parse_range("10.0.0.99/24")), "10.0.0.0-10.0.0.255")

    def test_merge_removes_duplicates_overlap_containment_and_adjacency(self):
        ranges = [
            mod.parse_range("10.0.0.0/25"),
            mod.parse_range("10.0.0.64-10.0.0.200"),
            mod.parse_range("10.0.0.201"),
            mod.parse_range("10.0.0.201"),
            mod.parse_range("2001:db8::1"),
        ]
        self.assertEqual(
            [mod.format_range(item) for item in mod.merge_ranges(ranges)],
            ["10.0.0.0-10.0.0.201", "2001:db8::1"],
        )

    def test_intersection_is_exact_for_both_versions(self):
        left = mod.merge_ranges(
            [mod.parse_range("10.0.0.0/24"), mod.parse_range("2001:db8::/126")]
        )
        right = mod.merge_ranges(
            [mod.parse_range("10.0.0.128/25"), mod.parse_range("2001:db8::2/127")]
        )
        self.assertEqual(
            [mod.format_range(item) for item in mod.intersect_ranges(left, right)],
            ["10.0.0.128-10.0.0.255", "2001:db8::2-2001:db8::3"],
        )

    def test_bad_range_reports_input_file_and_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.txt"
            path.write_text("192.0.2.1\nnot-an-ip\n", encoding="utf-8")
            with self.assertRaisesRegex(mod.InputError, r"bad\.txt, line 2"):
                mod.read_ranges(path)

    def test_main_reads_current_files_and_writes_utf8_lf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.txt"
            second = root / "second.txt"
            output = root / "Ban.dat"
            first.write_text("10.0.0.0-10.0.0.10\n2001:db8::1\n", encoding="utf-8")
            second.write_text("10.0.0.8/30\n10.0.0.11\n2001:db8::2\n", encoding="utf-8")

            self.assertEqual(mod.main([str(first), str(second), "-o", str(output)]), 0)
            self.assertEqual(
                output.read_bytes(),
                b"10.0.0.0-10.0.0.11\n2001:db8::1-2001:db8::2\n",
            )
            self.assertEqual(list(root.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
