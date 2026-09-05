"""Run the analyst against an actual questionnaire item."""

from __future__ import annotations

import argparse
import json

from regodit.config import DATA_DIR
from regodit.questionnaire import QUESTIONNAIRE_NAME, parse_questionnaire
from .engine import investigate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question_id", help="stable ID such as VSQ-020")
    args = parser.parse_args()
    source = next(DATA_DIR.rglob(QUESTIONNAIRE_NAME))
    items = {item.id: item for item in parse_questionnaire(source)}
    if args.question_id not in items:
        parser.error(f"unknown question ID: {args.question_id}")
    print(json.dumps(investigate(items[args.question_id]).to_dict(), indent=2))


if __name__ == "__main__":
    main()
