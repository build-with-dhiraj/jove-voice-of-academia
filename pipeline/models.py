"""Shared Pydantic v2 models for the Voice of Academia pipeline.

These are the contracts every downstream phase (fetch/disambiguate/tag/store/
aggregate) consumes. The Phase 2 dispatch in
``Voice of Academia — Implementation Plan.md`` defines the field set;
this module makes it concrete.

Three models live here:

* :class:`ThreadCandidate` — what discovery emits (a URL plus provenance).
* :class:`Thread` — a fully-fetched Reddit submission with its comment tree.
* :class:`Comment` — one comment, attached to a thread.

Downstream models (``TaggedComment``, ``FrequencyRow``, ``Aggregate``) are
defined in the phases that introduce them. Keeping this module narrow avoids
forcing Phase 2 to lock decisions that belong to Phase 5/7.
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
