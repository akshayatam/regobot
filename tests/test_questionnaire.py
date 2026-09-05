import json
import unittest
from pathlib import Path

from regodit.models import Claim, Evidence, QuestionnaireItem
from regodit.questionnaire import ARTIFACT_DIR, DATA_DIR, QUESTIONNAIRE_NAME, build_manifest, generate, parse_questionnaire


class QuestionnaireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = next(DATA_DIR.rglob(QUESTIONNAIRE_NAME))
        cls.source_hash_before = cls.source.read_bytes()
        cls.manifest, cls.items = generate()

    def test_complete_questionnaire_and_stable_unique_ids(self):
        self.assertEqual(len(self.items), 66)
        self.assertEqual(len({item.id for item in self.items}), 66)
        self.assertEqual(self.items[0].id, "VSQ-001")
        self.assertEqual(self.items[-1].id, "VSQ-066")

    def test_positions_and_source_values_are_preserved(self):
        item = self.items[51]
        self.assertEqual(item.source_question_id, "52.0")
        self.assertEqual(item.source_row, 67)
        self.assertEqual(item.source_cell, "B67")
        self.assertEqual(item.question, "52.0")
        self.assertEqual(item.normalized_control, "unclassified")

    def test_no_answers_are_fabricated(self):
        self.assertTrue(all(item.answer is None for item in self.items))
        self.assertTrue(all(item.status == "UNKNOWN" for item in self.items))
        self.assertTrue(all(item.confidence == 0 for item in self.items))
        self.assertTrue(all(item.evidence_ids == [] for item in self.items))

    def test_all_source_files_are_manifested_and_solsphere_flagged(self):
        files = [path for path in DATA_DIR.rglob("*") if path.is_file()]
        self.assertEqual(len(self.manifest), len(files))
        solsphere = [row for row in self.manifest if "Solsphere" in str(row["path"])]
        self.assertEqual(len(solsphere), 2)
        self.assertTrue(all(row["likely_organization"] == "Solsphere" for row in solsphere))
        self.assertTrue(all(row["entity_ambiguous_or_potentially_irrelevant"] for row in solsphere))

    def test_artifacts_are_valid_and_source_is_unchanged(self):
        for name in ("data_manifest.json", "questionnaire_items.json", "dataset_audit.md"):
            self.assertTrue((ARTIFACT_DIR / name).is_file())
        json.loads((ARTIFACT_DIR / "data_manifest.json").read_text())
        json.loads((ARTIFACT_DIR / "questionnaire_items.json").read_text())
        self.assertEqual(self.source.read_bytes(), self.source_hash_before)

    def test_models_validate_required_invariants(self):
        with self.assertRaises(ValueError):
            QuestionnaireItem("", "1.0", "x", "x", "x", "x", "x", 1, "B1")
        with self.assertRaises(ValueError):
            Evidence("", "file", "data/file", "policy", "POLICY_REQUIREMENT", "Regodit", "p1", "text")
        with self.assertRaises(ValueError):
            Claim("c", "MFA", "enabled", "yes", "all", "Regodit", "VERIFIED", 0.9, [], "now", "now")


if __name__ == "__main__":
    unittest.main()
