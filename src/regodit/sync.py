"""Centralized questionnaire synchronization.

Every path that changes durable security knowledge - a clarification, a correction, a resolved
conflict, or an accepted model retest - funnels through here so the questionnaire table can never
drift away from the security profile and no caller has to remember which rows are affected.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Sequence

from regodit.analyst import AnalystEngine, normalize_control
from regodit.memory import SecurityProfile
from regodit.models import QuestionnaireItem

LOGGER = logging.getLogger("regodit.sync")
RESOLVED = {"VERIFIED", "USER_CONFIRMED"}


@dataclass(frozen=True)
class RowChange:
    question_id: str
    previous_status: str
    new_status: str
    answer: str | None
    source: str

    @property
    def changed(self) -> bool:
        return self.previous_status != self.new_status

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "changed": self.changed}


class QuestionnaireSynchronizer:
    """Recomputes and persists every questionnaire row affected by a security-profile change."""

    def __init__(self, items: Sequence[QuestionnaireItem], profile: SecurityProfile, engine: AnalystEngine):
        self.items = list(items)
        self.profile = profile
        self.engine = engine

    def affected_questions(self, control: str) -> list[QuestionnaireItem]:
        """Every questionnaire item mapped to the same normalized control, not just the active one."""
        target = normalize_control(control)
        return [item for item in self.items if normalize_control(item.normalized_control) == target]

    def _status(self, question_id: str) -> str:
        state = self.profile.questionnaire_state(question_id)
        return state.status if state else "UNKNOWN"

    def _confirmed_summary(self, control: str, organization: str) -> tuple[str, list[str]] | None:
        """Summarize the active user-confirmed claims for a control, if any support a row answer."""
        active = [claim for claim in self.profile.lookup(control, organization).claims if claim.evidence_ids]
        if not active:
            return None
        answer = "; ".join(f"{claim.attribute}={claim.value}" for claim in active)
        evidence_ids = [identifier for claim in active for identifier in claim.evidence_ids]
        return answer, evidence_ids

    def synchronize_control(
        self,
        control: str,
        reason: str,
        organization: str = "Regodit",
        run_id: str = "profile_change",
        run_type: str = "profile_sync",
    ) -> list[RowChange]:
        """Recompute every row mapped to `control` and persist the result immediately."""
        normalized = normalize_control(control)
        affected = self.affected_questions(normalized)
        if not affected:
            LOGGER.info("No questionnaire rows are mapped to control %s", normalized)
            return []
        summary = self._confirmed_summary(normalized, organization)
        changes: list[RowChange] = []
        for item in affected:
            previous = self._status(item.id)
            result = self.engine.investigate(item, organization)
            if result.status in RESOLVED:
                # Evidence and the profile now settle this row on their own.
                self.profile.save_questionnaire_result(result, run_type=run_type, run_id=run_id, note=reason)
                changes.append(RowChange(item.id, previous, result.status, result.answer, "investigation"))
                continue
            if summary:
                # A confirmed claim answers the row even when the analyst needs a narrower attribute.
                answer, evidence_ids = summary
                self.profile.save_confirmed_questionnaire_state(
                    item.id, answer, evidence_ids, run_type=run_type, note=reason,
                )
                changes.append(RowChange(item.id, previous, "USER_CONFIRMED", answer, "user_confirmation"))
                continue
            self.profile.save_questionnaire_result(result, run_type=run_type, run_id=run_id, note=reason)
            changes.append(RowChange(item.id, previous, result.status, result.answer, "investigation"))
        LOGGER.info(
            "Synchronized %d questionnaire row(s) for %s; %d changed status",
            len(changes), normalized, sum(change.changed for change in changes),
        )
        return changes

    def synchronize_controls(self, controls: Iterable[str], reason: str, **kwargs: Any) -> list[RowChange]:
        changes: list[RowChange] = []
        for control in dict.fromkeys(normalize_control(value) for value in controls if value):
            changes.extend(self.synchronize_control(control, reason, **kwargs))
        return changes
