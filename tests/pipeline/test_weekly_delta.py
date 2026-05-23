"""Unit tests for :mod:`pipeline.weekly_delta` — Phase 10 cron orchestrator.

Strategy mirrors ``tests/scripts/test_mega_backfill.py``: mock the
pipeline modules at their public boundaries so tests run zero-cost and
offline. Tests focus on the delta-specific contracts not already
covered by mega_backfill's tests:

1. **Missing ``last_run.json`` → 7-day lookback** — fallback semantics.
2. **Existing ``last_run.json`` → used as time horizon** — happy path.
3. **``--dry-run`` exits 0 without LLM calls** — operator safety.
4. **Cost gate above ceiling aborts with exit code 2** — budget guard.
5. **End-of-run writes updated ``last_run.json``** — checkpoint advance.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import weekly_delta
from pipeline.models import (
    Comment,
    TaggedComment,
    ThemeAssignment,
    Thread,
    ThreadCandidate,
)


# =========================================================================
# Helpers — minimal factories for the pipeline models
# =========================================================================


def _make_candidate(thread_id_segment: str) -> ThreadCandidate:
    """Build a ThreadCandidate with a parseable Reddit permalink."""

    return ThreadCandidate(
        permalink=(
            f"https://www.reddit.com/r/labrats/comments/"
            f"{thread_id_segment}/test/"
        ),
        subreddit="labrats",
        discovered_via="ring1_subreddit_enum",
        discovered_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
        seed_query=None,
    )


def _make_thread(thread_id_segment: str, n_comments: int = 1) -> Thread:
    """Build a minimal Thread with ``n_comments`` synthetic comments."""

    comments = [
        Comment(
            id=f"t1_{thread_id_segment}_{i}",
            parent_id=f"t3_{thread_id_segment}",
            permalink=(
                f"https://www.reddit.com/r/labrats/comments/"
                f"{thread_id_segment}/_/t1_{thread_id_segment}_{i}/"
            ),
            body=f"JoVE comment {i}",
            author=f"user{i}",
            created_utc=datetime(2025, 6, 1, tzinfo=timezone.utc),
            score=1,
            depth=0,
        )
        for i in range(n_comments)
    ]
    return Thread(
        id=f"t3_{thread_id_segment}",
        permalink=(
            f"https://www.reddit.com/r/labrats/comments/"
            f"{thread_id_segment}/_/"
        ),
        subreddit="labrats",
        title="Is JoVE legit?",
        body=None,
        author="op",
        created_utc=datetime(2025, 6, 1, tzinfo=timezone.utc),
        score=10,
        num_comments=n_comments,
        is_self=True,
        comments=comments,
    )


def _make_tagged_for(comment: Comment, thread_id: str) -> TaggedComment:
    return TaggedComment(
        comment_id=comment.id,
        thread_id=thread_id,
        permalink=comment.permalink,
        sentiment="negative",
        themes=[ThemeAssignment(theme="author_fees", sentiment="negative")],
        product_attribution="journal",
        journal_named="JoVE",
        voice_candidate=False,
        voice_candidate_reason=None,
        tagged_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
        tagged_by_model="claude-haiku-4-5-20251001+claude-sonnet-4-6",
    )


# =========================================================================
# Fixture — redirect every durable path into tmp_path
# =========================================================================


@pytest.fixture
def tmp_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every durable path the script writes into ``tmp_path``.

    Identical posture to ``tests/scripts/test_mega_backfill.py`` — the
    module imports the JSONL constants from ``scripts.mega_backfill``,
    so we monkeypatch BOTH the source module *and* the re-export
    location to be safe against future imports.
    """

    last_run = tmp_path / "last_run.json"
    threads_jsonl = tmp_path / "threads.jsonl"
    tagged_jsonl = tmp_path / "tagged-comments.jsonl"
    cost_log = tmp_path / "tag-cost-log.jsonl"
    aggregate_out = tmp_path / "dashboard" / "data" / "latest.json"

    # weekly_delta-owned constants
    monkeypatch.setattr(weekly_delta, "LAST_RUN_PATH", last_run)
    # Re-exported from mega_backfill — patch BOTH locations so any future
    # accessor (direct vs via the re-export) gets the temp paths.
    monkeypatch.setattr(weekly_delta, "THREADS_PATH", threads_jsonl)
    monkeypatch.setattr(weekly_delta, "TAGGED_COMMENTS_PATH", tagged_jsonl)
    monkeypatch.setattr(weekly_delta, "COST_LOG_PATH", cost_log)
    monkeypatch.setattr(weekly_delta, "AGGREGATE_OUTPUT_PATH", aggregate_out)

    # Also patch the source-module constants — the JSONL helpers resolve
    # their default path at call time from the source module's globals.
    from scripts import mega_backfill

    monkeypatch.setattr(mega_backfill, "THREADS_PATH", threads_jsonl)
    monkeypatch.setattr(mega_backfill, "TAGGED_COMMENTS_PATH", tagged_jsonl)
    monkeypatch.setattr(mega_backfill, "COST_LOG_PATH", cost_log)
    monkeypatch.setattr(mega_backfill, "AGGREGATE_OUTPUT_PATH", aggregate_out)

    return tmp_path


