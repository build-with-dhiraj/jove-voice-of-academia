"""Unit tests for :mod:`pipeline.aggregate` (Phase 7 — aggregate layer).

Each test exercises one contract:

* **Bucketization** — group by ``(sentiment, theme, journal)`` and emit
  one row per bucket, with correct ``thread_count`` / ``comment_count``.
* **Ranking** — breadth-first (spec D4) with comment-count then
  recency tiebreakers (spec D5).
* **Multi-theme fan-out** — a comment with multiple ``ThemeAssignment``
  entries contributes to multiple buckets per spec D1.
* **Time-horizon filter** — pre-2010 threads are excluded per spec D7.
* **Empty voice panel** — no ``Published/`` directory ⇒ ``[]``, not an
  error.
* **Voice-panel parse** — given a synthetic ``Published/`` Markdown
  file, voice rows are correctly extracted.
* **Aggregate header counts** — ``total_threads`` and ``total_comments``
  reflect the in-scope contributing universe.
* **Sentinel: out-of-scope filter** — comments with
  ``product_attribution == "out-of-scope"`` are excluded entirely.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pipeline.aggregate import (
    DEFAULT_JOURNAL,
    DEFAULT_TIME_HORIZON_STR,
    build_aggregate,
    parse_voice_published_dir,
    parse_voice_published_file,
)
from pipeline.models import (
    Aggregate,
    Comment,
    FrequencyRow,
    TaggedComment,
    ThemeAssignment,
    Thread,
    VoicePanelRow,
)


# =========================================================================
# Fixture helpers
# =========================================================================


def _make_thread(
    *,
    thread_id: str,
    permalink: str | None = None,
    subreddit: str = "AskAcademia",
    created_utc: datetime | None = None,
    comments: list[Comment] | None = None,
    title: str = "Is JoVE respectable?",
) -> Thread:
    return Thread(
        id=thread_id,
        permalink=permalink
        or f"https://www.reddit.com/r/{subreddit}/comments/{thread_id[3:]}/slug/",
        subreddit=subreddit,
        title=title,
        body=None,
        author="op_user",
        created_utc=created_utc or datetime(2025, 6, 1, tzinfo=timezone.utc),
        score=42,
        num_comments=len(comments) if comments is not None else 0,
        is_self=True,
        comments=comments or [],
    )


def _make_comment(
    *,
    comment_id: str,
    parent_id: str,
    created_utc: datetime | None = None,
    score: int = 10,
    body: str = "JoVE comment body.",
    author: str | None = "some_user",
) -> Comment:
    return Comment(
        id=comment_id,
        parent_id=parent_id,
        permalink=(
            f"https://www.reddit.com/r/AskAcademia/comments/abc/_/{comment_id}/"
        ),
        body=body,
        author=author,
        created_utc=created_utc or datetime(2025, 6, 2, tzinfo=timezone.utc),
        score=score,
        depth=0,
    )


def _make_tagged(
    *,
    comment_id: str,
    thread_id: str,
    themes: list[ThemeAssignment],
    sentiment: str | None = "negative",
    product_attribution: str = "journal",
    journal_named: str | None = "JoVE",
) -> TaggedComment:
    return TaggedComment(
        comment_id=comment_id,
        thread_id=thread_id,
        permalink=(
            f"https://www.reddit.com/r/AskAcademia/comments/abc/_/{comment_id}/"
        ),
        sentiment=sentiment,  # type: ignore[arg-type]
        themes=themes,
        product_attribution=product_attribution,  # type: ignore[arg-type]
        journal_named=journal_named,
        voice_candidate=False,
        voice_candidate_reason=None,
        tagged_at=datetime(2025, 6, 3, tzinfo=timezone.utc),
        tagged_by_model="claude-haiku-4-5-20251001+claude-sonnet-4-6",
    )


# =========================================================================
# Bucketization happy path
# =========================================================================


def test_bucketization_basic_shapes() -> None:
    """3 threads × 2 tagged comments × 2 themes each → expected buckets.

    Verifies thread_count, comment_count, upvote_sum, rank=1 (only one
    bucket per sentiment in this fixture).
    """

    # Thread 1: 2 comments, both negative author_fees + negative legitimacy.
    t1_comments = [
        _make_comment(comment_id="t1_a", parent_id="t3_one", score=5),
        _make_comment(comment_id="t1_b", parent_id="t3_one", score=7),
    ]
    t1 = _make_thread(thread_id="t3_one", comments=t1_comments)

    # Thread 2 & 3: each 2 comments, same theme pattern.
    t2_comments = [
        _make_comment(comment_id="t2_a", parent_id="t3_two", score=3),
        _make_comment(comment_id="t2_b", parent_id="t3_two", score=4),
    ]
    t2 = _make_thread(thread_id="t3_two", comments=t2_comments)

    t3_comments = [
        _make_comment(comment_id="t3_a", parent_id="t3_three", score=2),
        _make_comment(comment_id="t3_b", parent_id="t3_three", score=1),
    ]
    t3 = _make_thread(thread_id="t3_three", comments=t3_comments)

    tagged: list[TaggedComment] = []
    for thread in (t1, t2, t3):
        for c in thread.comments:
            tagged.append(
                _make_tagged(
                    comment_id=c.id,
                    thread_id=thread.id,
                    themes=[
                        ThemeAssignment(theme="author_fees", sentiment="negative"),
                        ThemeAssignment(theme="legitimacy", sentiment="negative"),
                    ],
                )
            )

    threads = {t.id: t for t in (t1, t2, t3)}
    agg = build_aggregate(
        tagged, threads, voice_published=[], now=datetime(2026, 5, 23, tzinfo=timezone.utc)
    )

    # Two buckets: (negative, author_fees, JoVE) and (negative, legitimacy, JoVE).
    assert len(agg.rows) == 2

    rows_by_theme = {r.theme: r for r in agg.rows}
    fees = rows_by_theme["author_fees"]
    legit = rows_by_theme["legitimacy"]

    # Both buckets see all 3 threads, all 6 comments.
    assert fees.thread_count == 3
    assert fees.comment_count == 6
    assert fees.upvote_sum == 5 + 7 + 3 + 4 + 2 + 1
    assert fees.journal == DEFAULT_JOURNAL

    assert legit.thread_count == 3
    assert legit.comment_count == 6

    # Header totals.
    assert agg.total_threads == 3
    assert agg.total_comments == 6


# =========================================================================
# Ranking: breadth-first → depth → recency
# =========================================================================


def test_ranking_breadth_first_with_depth_tiebreaker() -> None:
    """Three buckets with thread counts [5,3,5] and comment counts [10,15,8].

    Expected: rank=1 is the (5-thread, 10-comment) bucket, rank=2 is
    (5-thread, 8-comment) bucket, rank=3 is (3-thread, 15-comment).
    Breadth-first beats depth — comment_count is only consulted on tie.
    """

    threads: dict[str, Thread] = {}
    tagged: list[TaggedComment] = []

    # Bucket A: theme="a", 5 threads × 2 comments = 10 comments.
    for i in range(5):
        tid = f"t3_a{i}"
        cs = [
            _make_comment(comment_id=f"ca{i}_x", parent_id=tid),
            _make_comment(comment_id=f"ca{i}_y", parent_id=tid),
        ]
        thread = _make_thread(thread_id=tid, comments=cs)
        threads[tid] = thread
        for c in cs:
            tagged.append(
                _make_tagged(
                    comment_id=c.id,
                    thread_id=tid,
                    themes=[ThemeAssignment(theme="a", sentiment="negative")],
                )
            )

    # Bucket B: theme="b", 3 threads × 5 comments = 15 comments.
    for i in range(3):
        tid = f"t3_b{i}"
        cs = [
            _make_comment(comment_id=f"cb{i}_{j}", parent_id=tid) for j in range(5)
        ]
        thread = _make_thread(thread_id=tid, comments=cs)
        threads[tid] = thread
        for c in cs:
            tagged.append(
                _make_tagged(
                    comment_id=c.id,
                    thread_id=tid,
                    themes=[ThemeAssignment(theme="b", sentiment="negative")],
                )
            )

    # Bucket C: theme="c", 5 threads × ~2 comments (only 8 total).
    # First 4 threads have 2 comments each, 5th has 0 comments — but
    # for thread_count we need ALL 5 to contribute. So put 1 comment
    # in thread 5.
    c_thread_ids = [f"t3_c{i}" for i in range(5)]
    c_comment_specs: list[tuple[str, str]] = []
    for i in range(4):
        for j in range(2):
            c_comment_specs.append((f"cc{i}_{j}", c_thread_ids[i]))
    # 5th thread: 0 supporting comments → won't count toward thread_count.
    # Instead make: 4 threads × 2 + 1 thread × 0 = 4 threads, 8 comments.
    # To hit 5 thread_count + 8 comment_count, we need 5 threads
    # contributing at least 1 comment each, plus 3 extras → 3 threads × 2
    # + 2 threads × 1 = 5 threads, 8 comments.
    c_comment_specs = []
    # 3 threads with 2 comments
    for i in range(3):
        c_comment_specs.append((f"cc{i}_0", c_thread_ids[i]))
        c_comment_specs.append((f"cc{i}_1", c_thread_ids[i]))
    # 2 threads with 1 comment
    for i in range(3, 5):
        c_comment_specs.append((f"cc{i}_0", c_thread_ids[i]))

    # Build the c threads with their comments.
    c_threads_comments: dict[str, list[Comment]] = {tid: [] for tid in c_thread_ids}
    for cid, tid in c_comment_specs:
        c_threads_comments[tid].append(
            _make_comment(comment_id=cid, parent_id=tid)
        )
    for tid in c_thread_ids:
        threads[tid] = _make_thread(thread_id=tid, comments=c_threads_comments[tid])

    for cid, tid in c_comment_specs:
        tagged.append(
            _make_tagged(
                comment_id=cid,
                thread_id=tid,
                themes=[ThemeAssignment(theme="c", sentiment="negative")],
            )
        )

    agg = build_aggregate(tagged, threads, voice_published=[])
    rows_by_theme = {r.theme: r for r in agg.rows}

    assert rows_by_theme["a"].thread_count == 5
    assert rows_by_theme["a"].comment_count == 10
    assert rows_by_theme["b"].thread_count == 3
    assert rows_by_theme["b"].comment_count == 15
    assert rows_by_theme["c"].thread_count == 5
    assert rows_by_theme["c"].comment_count == 8

    # Spec D4: thread_count primary → A (5) and C (5) before B (3);
    # then within (5,5) tie → A wins on comment_count (10 > 8).
    assert rows_by_theme["a"].rank == 1
    assert rows_by_theme["c"].rank == 2
    assert rows_by_theme["b"].rank == 3


def test_ranking_recency_tiebreaker() -> None:
    """Two buckets with identical thread_count AND comment_count.

    Recency wins per spec D5 — the bucket with the more-recent
    last_comment_at gets the lower (=better) rank.
    """

    # Both buckets: 1 thread × 1 comment each.
    early_thread = _make_thread(
        thread_id="t3_early",
        created_utc=datetime(2025, 1, 1, tzinfo=timezone.utc),
        comments=[
            _make_comment(
                comment_id="ce_1",
                parent_id="t3_early",
                created_utc=datetime(2025, 1, 5, tzinfo=timezone.utc),
            )
        ],
    )
    late_thread = _make_thread(
        thread_id="t3_late",
        created_utc=datetime(2025, 6, 1, tzinfo=timezone.utc),
        comments=[
            _make_comment(
                comment_id="cl_1",
                parent_id="t3_late",
                created_utc=datetime(2025, 6, 5, tzinfo=timezone.utc),
            )
        ],
    )
    threads = {t.id: t for t in (early_thread, late_thread)}

    tagged = [
        _make_tagged(
            comment_id="ce_1",
            thread_id="t3_early",
            themes=[ThemeAssignment(theme="early_theme", sentiment="negative")],
        ),
        _make_tagged(
            comment_id="cl_1",
            thread_id="t3_late",
            themes=[ThemeAssignment(theme="late_theme", sentiment="negative")],
        ),
    ]
    agg = build_aggregate(tagged, threads, voice_published=[])
    rows_by_theme = {r.theme: r for r in agg.rows}

    # Both buckets: thread_count=1, comment_count=1. Late wins on recency.
    assert rows_by_theme["late_theme"].rank == 1
    assert rows_by_theme["early_theme"].rank == 2


# =========================================================================
# Multi-theme fan-out per spec D1
# =========================================================================


def test_multi_theme_fan_out() -> None:
    """A comment with two ThemeAssignment entries contributes to two buckets.

    Per spec D1: *"the videos are great but the fee is outrageous"* →
    one record in (positive, video_format) AND one in (negative,
    author_fees). Confirms the bucketization fan-out.
    """

    thread = _make_thread(
        thread_id="t3_mt",
        comments=[
            _make_comment(comment_id="cmt_1", parent_id="t3_mt", score=20)
        ],
    )
    threads = {thread.id: thread}

    tagged = [
        _make_tagged(
            comment_id="cmt_1",
            thread_id="t3_mt",
            themes=[
                ThemeAssignment(theme="video_format", sentiment="positive"),
                ThemeAssignment(theme="author_fees", sentiment="negative"),
            ],
            sentiment="mixed",
        )
    ]
    agg = build_aggregate(tagged, threads, voice_published=[])

    # Two buckets, one per ThemeAssignment.
    assert len(agg.rows) == 2

    rows_by_key = {(r.sentiment, r.theme): r for r in agg.rows}
    assert ("positive", "video_format") in rows_by_key
    assert ("negative", "author_fees") in rows_by_key

    # Each bucket: 1 thread, 1 comment (the same comment).
    for r in agg.rows:
        assert r.thread_count == 1
        assert r.comment_count == 1
        assert r.upvote_sum == 20  # full comment score in both buckets


# =========================================================================
# Time horizon filter per spec D7
# =========================================================================


def test_pre_2010_thread_excluded() -> None:
    """A pre-2010 thread is excluded from the aggregate per spec D7."""

    old_thread = _make_thread(
        thread_id="t3_old",
        created_utc=datetime(2009, 6, 1, tzinfo=timezone.utc),
        comments=[
            _make_comment(
                comment_id="cold_1",
                parent_id="t3_old",
                created_utc=datetime(2009, 6, 5, tzinfo=timezone.utc),
            )
        ],
    )
    new_thread = _make_thread(
        thread_id="t3_new",
        created_utc=datetime(2025, 6, 1, tzinfo=timezone.utc),
        comments=[
            _make_comment(
                comment_id="cnew_1",
                parent_id="t3_new",
                created_utc=datetime(2025, 6, 5, tzinfo=timezone.utc),
            )
        ],
    )
    threads = {t.id: t for t in (old_thread, new_thread)}

    tagged = [
        _make_tagged(
            comment_id="cold_1",
            thread_id="t3_old",
            themes=[ThemeAssignment(theme="old_theme", sentiment="negative")],
        ),
        _make_tagged(
            comment_id="cnew_1",
            thread_id="t3_new",
            themes=[ThemeAssignment(theme="new_theme", sentiment="negative")],
        ),
    ]
    agg = build_aggregate(tagged, threads, voice_published=[])

    # Old theme's only supporter is pre-2010 → bucket vanishes.
    themes_present = {r.theme for r in agg.rows}
    assert "new_theme" in themes_present
    assert "old_theme" not in themes_present

    # Header counts reflect only the in-horizon contributors.
    assert agg.total_threads == 1
    assert agg.total_comments == 1


def test_mixed_era_supporters_filter_to_post_2010_only() -> None:
    """A bucket with some pre-2010 + some post-2010 supporters keeps only post-2010."""

    old_thread = _make_thread(
        thread_id="t3_old",
        created_utc=datetime(2008, 1, 1, tzinfo=timezone.utc),
        comments=[
            _make_comment(
                comment_id="cold_1",
                parent_id="t3_old",
                created_utc=datetime(2008, 2, 1, tzinfo=timezone.utc),
                score=999,
            )
        ],
    )
    new_thread = _make_thread(
        thread_id="t3_new",
        created_utc=datetime(2025, 1, 1, tzinfo=timezone.utc),
        comments=[
            _make_comment(
                comment_id="cnew_1",
                parent_id="t3_new",
                created_utc=datetime(2025, 2, 1, tzinfo=timezone.utc),
                score=10,
            )
        ],
    )
    threads = {t.id: t for t in (old_thread, new_thread)}

    tagged = [
        _make_tagged(
            comment_id="cold_1",
            thread_id="t3_old",
            themes=[ThemeAssignment(theme="shared", sentiment="negative")],
        ),
        _make_tagged(
            comment_id="cnew_1",
            thread_id="t3_new",
            themes=[ThemeAssignment(theme="shared", sentiment="negative")],
        ),
    ]
    agg = build_aggregate(tagged, threads, voice_published=[])

    assert len(agg.rows) == 1
    row = agg.rows[0]
    assert row.theme == "shared"
    assert row.thread_count == 1  # only the post-2010 thread
    assert row.comment_count == 1
    # Critically: the pre-2010 comment's score (999) is NOT in the sum.
    assert row.upvote_sum == 10


# =========================================================================
# Empty voice panel (graceful)
# =========================================================================


def test_empty_voice_published_when_dir_missing(tmp_path: Path) -> None:
    """Pointing at a nonexistent dir returns ``[]``, no error."""

    nonexistent = tmp_path / "does_not_exist"
    assert not nonexistent.exists()

    rows = parse_voice_published_dir(nonexistent)
    assert rows == []


def test_empty_voice_published_when_dir_empty(tmp_path: Path) -> None:
    """An empty Published/ dir produces ``[]``."""

    empty_dir = tmp_path / "Published"
    empty_dir.mkdir()
    assert parse_voice_published_dir(empty_dir) == []


def test_build_aggregate_falls_back_to_empty_voice_when_dir_missing(
    tmp_path: Path,
) -> None:
    """build_aggregate doesn't crash when ``Published/`` is missing."""

    nonexistent = tmp_path / "missing"
    thread = _make_thread(
        thread_id="t3_a",
        comments=[_make_comment(comment_id="ca", parent_id="t3_a")],
    )
    tagged = [
        _make_tagged(
            comment_id="ca",
            thread_id="t3_a",
            themes=[ThemeAssignment(theme="t1", sentiment="negative")],
        )
    ]
    agg = build_aggregate(
        tagged, {thread.id: thread}, voice_published_dir=nonexistent
    )
    assert agg.voice_published == []


