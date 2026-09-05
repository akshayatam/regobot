"""Central project paths and environment-based runtime configuration."""

from __future__ import annotations

import os
from pathlib import Path

def _default_project_root() -> Path:
    """Locate a checkout when run from source or from an editable install."""
    candidates = (Path.cwd(), Path(__file__).resolve().parents[2])
    for candidate in candidates:
        if (candidate / "data").is_dir() and (candidate / "BASE_MODEL_PROMPT.md").is_file():
            return candidate
    return Path.cwd()


PROJECT_ROOT = Path(os.environ.get("REGODIT_PROJECT_ROOT", _default_project_root())).resolve()
DATA_DIR = Path(os.environ.get("REGODIT_DATA_DIR", PROJECT_ROOT / "data")).resolve()
ARTIFACT_DIR = Path(os.environ.get("REGODIT_ARTIFACT_DIR", PROJECT_ROOT / "artifacts")).resolve()
EVIDENCE_PATH = Path(os.environ.get("REGODIT_EVIDENCE_PATH", ARTIFACT_DIR / "evidence.jsonl")).resolve()
PROFILE_DB = Path(os.environ.get("REGODIT_PROFILE_DB", ARTIFACT_DIR / "security_profile.sqlite3")).resolve()
HOST = os.environ.get("REGODIT_HOST", "127.0.0.1")
PORT = int(os.environ.get("REGODIT_PORT", "8501"))
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai").strip().casefold()
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-5-mini").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
PRISMTRACE_API_KEY = os.environ.get("PRISMTRACE_API_KEY", "").strip()
PRISMTRACE_PROJECT_ID = os.environ.get("PRISMTRACE_PROJECT_ID", "").strip()
PRISMTRACE_HOST = os.environ.get("PRISMTRACE_HOST", "").strip()
PRISMTRACE_AGENT_NAME = os.environ.get("PRISMTRACE_AGENT_NAME", "regodit-security-analyst").strip()
