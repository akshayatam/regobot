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


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """Populate os.environ from a .env file so a restart picks up configuration edits.

    Real environment variables always win, which keeps `LLM_MODEL=x python -m regodit serve`
    predictable and keeps secrets out of the file when they are exported by a shell.
    """
    applied: dict[str, str] = {}
    if path is not None:
        candidates = [Path(path)]
    else:
        override = os.environ.get("REGODIT_PROJECT_ROOT", "").strip()
        candidates = [Path(override) / ".env"] if override else []
        candidates += [Path.cwd() / ".env", _default_project_root() / ".env"]
    env_path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if env_path is None:
        return applied
    try:
        raw = env_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return applied
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if not key or key in os.environ:
            continue
        os.environ[key] = value
        applied[key] = value
    return applied


load_dotenv()


def _path_setting(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).resolve() if value else default.resolve()


DEFAULT_LLM_MODEL = "gpt-5-mini"


def active_model() -> str:
    """Resolve the configured reasoning model at call time.

    `OPENAI_MODEL` is the documented Phase 10 switch; `LLM_MODEL` is kept for the
    configuration written by earlier phases. Business logic must call this rather
    than hardcode a model name so a restart with a different value takes effect.
    """
    for name in ("OPENAI_MODEL", "LLM_MODEL"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return DEFAULT_LLM_MODEL


def model_source() -> str:
    """Name the environment variable that supplied the active model, for the UI."""
    for name in ("OPENAI_MODEL", "LLM_MODEL"):
        if os.environ.get(name, "").strip():
            return name
    return "default"


PROJECT_ROOT = _path_setting("REGODIT_PROJECT_ROOT", _default_project_root())
DATA_DIR = _path_setting("REGODIT_DATA_DIR", PROJECT_ROOT / "data")
ARTIFACT_DIR = _path_setting("REGODIT_ARTIFACT_DIR", PROJECT_ROOT / "artifacts")
EVIDENCE_PATH = _path_setting("REGODIT_EVIDENCE_PATH", ARTIFACT_DIR / "evidence.jsonl")
PROFILE_DB = _path_setting("REGODIT_PROFILE_DB", ARTIFACT_DIR / "security_profile.sqlite3")
CONVERSATION_DB = _path_setting("REGODIT_CONVERSATION_DB", ARTIFACT_DIR / "conversations.sqlite3")
HOST = os.environ.get("REGODIT_HOST", "127.0.0.1")
PORT = int(os.environ.get("REGODIT_PORT", "8501"))
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai").strip().casefold()
LLM_MODEL = active_model()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
PRISMTRACE_API_KEY = os.environ.get("PRISMTRACE_API_KEY", "").strip()
PRISMTRACE_PROJECT_ID = os.environ.get("PRISMTRACE_PROJECT_ID", "").strip()
PRISMTRACE_HOST = os.environ.get("PRISMTRACE_HOST", "").strip()
PRISMTRACE_AGENT_NAME = os.environ.get("PRISMTRACE_AGENT_NAME", "regodit-security-analyst").strip()
