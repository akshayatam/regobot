"""Evidence-first decision engine for questionnaire investigations."""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Literal, Protocol

from regodit.analyst.claims import SecurityClaim, extract_claims, validate_claim
from regodit.models import AnswerStatus, Evidence, QuestionnaireItem
from regodit.retrieval import retrieve_evidence
from regodit.llm import LLMRun, OpenAIAnalyst, StructuredOutputError
from regodit.observability import PrismObserver

LOGGER = logging.getLogger("regodit.analyst")
NextAction = Literal["ANSWER", "ASK_FOLLOW_UP", "RESOLVE_CONFLICT", "MARK_UNKNOWN"]


def normalize_control(value: str) -> str:
    aliases = {
        "mfa": "mfa",
        "multi_factor_authentication": "mfa",
        "encryption_at_rest": "encryption_at_rest",
        "encryption_in_transit": "encryption_in_transit",
        "access_reviews": "access_reviews",
        "vulnerability_management": "vulnerability_management",
        "least_privilege": "least_privilege",
    }
    normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    return aliases.get(normalized, normalized)


@dataclass(frozen=True)
class ProfileEvidence:
    """Read-only security-profile response used during an investigation."""

    claims: tuple[SecurityClaim, ...] = ()
    evidence: tuple[Evidence, ...] = ()


class SecurityProfileReader(Protocol):
    def lookup(self, control: str, organization: str) -> ProfileEvidence: ...


class EmptySecurityProfile:
    def lookup(self, control: str, organization: str) -> ProfileEvidence:
        return ProfileEvidence()


@dataclass(frozen=True)
class Conflict:
    claim_ids: tuple[str, str]
    evidence_ids: tuple[str, ...]
    description: str
    claim_values: tuple[bool | str, bool | str] = ("", "")

    def __post_init__(self) -> None:
        if len(self.claim_ids) != 2 or not all(self.claim_ids):
            raise ValueError("a conflict requires exactly two claim IDs")
        if not self.evidence_ids:
            raise ValueError("a conflict requires evidence IDs")


@dataclass(frozen=True)
class InvestigationResult:
    question_id: str
    answerable: bool
    answer: str | None
    status: AnswerStatus
    next_action: NextAction
    claims: tuple[SecurityClaim, ...]
    evidence_ids: tuple[str, ...]
    conflicts: tuple[Conflict, ...]
    missing_information: tuple[str, ...]
    follow_up_question: str | None
    confidence: float
    searched_profile: bool
    searched_evidence: bool

    def __post_init__(self) -> None:
        if not self.question_id:
            raise ValueError("question_id is required")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if not self.searched_profile or not self.searched_evidence:
            raise ValueError("investigation must search profile and evidence")
        if self.next_action == "ANSWER":
            if not self.answerable or not self.answer or self.status not in {"VERIFIED", "USER_CONFIRMED"}:
                raise ValueError("ANSWER requires a supported answer and resolved status")
            if not self.claims or not self.evidence_ids or self.follow_up_question:
                raise ValueError("ANSWER requires claims/evidence and no follow-up")
        elif self.next_action == "ASK_FOLLOW_UP":
            if self.answerable or self.answer is not None or not self.follow_up_question or not self.missing_information:
                raise ValueError("ASK_FOLLOW_UP requires missing information and one question")
        elif self.next_action == "RESOLVE_CONFLICT":
            if self.answerable or self.answer is not None or not self.conflicts or not self.follow_up_question:
                raise ValueError("RESOLVE_CONFLICT requires conflicts and a clarification question")
        elif self.next_action == "MARK_UNKNOWN":
            if self.answerable or self.answer is not None or self.follow_up_question is not None or self.status != "UNKNOWN":
                raise ValueError("MARK_UNKNOWN cannot include an answer or follow-up")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["claims"] = [claim.to_dict() for claim in self.claims]
        result["conflicts"] = [asdict(conflict) for conflict in self.conflicts]
        return result


