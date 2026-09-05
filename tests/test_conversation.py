import tempfile
import unittest
from pathlib import Path

from regodit.ui.app import AppService


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.profile_path = root / "profile.sqlite3"
        self.conversation_path = root / "conversation.sqlite3"
        self.service = AppService(self.profile_path, root / "exports", conversation_db=self.conversation_path)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_search_before_ask_returns_verified_mfa_with_sources(self):
        result = self.service.chat_message("mfa", "Is MFA required?")
        self.assertEqual(result["assistant"]["status"], "VERIFIED")
        self.assertTrue(result["assistant"]["evidence"])
        self.assertIsNone(result["pending"])

    def test_missing_background_check_is_confirmed_and_remembered(self):
        first = self.service.chat_message("background", "Are employee background checks required?")
        self.assertEqual(first["assistant"]["status"], "UNKNOWN")
        self.assertIsNotNone(first["pending"])
        confirmed = self.service.chat_message("background", "Yes")
        self.assertEqual(confirmed["assistant"]["status"], "USER_CONFIRMED")
        remembered = self.service.chat_message("remember", "Are employee background checks required?")
        self.assertEqual(remembered["assistant"]["status"], "USER_CONFIRMED")
        self.assertIsNone(remembered["pending"])

    def test_vague_backup_answer_collects_only_required_fields_and_updates_row(self):
        first = self.service.chat_message("backup", "Investigate VSQ-041")
        self.assertIn("performed", first["assistant"]["content"])
        second = self.service.chat_message("backup", "Yes")
        self.assertIn("frequently", second["assistant"]["content"])
        third = self.service.chat_message("backup", "Daily")
        self.assertIn("automated", third["assistant"]["content"])
        final = self.service.chat_message("backup", "Yes")
        self.assertEqual(final["assistant"]["status"], "USER_CONFIRMED")
        facts = {claim.attribute: claim.value for claim in self.service.profile.lookup("backups", "Regodit").claims}
        self.assertEqual(facts, {"automated": True, "cadence": "daily", "enabled": True})
        self.assertEqual(self.service.profile.questionnaire_state("VSQ-041").status, "USER_CONFIRMED")

    def test_pending_interrupt_survives_service_restart(self):
        self.service.chat_message("durable", "Investigate VSQ-041")
        self.service.close()
        self.service = AppService(self.profile_path, Path(self.temp.name) / "exports", conversation_db=self.conversation_path)
        resumed = self.service.chat_message("durable", "Yes")
        self.assertIn("frequently", resumed["assistant"]["content"])

    def test_correction_supersedes_prior_fact(self):
        for response in ("Investigate VSQ-041", "Yes", "Daily", "Yes"):
            self.service.chat_message("correction", response)
        result = self.service.chat_message("correction", "Actually, backups now run every six hours.")
        self.assertIn("daily → every six hours", result["assistant"]["content"])
        history = [entry for entry in self.service.profile.claim_history("backups") if entry.claim.attribute == "cadence"]
        self.assertEqual([entry.status for entry in history], ["SUPERSEDED", "ACTIVE"])

    def test_conflict_is_explained_and_conversationally_resolved(self):
        self.service.profile.record_user_claim(
            "mfa", "implemented", False, "active account",
            "One active account may not have MFA enabled.", "owner@example.com",
        )
        conflict = self.service.chat_message("conflict", "Investigate VSQ-060")
        self.assertEqual(conflict["assistant"]["status"], "CONFLICT")
        self.assertGreaterEqual(len(conflict["assistant"]["evidence"]), 2)
        self.assertIsNotNone(conflict["pending"])
        resolved = self.service.chat_message(
            "conflict", "Yes. That record was outdated. MFA is now enforced for every active account.",
        )
        self.assertEqual(resolved["assistant"]["status"], "USER_CONFIRMED")
        self.assertEqual([entry.status for entry in self.service.profile.claim_history("mfa")], ["SUPERSEDED", "ACTIVE"])

    def test_generate_questionnaire_is_transparent_and_non_destructive(self):
        result = self.service.chat_message("export", "Generate questionnaire")
        self.assertIn("unknown", result["assistant"]["content"])
        self.assertTrue(Path(result["assistant"]["downloads"]["xlsx"]).exists())
        self.assertNotEqual(Path(result["assistant"]["downloads"]["xlsx"]).resolve(), self.service.source.resolve())


if __name__ == "__main__":
    unittest.main()