# =========================================================================
# Voice panel parsing
# =========================================================================


_SYNTHETIC_PUBLISHED_FILE = """\
---
type: note
status: active
domain: [research]
tags: [type/note, status/active, jove, research, voice-of-academia, voice-published]
created: 2026-05-19
updated: 2026-05-19
source_urls: []
confidence: inferred
aliases: []
---

# Voice Published — Week 2026-W22

> [!info] **D's curated voice quotes for week 2026-W22.**

## JoVE · NEGATIVE · aggressive_marketing:negative
**Author**: u/anonPostdoc_42
**Thread**: [Special issue invitation form JoVE](https://www.reddit.com/r/AskAcademia/comments/1nkbbm3/special_issue_invitation_form_jove_they_cant_be/)
**Comment**: [link](https://www.reddit.com/r/AskAcademia/comments/1nkbbm3/special_issue_invitation_form_jove_they_cant_be/k7a3xyz/)
**Date**: 2026-04-12
**Score**: 412
**Voice rubric**: First-person specific anecdote — matches YES-1 pattern

> JoVE fascinates me in that they have historically published really useful material,
> but have dogshit editorial and marketing practices.

**Curator note**: Strongest single anecdote in the corpus.

---

## JoVE · POSITIVE · reproducibility_value:positive
**Author**: u/drosophila_pi
**Thread**: [JoVE protocols](https://www.reddit.com/r/labrats/comments/9hzpzh/jove_protocols/)
**Comment**: [link](https://www.reddit.com/r/labrats/comments/9hzpzh/jove_protocols/e6gqx9p/)
**Date**: 2026-03-18
**Score**: 247
**Voice rubric**: Specific niche-protocol benefit

> For protocols requiring surgeries or otherwise complex procedures,
> having a video guide is absolutely invaluable.

---
"""


