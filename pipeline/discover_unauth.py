"""Unauth Reddit thread discovery via direct .json endpoints.

A drop-in alternative to :mod:`pipeline.discover` that requires NO Reddit
OAuth credentials and NO Firecrawl key. Used by the v1.3 mega-backfill
because:

* PRAW (used by :mod:`pipeline.discover`) requires either OAuth creds or
  uses an unauth path throttled to ~10 req/min — too slow for the 2010+
  full-corpus backfill.
* Firecrawl-on-Google (Ring 3 in the original spec) requires
  ``FIRECRAWL_API_KEY`` which is not present on this machine.

Three rings preserved with adapted mechanisms:

1. **Ring 1 — Subreddit enumeration via .json**: paginate
   ``https://www.reddit.com/r/<sub>/new.json?limit=100&after=...`` back to
   the time horizon. Filter on JoVE alias keyword match against
   title+selftext. Rate-limited at ~30 req/min unauth (Reddit's documented
   ceiling for unauth requests via the .json endpoint).
2. **Ring 2 — Reddit search via .json**: hit
   ``https://www.reddit.com/search.json?q=<query>&sort=<sort>&t=all&limit=100``
   for each JoVE alias × {relevance, new, top}. Restrict in-process to
   the seed-threads.json discovery universe (core ∪ broad).
3. **Ring 3 — DROPPED for v1.3** because Firecrawl is unavailable. To
   compensate, Ring 2 is run with extra query variants
   (legitimacy/predatory framings) to widen recall.

All candidates dedup by Reddit thread id and yielded as
:class:`ThreadCandidate` for parity with the PRAW-based discoverer — the
downstream pipeline (fetch/disambiguate/tag/store/aggregate) is shape-
identical.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from typing import Any

import requests

from pipeline.config import TIME_HORIZON, load_data_json
from pipeline.discover import (
    dedupe_candidates,
    extract_subreddit,
    extract_thread_id,
)
from pipeline.models import ThreadCandidate

logger = logging.getLogger(__name__)


# Reddit unauth rate ceiling — documented ~30 req/min. Stay well under to
# avoid 429s. With request spacing ~2s we get 30 req/min comfortably.
_UNAUTH_REQUEST_SPACING_S = 1.5

# User-Agent string used on every request. Reddit explicitly requires a
# unique UA per app — generic UAs get aggressively rate-limited.
_UA = "voa-v1.3-mega-backfill/0.1 by /u/in-quiz-ition (https://github.com/jove-publishing)"

# Maximum pages to walk for Ring 1 per subreddit. Subreddits with very long
# histories can have 100k+ posts; walking all of them would burn 30+ hours
# per sub. ~200 pages × 100 items = up to 20k posts per sub, which covers
# 5-10 years of activity on most academia subs.
_RING1_MAX_PAGES = 200

# Maximum pages to walk for Ring 2 per (alias × sort). Reddit's search
# API caps at ~1000 results regardless, so 10 pages × 100 is the ceiling.
_RING2_MAX_PAGES = 10


def _http_get_json(url: str, *, params: dict[str, Any] | None = None) -> Any:
    """GET a Reddit .json URL with the project user-agent.

    Returns parsed JSON. Raises on HTTP error. Sleeps the unauth spacing
    *before* returning so the next caller is naturally rate-limited.
    """

    headers = {"User-Agent": _UA}
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    time.sleep(_UNAUTH_REQUEST_SPACING_S)
    if resp.status_code == 429:
        # Too many requests — back off harder.
        time.sleep(30.0)
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        time.sleep(_UNAUTH_REQUEST_SPACING_S)
    resp.raise_for_status()
    return resp.json()


def _seed_subreddits() -> list[str]:
    seeds = load_data_json("seed-threads.json")
    anchors = seeds.get("discovery_anchors", {})
    core = anchors.get("subreddits_core", []) or []
    broad = anchors.get("subreddits_broad", []) or []
    seen: set[str] = set()
    out: list[str] = []
    for s in [*core, *broad]:
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _v1_journal_aliases() -> list[str]:
    journals = load_data_json("journals.json") if _journals_exists() else None
    if journals is None:
        # Fallback if data/journals.json is absent on this branch.
        return ["JoVE", "Journal of Visualized Experiments", "jove.com"]
    return list(journals.get("jove_aliases_to_match", []))


def _journals_exists() -> bool:
    from pipeline.config import DATA_DIR

    return (DATA_DIR / "journals.json").exists()


def _matches_aliases(text: str, aliases: Iterable[str]) -> bool:
    if not text:
        return False
    haystack = text.lower()
    return any(alias.lower() in haystack for alias in aliases)


def _in_scope_subreddits() -> set[str]:
    return {s.lower() for s in _seed_subreddits()}


# ---- Ring 1 -------------------------------------------------------------


def ring1_subreddit_enum_unauth(
    time_horizon: datetime = TIME_HORIZON,
) -> Iterator[ThreadCandidate]:
    """Enumerate subreddit new feeds via .json, filter by JoVE alias.

    Walks ``/r/<sub>/new.json?limit=100&after=...`` until either
    ``time_horizon`` is reached, ``_RING1_MAX_PAGES`` is hit, or pagination
    runs out. Subreddits that don't admit unauth access (private/quarantined)
    are skipped with a warning.
    """

    subs = _seed_subreddits()
    aliases = _v1_journal_aliases()
    if not aliases:
        logger.warning("Ring 1: no JoVE aliases configured — skipping")
        return

    horizon_ts = time_horizon.timestamp()

    for sub in subs:
        logger.info("Ring 1: enumerating r/%s (max %d pages)", sub, _RING1_MAX_PAGES)
        after: str | None = None
        page_count = 0
        hit_count = 0

        while page_count < _RING1_MAX_PAGES:
            page_count += 1
            url = f"https://www.reddit.com/r/{sub}/new.json"
            params: dict[str, Any] = {"limit": 100, "raw_json": 1}
            if after:
                params["after"] = after

            try:
                data = _http_get_json(url, params=params)
            except requests.HTTPError as exc:
                logger.warning(
                    "Ring 1: r/%s page %d HTTP error: %s — moving on",
                    sub,
                    page_count,
                    exc,
                )
                break
            except requests.RequestException as exc:
                logger.warning(
                    "Ring 1: r/%s page %d transport error: %s — moving on",
                    sub,
                    page_count,
                    exc,
                )
                break

            children = data.get("data", {}).get("children", [])
            if not children:
                break

            stop_paging = False
            for child in children:
                d = child.get("data", {})
                created = d.get("created_utc", 0)
                if created < horizon_ts:
                    stop_paging = True
                    break

                title = d.get("title", "")
                selftext = d.get("selftext", "")
                if not _matches_aliases(title + " " + selftext, aliases):
                    continue

                permalink = "https://www.reddit.com" + d.get("permalink", "")
                yield ThreadCandidate(
                    permalink=permalink,
                    subreddit=d.get("subreddit", sub),
                    discovered_via="ring1_subreddit_enum",
                    discovered_at=datetime.now(timezone.utc),
                    seed_query=None,
                )
                hit_count += 1

            after = data.get("data", {}).get("after")
            if not after or stop_paging:
                break

        logger.info(
            "Ring 1: r/%s done — %d page(s), %d JoVE hit(s)",
            sub,
            page_count,
            hit_count,
        )


# ---- Ring 2 -------------------------------------------------------------


# Extra query patterns added in v1.3 to compensate for the missing Firecrawl
# Ring 3. Reddit search is shallow but its sort=top + sort=relevance reach
# pretty far back, and varied query framings widen recall.
_RING2_EXTRA_QUERIES = [
    "JoVE",
    '"Journal of Visualized Experiments"',
    "jove.com",
    "myjove",
    "JoVE legit",
    "JoVE respectable",
    "JoVE predatory",
    "JoVE peer review",
    "JoVE special issue",
    "JoVE guest editor",
    "JoVE request",
    "JoVE invitation",
    "JoVE invite",
    "JoVE fees",
    "JoVE author",
    "JoVE access",
    "JoVE video",
    "JoVE protocol",
    "JoVE subscription",
    "JoVE impact factor",
    "JoVE PubMed",
    "JoVE MDPI",
    "JoVE Frontiers",
    "JoVE Nature",
    "JoVE reproducibility",
    "JoVE marketing",
    "JoVE librarian",
    "JoVE methods",
    "JoVE protocols",
    "JoVE journal",
    "JoVE publication",
    "JoVE manuscript",
    "JoVE editor",
    "JoVE produce video",
]


def ring2_reddit_search_unauth() -> Iterator[ThreadCandidate]:
    """Search reddit.com/search.json for each JoVE query × sort.

    Restricts hits to the seed-threads discovery universe in-process.
    Compared to Ring 2 in the original spec, this version includes
    many more query framings (legitimacy, predatory, peer review, fees,
    access) to compensate for the dropped Ring 3.
    """

    in_scope = _in_scope_subreddits()
    sorts = ("relevance", "new", "top")

    for query in _RING2_EXTRA_QUERIES:
        for sort in sorts:
            logger.info("Ring 2: search sort=%s query=%s", sort, query)
            after: str | None = None
            page_count = 0
            kept_count = 0

            while page_count < _RING2_MAX_PAGES:
                page_count += 1
                url = "https://www.reddit.com/search.json"
                params: dict[str, Any] = {
                    "q": query,
                    "sort": sort,
                    "t": "all",
                    "limit": 100,
                    "raw_json": 1,
                }
                if after:
                    params["after"] = after

                try:
                    data = _http_get_json(url, params=params)
                except requests.HTTPError as exc:
                    logger.warning(
                        "Ring 2: search %s sort=%s page %d HTTP error: %s",
                        query,
                        sort,
                        page_count,
                        exc,
                    )
                    break
                except requests.RequestException as exc:
                    logger.warning(
                        "Ring 2: search %s sort=%s page %d transport error: %s",
                        query,
                        sort,
                        page_count,
                        exc,
                    )
                    break

                children = data.get("data", {}).get("children", [])
                if not children:
                    break

                for child in children:
                    d = child.get("data", {})
                    sub = d.get("subreddit", "")
                    if not sub or sub.lower() not in in_scope:
                        continue
                    permalink = "https://www.reddit.com" + d.get("permalink", "")
                    yield ThreadCandidate(
                        permalink=permalink,
                        subreddit=sub,
                        discovered_via="ring2_reddit_search",
                        discovered_at=datetime.now(timezone.utc),
                        seed_query=query,
                    )
                    kept_count += 1

                after = data.get("data", {}).get("after")
                if not after:
                    break

            logger.info(
                "Ring 2: %s sort=%s done — %d page(s), %d in-scope hit(s)",
                query,
                sort,
                page_count,
                kept_count,
            )


# ---- Seed bootstrap (always include) -----------------------------------


def known_seed_candidates() -> Iterator[ThreadCandidate]:
    """Yield the 15 D-validated seed threads as Ring-0 candidates.

    Guarantees that even if Ring 1/2 miss something (or Reddit hides a
    thread from search), the seed set is always in the corpus.
    """

    seeds = load_data_json("seed-threads.json")
    for entry in seeds.get("known_threads", []):
        url = entry.get("url")
        sub = entry.get("subreddit")
        if not url or not sub:
            continue
        yield ThreadCandidate(
            permalink=url,
            subreddit=sub,
            discovered_via="ring1_subreddit_enum",  # closest existing enum value
            discovered_at=datetime.now(timezone.utc),
            seed_query="known_seed",
        )


# ---- Orchestrator -------------------------------------------------------


def discover_all_unauth(
    time_horizon: datetime = TIME_HORIZON,
) -> Iterator[ThreadCandidate]:
    """Run all unauth rings and yield deduplicated candidates.

    Order: seed bootstrap → Ring 1 (subreddit enum) → Ring 2 (search).
    Seeds come first so they always win the dedupe in case of conflicts.
    """

    def _all() -> Iterator[ThreadCandidate]:
        yield from known_seed_candidates()
        yield from ring1_subreddit_enum_unauth(time_horizon=time_horizon)
        yield from ring2_reddit_search_unauth()

    yield from dedupe_candidates(_all())
