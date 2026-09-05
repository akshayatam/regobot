"""OpenAI structured-output runtime with strict evidence-boundary validation."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from regodit.config import LLM_MODEL, LLM_PROVIDER, OPENAI_API_KEY
from regodit.models import Evidence

if TYPE_CHECKING:
    from regodit.analyst.claims import SecurityClaim

LOGGER = logging.getLogger("regodit.llm")

SYSTEM_INSTRUCTIONS = """You are the Regodit security analyst reasoning component.
Answer only from supplied Regodit evidence and persisted user-confirmed claims. If evidence is insufficient, return insufficient evidence rather than using outside knowledge.
Never treat policy requirements as proof of implementation. Never use evidence belonging only to another organization. Cite only supplied evidence IDs. Every claim's support_text must be an exact contiguous excerpt from one cited evidence item's text. Detect contradictions instead of choosing a preferred source. Return one concise follow-up question only when necessary. Return JSON matching the supplied schema and no prose outside it."""

EVIDENCE_TYPES = (
    "POLICY_REQUIREMENT", "OPERATIONAL_RECORD", "ASSESSMENT_EVIDENCE",
    "CONTRACTUAL_REQUIREMENT", "USER_CONFIRMATION", "OBSERVATION",
)
STRENGTHS = ("DOCUMENTED", "IMPLEMENTED", "OBSERVED", "USER_CONFIRMED")
STATUSES = ("VERIFIED", "USER_CONFIRMED", "UNKNOWN", "CONFLICT")

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answerable", "answer", "status", "claims", "evidence_ids", "conflicts", "missing_information", "follow_up_question"],
    "properties": {
        "answerable": {"type": "boolean"},
        "answer": {"type": ["string", "null"]},
        "status": {"type": "string", "enum": list(STATUSES)},
        "claims": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["control", "attribute", "scope", "value", "subject", "strength", "evidence_type", "evidence_ids", "support_text", "relevant_dates"],
                "properties": {
                    "control": {"type": "string"}, "attribute": {"type": "string"},
                    "scope": {"type": "string"}, "value": {"type": ["boolean", "string"]},
                    "subject": {"type": "string"}, "strength": {"type": "string", "enum": list(STRENGTHS)},
                    "evidence_type": {"type": "string", "enum": list(EVIDENCE_TYPES)},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "support_text": {"type": "string"},
                    "relevant_dates": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "conflicts": {"type": "array", "items": {"type": "string"}},
        "missing_information": {"type": "array", "items": {"type": "string"}},
        "follow_up_question": {"type": ["string", "null"]},
    },
}


class StructuredOutputError(ValueError):
    """Raised when model output fails Regodit's application-level checks."""


@dataclass(frozen=True)
class GroundedAnalysis:
    answerable: bool
    answer: str | None
    status: str
    claims: tuple["SecurityClaim", ...]
    evidence_ids: tuple[str, ...]
    conflicts: tuple[str, ...]
    missing_information: tuple[str, ...]
    follow_up_question: str | None


@dataclass(frozen=True)
class LLMRun:
    analysis: GroundedAnalysis
    model: str
    messages: tuple[dict[str, str], ...]
    raw_output: str
    latency_ms: int
    input_tokens: int
    output_tokens: int


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StructuredOutputError(f"{name} must be a non-empty string")
    return value.strip()