def _fake_forecast(grand_total_usd: float) -> dict:
    """Build a forecast dict matching ``estimate_mega_backfill_cost`` shape."""

    return {
        "num_threads": 1,
        "avg_comments_per_thread": 15,
        "total_comments": 15,
        "per_call_assumptions": {},
        "models_and_pricing_usd_per_mtok": {},
        "totals": {
            "classify_total_input_tokens": 0,
            "classify_total_output_tokens": 0,
            "classify_total_usd": 0.0,
            "voice_total_input_tokens": 0,
            "voice_total_output_tokens": 0,
            "voice_total_usd": 0.0,
            "grand_total_usd": grand_total_usd,
        },
        "uncertainty_note": "fake forecast for tests",
    }


# =========================================================================
# 1. Missing last_run.json → 7-day fallback
# =========================================================================


def test_missing_last_run_uses_seven_day_fallback(
    tmp_work: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``last_run.json`` on disk → discovery is invoked with ``now - 7d``.

    Captures the ``time_horizon`` kwarg ``discover_all`` receives, asserts
    it's within a small window of ``now - 7 days``. Uses dry-run mode so
    no fetch/tag calls fire.
    """

    captured: dict = {}

    def _fake_discover_all(**kwargs):
        captured["time_horizon"] = kwargs.get("time_horizon")
        return iter([])

    monkeypatch.setattr(weekly_delta.discover, "discover_all", _fake_discover_all)

    args = weekly_delta.parse_args(["--dry-run"])
    code = weekly_delta.run(args)

    assert code == 0
    assert "time_horizon" in captured, "discover_all should have been called"
    horizon = captured["time_horizon"]
    assert isinstance(horizon, datetime), (
        f"Expected datetime, got {type(horizon).__name__}"
    )
    # Within 5 minutes of now - 7d
    expected = datetime.now(tz=timezone.utc) - timedelta(
        days=weekly_delta.LAST_RUN_FALLBACK_DAYS
    )
    delta = abs((horizon - expected).total_seconds())
    assert delta < 300, (
        f"Expected horizon within 5min of {expected}, got {horizon} "
        f"(delta={delta}s)"
    )


# =========================================================================
# 2. Existing last_run.json → used as time horizon
# =========================================================================


def test_existing_last_run_used_as_time_horizon(
    tmp_work: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-existing ``last_run.json`` → that timestamp is the discovery horizon.

    Writes a ``last_run.json`` with a known timestamp from 14 days ago,
    runs --dry-run, asserts discovery was invoked with that exact
    timestamp (not the 7-day fallback).
    """

    known_anchor = datetime(
        2026, 5, 9, 23, 0, 0, tzinfo=timezone.utc
    )  # known fixed time
    weekly_delta.save_last_run(
        last_run_at=known_anchor,
        threads_processed=42,
        comments_tagged=189,
        cost_usd=12.34,
    )

    captured: dict = {}

    def _fake_discover_all(**kwargs):
        captured["time_horizon"] = kwargs.get("time_horizon")
        return iter([])

    monkeypatch.setattr(weekly_delta.discover, "discover_all", _fake_discover_all)

    args = weekly_delta.parse_args(["--dry-run"])
    code = weekly_delta.run(args)

    assert code == 0
    assert captured["time_horizon"] == known_anchor, (
        f"Expected horizon == {known_anchor}; got {captured.get('time_horizon')}"
    )


# =========================================================================
# 3. --dry-run prints summary, exits 0, no fetch/tag
# =========================================================================


def test_dry_run_does_not_fetch_or_tag(
    tmp_work: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--dry-run`` shows summary, doesn't fetch or tag.

    Mocks discovery to return 5 candidates. Asserts:
    * exit code 0
    * fetch_thread never called
    * tag_comment never called
    * the dry-run banner appears in stdout
    """

    candidates = [_make_candidate(f"dry{i}") for i in range(5)]
    monkeypatch.setattr(
        weekly_delta.discover, "discover_all", lambda **_kw: iter(candidates)
    )

    fetched: list = []
    tagged: list = []

    monkeypatch.setattr(
        weekly_delta.fetch,
        "fetch_thread",
        lambda c: (fetched.append(c), _make_thread("x"))[-1],
    )
    monkeypatch.setattr(
        weekly_delta.tag,
        "tag_comment",
        lambda comment, thread: (tagged.append(comment), _make_tagged_for(
            comment, thread.id
        ))[-1],
    )
    monkeypatch.setattr(
        weekly_delta.tag,
        "estimate_mega_backfill_cost",
        lambda **_kw: _fake_forecast(0.5),
    )

    args = weekly_delta.parse_args(["--dry-run"])
    code = weekly_delta.run(args)

    assert code == 0
    assert fetched == [], "fetch_thread must not be called in --dry-run"
    assert tagged == [], "tag_comment must not be called in --dry-run"

    out = capsys.readouterr().out
    assert "Weekly delta dry-run" in out
    assert "Forecast LLM cost" in out


# =========================================================================
# 4. Cost gate above ceiling aborts with exit code 2
# =========================================================================


def test_cost_gate_aborts_above_ceiling(
    tmp_work: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Forecast > ``--max-cost-usd`` → exit 2, no fetch or tag calls.

    The fake forecast returns $50; we pass ``--max-cost-usd 10`` so the
    gate must abort. Asserts non-zero exit, no fetch/tag side effects,
    and the ABORT banner reaches stderr/stdout.
    """

    candidates = [_make_candidate(f"gate{i}") for i in range(3)]
    monkeypatch.setattr(
        weekly_delta.discover, "discover_all", lambda **_kw: iter(candidates)
    )

    fetched: list = []
    tagged: list = []

    monkeypatch.setattr(
        weekly_delta.fetch,
        "fetch_thread",
        lambda c: (fetched.append(c), _make_thread("x"))[-1],
    )
    monkeypatch.setattr(
        weekly_delta.tag,
        "tag_comment",
        lambda comment, thread: (tagged.append(comment), _make_tagged_for(
            comment, thread.id
        ))[-1],
    )
    monkeypatch.setattr(
        weekly_delta.tag,
        "estimate_mega_backfill_cost",
        lambda **_kw: _fake_forecast(50.0),
    )

    args = weekly_delta.parse_args(["--max-cost-usd", "10"])
    code = weekly_delta.run(args)

    assert code == 2, f"Expected exit 2 on cost-gate abort; got {code}"
    assert fetched == [], "fetch_thread must not be called past the gate"
    assert tagged == [], "tag_comment must not be called past the gate"

    # ``readouterr`` is destructive — call it ONCE, combine both streams.
    captured = capsys.readouterr()
    full = captured.out + captured.err
    assert "ABORT" in full or "exceeds" in full, (
        f"Expected ABORT/exceeds in output; got: {full!r}"
    )


# =========================================================================
# 5. End-of-run writes updated last_run.json
# =========================================================================


def test_end_of_run_writes_last_run_json(
    tmp_work: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful run → ``last_run.json`` is written with current timestamp.

    Mocks the pipeline end-to-end with a single candidate that produces
    one tagged comment. Asserts the post-run ``last_run.json`` exists,
    is parseable, and its ``last_run_at`` is recent (within 1 minute).
    """

    candidates = [_make_candidate("end0")]
    monkeypatch.setattr(
        weekly_delta.discover, "discover_all", lambda **_kw: iter(candidates)
    )

    fake_thread = _make_thread("end0", n_comments=1)
    monkeypatch.setattr(
        weekly_delta.fetch, "fetch_thread", lambda _c: fake_thread
    )
    monkeypatch.setattr(
        weekly_delta.disambiguate, "is_in_scope", lambda _t: (True, "passed")
    )
    monkeypatch.setattr(
        weekly_delta.tag,
        "tag_comment",
        lambda comment, thread: _make_tagged_for(comment, thread.id),
    )
    monkeypatch.setattr(
        weekly_delta.tag,
        "estimate_mega_backfill_cost",
        lambda **_kw: _fake_forecast(0.10),
    )
    monkeypatch.setattr(
        weekly_delta.store, "upsert_thread", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(
        weekly_delta.store, "write_voice_candidates", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(
        weekly_delta,
        "build_and_write_aggregate",
        lambda **_kw: {
            "total_threads": 1,
            "total_comments": 1,
            "aggregate_path": str(tmp_work / "latest.json"),
        },
    )

    before = datetime.now(tz=timezone.utc)
    args = weekly_delta.parse_args(["--max-cost-usd", "5", "--quiet"])
    code = weekly_delta.run(args)
    after = datetime.now(tz=timezone.utc)

    assert code == 0, f"Expected exit 0 on happy path; got {code}"

    assert weekly_delta.LAST_RUN_PATH.exists(), (
        "last_run.json should have been written"
    )

    with weekly_delta.LAST_RUN_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["version"] == weekly_delta.LAST_RUN_VERSION
    assert "last_run_at" in data
    last_run_at = datetime.fromisoformat(data["last_run_at"])
    if last_run_at.tzinfo is None:
        last_run_at = last_run_at.replace(tzinfo=timezone.utc)
    # The timestamp recorded is the *start* of the run, so it should be
    # at or after ``before`` and at or before ``after``.
    assert before <= last_run_at <= after, (
        f"Expected last_run_at in [{before}, {after}]; got {last_run_at}"
    )

    # Counts should reflect that we processed one thread + one comment.
    assert data["threads_processed"] == 1
    assert data["comments_tagged"] == 1


# =========================================================================
# Helper-fn unit tests (round out coverage on the small bits)
# =========================================================================


def test_resolve_time_horizon_falls_back_to_lookback_when_none() -> None:
    """``last_run is None`` → falls back to ``now - fallback_days``."""

    now = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)
    horizon = weekly_delta.resolve_time_horizon(
        None, now=now, fallback_days=7
    )
    assert horizon == datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)


def test_resolve_time_horizon_uses_anchor_when_set() -> None:
    """``last_run`` set → returned as-is, ignoring ``now``/``fallback_days``."""

    anchor = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 5, 23, tzinfo=timezone.utc)
    horizon = weekly_delta.resolve_time_horizon(
        anchor, now=now, fallback_days=999
    )
    assert horizon == anchor


def test_save_and_load_last_run_roundtrip(tmp_work: Path) -> None:
    """``save_last_run`` then ``load_last_run`` gives back the same timestamp."""

    anchor = datetime(2026, 5, 17, 23, 0, 0, tzinfo=timezone.utc)
    weekly_delta.save_last_run(
        last_run_at=anchor,
        threads_processed=12,
        comments_tagged=47,
        cost_usd=3.21,
    )
    loaded = weekly_delta.load_last_run()
    assert loaded == anchor


def test_load_last_run_missing_returns_none(tmp_work: Path) -> None:
    """No file at path → returns None (the 7-day-fallback signal)."""

    # tmp_work fixture has already redirected LAST_RUN_PATH; nothing
    # is written, so the file is absent.
    assert not weekly_delta.LAST_RUN_PATH.exists()
    assert weekly_delta.load_last_run() is None


def test_load_last_run_version_mismatch_raises(tmp_work: Path) -> None:
    """A checkpoint with an unknown version fails loudly."""

    weekly_delta.LAST_RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
    weekly_delta.LAST_RUN_PATH.write_text(
        json.dumps({"version": 999, "last_run_at": "2026-05-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="version=999"):
        weekly_delta.load_last_run()


def test_load_last_run_invalid_timestamp_falls_back(
    tmp_work: Path,
) -> None:
    """Unparseable ``last_run_at`` → returns None (caller falls back)."""

    weekly_delta.LAST_RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
    weekly_delta.LAST_RUN_PATH.write_text(
        json.dumps({"version": 1, "last_run_at": "not-a-date"}),
        encoding="utf-8",
    )
    assert weekly_delta.load_last_run() is None


def test_force_time_horizon_overrides_last_run(
    tmp_work: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--force-time-horizon`` wins over an existing last_run.json."""

    # Even though we write a last_run.json, the CLI override should win.
    weekly_delta.save_last_run(
        last_run_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )

    captured: dict = {}

    def _fake_discover_all(**kwargs):
        captured["time_horizon"] = kwargs.get("time_horizon")
        return iter([])

    monkeypatch.setattr(weekly_delta.discover, "discover_all", _fake_discover_all)

    forced = "2026-05-15T12:00:00+00:00"
    args = weekly_delta.parse_args(
        ["--dry-run", "--force-time-horizon", forced]
    )
    code = weekly_delta.run(args)
    assert code == 0
    expected = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
    assert captured["time_horizon"] == expected


def test_force_time_horizon_bad_iso_returns_exit_1(
    tmp_work: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unparseable ``--force-time-horizon`` exits 1 with a helpful message."""

    monkeypatch.setattr(
        weekly_delta.discover, "discover_all", lambda **_kw: iter([])
    )
    args = weekly_delta.parse_args(
        ["--dry-run", "--force-time-horizon", "definitely-not-iso"]
    )
    code = weekly_delta.run(args)
    assert code == 1
    err = capsys.readouterr().err
    assert "ISO 8601" in err


def test_out_of_scope_thread_not_appended_to_jsonl(
    tmp_work: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disambiguate-rejected thread MUST NOT be written to ``threads.jsonl``.

    Regression guard for the v1-cleanup fix at ``weekly_delta.py:672``:
    the orchestrator wraps every disambiguate rejection reason with the
    ``skipped:`` prefix (e.g. ``skipped:subreddit_blacklist``), so the
    JSONL-append guard must use ``startswith("skipped:")`` rather than
    an exact match on a single literal reason — otherwise out-of-scope
    threads silently pollute the durable mirror.
    """

    candidates = [_make_candidate("oos0")]
    monkeypatch.setattr(
        weekly_delta.discover, "discover_all", lambda **_kw: iter(candidates)
    )

    rejected_thread = _make_thread("oos0", n_comments=1)
    monkeypatch.setattr(
        weekly_delta.fetch, "fetch_thread", lambda _c: rejected_thread
    )
    # Disambiguate rejects the thread with a real (non-"not_jove") reason.
    monkeypatch.setattr(
        weekly_delta.disambiguate,
        "is_in_scope",
        lambda _t: (False, "subreddit_blacklist"),
    )
    # tag/store should not be reached because process_one_thread returns
    # early on rejection — but stub them defensively so a regression in
    # that early-return doesn't pollute the test environment.
    monkeypatch.setattr(
        weekly_delta.tag,
        "tag_comment",
        lambda _c, _t: pytest.fail("tag_comment must not be called for OOS"),
    )
    monkeypatch.setattr(
        weekly_delta.store, "upsert_thread", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(
        weekly_delta.store, "write_voice_candidates", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(
        weekly_delta.tag,
        "estimate_mega_backfill_cost",
        lambda **_kw: _fake_forecast(0.10),
    )
    monkeypatch.setattr(
        weekly_delta,
        "build_and_write_aggregate",
        lambda **_kw: {
            "total_threads": 0,
            "total_comments": 0,
            "aggregate_path": str(tmp_work / "latest.json"),
        },
    )

    args = weekly_delta.parse_args(["--max-cost-usd", "5", "--quiet"])
    code = weekly_delta.run(args)
    assert code == 0

    # The rejected thread must NOT be in threads.jsonl.
    threads_path = weekly_delta.THREADS_PATH
    if threads_path.exists():
        content = threads_path.read_text(encoding="utf-8").strip()
        assert content == "", (
            f"threads.jsonl should be empty for OOS-only run; got: {content!r}"
        )
    # else: file doesn't exist at all — also acceptable.
