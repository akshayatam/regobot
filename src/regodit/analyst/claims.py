"""Conservative, validated claim extraction from retrieved evidence."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Literal

from regodit.models import Evidence, EvidenceType

ClaimStrength = Literal["DOCUMENTED", "IMPLEMENTED", "OBSERVED", "USER_CONFIRMED"]
WEIGHTS: dict[str, float] = {
    "OPERATIONAL_RECORD": 0.95,
    "ASSESSMENT_EVIDENCE": 0.95,
    "POLICY_REQUIREMENT": 0.80,
    "CONTRACTUAL_REQUIREMENT": 0.75,
    "USER_CONFIRMATION": 0.85,
    "OBSERVATION": 0.40,
}
CONTROL_PATTERNS = {
    "mfa": ("mfa", "multi-factor authentication", "multifactor authentication"),
    "encryption_at_rest": ("encrypted at rest", "encryption at rest", "aes-256"),
    "encryption_in_transit": ("encrypted in transit", "encryption in transit", "tls"),
    "backups": ("backup", "snapshot"),
    "vulnerability_management": ("vulnerability scan", "vulnerability scanning"),
    "access_reviews": ("access review", "user access rights are reviewed"),
    "least_privilege": ("least privilege",),
    "patching": ("patch", "remediation timeline"),
}
RECOMMENDATION_PREFIXES = ("recommend", "encourage", "consider", "should ", "enable ", "enhance ", "ensure ", "before implementing")


def claim_signature(control: str, attribute: str, scope: str, value: bool | str) -> str:
    """Stable identity for what a claim asserts, independent of which evidence carried it.

    Re-extraction from the same documents reproduces the signature, which lets a resolved
    conflict stay resolved without deleting the underlying evidence.
    """
    payload = "\0".join((control, attribute, scope, repr(value)))
    return "sig-" + hashlib.sha256(payload.encode()).hexdigest()[:24]


def _required(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


@dataclass(frozen=True)
class SecurityClaim:
    id: str
    control: str
    attribute: str
    scope: str
    value: bool | str
    subject: str
    strength: ClaimStrength
    evidence_type: EvidenceType
    evidence_ids: tuple[str, ...]
    support_text: str
    evidence_weight: float
    relevant_dates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("id", "control", "attribute", "scope", "subject", "strength", "evidence_type", "support_text"):
            _required(getattr(self, name), name)
        if not self.evidence_ids:
            raise ValueError("claims require at least one evidence ID")
        if not 0 <= self.evidence_weight <= 1:
            raise ValueError("evidence_weight must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["evidence_ids"] = list(self.evidence_ids)
        result["relevant_dates"] = list(self.relevant_dates)
        return result


@dataclass(frozen=True)
class ClaimExtractionResult:
    claims: tuple[SecurityClaim, ...] = ()
    insufficient_evidence: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.insufficient_evidence and self.claims:
            raise ValueError("insufficient result cannot also contain claims")
        if self.insufficient_evidence and not self.reason:
            raise ValueError("insufficient result requires a reason")


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip()]


def _scope(sentence: str) -> str:
    lower = sentence.casefold()
    choices = (
        ("GitHub", "github"), ("source-code platform", "source-code platform"),
        ("production access", "production"), ("administrative access", "administrative access"),
        ("privileged accounts", "privileged accounts"), ("AWS RDS and Amazon S3", "aws rds and amazon s3"),
        ("second geographic region", "second geographic region"),
        ("sensitive data", "sensitive data"), ("all core systems", "all core systems"),
    )
    for label, phrase in choices:
        if phrase in lower:
            return label
    return "organization-wide/unspecified"


def _strength(evidence: Evidence, sentence: str) -> ClaimStrength | None:
    lower = sentence.casefold().lstrip(" -|")
    if lower.startswith(RECOMMENDATION_PREFIXES) or re.search(r"\b(?:recommend|encourage|consider|should|enhance)\b", lower):
        return None
    if evidence.evidence_type in {"POLICY_REQUIREMENT", "CONTRACTUAL_REQUIREMENT"}:
        requirement_markers = (
            "required", "requires", "must", "shall", "enforced", "does not", "prohibited",
            "is encrypted", "are encrypted",
        )
        return "DOCUMENTED" if any(marker in lower for marker in requirement_markers) else None
    if evidence.evidence_type == "USER_CONFIRMATION":
        return "USER_CONFIRMED"
    if evidence.evidence_type in {"ASSESSMENT_EVIDENCE", "OPERATIONAL_RECORD"}:
        implementation_markers = (
            "is enforced", "are enforced", "requires", "is required", "implemented", "operated effectively",
            "we inspected evidence", "we confirmed", "were executed", "is configured", "maintains automated",
            "does not currently operate", "performed", "reviewed",
        )
        return "IMPLEMENTED" if any(marker in lower for marker in implementation_markers) else None
    # Unannotated observations (including questionnaire prompts and diagrams) do not establish claims.
    return None


def _attribute(control: str, sentence: str, strength: ClaimStrength) -> tuple[str, bool | str]:
    lower = sentence.casefold()
    control_negations = {
        "mfa": r"(?:mfa|multi-factor authentication).{0,35}\b(?:not|disabled)|\bdoes not.{0,35}(?:mfa|multi-factor authentication)",
        "encryption_at_rest": r"\bnot encrypted at rest|\bdoes not encrypt.{0,35}\bat rest",
        "encryption_in_transit": r"\bnot encrypted in transit|\bdoes not encrypt.{0,35}\bin transit",
        "backups": r"\bdoes not.{0,35}\b(?:backup|snapshot)|\bbackups? (?:are )?not",
        "vulnerability_management": r"\bdoes not.{0,45}\bvulnerability (?:scan|scanning)|\bnot currently operate.{0,45}\bvulnerability",
        "access_reviews": r"\bdoes not.{0,35}\baccess review|\baccess reviews? (?:are )?not",
        "least_privilege": r"\bdoes not.{0,35}\bleast privilege|\bleast privilege.{0,20}\bnot",
        "patching": r"\bdoes not.{0,35}\bpatch|\bpatch(?:ing|es)? (?:is |are )?not",
    }
    negative = bool(re.search(control_negations.get(control, r"a^"), lower))
    suffix = "implemented" if strength == "IMPLEMENTED" else "required" if strength == "DOCUMENTED" else "observed"
    if control == "backups" and re.search(r"\b(?:daily|weekly|monthly|annually)\b", lower):
        cadence = re.search(r"\b(daily|weekly|monthly|annually)\b", lower)
        return "cadence", cadence.group(1) if cadence else "unspecified"
    if control == "backups" and "encrypted" in lower:
        return "encryption", not negative
    if control == "backups" and ("geographic" in lower or "multi-region" in lower):
        return "geographic_redundancy", not negative
    return suffix, not negative


def _dates(sentence: str) -> tuple[str, ...]:
    patterns = r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{4})\b"
    return tuple(re.findall(patterns, sentence, re.IGNORECASE))


def validate_claim(claim: SecurityClaim, retrieved: Iterable[Evidence]) -> None:
    evidence_by_id = {item.id: item for item in retrieved}
    unknown = set(claim.evidence_ids) - evidence_by_id.keys()
    if unknown:
        raise ValueError(f"claim references evidence IDs outside retrieved set: {sorted(unknown)}")
    if not any(claim.support_text in evidence_by_id[item_id].text for item_id in claim.evidence_ids):
        raise ValueError("claim support_text is not present in its cited evidence")
    cited_types = {evidence_by_id[item_id].evidence_type for item_id in claim.evidence_ids}
    if claim.evidence_type not in cited_types:
        raise ValueError("claim evidence_type does not match cited evidence")
    if claim.strength == "IMPLEMENTED" and claim.evidence_type in {"POLICY_REQUIREMENT", "CONTRACTUAL_REQUIREMENT"}:
        raise ValueError("policy or contract evidence cannot establish implementation")


def extract_claims(evidence: Iterable[Evidence], control: str | None = None) -> ClaimExtractionResult:
    retrieved = list(evidence)
    normalized = re.sub(r"[^a-z0-9]+", "_", control.casefold()).strip("_") if control else None
    if normalized and normalized not in CONTROL_PATTERNS:
        return ClaimExtractionResult(
            insufficient_evidence=True,
            reason=f"Claim extraction is not configured for control {normalized!r}.",
        )
    controls = [normalized] if normalized else list(CONTROL_PATTERNS)
    claims: list[SecurityClaim] = []
    seen: set[tuple[str, str, str, bool | str, str]] = set()
    for item in retrieved:
        if not item.text or item.source_category == "questionnaire":
            continue
        for sentence in _sentences(item.text):
            lower = sentence.casefold()
            for candidate in controls:
                if not any(phrase in lower for phrase in CONTROL_PATTERNS[candidate]):
                    continue
                strength = _strength(item, sentence)
                if strength is None:
                    continue
                attribute, value = _attribute(candidate, sentence, strength)
                scope = _scope(sentence)
                key = (candidate, attribute, scope, value, item.id)
                if key in seen:
                    continue
                seen.add(key)
                claim_id = "claim-" + hashlib.sha256(f"{item.id}\0{candidate}\0{attribute}\0{scope}\0{value}".encode()).hexdigest()[:20]
                claim = SecurityClaim(
                    id=claim_id, control=candidate, attribute=attribute, scope=scope, value=value,
                    subject=item.organization, strength=strength, evidence_type=item.evidence_type,
                    evidence_ids=(item.id,), support_text=sentence, evidence_weight=WEIGHTS[item.evidence_type],
                    relevant_dates=_dates(sentence),
                )
                validate_claim(claim, retrieved)
                claims.append(claim)
    if not claims:
        return ClaimExtractionResult(insufficient_evidence=True, reason="No retrieved evidence supports a validated claim for the requested control.")
    return ClaimExtractionResult(claims=tuple(claims))
