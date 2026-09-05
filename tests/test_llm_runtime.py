import json
import unittest
from types import SimpleNamespace

from regodit.analyst import AnalystEngine, ProfileEvidence
from regodit.analyst.claims import SecurityClaim
from regodit.llm.runtime import OpenAIAnalyst, StructuredOutputError, validate_model_output
from regodit.models import Evidence, QuestionnaireItem
from regodit.observability import PrismObserver


EVIDENCE = Evidence(
    "ev-mfa", "assessment.docx", "data/assessment.docx", "assessment",
    "ASSESSMENT_EVIDENCE", "Regodit", "paragraph 4",
    "We confirmed MFA is implemented for production access.",
)


def payload(**overrides):
    value = {
        "answerable": True,
        "answer": "Yes — MFA is implemented for production access.",
        "status": "VERIFIED",
        "claims": [{
            "control": "mfa", "attribute": "implemented", "scope": "production access",
            "value": True, "subject": "Regodit", "strength": "IMPLEMENTED",
            "evidence_type": "ASSESSMENT_EVIDENCE", "evidence_ids": ["ev-mfa"],
            "support_text": EVIDENCE.text, "relevant_dates": [],
        }],
        "evidence_ids": ["ev-mfa"], "conflicts": [], "missing_information": [],
        "follow_up_question": None,
    }
    value.update(overrides)
    return value


class FakeResponses:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_text=json.dumps(self.result),
            usage=SimpleNamespace(input_tokens=120, output_tokens=60),
        )


class LLMRuntimeTests(unittest.TestCase):
    def test_model_output_rejects_nonexistent_evidence(self):
        invalid = payload(evidence_ids=["ev-invented"])
        with self.assertRaises(StructuredOutputError):
            validate_model_output(json.dumps(invalid), [EVIDENCE], "mfa", "Regodit")

    def test_model_output_rejects_policy_as_implementation(self):
        policy = Evidence("ev-policy", "policy.docx", "data/policy.docx", "policy", "POLICY_REQUIREMENT", "Regodit", "p1", "MFA must be implemented.")
        invalid = payload(claims=[{
            **payload()["claims"][0], "evidence_ids": ["ev-policy"],
            "evidence_type": "POLICY_REQUIREMENT", "support_text": policy.text,
        }], evidence_ids=["ev-policy"])
        with self.assertRaises(StructuredOutputError):
            validate_model_output(json.dumps(invalid), [policy], "mfa", "Regodit")

    def test_answerable_output_rejects_unresolved_missing_information(self):
        with self.assertRaises(StructuredOutputError):
            validate_model_output(
                json.dumps(payload(missing_information=["audit period"])), [EVIDENCE], "mfa", "Regodit"
            )

    def test_real_engine_uses_strict_structured_model_path(self):
        responses = FakeResponses(payload())
        runtime = OpenAIAnalyst(SimpleNamespace(responses=responses), model="test-model")
        item = QuestionnaireItem("Q-1", "1", "Access", "Is MFA implemented?", "mfa", "test.xlsx", "Sheet1", 1, "B1")
        result = AnalystEngine(retriever=lambda *args: [EVIDENCE], model_runtime=runtime).investigate(item)
        self.assertEqual(result.status, "VERIFIED")
        self.assertEqual(result.answer, "Yes — MFA is implemented for production access.")
        self.assertEqual(len(responses.calls), 1)
        request = responses.calls[0]
        self.assertTrue(request["text"]["format"]["strict"])
        self.assertIn("Answer only from supplied Regodit evidence", request["input"][0]["content"])

    def test_invalid_model_output_falls_back_without_crashing(self):
        responses = FakeResponses(payload(evidence_ids=["ev-invented"]))
        runtime = OpenAIAnalyst(SimpleNamespace(responses=responses), model="test-model")
        item = QuestionnaireItem("Q-1", "1", "Access", "Is MFA implemented?", "mfa", "test.xlsx", "Sheet1", 1, "B1")
        result = AnalystEngine(retriever=lambda *args: [EVIDENCE], model_runtime=runtime).investigate(item)
        self.assertEqual(result.status, "VERIFIED")
        self.assertNotEqual(result.answer, payload()["answer"])

    def test_model_omission_cannot_erase_deterministic_conflict(self):
        positive = Evidence("ev-positive", "policy.docx", "data/policy.docx", "policy", "POLICY_REQUIREMENT", "Regodit", "p1", "MFA is required for all core systems.")
        negative_evidence = Evidence("ev-negative", "confirmation", "profile://negative", "other", "USER_CONFIRMATION", "Regodit", "confirmation", "One active account may not have MFA enabled.")
        negative = SecurityClaim("c-negative", "mfa", "implemented", "organization-wide/unspecified", False, "Regodit", "USER_CONFIRMED", "USER_CONFIRMATION", ("ev-negative",), negative_evidence.text, 0.85)

        class Profile:
            def lookup(self, *_):
                return ProfileEvidence((negative,), (negative_evidence,))

        empty = payload(answerable=False, answer=None, status="UNKNOWN", claims=[], evidence_ids=[], missing_information=["implementation"], follow_up_question="Is MFA currently implemented?")
        runtime = OpenAIAnalyst(SimpleNamespace(responses=FakeResponses(empty)), model="test-model")
        item = QuestionnaireItem("Q-1", "1", "Access", "Is MFA required?", "mfa", "test.xlsx", "Sheet1", 1, "B1")
        result = AnalystEngine(profile=Profile(), retriever=lambda *args: [positive], model_runtime=runtime).investigate(item)
        self.assertEqual(result.status, "CONFLICT")
        self.assertTrue(result.conflicts)

    def test_prism_trajectory_preserves_session_and_pipeline(self):
        class FakePrism:
            def __init__(self):
                self.trajectories = []

            def submit_trajectory(self, steps, **kwargs):
                self.trajectories.append((steps, kwargs))
                return {"id": "trajectory-1"}

        observer = PrismObserver("conversation-123")
        observer._client = FakePrism()
        observer.investigation(
            request_id="request-1", question_id="Q-1", control="mfa", model="test-model",
            retrieval_ms=4, model_ms=12, evidence_count=3, status="CONFLICT",
            conflict=True, follow_up=True,
        )
        steps, details = observer._client.trajectories[0]
        self.assertEqual(details["conversation_id"], "conversation-123")
        self.assertEqual([step["step_type"] for step in steps], ["tool_call", "reasoning", "final_answer"])
        self.assertEqual(details["agent_name"], "regodit-security-analyst")
        self.assertFalse(details["async_send"])
        self.assertEqual(observer.trajectory_ids, ["trajectory-1"])


if __name__ == "__main__":
    unittest.main()
