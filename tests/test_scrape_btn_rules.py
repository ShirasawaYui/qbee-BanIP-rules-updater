import tempfile
import unittest
from pathlib import Path

import scrape_BTN as mod


class TestBTNRules(unittest.TestCase):
    def test_raw_url_converts_blob_link(self):
        self.assertEqual(
            mod.raw_url(
                "https://github.com/PBH-BTN/BTN-Collected-Rules/blob/main/combine/all.txt"
            ),
            "https://raw.githubusercontent.com/PBH-BTN/BTN-Collected-Rules/main/combine/all.txt",
        )

    def test_raw_url_keeps_non_blob_url(self):
        url = "https://raw.githubusercontent.com/example/repo/main/rules.txt"
        self.assertEqual(mod.raw_url(url), url)

    def test_remove_comments(self):
        self.assertEqual(
            mod.remove_comments(["# heading", "  1.2.3.4  ", "", "\t# note", "2001:db8::/32"]),
            ["1.2.3.4", "2001:db8::/32"],
        )

    def test_write_rules_is_utf8_lf_and_dedicated_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "BTN.txt"
            mod.write_rules(path, ["# comment", "1.2.3.4", " 2001:db8::/32 "])
            self.assertEqual(path.read_bytes(), b"1.2.3.4\n2001:db8::/32\n")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
