"""Validated shared models for questionnaire, evidence, and claims."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

AnswerStatus = Literal["VERIFIED", "USER_CONFIRMED", "UNKNOWN", "CONFLICT"]
EvidenceType = Literal[
    "POLICY_REQUIREMENT",
    "OPERATIONAL_RECORD",
    "ASSESSMENT_EVIDENCE",
    "CONTRACTUAL_REQUIREMENT",
    "USER_CONFIRMATION",
    "OBSERVATION",
]


def _required(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


@dataclass(frozen=True)
class QuestionnaireItem:
    id: str
    source_question_id: str
    category: str
    question: str
    normalized_control: str
    source_file: str
    source_sheet: str
    source_row: int
    source_cell: str
    answer: str | None = None
    comments: str | None = None
    evidence_reference: str | None = None
    status: AnswerStatus = "UNKNOWN"
    confidence: float = 0.0
    evidence_ids: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for name in ("id", "source_question_id", "category", "question", "normalized_control"):
            _required(getattr(self, name), name)
        if self.source_row < 1:
            raise ValueError("source_row must be positive")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if self.status == "UNKNOWN" and self.confidence != 0.0:
            raise ValueError("an unanswered UNKNOWN item must have zero confidence")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Evidence:
    id: str
    source_name: str
    source_path: str
    source_category: str
    evidence_type: EvidenceType
    organization: str
    location: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("id", "source_name", "source_path", "source_category", "evidence_type", "organization", "location"):
            _required(getattr(self, name), name)
        if not isinstance(self.text, str):
            raise ValueError("text must be a string")


@dataclass(frozen=True)
class Claim:
    id: str
    control: str
    attribute: str
    value: str
    scope: str
    subject: str
    status: AnswerStatus
    confidence: float
    evidence_ids: list[str]
    created_at: str
    updated_at: str
    supersedes: str | None = None

    def __post_init__(self) -> None:
        for name in ("id", "control", "attribute", "value", "scope", "subject", "status", "created_at", "updated_at"):
            _required(getattr(self, name), name)
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if self.status == "VERIFIED" and not self.evidence_ids:
            raise ValueError("VERIFIED claims require evidence IDs")
