"""Unit tests for :mod:`pipeline.store` (Phase 6 — storage layer).

Mocks the Pinecone Index so the bulk of the suite runs offline and
zero-cost. One ``@pytest.mark.integration`` test at the bottom hits a
live Pinecone namespace and is skipped by default (run with
``pytest -m integration``).

Each unit test verifies one contract:

* the Pinecone record schema (field names, types, flat-lists, sentinels)
* the ``sentiment=None`` -> ``"unknown"`` substitution
* the ``journal_named=None`` -> ``""`` substitution
* long-body truncation at 4000 chars
* batch splitting at 100 records per ``upsert_records`` call
* first-write creates the candidates file with frontmatter
* subsequent writes append new blocks
* zero qualifying candidates leaves the file untouched
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from pipeline.models import (
    SENTIMENT_FALLBACK_STR,
    Comment,
    TaggedComment,
    ThemeAssignment,
    Thread,
)
from pipeline.store import (
    PINECONE_BATCH_SIZE,
    PINECONE_BODY_MAX_CHARS,
    upsert_thread,
    write_aggregate_json,
    write_voice_candidates,
)


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def sample_thread() -> Thread:
    """A representative JoVE-scope thread with one canonical comment."""

    return Thread(
        id="t3_seedthread",
        permalink="https://www.reddit.com/r/labrats/comments/seedthread/is_jove_respectable/",
        subreddit="labrats",
        title="Is JoVE respectable?",
        body="Curious whether postdocs view JoVE seriously.",
        author="op_user",
        created_utc=datetime(2025, 6, 1, tzinfo=timezone.utc),
        score=42,
        num_comments=2,
        is_self=True,
        comments=[],
    )


def _make_comment(
    *,
    comment_id: str = "t1_c01",
    parent_id: str = "t3_seedthread",
    body: str = "JoVE is fine for technique-heavy work but the fees sting.",
    author: str | None = "some_user",
    score: int = 12,
    depth: int = 0,
    permalink: str | None = None,
) -> Comment:
    return Comment(
        id=comment_id,
        parent_id=parent_id,
        permalink=permalink
        or f"https://www.reddit.com/r/labrats/comments/seedthread/_/{comment_id}/",
        body=body,
        author=author,
        created_utc=datetime(2025, 6, 2, tzinfo=timezone.utc),
        score=score,
        depth=depth,
    )


def _make_tagged(
    *,
    comment_id: str = "t1_c01",
    thread_id: str | None = "t3_seedthread",
    sentiment: str | None = "negative",
    themes: list[ThemeAssignment] | None = None,
    journal_named: str | None = "JoVE",
    voice_candidate: bool = False,
    voice_candidate_reason: str | None = None,
    product_attribution: str = "journal",
) -> TaggedComment:
    return TaggedComment(
        comment_id=comment_id,
        thread_id=thread_id,
        permalink=f"https://www.reddit.com/r/labrats/comments/seedthread/_/{comment_id}/",
        sentiment=sentiment,  # type: ignore[arg-type]
        themes=themes
        or [ThemeAssignment(theme="author_fees", sentiment="negative")],
        product_attribution=product_attribution,  # type: ignore[arg-type]
        journal_named=journal_named,
        voice_candidate=voice_candidate,
        voice_candidate_reason=voice_candidate_reason,
        tagged_at=datetime(2025, 6, 3, tzinfo=timezone.utc),
        tagged_by_model="claude-haiku-4-5-20251001+claude-sonnet-4-6",
    )


class FakeIndex:
    """Stand-in for ``pinecone.db_data._Index``.

    Records each ``upsert_records`` call so tests can assert on the
    payload — both contents and call-count for batching tests.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def upsert_records(
        self, *, namespace: str, records: list[dict[str, Any]]
    ) -> object:
        # Defensive-copy the records list so subsequent batches don't
        # accidentally observe shared mutable references.
        self.calls.append({"namespace": namespace, "records": list(records)})
        # Return an object shaped vaguely like the SDK's UpsertRecordsResponse —
        # nothing in production code reads it so a sentinel is fine.
        return {"record_count": len(records)}


# =========================================================================
# upsert_thread — happy path schema check
# =========================================================================


