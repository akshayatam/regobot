import tempfile
import unittest
from pathlib import Path

from regodit.models import QuestionnaireItem
from regodit.analyst.claims import SecurityClaim
from regodit.analyst import AnalystEngine
from regodit.memory import SecurityProfile


def item(question_id="Q-BACKUP", question="How frequently are production database backups performed?", control="backups"):
    return QuestionnaireItem(question_id, "1.0", "BC/DR", question, control, "test", "sheet", 1, "B1")


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "profile.sqlite3"
        self.profile = SecurityProfile(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_resolved_fact_persists_across_restarts_and_avoids_duplicate_question(self):
        q = item()
        result = self.profile.answer_follow_up(q, "Backups run daily.", "alice@example.com", retriever=lambda *args: [])
        self.assertEqual(result.next_action, "ANSWER")
        self.assertEqual(result.status, "USER_CONFIRMED")
        self.assertFalse(self.profile.needs_question(q.id))
        reopened = SecurityProfile(self.db)
        repeated = AnalystEngine(profile=reopened, retriever=lambda *args: []).investigate(q)
        self.assertEqual(repeated.next_action, "ANSWER")
        self.assertEqual(repeated.answer, "Daily")
        self.assertFalse(reopened.needs_question(q.id))

    def test_user_correction_preserves_history_and_supersession(self):
        old = self.profile.record_user_claim("backups", "cadence", "daily", "production database", "Backups run daily.", "alice")
        new = self.profile.correct_claim(old.id, "every six hours", "We changed backups to every six hours.", "alice")
        history = self.profile.claim_history("backups")
        self.assertEqual(len(history), 2)
        by_id = {entry.claim.id: entry for entry in history}
        self.assertEqual(by_id[old.id].status, "SUPERSEDED")
        self.assertEqual(by_id[old.id].superseded_by, new.id)
        self.assertEqual(by_id[new.id].supersedes, old.id)
        active = self.profile.lookup("backups", "Regodit")
        self.assertEqual([claim.value for claim in active.claims], ["every six hours"])

    def test_mfa_requirement_and_account_gap_surface_conflict_not_flat_yes(self):
        policy_evidence = __import__("regodit.models", fromlist=["Evidence"]).Evidence(
            "ev-policy", "policy.docx", "data/policy.docx", "policy", "POLICY_REQUIREMENT", "Regodit", "p1", "MFA is required for all core systems.")
        policy = SecurityClaim("claim-policy", "mfa", "required", "all core systems", True, "Regodit", "DOCUMENTED", "POLICY_REQUIREMENT", ("ev-policy",), policy_evidence.text, 0.8)
        self.profile.add_claim(policy, [policy_evidence], "DOCUMENT")
        gap = self.profile.record_user_claim("mfa", "implemented", False, "active account", "One active account may not have MFA enabled.", "security-owner")
        q = item("Q-MFA", "Is MFA required for all core systems?", "MFA")
        result = AnalystEngine(profile=self.profile, retriever=lambda *args: []).investigate(q)
        self.assertEqual(result.status, "CONFLICT")
        self.assertEqual(result.next_action, "RESOLVE_CONFLICT")
        self.assertIsNone(result.answer)
        self.assertIn(True, result.conflicts[0].claim_values)
        self.assertIn(False, result.conflicts[0].claim_values)
        self.assertIn("disagrees", result.conflicts[0].description)

    def test_resolved_conflict_updates_active_profile(self):
        old = self.profile.record_user_claim("mfa", "implemented", False, "all core systems", "One active account lacks MFA.", "owner")
        current = self.profile.record_user_claim("mfa", "implemented", True, "all core systems", "All active accounts now have MFA enabled.", "owner")
        self.profile.resolve_conflict(current.id, [old.id], "owner", "Remediation is complete.")
        active = self.profile.lookup("mfa", "Regodit")
        self.assertEqual([(claim.id, claim.value) for claim in active.claims], [(current.id, True)])
        history = {entry.claim.id: entry for entry in self.profile.claim_history("mfa")}
        self.assertEqual(history[old.id].status, "SUPERSEDED")
        self.assertEqual(history[old.id].superseded_by, current.id)

    def test_questionnaire_answer_updates_after_memory_correction(self):
        q = item()
        first = self.profile.answer_follow_up(q, "Backups run daily.", "alice", retriever=lambda *args: [])
        old_id = first.claims[0].id
        second = self.profile.answer_follow_up(q, "Backups changed to every six hours.", "alice", supersedes=old_id, retriever=lambda *args: [])
        state = self.profile.questionnaire_state(q.id)
        self.assertEqual(second.answer, "Every six hours")
        self.assertIsNotNone(state)
        self.assertEqual(state.answer, "Every six hours")
        self.assertEqual(state.status, "USER_CONFIRMED")
        self.assertEqual(len(self.profile.claim_history("backups")), 2)

    def test_raw_confirmation_and_stakeholder_are_preserved(self):
        claim = self.profile.record_user_claim("backups", "cadence", "daily", "production", "Backups run daily.", "alice@example.com")
        with self.profile._connect() as db:
            row = db.execute("SELECT * FROM user_confirmations WHERE claim_id = ?", (claim.id,)).fetchone()
            evidence = db.execute("SELECT * FROM evidence WHERE id = ?", (claim.evidence_ids[0],)).fetchone()
        self.assertEqual(row["raw_response"], "Backups run daily.")
        self.assertEqual(row["stakeholder"], "alice@example.com")
        self.assertEqual(evidence["evidence_type"], "USER_CONFIRMATION")


if __name__ == "__main__":
    unittest.main()
