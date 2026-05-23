"""Shared Pydantic v2 models for the Voice of Academia pipeline.

These are the contracts every downstream phase (fetch/disambiguate/tag/store/
aggregate) consumes. The Phase 2 dispatch in
``Voice of Academia — Implementation Plan.md`` defines the field set;
this module makes it concrete.

Models that live here:

* :class:`ThreadCandidate` — what discovery emits (a URL plus provenance).
* :class:`Thread` — a fully-fetched Reddit submission with its comment tree.
* :class:`Comment` — one comment, attached to a thread.
* :class:`ThemeAssignment` — one (theme, sentiment) pair for a tagged comment.
* :class:`TaggedComment` — output of Phase 5 tagging; consumed by Phase 6 store.
* :class:`FrequencyRow` — one row in the dashboard frequency table (Phase 7).
* :class:`VoicePanelRow` — one curated quote in the Voice of Academia panel
  (Phase 7 + Phase 11 curation).
* :class:`Aggregate` — the top-level dashboard document (Phase 7).

The Phase 7 models (``FrequencyRow`` / ``VoicePanelRow`` / ``Aggregate``)
mirror the TypeScript shapes in ``dashboard/lib/types.ts`` field-for-field.
Drift between this module and that TypeScript file = broken dashboard at
build time, so the two must be kept in sync.

Note: ``TaggedComment`` is added here (rather than in a separate
``tag_models.py``) because the Implementation Plan's file-structure tree
explicitly lists it under ``pipeline/models.py``, and because Phases 6 and 7
import it as part of the same pipeline contract surface. Splitting would
fragment the import surface for downstream specialists.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DiscoveredVia = Literal[
    "ring1_subreddit_enum",
    "ring2_reddit_search",
    "ring3_firecrawl_google",
]

# Phase 6 store-layer constant. Pinecone metadata cannot hold ``None``;
# ``TaggedComment.sentiment`` is ``None`` on classification failure (see
# :mod:`pipeline.tag` fallback). When persisting that record to Pinecone we
# substitute this literal string so the column type stays uniform across
# every record in the ``social-listening`` namespace. ``"unknown"`` (not
# ``"null"``) so dashboard filters can render it as a real bucket without
# special-casing.
SENTIMENT_FALLBACK_STR: str = "unknown"


class ThreadCandidate(BaseModel):
    """A Reddit thread surfaced by one of the three discovery rings.

    A candidate is *not* a confirmed in-scope JoVE thread — disambiguation
    (Phase 4) decides that, after fetch (Phase 3) has hydrated the body and
    comments. The candidate carries enough provenance to debug discovery:
    which ring found it, what seed query (if any) brought it in, and when.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    permalink: str = Field(
        description="Canonical Reddit URL — ``https://www.reddit.com/r/<sub>/comments/<id>/<slug>/``"
    )
    subreddit: str = Field(description="Subreddit display name (no ``r/`` prefix)")
    discovered_via: DiscoveredVia = Field(
        description="Which of the three concentric rings produced this candidate"
    )
    discovered_at: datetime = Field(description="UTC timestamp the candidate was emitted")
    seed_query: str | None = Field(
        default=None,
        description="The keyword/query that surfaced this thread (None for Ring 1 enumeration)",
    )