def test_upsert_thread_record_schema(sample_thread: Thread) -> None:
    """Verify thread + comment record fields exactly match the spec.

    Confirms:
    * thread record has ``_id``, ``type="thread"``, ``text`` field
    * comment record has the Phase 5 tagging fields flattened to
      scalars/lists
    * themes is ``list[str]`` (not list-of-dict)
    * themes_with_sentiment is ``list["theme:sentiment"]``
    """

    comment = _make_comment(comment_id="t1_c01")
    sample_thread.comments = [comment]
    tagged = _make_tagged(
        comment_id="t1_c01",
        themes=[
            ThemeAssignment(theme="video_format", sentiment="positive"),
            ThemeAssignment(theme="author_fees", sentiment="negative"),
        ],
        sentiment="mixed",
    )

    fake = FakeIndex()
    upsert_thread(sample_thread, [tagged], index=fake)

    # One call, two records (1 thread + 1 comment).
    assert len(fake.calls) == 1
    assert fake.calls[0]["namespace"] == "social-listening"
    recs = fake.calls[0]["records"]
    assert len(recs) == 2

    thread_rec, comment_rec = recs
    # Thread record
    assert thread_rec["_id"] == "t3_seedthread"
    assert thread_rec["type"] == "thread"
    assert thread_rec["subreddit"] == "labrats"
    assert thread_rec["author"] == "op_user"
    assert thread_rec["permalink"].startswith("https://www.reddit.com/")
    assert "text" in thread_rec  # embedded field per index fieldMap
    assert thread_rec["text"].startswith("Is JoVE respectable?")
    assert thread_rec["score"] == 42
    assert thread_rec["reply_count"] == 2

    # Comment record
    assert comment_rec["_id"] == "t1_c01"
    assert comment_rec["type"] == "comment"
    assert comment_rec["subreddit"] == "labrats"
    assert comment_rec["author"] == "some_user"
    assert comment_rec["parent_id"] == "t3_seedthread"
    assert comment_rec["parent_thread_id"] == "t3_seedthread"
    assert "text" in comment_rec
    assert comment_rec["text"] == comment.body
    assert comment_rec["score"] == 12
    assert comment_rec["sentiment"] == "mixed"

    # Themes must be flat list-of-strings (Pinecone metadata constraint).
    assert comment_rec["themes"] == ["video_format", "author_fees"]
    assert all(isinstance(t, str) for t in comment_rec["themes"])
    assert comment_rec["themes_with_sentiment"] == [
        "video_format:positive",
        "author_fees:negative",
    ]
    assert comment_rec["product_attribution"] == "journal"
    assert comment_rec["journal_named"] == "JoVE"
    assert comment_rec["voice_candidate"] is False
    assert comment_rec["voice_candidate_reason"] == ""
    assert comment_rec["tagged_by_model"].startswith("claude-haiku")


# =========================================================================
# Sentiment None → "unknown"
# =========================================================================


def test_sentiment_none_substitutes_fallback(sample_thread: Thread) -> None:
    """``sentiment=None`` (classification-failure fallback) -> ``"unknown"``.

    Pinecone metadata cannot hold ``None``; the dashboard depends on
    every record having a string sentiment for bucket filtering.
    """

    comment = _make_comment(comment_id="t1_c01")
    sample_thread.comments = [comment]
    tagged = _make_tagged(comment_id="t1_c01", sentiment=None)

    fake = FakeIndex()
    upsert_thread(sample_thread, [tagged], index=fake)

    comment_rec = fake.calls[0]["records"][1]
    assert comment_rec["sentiment"] == SENTIMENT_FALLBACK_STR
    assert comment_rec["sentiment"] == "unknown"
    # Critically: the value is a string, not None.
    assert isinstance(comment_rec["sentiment"], str)


# =========================================================================
# journal_named None → ""
# =========================================================================


def test_journal_named_none_substitutes_empty_string(sample_thread: Thread) -> None:
    """``journal_named=None`` is converted to ``""`` for Pinecone.

    Same Pinecone-metadata constraint — None is rejected; we use ``""``
    so the dashboard can ``if journal_named:`` for the truthy check.
    """

    comment = _make_comment(comment_id="t1_c01")
    sample_thread.comments = [comment]
    tagged = _make_tagged(comment_id="t1_c01", journal_named=None)

    fake = FakeIndex()
    upsert_thread(sample_thread, [tagged], index=fake)

    comment_rec = fake.calls[0]["records"][1]
    assert comment_rec["journal_named"] == ""
    assert isinstance(comment_rec["journal_named"], str)


# =========================================================================
# Long body truncation
# =========================================================================


