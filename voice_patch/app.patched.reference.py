"""Hackathon-ready local web UI for investigations and questionnaire completion."""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import re
import uuid
from collections import Counter
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from regodit.analyst import AnalystEngine, InvestigationResult, normalize_control
from regodit.config import ARTIFACT_DIR, CONVERSATION_DB, DATA_DIR, HOST, PORT, PROFILE_DB
from regodit.conversation import AnalystState, ConversationService
from regodit.ingestion import load_repository
from regodit.memory import SecurityProfile
from regodit.models import QuestionnaireItem
from regodit.questionnaire import QUESTIONNAIRE_NAME, parse_questionnaire

DEFAULT_DB = PROFILE_DB
ROOT = ARTIFACT_DIR.parent

LOGGER = logging.getLogger("regodit.ui")
M = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
STATUS_LABELS = {
    "VERIFIED": "Verified from company evidence",
    "USER_CONFIRMED": "Confirmed by user",
    "UNKNOWN": "Unknown / needs confirmation",
    "CONFLICT": "Conflict",
}


def _column_number(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference)
    value = 0
    for char in letters.group() if letters else "":
        value = value * 26 + ord(char) - 64
    return value


def _set_inline_cell(row: ET.Element, reference: str, value: str) -> None:
    cells = row.findall(f"{{{M}}}c")
    cell = next((item for item in cells if item.attrib.get("r") == reference), None)
    if cell is None:
        cell = ET.Element(f"{{{M}}}c", {"r": reference})
        insert_at = next((index for index, item in enumerate(cells) if _column_number(item.attrib["r"]) > _column_number(reference)), len(cells))
        row.insert(insert_at, cell)
    for child in list(cell):
        if child.tag in {f"{{{M}}}v", f"{{{M}}}f", f"{{{M}}}is"}:
            cell.remove(child)
    cell.attrib["t"] = "inlineStr"
    inline = ET.SubElement(cell, f"{{{M}}}is")
    text = ET.SubElement(inline, f"{{{M}}}t")
    text.text = value


