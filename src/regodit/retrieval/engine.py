"""Deterministic hybrid retrieval over the reusable evidence repository."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable

from regodit.ingestion import load_repository
from regodit.models import Evidence

TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")
STOPWORDS = {
    "a", "all", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for", "from", "have",
    "how", "in", "is", "it", "of", "on", "or", "that", "the", "their", "this", "to", "what", "when",
    "where", "which", "who", "will", "with", "your",
}
CONTROL_TERMS = {
    "mfa": ("mfa", "multi-factor", "multifactor", "authentication", "otp", "source-code", "github"),
    "access_control": ("access", "iam", "authorization", "least privilege", "role-based"),
    "encryption_at_rest": ("encryption at rest", "encrypted at rest", "aes-256", "kms"),
    "encryption_in_transit": ("encryption in transit", "encrypted in transit", "tls", "https"),
    "backups": ("backup", "snapshot", "restore", "recovery"),
    "vulnerability_management": ("vulnerability", "scanning", "scan", "remediation"),
    "patching": ("patch", "remediation", "critical", "high-risk"),
    "incident_response": ("incident", "security event", "breach", "response plan"),
    "access_reviews": ("access review", "recertification", "user access", "privilege review"),
}


def tokenize(text: str) -> list[str]:
    return [token for token in TOKEN_RE.findall(text.casefold()) if token not in STOPWORDS and len(token) > 1]


def _trigrams(text: str) -> set[str]:
    normalized = " ".join(tokenize(text))
    return {normalized[index:index + 3] for index in range(max(0, len(normalized) - 2))}


def _normalize_control(control: str | None) -> str | None:
    if not control:
        return None
    return re.sub(r"[^a-z0-9]+", "_", control.casefold()).strip("_")


@dataclass(frozen=True)
class RetrievalHit:
    evidence: Evidence
    score: float
    lexical_score: float
    semantic_score: float
    control_score: float
    metadata_score: float
    matched_terms: tuple[str, ...]

    def explanation(self) -> str:
        terms = ", ".join(self.matched_terms) or "character similarity only"
        return (
            f"lexical={self.lexical_score:.3f}, semantic={self.semantic_score:.3f}, "
            f"control={self.control_score:.3f}, metadata={self.metadata_score:.3f}; matched: {terms}"
        )


class HybridRetriever:
    """Small in-memory BM25 + character-similarity index with metadata filtering."""

    def __init__(self, evidence: Iterable[Evidence]):
        self.evidence = list(evidence)
        self.tokens = [tokenize(self._searchable(record)) for record in self.evidence]
        self.term_frequencies = [Counter(tokens) for tokens in self.tokens]
        self.doc_frequencies: Counter[str] = Counter()
        for tokens in self.tokens:
            self.doc_frequencies.update(set(tokens))
        self.average_length = sum(map(len, self.tokens)) / max(len(self.tokens), 1)
        self.trigrams = [_trigrams(self._searchable(record)) for record in self.evidence]

    @staticmethod
    def _searchable(record: Evidence) -> str:
        heading = str(record.metadata.get("section_heading") or "")
        return f"{record.source_name} {record.source_category} {heading} {record.text}"

    def _bm25(self, index: int, query_terms: list[str]) -> float:
        frequencies = self.term_frequencies[index]
        length = len(self.tokens[index])
        score = 0.0
        for term in set(query_terms):
            frequency = frequencies[term]
            if not frequency:
                continue
            document_frequency = self.doc_frequencies[term]
            inverse_frequency = math.log(1 + (len(self.evidence) - document_frequency + 0.5) / (document_frequency + 0.5))
            denominator = frequency + 1.5 * (1 - 0.75 + 0.75 * length / max(self.average_length, 1))
            score += inverse_frequency * frequency * 2.5 / denominator
        return score

    def search(
        self,
        query: str,
        control: str | None = None,
        organization: str = "Regodit",
        top_k: int = 8,
        source_categories: set[str] | None = None,
    ) -> list[RetrievalHit]:
        if not query.strip() or top_k < 1:
            return []
        normalized_control = _normalize_control(control)
        expanded = " ".join(CONTROL_TERMS.get(normalized_control or "", ()))
        query_terms = tokenize(f"{query} {expanded}")
        query_trigrams = _trigrams(query)
        candidates: list[tuple[int, float, float, float, float, tuple[str, ...]]] = []
        for index, record in enumerate(self.evidence):
            if source_categories and record.source_category not in source_categories:
                continue
            # A Regodit investigation excludes known unrelated entities. Unknown and multiple remain inspectable.
            if organization and record.organization not in {organization, "unknown", "multiple"}:
                continue
            lexical_raw = self._bm25(index, query_terms)
            lexical = lexical_raw / (lexical_raw + 5.0) if lexical_raw else 0.0
            union = query_trigrams | self.trigrams[index]
            semantic = len(query_trigrams & self.trigrams[index]) / len(union) if union else 0.0
            searchable = self._searchable(record).casefold()
            control_phrases = CONTROL_TERMS.get(normalized_control or "", ())
            control_score = min(1.0, sum(phrase in searchable for phrase in control_phrases) / 2) if control_phrases else 0.0
            if record.organization == organization:
                entity_score = 1.0
            elif record.organization == "multiple":
                entity_score = 0.35
            else:
                entity_score = 0.15
            quality = {
                "OPERATIONAL_RECORD": 1.0,
                "ASSESSMENT_EVIDENCE": 1.0,
                "POLICY_REQUIREMENT": 0.85,
                "CONTRACTUAL_REQUIREMENT": 0.75,
                "USER_CONFIRMATION": 0.85,
                "OBSERVATION": 0.25,
            }[record.evidence_type]
            metadata = entity_score * quality
            score = 0.45 * lexical + 0.15 * semantic + 0.20 * control_score + 0.20 * metadata
            matched = tuple(sorted(set(query_terms) & set(self.tokens[index])))
            if score > 0.04 and (matched or semantic > 0.01):
                candidates.append((index, score, lexical, semantic, control_score, matched))
        candidates.sort(key=lambda row: (-row[1], self.evidence[row[0]].id))
        return [
            RetrievalHit(self.evidence[index], score, lexical, semantic, control_score,
                         (1.0 if self.evidence[index].organization == organization else 0.35 if self.evidence[index].organization == "multiple" else 0.15)
                         * {"OPERATIONAL_RECORD": 1.0, "ASSESSMENT_EVIDENCE": 1.0, "POLICY_REQUIREMENT": 0.85,
                            "CONTRACTUAL_REQUIREMENT": 0.75, "USER_CONFIRMATION": 0.85, "OBSERVATION": 0.25}[self.evidence[index].evidence_type],
                         matched)
            for index, score, lexical, semantic, control_score, matched in candidates[:top_k]
        ]


_DEFAULT_RETRIEVER: HybridRetriever | None = None


def default_retriever() -> HybridRetriever:
    global _DEFAULT_RETRIEVER
    if _DEFAULT_RETRIEVER is None:
        _DEFAULT_RETRIEVER = HybridRetriever(load_repository())
    return _DEFAULT_RETRIEVER


def retrieve_evidence(query: str, control: str | None = None, organization: str = "Regodit", top_k: int = 8) -> list[Evidence]:
    """Return full Evidence objects; known non-target organizations are filtered out."""
    return [hit.evidence for hit in default_retriever().search(query, control, organization, top_k)]