def test_parse_voice_published_file_extracts_two_rows(tmp_path: Path) -> None:
    """A synthetic file with 2 voice cards parses into 2 VoicePanelRows.

    Verifies all fields are populated correctly and one row carries an
    optional curator_note while the other does not.
    """

    f = tmp_path / "2026-W22.md"
    f.write_text(_SYNTHETIC_PUBLISHED_FILE, encoding="utf-8")

    rows = parse_voice_published_file(f)
    assert len(rows) == 2

    by_author = {r.author: r for r in rows}

    neg = by_author["anonPostdoc_42"]
    assert neg.sentiment == "negative"
    assert neg.theme == "aggressive_marketing"
    assert neg.product_attribution == "journal"
    assert neg.score == 412
    assert neg.permalink == (
        "https://www.reddit.com/r/AskAcademia/comments/"
        "1nkbbm3/special_issue_invitation_form_jove_they_cant_be/k7a3xyz/"
    )
    assert neg.created_utc == datetime(2026, 4, 12, tzinfo=timezone.utc)
    assert "JoVE fascinates me" in neg.body
    assert "dogshit editorial" in neg.body
    assert neg.curator_note is not None
    assert "Strongest single anecdote" in neg.curator_note
    # curated_at parsed from frontmatter ``updated:`` line.
    assert neg.curated_at == datetime(2026, 5, 19, tzinfo=timezone.utc)

    pos = by_author["drosophila_pi"]
    assert pos.sentiment == "positive"
    assert pos.theme == "reproducibility_value"
    assert pos.score == 247
    assert pos.curator_note is None  # no curator note in this block