class Comment(BaseModel):
    """A single Reddit comment within a fetched :class:`Thread`.

    Field names mirror PRAW's ``Comment`` API where reasonable, but ``id`` is
    PRAW's ``fullname`` (``t1_xxx``) — the namespaced form Pinecone will key on
    — not the bare 36-character id.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="PRAW fullname, e.g. ``t1_abc123``")
    parent_id: str = Field(
        description="PRAW fullname of the parent (``t3_xxx`` for top-level comments, "
        "``t1_xxx`` for replies)"
    )
    permalink: str = Field(description="Absolute reddit.com URL to this comment")
    body: str = Field(description="Markdown body. ``[deleted]``/``[removed]`` are filtered at fetch time")
    author: str | None = Field(
        default=None,
        description="Reddit username; None if the account is deleted",
    )
    created_utc: datetime = Field(description="Comment creation time, UTC")
    score: int = Field(description="Net upvotes at scrape time")
    depth: int = Field(
        ge=0,
        description="Distance from the submission root (0 = top-level comment)",
    )


class Thread(BaseModel):
    """A fully-fetched Reddit submission plus its comment tree.

    Discovery produces :class:`ThreadCandidate`; fetch (Phase 3) hydrates it
    into this shape. The ``comments`` list is flat (not a tree); the tree
    structure is recoverable from each ``Comment.parent_id``.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="PRAW fullname, e.g. ``t3_abc123``")
    permalink: str = Field(description="Absolute reddit.com URL to the submission")
    subreddit: str = Field(description="Subreddit display name (no ``r/`` prefix)")
    title: str = Field(description="Submission title")
    body: str | None = Field(
        default=None,
        description="Self-text body, or None for link posts",
    )
    author: str | None = Field(
        default=None,
        description="Reddit username; None if the account is deleted",
    )
    created_utc: datetime = Field(description="Submission creation time, UTC")
    score: int = Field(description="Net upvotes at scrape time")
    num_comments: int = Field(
        ge=0,
        description="Reddit-reported count (may differ from ``len(comments)`` "
        "if some are deleted/removed and filtered out)",
    )
    is_self: bool = Field(description="True for text/self-posts, False for link posts")
    comments: list[Comment] = Field(
        default_factory=list,
        description="Flat list of public comments (deleted/removed excluded per RBP)",
    )


# ---- Phase 5 (tag) — emits by ``pipeline.tag.tag_comment`` ---------------

Sentiment = Literal["positive", "neutral", "negative", "mixed"]
ThemeSentiment = Literal["positive", "neutral", "negative"]
ProductAttribution = Literal[
    "journal",
    "eoe",
    "visualize",
    "research-general",
    "other-journal",
    "out-of-scope",
]


class ThemeAssignment(BaseModel):
    """One (theme, sentiment) pair attached to a tagged comment.

    Per spec D1, a single comment can hold multiple ``ThemeAssignment``
    entries — e.g. ``"the videos are great but the fee is outrageous"``
    becomes ``[ThemeAssignment(theme="video_format", sentiment="positive"),
    ThemeAssignment(theme="author_fees", sentiment="negative")]``.

    Per spec D2, themes are mutually exclusive within a sentiment bucket on
    a single comment — the LLM is instructed to emit at most one row per
    sentiment unless the comment genuinely contains two distinct ideas.

    The ``theme`` value is the ``id`` from ``data/taxonomy.json`` (e.g.
    ``"video_format"``), or the literal ``"emerging_other"`` for comments
    that don't fit a canonical theme (spec D2 fallback).
    """

    model_config = ConfigDict(extra="forbid")

    theme: str = Field(
        description=(
            "Canonical theme ``id`` from ``data/taxonomy.json``, or "
            "``'emerging_other'`` for the spec-D2 fallback bucket."
        )
    )
    sentiment: ThemeSentiment = Field(
        description="Sentiment of this comment toward this specific theme."
    )


