"""Phase 7 — Aggregate layer: build the dashboard's frequency table.

This module is the *last* build phase before mega-backfill orchestration
(Phase 8). It takes the corpus of :class:`pipeline.models.TaggedComment`
objects plus their parent :class:`pipeline.models.Thread` objects and
produces an :class:`pipeline.models.Aggregate` ready for serialization
to ``dashboard/data/latest.json``.

What it does:

1. **Group** tagged comments by ``(sentiment, theme, journal=JoVE)``.
   Per spec D1, a single comment with multiple
   :class:`pipeline.models.ThemeAssignment` entries fans out: one bucket
   entry per ``(theme, sentiment)`` pair.

2. **Derive** per-bucket stats from the supporting comments — distinct
   threads, distinct comments, summed upvotes, summed direct-reply
   counts, most-recent comment timestamp, most-recent thread timestamp,
   and the deduplicated sorted list of supporting thread permalinks.

3. **Rank** within ``(sentiment, journal)`` per spec D4 — breadth-first
   on ``thread_count``, tiebreaker on ``comment_count``, then per spec
   D5 a final tiebreaker on the most-recent-comment timestamp. v1
   ``journal`` is always ``"JoVE"`` so the sentiment grouping is the
   primary dimension.

4. **Filter** to the 2010+ time horizon per spec D7 — a thread whose
   ``created_utc`` is pre-2010 is excluded; a row whose supporting
   threads are *all* pre-2010 disappears. Mixed-era rows (some pre, some
   post) keep only the post-2010 supporters.

5. **Load** the curated voice panel from the vault's
   ``Voice of Academia/Published/`` folder. The folder may be empty in
   v1 (D hasn't done a curation pass yet) — that's handled gracefully
   by returning an empty list.

Decision: ``VoicePanelRow.sentiment`` is 4-value (``Sentiment`` =
``positive|neutral|negative|mixed``) rather than 3-value
(``ThemeSentiment``). Voice quotes preserve the source comment's overall
polarity faithfully — a comment like *"the videos are great but the
fees are awful"* genuinely IS mixed, and the Voice panel exists exactly
to surface that nuance. Collapsing to a 3-value polarity would damage
the panel's credibility (spec D6 asymmetric trust — one tepid voice
damages the whole panel). The TypeScript ``Sentiment`` type in
``dashboard/lib/types.ts`` was widened in lockstep — see the module
comment there for the cross-reference.

The frequency-table sentiment stays 3-value (``ThemeSentiment``)
because per spec D1 a "mixed" overall sentiment is decomposed into one
row per ``(theme, polarity)``. So a comment whose overall sentiment is
``mixed`` contributes two rows — one positive (for the praise) and one
negative (for the criticism) — never a single mixed row.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from pipeline.config import TIME_HORIZON, VAULT_VOICE_PUBLISHED
from pipeline.models import (
    Aggregate,
    Comment,
    FrequencyRow,
    ProductAttribution,
    Sentiment,
    TaggedComment,
    ThemeSentiment,
    Thread,
    VoicePanelRow,
)

logger = logging.getLogger(__name__)


# ---- Public constants ----------------------------------------------------

#: Default journal label for v1. All rows in the v1 dashboard carry this
#: value; the field is reserved for v2 multi-journal expansion. Stored as
#: a module constant rather than hardcoded inline so v2 can swap to
#: per-comment ``tagged.journal_named`` resolution without touching call
#: sites.
DEFAULT_JOURNAL: str = "JoVE"

#: ISO time-horizon string for the dashboard. Mirrors the
#: :data:`pipeline.config.TIME_HORIZON` ``datetime`` but in the string
#: shape the dashboard expects.
DEFAULT_TIME_HORIZON_STR: str = "2010-01-01"


# =========================================================================
# Helper functions — lookups over the threads dict
# =========================================================================


def comment_thread_id(tc: TaggedComment) -> str | None:
    """Return the PRAW fullname (``t3_xxx``) of the parent thread.

    Phase 6 added :attr:`TaggedComment.thread_id`, so this is now an O(1)
    attribute read. We expose it as a function for future flexibility:
    if a stale ``TaggedComment`` (predating the Phase 6 field) is
    encountered, we can fall back to permalink regex parsing without
    changing call sites.
    """

    if tc.thread_id:
        return tc.thread_id
    # Backwards-compat fallback: parse from the comment permalink.
    # Reddit comment URLs are shaped ``.../comments/{thread_id}/{slug}/{comment_id}/``;
    # the ``thread_id`` is the alphanumeric segment immediately after
    # ``/comments/``. PRAW fullnames are ``t3_<thread_id>`` so we prefix.
    if tc.permalink:
        match = re.search(r"/comments/([a-z0-9]+)/", tc.permalink)
        if match:
            return f"t3_{match.group(1)}"
    return None


def _find_comment_in_thread(
    comment_id: str, thread: Thread
) -> Comment | None:
    """Return the :class:`Comment` with ``id == comment_id`` in ``thread``.

    Linear scan — fine for typical JoVE thread sizes (<50 comments most
    threads). Callers that aggregate over many comments should batch the
    lookups into a single dict pass per thread; see :func:`build_aggregate`.
    """

    for c in thread.comments:
        if c.id == comment_id:
            return c
    return None


def _count_direct_replies(comment_id: str, comments: list[Comment]) -> int:
    """Count comments whose ``parent_id`` equals ``comment_id``.

    Mirror of the helper in :mod:`pipeline.store` — duplicated here so
    Phase 7 doesn't reach into Phase 6's private name. The function is
    pure and trivial; the cost of duplication is one extra small
    function, which is preferable to the cost of cross-module coupling.
    """

    return sum(1 for c in comments if c.parent_id == comment_id)


# =========================================================================
# Bucketization: group tagged comments by (sentiment, theme, journal)
# =========================================================================


def _bucket_key(
    tc: TaggedComment, theme: str, sentiment: ThemeSentiment
) -> tuple[ThemeSentiment, str, str]:
    """Build the ``(sentiment, theme, journal)`` bucket key.

    v1: ``journal`` is always ``DEFAULT_JOURNAL`` (``"JoVE"``) regardless
    of ``tagged.journal_named``, because the v1 scope is JoVE-only.
    Comments with ``product_attribution == "out-of-scope"`` are filtered
    out *before* bucketing.
    """

    _ = tc  # journal is a constant in v1; tc reserved for v2.
    return (sentiment, theme, DEFAULT_JOURNAL)


def _is_in_scope(tc: TaggedComment) -> bool:
    """A tagged comment contributes to the aggregate iff JoVE-scope.

    Per spec, v1 admits only ``product_attribution == "journal"`` (the
    JoVE product line) plus any comment that names ``"JoVE"`` explicitly.
    A comment tagged ``"out-of-scope"`` was disambiguated as not about
    JoVE at all and must NOT contribute to the frequency table.
    """

    if tc.product_attribution == "out-of-scope":
        return False
    return True


def _thread_in_horizon(thread: Thread, horizon: datetime) -> bool:
    """Return True iff the thread's ``created_utc`` is at-or-after horizon.

    Used both at bucketization time (to filter out pre-2010 supporting
    threads) and at row-emission time (to skip buckets whose all
    supporters are out of horizon).
    """

    return thread.created_utc >= horizon


# =========================================================================
# Bucket -> FrequencyRow projection
# =========================================================================


def _build_row(
    *,
    sentiment: ThemeSentiment,
    theme: str,
    journal: str,
    supporting_tcs: list[TaggedComment],
    threads: dict[str, Thread],
    horizon: datetime,
) -> FrequencyRow | None:
    """Compute the per-bucket stats and emit one :class:`FrequencyRow`.

    Returns ``None`` if every supporting thread is pre-horizon, per spec
    D7. (A bucket can have *some* pre-2010 supporters and still produce
    a row — we just drop the pre-2010 supporters from the stats.)

    The ``rank`` field is filled with a placeholder (``1``); the final
    ranking is computed at the aggregate level once all rows in a
    ``(sentiment, journal)`` bucket are known.

    Implementation:

    * Pre-build a single ``{comment_id: (Comment, Thread)}`` lookup per
      supporting thread to avoid O(N²) scans on long comment lists.
    * Drop a supporting comment whose ``thread_id`` resolves to a
      thread we don't have in the ``threads`` dict (shouldn't happen
      in production — Phase 8 mega-backfill stores threads-then-tags
      atomically — but skip gracefully so the aggregate is robust to
      partial-write states).
    """

    # Resolve each supporting comment to a (Comment, Thread) pair.
    # Drop comments whose parent thread isn't in ``threads`` (orphans)
    # or is pre-horizon.
    resolved: list[tuple[TaggedComment, Comment, Thread]] = []
    for tc in supporting_tcs:
        tid = comment_thread_id(tc)
        if tid is None:
            logger.debug(
                "Skipping tagged comment %s — could not resolve thread_id.",
                tc.comment_id,
            )
            continue
        thread = threads.get(tid)
        if thread is None:
            logger.debug(
                "Skipping tagged comment %s — parent thread %s not in corpus.",
                tc.comment_id,
                tid,
            )
            continue
        if not _thread_in_horizon(thread, horizon):
            # Spec D7: pre-horizon supporters are dropped at the row level.
            continue
        comment = _find_comment_in_thread(tc.comment_id, thread)
        if comment is None:
            logger.debug(
                "Skipping tagged comment %s — comment id not found in thread %s.",
                tc.comment_id,
                tid,
            )
            continue
        resolved.append((tc, comment, thread))

    if not resolved:
        # Every supporter was filtered out. Spec D7: the bucket vanishes
        # from the dashboard — pre-2010 threads must not anchor today's
        # sentiment.
        return None

    # Distinct threads and comments
    thread_by_id: dict[str, Thread] = {}
    comments_by_id: dict[str, Comment] = {}
    for _, comment, thread in resolved:
        thread_by_id[thread.id] = thread
        comments_by_id[comment.id] = comment

    thread_count = len(thread_by_id)
    comment_count = len(comments_by_id)

    # Aggregated stats
    upvote_sum = sum(c.score for c in comments_by_id.values())

    # Direct-reply counts per supporting comment. To avoid O(N²) across
    # a thread's comments, build a per-thread parent-counts index once.
    thread_reply_index: dict[str, dict[str, int]] = {}
    for t in thread_by_id.values():
        index: dict[str, int] = defaultdict(int)
        for c in t.comments:
            index[c.parent_id] += 1
        thread_reply_index[t.id] = dict(index)

    reply_count = 0
    for c in comments_by_id.values():
        # We need to know which thread this comment lives in. Look it
        # up via the resolved list — cheaper than another comment scan.
        for tc_iter, c_iter, t_iter in resolved:
            if c_iter.id == c.id:
                reply_count += thread_reply_index[t_iter.id].get(c.id, 0)
                break

    last_comment_at = max(c.created_utc for c in comments_by_id.values())
    newest_thread_at = max(t.created_utc for t in thread_by_id.values())

    thread_urls = sorted({t.permalink for t in thread_by_id.values()})

    return FrequencyRow(
        sentiment=sentiment,
        theme=theme,
        journal=journal,
        thread_count=thread_count,
        comment_count=comment_count,
        upvote_sum=upvote_sum,
        reply_count=reply_count,
        last_comment_at=last_comment_at,
        newest_thread_at=newest_thread_at,
        thread_urls=thread_urls,
        rank=1,  # placeholder; final rank set in _rank_rows
    )


# =========================================================================
# Ranking within (sentiment, journal)
# =========================================================================


def _rank_rows(rows: list[FrequencyRow]) -> list[FrequencyRow]:
    """Rank rows breadth-first per spec D4 within ``(sentiment, journal)``.

    Sort key: ``(-thread_count, -comment_count, -last_comment_at)``.
    The negative signs flip the natural ascending sort to descending —
    higher thread count gets a lower (=better) rank position.

    Spec D5 recency tiebreaker: same thread + comment count → use the
    most-recent-comment timestamp (later wins). All three components
    must be in the sort key so a single tie doesn't bubble up to
    rank-by-insertion-order.

    Returns the *same* rows list with ``.rank`` mutated in place, sorted
    to a deterministic output order — ``(sentiment, journal, rank)``.
    The dashboard groups by sentiment for display so this order is
    convenient for ``data.ts`` consumers.
    """

    # Group by (sentiment, journal) without using itertools.groupby,
    # because groupby requires pre-sorted input and the in-place mutation
    # makes that awkward. A defaultdict is cleaner.
    buckets: dict[tuple[ThemeSentiment, str], list[FrequencyRow]] = defaultdict(list)
    for row in rows:
        buckets[(row.sentiment, row.journal)].append(row)

    ranked: list[FrequencyRow] = []
    # Iterate buckets in a deterministic order so the output list order
    # is stable across runs (helps git-diff readability for the weekly
    # commit of latest.json).
    for key in sorted(buckets.keys()):
        bucket_rows = buckets[key]
        bucket_rows.sort(
            key=lambda r: (
                -r.thread_count,
                -r.comment_count,
                -r.last_comment_at.timestamp(),
            )
        )
        for i, r in enumerate(bucket_rows, start=1):
            r.rank = i
        ranked.extend(bucket_rows)

    return ranked


# =========================================================================
# Voice panel parsing (from vault Published/ markdown)
# =========================================================================


# Format of one block in a Published/YYYY-WWW.md file mirrors the
# Candidates/ block emitted by :mod:`pipeline.store`. After D's curation
# (Phase 11), an approved block is copied verbatim from Candidates/ to
# Published/ — so the parser regex set here intentionally matches the
# emitted shape there.
#
# Each block starts with ``## {journal} · {SENTIMENT} · {themes_pretty}``
# and ends at the next ``## `` heading (or EOF). We use regex anchors
# per field rather than a strict template because curators may hand-edit
# (e.g. add a ``**Curator note**: ...`` line) and the parser should be
# tolerant. We use heading-based splitting (rather than ``---``
# separator splitting) because the file's title header (``# Voice
# Published``) lives in the same chunk as the first card, and trailing
# ``---`` separators are sometimes absent on the last block. Heading
# detection is uniform across both conditions.

# Matches the start of a Voice-block heading. Used both to identify
# where a block begins and to parse the heading fields once isolated.
_BLOCK_START_RE = re.compile(r"^##\s+\S+\s+·\s+[A-Z]+\s+·\s+.+$", re.MULTILINE)
_HEADING_RE = re.compile(
    r"^##\s+(?P<journal>[^·\n]+?)\s+·\s+(?P<sentiment>[A-Z]+)\s+·\s+(?P<themes>.+)$",
    re.MULTILINE,
)
_AUTHOR_RE = re.compile(r"\*\*Author\*\*\s*:\s*u/(?P<author>[^\s\n]+)", re.IGNORECASE)
_COMMENT_LINK_RE = re.compile(
    r"\*\*Comment\*\*\s*:\s*\[[^\]]+\]\((?P<url>[^)]+)\)", re.IGNORECASE
)
_DATE_RE = re.compile(r"\*\*Date\*\*\s*:\s*(?P<date>\d{4}-\d{2}-\d{2})", re.IGNORECASE)
_SCORE_RE = re.compile(r"\*\*Score\*\*\s*:\s*(?P<score>-?\d+)", re.IGNORECASE)
_RUBRIC_RE = re.compile(
    r"\*\*Voice rubric\*\*\s*:\s*(?P<reason>.+?)(?:\n\n|\n>)", re.DOTALL | re.IGNORECASE
)
_CURATOR_NOTE_RE = re.compile(
    r"\*\*Curator note\*\*\s*:\s*(?P<note>.+?)(?:\n\n|\Z)",
    re.DOTALL | re.IGNORECASE,
)
_BODY_BLOCKQUOTE_RE = re.compile(
    r"(?:^|\n)((?:>[^\n]*\n?)+)", re.MULTILINE
)


def _parse_voice_block(block: str, *, curated_at: datetime) -> VoicePanelRow | None:
    """Parse one Markdown block into a :class:`VoicePanelRow`.

    Returns ``None`` if the block is missing required fields (e.g. an
    incomplete hand-edit). The caller logs the skip.

    Required fields: heading (gives sentiment + theme + journal),
    Author, Comment link, Date, Score, body blockquote. Optional:
    Curator note (only present after D adds context).
    """

    head_match = _HEADING_RE.search(block)
    if not head_match:
        return None

    sentiment_raw = head_match.group("sentiment").strip().lower()
    themes_raw = head_match.group("themes").strip()

    sentiment: Sentiment
    if sentiment_raw == "positive":
        sentiment = "positive"
    elif sentiment_raw == "neutral":
        sentiment = "neutral"
    elif sentiment_raw == "negative":
        sentiment = "negative"
    elif sentiment_raw == "mixed":
        sentiment = "mixed"
    else:
        # ``UNKNOWN`` sentinel from Pinecone fallback; D would not have
        # promoted this to Published. Skip defensively.
        logger.warning("Voice block has unparseable sentiment %r; skipping.", sentiment_raw)
        return None

    # Themes are emitted as "theme:sentiment, theme:sentiment, ..." or
    # the literal "other". Take the first theme id; ``None`` for "other".
    theme: str | None
    first_token = themes_raw.split(",")[0].strip()
    if first_token == "other" or not first_token:
        theme = None
    else:
        # Strip ":sentiment" suffix if present.
        theme = first_token.split(":")[0].strip() or None

    author_m = _AUTHOR_RE.search(block)
    link_m = _COMMENT_LINK_RE.search(block)
    date_m = _DATE_RE.search(block)
    score_m = _SCORE_RE.search(block)
    rubric_m = _RUBRIC_RE.search(block)
    curator_m = _CURATOR_NOTE_RE.search(block)
    body_m = _BODY_BLOCKQUOTE_RE.search(block)

    if not (author_m and link_m and date_m and score_m and body_m):
        logger.warning(
            "Voice block missing one of required fields (author/link/date/score/body); skipping."
        )
        return None

    try:
        created_utc = datetime.strptime(date_m.group("date"), "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        logger.warning("Voice block has unparseable date %r; skipping.", date_m.group("date"))
        return None

    try:
        score = int(score_m.group("score"))
    except ValueError:
        logger.warning("Voice block has unparseable score %r; skipping.", score_m.group("score"))
        return None

    # Body: strip the ``> `` prefix from each line, rejoin.
    body_lines = body_m.group(1).splitlines()
    cleaned_body_lines = []
    for line in body_lines:
        if line.startswith("> "):
            cleaned_body_lines.append(line[2:])
        elif line.startswith(">"):
            cleaned_body_lines.append(line[1:])
        else:
            cleaned_body_lines.append(line)
    body = "\n".join(cleaned_body_lines).strip()

    # Curator note: optional. ``rubric`` (the LLM reason) is NOT shown on
    # the Voice panel — that's a candidate-time audit trail, not curator
    # context. We keep `curator_note` separate.
    curator_note: str | None = None
    if curator_m:
        curator_note = curator_m.group("note").strip()

    # Product attribution: derived from the heading's journal field.
    # Heading "JoVE" -> "journal"; anything else falls to "out-of-scope"
    # for v1 (per spec D7 scope clamp). Reserved for v2 multi-product.
    journal_raw = head_match.group("journal").strip()
    product_attribution: ProductAttribution
    if journal_raw == DEFAULT_JOURNAL:
        product_attribution = "journal"
    else:
        product_attribution = "out-of-scope"

    # ``rubric`` regex is matched only to consume the field; the LLM's
    # voice-candidacy reason is not surfaced on the panel.
    _ = rubric_m

    # author group strips trailing punctuation like a stray comma.
    author = author_m.group("author").strip().rstrip(",")

    return VoicePanelRow(
        sentiment=sentiment,
        theme=theme,
        product_attribution=product_attribution,
        author=author,
        permalink=link_m.group("url").strip(),
        body=body,
        score=score,
        created_utc=created_utc,
        curated_at=curated_at,
        curator_note=curator_note,
    )


def parse_voice_published_file(path: Path) -> list[VoicePanelRow]:
    """Parse one ``Published/YYYY-WWW.md`` file into voice panel rows.

    The file's ``updated:`` frontmatter date is used as the
    ``curated_at`` timestamp for every block in the file. If the
    frontmatter is missing or unparseable, falls back to the file's
    filesystem mtime.

    Returns an empty list when the file has no parseable blocks (e.g.
    only frontmatter + header, no curated entries yet).

    Block isolation strategy: scan for ``^## {journal} · {SENTIMENT} ·
    {themes}$`` headings and treat each as the start of a block, ending
    at the next such heading (or EOF). This is tolerant of trailing
    ``---`` separators being present or absent, and tolerant of the
    file's ``# Voice Published`` title living in the same chunk as the
    first card.
    """

    if not path.exists():
        return []

    content = path.read_text(encoding="utf-8")

    # Curated_at: try to read the ``updated:`` line from the frontmatter
    # block. The frontmatter is delimited by ``---\n``...``---\n``.
    curated_at = _curated_at_from_frontmatter(content) or _curated_at_from_mtime(path)

    # Strip the frontmatter so heading-scanning isn't tricked by stray
    # text inside the YAML block.
    body = _strip_frontmatter(content)

    # Find every block-start heading offset in the body.
    starts = [m.start() for m in _BLOCK_START_RE.finditer(body)]
    if not starts:
        return []

    rows: list[VoicePanelRow] = []
    # Pair each start with the next start (or EOF) to slice out one block.
    boundaries = starts + [len(body)]
    for i in range(len(starts)):
        block = body[boundaries[i] : boundaries[i + 1]]
        # Strip trailing ``---`` separator (if present) so it doesn't
        # contaminate the body blockquote regex.
        block = re.sub(r"\n---\s*\n*\Z", "\n", block).strip()
        if not block:
            continue
        row = _parse_voice_block(block, curated_at=curated_at)
        if row is not None:
            rows.append(row)
    return rows


def parse_voice_published_dir(path: Path) -> list[VoicePanelRow]:
    """Parse all ``*.md`` files in a ``Published/`` directory.

    Files are sorted by name (ISO-week filenames sort lexicographically
    = chronologically, e.g. ``2026-W22.md`` < ``2026-W23.md``).
    Within each file, blocks appear in curation order (top of file =
    earliest curated). Returns the flattened list.

    Robust to:

    * Directory does not exist → empty list (v1 is pre-curation).
    * Files contain frontmatter only → empty list (no rows extracted).
    * Mixed valid + invalid blocks → only valid blocks are returned;
      warnings are logged for the invalid ones.
    """

    if not path.exists() or not path.is_dir():
        return []

    rows: list[VoicePanelRow] = []
    for md_file in sorted(path.glob("*.md")):
        try:
            file_rows = parse_voice_published_file(md_file)
        except OSError as exc:
            logger.warning("Failed to read voice published file %s: %s", md_file, exc)
            continue
        rows.extend(file_rows)
    return rows


def _strip_frontmatter(content: str) -> str:
    """Remove a leading ``---\\n...\\n---\\n`` frontmatter block from content.

    Returns the original content if frontmatter is absent.
    """

    if not content.startswith("---"):
        return content
    end = content.find("\n---", 3)
    if end == -1:
        return content
    # Advance past the closing ``---`` and the trailing newline.
    return content[end + 4 :].lstrip("\n")


def _curated_at_from_frontmatter(content: str) -> datetime | None:
    """Parse the ``updated: YYYY-MM-DD`` line from the frontmatter block.

    Returns ``None`` if not found / unparseable; caller falls back to
    file mtime.
    """

    if not content.startswith("---"):
        return None
    end = content.find("\n---", 3)
    if end == -1:
        return None
    frontmatter = content[3:end]
    match = re.search(r"^updated:\s*(\d{4}-\d{2}-\d{2})", frontmatter, re.MULTILINE)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _curated_at_from_mtime(path: Path) -> datetime:
    """Return the file's filesystem mtime as a UTC ``datetime``.

    Fallback when the frontmatter has no ``updated:`` field.
    """

    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


# =========================================================================
# Public API: build_aggregate
# =========================================================================


def build_aggregate(
    tagged_comments: Iterable[TaggedComment],
    threads: dict[str, Thread],
    voice_published: list[VoicePanelRow] | None = None,
    *,
    time_horizon: str = DEFAULT_TIME_HORIZON_STR,
    voice_published_dir: Path | None = None,
    now: datetime | None = None,
) -> Aggregate:
    """Build the dashboard's aggregate from tagged comments + threads.

    :param tagged_comments: iterable of all in-scope tagged comments. Will
        be filtered to drop ``product_attribution == "out-of-scope"`` per
        v1 scope.
    :param threads: dict keyed by ``thread.id`` (PRAW fullname like
        ``"t3_abc123"``). Phase 7 looks up each supporting comment's
        parent thread from this dict.
    :param voice_published: optional pre-loaded voice panel rows. If
        ``None``, the function loads them from
        ``voice_published_dir`` (or the default vault location). Passing
        an explicit list is the test-friendly path.
    :param time_horizon: ISO date string for the dashboard horizon.
        Defaults to ``"2010-01-01"`` per spec D7.
    :param voice_published_dir: optional override of the
        ``Voice of Academia/Published/`` directory. Defaults to
        :data:`pipeline.config.VAULT_VOICE_PUBLISHED`. Mostly useful in
        tests.
    :param now: optional override for ``generated_at`` (test-friendly).
        Defaults to ``datetime.now(tz=timezone.utc)``.

    :returns: a populated :class:`pipeline.models.Aggregate` ready for
        serialization via :func:`pipeline.store.write_aggregate_json`.
    """

    # 1. Filter to in-scope comments.
    in_scope = [tc for tc in tagged_comments if _is_in_scope(tc)]

    # 2. Bucket by (sentiment, theme, journal) — multi-row fan-out per
    #    spec D1 when a comment has multiple ThemeAssignment entries.
    buckets: dict[tuple[ThemeSentiment, str, str], list[TaggedComment]] = defaultdict(list)
    for tc in in_scope:
        for assignment in tc.themes:
            key = _bucket_key(tc, assignment.theme, assignment.sentiment)
            buckets[key].append(tc)

    # 3. Resolve time horizon to a datetime for filtering.
    try:
        horizon_dt = datetime.strptime(time_horizon, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        # Mirror config default if the caller passes a bad string.
        logger.warning(
            "Unparseable time_horizon %r; falling back to config TIME_HORIZON.",
            time_horizon,
        )
        horizon_dt = TIME_HORIZON

    # 4. Project each bucket to a FrequencyRow (with placeholder rank).
    rows: list[FrequencyRow] = []
    for (sentiment, theme, journal), supporting in buckets.items():
        row = _build_row(
            sentiment=sentiment,
            theme=theme,
            journal=journal,
            supporting_tcs=supporting,
            threads=threads,
            horizon=horizon_dt,
        )
        if row is not None:
            rows.append(row)

    # 5. Compute the in-horizon thread / comment counts for the header.
    #    These count distinct contributing threads (not all threads in
    #    the corpus) — the dashboard says "tracked across N threads",
    #    meaning the universe that produced the displayed rows.
    counted_thread_ids: set[str] = set()
    counted_comment_ids: set[str] = set()
    for tc in in_scope:
        tid = comment_thread_id(tc)
        if tid is None:
            continue
        thread = threads.get(tid)
        if thread is None:
            continue
        if not _thread_in_horizon(thread, horizon_dt):
            continue
        counted_thread_ids.add(tid)
        counted_comment_ids.add(tc.comment_id)

    # 6. Rank rows in-place within (sentiment, journal).
    rows = _rank_rows(rows)

    # 7. Load voice panel.
    if voice_published is None:
        target_dir = voice_published_dir or VAULT_VOICE_PUBLISHED
        voice_published = parse_voice_published_dir(target_dir)

    # 8. Assemble the Aggregate.
    return Aggregate(
        generated_at=now or datetime.now(tz=timezone.utc),
        time_horizon=time_horizon,
        total_threads=len(counted_thread_ids),
        total_comments=len(counted_comment_ids),
        rows=rows,
        voice_published=voice_published,
    )