def export_completed_xlsx(source: Path, output: Path, items: list[QuestionnaireItem], states: dict[str, Any]) -> Path:
    """Write answers to a new XLSX while preserving every other archive member."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(source) as original:
        relationships = ET.fromstring(original.read("xl/_rels/workbook.xml.rels"))
        targets = {rel.attrib["Id"]: rel.attrib["Target"] for rel in relationships}
        workbook = ET.fromstring(original.read("xl/workbook.xml"))
        target = None
        for sheet in workbook.findall(f".//{{{M}}}sheet"):
            if sheet.attrib["name"] == "Vendor Security Responses":
                target = str(PurePosixPath("xl") / targets[sheet.attrib[f"{{{R}}}id"]])
                break
        if target is None:
            raise ValueError("Vendor Security Responses sheet not found")
        worksheet = ET.fromstring(original.read(target))
        rows = {int(row.attrib["r"]): row for row in worksheet.findall(f".//{{{M}}}sheetData/{{{M}}}row")}
        for item in items:
            state = states.get(item.id)
            if state is None:
                continue
            row = rows[item.source_row]
            _set_inline_cell(row, f"C{item.source_row}", state.answer or "")
            _set_inline_cell(row, f"D{item.source_row}", f"{STATUS_LABELS[state.status]} | confidence {state.confidence:.2f}")
            _set_inline_cell(row, f"E{item.source_row}", ", ".join(state.evidence_ids))
        updated_sheet = ET.tostring(worksheet, encoding="utf-8", xml_declaration=True)
        with ZipFile(output, "w", ZIP_DEFLATED) as completed:
            for info in original.infolist():
                completed.writestr(info, updated_sheet if info.filename == target else original.read(info.filename))
    return output


PRIORITY_CONTROLS = {
    "MFA": 100, "access control": 90, "least privilege": 88, "encryption at rest": 86,
    "encryption in transit": 85, "incident response": 82, "backups": 80,
    "disaster recovery": 79, "vulnerability management": 78, "patching": 76,
    "employee onboarding": 72,
}


class AppService:
    def __init__(
        self, db_path: Path | str = DEFAULT_DB, artifact_dir: Path = ARTIFACT_DIR,
        session_id: str | None = None, conversation_db: Path | str | None = None,
    ):
        source = next(DATA_DIR.rglob(QUESTIONNAIRE_NAME))
        self.source = source
        self.items = parse_questionnaire(source)
        self.items_by_id = {item.id: item for item in self.items}
        self.profile = SecurityProfile(db_path)
        self.session_id = session_id or str(uuid.uuid4())
        self.engine = AnalystEngine(profile=self.profile, top_k=24, session_id=self.session_id)
        self.artifact_dir = artifact_dir
        self.repository = load_repository()
        self.evidence_by_id = {record.id: record for record in self.repository}
        profile_path = Path(db_path)
        checkpoint_path = Path(conversation_db) if conversation_db else (
            CONVERSATION_DB if profile_path.resolve() == Path(DEFAULT_DB).resolve()
            else profile_path.with_name(profile_path.stem + "_conversations.sqlite3")
        )
        self.synthetic_items = {
            "CHAT-BACKGROUND-CHECKS": QuestionnaireItem(
                "CHAT-BACKGROUND-CHECKS", "chat", "Personnel Security",
                "Are background checks required before employees are hired?", "employee background checks",
                "conversation", "chat", 1, "chat",
            )
        }
        self.chat = ConversationService(self, checkpoint_path)

    def close(self) -> None:
        self.engine.flush_traces()
        self.chat.close()
        self.profile.close()

    def opening(self, thread_id: str) -> dict[str, Any]:
        progress = self.dashboard()["progress"]
        content = (
            f"I've reviewed the available Regodit company evidence against the {progress['total']}-question security questionnaire. "
            f"I could verify or resolve {progress['completed']} questions. {progress['unknown']} still need information, "
            f"and {progress['conflicts']} conflicts require clarification. I can continue the review, resolve a conflict, "
            "or answer questions about the current security profile."
        )
        message = {
            "role": "assistant", "content": content, "action": "OPENING",
            "suggestions": ["Continue security review", "Resolve next conflict", "Show questionnaire progress", "Generate questionnaire"],
        }
        result = self.chat.opening(thread_id, message)
        result["progress"] = progress
        result["suggestions"] = message["suggestions"]
        return result

    def chat_message(self, thread_id: str, message: str) -> dict[str, Any]:
        result = self.chat.send(thread_id, message)
        result["progress"] = self.dashboard()["progress"]
        result["suggestions"] = ["Continue questionnaire", "Resolve next conflict", "Show unresolved questions", "Generate questionnaire"]
        return result

    def select_chat_question(self, message: str, intent: str) -> str | None:
        match = re.search(r"\bVSQ-\d{3}\b", message, re.IGNORECASE)
        if match and match.group().upper() in self.items_by_id:
            return match.group().upper()
        lower = message.casefold()
        if "background check" in lower:
            return "CHAT-BACKGROUND-CHECKS"
        if intent == "CONTINUE":
            candidates = []
            counts = Counter(normalize_control(item.normalized_control) for item in self.items)
            wants_conflict = "conflict" in lower
            for item in self.items:
                state = self.profile.questionnaire_state(item.id)
                status = state.status if state else "UNKNOWN"
                if status not in {"UNKNOWN", "CONFLICT"} or (wants_conflict and status != "CONFLICT"):
                    continue
                score = (1000 if status == "CONFLICT" else 0) + PRIORITY_CONTROLS.get(item.normalized_control, 0)
                score += counts[normalize_control(item.normalized_control)] * 5
                candidates.append((score, -item.source_row, item.id))
            return max(candidates)[2] if candidates else None
        query_terms = set(re.findall(r"[a-z0-9]+", lower)) - {"do", "we", "is", "are", "the", "a", "an", "our"}
        aliases = {"mfa": {"mfa", "authentication", "otp"}, "backups": {"backup", "backups", "recovery"}}
        ranked = []
        for item in self.items:
            text = f"{item.question} {item.normalized_control}".casefold()
            terms = set(re.findall(r"[a-z0-9]+", text))
            score = len(query_terms & terms)
            for control, words in aliases.items():
                if query_terms & words and control.casefold() in item.normalized_control.casefold():
                    score += 5
            ranked.append((score, -item.source_row, item.id))
        best = max(ranked)
        return best[2] if best[0] > 0 else None

    def _chat_item(self, question_id: str) -> QuestionnaireItem:
        return self.items_by_id.get(question_id) or self.synthetic_items[question_id]

    def investigate_for_chat(self, question_id: str) -> dict[str, Any]:
        item = self._chat_item(question_id)
        if question_id == "CHAT-BACKGROUND-CHECKS":
            remembered = self.profile.lookup("employee_background_checks", "Regodit").claims
            if remembered:
                claim = remembered[-1]
                answer = "Background checks are required before hiring." if claim.value else "Background checks are not required before hiring."
                message = {"role": "assistant", "content": "Earlier you confirmed that " + answer.casefold(),
                           "action": "ANSWER", "status": "USER_CONFIRMED", "question_id": question_id, "evidence": []}
                return {"question": item.question, "control": "employee_background_checks", "status": "USER_CONFIRMED",
                        "evidence_ids": list(claim.evidence_ids), "claims": [claim.to_dict()], "conflict_ids": [],
                        "missing_fields": [], "pending_question": None, "pending_question_type": None,
                        "pending_payload": None, "collected_fields": {}, "message": message}
        result = self.engine.investigate(item)
        if question_id in self.items_by_id:
            self.profile.save_questionnaire_result(result)
        evidence = self.evidence(list(result.evidence_ids))
        sources = [{key: row[key] for key in ("id", "source_name", "location", "text", "evidence_type")} for row in evidence]
        control = normalize_control(item.normalized_control)
        pending = result.follow_up_question
        pending_type = result.next_action
        missing = list(result.missing_information)
        if control == "backups" and result.status == "UNKNOWN":
            missing = ["enabled", "frequency", "automated"]
            pending = "Are production backups performed?"
            pending_type = "backup_enabled"
        if question_id == "CHAT-BACKGROUND-CHECKS":
            pending = "I couldn't find company evidence confirming whether employee background checks are performed. Are background checks required before hiring?"
            pending_type = "background_checks"
            missing = ["required before hiring"]
        if result.status == "CONFLICT":
            lines = []
            for claim in result.claims[:4]:
                source = next((row for row in sources if row["id"] in claim.evidence_ids), None)
                lines.append(f"{source['source_name'] if source else claim.evidence_type} says: “{claim.support_text}”")
            content = "I found conflicting information about " + control.replace("_", " ") + ". " + " ".join(lines) + " " + (pending or "Which statement is current?")
        elif result.answerable:
            content = result.answer or "The available evidence supports this control."
        else:
            content = pending or "I searched the security profile and company evidence, but could not establish a reliable answer."
        message = {
            "role": "assistant", "content": content, "action": result.next_action,
            "status": result.status, "question_id": question_id, "evidence": sources,
        }
        return {
            "question": item.question, "control": control, "status": result.status,
            "evidence_ids": list(result.evidence_ids), "claims": [claim.to_dict() for claim in result.claims],
            "conflict_ids": [identifier for conflict in result.conflicts for identifier in conflict.claim_ids],
            "missing_fields": missing, "pending_question": pending, "pending_question_type": pending_type,
            "pending_payload": {"question": pending, "question_id": question_id, "type": pending_type} if pending else None,
            "collected_fields": {}, "message": message,
        }

    @staticmethod
    def _backup_facts(response: str, pending_type: str | None) -> dict[str, Any]:
        lower = response.casefold()
        facts: dict[str, Any] = {}
        if pending_type in {"backup_enabled", "backup_automated"} or "backup" in lower:
            if re.search(r"\b(?:yes|performed|enabled|run|runs)\b", lower):
                facts["automated" if pending_type == "backup_automated" else "enabled"] = True
            elif re.search(r"\b(?:no|not performed|disabled)\b", lower):
                facts["automated" if pending_type == "backup_automated" else "enabled"] = False
        cadence = re.search(r"\b(every\s+(?:\d+|one|two|three|four|five|six|twelve)\s+(?:hours?|days?|weeks?|months?)|hourly|daily|weekly|monthly|quarterly|annually|every\s+24\s+hours)\b", lower)
        if cadence:
            facts["frequency"] = cadence.group(1)
        if "automat" in lower:
            facts["automated"] = not bool(re.search(r"\b(?:not|manual(?:ly)?)\b", lower))
        return facts

    def _persist_fields(self, control: str, facts: dict[str, Any], raw: str) -> list[Any]:
        attributes = {"frequency": "cadence"}
        claims = []
        for field, value in facts.items():
            existing = [entry for entry in self.profile.claim_history(control) if entry.status == "ACTIVE" and entry.claim.attribute == attributes.get(field, field)]
            if existing:
                claim = self.profile.correct_claim(existing[-1].claim.id, value, raw, "conversation user")
            else:
                claim = self.profile.record_user_claim(control, attributes.get(field, field), value, "production", raw, "conversation user")
            claims.append(claim)
        return claims

    def _update_control_rows(self, control: str, claims: list[Any]) -> list[str]:
        active = self.profile.lookup(control, "Regodit").claims
        if not active:
            return []
        answer = "; ".join(f"{claim.attribute}={claim.value}" for claim in active)
        evidence_ids = [identifier for claim in active for identifier in claim.evidence_ids]
        affected = []
        for item in self.items:
            if normalize_control(item.normalized_control) == control:
                self.profile.save_confirmed_questionnaire_state(item.id, answer, evidence_ids)
                affected.append(item.id)
        return affected

    def process_chat_response(self, state: AnalystState, response: str) -> dict[str, Any]:
        control = state.get("active_control") or ""
        pending_type = state.get("pending_question_type")
        collected = dict(state.get("collected_fields", {}))
        if control == "backups":
            facts = self._backup_facts(response, pending_type)
            if not facts:
                question = state.get("pending_question") or "Please provide the specific backup detail requested."
                return {"pending_question": question, "pending_payload": {"question": question, "type": pending_type},
                        "message": {"role": "assistant", "content": "I need a specific answer to update the questionnaire. " + question, "action": "FOLLOW_UP"}}
            self._persist_fields("backups", facts, response)
            collected.update(facts)
            active = {claim.attribute: claim.value for claim in self.profile.lookup("backups", "Regodit").claims}
            normalized = {"enabled": active.get("enabled"), "frequency": active.get("cadence"), "automated": active.get("automated")}
            missing = [field for field, value in normalized.items() if value is None]
            if missing:
                field = missing[0]
                questions = {"enabled": "Are production backups performed?", "frequency": "How frequently are they performed?", "automated": "Are those backups automated?"}
                question = questions[field]
                return {"collected_fields": collected, "missing_fields": missing, "pending_question": question,
                        "pending_question_type": "backup_" + field, "pending_payload": {"question": question, "type": "backup_" + field},
                        "message": {"role": "assistant", "content": question, "action": "FOLLOW_UP", "status": "UNKNOWN"}}
            claims = list(self.profile.lookup("backups", "Regodit").claims)
            affected = self._update_control_rows("backups", claims)
            return {"pending_question": None, "pending_payload": None, "missing_fields": [], "investigation_status": "USER_CONFIRMED",
                    "last_user_confirmation": normalized,
                    "message": {"role": "assistant", "content": f"Thanks — I recorded that production backups are enabled, run {normalized['frequency']}, and are {'automated' if normalized['automated'] else 'manual'}. I updated {len(affected)} related questionnaire item(s).", "action": "UPDATE", "status": "USER_CONFIRMED"}}
        if pending_type == "background_checks":
            lower = response.casefold()
            if not re.search(r"\b(?:yes|no|required|not required)\b", lower):
                question = state["pending_question"]
                return {"pending_question": question, "pending_payload": {"question": question, "type": pending_type},
                        "message": {"role": "assistant", "content": "Please confirm yes or no: " + question, "action": "FOLLOW_UP"}}
            value = not bool(re.search(r"\b(?:no|not required)\b", lower))
            claim = self.profile.record_user_claim("employee_background_checks", "required_before_hiring", value, "all employees", response, "conversation user")
            return {"pending_question": None, "pending_payload": None, "investigation_status": "USER_CONFIRMED",
                    "last_user_confirmation": claim.to_dict(), "message": {"role": "assistant", "content": "Recorded. I’ll use this confirmed hiring-screening fact in future investigations and won’t ask it again.", "action": "UPDATE", "status": "USER_CONFIRMED"}}
        if state.get("investigation_status") == "CONFLICT":
            lower = response.casefold()
            if not re.search(r"\b(?:yes|no|enforced|not enforced|current|outdated)\b", lower):
                question = state["pending_question"]
                return {"pending_question": question, "pending_payload": {"question": question, "type": pending_type},
                        "message": {"role": "assistant", "content": "I need the current state to resolve the conflict. " + question, "action": "RESOLVE_CONFLICT"}}
            value = not bool(re.search(r"\b(?:no|not enforced|not enabled)\b", lower))
            active = [entry for entry in self.profile.claim_history(control) if entry.status == "ACTIVE" and isinstance(entry.claim.value, bool) and entry.claim.value != value]
            supersedes = active[-1].claim.id if active else None
            claim = self.profile.record_user_claim(control, "implemented", value, "organization-wide/unspecified", response, "conversation user", supersedes=supersedes)
            affected = self._update_control_rows(control, [claim])
            return {"pending_question": None, "pending_payload": None, "conflict_ids": [], "investigation_status": "USER_CONFIRMED",
                    "last_user_confirmation": claim.to_dict(), "message": {"role": "assistant", "content": f"Conflict resolved. I recorded the current state, preserved the earlier evidence in history, and updated {len(affected)} questionnaire item(s).", "action": "UPDATE", "status": "USER_CONFIRMED"}}
        question_id = state.get("active_question_id")
        if question_id not in self.items_by_id:
            raise ValueError("cannot persist this conversational response")
        result = self.submit_follow_up(question_id, response, "conversation user")
        if result.next_action == "ASK_FOLLOW_UP":
            return {"pending_question": result.follow_up_question, "pending_question_type": result.next_action,
                    "pending_payload": {"question": result.follow_up_question, "question_id": question_id},
                    "missing_fields": list(result.missing_information),
                    "message": {"role": "assistant", "content": result.follow_up_question, "action": "FOLLOW_UP", "status": "UNKNOWN"}}
        affected = self._update_control_rows(control, list(result.claims))
        return {"pending_question": None, "pending_payload": None, "investigation_status": result.status,
                "message": {"role": "assistant", "content": f"Recorded. {result.answer or ''} I updated {len(affected)} related questionnaire item(s).", "action": "UPDATE", "status": result.status}}

    def navigate_chat(self, message: str) -> dict[str, Any]:
        lower = message.casefold()
        if "generate" in lower or "export" in lower:
            paths = self.export()
            p = self.dashboard()["progress"]
            content = f"Questionnaire generated: {p['completed']} verified/user-confirmed, {p['unknown']} unknown, and {p['conflicts']} unresolved conflicts."
            return {"message": {"role": "assistant", "content": content, "action": "NAVIGATE", "downloads": paths}}
        if any(term in lower for term in ("actually", "changed", "now run", "now runs")):
            control = "backups" if "backup" in lower else "mfa" if "mfa" in lower else None
            if not control:
                return {"message": {"role": "assistant", "content": "Which stored security fact should I correct?", "action": "FOLLOW_UP"}}
            history = [entry for entry in self.profile.claim_history(control) if entry.status == "ACTIVE" and entry.claim.evidence_type == "USER_CONFIRMATION"]
            if not history:
                return {"message": {"role": "assistant", "content": f"I don't have a user-confirmed {control} fact to correct yet.", "action": "NAVIGATE"}}
            prior = next((entry for entry in reversed(history) if entry.claim.attribute == "cadence"), history[-1])
            cadence = re.search(r"\b(every\s+(?:\d+|one|two|three|four|five|six|twelve)\s+(?:hours?|days?|weeks?|months?)|hourly|daily|weekly|monthly)\b", lower)
            new_value: Any = cadence.group(1) if cadence else (False if "not" in lower or "no" in lower else True)
            claim = self.profile.correct_claim(prior.claim.id, new_value, message, "conversation user")
            affected = self._update_control_rows(control, [claim])
            return {"message": {"role": "assistant", "content": f"Updated. {prior.claim.attribute}: {prior.claim.value} → {new_value}. I kept the earlier answer in audit history and updated {len(affected)} questionnaire item(s).", "action": "UPDATE", "status": "USER_CONFIRMED"}}
        dashboard = self.dashboard()
        p = dashboard["progress"]
        if "conflict" in lower:
            rows = [row for row in dashboard["questions"] if row["status"] == "CONFLICT"][:8]
            content = f"{p['conflicts']} conflicts remain. " + " ".join(f"{row['id']}: {row['question']}" for row in rows)
        elif "unresolved" in lower or "left" in lower:
            rows = [row for row in dashboard["questions"] if row["status"] in {"UNKNOWN", "CONFLICT"}][:8]
            content = f"{p['unknown']} unknown and {p['conflicts']} conflicts remain. Next items: " + " ".join(f"{row['id']}: {row['question']}" for row in rows)
        else:
            content = f"Security review progress: {p['completed']} completed, {p['unknown']} unknown, {p['conflicts']} conflicts, {p['total']} total."
        return {"message": {"role": "assistant", "content": content, "action": "NAVIGATE"}}

    def investigate(self, question_id: str) -> InvestigationResult:
        item = self._item(question_id)
        result = self.engine.investigate(item)
        self.profile.save_questionnaire_result(result)
        return result

    def analyze_all(self) -> dict[str, Any]:
        for item in self.items:
            result = self.engine.investigate(item)
            self.profile.save_questionnaire_result(result)
        self.export()
        return self.dashboard()

    # ------------------------------------------------------------------
    # Voice channel (ElevenLabs ConvAI server tool)
    # ------------------------------------------------------------------
    # A voice agent produces free text, not a question ID. We match the spoken
    # question to the closest questionnaire item and run the SAME investigation
    # the web UI runs, so spoken answers carry the same evidence guarantee.
    # If nothing matches well enough we say so rather than let the voice model
    # improvise - the golden rule applies on every channel.
    VOICE_STOPWORDS = frozenset({
        "does", "your", "organization", "organisation", "have", "the", "and", "you",
        "for", "are", "any", "with", "that", "this", "from", "what", "how", "who",
        "when", "where", "please", "provide", "describe", "list", "yes", "out",
        "there", "been", "will", "our", "its", "can", "than", "each", "such", "use",
        "performed", "perform", "process", "often", "conduct", "conducted", "place",
        "used", "level", "based", "within", "other", "must", "need", "ensure",
        "include", "including", "relevant", "appropriate", "least", "regarding",
        "organizations", "following", "provided", "available",
    })
    VOICE_ALIASES = {
        "mfa": "mfa", "2fa": "mfa", "multifactor": "mfa", "multi": "mfa",
        "factor": "mfa", "twofactor": "mfa", "otp": "mfa",
        "authenticate": "authentication", "authenticator": "authentication",
        "encrypt": "encryption", "encrypted": "encryption", "cryptography": "encryption",
        "backup": "backup", "backed": "backup", "restore": "backup", "recovery": "backup",
        "pentest": "penetration", "pentesting": "penetration",
        "vuln": "vulnerability", "vulnerabilities": "vulnerability", "scanning": "vulnerability",
        "offboard": "termination", "offboarding": "termination", "terminate": "termination",
        "onboard": "onboarding", "leaver": "termination",
        "prod": "production", "breach": "incident", "incidents": "incident",
        "vendor": "thirdparty", "supplier": "thirdparty", "subprocessor": "thirdparty",
        "background": "screening", "screen": "screening",
        "policies": "policy", "controls": "control", "employees": "employee",
        "staff": "employee", "personnel": "employee", "everyone": "employee",
    }
    VOICE_MIN_SCORE = 0.30
    VOICE_MIN_OVERLAP = 2
    VOICE_SOLO_SCORE = 0.60

    @classmethod
    def _stem(cls, word: str) -> str:
        word = word.replace("-", "")
        word = cls.VOICE_ALIASES.get(word, word)
        for suffix in ("ations", "ation", "ing", "ies", "ed", "es", "s"):
            if len(word) > 5 and word.endswith(suffix):
                base = word[: -len(suffix)]
                if suffix == "ies":
                    base += "y"
                return cls.VOICE_ALIASES.get(base, base)
        return word

    @classmethod
    def _voice_tokens(cls, text: str) -> set[str]:
        words = re.findall(r"[A-Za-z][A-Za-z0-9-]{1,}", text.casefold())
        return {cls._stem(w) for w in words
                if w not in cls.VOICE_STOPWORDS and len(w) > 2}

    def match_question(self, spoken: str) -> tuple[QuestionnaireItem | None, float]:
        asked = self._voice_tokens(spoken)
        if not asked:
            return None, 0.0
        best, best_score = None, 0.0
        for item in self.items:
            target = self._voice_tokens(f"{item.category} {item.question}")
            if not target:
                continue
            overlap = asked & target
            if not overlap:
                continue
            score = (len(overlap) / len(asked)) * 0.7 + (len(overlap) / len(target)) * 0.3
            if len(overlap) < self.VOICE_MIN_OVERLAP and score < self.VOICE_SOLO_SCORE:
                continue
            if score > best_score:
                best, best_score = item, score
        if best is None or best_score < self.VOICE_MIN_SCORE:
            return None, round(best_score, 3)
        return best, round(best_score, 3)

    def voice_ask(self, spoken: str, stakeholder: str | None = None) -> dict[str, Any]:
        spoken = (spoken or "").strip()
        if not spoken:
            raise ValueError("question is required")
        item, score = self.match_question(spoken)
        if item is None:
            return {
                "spoken_answer": "That is not something the questionnaire covers, and I could "
                                 "not find it in the company evidence. Could you rephrase it, or "
                                 "tell me the answer and I will record it?",
                "status": "UNKNOWN", "matched_question": None, "question_id": None,
                "match_score": score, "confidence": 0.0, "sources": [], "follow_up": None,
            }
        result = self.investigate(item.id)
        sources = [self.evidence_by_id[e].source_path for e in result.evidence_ids
                   if e in self.evidence_by_id]
        unique_sources = list(dict.fromkeys(sources))
        if result.status == "CONFLICT" or result.next_action == "RESOLVE_CONFLICT":
            detail = result.conflicts[0].description if result.conflicts else ""
            spoken_answer = (f"The company records disagree on this. {detail} "
                             f"{result.follow_up_question or ''}").strip()
        elif result.next_action == "ASK_FOLLOW_UP":
            spoken_answer = (f"The documents do not go far enough. "
                             f"{result.follow_up_question}").strip()
        elif result.answer:
            body = result.answer.rstrip(" .")
            if unique_sources:
                spoken_answer = f"{body}, according to {PurePosixPath(unique_sources[0]).name}."
            else:
                spoken_answer = f"{body}."
        else:
            spoken_answer = ("I could not verify that in the company evidence, so I am "
                             "marking it unknown rather than guessing.")
        return {
            "spoken_answer": spoken_answer,
            "status": result.status,
            "question_id": result.question_id,
            "matched_question": item.question,
            "match_score": score,
            "confidence": result.confidence,
            "sources": unique_sources[:4],
            "follow_up": result.follow_up_question,
        }

    def voice_record(self, question_id: str, response: str, stakeholder: str | None = None) -> dict[str, Any]:
        result = self.submit_follow_up(question_id, response, stakeholder)
        return {
            "spoken_answer": ("Recorded. I will not ask that again."
                              if result.status in {"USER_CONFIRMED", "VERIFIED"}
                              else f"I still need something more specific. {result.follow_up_question or ''}".strip()),
            "status": result.status,
            "question_id": result.question_id,
        }

    def submit_follow_up(self, question_id: str, response: str, stakeholder: str | None) -> InvestigationResult:
        item = self._item(question_id)
        try:
            result = self.profile.answer_follow_up(item, response, stakeholder, analyst=self.engine)
        except ValueError as exc:
            # Vague answers must not become claims. Re-run the investigation so the precise missing-field
            # question remains visible instead of accepting or embellishing the response.
            LOGGER.info("Follow-up for %s was insufficient (%s); requesting specificity", question_id, exc)
            result = self.engine.investigate(item)
            self.profile.save_questionnaire_result(result)
        self.export()
        return result

    def correct(self, claim_id: str, value: str, raw_response: str, stakeholder: str | None) -> dict[str, Any]:
        history = {entry.claim.id: entry for entry in self.profile.claim_history()}
        if claim_id not in history or history[claim_id].status != "ACTIVE":
            raise ValueError("an active claim must be selected")
        prior = history[claim_id].claim
        if isinstance(prior.value, bool):
            normalized = value.strip().casefold()
            if normalized not in {"yes", "true", "no", "false"}:
                raise ValueError("boolean corrections must be yes/no or true/false")
            corrected_value: bool | str = normalized in {"yes", "true"}
        else:
            corrected_value = value.strip()
            if not corrected_value:
                raise ValueError("corrected value cannot be blank")
        new_claim = self.profile.correct_claim(claim_id, corrected_value, raw_response, stakeholder)
        affected = []
        for item in self.items:
            if normalize_control(item.normalized_control) == prior.control:
                result = self.engine.investigate(item)
                self.profile.save_questionnaire_result(result)
                affected.append(item.id)
        self.export()
        return {"new_claim": new_claim.to_dict(), "superseded_claim_id": claim_id, "affected_questions": affected}

    def evidence(self, identifiers: list[str]) -> list[dict[str, Any]]:
        profile_records = {item.id: item for item in self.profile.get_evidence(identifiers)}
        result = []
        for identifier in identifiers:
            item = self.evidence_by_id.get(identifier) or profile_records.get(identifier)
            if item:
                result.append(asdict(item))
        return result

    def dashboard(self, status_filter: str | None = None) -> dict[str, Any]:
        rows = []
        counts: Counter[str] = Counter()
        for item in self.items:
            state = self.profile.questionnaire_state(item.id)
            status = state.status if state else "UNKNOWN"
            counts[status] += 1
            if status_filter and status_filter != "ALL" and status != status_filter:
                continue
            evidence_ids = list(state.evidence_ids) if state else []
            rows.append({
                "id": item.id, "category": item.category, "question": item.question,
                "answer": state.answer if state else None, "status": status,
                "status_label": STATUS_LABELS[status], "confidence": state.confidence if state else 0.0,
                "evidence_ids": evidence_ids, "evidence": self.evidence(evidence_ids),
                "missing_information": list(state.missing_information) if state else ["not investigated"],
            })
        completed = counts["VERIFIED"] + counts["USER_CONFIRMED"]
        active_user_claims = [
            {**entry.claim.to_dict(), "status": entry.status}
            for entry in self.profile.claim_history()
            if entry.status == "ACTIVE" and entry.claim.evidence_type == "USER_CONFIRMATION"
        ]
        return {
            "progress": {
                "completed": completed, "total": len(self.items), "verified": counts["VERIFIED"],
                "user_confirmed": counts["USER_CONFIRMED"], "unknown": counts["UNKNOWN"], "conflicts": counts["CONFLICT"],
            },
            "questions": rows,
            "active_user_claims": active_user_claims,
            "status_labels": STATUS_LABELS,
        }

    def export(self) -> dict[str, str]:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        states = {item.id: self.profile.questionnaire_state(item.id) for item in self.items}
        states = {key: value for key, value in states.items() if value is not None}
        json_path = self.artifact_dir / "completed_questionnaire.json"
        payload = self.dashboard()
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        xlsx_path = self.artifact_dir / "completed_questionnaire.xlsx"
        export_completed_xlsx(self.source, xlsx_path, self.items, states)
        return {"json": str(json_path), "xlsx": str(xlsx_path)}

    def _item(self, question_id: str) -> QuestionnaireItem:
        try:
            return self.items_by_id[question_id]
        except KeyError as exc:
            raise ValueError(f"unknown question ID: {question_id}") from exc


class Handler(BaseHTTPRequestHandler):
    service: AppService

    def _json(self, payload: Any, status: int = 200) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self._cors()
        self.end_headers()
        self.wfile.write(encoded)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = INDEX_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/dashboard":
            status = parse_qs(parsed.query).get("status", [None])[0]
            self._json(self.service.dashboard(status))
        elif parsed.path == "/api/chat":
            thread_id = parse_qs(parsed.query).get("thread_id", ["default"])[0]
            self._json(self.service.opening(thread_id))
        elif parsed.path.startswith("/download/"):
            kind = parsed.path.rsplit("/", 1)[-1]
            paths = self.service.export()
            if kind not in paths:
                self._json({"error": "unknown export"}, 404)
                return
            path = Path(paths[kind])
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json({"error": "not found"}, 404)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type, authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self._body()
            if self.path == "/api/analyze-all":
                payload = self.service.analyze_all()
            elif self.path == "/api/chat":
                payload = self.service.chat_message(body.get("thread_id", "default"), body["message"])
            elif self.path == "/api/investigate":
                payload = self.service.investigate(body["question_id"]).to_dict()
            elif self.path == "/api/follow-up":
                payload = self.service.submit_follow_up(body["question_id"], body["response"], body.get("stakeholder")).to_dict()
            elif self.path == "/api/correct":
                payload = self.service.correct(body["claim_id"], body["value"], body["raw_response"], body.get("stakeholder"))
            elif self.path == "/api/export":
                payload = self.service.export()
            elif self.path == "/api/voice-ask":
                payload = self.service.voice_ask(body.get("question", ""), body.get("stakeholder"))
            elif self.path == "/api/voice-record":
                payload = self.service.voice_record(body["question_id"], body["response"], body.get("stakeholder"))
            else:
                self._json({"error": "not found"}, 404)
                return
            self._json(payload)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception:
            LOGGER.exception("Unhandled UI request failure")
            self._json({"error": "The request failed safely. Check the application logs for details."}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.info(format, *args)


def make_server(service: AppService, host: str = "127.0.0.1", port: int = 8501) -> ThreadingHTTPServer:
    handler = type("RegoditHandler", (Handler,), {"service": service})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--analyze", action="store_true", help="investigate all questions before serving")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    service = AppService(args.db)
    if args.analyze:
        service.analyze_all()
    server = make_server(service, args.host, args.port)
    print(f"Regodit UI: http://{args.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


LEGACY_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Regodit AI Security Analyst</title>
<style>
:root{--bg:#07111f;--panel:#101d2e;--line:#26374d;--text:#edf4ff;--muted:#91a4bd;--blue:#5ba7ff;--green:#45d19a;--amber:#f7b955;--red:#ff6477;--violet:#b89cff}*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#07111f,#0c1625);color:var(--text);font:14px Inter,system-ui,sans-serif}header{padding:26px 4vw 14px;display:flex;justify-content:space-between;align-items:center}h1{font-size:25px;margin:0}header p{color:var(--muted);margin:5px 0}.shell{padding:0 4vw 50px}.metrics{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}.metric,.panel{background:rgba(16,29,46,.94);border:1px solid var(--line);border-radius:14px}.metric{padding:16px}.metric b{font-size:24px;display:block}.metric span{color:var(--muted)}nav{display:flex;gap:8px;margin:20px 0}.tab,button,select,input,textarea{font:inherit}.tab,button{border:1px solid var(--line);border-radius:9px;background:#17273b;color:var(--text);padding:9px 13px;cursor:pointer}.tab.active,button.primary{background:var(--blue);color:#06101e;border-color:var(--blue);font-weight:700}.view{display:none}.view.active{display:block}.panel{padding:18px;margin-bottom:14px}select,input,textarea{width:100%;background:#091525;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:10px;margin:6px 0 12px}textarea{min-height:95px}label{color:var(--muted);font-weight:600}.status{display:inline-block;border-radius:99px;padding:5px 9px;font-size:12px;font-weight:800}.VERIFIED{background:#163f36;color:var(--green)}.USER_CONFIRMED{background:#29244c;color:var(--violet)}.UNKNOWN{background:#453719;color:var(--amber)}.CONFLICT{background:#481f2a;color:var(--red)}.conflict{border:2px solid var(--red);background:#2a1520}.evidence{border-left:3px solid var(--blue);padding:9px 12px;margin:8px 0;background:#0a1627}.evidence small{color:var(--muted)}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);position:sticky;top:0;background:var(--panel)}.scroll{max-height:620px;overflow:auto}.question{max-width:390px}.muted{color:var(--muted)}.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}.actions{display:flex;gap:8px;flex-wrap:wrap}.error{color:var(--red);white-space:pre-wrap}@media(max-width:850px){.metrics{grid-template-columns:1fr 1fr}.row{grid-template-columns:1fr}table{font-size:12px}}
</style></head><body><header><div><h1>Regodit AI Security Analyst</h1><p>Evidence-first vendor security investigations</p></div><div class="actions"><a href="/download/json"><button>Export JSON</button></a><a href="/download/xlsx"><button>Export XLSX</button></a></div></header>
<main class="shell"><section class="metrics" id="metrics"></section><div class="panel"><span class="status VERIFIED">Verified from company evidence</span> <span class="status USER_CONFIRMED">Confirmed by user</span> <span class="status UNKNOWN">Unknown / needs confirmation</span> <span class="status CONFLICT">Conflict</span></div><nav><button class="tab active" data-view="investigation">Investigation</button><button class="tab" data-view="questionnaire">Questionnaire</button><button class="tab" data-view="profile">Security profile</button></nav>
<section id="investigation" class="view active"><div class="panel"><h2>Investigate a questionnaire item</h2><label>Active question</label><select id="questionSelect"></select><div class="actions"><button class="primary" onclick="investigate()">Search evidence & investigate</button><button onclick="analyzeAll()">Analyze all 66 questions</button></div></div><div id="result"></div></section>
<section id="questionnaire" class="view"><div class="panel"><div class="row"><div><h2>Questionnaire work queue</h2><p class="muted">Every answer retains status, confidence, and evidence.</p></div><div><label>Filter by status</label><select id="filter" onchange="load()"><option>ALL</option><option>VERIFIED</option><option>USER_CONFIRMED</option><option>UNKNOWN</option><option>CONFLICT</option></select></div></div><div class="scroll"><table><thead><tr><th>ID</th><th>Category / question</th><th>Answer</th><th>Status</th><th>Confidence</th><th>Evidence</th></tr></thead><tbody id="questions"></tbody></table></div></div></section>
<section id="profile" class="view"><div class="panel"><h2>Confirmed security profile</h2><p class="muted">Corrections preserve the previous claim as superseded.</p><div id="claims"></div></div></section></main>
<script>
let data;const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab,.view').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.view).classList.add('active')});
async function api(path,body){let r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});let j=await r.json();if(!r.ok)throw Error(j.error||r.statusText);return j}
function badge(s){return `<span class="status ${s}">${esc(data.status_labels[s])}</span>`}
function evidence(items){return items.map(e=>`<div class="evidence"><b>${esc(e.source_name)}</b><br><small>${esc(e.location)} · ${esc(e.evidence_type)} · ${esc(e.id)}</small><p>${esc(e.text||'[Visual evidence — no generated description]')}</p></div>`).join('')||'<p class="muted">No supporting evidence available.</p>'}
async function load(){let f=document.getElementById('filter')?.value||'ALL';data=await api('/api/dashboard?status='+f);let p=data.progress;document.getElementById('metrics').innerHTML=[['Completion',p.completed+' / '+p.total],['Verified',p.verified],['User confirmed',p.user_confirmed],['Unknown',p.unknown],['Conflicts',p.conflicts]].map(x=>`<div class="metric"><b>${x[1]}</b><span>${x[0]}</span></div>`).join('');let sel=document.getElementById('questionSelect'),old=sel.value;sel.innerHTML=data.questions.map(q=>`<option value="${q.id}">${q.id} · ${esc(q.question)}</option>`).join('');if(old)sel.value=old;document.getElementById('questions').innerHTML=data.questions.map(q=>`<tr class="${q.status==='CONFLICT'?'conflict':''}"><td>${q.id}</td><td class="question"><b>${esc(q.category)}</b><br>${esc(q.question)}</td><td>${esc(q.answer||'—')}</td><td>${badge(q.status)}</td><td>${Math.round(q.confidence*100)}%</td><td>${q.evidence.map(e=>`<small>${esc(e.source_name)}<br>${esc(e.location)}</small>`).join('<hr>')||'—'}</td></tr>`).join('');renderClaims()}
function renderClaims(){document.getElementById('claims').innerHTML=data.active_user_claims.map(c=>`<div class="evidence"><b>${esc(c.control)} · ${esc(c.attribute)}</b> ${badge('USER_CONFIRMED')}<p>Current value: <strong>${esc(c.value)}</strong> · Scope: ${esc(c.scope)}</p><details><summary>Correct this fact</summary><label>Corrected value</label><input id="v-${c.id}"><label>Reason / raw correction</label><textarea id="r-${c.id}"></textarea><label>Stakeholder</label><input id="s-${c.id}"><button onclick="correctClaim('${c.id}')">Save correction</button></details></div>`).join('')||'<p class="muted">No user-confirmed facts yet.</p>'}
async function investigate(){let id=document.getElementById('questionSelect').value;try{let r=await api('/api/investigate',{question_id:id});await load();showResult(r)}catch(e){showError(e)}}
function showResult(r){let q=data.questions.find(x=>x.id===r.question_id);let cls=r.status==='CONFLICT'?'panel conflict':'panel';let form=r.follow_up_question&&r.next_action==='ASK_FOLLOW_UP'?`<div><h3>${esc(r.follow_up_question)}</h3><textarea id="response" placeholder="Provide a specific answer"></textarea><input id="stakeholder" placeholder="Stakeholder name or work email (optional)"><button class="primary" onclick="followUp('${r.question_id}')">Confirm answer</button></div>`:'';let conflicts=(r.conflicts||[]).map(c=>`<p><strong>Conflict:</strong> ${esc(c.description)}</p>`).join('');document.getElementById('result').innerHTML=`<div class="${cls}"><h2>${esc(q?.question||r.question_id)}</h2>${badge(r.status)}<h3>${esc(r.answer||r.follow_up_question||'No reliable answer is available.')}</h3>${conflicts}${evidence(q?.evidence||[])}${form}</div>`}
async function followUp(id){try{let r=await api('/api/follow-up',{question_id:id,response:document.getElementById('response').value,stakeholder:document.getElementById('stakeholder').value});await load();showResult(r)}catch(e){showError(e)}}
async function correctClaim(id){try{await api('/api/correct',{claim_id:id,value:document.getElementById('v-'+id).value,raw_response:document.getElementById('r-'+id).value,stakeholder:document.getElementById('s-'+id).value});await load()}catch(e){showError(e)}}
async function analyzeAll(){document.getElementById('result').innerHTML='<div class="panel">Analyzing all questions against the security profile and company evidence…</div>';try{await api('/api/analyze-all',{});await load();document.getElementById('result').innerHTML='<div class="panel"><h2>Analysis complete</h2><p>Progress, statuses, evidence, and exports are updated.</p></div>'}catch(e){showError(e)}}
function showError(e){document.getElementById('result').innerHTML=`<div class="panel error">${esc(e.message)}</div>`}load();
</script></body></html>'''

INDEX_HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Regodit AI Security Analyst</title><style>
:root{--bg:#f6f7fb;--side:#101827;--card:#fff;--line:#e4e7ec;--text:#182230;--muted:#667085;--brand:#4255d4;--green:#087443;--amber:#a15c00;--red:#b42318}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px Inter,system-ui,sans-serif}.app{display:grid;grid-template-columns:235px 1fr;height:100vh}.side{background:var(--side);color:#fff;padding:22px 16px;display:flex;flex-direction:column;gap:8px}.logo{font-size:21px;font-weight:800;padding:4px 8px 20px}.side button{background:transparent;color:#d5d9e3;border:0;text-align:left;padding:11px;border-radius:8px;cursor:pointer;font:inherit}.side button:hover,.side button.active{background:#273449;color:white}.progress{margin-top:auto;background:#1d2939;border-radius:10px;padding:13px;color:#d0d5dd}.progress b{display:block;color:#fff;font-size:18px;margin-bottom:5px}.main{min-width:0;height:100vh}.view{display:none;height:100%}.view.active{display:flex}.chat{flex-direction:column;max-width:930px;margin:auto;background:white;border-left:1px solid var(--line);border-right:1px solid var(--line)}.top{padding:18px 24px;border-bottom:1px solid var(--line);font-weight:750}.messages{flex:1;overflow:auto;padding:28px 9%;display:flex;flex-direction:column;gap:22px}.msg{max-width:82%;line-height:1.55}.msg.user{align-self:flex-end;background:#eef0ff;padding:12px 15px;border-radius:15px}.msg.assistant{align-self:flex-start}.status{font-size:12px;font-weight:800;margin-bottom:5px}.VERIFIED{color:var(--green)}.USER_CONFIRMED{color:#6941c6}.UNKNOWN{color:var(--amber)}.CONFLICT{color:var(--red)}details{margin-top:9px;border:1px solid var(--line);border-radius:9px;padding:9px;background:#fafafa}.source{padding:8px 0;border-top:1px solid var(--line)}.source small{color:var(--muted)}.composer{border-top:1px solid var(--line);padding:14px 7% 20px}.suggestions{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:10px}.suggestions button,.ask,.generate{border:1px solid var(--line);background:white;border-radius:20px;padding:8px 12px;cursor:pointer}.input{display:flex;gap:9px}.input textarea{resize:none;min-height:50px;max-height:120px;flex:1;border:1px solid #cfd4dc;border-radius:14px;padding:14px;font:inherit}.send{background:var(--brand);color:#fff;border:0;border-radius:13px;padding:0 22px;font-weight:700;cursor:pointer}.workspace{padding:28px;overflow:auto;width:100%}.workspace h1{margin-top:0}.toolbar{display:flex;justify-content:space-between;align-items:center}.generate{background:var(--brand);color:#fff;border-color:var(--brand);border-radius:8px}.table{background:#fff;border:1px solid var(--line);border-radius:12px;overflow:auto}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:12px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted)}.pill{font-weight:800;font-size:11px}.muted{color:var(--muted)}@media(max-width:720px){.app{grid-template-columns:76px 1fr}.side button{font-size:0}.side button:first-letter{font-size:16px}.logo{font-size:0}.logo:first-letter{font-size:22px}.progress{display:none}.messages{padding:20px}}
</style></head><body><div class="app"><aside class="side"><div class="logo">Regodit</div><button class="nav active" data-view="chat">💬 Conversation</button><button class="nav" data-view="questionnaire">▦ Questionnaire</button><button class="nav" data-view="profile">◉ Security Profile</button><button class="nav" data-view="conflicts">⚠ Conflicts</button><button class="nav" data-view="evidence">⌕ Evidence</button><div class="progress" id="progress"></div></aside><main class="main">
<section id="chat" class="view chat active"><div class="top">Regodit <span class="muted">· AI Security Analyst</span></div><div class="messages" id="messages"></div><div class="composer"><div class="suggestions" id="suggestions"></div><div class="input"><textarea id="input" placeholder="Ask Regodit or answer the pending question…"></textarea><button class="send" onclick="send()">Send</button></div></div></section>
<section id="questionnaire" class="view workspace"><div><div class="toolbar"><div><h1>Questionnaire</h1><p class="muted">Evidence-backed status and completion queue.</p></div><button class="generate" onclick="sendAction('Generate questionnaire')">Generate Questionnaire</button></div><div class="table"><table><thead><tr><th>ID</th><th>Question</th><th>Answer</th><th>Status</th><th>Action</th></tr></thead><tbody id="questions"></tbody></table></div></div></section>
<section id="profile" class="view workspace"><div><h1>Security Profile</h1><p class="muted">Durable employee-confirmed facts. Corrections retain audit history.</p><div id="claims"></div></div></section>
<section id="conflicts" class="view workspace"><div><h1>Conflicts</h1><div id="conflictRows"></div></div></section>
<section id="evidence" class="view workspace"><div><h1>Evidence</h1><p class="muted">Sources are shown compactly with each verified conversational answer.</p></div></section>
</main></div><script>
let dashboard,thread=localStorage.regoditThread||(localStorage.regoditThread=crypto.randomUUID());const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const j=await r.json();if(!r.ok)throw Error(j.error||r.statusText);return j}
document.querySelectorAll('.nav').forEach(b=>b.onclick=()=>show(b.dataset.view));function show(id){document.querySelectorAll('.nav,.view').forEach(x=>x.classList.remove('active'));document.querySelector(`[data-view="${id}"]`).classList.add('active');document.getElementById(id).classList.add('active')}
function label(s){return {VERIFIED:'✓ Verified from company information',USER_CONFIRMED:'✓ Confirmed by user',UNKNOWN:'Unknown / needs confirmation',CONFLICT:'Conflict'}[s]||''}
function message(m){let sources=(m.evidence||[]).map(e=>`<div class="source"><b>${esc(e.source_name)}</b><br><small>${esc(e.location)} · ${esc(e.evidence_type)}</small><div>${esc(e.text||'[Visual evidence]')}</div></div>`).join('');return `<article class="msg ${m.role}">${m.status?`<div class="status ${m.status}">${label(m.status)}</div>`:''}<div>${esc(m.content)}</div>${sources?`<details><summary>Sources (${m.evidence.length})</summary>${sources}</details>`:''}</article>`}
function renderChat(r){document.getElementById('messages').innerHTML=(r.messages||[]).map(message).join('');document.getElementById('messages').scrollTop=999999;renderSuggestions(r.suggestions||[]);renderProgress(r.progress)}
function renderSuggestions(items){document.getElementById('suggestions').innerHTML=items.map(x=>`<button onclick="sendAction('${esc(x)}')">${esc(x)}</button>`).join('')}
function renderProgress(p){if(!p)return;document.getElementById('progress').innerHTML=`<b>${p.completed} / ${p.total} complete</b>${p.unknown} unknown · ${p.conflicts} conflicts`}
async function send(){const input=document.getElementById('input'),value=input.value.trim();if(!value)return;input.value='';try{renderChat(await api('/api/chat',{thread_id:thread,message:value}));await loadDashboard();show('chat')}catch(e){alert(e.message)}}function sendAction(value){document.getElementById('input').value=value;send()}
function ask(id){sendAction(`Investigate ${id}`)}
async function loadDashboard(){dashboard=await api('/api/dashboard');renderProgress(dashboard.progress);document.getElementById('questions').innerHTML=dashboard.questions.map(q=>`<tr><td>${q.id}</td><td><b>${esc(q.category)}</b><br>${esc(q.question)}</td><td>${esc(q.answer||'—')}</td><td><span class="pill ${q.status}">${label(q.status)}</span></td><td>${q.status==='UNKNOWN'||q.status==='CONFLICT'?`<button class="ask" onclick="ask('${q.id}')">Ask Regodit</button>`:'—'}</td></tr>`).join('');document.getElementById('claims').innerHTML=dashboard.active_user_claims.map(c=>`<details open><summary><b>${esc(c.control)} · ${esc(c.attribute)}</b></summary><p>${esc(c.value)} · ${esc(c.scope)}</p></details>`).join('')||'<p>No employee-confirmed facts yet.</p>';let conflicts=dashboard.questions.filter(q=>q.status==='CONFLICT');document.getElementById('conflictRows').innerHTML=conflicts.map(q=>`<details open><summary>${q.id} · ${esc(q.question)}</summary><p>${esc(q.answer||'Conflicting evidence requires clarification.')}</p><button class="ask" onclick="ask('${q.id}')">Resolve in chat</button></details>`).join('')||'<p>No unresolved conflicts.</p>'}
document.getElementById('input').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}});Promise.all([api('/api/chat?thread_id='+encodeURIComponent(thread)).then(renderChat),loadDashboard()]);
</script>
<elevenlabs-convai agent-id="agent_1601m1s5w1r1e2zvq2zzp5ez0w26"></elevenlabs-convai>
<script src="https://unpkg.com/@elevenlabs/convai-widget-embed" async type="text/javascript"></script>
</body></html>'''


if __name__ == "__main__":
    main()