def test_parse_voice_published_dir_loads_multiple_files(tmp_path: Path) -> None:
    """Multiple Published/*.md files all parse into the flat row list."""

    pub_dir = tmp_path / "Published"
    pub_dir.mkdir()

    # File 1: the full synthetic file (2 rows).
    (pub_dir / "2026-W22.md").write_text(_SYNTHETIC_PUBLISHED_FILE, encoding="utf-8")
    # File 2: a smaller file with one row.
    (pub_dir / "2026-W23.md").write_text(
        """\
---
updated: 2026-05-26
---

# Voice Published — Week 2026-W23

## JoVE · NEUTRAL · legitimacy:neutral
**Author**: u/fluffy_antelope_3395
**Thread**: [Title](https://www.reddit.com/r/AskAcademia/comments/1nkbbm3/title/)
**Comment**: [link](https://www.reddit.com/r/AskAcademia/comments/1nkbbm3/title/k7c9pwm/)
**Date**: 2026-04-04
**Score**: 178
**Voice rubric**: Historical-frame critique

> It was a different landscape when the journal started nearly 20 years ago.

---
""",
        encoding="utf-8",
    )

    rows = parse_voice_published_dir(pub_dir)
    assert len(rows) == 3

    sentiments = sorted({r.sentiment for r in rows})
    assert sentiments == ["negative", "neutral", "positive"]


