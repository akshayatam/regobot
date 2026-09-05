import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from regodit.analyst import AnalystEngine, InvestigationResult
from regodit.analyst.claims import SecurityClaim
from regodit.config import active_model, model_source
from regodit.memory import SecurityProfile
from regodit.models import QuestionnaireItem
from regodit.retest import RetestService
from regodit.sync import QuestionnaireSynchronizer
from regodit.ui.app import AppService


def item(question_id="VSQ-901", control="mfa", question="Is multi-factor authentication required?"):
    return QuestionnaireItem(question_id, "1.0", "Access Control", question, control, "test", "sheet", 1, "B1")


def result(question_id, status, answer=None, evidence_ids=(), claims=(), conflicts=(), confidence=0.9):
    """Build the InvestigationResult a model run would produce, without inventing company evidence."""
    if status in {"VERIFIED", "USER_CONFIRMED"}:
        return InvestigationResult(
            question_id, True, answer, status, "ANSWER", tuple(claims), tuple(evidence_ids), (), (), None,
            confidence, True, True,
        )
    if status == "CONFLICT":
        return InvestigationResult(
            question_id, False, None, "CONFLICT", "RESOLVE_CONFLICT", tuple(claims), tuple(evidence_ids),
            tuple(conflicts), ("conflict resolution",), "Which statement is current?", 0.4, True, True,
        )
    return InvestigationResult(
        question_id, False, None, "UNKNOWN", "MARK_UNKNOWN", tuple(claims), tuple(evidence_ids), (),
        ("yes/no answer",), None, 0.0, True, True,
    )


class ScriptedEngine:
    """Stands in for a differently configured model runtime with deterministic outputs."""

    def __init__(self, model, outcomes):
        self.model_runtime = SimpleNamespace(model=model, enabled=True)
        self.outcomes = outcomes
        self.observer = SimpleNamespace(retest=lambda **kwargs: self.traces.append(kwargs))
        self.traces = []
        self.investigated = []

    def investigate(self, questionnaire_item, organization="Regodit"):
        self.investigated.append(questionnaire_item.id)
        return self.outcomes[questionnaire_item.id]

    def flush_traces(self):
        return None


def claim(value=True, evidence_id="ev-test-policy", attribute="required"):
    return SecurityClaim(
        f"claim-{evidence_id}-{attribute}-{value}", "mfa", attribute, "organization-wide/unspecified", value,
        "Regodit", "DOCUMENTED", "POLICY_REQUIREMENT", (evidence_id,), "MFA is required.", 0.8,
    )