class TaggedComment(BaseModel):
    """Output of Phase 5 tagging — one per :class:`Comment`.

    Field set matches the Pinecone metadata schema defined in
    ``Voice of Academia — Implementation Plan.md`` Phase 6. Phase 6 (store)
    consumes this directly when upserting comment records to the
    ``social-listening`` namespace.

    Notes on key fields:

    * ``sentiment`` — overall comment sentiment (may be ``None`` only when
      classification fails — see Phase 5 error-handling fallback in
      :mod:`pipeline.tag`). Distinct from per-theme sentiment in
      ``themes``: a comment can be overall ``"mixed"`` while each theme
      assignment carries one of ``positive|neutral|negative``.
    * ``themes`` — list of :class:`ThemeAssignment`. May be empty only on
      hard parse failure; even then the fallback writes a single
      ``ThemeAssignment(theme="emerging_other", sentiment="neutral")``.
    * ``product_attribution`` — v1 is always ``"journal"`` when
      ``journal_named == "JoVE"``, ``"out-of-scope"`` otherwise. Other
      values are reserved for v2 expansion.
    * ``journal_named`` — v1 admits only ``"JoVE"`` or ``None``
      (``data/journals.json`` v1_active).
    * ``voice_candidate`` / ``voice_candidate_reason`` — output of the
      Sonnet judgment call against ``data/voice-rubric.md``.
    * ``tagged_by_model`` — concatenation of the two model IDs used,
      e.g. ``"claude-haiku-4-5-20251001+claude-sonnet-4-6"``.
    """

    model_config = ConfigDict(extra="forbid")

    comment_id: str = Field(
        description="PRAW fullname (``t1_xxx``) — matches ``Comment.id``."
    )
    thread_id: str | None = Field(
        default=None,
        description=(
            "PRAW fullname of the parent submission (``t3_xxx``) the comment "
            "lives under. ``None`` is permitted for backwards-compatibility "
            "with TaggedComments emitted before this field existed, but "
            "Phase 5's ``tag_comment`` populates it from ``thread_context.id``. "
            "Phase 7's aggregator uses it to join comments to their parent "
            "thread without re-parsing the permalink with regex."
        ),
    )
    permalink: str = Field(description="Absolute reddit.com URL to the comment.")
    sentiment: Sentiment | None = Field(
        default=None,
        description=(
            "Overall comment sentiment. ``None`` only on classification "
            "failure (see :mod:`pipeline.tag` fallback)."
        ),
    )
    themes: list[ThemeAssignment] = Field(
        default_factory=list,
        description=(
            "List of (theme, sentiment) assignments. Multi-theme per spec D1; "
            "``emerging_other`` fallback per spec D2."
        ),
    )
    product_attribution: ProductAttribution = Field(
        description=(
            "Which JoVE product line the comment is about. v1 = "
            "``'journal'`` (JoVE) or ``'out-of-scope'``."
        )
    )
    journal_named: str | None = Field(
        default=None,
        description=(
            "Journal explicitly named in the comment. v1 admits only "
            "``'JoVE'`` or ``None`` per ``data/journals.json`` v1_active."
        ),
    )
    voice_candidate: bool = Field(
        description=(
            "True if the Sonnet rubric judgment returned YES "
            "(quotable by a journalist)."
        )
    )
    voice_candidate_reason: str | None = Field(
        default=None,
        description=(
            "One-sentence reasoning from the Sonnet rubric call. ``None`` "
            "is permitted but rare — the prompt asks for a reason on every "
            "judgment so D can audit drift."
        ),
    )
    tagged_at: datetime = Field(
        description="UTC timestamp of when :mod:`pipeline.tag` ran on this comment."
    )
    tagged_by_model: str = Field(
        description=(
            "Concatenated model IDs used, "
            "e.g. ``'claude-haiku-4-5-20251001+claude-sonnet-4-6'``."
        )
    )


# ---- Phase 7 (aggregate) — consumed by the dashboard's ``latest.json`` ----
#
# Field-for-field mirror of ``dashboard/lib/types.ts``. When this list of
# fields changes, the TypeScript types file MUST change in lockstep (the
# Next.js dashboard renders against those types and ``JSON.parse`` will
# silently accept extras but fail-by-undefined on missing fields the UI
# components reference).


