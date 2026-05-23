"""Three-ring Reddit thread discovery for the Voice of Academia pipeline.

Per spec D8 (``Voice of Academia — System Design.md``) discovery runs three
concentric rings whose union is deduplicated by Reddit thread id:

1. **Ring 1 — Subreddit enumeration via PRAW.** For each subreddit in
   ``data/seed-threads.json`` (``subreddits_core`` ∪ ``subreddits_broad``),
   walk ``subreddit.new(limit=None)`` until ``TIME_HORIZON``. Filter on
   keyword match against ``data/journals.json``'s ``jove_aliases_to_match``.

2. **Ring 2 — Reddit search via PRAW.** For each v1 journal alias, search
   ``reddit.subreddit("all").search(...)`` across sort orders
   (``relevance``, ``new``, ``top``) and keep hits in the discovery subreddit
   universe (core ∪ broad).

3. **Ring 3 — Firecrawl on Google.** Issue ``site:reddit.com "<alias>"``
   queries through the Firecrawl ``search`` API. Google indexes Reddit
   further back than Reddit's own search, which is how we reach 2010–2017.

v1 scope: this module iterates ``data/journals.json``'s ``v1_active`` only
(``["JoVE"]``). ``v2_planned`` is ignored — the data model is journal-agnostic
for forward-compatibility, but v1 build is JoVE-only.

PRAW auth posture: if both ``REDDIT_CLIENT_ID`` and ``REDDIT_CLIENT_SECRET``
are set, authenticated mode is used. Otherwise PRAW is constructed with empty
strings, which puts it in read-only mode (the PRAW 7.8 supported path for
unauthenticated access — passing ``None`` is rejected by the constructor).
Read-only is rate-limited to ~10 req/min; mega-backfill is expected to take
25–40 hours under that ceiling, which is acceptable per the spec.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pipeline.config import (
    FIRECRAWL_API_KEY,
    REDDIT_CLIENT_ID,
    REDDIT_CLIENT_SECRET,
    REDDIT_USER_AGENT,
    TIME_HORIZON,
    load_data_json,
    reddit_auth_mode,
)
from pipeline.models import ThreadCandidate

if TYPE_CHECKING:  # pragma: no cover - typing only
    import praw

logger = logging.getLogger(__name__)


# Reddit permalinks resolve to ``/r/<sub>/comments/<id>/<slug>/`` regardless of
# host (old.reddit.com, www.reddit.com, np.reddit.com, no subdomain at all).
# Capture group 1 = subreddit, group 2 = thread id. Lowercase per Reddit's
# canonical form.
_REDDIT_THREAD_RE = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)?reddit\.com/r/([^/]+)/comments/([a-z0-9]+)(?:/|$)",
    re.IGNORECASE,
)


def extract_thread_id(url: str) -> str | None:
    """Return the lowercased Reddit thread id from any permalink shape.

    Accepts URLs with or without the ``www.``/``old.``/``np.`` host, with or
    without a trailing slug, with or without a query string. Returns ``None``
    when the URL is not a Reddit thread permalink (e.g. a subreddit root, a
    user page, or a non-reddit URL). The lowercase normalisation matters
    because Ring 3 (Firecrawl) sometimes returns mixed-case ids.
    """

    if not url:
        return None
    m = _REDDIT_THREAD_RE.match(url.strip())
    if not m:
        return None
    return m.group(2).lower()


def extract_subreddit(url: str) -> str | None:
    """Return the subreddit name from a Reddit permalink, or ``None``."""

    if not url:
        return None
    m = _REDDIT_THREAD_RE.match(url.strip())
    if not m:
        return None
    return m.group(1)


def _build_reddit_client() -> praw.Reddit:
    """Construct a PRAW Reddit client honoring v1's unauth-as-default posture.

    PRAW 7.8 rejects ``client_id=None`` — the supported path for read-only
    access is to pass *empty strings*. Authenticated mode is used only when
    both env vars are present.
    """

    import praw  # local import keeps module import-time light for tests

    mode = reddit_auth_mode()
    if mode == "authenticated":
        logger.info("PRAW: authenticated mode (~100 req/min)")
        return praw.Reddit(
            client_id=REDDIT_CLIENT_ID,
            client_secret=REDDIT_CLIENT_SECRET,
            user_agent=REDDIT_USER_AGENT,
            check_for_async=False,
        )

    logger.warning(
        "PRAW: read-only/unauth mode (~10 req/min) — set REDDIT_CLIENT_ID + "
        "REDDIT_CLIENT_SECRET to switch to authenticated mode."
    )
    return praw.Reddit(
        client_id="",
        client_secret="",
        user_agent=REDDIT_USER_AGENT,
        check_for_async=False,
    )


def _matches_journal_aliases(text: str, aliases: Iterable[str]) -> bool:
    """Case-insensitive substring match for any of the journal aliases.

    Cheap and intentionally permissive — false positives are caught later by
    Phase 4 disambiguation. False negatives here cannot be recovered, so we
    err on the side of letting through.
    """

    if not text:
        return False
    haystack = text.lower()
    return any(alias.lower() in haystack for alias in aliases)


def _seed_subreddits() -> list[str]:
    """Return the union of ``subreddits_core`` and ``subreddits_broad`` from seed data."""

    seeds = load_data_json("seed-threads.json")
    anchors = seeds.get("discovery_anchors", {})
    core = anchors.get("subreddits_core", []) or []
    broad = anchors.get("subreddits_broad", []) or []
    # Preserve order, dedupe case-insensitively
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
    """Return the aliases to match for v1 (JoVE only)."""

    journals = load_data_json("journals.json")
    if "JoVE" not in journals.get("v1_active", []):
        # Defensive: v1_active is locked to ["JoVE"] but this guards against
        # someone editing the file. We do not silently expand to v2_planned.
        logger.warning("journals.json v1_active does not contain JoVE — discovery will return no Ring 1/2 hits")
        return []
    return list(journals.get("jove_aliases_to_match", []))


# ---- Ring 1 -------------------------------------------------------------


def ring1_subreddit_enum(
    reddit: praw.Reddit,
    time_horizon: datetime = TIME_HORIZON,
) -> Iterator[ThreadCandidate]:
    """Yield candidates from new-feed enumeration of each seed subreddit.

    Walks ``subreddit.new()`` (Reddit's reverse-chronological feed) until a
    submission older than ``time_horizon`` is reached. Filters by journal
    alias keyword match against the submission's title and selftext.
    """

    subreddits = _seed_subreddits()
    aliases = _v1_journal_aliases()
    if not aliases:
        return

    horizon_ts = time_horizon.timestamp()

    for sub_name in subreddits:
        logger.info("Ring 1: enumerating r/%s", sub_name)
        sub = reddit.subreddit(sub_name)
        try:
            for submission in sub.new(limit=None):
                if submission.created_utc < horizon_ts:
                    # ``.new()`` is reverse-chronological → first old hit ends the loop.
                    break
                text = " ".join(
                    [
                        submission.title or "",
                        getattr(submission, "selftext", "") or "",
                    ]
                )
                if not _matches_journal_aliases(text, aliases):
                    continue
                yield ThreadCandidate(
                    permalink=f"https://www.reddit.com{submission.permalink}",
                    subreddit=submission.subreddit.display_name,
                    discovered_via="ring1_subreddit_enum",
                    discovered_at=datetime.now(timezone.utc),
                    seed_query=None,
                )
        except Exception:  # pragma: no cover - PRAW raises a handful of network errors
            logger.exception("Ring 1: error enumerating r/%s — continuing", sub_name)
            continue


# ---- Ring 2 -------------------------------------------------------------


def _in_scope_subreddits() -> set[str]:
    """Lowercased set of subreddit names in the discovery universe (Ring 2 filter)."""

    return {s.lower() for s in _seed_subreddits()}


def ring2_reddit_search(reddit: praw.Reddit) -> Iterator[ThreadCandidate]:
    """Yield candidates from PRAW search across ``r/all`` for each JoVE alias.

    Iterates the cross product of ``v1_active`` aliases × sort orders, keeping
    only hits whose subreddit is in the discovery universe (core ∪ broad).
    Reddit's search is shallow (~100 results per sort) but covers what its
    new-feed enumeration in Ring 1 might miss for cross-subreddit recency.
    """

    aliases = _v1_journal_aliases()
    if not aliases:
        return

    in_scope = _in_scope_subreddits()
    sorts = ("relevance", "new", "top")

    for alias in aliases:
        for sort in sorts:
            query = f'"{alias}"'
            logger.info("Ring 2: searching r/all sort=%s for %s", sort, query)
            try:
                results = reddit.subreddit("all").search(
                    query,
                    sort=sort,
                    time_filter="all",
                    limit=None,
                )
                for submission in results:
                    sub_lower = submission.subreddit.display_name.lower()
                    if sub_lower not in in_scope:
                        continue
                    yield ThreadCandidate(
                        permalink=f"https://www.reddit.com{submission.permalink}",
                        subreddit=submission.subreddit.display_name,
                        discovered_via="ring2_reddit_search",
                        discovered_at=datetime.now(timezone.utc),
                        seed_query=query,
                    )
            except Exception:  # pragma: no cover - network/API exception path
                logger.exception("Ring 2: error on sort=%s alias=%s — continuing", sort, alias)
                continue


# ---- Ring 3 -------------------------------------------------------------


def _ring3_query_set(aliases: Iterable[str]) -> list[str]:
    """Build Google query strings for each JoVE alias.

    Returns a list of ``site:reddit.com "<alias>"`` queries (no theme
    keywords). Per spec D8 the goal is breadth — pulling Reddit's *long tail*
    of mentions from before Reddit's own search index reaches. Theme-keyed
    queries can be added in a follow-up iteration if recall is short.
    """

    return [f'site:reddit.com "{alias}"' for alias in aliases]


def ring3_firecrawl_google(
    fc_client: Any | None = None,
    queries: Iterable[str] | None = None,
    limit_per_query: int = 100,
) -> Iterator[ThreadCandidate]:
    """Yield candidates from Firecrawl-on-Google searches.

    ``fc_client`` is optional — if ``None``, the function constructs a
    ``firecrawl.Firecrawl`` client using ``FIRECRAWL_API_KEY``. Passing a
    client (or a stub with a ``.search`` method) makes the function trivially
    mockable.

    Skips silently when ``FIRECRAWL_API_KEY`` is unset — the warning surfaces
    once at start. Discovery still produces Rings 1+2 in that case.
    """

    aliases = _v1_journal_aliases()
    if not aliases:
        return

    if fc_client is None:
        if not FIRECRAWL_API_KEY:
            logger.warning(
                "Ring 3: FIRECRAWL_API_KEY unset — skipping Firecrawl-on-Google discovery."
            )
            return
        from firecrawl import Firecrawl

        fc_client = Firecrawl(api_key=FIRECRAWL_API_KEY)

    qset = list(queries) if queries is not None else _ring3_query_set(aliases)

    for q in qset:
        logger.info("Ring 3: Firecrawl search: %s", q)
        try:
            results = fc_client.search(query=q, limit=limit_per_query)
        except Exception:  # pragma: no cover - network/API exception path
            logger.exception("Ring 3: Firecrawl search failed for query=%s — continuing", q)
            continue

        # Firecrawl v2 SearchData exposes .web/.news/.images; .web holds the
        # SERP results we want. Each item has .url and .title.
        web_hits = getattr(results, "web", None) or []
        for hit in web_hits:
            url = getattr(hit, "url", None)
            if not url:
                continue
            thread_id = extract_thread_id(url)
            if thread_id is None:
                continue
            subreddit = extract_subreddit(url) or "unknown"
            yield ThreadCandidate(
                permalink=url,
                subreddit=subreddit,
                discovered_via="ring3_firecrawl_google",
                discovered_at=datetime.now(timezone.utc),
                seed_query=q,
            )


# ---- Orchestrator -------------------------------------------------------


def dedupe_candidates(
    candidates: Iterable[ThreadCandidate],
) -> Iterator[ThreadCandidate]:
    """Yield candidates with first-seen-wins dedupe by Reddit thread id.

    Many rings will surface the same thread — that's expected and fine. The
    *first* candidate yielded for an id is kept (preserving its provenance);
    later duplicates are dropped. Candidates with a permalink we cannot parse
    as a Reddit thread URL are filtered out entirely.
    """

    seen: set[str] = set()
    for cand in candidates:
        tid = extract_thread_id(cand.permalink)
        if tid is None:
            logger.debug("Dedupe: dropping non-thread permalink %s", cand.permalink)
            continue
        if tid in seen:
            continue
        seen.add(tid)
        yield cand


def discover_all(
    time_horizon: datetime = TIME_HORIZON,
    reddit: praw.Reddit | None = None,
    fc_client: Any | None = None,
) -> Iterator[ThreadCandidate]:
    """Run all three rings and yield deduplicated candidates.

    Generator-shaped so callers (mega backfill, weekly delta) can checkpoint
    every N candidates without holding the full set in memory. Ring order is
    Ring 1 → Ring 2 → Ring 3, which means first-seen-wins prefers the cheaper
    Ring 1 provenance for threads that show up in multiple rings. That's the
    right default for debugging — Ring 1 hits are usually the ones we'd
    re-discover on a fresh weekly run.
    """

    if reddit is None:
        reddit = _build_reddit_client()

    def _all() -> Iterator[ThreadCandidate]:
        yield from ring1_subreddit_enum(reddit, time_horizon=time_horizon)
        yield from ring2_reddit_search(reddit)
        yield from ring3_firecrawl_google(fc_client=fc_client)

    yield from dedupe_candidates(_all())
