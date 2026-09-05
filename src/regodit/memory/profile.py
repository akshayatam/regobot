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

from regodit.analyst import AnalystEngine, InvestigationResult, ProfileEvidence, SecurityClaim, determine_intent
from regodit.analyst.claims import WEIGHTS
from regodit.config import PROFILE_DB
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
        LOGGER.info("Resolved conflict in favor of %s; superseded %s", winning_claim_id, losing)

    def save_questionnaire_result(self, result: InvestigationResult) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO questionnaire_state VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(question_id) DO UPDATE SET answer=excluded.answer, status=excluded.status,
                   confidence=excluded.confidence, evidence_ids_json=excluded.evidence_ids_json,
                   missing_information_json=excluded.missing_information_json, updated_at=excluded.updated_at""",
                (result.question_id, result.answer, result.status, result.confidence, json.dumps(result.evidence_ids),
                 json.dumps(result.missing_information), _now()),
            )

    def questionnaire_state(self, question_id: str) -> QuestionnaireState | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM questionnaire_state WHERE question_id = ?", (question_id,)).fetchone()
        if row is None:
            return None
        return QuestionnaireState(
            row["question_id"], row["answer"], row["status"], row["confidence"],
            tuple(json.loads(row["evidence_ids_json"])), tuple(json.loads(row["missing_information_json"])), row["updated_at"],
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
