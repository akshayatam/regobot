"""Canonical Regodit command-line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(prog="regodit", description="Regodit AI Security Analyst")
    subcommands = parser.add_subparsers(dest="command", required=True)
    initialize = subcommands.add_parser("initialize", help="audit data and rebuild the evidence repository")
    initialize.add_argument("--verbose", action="store_true")
    serve = subcommands.add_parser("serve", help="launch the local web application")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--db")
    serve.add_argument("--analyze", action="store_true")
    demo = subcommands.add_parser("demo", help="seed the deterministic judging profile")
    demo.add_argument("--db")
    demo.add_argument("--force", action="store_true")
    retest = subcommands.add_parser("retest", help="re-evaluate questions with the configured model")
    retest.add_argument("--scope", choices=("unresolved", "all", "selected"), default="unresolved")
    retest.add_argument("--unresolved", action="store_const", const="unresolved", dest="scope")
    retest.add_argument("--all", action="store_const", const="all", dest="scope")
    retest.add_argument("--question", action="append", default=[])
    retest.add_argument("--db")
    retest.add_argument("--json", action="store_true")
    retest.add_argument("--verbose", action="store_true")
    trace_eval = subcommands.add_parser("trace-evaluation", help="run real OpenAI + PRISM evaluation scenarios")
    trace_eval.add_argument("--db")
    trace_eval.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.command == "initialize":
        from regodit.ingestion import ingest_all, write_repository
        from regodit.questionnaire import generate
        logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
        manifest, items = generate()
        records, report = ingest_all()
        write_repository(records, report)
        print(f"Initialized {len(manifest)} source files, {len(items)} questions, and {len(records)} evidence records.")
        if report["failed_file_count"] or report["unsupported_file_count"]:
            raise SystemExit(1)
    elif args.command == "serve":
        from regodit.ui.app import main as serve_main
        forwarded = [sys.argv[0]]
        if args.host:
            forwarded += ["--host", args.host]
        if args.port:
            forwarded += ["--port", str(args.port)]
        if args.db:
            forwarded += ["--db", args.db]
        if args.analyze:
            forwarded.append("--analyze")
        sys.argv = forwarded
        serve_main()
    elif args.command == "retest":
        from regodit.retest import main as retest_main
        forwarded = ["--scope", args.scope]
        for question_id in args.question:
            forwarded += ["--question", question_id]
        if args.db:
            forwarded += ["--db", args.db]
        if args.json:
            forwarded.append("--json")
        if args.verbose:
            forwarded.append("--verbose")
        raise SystemExit(retest_main(forwarded))
    elif args.command == "demo":
        from regodit.demo import DEFAULT_DEMO_DB, seed_demo
        path = Path(args.db) if args.db else DEFAULT_DEMO_DB
        result = seed_demo(path, args.force)
        print(json.dumps(result, indent=2))
        print(f"\nRun: python -m regodit serve --db {path}")
    else:
        from regodit.config import ARTIFACT_DIR
        from regodit.evaluation import run_traced_evaluation
        path = Path(args.db) if args.db else ARTIFACT_DIR / "prism_evaluation.sqlite3"
        try:
            result = run_traced_evaluation(path, args.force)
        except (RuntimeError, FileExistsError) as exc:
            parser.error(str(exc))
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
