import tempfile
import unittest
from pathlib import Path

from regodit.models import QuestionnaireItem
from regodit.analyst.claims import extract_claims
from regodit.retrieval import retrieve_evidence
from regodit.ui.app import AppService
from regodit.demo import seed_demo


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.service = AppService(root / "profile.sqlite3", root / "exports")

    def tearDown(self):
        self.temp.cleanup()

    def test_fully_verified_control_has_policy_and_assessment_chain(self):
        evidence = retrieve_evidence("Is MFA required and implemented for production access?", "mfa", top_k=16)
        claims = extract_claims(evidence, "mfa").claims
        self.assertTrue(any(c.strength == "DOCUMENTED" for c in claims))
        self.assertTrue(any(c.strength == "IMPLEMENTED" for c in claims))
        self.assertTrue(all(c.evidence_ids[0] in {item.id for item in evidence} for c in claims))

    def test_search_before_ask_answers_without_employee(self):
        result = self.service.investigate("VSQ-020")
        self.assertEqual(result.next_action, "ANSWER")
        self.assertEqual(result.status, "VERIFIED")
        self.assertIsNone(result.follow_up_question)
        self.assertTrue(result.evidence_ids)

    def test_missing_information_asks_one_targeted_follow_up(self):
        result = self.service.investigate("VSQ-019")
        self.assertEqual(result.next_action, "ASK_FOLLOW_UP")
        self.assertEqual(result.missing_information, ("storage location",))
        self.assertEqual(result.follow_up_question.count("?"), 1)

    def test_vague_yes_is_not_stored_and_specificity_is_requested(self):
        before = len(self.service.profile.claim_history())
        result = self.service.submit_follow_up("VSQ-039", "Yes.", "employee@example.com")
        self.assertEqual(result.next_action, "ASK_FOLLOW_UP")
        self.assertIn("frequently", result.follow_up_question)
        self.assertEqual(len(self.service.profile.claim_history()), before)

    def test_conflict_then_correction_updates_memory_without_erasing_history(self):
        gap = self.service.profile.record_user_claim(
            "mfa", "implemented", False, "active account", "One active account may not have MFA enabled.", "owner@example.com")
        conflict = self.service.investigate("VSQ-060")
        self.assertEqual(conflict.status, "CONFLICT")
        self.assertIsNone(conflict.answer)
        self.assertTrue(conflict.follow_up_question)
        self.service.correct(gap.id, "yes", "The account was remediated and all active accounts now use MFA.", "owner@example.com")
        resolved = self.service.investigate("VSQ-060")
        self.assertNotEqual(resolved.status, "CONFLICT")
        history = self.service.profile.claim_history("mfa")
        self.assertEqual([entry.status for entry in history], ["SUPERSEDED", "ACTIVE"])

    def test_solsphere_is_not_used_as_regodit_evidence(self):
        evidence = retrieve_evidence("How are backups and disaster recovery handled?", "backups", "Regodit", 30)
        self.assertTrue(evidence)
        self.assertTrue(all(item.organization != "Solsphere" for item in evidence))
        self.assertTrue(all("Solsphere" not in item.source_name for item in evidence))

    def test_demo_seed_is_repeatable_and_covers_judging_story(self):
        path = Path(self.temp.name) / "demo.sqlite3"
        result = seed_demo(path)
        self.assertEqual(result["verified"]["status"], "VERIFIED")
        self.assertTrue(result["verified"]["evidence_ids"])
        self.assertTrue(result["missing"]["follow_up"])
        self.assertEqual(result["vague_answer"]["action"], "ASK_FOLLOW_UP")
        self.assertEqual(result["conflict"]["status"], "CONFLICT")
        self.assertTrue(Path(result["exports"]["json"]).exists())
        with self.assertRaises(FileExistsError):
            seed_demo(path)


if __name__ == "__main__":
    unittest.main()
