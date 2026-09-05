import unittest

from regodit.models import Evidence, QuestionnaireItem
from regodit.questionnaire import DATA_DIR, QUESTIONNAIRE_NAME, parse_questionnaire
from regodit.analyst.claims import SecurityClaim
from regodit.analyst import AnalystEngine, ProfileEvidence


class SpyProfile:
    def __init__(self, result=ProfileEvidence()):
        self.result = result
        self.calls = []

    def lookup(self, control, organization):
        self.calls.append((control, organization))
        return self.result


class AnalystTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = next(DATA_DIR.rglob(QUESTIONNAIRE_NAME))
        cls.items = {item.id: item for item in parse_questionnaire(source)}

    def test_question_answered_entirely_from_documents_with_evidence(self):
        result = AnalystEngine(top_k=24).investigate(self.items["VSQ-020"])
        self.assertEqual(result.next_action, "ANSWER")
        self.assertEqual(result.status, "VERIFIED")
        self.assertTrue(result.answerable)
        self.assertTrue(result.answer.startswith("Yes"))
        self.assertTrue(result.evidence_ids)
        self.assertTrue(all(claim.evidence_ids[0] in result.evidence_ids for claim in result.claims))

    def test_targeted_follow_up_after_search(self):
        profile = SpyProfile()
        retrieval_calls = []

        def retriever(query, control, organization, top_k):
            retrieval_calls.append((query, control, organization, top_k))
            return []

        result = AnalystEngine(profile=profile, retriever=retriever).investigate(self.items["VSQ-019"])
        self.assertEqual(profile.calls, [("data_location", "Regodit")])
        self.assertEqual(len(retrieval_calls), 1)
        self.assertEqual(result.next_action, "ASK_FOLLOW_UP")
        self.assertEqual(result.missing_information, ("storage location",))
        self.assertEqual(result.follow_up_question, "In which country or countries is the relevant customer data currently stored?")

    def test_malformed_question_is_correctly_left_unknown_after_search(self):
        retrieval_calls = []

        def retriever(*args):
            retrieval_calls.append(args)
            return []

        result = AnalystEngine(retriever=retriever).investigate(self.items["VSQ-052"])
        self.assertEqual(len(retrieval_calls), 1)
        self.assertEqual(result.next_action, "MARK_UNKNOWN")
        self.assertEqual(result.status, "UNKNOWN")
        self.assertIsNone(result.follow_up_question)
        self.assertEqual(result.confidence, 0)

    def test_conflict_has_exactly_one_resolution_action(self):
        positive_evidence = Evidence("ev-positive", "policy.docx", "data/policy.docx", "policy", "POLICY_REQUIREMENT", "Regodit", "p1", "MFA is required for all core systems.")
        negative_evidence = Evidence("ev-negative", "record.xlsx", "data/record.xlsx", "operational/infrastructure", "OPERATIONAL_RECORD", "Regodit", "row 2", "MFA is not currently implemented for all core systems.")
        positive = SecurityClaim("c-positive", "mfa", "required", "all core systems", True, "Regodit", "DOCUMENTED", "POLICY_REQUIREMENT", ("ev-positive",), positive_evidence.text, 0.8)
        negative = SecurityClaim("c-negative", "mfa", "implemented", "all core systems", False, "Regodit", "IMPLEMENTED", "OPERATIONAL_RECORD", ("ev-negative",), negative_evidence.text, 0.95)
        profile = SpyProfile(ProfileEvidence((positive, negative), (positive_evidence, negative_evidence)))
        item = self.items["VSQ-060"]
        result = AnalystEngine(profile=profile, retriever=lambda *args: []).investigate(item)
        self.assertEqual(result.next_action, "RESOLVE_CONFLICT")
        self.assertEqual(result.status, "CONFLICT")
        self.assertEqual(len(result.conflicts), 1)
        self.assertFalse(result.answerable)
        self.assertIsNone(result.answer)

    def test_unsupported_profile_claim_is_rejected_from_final_result(self):
        evidence = Evidence("ev-real", "policy.docx", "data/policy.docx", "policy", "POLICY_REQUIREMENT", "Regodit", "p1", "MFA is required.")
        unsupported = SecurityClaim("c-fake", "mfa", "required", "all core systems", True, "Regodit", "DOCUMENTED", "POLICY_REQUIREMENT", ("ev-fake",), "fabricated", 0.8)
        profile = SpyProfile(ProfileEvidence((unsupported,), (evidence,)))
        result = AnalystEngine(profile=profile, retriever=lambda *args: []).investigate(self.items["VSQ-060"])
        self.assertNotEqual(result.next_action, "ANSWER")
        self.assertEqual(result.claims, ())
        self.assertEqual(result.evidence_ids, ())

    def test_result_model_rejects_search_bypass(self):
        from regodit.analyst import InvestigationResult
        with self.assertRaises(ValueError):
            InvestigationResult("q", False, None, "UNKNOWN", "MARK_UNKNOWN", (), (), (), (), None, 0, False, True)


if __name__ == "__main__":
    unittest.main()