@dataclass(frozen=True)
class QuestionIntent:
    control: str
    answer_kind: Literal["boolean", "frequency", "location", "list", "description", "attachment", "unknown"]
    required_strength: Literal["DOCUMENTED", "IMPLEMENTED", "ANY"]
    missing_fields: tuple[str, ...]


def determine_intent(item: QuestionnaireItem) -> QuestionIntent:
    question = item.question.strip()
    lower = question.casefold()
    control = normalize_control(item.normalized_control)
    if control == "unclassified" or not re.search(r"[a-zA-Z]{3}", question):
        return QuestionIntent(control, "unknown", "ANY", ("question wording",))
    if "how frequently" in lower or "how often" in lower or "cadence" in lower:
        return QuestionIntent(control, "frequency", "IMPLEMENTED", ("frequency",))
    if re.search(r"\bwhere\b|what countr|outside the united states|stored on site", lower):
        return QuestionIntent(control, "location", "IMPLEMENTED", ("storage location",))
    if lower.startswith("please list") or "who has access" in lower:
        return QuestionIntent(control, "list", "IMPLEMENTED", ("authorized personnel",))
    if "please provide" in lower or "please attach" in lower or "submit a" in lower:
        return QuestionIntent(control, "attachment", "ANY", ("requested document",))
    if lower.startswith("what") or "describe" in lower:
        return QuestionIntent(control, "description", "ANY", ("description",))
    documented = any(marker in lower for marker in ("require", "policy", "procedure", "plan in place", "program"))
    return QuestionIntent(control, "boolean", "DOCUMENTED" if documented else "IMPLEMENTED", ("yes/no answer",))


def _scopes_overlap(first: str, second: str) -> bool:
    broad = {"organization-wide/unspecified", "all core systems"}
    return first == second or first in broad or second in broad


def detect_conflicts(claims: Iterable[SecurityClaim]) -> tuple[Conflict, ...]:
    values = list(claims)
    conflicts: list[Conflict] = []
    seen: set[tuple[str, str]] = set()
    for index, first in enumerate(values):
        for second in values[index + 1:]:
            if first.control != second.control or first.value == second.value or not _scopes_overlap(first.scope, second.scope):
                continue
            # A requirement and a contrary implementation record is a real control gap; matching requirements are not implementation proof.
            compatible_attributes = first.attribute == second.attribute or {first.attribute, second.attribute} <= {"required", "implemented", "observed"}
            if not compatible_attributes:
                continue
            pair = tuple(sorted((first.id, second.id)))
            if pair in seen:
                continue
            seen.add(pair)
            evidence_ids = tuple(dict.fromkeys(first.evidence_ids + second.evidence_ids))
            conflicts.append(Conflict(
                claim_ids=(first.id, second.id), evidence_ids=evidence_ids,
                description=f"The evidence disagrees for {first.control} in scope {first.scope}/{second.scope}: {first.value!r} versus {second.value!r}.",
                claim_values=(first.value, second.value),
            ))
    return tuple(conflicts)


def _sufficient_claims(intent: QuestionIntent, claims: Iterable[SecurityClaim]) -> tuple[SecurityClaim, ...]:
    candidates = [claim for claim in claims if claim.control == intent.control]
    if intent.answer_kind == "boolean":
        allowed = {"required"} if intent.required_strength == "DOCUMENTED" else {"implemented"}
        return tuple(claim for claim in candidates if claim.attribute in allowed and isinstance(claim.value, bool))
    if intent.answer_kind == "frequency":
        return tuple(claim for claim in candidates if claim.attribute == "cadence" and isinstance(claim.value, str))
    # Free-text/location/list/document requests require a specifically extracted attribute; generic booleans are insufficient.
    expected = {"location": "location", "list": "authorized_personnel", "description": "description", "attachment": "document"}.get(intent.answer_kind)
    return tuple(claim for claim in candidates if claim.attribute == expected and isinstance(claim.value, str))


def _answer(intent: QuestionIntent, claims: tuple[SecurityClaim, ...]) -> str:
    values = list(dict.fromkeys(claim.value for claim in claims))
    if intent.answer_kind == "boolean":
        value = values[0]
        qualifier = "documented as required" if intent.required_strength == "DOCUMENTED" else "verified as implemented"
        return f"{'Yes' if value is True else 'No'} — {qualifier}."
    if intent.answer_kind == "frequency":
        return str(values[0]).capitalize()
    return "; ".join(str(value) for value in values)