def validate_model_output(raw: str, evidence: list[Evidence], expected_control: str, organization: str) -> GroundedAnalysis:
    # Lazy import avoids coupling the public analyst and LLM package initializers.
    from regodit.analyst.claims import SecurityClaim, WEIGHTS, validate_claim
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StructuredOutputError("model output is not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != set(OUTPUT_SCHEMA["required"]):
        raise StructuredOutputError("model output fields do not match the required schema")
    if type(payload["answerable"]) is not bool or payload["status"] not in STATUSES:
        raise StructuredOutputError("invalid answerable or status value")
    answer = payload["answer"]
    follow_up = payload["follow_up_question"]
    if answer is not None and not isinstance(answer, str):
        raise StructuredOutputError("answer must be text or null")
    if follow_up is not None and (not isinstance(follow_up, str) or follow_up.count("?") != 1):
        raise StructuredOutputError("follow_up_question must contain exactly one question")
    for name in ("claims", "evidence_ids", "conflicts", "missing_information"):
        if not isinstance(payload[name], list):
            raise StructuredOutputError(f"{name} must be a list")
    evidence_by_id = {item.id: item for item in evidence}
    cited = tuple(dict.fromkeys(_required_string(item, "evidence ID") for item in payload["evidence_ids"]))
    if set(cited) - evidence_by_id.keys():
        raise StructuredOutputError("result cites evidence outside the supplied set")
    claims: list[SecurityClaim] = []
    for index, item in enumerate(payload["claims"]):
        if not isinstance(item, dict):
            raise StructuredOutputError(f"claim {index} must be an object")
        required = set(OUTPUT_SCHEMA["properties"]["claims"]["items"]["required"])
        if set(item) != required:
            raise StructuredOutputError(f"claim {index} fields do not match the schema")
        control = _required_string(item["control"], "claim control")
        if control != expected_control:
            raise StructuredOutputError("claim control does not match the investigation")
        subject = _required_string(item["subject"], "claim subject")
        if subject != organization:
            raise StructuredOutputError("claim subject does not match the investigated organization")
        evidence_ids = tuple(dict.fromkeys(_required_string(value, "claim evidence ID") for value in item["evidence_ids"]))
        evidence_type = item["evidence_type"]
        if evidence_type not in EVIDENCE_TYPES or item["strength"] not in STRENGTHS:
            raise StructuredOutputError("claim evidence type or strength is invalid")
        fingerprint = json.dumps([evidence_ids, control, item["attribute"], item["scope"], item["value"]], sort_keys=True)
        claim = SecurityClaim(
            id="claim-llm-" + hashlib.sha256(fingerprint.encode()).hexdigest()[:16],
            control=control,
            attribute=_required_string(item["attribute"], "claim attribute"),
            scope=_required_string(item["scope"], "claim scope"),
            value=item["value"],
            subject=subject,
            strength=item["strength"],
            evidence_type=evidence_type,
            evidence_ids=evidence_ids,
            support_text=_required_string(item["support_text"], "claim support text"),
            evidence_weight=WEIGHTS[evidence_type],
            relevant_dates=tuple(_required_string(value, "relevant date") for value in item["relevant_dates"]),
        )
        try:
            validate_claim(claim, evidence)
        except ValueError as exc:
            raise StructuredOutputError(f"unsupported claim {index}: {exc}") from exc
        claims.append(claim)
    claim_ids = {evidence_id for claim in claims for evidence_id in claim.evidence_ids}
    if not set(cited).issubset(claim_ids):
        raise StructuredOutputError("top-level evidence IDs must be cited by validated claims")
    if payload["answerable"] and (not answer or payload["status"] not in {"VERIFIED", "USER_CONFIRMED"} or not claims):
        raise StructuredOutputError("answerable results require an answer, supported claims, and resolved status")
    if not payload["answerable"] and answer is not None:
        raise StructuredOutputError("unanswerable results cannot contain an answer")
    if payload["status"] == "CONFLICT" and not payload["conflicts"]:
        raise StructuredOutputError("CONFLICT requires a contradiction description")
    return GroundedAnalysis(
        payload["answerable"], answer.strip() if answer else None, payload["status"], tuple(claims), cited,
        tuple(_required_string(value, "conflict") for value in payload["conflicts"]),
        tuple(_required_string(value, "missing information") for value in payload["missing_information"]),
        follow_up.strip() if follow_up else None,
    )


class OpenAIAnalyst:
    """One-provider runtime; disabled cleanly when no OpenAI credential is configured."""

    def __init__(self, client: Any | None = None, model: str = LLM_MODEL):
        self.model = model
        self._client = client
        if self._client is None and self.enabled:
            try:
                from openai import OpenAI

                self._client = OpenAI(api_key=OPENAI_API_KEY)
            except Exception as exc:
                LOGGER.warning("OpenAI runtime could not initialize: %s", type(exc).__name__)
                self._client = None

    @property
    def enabled(self) -> bool:
        return (LLM_PROVIDER == "openai" and bool(OPENAI_API_KEY)) or self._client is not None

    def analyze(self, *, question: str, question_id: str, control: str, organization: str, evidence: list[Evidence]) -> LLMRun:
        if not self.enabled or self._client is None:
            raise RuntimeError("OpenAI reasoning is not configured")
        compact_evidence = [
            {
                "id": item.id, "source_name": item.source_name, "source_category": item.source_category,
                "evidence_type": item.evidence_type, "organization": item.organization,
                "location": item.location, "text": item.text,
            }
            for item in evidence
        ]
        user_payload = json.dumps({
            "question_id": question_id, "question": question, "normalized_control": control,
            "organization": organization, "evidence": compact_evidence,
        }, ensure_ascii=False)
        messages = (
            {"role": "system", "content": SYSTEM_INSTRUCTIONS},
            {"role": "user", "content": user_payload},
        )
        started = time.perf_counter()
        response = self._client.responses.create(
            model=self.model,
            input=list(messages),
            text={"format": {"type": "json_schema", "name": "regodit_investigation", "strict": True, "schema": OUTPUT_SCHEMA}},
        )
        latency_ms = round((time.perf_counter() - started) * 1000)
        raw = response.output_text
        analysis = validate_model_output(raw, evidence, control, organization)
        usage = getattr(response, "usage", None)
        return LLMRun(
            analysis, self.model, messages, raw, latency_ms,
            int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0),
        )