class FrequencyRow(BaseModel):
    """One ``(sentiment × theme × journal)`` row in the frequency table.

    Per spec D1, a single comment can fan out into multiple rows when it
    carries multiple ``ThemeAssignment`` entries. Per spec D4, the
    ``rank`` is breadth-first within ``(sentiment, journal)``:
    ``(-thread_count, -comment_count, -last_comment_at)``. Per spec D5,
    the recency tiebreaker uses the most-recent-comment timestamp on
    this bucket.

    Mirrors ``FrequencyRow`` in ``dashboard/lib/types.ts``.
    """

    model_config = ConfigDict(extra="forbid")

    sentiment: ThemeSentiment = Field(
        description=(
            "Per-theme sentiment: one of ``positive|neutral|negative``. "
            "Not 4-value — per spec D1 a comment's overall ``mixed`` "
            "sentiment is decomposed into one row per (theme, polarity), "
            "so the frequency table only sees 3-value sentiments."
        )
    )
    theme: str = Field(
        description=(
            "Canonical theme ``id`` from ``data/taxonomy.json``, or "
            "``'emerging_other'`` for the spec-D2 fallback bucket."
        )
    )
    journal: str = Field(
        description=(
            "Journal this row pertains to. v1: always ``'JoVE'``. "
            "Reserved for v2 multi-journal expansion."
        )
    )
    thread_count: int = Field(
        ge=0,
        description=(
            "Distinct threads where at least one comment is tagged "
            "``(theme, sentiment, journal)``. Primary ranking dimension "
            "(spec D4 breadth-first)."
        ),
    )
    comment_count: int = Field(
        ge=0,
        description=(
            "Distinct comments tagged ``(theme, sentiment, journal)``. "
            "Secondary ranking dimension."
        ),
    )
    upvote_sum: int = Field(
        description=(
            "Sum of ``Comment.score`` across all supporting comments. "
            "Shown on the row but NOT used in ranking math at v1 (spec "
            "D4 — engagement weighting deferred to v2)."
        )
    )
    reply_count: int = Field(
        ge=0,
        description=(
            "Sum of direct-reply counts across supporting comments — "
            "how many comments have ``parent_id`` equal to a supporting "
            "comment's id. Computed by walking each supporting thread's "
            "comment list. Like ``upvote_sum``, shown but not used in "
            "ranking."
        ),
    )
    last_comment_at: datetime = Field(
        description=(
            "Most-recent ``Comment.created_utc`` among supporting "
            "comments. Doubles as the recency tiebreaker (spec D5)."
        )
    )
    newest_thread_at: datetime = Field(
        description=(
            "Most-recent ``Thread.created_utc`` among supporting "
            "threads. Displayed as a column so the CEO can see "
            "old-but-still-alive vs new-conversation themes at a glance."
        )
    )
    thread_urls: list[str] = Field(
        description=(
            "Deduplicated, sorted list of supporting ``Thread.permalink`` "
            "URLs. Click-through targets for the row-expansion UX in "
            "Phase 9's dashboard."
        )
    )
    rank: int = Field(
        ge=1,
        description=(
            "1-indexed position within ``(sentiment, journal)`` bucket — "
            "``rank=1`` is the highest-breadth row. Computed at "
            "aggregate-build time per spec D4."
        ),
    )


