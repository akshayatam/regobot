"""Run the real dataset-backed model and PRISM evaluation scenarios."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from regodit.config import ARTIFACT_DIR
from regodit.ui import AppService


def run_traced_evaluation(path: Path, force: bool = False) -> dict[str, object]:
    if path.exists():
        if not force:
            raise FileExistsError(f"evaluation database exists: {path}; pass --force to replace it")
        path.unlink()
    session_id = f"regodit-evaluation-{uuid.uuid4()}"
    service = AppService(path, path.parent, session_id=session_id)
    if not service.engine.model_runtime.enabled:
        raise RuntimeError("OPENAI_API_KEY is required for a genuine model evaluation")
    if not service.engine.observer.enabled:
        raise RuntimeError("PRISMTRACE_API_KEY, PRISMTRACE_PROJECT_ID, and PRISMTRACE_HOST are required")

    verified = service.investigate("VSQ-020")
    missing = service.investigate("VSQ-019")
    irrelevant = service.investigate("VSQ-041")
    irrelevant_evidence = service.evidence(list(irrelevant.evidence_ids))

    service.profile.record_user_claim(
        "mfa", "implemented", False, "active account",
        "One active account may not have MFA enabled.", "evaluation-owner@regodit.example",
    )
    conflict = service.investigate("VSQ-060")
    confirmed = service.submit_follow_up(
        "VSQ-019", "Customer data is stored in the United States.",
        "evaluation-owner@regodit.example",
    )
    service.engine.flush_traces()
    return {
        "session_id": session_id,
        "prism_trajectory_ids": list(service.engine.observer.trajectory_ids),
        "prism_evaluations": service.engine.observer.evaluations(),
        "verified": {"status": verified.status, "question_id": verified.question_id},
        "missing": {"status": missing.status, "follow_up": missing.follow_up_question},
        "conflict": {"status": conflict.status, "follow_up": conflict.follow_up_question},
        "irrelevant_evidence": {
            "status": irrelevant.status,
            "solsphere_used": any(record["organization"] == "Solsphere" for record in irrelevant_evidence),
        },
        "user_confirmation": {"status": confirmed.status, "question_id": confirmed.question_id},
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ARTIFACT_DIR / "prism_evaluation.sqlite3")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run_traced_evaluation(args.db, args.force), indent=2))


if __name__ == "__main__":
    main()