def test_long_body_truncated(sample_thread: Thread) -> None:
    """Comment bodies longer than ``PINECONE_BODY_MAX_CHARS`` are cut.

    Pinecone's per-record metadata cap is 40 KB; the embedding model's
    effective context is ~512 tokens. Both reasons demand truncation.
    """

    long_body = "x" * (PINECONE_BODY_MAX_CHARS + 500)
    comment = _make_comment(comment_id="t1_c01", body=long_body)
    sample_thread.comments = [comment]
    tagged = _make_tagged(comment_id="t1_c01")

    fake = FakeIndex()
    upsert_thread(sample_thread, [tagged], index=fake)

    comment_rec = fake.calls[0]["records"][1]
    assert len(comment_rec["text"]) == PINECONE_BODY_MAX_CHARS
    # No ellipsis — strict head-truncate for reproducibility.
    assert comment_rec["text"] == "x" * PINECONE_BODY_MAX_CHARS


def test_long_thread_body_truncated(sample_thread: Thread) -> None:
    """The combined title+body for a thread is also truncated."""

    sample_thread.body = "y" * (PINECONE_BODY_MAX_CHARS + 500)
    sample_thread.comments = []

    fake = FakeIndex()
    upsert_thread(sample_thread, [], index=fake)

    thread_rec = fake.calls[0]["records"][0]
    assert len(thread_rec["text"]) == PINECONE_BODY_MAX_CHARS


# =========================================================================
# Batching: 250 comments → 3 upsert_records calls
# =========================================================================


def test_batching_splits_at_100(sample_thread: Thread) -> None:
    """A thread with 250 tagged comments produces 3 upsert calls.

    Layout: [thread + 99 comments] (100), [100 comments] (100),
    [51 comments] (51). Total: 251 records / 3 calls.
    """

    comments = []
    tags = []
    for i in range(250):
        cid = f"t1_c{i:04d}"
        comments.append(_make_comment(comment_id=cid))
        tags.append(_make_tagged(comment_id=cid))
    sample_thread.comments = comments

    fake = FakeIndex()
    upsert_thread(sample_thread, tags, index=fake)

    # 1 thread + 250 comments = 251 records → 100 + 100 + 51 = 3 calls.
    assert len(fake.calls) == 3
    assert len(fake.calls[0]["records"]) == PINECONE_BATCH_SIZE
    assert len(fake.calls[1]["records"]) == PINECONE_BATCH_SIZE
    assert len(fake.calls[2]["records"]) == 51
    total = sum(len(c["records"]) for c in fake.calls)
    assert total == 251


# =========================================================================
# Comments without a matching tag are skipped
# =========================================================================


def test_untagged_comment_skipped(sample_thread: Thread) -> None:
    """Comments without a matching ``TaggedComment`` are not upserted.

    Schema demands tag fields be present on every comment record, so a
    half-tagged thread (e.g. crash mid-Phase-5) writes only the comments
    that have tags — the rest are picked up on the next pass.
    """

    sample_thread.comments = [
        _make_comment(comment_id="t1_tagged"),
        _make_comment(comment_id="t1_untagged"),
    ]
    tags = [_make_tagged(comment_id="t1_tagged")]

    fake = FakeIndex()
    upsert_thread(sample_thread, tags, index=fake)

    recs = fake.calls[0]["records"]
    assert len(recs) == 2  # thread + 1 tagged comment
    comment_ids = [r["_id"] for r in recs if r["type"] == "comment"]
    assert comment_ids == ["t1_tagged"]


# =========================================================================
# reply_count is computed from the comment tree
# =========================================================================


def test_reply_count_computed(sample_thread: Thread) -> None:
    """``reply_count`` for a comment = #comments whose parent_id is it."""

    sample_thread.comments = [
        _make_comment(comment_id="t1_parent", parent_id="t3_seedthread"),
        _make_comment(comment_id="t1_reply1", parent_id="t1_parent"),
        _make_comment(comment_id="t1_reply2", parent_id="t1_parent"),
        _make_comment(comment_id="t1_nephew", parent_id="t1_reply1"),
    ]
    tags = [
        _make_tagged(comment_id="t1_parent"),
        _make_tagged(comment_id="t1_reply1"),
        _make_tagged(comment_id="t1_reply2"),
        _make_tagged(comment_id="t1_nephew"),
    ]

    fake = FakeIndex()
    upsert_thread(sample_thread, tags, index=fake)

    recs = {r["_id"]: r for r in fake.calls[0]["records"] if r["type"] == "comment"}
    assert recs["t1_parent"]["reply_count"] == 2  # two direct replies
    assert recs["t1_reply1"]["reply_count"] == 1  # one direct reply (nephew)
    assert recs["t1_reply2"]["reply_count"] == 0
    assert recs["t1_nephew"]["reply_count"] == 0


# =========================================================================
# Deleted-author handling
# =========================================================================


