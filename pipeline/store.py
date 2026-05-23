"""Phase 6 — Storage layer for the Voice of Academia pipeline.

Persists pipeline output to three destinations:

1. **Pinecone** (``jove-memory`` index, namespace ``social-listening``) —
   one record per :class:`~pipeline.models.Thread` and one per tagged
   :class:`~pipeline.models.Comment`. The integrated
   ``multilingual-e5-large`` embedding is configured server-side on the
   index — we send raw text and Pinecone embeds at upsert time. Per the
   index ``fieldMap`` (verified via ``pc.describe_index('jove-memory')``
   on 2026-05-23), the embedded field is named ``text``.

2. **Vault** (``$VAULT_ROOT/$VAULT_VOICE_CANDIDATES/YYYY-WWW.md``) —
   appends a Markdown block per ``voice_candidate=True`` comment so D
   can curate them in Obsidian once a week. First write to a given
   week's file creates the file with frontmatter per the
   ``Vault Organization Spec`` (kepano conventions); subsequent writes
   append blocks separated by ``---``.

3. **Aggregate JSON** — a low-level
   :func:`write_aggregate_json` helper Phase 7 will call after it builds
   the breadth-first ranked frequency table. This module owns the disk-
   serialization concern only; the aggregation logic itself is Phase 7.

Design notes:

* **Idempotency.** Pinecone ``upsert_records`` is upsert-by-``_id``;
  reposting the same record overwrites in place. So re-running
  :func:`upsert_thread` with stable PRAW fullnames is idempotent by
  construction. The Phase 8 mega-backfill checkpointing relies on this.

* **Metadata flattening.** Pinecone metadata supports flat scalars,
  booleans, and ``list[str]`` only — no nested dicts. ``TaggedComment``
  carries themes as ``list[ThemeAssignment]`` (Pydantic objects); we
  flatten to two parallel ``list[str]`` columns (``themes`` and
  ``themes_with_sentiment``) so the dashboard can filter on either
  flat-theme or theme+sentiment composites.

* **None handling.** Pinecone metadata cannot hold ``None``. We map
  ``sentiment=None`` (classification-failure fallback) to the constant
  :data:`pipeline.models.SENTIMENT_FALLBACK_STR` and ``journal_named=
  None`` / ``voice_candidate_reason=None`` to ``""``.

* **Batching.** ``upsert_records`` accepts up to 100 records per call.
  For threads with >100 comments we batch in chunks of 100. Most JoVE
  threads sit well below 100 comments, but the comment-tree expansion
  in Phase 3 can occasionally produce >100 (deep reply trees on the
  ``"Is JoVE legit?"`` seed thread, etc.).

* **Body truncation.** Pinecone metadata caps total record size at 40
  KB; ``multilingual-e5-large`` has an effective context window around
  512 tokens. We truncate ``text`` to :data:`PINECONE_BODY_MAX_CHARS`
  (4000 chars ≈ 1000 tokens — safely above the embedding budget so
  truncation happens at write rather than silently inside the embedding
  service).

References:

* Pinecone v9 SDK docs (verified 2026-05-23):
  https://docs.pinecone.io/reference/python-sdk
* ``Voice of Academia — Implementation Plan.md`` § Phase 6
* ``Voice of Academia — System Design.md`` § Pinecone schema
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pinecone import Pinecone

from pipeline.config import (
    PINECONE_API_KEY,
    PINECONE_INDEX,
    PINECONE_NAMESPACE,
    VAULT_VOICE_CANDIDATES,
)
from pipeline.models import (
    SENTIMENT_FALLBACK_STR,
    Comment,
    TaggedComment,
    Thread,
)

if TYPE_CHECKING:
    # The Pinecone SDK's Index class is not part of the public top-level API
    # in v9.x — we only need it for type hints, so keep the import private.
    from pinecone.db_data import _Index as PineconeIndex
else:
    PineconeIndex = Any  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


# ---- Tuning constants ----------------------------------------------------

#: Max chars of ``text`` we send to Pinecone. ``multilingual-e5-large``'s
#: effective context is ~512 tokens; 4000 chars is well above that and
#: leaves plenty of room within Pinecone's 40 KB per-record metadata cap.
PINECONE_BODY_MAX_CHARS: int = 4000

#: Pinecone's documented batch size limit for ``upsert_records`` (v9.x).
#: Going above this raises an API error; staying under keeps per-call
#: latency predictable for the cron job's 6-hour timeout.
PINECONE_BATCH_SIZE: int = 100


# =========================================================================
# Pinecone client (lazy — only constructed if upsert is called)
# =========================================================================


# Cached Pinecone Index handle. Typed as ``Any`` so the v9 SDK's
# ``Index | GrpcIndex`` return shape from ``pc.Index(...)`` doesn't fight
# mypy — the actual call surface we use (``upsert_records``) is identical
# across both.
_PINECONE_INDEX_CACHE: Any = None


def _get_index() -> Any:
    """Return a cached handle to the ``jove-memory`` Pinecone index.

    Constructed lazily so importing :mod:`pipeline.store` doesn't
    require ``PINECONE_API_KEY`` at import time — keeping unit tests
    (which mock the index) fast and cred-free.

    Raises ``RuntimeError`` if ``PINECONE_API_KEY`` is unset when an
    actual upsert is attempted — surface the misconfig loudly rather
    than fail silently mid-batch.
    """

    global _PINECONE_INDEX_CACHE
    if _PINECONE_INDEX_CACHE is None:
        if not PINECONE_API_KEY:
            raise RuntimeError(
                "PINECONE_API_KEY is not set. Set it in .env (see "
                ".env.example) before calling store.upsert_thread()."
            )
        pc = Pinecone(api_key=PINECONE_API_KEY)
        _PINECONE_INDEX_CACHE = pc.Index(PINECONE_INDEX)
    return _PINECONE_INDEX_CACHE


# =========================================================================
# Pinecone record builders
# =========================================================================


def _truncate_text(s: str, max_chars: int = PINECONE_BODY_MAX_CHARS) -> str:
    """Hard-truncate a string to ``max_chars``.

    No ellipsis — we want byte-exact reproducibility for the same input.
    Embedding quality at boundary cuts isn't meaningfully different
    from clean cuts at this length; the dashboard will surface the full
    body via the Reddit ``permalink`` anyway.
    """

    if len(s) <= max_chars:
        return s
    return s[:max_chars]


def _thread_record(thread: Thread) -> dict[str, Any]:
    """Build the Pinecone record for one thread (parent row).

    The ``text`` field is ``title + "\\n\\n" + body`` so semantic search
    can hit on either; long bodies are truncated to
    :data:`PINECONE_BODY_MAX_CHARS`.
    """

    body_part = thread.body or ""
    embedded_text = (
        thread.title + "\n\n" + body_part if body_part else thread.title
    )
    return {
        "_id": thread.id,
        "type": "thread",
        "subreddit": thread.subreddit,
        "author": thread.author or "[deleted]",
        "created_utc": thread.created_utc.isoformat(),
        "permalink": thread.permalink,
        "text": _truncate_text(embedded_text),
        "score": int(thread.score),
        "reply_count": int(thread.num_comments),
    }


def _comment_record(
    comment: Comment,
    tagged: TaggedComment,
    thread_id: str,
    subreddit: str,
    reply_count: int,
) -> dict[str, Any]:
    """Build the Pinecone record for one tagged comment.

    All tag-side fields are flattened to scalars or ``list[str]`` because
    Pinecone metadata does not support nested objects.

    ``parent_thread_id`` is populated from the explicit ``thread_id``
    argument (which the caller derives from ``Thread.id`` — we don't
    rely on ``TaggedComment.thread_id`` being non-None during the
    transition period when older tagged-comment artifacts may pre-date
    the contract fix).

    Per Pinecone metadata constraints:
    * ``None`` becomes the appropriate string sentinel
      (:data:`SENTIMENT_FALLBACK_STR` for sentiment, ``""`` for
      ``journal_named`` and ``voice_candidate_reason``).
    * ``ThemeAssignment`` Pydantic objects are flattened to two parallel
      ``list[str]``s:
        - ``themes`` — just theme IDs, for cheap filtering.
        - ``themes_with_sentiment`` — ``"{theme}:{sentiment}"`` pairs,
          for compound dashboard filters (e.g. "show all positive
          ``video_format`` comments").
    """

    sentiment_str = (
        tagged.sentiment if tagged.sentiment is not None else SENTIMENT_FALLBACK_STR
    )
    themes_flat = [ta.theme for ta in tagged.themes]
    themes_with_sentiment = [f"{ta.theme}:{ta.sentiment}" for ta in tagged.themes]

    return {
        "_id": comment.id,
        "type": "comment",
        "subreddit": subreddit,
        "author": comment.author or "[deleted]",
        "created_utc": comment.created_utc.isoformat(),
        "permalink": comment.permalink,
        "parent_id": comment.parent_id,
        "parent_thread_id": thread_id,
        "text": _truncate_text(comment.body),
        "score": int(comment.score),
        "reply_count": int(reply_count),
        "sentiment": sentiment_str,
        "themes": themes_flat,
        "themes_with_sentiment": themes_with_sentiment,
        "product_attribution": tagged.product_attribution,
        "journal_named": tagged.journal_named or "",
        "voice_candidate": bool(tagged.voice_candidate),
        "voice_candidate_reason": tagged.voice_candidate_reason or "",
        "tagged_at": tagged.tagged_at.isoformat(),
        "tagged_by_model": tagged.tagged_by_model,
    }


def _count_direct_replies(comment_id: str, comments: list[Comment]) -> int:
    """Count comments whose ``parent_id`` is this comment's fullname.

    PRAW's depth attribute tells us how far we are from the submission
    root, but doesn't directly expose direct-reply counts; we recompute
    by scanning the flat comment list. O(N) per comment in a thread;
    fine for thread sizes typical of the JoVE corpus (most <50, rare
    threads up to ~300).
    """

    return sum(1 for c in comments if c.parent_id == comment_id)


# =========================================================================
# Public API — upsert_thread
# =========================================================================


def upsert_thread(
    thread: Thread,
    tagged_comments: list[TaggedComment],
    *,
    index: PineconeIndex | None = None,
    namespace: str = PINECONE_NAMESPACE,
    batch_size: int = PINECONE_BATCH_SIZE,
) -> None:
    """Persist one thread + its tagged comments to Pinecone.

    Writes to namespace ``social-listening`` on the ``jove-memory``
    index. Idempotent: Pinecone ``upsert_records`` upserts by ``_id``
    (=PRAW fullname here), so re-running with the same data overwrites
    in place. The Phase 8 mega-backfill checkpointing depends on this.

    :param thread: a fully-fetched thread + comment tree.
    :param tagged_comments: the Phase 5 outputs corresponding to
        ``thread.comments``. Comments without a matching tag (e.g. ones
        Phase 5 hadn't reached yet at a partial-run boundary) are
        silently skipped — they will be picked up on the next pass.
    :param index: optional Pinecone Index handle for dependency
        injection in tests. Defaults to the live ``jove-memory`` index.
    :param namespace: target namespace. Defaults to
        :data:`pipeline.config.PINECONE_NAMESPACE` (``social-listening``).
    :param batch_size: records per ``upsert_records`` call. Capped at
        100 by Pinecone v9.x. Lower it to reduce per-call latency or
        when running under tight memory budgets.

    Records emitted (in order):

    1. **One thread record** — ``_id = thread.id``, ``type="thread"``,
       ``text = title + body``.
    2. **One comment record per matched tag** — ``_id = comment.id``,
       ``type="comment"``, ``text = comment.body``, with all Phase 5
       classification + voice fields flattened to scalars/lists.

    Batching: records are submitted in chunks of at most ``batch_size``;
    for a thread with 250 comments and ``batch_size=100`` this produces
    3 ``upsert_records`` calls (100 + 100 + 51 — the +1 is the thread
    record, packed into the first batch).
    """

    if batch_size > PINECONE_BATCH_SIZE:
        raise ValueError(
            f"batch_size must be <= {PINECONE_BATCH_SIZE} (Pinecone v9 cap); "
            f"got {batch_size}."
        )

    idx = index if index is not None else _get_index()

    # Build the tag lookup by comment fullname so we can attach O(1).
    tag_by_comment_id = {tc.comment_id: tc for tc in tagged_comments}

    records: list[dict[str, Any]] = []
    records.append(_thread_record(thread))

    for comment in thread.comments:
        tag = tag_by_comment_id.get(comment.id)
        if tag is None:
            # Comment was fetched but not yet tagged — skip; next run
            # picks it up. We intentionally do NOT write a tag-less
            # comment record because the schema requires the tag fields
            # to be present for consistent filterability.
            continue
        rec = _comment_record(
            comment,
            tag,
            thread_id=thread.id,
            subreddit=thread.subreddit,
            reply_count=_count_direct_replies(comment.id, thread.comments),
        )
        records.append(rec)

    if not records:
        # Shouldn't happen in practice — the thread record itself is
        # always appended. Guard so an empty batch doesn't raise from
        # Pinecone's "records is empty" validation.
        logger.warning(
            "upsert_thread called with no records to write for thread %s",
            thread.id,
        )
        return

    # Batch the records (chunks of <= batch_size) and submit.
    total = len(records)
    for start in range(0, total, batch_size):
        chunk = records[start : start + batch_size]
        idx.upsert_records(namespace=namespace, records=chunk)
        logger.info(
            "Upserted %d/%d records to %s (thread=%s, chunk_start=%d)",
            len(chunk),
            total,
            namespace,
            thread.id,
            start,
        )


# =========================================================================
# Vault voice-candidate writes
# =========================================================================


_CANDIDATES_FRONTMATTER_TEMPLATE = """\
---
type: note
status: active
domain: [research]
tags: [type/note, status/active, jove, research, voice-of-academia, voice-candidates, src/research]
created: {created}
updated: {updated}
source_urls: []
confidence: inferred
aliases: []
---

# Voice Candidates — Week {week}

> [!info] **LLM-surfaced Voice of Academia candidates for week {week}.** D's weekly curation ritual reviews these and promotes the keepers to ``Voice of Academia/Published/{week}.md``. This file is the immutable LLM proposal log — do not edit existing entries; new candidates append below.

"""


def _candidates_path(week: str) -> Path:
    """Return the candidates Markdown path for a given ISO week.

    Format: ``{VAULT_VOICE_CANDIDATES}/{YYYY-WWW}.md``, where ``YYYY-WWW``
    is ISO-week notation (e.g. ``2026-W22``). Splitting per week makes
    the human curation flow a fixed-size weekly chunk and lets the
    Phase 11 ``voice_curate`` CLI scope its prompts to one file at a
    time.
    """

    return VAULT_VOICE_CANDIDATES / f"{week}.md"


def _current_week_iso(now: datetime | None = None) -> str:
    """Return the ISO-week string ``YYYY-WWW`` for now (or a passed time).

    Uses ISO-week semantics (``%G-W%V``), not calendar-year week
    (``%Y-W%U``). At year boundaries this matters — the first few days
    of January can belong to week 52/53 of the prior year.
    """

    if now is None:
        now = datetime.now(tz=timezone.utc)
    return now.strftime("%G-W%V")


def _format_voice_block(
    tagged: TaggedComment, comment: Comment, thread: Thread
) -> str:
    """Render one Markdown block for a voice candidate.

    Layout matches the Phase 6 spec snippet plus the Vault Organization
    rule that quotes get blockquote (``>``) prefixes. Theme list is
    rendered as ``"theme:sentiment"`` pairs so the curator can see at a
    glance which theme drove the candidacy.
    """

    sentiment = (
        tagged.sentiment.upper()
        if tagged.sentiment is not None
        else SENTIMENT_FALLBACK_STR.upper()
    )
    themes_pretty = (
        ", ".join(f"{ta.theme}:{ta.sentiment}" for ta in tagged.themes)
        or "other"
    )
    journal = tagged.journal_named or "Unknown journal"
    reason = tagged.voice_candidate_reason or "(no reason recorded)"
    author = comment.author or "[deleted]"
    date = comment.created_utc.strftime("%Y-%m-%d")

    # Render the body as a blockquote — split on newlines so multi-line
    # comments preserve the blockquote prefix on each line (Obsidian
    # markdown collapses adjacent ``> ...`` lines into one blockquote).
    body_lines = [f"> {line}" if line else ">" for line in comment.body.splitlines()]
    if not body_lines:
        body_lines = ["> (empty body)"]
    body_block = "\n".join(body_lines)

    return (
        f"## {journal} · {sentiment} · {themes_pretty}\n"
        f"**Author**: u/{author}  \n"
        f"**Thread**: [{thread.title}]({thread.permalink})  \n"
        f"**Comment**: [link]({comment.permalink})  \n"
        f"**Date**: {date}  \n"
        f"**Score**: {comment.score}  \n"
        f"**Voice rubric**: {reason}\n\n"
        f"{body_block}\n\n"
        f"---\n\n"
    )


def write_voice_candidates(
    tagged_comments: list[TaggedComment],
    thread: Thread,
    *,
    week: str | None = None,
    now: datetime | None = None,
) -> Path | None:
    """Append voice candidates to that week's Markdown file.

    Only comments with ``voice_candidate=True`` are written. The file
    path is ``{VAULT_VOICE_CANDIDATES}/{week}.md``. First write to a
    given week creates the file with frontmatter (per the Vault
    Organization Spec — ``type: note``, ``status: active``,
    ``domain: [research]``, etc.); subsequent writes append new blocks
    separated by ``---``.

    Returns the :class:`Path` that was written, or ``None`` if no
    candidates qualified (so the caller's logs can tell "no qualifying
    candidates" apart from "the file was written").

    :param tagged_comments: Phase 5 outputs for one thread.
    :param thread: parent thread (used for title + permalink in the
        rendered block).
    :param week: optional ISO-week string ``"YYYY-WWW"``. Defaults to
        the current UTC week. Accepted explicitly so the Phase 8 mega-
        backfill can stamp historical comments with the *current*
        curation week (so D reviews the whole backfill in one weekly
        batch, not 600 separate week-files).
    :param now: optional override of "current time", for tests.
    """

    # Filter to the candidates we'll write. If nothing qualifies, leave
    # the file untouched — empty appends create confusing "(empty)"
    # blocks that the curator has to skim past.
    candidates_to_write: list[tuple[TaggedComment, Comment]] = []
    comment_by_id = {c.id: c for c in thread.comments}
    for tc in tagged_comments:
        if not tc.voice_candidate:
            continue
        comment = comment_by_id.get(tc.comment_id)
        if comment is None:
            # Tagged-comment refers to a comment we don't have in this
            # thread — likely a stale tag from a prior fetch that
            # dropped a deleted comment. Skip cleanly.
            logger.warning(
                "Tagged comment %s has no matching Comment in thread %s; skipping voice write.",
                tc.comment_id,
                thread.id,
            )
            continue
        candidates_to_write.append((tc, comment))

    if not candidates_to_write:
        return None

    if week is None:
        week = _current_week_iso(now=now)

    path = _candidates_path(week)
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        today = (now or datetime.now(tz=timezone.utc)).strftime("%Y-%m-%d")
        header = _CANDIDATES_FRONTMATTER_TEMPLATE.format(
            created=today, updated=today, week=week
        )
        path.write_text(header, encoding="utf-8")

    blocks = "".join(
        _format_voice_block(tc, c, thread) for tc, c in candidates_to_write
    )
    with path.open("a", encoding="utf-8") as f:
        f.write(blocks)

    logger.info(
        "Wrote %d voice candidate(s) for thread %s to %s",
        len(candidates_to_write),
        thread.id,
        path,
    )
    return path


# =========================================================================
# Aggregate JSON helper (Phase 7 consumes)
# =========================================================================


def write_aggregate_json(agg_dict: dict[str, Any], output_path: Path) -> Path:
    """Serialize an aggregate dict to disk.

    This is the disk-serialization concern that Phase 7's
    ``aggregate.py`` will call after it builds the breadth-first
    frequency table. Owned by Phase 6 because the disk-IO posture
    (where the file lives, how the cron commits it) is a storage
    concern.

    Writes pretty-printed JSON with ``sort_keys=True`` for stable diffs
    when the cron commits ``dashboard/data/latest.json`` to the repo —
    a smaller diff makes the weekly auto-commit easier to review at a
    glance.

    :param agg_dict: pre-serialized dict (typically from a Pydantic
        ``model_dump(mode="json")`` so datetimes are already ISO
        strings).
    :param output_path: target file path. Parent dirs are created if
        missing.
    :returns: the resolved output path.
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(agg_dict, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Wrote aggregate JSON to %s", output_path)
    return output_path