def _follow_up(item: QuestionnaireItem, intent: QuestionIntent) -> str | None:
    control = intent.control.replace("_", " ")
    templates = {
        "frequency": f"How frequently is {control} performed in the current production environment?",
        "location": "In which country or countries is the relevant customer data currently stored?",
        "list": "Which currently authorized personnel have access, including their names and work email addresses?",
        "attachment": f"Can you provide the current document requested for {control}?",
        "description": f"What is the current documented process for {control}?",
        "boolean": f"Is {control} currently implemented for the scope asked about?",
    }
    return templates.get(intent.answer_kind)


class AnalystEngine:
    def __init__(
        self,
        profile: SecurityProfileReader | None = None,
        retriever: Callable[[str, str | None, str, int], list[Evidence]] = retrieve_evidence,
        top_k: int = 16,
        model_runtime: OpenAIAnalyst | None = None,
        session_id: str | None = None,
        observer: PrismObserver | None = None,
    ):
        self.profile = profile or EmptySecurityProfile()
        self.retriever = retriever
        self.top_k = top_k
        self.model_runtime = model_runtime or OpenAIAnalyst()
        self.session_id = session_id or str(uuid.uuid4())
        self.observer = observer or PrismObserver(self.session_id)

    def flush_traces(self) -> None:
        self.observer.flush()

    def investigate(self, item: QuestionnaireItem, organization: str = "Regodit") -> InvestigationResult:
        intent = determine_intent(item)
        LOGGER.info("Checking security profile for %s (%s)", item.id, intent.control)
        profile_result = self.profile.lookup(intent.control, organization)
        LOGGER.info("Retrieving evidence for %s before choosing any next action", item.id)
        retrieval_started = time.perf_counter()
        retrieved = self.retriever(item.question, intent.control, organization, self.top_k)
        retrieval_ms = round((time.perf_counter() - retrieval_started) * 1000)
        all_evidence = list(profile_result.evidence) + retrieved
        evidence_by_id = {evidence.id: evidence for evidence in all_evidence}
        request_id = str(uuid.uuid4())
        model_run: LLMRun | None = None
        model_attempt: dict[str, Any] | None = None
        if self.model_runtime.enabled and intent.answer_kind != "unknown":
            try:
                model_run = self.model_runtime.analyze(
                    question=item.question, question_id=item.id, control=intent.control,
                    organization=organization, evidence=all_evidence,
                    answer_kind=intent.answer_kind, required_strength=intent.required_strength,
                )
                model_attempt = self.model_runtime.last_attempt
                self.observer.model_call(
                    request_id=request_id, model=model_run.model, messages=list(model_run.messages),
                    output=model_run.raw_output, latency_ms=model_run.latency_ms,
                    input_tokens=model_run.input_tokens, output_tokens=model_run.output_tokens,
                    metadata={
                        "question_id": item.id, "normalized_control": intent.control,
                        "retrieved_evidence_count": len(all_evidence),
                        "evidence_source_categories": sorted({record.source_category for record in all_evidence}),
                    },
                )
            except Exception as exc:
                # Model/network/schema failures must not bypass evidence checks or crash the application.
                LOGGER.warning("Grounded model analysis failed for %s; using deterministic fallback: %s: %s", item.id, type(exc).__name__, exc)
                attempt = self.model_runtime.last_attempt
                model_attempt = attempt
                if attempt:
                    self.observer.model_call(
                        request_id=request_id, model=self.model_runtime.model, messages=list(attempt["messages"]),
                        output=attempt["raw_output"], latency_ms=attempt["latency_ms"],
                        input_tokens=attempt["input_tokens"], output_tokens=attempt["output_tokens"],
                        metadata={
                            "question_id": item.id, "normalized_control": intent.control,
                            "retrieved_evidence_count": len(all_evidence), "structured_output_valid": False,
                            "validation_error_type": type(exc).__name__,
                        },
                    )
                model_run = None
        # The model may enrich interpretation, but it may not erase conservative evidence claims
        # and thereby hide a contradiction. Merge both validated sources by stable claim content.
        deterministic_claims = list(extract_claims(retrieved, intent.control).claims)
        model_claims = list(model_run.analysis.claims) if model_run else []
        extracted_claims = []
        seen_claims: set[tuple[Any, ...]] = set()
        for claim in deterministic_claims + model_claims:
            key = (claim.control, claim.attribute, claim.scope, claim.value, claim.evidence_ids, claim.support_text)
            if key not in seen_claims:
                seen_claims.add(key)
                extracted_claims.append(claim)
        claims = list(profile_result.claims) + list(extracted_claims)
        # Every claim, including injected profile claims, must remain backed by evidence available to this investigation.
        valid_claims: list[SecurityClaim] = []
        for claim in claims:
            try:
                validate_claim(claim, evidence_by_id.values())
            except ValueError as exc:
                LOGGER.warning("Rejected unsupported claim %s: %s", claim.id, exc)
            else:
                valid_claims.append(claim)
        conflicts = detect_conflicts(valid_claims)
        cited = tuple(dict.fromkeys(eid for claim in valid_claims for eid in claim.evidence_ids))
        if conflicts:
            model_follow_up = model_run.analysis.follow_up_question if model_run and model_run.analysis.status == "CONFLICT" else None
            result = InvestigationResult(
                item.id, False, None, "CONFLICT", "RESOLVE_CONFLICT", tuple(valid_claims), cited, conflicts,
                ("conflict resolution",),
                model_follow_up or f"The available evidence disagrees about {intent.control.replace('_', ' ')}. Which statement reflects the current situation?",
                round(max((claim.evidence_weight for claim in valid_claims), default=0) * 0.5, 3), True, True,
            )
        else:
            sufficient = _sufficient_claims(intent, valid_claims)
            if sufficient:
                sufficient_ids = tuple(dict.fromkeys(eid for claim in sufficient for eid in claim.evidence_ids))
                status: AnswerStatus = "USER_CONFIRMED" if all(c.strength == "USER_CONFIRMED" for c in sufficient) else "VERIFIED"
                model_answer = None
                if model_run and model_run.analysis.answerable and model_run.analysis.status == status:
                    if set(model_run.analysis.evidence_ids).issubset(sufficient_ids):
                        model_answer = model_run.analysis.answer
                result = InvestigationResult(
                    item.id, True, model_answer or _answer(intent, sufficient), status, "ANSWER", sufficient, sufficient_ids, (), (), None,
                    max(claim.evidence_weight for claim in sufficient), True, True,
                )
            else:
                model_missing = model_run.analysis.missing_information if model_run and model_run.analysis.status == "UNKNOWN" else ()
                model_follow_up = model_run.analysis.follow_up_question if model_run and model_run.analysis.status == "UNKNOWN" else None
                follow_up = model_follow_up or _follow_up(item, intent)
                if follow_up:
                    result = InvestigationResult(
                        item.id, False, None, "UNKNOWN", "ASK_FOLLOW_UP", tuple(valid_claims), cited, (),
                        model_missing or intent.missing_fields, follow_up, 0.0, True, True,
                    )
                else:
                    result = InvestigationResult(
                        item.id, False, None, "UNKNOWN", "MARK_UNKNOWN", tuple(valid_claims), cited, (),
                        model_missing or intent.missing_fields, None, 0.0, True, True,
                    )
        if model_attempt:
            self.observer.investigation(
                request_id=request_id, question_id=item.id, control=intent.control, model=self.model_runtime.model,
                retrieval_ms=retrieval_ms, model_ms=model_attempt["latency_ms"], evidence_count=len(all_evidence),
                status=result.status, conflict=bool(result.conflicts), follow_up=bool(result.follow_up_question),
            )
        return result


def investigate(item: QuestionnaireItem, organization: str = "Regodit") -> InvestigationResult:
    return AnalystEngine().investigate(item, organization)
