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

Downstream models (``FrequencyRow``, ``Aggregate``) are defined in the phases
that introduce them. Keeping this module narrow avoids forcing Phase 2 to lock
decisions that belong to Phase 7.

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
