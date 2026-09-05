#!/usr/bin/env python3
"""Retest the questionnaire with the currently configured model.

Usable straight after editing `.env`, without starting the web application:

    python scripts/retest.py --scope unresolved
    python scripts/retest.py --scope all
    python scripts/retest.py --question VSQ-060
"""

from __future__ import annotations

import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src"
if SOURCE.is_dir() and str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from regodit.retest import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
