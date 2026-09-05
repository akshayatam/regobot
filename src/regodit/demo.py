"""Create a deterministic demo profile that showcases evidence, follow-up, conflict, and memory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from regodit.config import ARTIFACT_DIR
from regodit.ui import AppService

DEFAULT_DEMO_DB = ARTIFACT_DIR / "demo_security_profile.sqlite3"


def seed_demo(path: Path, force: bool = False, artifact_dir: Path | None = None) -> dict[str, object]:
    if path.exists():
        if not force:
            raise FileExistsError(f"demo database already exists: {path}; pass --force to replace it")
        path.unlink()
    service = AppService(path, artifact_dir or path.parent)

    verified = service.investigate("VSQ-020")
    missing = service.investigate("VSQ-019")
    vague = service.submit_follow_up("VSQ-039", "Yes.", "demo.user@regodit.example")

    service.profile.record_user_claim(
        "mfa", "implemented", False, "active account",
        "One active account may not have MFA enabled.", "demo.security.owner@regodit.example",
    )
    conflict = service.investigate("VSQ-060")

    service.profile.record_user_claim(
        "backups", "cadence", "daily", "production database",
        "Production database backups run daily.", "demo.security.owner@regodit.example",
    )
    exports = service.export()
    result = {
        "database": str(path),
        "verified": {"question_id": verified.question_id, "status": verified.status, "evidence_ids": list(verified.evidence_ids)},
        "missing": {"question_id": missing.question_id, "follow_up": missing.follow_up_question},
        "vague_answer": {"question_id": vague.question_id, "action": vague.next_action, "follow_up": vague.follow_up_question},
        "conflict": {"question_id": conflict.question_id, "status": conflict.status, "question": conflict.follow_up_question},
        "exports": exports,
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DEMO_DB)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    print(json.dumps(seed_demo(args.db, args.force), indent=2))
    print(f"\nRun: python -m regodit serve --db {args.db}")


if __name__ == "__main__":
    main()