def test_deleted_author_serializes_to_string(sample_thread: Thread) -> None:
    """``Comment.author=None`` (deleted) -> ``"[deleted]"`` not ``None``.

    Pinecone metadata can't hold None; we mirror Reddit's display
    convention so the dashboard renders consistently with the live UI.
    """

    sample_thread.author = None
    comment = _make_comment(comment_id="t1_c01", author=None)
    sample_thread.comments = [comment]
    tags = [_make_tagged(comment_id="t1_c01")]

    fake = FakeIndex()
    upsert_thread(sample_thread, tags, index=fake)

    recs = fake.calls[0]["records"]
    assert recs[0]["author"] == "[deleted]"
    assert recs[1]["author"] == "[deleted]"


# =========================================================================
# write_voice_candidates — first-write creates file with frontmatter
# =========================================================================


def test_write_voice_candidates_first_write_creates_frontmatter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sample_thread: Thread
) -> None:
    """First write to a week's file emits frontmatter + heading + block.

    Subsequent writes use append-mode without rewriting frontmatter.
    """

    monkeypatch.setattr("pipeline.store.VAULT_VOICE_CANDIDATES", tmp_path)

    comment = _make_comment(
        comment_id="t1_c01",
        body="Wasted 9 months. Editor never replied. Career timeline hit.",
        score=247,
    )
    sample_thread.comments = [comment]
    tagged = _make_tagged(
        comment_id="t1_c01",
        voice_candidate=True,
        voice_candidate_reason="first-person specific anecdote — matches YES-1 pattern",
    )

    out = write_voice_candidates([tagged], sample_thread, week="2026-W22")
    assert out is not None
    assert out.name == "2026-W22.md"
    assert out.exists()

    content = out.read_text(encoding="utf-8")
    # Frontmatter present
    assert content.startswith("---\n")
    assert "type: note" in content
    assert "status: active" in content
    assert "domain: [research]" in content
    assert "voice-of-academia" in content
    # Heading
    assert "# Voice Candidates — Week 2026-W22" in content
    # Block body
    assert "JoVE · NEGATIVE · author_fees:negative" in content
    assert "u/some_user" in content
    assert "Is JoVE respectable?" in content
    assert "matches YES-1 pattern" in content
    # Blockquote-rendered body
    assert "> Wasted 9 months." in content
    # Separator
    assert "---\n" in content[100:]  # at least one trailing --- after frontmatter


# =========================================================================
# write_voice_candidates — append on existing file
# =========================================================================


def test_write_voice_candidates_appends_to_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sample_thread: Thread
) -> None:
    """Second write to the same week appends without rewriting frontmatter."""

    monkeypatch.setattr("pipeline.store.VAULT_VOICE_CANDIDATES", tmp_path)

    sample_thread.comments = [
        _make_comment(comment_id="t1_c01", body="First candidate body."),
        _make_comment(comment_id="t1_c02", body="Second candidate body."),
    ]
    first_batch = [
        _make_tagged(
            comment_id="t1_c01",
            voice_candidate=True,
            voice_candidate_reason="first batch reason",
        )
    ]
    write_voice_candidates(first_batch, sample_thread, week="2026-W22")

    second_batch = [
        _make_tagged(
            comment_id="t1_c02",
            voice_candidate=True,
            voice_candidate_reason="second batch reason",
        )
    ]
    write_voice_candidates(second_batch, sample_thread, week="2026-W22")

    out = tmp_path / "2026-W22.md"
    content = out.read_text(encoding="utf-8")

    # Exactly one frontmatter block.
    assert content.count("---\ntype: note") == 1
    assert content.count("# Voice Candidates — Week 2026-W22") == 1

    # Both bodies present.
    assert "> First candidate body." in content
    assert "> Second candidate body." in content
    assert "first batch reason" in content
    assert "second batch reason" in content


# =========================================================================
# write_voice_candidates — no qualifying candidates -> no file touch
# =========================================================================


def test_write_voice_candidates_skips_when_no_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sample_thread: Thread
) -> None:
    """If no tags have voice_candidate=True, leave the file untouched."""

    monkeypatch.setattr("pipeline.store.VAULT_VOICE_CANDIDATES", tmp_path)

    sample_thread.comments = [_make_comment(comment_id="t1_c01")]
    tags = [_make_tagged(comment_id="t1_c01", voice_candidate=False)]

    out = write_voice_candidates(tags, sample_thread, week="2026-W22")
    assert out is None

    target = tmp_path / "2026-W22.md"
    assert not target.exists()


def test_write_voice_candidates_skips_with_empty_taglist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sample_thread: Thread
) -> None:
    """Empty tag list is also a no-op (no file written)."""

    monkeypatch.setattr("pipeline.store.VAULT_VOICE_CANDIDATES", tmp_path)
    sample_thread.comments = []

    out = write_voice_candidates([], sample_thread, week="2026-W22")
    assert out is None
    assert not (tmp_path / "2026-W22.md").exists()


