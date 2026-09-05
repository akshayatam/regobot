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


def _path_setting(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).resolve() if value else default.resolve()


PROJECT_ROOT = _path_setting("REGODIT_PROJECT_ROOT", _default_project_root())
DATA_DIR = _path_setting("REGODIT_DATA_DIR", PROJECT_ROOT / "data")
ARTIFACT_DIR = _path_setting("REGODIT_ARTIFACT_DIR", PROJECT_ROOT / "artifacts")
EVIDENCE_PATH = _path_setting("REGODIT_EVIDENCE_PATH", ARTIFACT_DIR / "evidence.jsonl")
PROFILE_DB = _path_setting("REGODIT_PROFILE_DB", ARTIFACT_DIR / "security_profile.sqlite3")
CONVERSATION_DB = _path_setting("REGODIT_CONVERSATION_DB", ARTIFACT_DIR / "conversations.sqlite3")
HOST = os.environ.get("REGODIT_HOST", "127.0.0.1")
PORT = int(os.environ.get("REGODIT_PORT", "8501"))
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai").strip().casefold()
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-5-mini").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
PRISMTRACE_API_KEY = os.environ.get("PRISMTRACE_API_KEY", "").strip()
PRISMTRACE_PROJECT_ID = os.environ.get("PRISMTRACE_PROJECT_ID", "").strip()
PRISMTRACE_HOST = os.environ.get("PRISMTRACE_HOST", "").strip()
PRISMTRACE_AGENT_NAME = os.environ.get("PRISMTRACE_AGENT_NAME", "regodit-security-analyst").strip()