def test_parse_voice_block_handles_mixed_sentiment(tmp_path: Path) -> None:
    """A MIXED-sentiment block parses to ``Sentiment="mixed"``.

    This is the load-bearing test for the Phase 7 decision to widen
    ``VoicePanelRow.sentiment`` to 4-value (Option A).
    """

    f = tmp_path / "2026-W22.md"
    f.write_text(
        """\
---
updated: 2026-05-19
---

# Voice Published

## JoVE · MIXED · video_format:positive, author_fees:negative
**Author**: u/some_user
**Thread**: [Title](https://www.reddit.com/r/labrats/comments/abc/_/)
**Comment**: [link](https://www.reddit.com/r/labrats/comments/abc/_/cmt_id/)
**Date**: 2026-04-15
**Score**: 88
**Voice rubric**: Genuinely mixed; preserves both polarities

> The videos are great but the fees are outrageous.

---
""",
        encoding="utf-8",
    )

    rows = parse_voice_published_file(f)
    assert len(rows) == 1
    row = rows[0]
    assert row.sentiment == "mixed"  # The whole point of Option A.
    # First-listed theme wins for the single ``theme`` field.
    assert row.theme == "video_format"


# =========================================================================
# Out-of-scope filter
# =========================================================================


def test_out_of_scope_comments_excluded() -> None:
    """``product_attribution == "out-of-scope"`` is dropped pre-bucketing."""

    thread = _make_thread(
        thread_id="t3_oos",
        comments=[
            _make_comment(comment_id="cmt_oos", parent_id="t3_oos"),
            _make_comment(comment_id="cmt_in", parent_id="t3_oos"),
        ],
    )
    threads = {thread.id: thread}

    tagged = [
        _make_tagged(
            comment_id="cmt_oos",
            thread_id="t3_oos",
            themes=[ThemeAssignment(theme="t_oos", sentiment="negative")],
            product_attribution="out-of-scope",
            journal_named=None,
        ),
        _make_tagged(
            comment_id="cmt_in",
            thread_id="t3_oos",
            themes=[ThemeAssignment(theme="t_in", sentiment="negative")],
        ),
    ]
    agg = build_aggregate(tagged, threads, voice_published=[])
    themes_present = {r.theme for r in agg.rows}
    assert themes_present == {"t_in"}
    # The OOS comment didn't even count toward total_comments.
    assert agg.total_comments == 1


