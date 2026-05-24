"""Unauth Reddit thread fetcher via direct .json endpoints.

A drop-in alternative to :mod:`pipeline.fetch` that requires NO Reddit
OAuth credentials, NO PRAW, and NO Firecrawl. Used by the v1.3 mega-
backfill.

Each :class:`~pipeline.models.ThreadCandidate` is converted to a fully
hydrated :class:`~pipeline.models.Thread` by:

1. GET ``<permalink>.json?raw_json=1&limit=500&depth=10`` (unauth, no UA
   restrictions beyond Reddit's documented requirement to send one).
2. Parse the two-element ``[submission, comments]`` payload via the same
   :func:`pipeline.fetch._thread_from_reddit_json` Firecrawl-fallback
   path already uses — so the shape is byte-exact identical to the PRAW
   path.

The endpoint serves up to 500 comments. Very deep threads (extremely
rare for the JoVE corpus) lose the tail; that's an acknowledged
limitation, also present in :func:`pipeline.fetch._fetch_via_firecrawl`.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from pipeline.fetch import _thread_from_reddit_json
from pipeline.models import Thread, ThreadCandidate

logger = logging.getLogger(__name__)

# Reddit unauth rate ceiling — same as the discoverer.
_UNAUTH_REQUEST_SPACING_S = 1.5

_UA = "voa-v1.3-mega-backfill/0.1 by /u/in-quiz-ition (https://github.com/jove-publishing)"


def _json_url_from_permalink(permalink: str) -> str:
    """Convert a Reddit permalink to its ``.json`` equivalent."""

    stripped = permalink.rstrip("/")
    return f"{stripped}.json"


def fetch_thread_unauth(candidate: ThreadCandidate) -> Thread:
    """Fetch+hydrate a Thread via the unauth .json endpoint.

    Raises on HTTP error or unexpected JSON shape — the caller wraps in
    try/except so a single bad thread doesn't kill the backfill loop.
    """

    json_url = _json_url_from_permalink(candidate.permalink)
    headers = {"User-Agent": _UA}
    params: dict[str, Any] = {
        "raw_json": 1,
        "limit": 500,  # Reddit caps comments-per-response at 500.
        "depth": 10,  # Deep enough for the vast majority of JoVE threads.
    }

    resp = requests.get(json_url, headers=headers, params=params, timeout=60)
    time.sleep(_UNAUTH_REQUEST_SPACING_S)

    if resp.status_code == 429:
        # Polite back-off.
        logger.warning("Fetch: r/%s 429 on %s — sleeping 30s then retrying", candidate.subreddit, json_url)
        time.sleep(30.0)
        resp = requests.get(json_url, headers=headers, params=params, timeout=60)
        time.sleep(_UNAUTH_REQUEST_SPACING_S)

    resp.raise_for_status()
    payload = resp.json()

    if not isinstance(payload, list) or len(payload) < 2:
        raise RuntimeError(
            f"Unexpected Reddit .json shape for {json_url}: expected list of 2, "
            f"got {type(payload).__name__}"
        )

    return _thread_from_reddit_json(payload)
