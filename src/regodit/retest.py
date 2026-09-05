"""Replay questionnaire items through the currently configured model.

A retest re-evaluates questions against the same underlying evidence, the current security
profile, and current user confirmations. It never clears the questionnaire, never deletes
evidence, and never lets a model conclusion overwrite a confirmed fact.
"""

from __future__ import annotations

import argparse
import json
import logging
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from regodit.analyst import AnalystEngine, InvestigationResult, normalize_control
from regodit.config import model_source
from regodit.memory import SecurityProfile
from regodit.models import QuestionnaireItem
from regodit.sync import QuestionnaireSynchronizer

LOGGER = logging.getLogger("regodit.retest")
SCOPES = ("unresolved", "all", "selected")
UNRESOLVED = {"UNKNOWN", "CONFLICT"}
RESOLVED = {"VERIFIED", "USER_CONFIRMED"}

# Part 9 precedence. A model conclusion is never independently authoritative, so it sits last.
PRECEDENCE = (
    "current direct operational evidence",
    "current assessment evidence",
    "valid current user confirmation",
    "current policy requirement",
    "informal observation",
    "model inference",
)


@dataclass(frozen=True)
class RetestOutcome:
    question_id: str
    control: str
    previous_status: str
    previous_answer: str | None
    previous_model: str | None
    previous_evidence_ids: tuple[str, ...]
    new_status: str
    new_answer: str | None
    new_evidence_ids: tuple[str, ...]
    model: str
    accepted: bool
    decision: str
    note: str
    conflict_resolved: bool = False
    new_conflict: bool = False
    suspicious_upgrade: bool = False

    @property
    def changed(self) -> bool:
        return self.accepted and self.previous_status != self.new_status

    def to_dict(self) -> dict[str, Any]:
        record = asdict(self)
        record["previous_evidence_ids"] = list(self.previous_evidence_ids)
        record["new_evidence_ids"] = list(self.new_evidence_ids)
        record["changed"] = self.changed
        return record


@dataclass
class RetestReport:
    run_id: str
    model: str
    model_source: str
    scope: str
    previous_models: list[str] = field(default_factory=list)
    outcomes: list[RetestOutcome] = field(default_factory=list)
    before: dict[str, int] = field(default_factory=dict)
    after: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        changed = [outcome for outcome in self.outcomes if outcome.changed]
        return {
            "run_id": self.run_id,
            "model": self.model,
            "model_source": self.model_source,
            "previous_models": self.previous_models,
            "scope": self.scope,
            "evaluated": len(self.outcomes),
            "newly_resolved": sum(
                1 for outcome in changed
                if outcome.previous_status in UNRESOLVED and outcome.new_status in RESOLVED
            ),
            "still_unknown": sum(1 for outcome in self.outcomes if outcome.new_status == "UNKNOWN"),
            "conflicts_resolved": sum(1 for outcome in self.outcomes if outcome.conflict_resolved),
            "new_conflicts": sum(1 for outcome in self.outcomes if outcome.new_conflict),
            "not_applied": sum(1 for outcome in self.outcomes if not outcome.accepted),
            "suspicious_upgrades": [
                outcome.question_id for outcome in self.outcomes if outcome.suspicious_upgrade
            ],
            "before": self.before,
            "after": self.after,
            "changes": [outcome.to_dict() for outcome in changed],
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "precedence": list(PRECEDENCE),
        }


def _agrees(previous: str | None, current: str | None) -> bool:
    """Conservatively decide whether a retest answer says the same thing as the stored one.

    Anything that cannot be established as agreement is treated as disagreement, so a confirmed
    fact is preserved and the model reading is kept as a conflict candidate instead.
    """
    if previous is None or current is None:
        return False
    first, second = previous.strip().casefold(), current.strip().casefold()
    if first == second:
        return True
    polarity = ("yes", "no")
    first_polarity = next((value for value in polarity if first.startswith(value)), None)
    second_polarity = next((value for value in polarity if second.startswith(value)), None)
    if first_polarity and second_polarity:
        return first_polarity == second_polarity
    return False