# =========================================================================
# write_voice_candidates — current week defaulting
# =========================================================================


def test_write_voice_candidates_defaults_to_current_iso_week(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sample_thread: Thread
) -> None:
    """If ``week`` is omitted, derives ``YYYY-WWW`` from passed ``now``.

    Use ``now`` injection (not real clock) so the test is deterministic.
    """

    monkeypatch.setattr("pipeline.store.VAULT_VOICE_CANDIDATES", tmp_path)

    sample_thread.comments = [_make_comment(comment_id="t1_c01")]
    tags = [
        _make_tagged(
            comment_id="t1_c01",
            voice_candidate=True,
            voice_candidate_reason="r",
        )
    ]
    # 2026-05-25 is a Monday in ISO week 22.
    fixed_now = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    out = write_voice_candidates(tags, sample_thread, now=fixed_now)
    assert out is not None
    assert out.name == "2026-W22.md"


# =========================================================================
# Comment fields & metadata — no None values anywhere
# =========================================================================


def test_no_none_in_pinecone_metadata(sample_thread: Thread) -> None:
    """Every record value must be a non-None scalar/bool/list-of-string.

    Pinecone metadata API rejects None outright; a single leaked None
    field would fail a batch upsert and silently lose the rest.
    """

    sample_thread.body = None  # link-post case
    sample_thread.author = None
    comment = _make_comment(comment_id="t1_c01", author=None)
    sample_thread.comments = [comment]
    tagged = _make_tagged(
        comment_id="t1_c01",
        sentiment=None,
        journal_named=None,
        voice_candidate=False,
        voice_candidate_reason=None,
    )

    fake = FakeIndex()
    upsert_thread(sample_thread, [tagged], index=fake)

    for rec in fake.calls[0]["records"]:
        for key, value in rec.items():
            assert value is not None, f"Field {key!r} is None in {rec.get('_id')}"


# =========================================================================
# write_aggregate_json — sorted keys, indent, idempotent path creation
# =========================================================================


def test_write_aggregate_json_serializes_sorted_indented(tmp_path: Path) -> None:
    """JSON output is pretty-printed with sorted keys for stable diffs."""

    target = tmp_path / "subdir" / "latest.json"  # subdir doesn't exist yet
    agg = {
        "z_last": [1, 2, 3],
        "a_first": "hello",
        "nested": {"b": 2, "a": 1},
    }
    out = write_aggregate_json(agg, target)
    assert out == target
    assert out.exists()

    content = out.read_text(encoding="utf-8")
    # Sorted at top level.
    a_idx = content.find('"a_first"')
    n_idx = content.find('"nested"')
    z_idx = content.find('"z_last"')
    assert -1 < a_idx < n_idx < z_idx
    # Sorted at nested level too.
    assert content.find('"a": 1') < content.find('"b": 2')
    # Pretty-printed (newlines + 2-space indent).
    assert "\n  " in content


def test_write_aggregate_json_unicode_preserved(tmp_path: Path) -> None:
    """Non-ASCII (e.g. en-dash) is written verbatim, not escaped."""

    target = tmp_path / "latest.json"
    write_aggregate_json({"label": "Voice — Academia"}, target)
    assert "Voice — Academia" in target.read_text(encoding="utf-8")


# =========================================================================
# Integration test — live Pinecone (skipped by default)
# =========================================================================


@pytest.mark.integration
def test_live_pinecone_upsert_roundtrip(sample_thread: Thread) -> None:
    """Live Pinecone test against a throwaway namespace.

    Skipped by default; run with ``pytest -m integration``. Wrapped in
    try/except so a missing API key doesn't crash the suite.
    """

    import os

    if not os.environ.get("PINECONE_API_KEY"):
        pytest.skip("PINECONE_API_KEY not set; skipping live integration test.")

    from pinecone import Pinecone

    pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
    idx = pc.Index("jove-memory")

    test_namespace = "social-listening-test"
    sample_thread.id = "t3_storetest"
    comment = _make_comment(
        comment_id="t1_storetest",
        body="Integration test body — JoVE fees discussion.",
    )
    sample_thread.comments = [comment]
    tags = [_make_tagged(comment_id="t1_storetest")]

    try:
        upsert_thread(
            sample_thread, tags, index=idx, namespace=test_namespace
        )
    finally:
        # Clean up: delete the test records (best-effort; ignore errors).
        try:
            idx.delete(
                ids=["t3_storetest", "t1_storetest"], namespace=test_namespace
            )
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