# =========================================================================
# Aggregate header + serializable
# =========================================================================


def test_aggregate_serializes_with_model_dump_json_compatible_types() -> None:
    """Aggregate.model_dump(mode='json') yields strings for datetimes.

    Critical for the dashboard contract — JSON ISO strings, not Python
    datetime objects.
    """

    thread = _make_thread(
        thread_id="t3_a",
        comments=[_make_comment(comment_id="c_a", parent_id="t3_a")],
    )
    tagged = [
        _make_tagged(
            comment_id="c_a",
            thread_id="t3_a",
            themes=[ThemeAssignment(theme="t1", sentiment="negative")],
        )
    ]
    agg = build_aggregate(
        tagged,
        {thread.id: thread},
        voice_published=[],
        now=datetime(2026, 5, 23, 18, 0, tzinfo=timezone.utc),
    )

    dump = agg.model_dump(mode="json")
    assert isinstance(dump["generated_at"], str)
    assert dump["generated_at"].startswith("2026-05-23T")
    assert isinstance(dump["rows"], list)
    assert dump["time_horizon"] == DEFAULT_TIME_HORIZON_STR

    # Row datetimes are ISO strings.
    row = dump["rows"][0]
    assert isinstance(row["last_comment_at"], str)
    assert isinstance(row["newest_thread_at"], str)