def _counts(states: dict[str, Any], items: Sequence[QuestionnaireItem]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for item in items:
        state = states.get(item.id)
        counter[state.status if state else "UNKNOWN"] += 1
    return {
        "verified": counter["VERIFIED"], "user_confirmed": counter["USER_CONFIRMED"],
        "resolved": counter["VERIFIED"] + counter["USER_CONFIRMED"],
        "unknown": counter["UNKNOWN"], "conflict": counter["CONFLICT"], "total": len(items),
    }


class RetestService:
    """Re-evaluates questionnaire items with the current model under evidence-aware precedence."""

    def __init__(
        self,
        items: Sequence[QuestionnaireItem],
        profile: SecurityProfile,
        engine: AnalystEngine,
        synchronizer: QuestionnaireSynchronizer | None = None,
    ):
        self.items = list(items)
        self.items_by_id = {item.id: item for item in self.items}
        self.profile = profile
        self.engine = engine
        self.synchronizer = synchronizer or QuestionnaireSynchronizer(self.items, profile, engine)

    @property
    def model(self) -> str:
        return self.engine.model_runtime.model

    def select(self, scope: str, question_ids: Iterable[str] | None = None) -> list[QuestionnaireItem]:
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
        if scope == "selected":
            identifiers = list(dict.fromkeys(question_ids or ()))
            if not identifiers:
                raise ValueError("the selected scope requires at least one question ID")
            unknown = [value for value in identifiers if value not in self.items_by_id]
            if unknown:
                raise ValueError(f"unknown question ID(s): {', '.join(sorted(unknown))}")
            return [self.items_by_id[value] for value in identifiers]
        if scope == "all":
            return list(self.items)
        states = self.profile.questionnaire_states()
        return [
            item for item in self.items
            if (states[item.id].status if item.id in states else "UNKNOWN") in UNRESOLVED
        ]

    def _previous_model(self, question_id: str) -> str | None:
        history = [entry for entry in self.profile.evaluation_history(question_id) if entry.accepted]
        return history[-1].model if history else None

    def _decide(self, previous: Any, result: InvestigationResult) -> tuple[bool, str, str]:
        """Apply Part 9 retest safety. Returns (accepted, decision, note)."""
        previous_status = previous.status if previous else "UNKNOWN"
        if previous_status == "USER_CONFIRMED" and result.status != "USER_CONFIRMED":
            if result.status == "VERIFIED" and _agrees(previous.answer if previous else None, result.answer):
                # Current company evidence outranks a user confirmation only when it agrees with it.
                return True, "upgraded_to_verified", (
                    "The retest confirmed the same answer directly from company evidence."
                )
            if result.status in RESOLVED or result.status == "CONFLICT":
                return False, "conflict_candidate", (
                    "The retest disagrees with a current user-confirmed fact. The confirmed state was kept "
                    "and the model result was preserved as a conflict candidate for review."
                )
            return False, "kept_user_confirmed", (
                "The retest could not reproduce the confirmed answer. A user confirmation outranks a model "
                "inference, so the confirmed state was kept."
            )
        if previous_status == "VERIFIED" and result.status == "UNKNOWN":
            return False, "kept_verified", (
                "The retest found no supporting evidence, but the stored answer is backed by company "
                "evidence. Evidence outranks a model inference, so the verified state was kept."
            )
        if previous_status in UNRESOLVED and result.status in RESOLVED:
            return True, "resolved", "The retest resolved a previously unresolved question."
        if result.status == "CONFLICT" and previous_status != "CONFLICT":
            return True, "new_conflict", "The retest surfaced a contradiction that must be clarified."
        if previous_status == result.status:
            return True, "unchanged", "The retest reached the same status."
        return True, "updated", "The retest updated the questionnaire status."

    def retest_question(self, item: QuestionnaireItem, run_id: str, organization: str = "Regodit") -> RetestOutcome:
        previous = self.profile.questionnaire_state(item.id)
        previous_status = previous.status if previous else "UNKNOWN"
        previous_evidence = tuple(previous.evidence_ids) if previous else ()
        result = self.engine.investigate(item, organization)
        accepted, decision, note = self._decide(previous, result)
        new_evidence = tuple(result.evidence_ids)
        # Part 8: an upgrade that rests on no evidence the previous run had not already seen is
        # a reasoning change, not a new finding, and is flagged rather than trusted.
        suspicious = (
            accepted and previous_status == "UNKNOWN" and result.status == "VERIFIED"
            and not set(new_evidence) - set(previous_evidence)
        )
        if suspicious:
            note += " Flagged: resolved without evidence the previous run had not already seen."
        outcome = RetestOutcome(
            question_id=item.id, control=normalize_control(item.normalized_control),
            previous_status=previous_status, previous_answer=previous.answer if previous else None,
            previous_model=self._previous_model(item.id), previous_evidence_ids=previous_evidence,
            new_status=result.status, new_answer=result.answer, new_evidence_ids=new_evidence,
            model=self.model, accepted=accepted, decision=decision, note=note,
            conflict_resolved=accepted and previous_status == "CONFLICT" and result.status != "CONFLICT",
            new_conflict=accepted and result.status == "CONFLICT" and previous_status != "CONFLICT",
            suspicious_upgrade=suspicious,
        )
        if accepted:
            self.profile.save_questionnaire_result(
                result, model=self.model, run_type="model_retest", run_id=run_id, note=note,
            )
        else:
            # The rejected evaluation is still recorded so both readings remain comparable.
            self.profile.record_evaluation(
                question_id=item.id, model=self.model, run_type="model_retest", run_id=run_id,
                answer=result.answer, status=result.status, confidence=result.confidence,
                evidence_ids=result.evidence_ids,
                conflicts=tuple(conflict.description for conflict in result.conflicts),
                previous_status=previous_status, accepted=False, note=note,
            )
            if decision == "conflict_candidate":
                self.profile.record_conflict_candidate(
                    question_id=item.id, control=outcome.control, model=self.model,
                    existing_status=previous_status, existing_answer=previous.answer if previous else None,
                    proposed_status=result.status, proposed_answer=result.answer,
                    evidence_ids=result.evidence_ids, reason=note,
                )
        self.engine.observer.retest(
            question_id=item.id, control=outcome.control, model=self.model, run_id=run_id,
            previous_status=previous_status, new_status=result.status,
            conflict_resolved=outcome.conflict_resolved, accepted=accepted, decision=decision,
            evidence_count=len(new_evidence),
        )
        return outcome

    def run(
        self,
        scope: str = "unresolved",
        question_ids: Iterable[str] | None = None,
        organization: str = "Regodit",
    ) -> RetestReport:
        selected = self.select(scope, question_ids)
        run_id = f"retest-{uuid.uuid4()}"
        report = RetestReport(run_id=run_id, model=self.model, model_source=model_source(), scope=scope)
        report.before = _counts(self.profile.questionnaire_states(), self.items)
        report.previous_models = sorted({
            entry.model for entry in self.profile.evaluation_history()
            if entry.accepted and entry.model not in {self.model, "user"}
        })
        LOGGER.info("Starting %s retest of %d question(s) with model %s", scope, len(selected), self.model)
        for item in selected:
            report.outcomes.append(self.retest_question(item, run_id, organization))
        report.after = _counts(self.profile.questionnaire_states(), self.items)
        self.engine.flush_traces()
        LOGGER.info("Retest %s complete: %s", run_id, report.summary()["newly_resolved"])
        return report


def build_service(db_path: Path | str | None = None) -> Any:
    """Construct the full application service so a CLI retest uses the same wiring as the UI."""
    from regodit.ui.app import AppService

    return AppService(db_path) if db_path else AppService()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m regodit.retest",
        description="Re-evaluate questionnaire items with the currently configured model.",
    )
    parser.add_argument("--scope", choices=SCOPES, default="unresolved")
    parser.add_argument("--unresolved", action="store_const", const="unresolved", dest="scope",
                        help="shorthand for --scope unresolved")
    parser.add_argument("--all", action="store_const", const="all", dest="scope", help="shorthand for --scope all")
    parser.add_argument("--question", action="append", default=[],
                        help="question ID to retest; repeatable and implies --scope selected")
    parser.add_argument("--db", help="security profile database (defaults to the configured profile)")
    parser.add_argument("--json", action="store_true", help="print the full machine-readable report")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")

    scope = "selected" if args.question else args.scope
    service = build_service(args.db)
    try:
        report = service.retest(scope, args.question)
    except ValueError as exc:
        parser.error(str(exc))
        return 2
    finally:
        service.close()
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"Active model: {report['model']} (from {report['model_source']})")
    if report["previous_models"]:
        print(f"Previous model(s): {', '.join(report['previous_models'])}")
    print(f"Scope: {report['scope']}    Questions evaluated: {report['evaluated']}")
    print(f"Newly resolved: {report['newly_resolved']}")
    print(f"Still unknown: {report['still_unknown']}")
    print(f"Conflicts resolved: {report['conflicts_resolved']}")
    print(f"New conflicts: {report['new_conflicts']}")
    print(f"Not applied (existing state kept): {report['not_applied']}")
    if report["suspicious_upgrades"]:
        print(f"Flagged upgrades without stronger evidence: {', '.join(report['suspicious_upgrades'])}")
    before, after = report["before"], report["after"]
    print(f"\nBefore: {before['resolved']} resolved, {before['unknown']} unknown, {before['conflict']} conflict")
    print(f"After:  {after['resolved']} resolved, {after['unknown']} unknown, {after['conflict']} conflict")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
