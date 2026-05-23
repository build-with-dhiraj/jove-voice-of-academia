"""Pipeline configuration — env loading + project-wide constants.

v1 scope: JoVE only (see ``data/journals.json`` ``v1_active``).

Reddit auth posture (v1): both ``REDDIT_CLIENT_ID`` and ``REDDIT_CLIENT_SECRET``
may be unset. If either is missing the pipeline runs in PRAW read-only mode by
passing empty strings to ``praw.Reddit`` (the underlying ``client_id=None``
pattern documented in older PRAW guides is rejected by PRAW 7.8.x —
empty strings are the supported path for read-only access).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Repo root (this file lives at ``<repo>/pipeline/config.py``)
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
DATA_DIR: Path = REPO_ROOT / "data"

# Load .env at import time so any caller reading these constants gets values.
# load_dotenv is a no-op when .env is absent (e.g. CI), which is the intended
# behavior for unauth/local runs.
load_dotenv(REPO_ROOT / ".env")

logger = logging.getLogger(__name__)


# ---- Reddit (optional in v1 — unauth mode works) -------------------------
REDDIT_CLIENT_ID: str | None = os.environ.get("REDDIT_CLIENT_ID") or None
REDDIT_CLIENT_SECRET: str | None = os.environ.get("REDDIT_CLIENT_SECRET") or None
REDDIT_USER_AGENT: str = os.environ.get(
    "REDDIT_USER_AGENT", "voa-pipeline/0.1 by /u/in-quiz-ition"
)

# ---- Service API keys (declared now; consumed in later phases) -----------
ANTHROPIC_API_KEY: str | None = os.environ.get("ANTHROPIC_API_KEY") or None
PINECONE_API_KEY: str | None = os.environ.get("PINECONE_API_KEY") or None
FIRECRAWL_API_KEY: str | None = os.environ.get("FIRECRAWL_API_KEY") or None

# ---- Pinecone constants (target index + namespace) -----------------------
PINECONE_INDEX: str = os.environ.get("PINECONE_INDEX", "jove-memory")
PINECONE_NAMESPACE: str = os.environ.get("PINECONE_NAMESPACE", "social-listening")

# ---- Vault paths ---------------------------------------------------------
VAULT_ROOT: Path = Path(os.environ.get("VAULT_ROOT", "/Users/pw/Obsidian/JoVE"))
VAULT_VOICE_CANDIDATES: Path = VAULT_ROOT / os.environ.get(
    "VAULT_VOICE_CANDIDATES",
    "30 — Customers & Market/Voice of Academia/Candidates",
)
VAULT_VOICE_PUBLISHED: Path = VAULT_ROOT / os.environ.get(
    "VAULT_VOICE_PUBLISHED",
    "30 — Customers & Market/Voice of Academia/Published",
)
VAULT_PULSE: Path = VAULT_ROOT / os.environ.get(
    "VAULT_PULSE",
    "30 — Customers & Market/Voice of Academia/Pulse",
)

# ---- Pipeline policy constants ------------------------------------------
# Spec D7: only threads/comments from 2010-01-01 onward enter the dashboard.
# (Pinecone stores everything — this cutoff is for downstream aggregation.)
TIME_HORIZON: datetime = datetime(2010, 1, 1, tzinfo=timezone.utc)

# Conservative under PRAW's documented ~10 req/min read-only ceiling.
RATE_LIMIT_REQ_PER_MIN: int = 6


def reddit_auth_mode() -> str:
    """Return ``"authenticated"`` if both Reddit creds are set, else ``"unauth"``.

    Used by :mod:`pipeline.discover` to pick the right PRAW constructor and
    emit a log line so we don't silently fall back to the slower mode.
    """

    if REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET:
        return "authenticated"
    return "unauth"


def load_data_json(filename: str) -> dict[str, Any]:
    """Load one of the JSON config artifacts under ``<repo>/data/``.

    Filename is a basename (``"taxonomy.json"``), not an absolute path. Raises
    ``FileNotFoundError`` if the artifact doesn't exist — by design, since all
    canonical config artifacts ship with the repo.
    """

    path = DATA_DIR / filename
    if not path.exists():
        raise FileNotFoundError(
            f"Expected data artifact at {path}. "
            "Check that data/ files are committed and the filename is correct."
        )
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)