class VoicePanelRow(BaseModel):
    """One curated voice quote on the Voice of Academia panel.

    Sourced from D's weekly curation pass over LLM-surfaced candidates
    in the vault's ``Voice of Academia/Published/`` folder (spec D6).
    The shape MUST match the consumer types in
    ``dashboard/lib/types.ts``.

    The ``sentiment`` field is intentionally 4-value (``Sentiment``, not
    ``ThemeSentiment``) so genuinely mixed comments — e.g. *"videos are
    great but the fees are awful"* — can be surfaced honestly without
    forcing the curator to collapse the dual claim into a single polarity.
    See ``pipeline/aggregate.py`` module docstring for the rationale; the
    TypeScript type was widened in lockstep.
    """

    model_config = ConfigDict(extra="forbid")

    sentiment: Sentiment = Field(
        description=(
            "Overall sentiment of the curated comment: 4-value "
            "``positive|neutral|negative|mixed``. ``mixed`` is preserved "
            "from the source comment so quotes carrying competing claims "
            "(e.g. *praising one aspect, criticizing another*) read "
            "honestly on the panel — collapsing them would damage the "
            "panel's credibility (spec D6 asymmetric trust)."
        )
    )
    theme: str | None = Field(
        default=None,
        description=(
            "Optional canonical theme ``id`` from ``data/taxonomy.json``, "
            "or ``None``. Voice quotes are not required to map cleanly "
            "to a single theme — particularly when ``sentiment=='mixed'``."
        ),
    )
    product_attribution: ProductAttribution = Field(
        description=(
            "Which JoVE product line the comment is about. v1: "
            "``'journal'`` for the JoVE journal, ``'out-of-scope'`` for "
            "the (rare) panel entry that survives curation without "
            "neat product mapping."
        )
    )
    author: str = Field(
        description=(
            "Reddit username (e.g. ``'somePostdoc'``), or "
            "``'[deleted]'`` when the account was deleted by the user "
            "before curation. Public attribution per spec D12."
        )
    )
    permalink: str = Field(
        description=(
            "Absolute reddit.com URL to the original comment. "
            "Click-through target for the 'Open ↗' UI on the panel card."
        )
    )
    body: str = Field(
        description=(
            "Verbatim comment text as it appears on Reddit at curation "
            "time. May be lightly trimmed of trailing whitespace; "
            "content is never edited. Honors Reddit's RBP (spec D12)."
        )
    )
    score: int = Field(
        description=(
            "Net upvotes on the Reddit comment at curation time. "
            "Snapshot; not updated after curation since the score is "
            "an attestation of how the community reacted, not a live "
            "vote count."
        )
    )
    created_utc: datetime = Field(
        description=(
            "Original Reddit comment creation timestamp, UTC."
        )
    )
    curated_at: datetime = Field(
        description=(
            "When D promoted this candidate from the ``Candidates/`` "
            "folder to the ``Published/`` folder during a weekly "
            "curation ritual."
        )
    )
    curator_note: str | None = Field(
        default=None,
        description=(
            "Optional context D added during curation — e.g. "
            "*'former JoVE employee per thread context'* or "
            "*'concrete number + mixed sentiment'*. Renders as a "
            "footer on the panel card."
        ),
    )


class Aggregate(BaseModel):
    """The top-level dashboard document committed weekly to repo.

    Written by ``pipeline.aggregate.build_aggregate`` and serialized to
    ``dashboard/data/latest.json`` via ``pipeline.store.write_aggregate_json``.
    Read by ``dashboard/lib/data.ts`` at build time; rendered by the
    Next.js page under ISR.

    Mirrors ``Aggregate`` in ``dashboard/lib/types.ts``.
    """

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime = Field(
        description=(
            "UTC timestamp when this aggregate was built. Surfaces on "
            "the dashboard header as 'data last refreshed YYYY-MM-DD'."
        )
    )
    time_horizon: str = Field(
        description=(
            "ISO date string of the earliest thread/comment included. "
            "v1: ``'2010-01-01'`` per spec D7. Stored as string (not "
            "``datetime``) because it's a policy constant, not a "
            "moment in time — JSON consumers can treat it as a date."
        )
    )
    total_threads: int = Field(
        ge=0,
        description=(
            "Number of distinct threads contributing at least one "
            "in-scope tagged comment to the rows. Shown in the header."
        ),
    )
    total_comments: int = Field(
        ge=0,
        description=(
            "Number of in-scope tagged comments contributing to the "
            "rows. Shown in the header alongside ``total_threads``."
        ),
    )
    rows: list[FrequencyRow] = Field(
        description=(
            "Frequency-table rows, ordered by ``(sentiment, journal, "
            "rank)``. Phase 9 dashboard groups by sentiment for display."
        )
    )
    voice_published: list[VoicePanelRow] = Field(
        description=(
            "D's curated voice quotes, loaded from the vault's "
            "``Voice of Academia/Published/`` folder. Empty list is "
            "valid (no curation pass has occurred yet — common in v1 "
            "before first weekly review)."
        )
    )