def test_aggregate_returns_aggregate_instance() -> None:
    """Smoke test: build_aggregate returns an :class:`Aggregate`."""

    agg = build_aggregate([], {}, voice_published=[])
    assert isinstance(agg, Aggregate)
    assert agg.rows == []
    assert agg.total_threads == 0
    assert agg.total_comments == 0


# =========================================================================
# Direct-reply counting
# =========================================================================


def test_reply_count_aggregates_across_supporting_comments() -> None:
    """A bucket's reply_count = sum of each supporting comment's direct replies."""

    # Thread layout:
    # t3_one:
    #   - cmt_parent (tagged, theme=A)
    #     - cmt_reply1 (untagged or different theme)
    #     - cmt_reply2 (untagged or different theme)
    #   - cmt_other (tagged, theme=A)
    #     - cmt_other_reply (untagged or different theme)
    #
    # Bucket A: supporters = [cmt_parent, cmt_other]
    # cmt_parent has 2 direct replies, cmt_other has 1.
    # Expected reply_count = 3.

    comments = [
        _make_comment(comment_id="cmt_parent", parent_id="t3_one"),
        _make_comment(comment_id="cmt_reply1", parent_id="cmt_parent"),
        _make_comment(comment_id="cmt_reply2", parent_id="cmt_parent"),
        _make_comment(comment_id="cmt_other", parent_id="t3_one"),
        _make_comment(comment_id="cmt_other_reply", parent_id="cmt_other"),
    ]
    thread = _make_thread(thread_id="t3_one", comments=comments)
    threads = {thread.id: thread}

    tagged = [
        _make_tagged(
            comment_id="cmt_parent",
            thread_id="t3_one",
            themes=[ThemeAssignment(theme="A", sentiment="negative")],
        ),
        _make_tagged(
            comment_id="cmt_other",
            thread_id="t3_one",
            themes=[ThemeAssignment(theme="A", sentiment="negative")],
        ),
    ]
    agg = build_aggregate(tagged, threads, voice_published=[])
    assert len(agg.rows) == 1
    row = agg.rows[0]
    assert row.reply_count == 3


# =========================================================================
# Thread URLs deduplicated and sorted
# =========================================================================


