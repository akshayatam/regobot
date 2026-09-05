"""Developer CLI for inspecting retrieval scores, provenance, and extracted claims."""

from __future__ import annotations

import argparse
import json
import logging

from regodit.analyst.claims import extract_claims
from .engine import default_retriever


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="security question to investigate")
    parser.add_argument("--control")
    parser.add_argument("--organization", default="Regodit")
    parser.add_argument("--top-k", type=int, default=8)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    hits = default_retriever().search(args.query, args.control, args.organization, args.top_k)
    print("RETRIEVED EVIDENCE")
    for rank, hit in enumerate(hits, 1):
        item = hit.evidence
        print(f"\n[{rank}] {item.id} score={hit.score:.3f}")
        print(f"    source={item.source_name} | {item.location} | entity={item.organization} | type={item.evidence_type}")
        print(f"    why={hit.explanation()}")
        print(f"    text={item.text[:600].replace(chr(10), ' ')}")
    result = extract_claims((hit.evidence for hit in hits), args.control)
    print("\nEXTRACTED CLAIMS")
    print(json.dumps({"claims": [claim.to_dict() for claim in result.claims], "insufficient_evidence": result.insufficient_evidence, "reason": result.reason}, indent=2))


if __name__ == "__main__":
    main()
