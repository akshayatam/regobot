import unittest

from regodit.models import Evidence
from regodit.analyst.claims import SecurityClaim, extract_claims, validate_claim
from regodit.retrieval import HybridRetriever, retrieve_evidence


class RetrievalClaimTests(unittest.TestCase):
    def test_mfa_retrieval_returns_real_regodit_evidence_with_provenance(self):
        evidence = retrieve_evidence("Is MFA required for GitHub?", control="mfa", top_k=8)
        self.assertTrue(evidence)
        self.assertTrue(any("multi-factor authentication is required" in item.text.casefold() for item in evidence))
        self.assertTrue(all(item.organization != "Solsphere" for item in evidence))
        self.assertTrue(all(item.id and item.source_path and item.location for item in evidence))

    def test_claims_distinguish_policy_requirement_and_implementation(self):
        evidence = retrieve_evidence("Is MFA required and implemented for production access?", control="mfa", top_k=12)
        result = extract_claims(evidence, "mfa")
        self.assertFalse(result.insufficient_evidence)
        self.assertTrue(any(c.strength == "DOCUMENTED" and c.evidence_type == "POLICY_REQUIREMENT" for c in result.claims))
        self.assertTrue(any(c.strength == "IMPLEMENTED" and c.evidence_type == "ASSESSMENT_EVIDENCE" for c in result.claims))
        self.assertTrue(all(c.evidence_ids[0] in {item.id for item in evidence} for c in result.claims))

    def test_policy_never_becomes_implementation(self):
        evidence = retrieve_evidence("Is MFA required?", control="mfa", top_k=20)
        result = extract_claims([item for item in evidence if item.evidence_type == "POLICY_REQUIREMENT"], "mfa")
        self.assertTrue(result.claims)
        self.assertTrue(all(c.strength == "DOCUMENTED" for c in result.claims))

    def test_unsupported_evidence_id_and_text_are_rejected(self):
        evidence = retrieve_evidence("MFA", control="mfa", top_k=2)
        base = dict(id="claim-test", control="mfa", attribute="required", scope="GitHub", value=True,
                    subject="Regodit", strength="DOCUMENTED", evidence_type="POLICY_REQUIREMENT",
                    evidence_weight=0.8, relevant_dates=())
        with self.assertRaises(ValueError):
            validate_claim(SecurityClaim(evidence_ids=("ev-not-retrieved",), support_text="made up", **base), evidence)
        with self.assertRaises(ValueError):
            validate_claim(SecurityClaim(evidence_ids=(evidence[0].id,), support_text="made up", **base), evidence)

    def test_insufficient_evidence_is_valid(self):
        result = extract_claims([], "mfa")
        self.assertTrue(result.insufficient_evidence)
        self.assertEqual(result.claims, ())

    def test_entity_filter_prevents_solsphere_noise(self):
        regodit = Evidence("ev-r", "r.docx", "data/r.docx", "policy", "POLICY_REQUIREMENT", "Regodit", "p1", "MFA is required.")
        solsphere = Evidence("ev-s", "s.docx", "data/s.docx", "operational/infrastructure", "OPERATIONAL_RECORD", "Solsphere", "p1", "MFA MFA MFA GitHub authentication.")
        retriever = HybridRetriever([solsphere, regodit])
        hits = retriever.search("Is MFA required for GitHub?", "mfa", "Regodit", 8)
        self.assertEqual([hit.evidence.id for hit in hits], ["ev-r"])

    def test_retrieval_explanation_exposes_score_components(self):
        hits = HybridRetriever(retrieve_evidence("backups", "backups", top_k=5)).search("daily backups", "backups", "Regodit", 2)
        self.assertTrue(hits)
        explanation = hits[0].explanation()
        self.assertIn("lexical=", explanation)
        self.assertIn("semantic=", explanation)
        self.assertIn("metadata=", explanation)


if __name__ == "__main__":
    unittest.main()
