"""SQLite-backed claim memory with provenance, corrections, and questionnaire state."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from regodit.analyst import (
    AnalystEngine, InvestigationResult, ProfileEvidence, SecurityClaim, claim_signature, determine_intent,
)
from regodit.analyst.claims import WEIGHTS
from regodit.config import PROFILE_DB, active_model
from regodit.models import Evidence, QuestionnaireItem
from regodit.retrieval import retrieve_evidence

LOGGER = logging.getLogger("regodit.memory")
DEFAULT_DB = PROFILE_DB


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_value(value: bool | str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _overlaps(first: str, second: str) -> bool:
    broad = {"organization-wide/unspecified", "all core systems"}
    return first == second or first in broad or second in broad


@dataclass(frozen=True)
class StoredClaim:
    claim: SecurityClaim
    status: str
    created_at: str
    updated_at: str
    supersedes: str | None
    superseded_by: str | None
    source_type: str


@dataclass(frozen=True)
class QuestionnaireState:
    question_id: str
    answer: str | None
    status: str
    confidence: float
    evidence_ids: tuple[str, ...]
    missing_information: tuple[str, ...]
    updated_at: str


@dataclass(frozen=True)
class QuestionEvaluation:
    """One recorded evaluation of a questionnaire item, kept for comparison and audit."""

    id: int
    question_id: str
    model: str
    run_type: str
    run_id: str
    answer: str | None
    status: str
    confidence: float
    evidence_ids: tuple[str, ...]
    conflicts: tuple[str, ...]
    previous_status: str | None
    accepted: bool
    note: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        record = asdict(self)
        record["evidence_ids"] = list(self.evidence_ids)
        record["conflicts"] = list(self.conflicts)
        return record


@dataclass(frozen=True)
class ObsoleteClaim:
    """A claim assertion a user clarification has retired, with the evidence known at the time."""

    signature: str
    control: str
    subject: str
    attribute: str
    scope: str
    value: bool | str
    evidence_ids: tuple[str, ...]
    reason: str
    resolved_at: str


class SecurityProfile:
    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    source_category TEXT NOT NULL,
                    evidence_type TEXT NOT NULL,
                    organization TEXT NOT NULL,
                    location TEXT NOT NULL,
                    text TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims (
                    id TEXT PRIMARY KEY,
                    control_name TEXT NOT NULL,
                    attribute_name TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    claim_strength TEXT NOT NULL,
                    evidence_type TEXT NOT NULL,
                    evidence_ids_json TEXT NOT NULL,
                    support_text TEXT NOT NULL,
                    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                    relevant_dates_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'SUPERSEDED')),
                    source_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    supersedes TEXT REFERENCES claims(id),
                    superseded_by TEXT REFERENCES claims(id)
                );
                CREATE INDEX IF NOT EXISTS idx_claim_lookup
                    ON claims(control_name, subject, status);
                CREATE TABLE IF NOT EXISTS user_confirmations (
                    id TEXT PRIMARY KEY,
                    claim_id TEXT NOT NULL REFERENCES claims(id),
                    stakeholder TEXT,
                    raw_response TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS questionnaire_state (
                    question_id TEXT PRIMARY KEY,
                    answer TEXT,
                    status TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    evidence_ids_json TEXT NOT NULL,
                    missing_information_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS question_evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    run_type TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    answer TEXT,
                    status TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    evidence_ids_json TEXT NOT NULL,
                    conflicts_json TEXT NOT NULL,
                    previous_status TEXT,
                    accepted INTEGER NOT NULL,
                    note TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_evaluation_question
                    ON question_evaluations(question_id, created_at);
                CREATE TABLE IF NOT EXISTS obsolete_claims (
                    signature TEXT PRIMARY KEY,
                    control_name TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    attribute_name TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    evidence_ids_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    resolved_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_obsolete_lookup
                    ON obsolete_claims(control_name, subject);
                CREATE TABLE IF NOT EXISTS conflict_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question_id TEXT NOT NULL,
                    control_name TEXT NOT NULL,
                    model TEXT NOT NULL,
                    existing_status TEXT NOT NULL,
                    existing_answer TEXT,
                    proposed_status TEXT NOT NULL,
                    proposed_answer TEXT,
                    evidence_ids_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conflict_resolutions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    winning_claim_id TEXT NOT NULL REFERENCES claims(id),
                    superseded_claim_ids_json TEXT NOT NULL,
                    stakeholder TEXT,
                    raw_response TEXT,
                    resolved_at TEXT NOT NULL
                );
            """)

    def close(self) -> None:
        """Connections are operation-scoped; retained for application lifecycle symmetry."""

    def _evidence_from_row(self, row: sqlite3.Row) -> Evidence:
        return Evidence(
            row["id"], row["source_name"], row["source_path"], row["source_category"],
            row["evidence_type"], row["organization"], row["location"], row["text"],
            json.loads(row["metadata_json"]),
        )

    def _claim_from_row(self, row: sqlite3.Row) -> SecurityClaim:
        return SecurityClaim(
            row["id"], row["control_name"], row["attribute_name"], row["scope"], json.loads(row["value_json"]),
            row["subject"], row["claim_strength"], row["evidence_type"], tuple(json.loads(row["evidence_ids_json"])),
            row["support_text"], row["confidence"], tuple(json.loads(row["relevant_dates_json"])),
        )

    def lookup(self, control: str, organization: str) -> ProfileEvidence:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM claims WHERE control_name = ? AND subject = ? AND status = 'ACTIVE' ORDER BY created_at, id",
                (control, organization),
            ).fetchall()
            claims = tuple(self._claim_from_row(row) for row in rows)
            evidence_ids = list(dict.fromkeys(eid for claim in claims for eid in claim.evidence_ids))
            evidence: list[Evidence] = []
            if evidence_ids:
                placeholders = ",".join("?" for _ in evidence_ids)
                evidence_rows = db.execute(f"SELECT * FROM evidence WHERE id IN ({placeholders})", evidence_ids).fetchall()
                by_id = {row["id"]: self._evidence_from_row(row) for row in evidence_rows}
                evidence = [by_id[eid] for eid in evidence_ids if eid in by_id]
            LOGGER.info("Loaded %d active profile claims for %s/%s", len(claims), organization, control)
            return ProfileEvidence(claims, tuple(evidence))

    def get_evidence(self, evidence_ids: Iterable[str]) -> tuple[Evidence, ...]:
        identifiers = list(dict.fromkeys(evidence_ids))
        if not identifiers:
            return ()
        placeholders = ",".join("?" for _ in identifiers)
        with self._connect() as db:
            rows = db.execute(f"SELECT * FROM evidence WHERE id IN ({placeholders})", identifiers).fetchall()
        by_id = {row["id"]: self._evidence_from_row(row) for row in rows}
        return tuple(by_id[item] for item in identifiers if item in by_id)

    def claim_history(self, control: str | None = None) -> tuple[StoredClaim, ...]:
        query = "SELECT * FROM claims"
        parameters: tuple[Any, ...] = ()
        if control:
            query += " WHERE control_name = ?"
            parameters = (control,)
        query += " ORDER BY created_at, id"
        with self._connect() as db:
            rows = db.execute(query, parameters).fetchall()
        return tuple(StoredClaim(
            self._claim_from_row(row), row["status"], row["created_at"], row["updated_at"],
            row["supersedes"], row["superseded_by"], row["source_type"],
        ) for row in rows)

    def _store_evidence(self, db: sqlite3.Connection, evidence: Evidence) -> None:
        db.execute(
            "INSERT OR IGNORE INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (evidence.id, evidence.source_name, evidence.source_path, evidence.source_category, evidence.evidence_type,
             evidence.organization, evidence.location, evidence.text, json.dumps(evidence.metadata, ensure_ascii=False, sort_keys=True)),
        )

    def add_claim(
        self,
        claim: SecurityClaim,
        evidence: Iterable[Evidence],
        source_type: str,
        supersedes: str | None = None,
    ) -> None:
        evidence_list = list(evidence)
        available = {item.id for item in evidence_list}
        missing = set(claim.evidence_ids) - available
        if missing:
            raise ValueError(f"cannot persist claim without cited evidence: {sorted(missing)}")
        timestamp = _now()
        with self._connect() as db:
            if supersedes:
                prior = db.execute("SELECT * FROM claims WHERE id = ? AND status = 'ACTIVE'", (supersedes,)).fetchone()
                if prior is None:
                    raise ValueError(f"active superseded claim not found: {supersedes}")
                if prior["control_name"] != claim.control or prior["subject"] != claim.subject:
                    raise ValueError("correction must concern the same control and subject")
            for item in evidence_list:
                self._store_evidence(db, item)
            db.execute(
                """INSERT INTO claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, NULL)""",
                (claim.id, claim.control, claim.attribute, _json_value(claim.value), claim.scope, claim.subject,
                 claim.strength, claim.evidence_type, json.dumps(claim.evidence_ids), claim.support_text,
                 claim.evidence_weight, json.dumps(claim.relevant_dates), source_type, timestamp, timestamp, supersedes),
            )
            if supersedes:
                db.execute(
                    "UPDATE claims SET status = 'SUPERSEDED', superseded_by = ?, updated_at = ? WHERE id = ?",
                    (claim.id, timestamp, supersedes),
                )
        LOGGER.info("Stored claim %s (%s/%s)", claim.id, claim.control, claim.attribute)

    def record_user_claim(
        self,
        control: str,
        attribute: str,
        value: bool | str,
        scope: str,
        raw_response: str,
        stakeholder: str | None = None,
        organization: str = "Regodit",
        supersedes: str | None = None,
    ) -> SecurityClaim:
        if not raw_response.strip():
            raise ValueError("raw_response cannot be blank")
        timestamp = _now()
        identity = f"{timestamp}\0{control}\0{attribute}\0{scope}\0{raw_response}"
        claim_id = "claim-user-" + hashlib.sha256(identity.encode()).hexdigest()[:20]
        evidence_id = "ev-user-" + hashlib.sha256((identity + "\0evidence").encode()).hexdigest()[:20]
        evidence = Evidence(
            evidence_id, "User confirmation", f"profile://confirmations/{evidence_id}", "other", "USER_CONFIRMATION",
            organization, f"confirmation {timestamp}", raw_response,
            {"record_kind": "user_confirmation", "stakeholder": stakeholder, "timestamp": timestamp},
        )
        claim = SecurityClaim(
            claim_id, control, attribute, scope, value, organization, "USER_CONFIRMED", "USER_CONFIRMATION",
            (evidence_id,), raw_response, WEIGHTS["USER_CONFIRMATION"], (),
        )
        self.add_claim(claim, [evidence], "USER_CONFIRMATION", supersedes)
        with self._connect() as db:
            db.execute(
                "INSERT INTO user_confirmations VALUES (?, ?, ?, ?, ?)",
                ("confirmation-" + claim_id, claim_id, stakeholder, raw_response, timestamp),
            )
        return claim

    def correct_claim(
        self,
        prior_claim_id: str,
        value: bool | str,
        raw_response: str,
        stakeholder: str | None = None,
        scope: str | None = None,
    ) -> SecurityClaim:
        with self._connect() as db:
            prior = db.execute("SELECT * FROM claims WHERE id = ? AND status = 'ACTIVE'", (prior_claim_id,)).fetchone()
        if prior is None:
            raise ValueError(f"active claim not found: {prior_claim_id}")
        return self.record_user_claim(
            prior["control_name"], prior["attribute_name"], value, scope or prior["scope"], raw_response,
            stakeholder, prior["subject"], prior_claim_id,
        )

    def resolve_conflict(
        self,
        winning_claim_id: str,
        superseded_claim_ids: Iterable[str],
        stakeholder: str | None = None,
        raw_response: str | None = None,
    ) -> None:
        losing = list(dict.fromkeys(superseded_claim_ids))
        if not losing or winning_claim_id in losing:
            raise ValueError("resolution requires distinct losing claims")
        timestamp = _now()
        with self._connect() as db:
            winner = db.execute("SELECT * FROM claims WHERE id = ? AND status = 'ACTIVE'", (winning_claim_id,)).fetchone()
            if winner is None:
                raise ValueError("winning claim must be active")
            for claim_id in losing:
                row = db.execute("SELECT * FROM claims WHERE id = ? AND status = 'ACTIVE'", (claim_id,)).fetchone()
                if row is None:
                    raise ValueError(f"losing claim must be active: {claim_id}")
                if row["control_name"] != winner["control_name"] or row["subject"] != winner["subject"]:
                    raise ValueError("resolved claims must concern the same control and subject")
            db.executemany(
                "UPDATE claims SET status = 'SUPERSEDED', superseded_by = ?, updated_at = ? WHERE id = ?",
                [(winning_claim_id, timestamp, claim_id) for claim_id in losing],
            )
            db.execute(
                "INSERT INTO conflict_resolutions (winning_claim_id, superseded_claim_ids_json, stakeholder, raw_response, resolved_at) VALUES (?, ?, ?, ?, ?)",
                (winning_claim_id, json.dumps(losing), stakeholder, raw_response, timestamp),
            )
        # Superseding stored claims is not enough: the same assertion is re-derived from the
        # source documents on every investigation, so retire the assertion as well.
        retired = [entry.claim for entry in self.claim_history() if entry.claim.id in set(losing)]
        if retired:
            self.mark_claims_obsolete(retired, raw_response or f"Superseded by {winning_claim_id}")
        LOGGER.info("Resolved conflict in favor of %s; superseded %s", winning_claim_id, losing)

    def save_questionnaire_result(
        self,
        result: InvestigationResult,
        model: str | None = None,
        run_type: str = "investigation",
        run_id: str | None = None,
        note: str | None = None,
    ) -> None:
        previous = self.questionnaire_state(result.question_id)
        with self._connect() as db:
            db.execute(
                """INSERT INTO questionnaire_state VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(question_id) DO UPDATE SET answer=excluded.answer, status=excluded.status,
                   confidence=excluded.confidence, evidence_ids_json=excluded.evidence_ids_json,
                   missing_information_json=excluded.missing_information_json, updated_at=excluded.updated_at""",
                (result.question_id, result.answer, result.status, result.confidence, json.dumps(result.evidence_ids),
                 json.dumps(result.missing_information), _now()),
            )
        self.record_evaluation(
            question_id=result.question_id, model=model or active_model(), run_type=run_type,
            run_id=run_id or run_type, answer=result.answer, status=result.status, confidence=result.confidence,
            evidence_ids=result.evidence_ids,
            conflicts=tuple(conflict.description for conflict in result.conflicts),
            previous_status=previous.status if previous else None, accepted=True, note=note,
        )

    def record_evaluation(
        self,
        *,
        question_id: str,
        model: str,
        run_type: str,
        run_id: str,
        answer: str | None,
        status: str,
        confidence: float,
        evidence_ids: Iterable[str] = (),
        conflicts: Iterable[str] = (),
        previous_status: str | None = None,
        accepted: bool = True,
        note: str | None = None,
    ) -> None:
        """Append one evaluation to the immutable history, whether or not it was accepted."""
        if not question_id.strip() or not model.strip():
            raise ValueError("an evaluation requires a question ID and a model")
        with self._connect() as db:
            db.execute(
                """INSERT INTO question_evaluations
                   (question_id, model, run_type, run_id, answer, status, confidence, evidence_ids_json,
                    conflicts_json, previous_status, accepted, note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (question_id, model.strip(), run_type, run_id, answer, status, float(confidence),
                 json.dumps(list(dict.fromkeys(evidence_ids))), json.dumps(list(conflicts)),
                 previous_status, int(accepted), note, _now()),
            )

    def evaluation_history(self, question_id: str | None = None, run_id: str | None = None) -> tuple[QuestionEvaluation, ...]:
        query = "SELECT * FROM question_evaluations"
        clauses: list[str] = []
        parameters: list[Any] = []
        if question_id:
            clauses.append("question_id = ?")
            parameters.append(question_id)
        if run_id:
            clauses.append("run_id = ?")
            parameters.append(run_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, id"
        with self._connect() as db:
            rows = db.execute(query, parameters).fetchall()
        return tuple(QuestionEvaluation(
            row["id"], row["question_id"], row["model"], row["run_type"], row["run_id"], row["answer"],
            row["status"], row["confidence"], tuple(json.loads(row["evidence_ids_json"])),
            tuple(json.loads(row["conflicts_json"])), row["previous_status"], bool(row["accepted"]),
            row["note"], row["created_at"],
        ) for row in rows)

    def mark_claims_obsolete(self, claims: Iterable[SecurityClaim], reason: str) -> tuple[str, ...]:
        """Retire specific claim assertions that a user clarification has superseded.

        Evidence rows are never deleted. Re-extraction reproduces the same signature, so the
        resolved conflict stays resolved until genuinely new evidence carries the assertion again.
        """
        if not reason.strip():
            raise ValueError("retiring a claim assertion requires a stated reason")
        timestamp = _now()
        signatures: list[str] = []
        with self._connect() as db:
            for claim in claims:
                signature = claim_signature(claim.control, claim.attribute, claim.scope, claim.value)
                existing = db.execute("SELECT evidence_ids_json FROM obsolete_claims WHERE signature = ?", (signature,)).fetchone()
                known = list(json.loads(existing["evidence_ids_json"])) if existing else []
                merged = list(dict.fromkeys(known + list(claim.evidence_ids)))
                db.execute(
                    """INSERT INTO obsolete_claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(signature) DO UPDATE SET evidence_ids_json=excluded.evidence_ids_json,
                       reason=excluded.reason, resolved_at=excluded.resolved_at""",
                    (signature, claim.control, claim.subject, claim.attribute, claim.scope,
                     _json_value(claim.value), json.dumps(merged), reason.strip(), timestamp),
                )
                signatures.append(signature)
        LOGGER.info("Retired %d claim assertion(s) after clarification: %s", len(signatures), reason)
        return tuple(dict.fromkeys(signatures))

    def obsolete_claims(self, control: str | None = None, subject: str | None = None) -> tuple[ObsoleteClaim, ...]:
        query = "SELECT * FROM obsolete_claims"
        clauses: list[str] = []
        parameters: list[Any] = []
        if control:
            clauses.append("control_name = ?")
            parameters.append(control)
        if subject:
            clauses.append("subject = ?")
            parameters.append(subject)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self._connect() as db:
            rows = db.execute(query + " ORDER BY resolved_at, signature", parameters).fetchall()
        return tuple(ObsoleteClaim(
            row["signature"], row["control_name"], row["subject"], row["attribute_name"], row["scope"],
            json.loads(row["value_json"]), tuple(json.loads(row["evidence_ids_json"])), row["reason"], row["resolved_at"],
        ) for row in rows)

    def is_obsolete(self, claim: SecurityClaim) -> bool:
        """True only when the assertion was retired and carries no evidence unseen at resolution time."""
        signature = claim_signature(claim.control, claim.attribute, claim.scope, claim.value)
        with self._connect() as db:
            row = db.execute("SELECT evidence_ids_json FROM obsolete_claims WHERE signature = ?", (signature,)).fetchone()
        if row is None:
            return False
        # New evidence for a retired assertion is a genuine new finding, not a stale conflict.
        return set(claim.evidence_ids).issubset(set(json.loads(row["evidence_ids_json"])))

    def record_conflict_candidate(
        self,
        *,
        question_id: str,
        control: str,
        model: str,
        existing_status: str,
        existing_answer: str | None,
        proposed_status: str,
        proposed_answer: str | None,
        evidence_ids: Iterable[str],
        reason: str,
    ) -> None:
        """Preserve a model result that disagrees with confirmed state instead of applying it."""
        with self._connect() as db:
            db.execute(
                """INSERT INTO conflict_candidates
                   (question_id, control_name, model, existing_status, existing_answer, proposed_status,
                    proposed_answer, evidence_ids_json, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (question_id, control, model, existing_status, existing_answer, proposed_status,
                 proposed_answer, json.dumps(list(dict.fromkeys(evidence_ids))), reason, _now()),
            )
        LOGGER.info("Recorded conflict candidate for %s; confirmed state was not overwritten", question_id)

    def conflict_candidates(self, question_id: str | None = None) -> tuple[dict[str, Any], ...]:
        query = "SELECT * FROM conflict_candidates"
        parameters: tuple[Any, ...] = ()
        if question_id:
            query += " WHERE question_id = ?"
            parameters = (question_id,)
        with self._connect() as db:
            rows = db.execute(query + " ORDER BY created_at, id", parameters).fetchall()
        return tuple({
            "id": row["id"], "question_id": row["question_id"], "control": row["control_name"],
            "model": row["model"], "existing_status": row["existing_status"], "existing_answer": row["existing_answer"],
            "proposed_status": row["proposed_status"], "proposed_answer": row["proposed_answer"],
            "evidence_ids": list(json.loads(row["evidence_ids_json"])), "reason": row["reason"],
            "created_at": row["created_at"],
        } for row in rows)

    def questionnaire_states(self) -> dict[str, QuestionnaireState]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM questionnaire_state").fetchall()
        return {row["question_id"]: QuestionnaireState(
            row["question_id"], row["answer"], row["status"], row["confidence"],
            tuple(json.loads(row["evidence_ids_json"])), tuple(json.loads(row["missing_information_json"])), row["updated_at"],
        ) for row in rows}

    def questionnaire_state(self, question_id: str) -> QuestionnaireState | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM questionnaire_state WHERE question_id = ?", (question_id,)).fetchone()
        if row is None:
            return None
        return QuestionnaireState(
            row["question_id"], row["answer"], row["status"], row["confidence"],
            tuple(json.loads(row["evidence_ids_json"])), tuple(json.loads(row["missing_information_json"])), row["updated_at"],
        )

    def save_confirmed_questionnaire_state(
        self, question_id: str, answer: str, evidence_ids: Iterable[str], missing_information: Iterable[str] = (),
        run_type: str = "user_confirmation", model: str | None = None, note: str | None = None,
    ) -> None:
        """Persist a user-confirmed row assembled from validated conversational facts."""
        identifiers = tuple(dict.fromkeys(evidence_ids))
        if not answer.strip() or not identifiers:
            raise ValueError("confirmed questionnaire state requires an answer and evidence")
        previous = self.questionnaire_state(question_id)
        with self._connect() as db:
            db.execute(
                """INSERT INTO questionnaire_state VALUES (?, ?, 'USER_CONFIRMED', 0.85, ?, ?, ?)
                   ON CONFLICT(question_id) DO UPDATE SET answer=excluded.answer, status='USER_CONFIRMED',
                   confidence=excluded.confidence, evidence_ids_json=excluded.evidence_ids_json,
                   missing_information_json=excluded.missing_information_json, updated_at=excluded.updated_at""",
                (question_id, answer, json.dumps(identifiers), json.dumps(tuple(missing_information)), _now()),
            )
        self.record_evaluation(
            question_id=question_id, model=model or "user", run_type=run_type, run_id=run_type,
            answer=answer, status="USER_CONFIRMED", confidence=0.85, evidence_ids=identifiers,
            previous_status=previous.status if previous else None, accepted=True, note=note,
        )

    def needs_question(self, question_id: str) -> bool:
        state = self.questionnaire_state(question_id)
        return state is None or state.status in {"UNKNOWN", "CONFLICT"}

    def answer_follow_up(
        self,
        item: QuestionnaireItem,
        raw_response: str,
        stakeholder: str | None = None,
        organization: str = "Regodit",
        supersedes: str | None = None,
        retriever: Callable[[str, str | None, str, int], list[Evidence]] = retrieve_evidence,
        analyst: AnalystEngine | None = None,
    ) -> InvestigationResult:
        intent = determine_intent(item)
        attribute, value, scope = normalize_user_response(intent.control, intent.answer_kind, raw_response)
        self.record_user_claim(intent.control, attribute, value, scope, raw_response, stakeholder, organization, supersedes)
        result = (analyst or AnalystEngine(profile=self, retriever=retriever)).investigate(item, organization)
        self.save_questionnaire_result(result)
        return result


def normalize_user_response(control: str, answer_kind: str, raw_response: str) -> tuple[str, bool | str, str]:
    text = raw_response.strip()
    lower = text.casefold()
    if not text:
        raise ValueError("raw response cannot be blank")
    if answer_kind == "boolean":
        if re.match(r"^(?:yes|true)\b", lower):
            value: bool | str = True
        elif re.match(r"^(?:no|false)\b", lower):
            value = False
        else:
            raise ValueError("boolean confirmation must begin with yes/no or true/false")
        # A user response to a requirement question confirms the stated requirement, not audited implementation.
        return "required", value, "organization-wide/unspecified"
    if answer_kind == "frequency":
        match = re.search(r"\b(every\s+(?:\d+|one|two|three|four|five|six|twelve)\s+(?:hours?|days?|weeks?|months?)|hourly|daily|weekly|monthly|quarterly|annually)\b", lower)
        if not match:
            raise ValueError("frequency response must state an explicit cadence")
        return "cadence", match.group(1), "organization-wide/unspecified"
    if answer_kind == "location":
        return "location", text, "customer data"
    if answer_kind == "list":
        return "authorized_personnel", text, "scope asked in questionnaire"
    if answer_kind == "attachment":
        return "document", text, "organization-wide/unspecified"
    if answer_kind == "description":
        return "description", text, "organization-wide/unspecified"
    raise ValueError("cannot normalize a response to an unknown question")
