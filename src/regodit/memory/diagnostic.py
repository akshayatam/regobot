"""Inspect persisted security-profile claim history."""

from __future__ import annotations

import argparse
import json

from .profile import DEFAULT_DB, SecurityProfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=str, default=str(DEFAULT_DB))
    parser.add_argument("--control")
    args = parser.parse_args()
    profile = SecurityProfile(args.db)
    history = profile.claim_history(args.control)
    print(json.dumps([
        {**entry.claim.to_dict(), "status": entry.status, "created_at": entry.created_at,
         "updated_at": entry.updated_at, "supersedes": entry.supersedes,
         "superseded_by": entry.superseded_by, "source_type": entry.source_type}
        for entry in history
    ], indent=2))


if __name__ == "__main__":
    main()
