import json
from pathlib import Path
import unittest


class FixtureIntegrityTests(unittest.TestCase):
    def test_locked_signoffs_and_measurement_revisions_are_present(self):
        data = json.loads((Path(__file__).parents[1] / "data/facts.json").read_text(encoding="utf-8"))
        self.assertEqual([row["id"] for row in data["measurements"]], ["V1", "V2"])
        self.assertEqual(data["measurements"][1]["supersedes"], "V1")
        self.assertEqual(len(data["certifications"]), 2)
        self.assertTrue(all(row["period"] == "2026-10" for row in data["certifications"]))


if __name__ == "__main__":
    unittest.main()