class ModelConfigurationTests(unittest.TestCase):
    def test_model_comes_from_configuration_and_survives_a_restart(self):
        original = {name: os.environ.get(name) for name in ("OPENAI_MODEL", "LLM_MODEL")}
        try:
            os.environ.pop("LLM_MODEL", None)
            os.environ["OPENAI_MODEL"] = "gpt-retest-a"
            self.assertEqual(active_model(), "gpt-retest-a")
            self.assertEqual(model_source(), "OPENAI_MODEL")
            os.environ["OPENAI_MODEL"] = "gpt-retest-b"
            self.assertEqual(active_model(), "gpt-retest-b")
            self.assertEqual(AnalystEngine().model_runtime.model, "gpt-retest-b")
        finally:
            for name, value in original.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_stopping_and_restarting_with_a_new_model_keeps_all_persistent_state(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "profile.sqlite3"
            original = os.environ.get("OPENAI_MODEL")
            try:
                os.environ["OPENAI_MODEL"] = "gpt-retest-a"
                first = AppService(db, Path(temp) / "exports")
                first.investigate("VSQ-020")
                first.profile.record_user_claim(
                    "backups", "cadence", "daily", "production database", "Backups run daily.", "owner@example.com")
                before = first.dashboard()["progress"]
                claims_before = len(first.profile.claim_history())
                self.assertEqual(first.model_info()["active_model"], "gpt-retest-a")
                first.close()

                os.environ["OPENAI_MODEL"] = "gpt-retest-b"
                second = AppService(db, Path(temp) / "exports")
                self.assertEqual(second.model_info()["active_model"], "gpt-retest-b")
                self.assertEqual(second.model_info()["source"], "OPENAI_MODEL")
                self.assertEqual(second.dashboard()["progress"], before)
                self.assertEqual(len(second.profile.claim_history()), claims_before)
                self.assertEqual(second.profile.questionnaire_state("VSQ-020").status, "VERIFIED")
                self.assertTrue(second.profile.evaluation_history("VSQ-020"))
                second.close()
            finally:
                if original is None:
                    os.environ.pop("OPENAI_MODEL", None)
                else:
                    os.environ["OPENAI_MODEL"] = original


class RetestScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.profile = SecurityProfile(Path(self.temp.name) / "profile.sqlite3")
        self.items = [item("VSQ-901"), item("VSQ-902", question="Are backups performed?"), item("VSQ-903")]

    def tearDown(self):
        self.temp.cleanup()

    def _service(self, model, outcomes):
        engine = ScriptedEngine(model, outcomes)
        service = RetestService(self.items, self.profile, engine, QuestionnaireSynchronizer(self.items, self.profile, engine))
        return service, engine

    def test_unresolved_scope_skips_already_resolved_questions(self):
        self.profile.save_questionnaire_result(
            result("VSQ-901", "VERIFIED", "Yes.", ("ev-a",), (claim(),)), model="gpt-old")
        outcomes = {
            "VSQ-902": result("VSQ-902", "UNKNOWN"),
            "VSQ-903": result("VSQ-903", "UNKNOWN"),
        }
        service, engine = self._service("gpt-new", outcomes)
        report = service.run("unresolved").summary()
        self.assertEqual(sorted(engine.investigated), ["VSQ-902", "VSQ-903"])
        self.assertEqual(report["evaluated"], 2)
        self.assertEqual(report["scope"], "unresolved")

    def test_all_scope_evaluates_every_question(self):
        outcomes = {value.id: result(value.id, "UNKNOWN") for value in self.items}
        service, engine = self._service("gpt-new", outcomes)
        report = service.run("all").summary()
        self.assertEqual(len(engine.investigated), 3)
        self.assertEqual(report["evaluated"], 3)

    def test_selected_scope_evaluates_one_question_and_rejects_unknown_ids(self):
        outcomes = {"VSQ-902": result("VSQ-902", "UNKNOWN")}
        service, engine = self._service("gpt-new", outcomes)
        report = service.run("selected", ["VSQ-902"]).summary()
        self.assertEqual(engine.investigated, ["VSQ-902"])
        self.assertEqual(report["evaluated"], 1)
        with self.assertRaises(ValueError):
            service.run("selected", ["VSQ-999"])
        with self.assertRaises(ValueError):
            service.run("selected", [])

    def test_unknown_becomes_verified_with_evidence_and_history_is_preserved(self):
        self.profile.save_questionnaire_result(result("VSQ-901", "UNKNOWN"), model="gpt-old")
        outcomes = {"VSQ-901": result("VSQ-901", "VERIFIED", "Yes — required.", ("ev-a", "ev-b"), (claim(),))}
        service, _ = self._service("gpt-new", outcomes)
        report = service.run("selected", ["VSQ-901"]).summary()
        self.assertEqual(report["newly_resolved"], 1)
        self.assertEqual(self.profile.questionnaire_state("VSQ-901").status, "VERIFIED")
        history = self.profile.evaluation_history("VSQ-901")
        self.assertEqual([entry.status for entry in history], ["UNKNOWN", "VERIFIED"])
        self.assertEqual([entry.model for entry in history], ["gpt-old", "gpt-new"])
        self.assertEqual(history[-1].run_type, "model_retest")
        self.assertEqual(history[-1].previous_status, "UNKNOWN")

    def test_unresolvable_question_stays_unknown(self):
        self.profile.save_questionnaire_result(result("VSQ-901", "UNKNOWN"), model="gpt-old")
        service, _ = self._service("gpt-new", {"VSQ-901": result("VSQ-901", "UNKNOWN")})
        report = service.run("selected", ["VSQ-901"]).summary()
        self.assertEqual(report["still_unknown"], 1)
        self.assertEqual(report["newly_resolved"], 0)
        self.assertEqual(self.profile.questionnaire_state("VSQ-901").status, "UNKNOWN")

    def test_upgrade_without_stronger_evidence_is_flagged_not_celebrated(self):
        self.profile.save_questionnaire_result(
            result("VSQ-901", "UNKNOWN", evidence_ids=("ev-a",)), model="gpt-old")
        outcomes = {"VSQ-901": result("VSQ-901", "VERIFIED", "Yes.", ("ev-a",), (claim(),))}
        service, _ = self._service("gpt-new", outcomes)
        report = service.run("selected", ["VSQ-901"]).summary()
        self.assertEqual(report["suspicious_upgrades"], ["VSQ-901"])
        self.assertIn("Flagged", report["outcomes"][0]["note"])

    def test_stronger_evidence_upgrade_is_not_flagged(self):
        self.profile.save_questionnaire_result(
            result("VSQ-901", "UNKNOWN", evidence_ids=("ev-a",)), model="gpt-old")
        outcomes = {"VSQ-901": result("VSQ-901", "VERIFIED", "Yes.", ("ev-a", "ev-new"), (claim(),))}
        service, _ = self._service("gpt-new", outcomes)
        report = service.run("selected", ["VSQ-901"]).summary()
        self.assertEqual(report["suspicious_upgrades"], [])

    def test_model_disagreement_never_overwrites_a_user_confirmed_fact(self):
        self.profile.save_confirmed_questionnaire_state("VSQ-901", "required=True", ["ev-user"])
        outcomes = {"VSQ-901": result("VSQ-901", "VERIFIED", "No — not required.", ("ev-a",), (claim(False),))}
        service, _ = self._service("gpt-new", outcomes)
        report = service.run("selected", ["VSQ-901"]).summary()
        self.assertEqual(report["not_applied"], 1)
        self.assertEqual(self.profile.questionnaire_state("VSQ-901").status, "USER_CONFIRMED")
        self.assertEqual(self.profile.questionnaire_state("VSQ-901").answer, "required=True")
        candidates = self.profile.conflict_candidates("VSQ-901")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["proposed_answer"], "No — not required.")
        history = self.profile.evaluation_history("VSQ-901")
        self.assertEqual([entry.accepted for entry in history], [True, False])

    def test_verified_state_is_not_erased_when_a_retest_finds_nothing(self):
        self.profile.save_questionnaire_result(
            result("VSQ-901", "VERIFIED", "Yes.", ("ev-a",), (claim(),)), model="gpt-old")
        service, _ = self._service("gpt-new", {"VSQ-901": result("VSQ-901", "UNKNOWN")})
        report = service.run("selected", ["VSQ-901"]).summary()
        self.assertEqual(report["not_applied"], 1)
        self.assertEqual(self.profile.questionnaire_state("VSQ-901").status, "VERIFIED")
        self.assertEqual(report["outcomes"][0]["decision"], "kept_verified")

    def test_agreeing_company_evidence_may_upgrade_a_confirmed_row(self):
        self.profile.save_confirmed_questionnaire_state("VSQ-901", "Yes — confirmed by the team", ["ev-user"])
        outcomes = {"VSQ-901": result("VSQ-901", "VERIFIED", "Yes — documented as required.", ("ev-a",), (claim(),))}
        service, _ = self._service("gpt-new", outcomes)
        report = service.run("selected", ["VSQ-901"]).summary()
        self.assertEqual(report["outcomes"][0]["decision"], "upgraded_to_verified")
        self.assertEqual(self.profile.questionnaire_state("VSQ-901").status, "VERIFIED")

    def test_retests_are_prism_traced_with_run_metadata(self):
        self.profile.save_questionnaire_result(result("VSQ-901", "UNKNOWN"), model="gpt-old")
        outcomes = {"VSQ-901": result("VSQ-901", "VERIFIED", "Yes.", ("ev-a", "ev-b"), (claim(),))}
        service, engine = self._service("gpt-new", outcomes)
        report = service.run("selected", ["VSQ-901"]).summary()
        self.assertEqual(len(engine.traces), 1)
        trace = engine.traces[0]
        self.assertEqual(trace["question_id"], "VSQ-901")
        self.assertEqual(trace["model"], "gpt-new")
        self.assertEqual(trace["previous_status"], "UNKNOWN")
        self.assertEqual(trace["new_status"], "VERIFIED")
        self.assertFalse(trace["conflict_resolved"])
        self.assertEqual(trace["run_id"], report["run_id"])

    def test_comparison_reports_before_and_after_counts_and_models(self):
        self.profile.save_questionnaire_result(result("VSQ-901", "UNKNOWN"), model="gpt-old")
        self.profile.save_questionnaire_result(result("VSQ-902", "UNKNOWN"), model="gpt-old")
        self.profile.save_questionnaire_result(result("VSQ-903", "UNKNOWN"), model="gpt-old")
        outcomes = {
            "VSQ-901": result("VSQ-901", "VERIFIED", "Yes.", ("ev-a", "ev-new"), (claim(),)),
            "VSQ-902": result("VSQ-902", "UNKNOWN"),
            "VSQ-903": result("VSQ-903", "UNKNOWN"),
        }
        service, _ = self._service("gpt-new", outcomes)
        report = service.run("unresolved").summary()
        self.assertEqual(report["previous_models"], ["gpt-old"])
        self.assertEqual(report["model"], "gpt-new")
        self.assertEqual(report["before"], {"verified": 0, "user_confirmed": 0, "resolved": 0, "unknown": 3, "conflict": 0, "total": 3})
        self.assertEqual(report["after"], {"verified": 1, "user_confirmed": 0, "resolved": 1, "unknown": 2, "conflict": 0, "total": 3})
        self.assertEqual(len(report["changes"]), 1)
        self.assertEqual(report["changes"][0]["question_id"], "VSQ-901")


class ConflictSynchronizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = AppService(Path(self.temp.name) / "profile.sqlite3", Path(self.temp.name) / "exports")

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_resolved_conflict_leaves_active_conflict_state_immediately(self):
        self.service.analyze_all()
        self.service.profile.record_user_claim(
            "mfa", "implemented", False, "active account",
            "One active account may not have MFA enabled.", "owner@example.com")
        conflicted = self.service.investigate("VSQ-060")
        self.assertEqual(conflicted.status, "CONFLICT")
        self.assertEqual(self.service.dashboard()["progress"]["conflicts"], 1)

        self.service.chat_message("thread", "Investigate VSQ-060")
        self.service.chat_message(
            "thread", "Yes. That record is outdated. MFA is now enforced for all active GitHub accounts.")

        progress = self.service.dashboard()["progress"]
        self.assertEqual(progress["conflicts"], 0)
        row = next(row for row in self.service.dashboard()["questions"] if row["id"] == "VSQ-060")
        self.assertIn(row["status"], {"VERIFIED", "USER_CONFIRMED"})
        history = self.service.profile.claim_history("mfa")
        self.assertIn("SUPERSEDED", [entry.status for entry in history])

    def test_retest_after_resolution_does_not_resurrect_the_obsolete_conflict(self):
        self.service.analyze_all()
        self.service.profile.record_user_claim(
            "mfa", "implemented", False, "active account",
            "One active account may not have MFA enabled.", "owner@example.com")
        self.service.chat_message("thread", "Investigate VSQ-060")
        self.service.chat_message("thread", "Yes. That record is outdated. MFA is now enforced.")

        report = self.service.retest("all")
        self.assertEqual(report["new_conflicts"], 0)
        self.assertEqual(report["after"]["conflict"], 0)
        self.assertTrue(self.service.profile.obsolete_claims("mfa"))

    def test_new_evidence_can_still_reopen_a_previously_retired_assertion(self):
        retired = SecurityClaim(
            "claim-retired", "mfa", "implemented", "GitHub", False, "Regodit", "IMPLEMENTED",
            "OPERATIONAL_RECORD", ("ev-known",), "One account does not have MFA.", 0.95)
        self.service.profile.mark_claims_obsolete([retired], "Clarified: the account was remediated.")
        self.assertTrue(self.service.profile.is_obsolete(retired))
        reopened = SecurityClaim(
            "claim-reopened", "mfa", "implemented", "GitHub", False, "Regodit", "IMPLEMENTED",
            "OPERATIONAL_RECORD", ("ev-newly-discovered",), "One account does not have MFA.", 0.95)
        self.assertFalse(self.service.profile.is_obsolete(reopened))

    def test_one_confirmation_updates_every_questionnaire_row_for_the_control(self):
        mapped = self.service.synchronizer.affected_questions("mfa")
        self.assertGreater(len(mapped), 1)
        self.service.profile.record_user_claim(
            "mfa", "implemented", True, "organization-wide/unspecified",
            "MFA is enforced for every active account.", "owner@example.com")
        changes = self.service.synchronizer.synchronize_control("mfa", "user confirmation")
        self.assertEqual({change.question_id for change in changes}, {value.id for value in mapped})
        states = self.service.profile.questionnaire_states()
        for value in mapped:
            self.assertIn(states[value.id].status, {"VERIFIED", "USER_CONFIRMED"})

    def test_backup_confirmation_in_chat_updates_all_backup_rows(self):
        backup_rows = {value.id for value in self.service.synchronizer.affected_questions("backups")}
        self.assertTrue(backup_rows)
        self.service.chat_message("backup-thread", "How often are backups performed?")
        self.service.chat_message("backup-thread", "Yes, production backups are performed.")
        self.service.chat_message("backup-thread", "They run daily.")
        self.service.chat_message("backup-thread", "Yes, they are automated.")
        states = self.service.profile.questionnaire_states()
        updated = {question_id for question_id in backup_rows if question_id in states}
        self.assertEqual(updated, backup_rows)

    def test_history_endpoint_exposes_evaluation_provenance(self):
        self.service.investigate("VSQ-020")
        history = self.service.history("VSQ-020")
        self.assertEqual(history["question_id"], "VSQ-020")
        self.assertTrue(history["evaluations"])
        entry = history["evaluations"][-1]
        self.assertEqual(entry["status"], "VERIFIED")
        self.assertTrue(entry["evidence_ids"])
        self.assertTrue(entry["created_at"])

    def test_dashboard_reports_the_active_model(self):
        info = self.service.dashboard()["model"]
        self.assertEqual(info["active_model"], self.service.engine.model_runtime.model)
        self.assertIn(info["source"], {"OPENAI_MODEL", "LLM_MODEL", "default"})
        self.assertEqual(info["retest_scopes"], ["unresolved", "all", "selected"])


if __name__ == "__main__":
    unittest.main()