def test_thread_urls_deduplicated_and_sorted() -> None:
    """Two comments in the same thread → one URL; URLs sorted lexicographically."""

    # Two threads, each with 2 tagged comments → 2 URLs in the bucket,
    # not 4. URLs are sorted ascending.
    t1 = _make_thread(
        thread_id="t3_b",  # permalink contains "b"
        permalink="https://www.reddit.com/r/AskAcademia/comments/bb/b_slug/",
        comments=[
            _make_comment(comment_id="cb_1", parent_id="t3_b"),
            _make_comment(comment_id="cb_2", parent_id="t3_b"),
        ],
    )
    t2 = _make_thread(
        thread_id="t3_a",
        permalink="https://www.reddit.com/r/AskAcademia/comments/aa/a_slug/",
        comments=[
            _make_comment(comment_id="ca_1", parent_id="t3_a"),
            _make_comment(comment_id="ca_2", parent_id="t3_a"),
        ],
    )
    threads = {t.id: t for t in (t1, t2)}

    tagged: list[TaggedComment] = []
    for thread in (t1, t2):
        for c in thread.comments:
            tagged.append(
                _make_tagged(
                    comment_id=c.id,
                    thread_id=thread.id,
                    themes=[ThemeAssignment(theme="x", sentiment="negative")],
                )
            )
    agg = build_aggregate(tagged, threads, voice_published=[])
    assert len(agg.rows) == 1
    urls = agg.rows[0].thread_urls
    assert len(urls) == 2  # deduplicated
    # Sorted ascending: aa/ < bb/.
    assert urls[0].endswith("/aa/a_slug/")
    assert urls[1].endswith("/bb/b_slug/")


# =========================================================================
# Type contract sanity: FrequencyRow matches expected fields
# =========================================================================


def test_frequency_row_field_set() -> None:
    """FrequencyRow exposes exactly the fields the dashboard's TypeScript expects.

    Drift here = broken dashboard. Mirror dashboard/lib/types.ts.
    """

    expected = {
        "sentiment",
        "theme",
        "journal",
        "thread_count",
        "comment_count",
        "upvote_sum",
        "reply_count",
        "last_comment_at",
        "newest_thread_at",
        "thread_urls",
        "rank",
    }
    assert set(FrequencyRow.model_fields) == expected


def test_voice_panel_row_field_set() -> None:
    """VoicePanelRow exposes exactly the fields the dashboard's TypeScript expects."""

    expected = {
        "sentiment",
        "theme",
        "product_attribution",
        "author",
        "permalink",
        "body",
        "score",
        "created_utc",
        "curated_at",
        "curator_note",
    }
    assert set(VoicePanelRow.model_fields) == expected


def test_aggregate_field_set() -> None:
    """Aggregate exposes exactly the fields the dashboard's TypeScript expects."""

    expected = {
        "generated_at",
        "time_horizon",
        "total_threads",
        "total_comments",
        "rows",
        "voice_published",
    }
    assert set(Aggregate.model_fields) == expected


def test_block_start_regex_handles_multi_word_journal(tmp_path: Path) -> None:
    """A Voice block whose journal header has a space (e.g. ``Unknown journal``)
    must parse — not silently disappear from the panel.

    Regression guard for the v1-cleanup fix at ``aggregate.py:394``:
    ``_BLOCK_START_RE`` previously used ``\\S+`` for the journal segment,
    which only matched non-whitespace runs. When ``TaggedComment.journal_named``
    is ``None``, ``pipeline.store`` writes ``"Unknown journal"`` as the
    fallback — a two-word value the old regex could not match. Loosened
    to ``\\S.*?`` to mirror ``voice_curate.py``'s convention.
    """

    pub_dir = tmp_path / "Published"
    pub_dir.mkdir()
    (pub_dir / "2026-W22.md").write_text(
        """\
---
updated: 2026-05-19
---

# Voice Published

## Unknown journal · POSITIVE · video_format:positive
**Author**: u/some_user
**Thread**: [Title](https://www.reddit.com/r/labrats/comments/abc/_/)
**Comment**: [link](https://www.reddit.com/r/labrats/comments/abc/_/cmt_id/)
**Date**: 2026-04-15
**Score**: 55
**Voice rubric**: Specific niche-protocol benefit

> The videos are genuinely useful when you can't find a written protocol.

---
""",
        encoding="utf-8",
    )

    rows = parse_voice_published_dir(pub_dir)
    assert len(rows) == 1, (
        f"Expected one row from multi-word journal header; got {len(rows)}"
    )
    row = rows[0]
    assert row.sentiment == "positive"
    assert row.theme == "video_format"
    # Journal "Unknown journal" is not the default ``JoVE`` → out-of-scope
    # per the v1 product_attribution clamp.
    assert row.product_attribution == "out-of-scope"
    assert row.author == "some_user"
