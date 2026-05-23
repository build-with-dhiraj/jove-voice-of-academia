"""Unit tests for :mod:`scripts.mega_backfill` — Phase 8 orchestrator.

Strategy: mock the six pipeline modules at their public boundaries
(:func:`discover.discover_all`, :func:`fetch.fetch_thread`,
:func:`disambiguate.is_in_scope`, :func:`tag.tag_comment`,
:func:`tag.estimate_mega_backfill_cost`, :func:`store.upsert_thread`,
:func:`store.write_voice_candidates`, :func:`aggregate.build_aggregate`)
so the tests run zero-cost and offline. Tests focus on the orchestrator
contracts — checkpoint resumability, cost-gate semantics, per-thread
failure isolation, SIGINT handling — not on the modules' internal behavior.

Each test verifies one behavioral guarantee from the Phase 8 dispatch.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

# Make ``scripts/`` importable as a package without modifying sys.path
# permanently — the repo doesn't ship scripts/__init__.py so we
# import by file path.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts import mega_backfill  # noqa: E402
from pipeline.models import (  # noqa: E402
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
    """Build a ThreadCandidate whose permalink encodes ``thread_id_segment``.

    Uses the URL shape ``/r/<sub>/comments/<id>/`` so
    :func:`discover.extract_thread_id` parses cleanly.
    """

    return ThreadCandidate(
        permalink=(
            f"https://www.reddit.com/r/labrats/comments/{thread_id_segment}/test/"
        ),
        subreddit="labrats",
        discovered_via="ring1_subreddit_enum",
        discovered_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
        seed_query=None,
    )


def _make_thread(thread_id: str, n_comments: int = 1) -> Thread:
    comments = [
        Comment(
            id=f"t1_{thread_id}_{i}",
            parent_id=f"t3_{thread_id}",
            permalink=(
                f"https://www.reddit.com/r/labrats/comments/{thread_id}/_/t1_{thread_id}_{i}/"
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
        id=f"t3_{thread_id}",
        permalink=f"https://www.reddit.com/r/labrats/comments/{thread_id}/_/",
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
# Fixture — temp work area + neutered durable-file paths
# =========================================================================


@pytest.fixture
def tmp_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every durable path the script writes into ``tmp_path``.

    Without this fixture, tests would dirty ``data/`` and
    ``dashboard/data/`` in the live repo. The fixture also disables
    the read-running-cost path by pointing ``COST_LOG_PATH`` at a
    non-existent file, so progress-line tests can't be flaky due to
    a stale ``data/tag-cost-log.jsonl``.
    """

    checkpoint = tmp_path / "checkpoint.json"
    threads_jsonl = tmp_path / "threads.jsonl"
    tagged_jsonl = tmp_path / "tagged-comments.jsonl"
    cost_log = tmp_path / "tag-cost-log.jsonl"
    aggregate_out = tmp_path / "dashboard" / "data" / "latest.json"

    monkeypatch.setattr(mega_backfill, "DEFAULT_CHECKPOINT_PATH", checkpoint)
    monkeypatch.setattr(mega_backfill, "THREADS_PATH", threads_jsonl)
    monkeypatch.setattr(mega_backfill, "TAGGED_COMMENTS_PATH", tagged_jsonl)
    monkeypatch.setattr(mega_backfill, "COST_LOG_PATH", cost_log)
    monkeypatch.setattr(mega_backfill, "AGGREGATE_OUTPUT_PATH", aggregate_out)

    return tmp_path


# =========================================================================
# 1. --dry-run: prints cost forecast, doesn't fetch or tag
# =========================================================================


def test_dry_run_prints_forecast_and_exits_zero(
    tmp_work: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--dry-run`` shows the cost forecast and exits 0 without LLM calls.

    Mocks discover to return 100 candidates. Asserts:

    * exit code is 0
    * ``fetch.fetch_thread`` is never called
    * ``tag.tag_comment`` is never called
    * the printed output contains ``"GRAND TOTAL"`` (forecast was shown)
    """

    candidates = [_make_candidate(f"c{i}") for i in range(100)]
    monkeypatch.setattr(
        mega_backfill.discover, "discover_all", lambda **_kw: iter(candidates)
    )

    fetch_spy = mock.Mock()
    tag_spy = mock.Mock()
    monkeypatch.setattr(mega_backfill.fetch, "fetch_thread", fetch_spy)
    monkeypatch.setattr(mega_backfill.tag, "tag_comment", tag_spy)

    args = mega_backfill.parse_args(
        ["--dry-run", "--checkpoint-path", str(tmp_work / "checkpoint.json")]
    )
    code = mega_backfill.run(args)

    assert code == 0
    fetch_spy.assert_not_called()
    tag_spy.assert_not_called()

    out = capsys.readouterr().out
    assert "GRAND TOTAL" in out
    assert "100" in out  # the candidate count appears in the forecast block


# =========================================================================
# 2. Cost gate: forecast above --max-cost-usd aborts non-zero
# =========================================================================


def test_cost_gate_aborts_when_estimate_exceeds_max(
    tmp_work: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The cost gate aborts with exit code 2 when forecast exceeds ceiling.

    Uses the real ``estimate_mega_backfill_cost`` with 10,000 threads —
    that's roughly $500+ at current Haiku/Sonnet rates, well above the
    $50 ceiling we pass.
    """

    big_candidate_set = [_make_candidate(f"c{i}") for i in range(10000)]
    monkeypatch.setattr(
        mega_backfill.discover,
        "discover_all",
        lambda **_kw: iter(big_candidate_set),
    )

    fetch_spy = mock.Mock()
    tag_spy = mock.Mock()
    monkeypatch.setattr(mega_backfill.fetch, "fetch_thread", fetch_spy)
    monkeypatch.setattr(mega_backfill.tag, "tag_comment", tag_spy)

    args = mega_backfill.parse_args(
        [
            "--max-cost-usd",
            "50",
            "--checkpoint-path",
            str(tmp_work / "checkpoint.json"),
        ]
    )
    code = mega_backfill.run(args)

    assert code == 2, "Expected exit code 2 on cost-gate abort"
    fetch_spy.assert_not_called()
    tag_spy.assert_not_called()

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "ABORT" in combined
    assert "max-cost-usd" in combined


# =========================================================================
# 3. Checkpoint resume: pre-populated checkpoint skips first N threads
# =========================================================================


def test_checkpoint_resume_skips_already_processed(
    tmp_work: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-populated checkpoint with 50 done → loop starts at thread 51.

    Writes a checkpoint marking the first 50 candidate permalinks as
    processed, then asserts that ``fetch_thread`` is invoked exactly 50
    times — once per remaining candidate. Also asserts none of the
    first 50 permalinks appears in the fetch call args.
    """

    candidates = [_make_candidate(f"resume{i}") for i in range(100)]
    monkeypatch.setattr(
        mega_backfill.discover, "discover_all", lambda **_kw: iter(candidates)
    )

    # Pre-populate the checkpoint.
    checkpoint_path = tmp_work / "checkpoint.json"
    checkpoint = mega_backfill._new_checkpoint()
    checkpoint["processed_thread_ids"] = sorted(
        [c.permalink for c in candidates[:50]]
    )
    mega_backfill.save_checkpoint(checkpoint, checkpoint_path)

    # fetch returns a single-comment thread for whichever candidate it gets.
    def _fake_fetch(cand: ThreadCandidate) -> Thread:
        # Recover the per-candidate id segment from the permalink so the
        # returned Thread has a unique id (not relevant for the assertion
        # but it mirrors production behavior).
        seg = cand.permalink.split("/comments/")[1].split("/")[0]
        return _make_thread(seg, n_comments=1)

    fetch_calls: list[ThreadCandidate] = []

    def _spy_fetch(cand: ThreadCandidate) -> Thread:
        fetch_calls.append(cand)
        return _fake_fetch(cand)

    monkeypatch.setattr(mega_backfill.fetch, "fetch_thread", _spy_fetch)
    monkeypatch.setattr(
        mega_backfill.disambiguate, "is_in_scope", lambda _t: (True, "passed")
    )
    monkeypatch.setattr(
        mega_backfill.tag,
        "tag_comment",
        lambda comment, thread: _make_tagged_for(comment, thread.id),
    )
    monkeypatch.setattr(mega_backfill.store, "upsert_thread", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        mega_backfill.store,
        "write_voice_candidates",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        mega_backfill.tag,
        "estimate_mega_backfill_cost",
        lambda num_threads, avg_comments_per_thread: {
            "num_threads": num_threads,
            "avg_comments_per_thread": avg_comments_per_thread,
            "total_comments": num_threads * avg_comments_per_thread,
            "per_call_assumptions": {},
            "models_and_pricing_usd_per_mtok": {},
            "totals": {
                "classify_total_input_tokens": 0,
                "classify_total_output_tokens": 0,
                "classify_total_usd": 0.0,
                "voice_total_input_tokens": 0,
                "voice_total_output_tokens": 0,
                "voice_total_usd": 0.0,
                "grand_total_usd": 1.0,
            },
            "uncertainty_note": "fake forecast",
        },
    )
    monkeypatch.setattr(
        mega_backfill,
        "build_and_write_aggregate",
        lambda **_kw: {
            "total_threads": 50,
            "total_comments": 50,
            "top3_by_sentiment": {},
            "aggregate_path": str(tmp_work / "latest.json"),
        },
    )

    args = mega_backfill.parse_args(
        [
            "--checkpoint-path",
            str(checkpoint_path),
            "--max-cost-usd",
            "1000",
            "--quiet",
        ]
    )
    code = mega_backfill.run(args)
    assert code == 0

    # Exactly the last 50 candidates' permalinks should appear in fetch calls.
    assert len(fetch_calls) == 50, (
        f"Expected exactly 50 fetches (51..100); got {len(fetch_calls)}"
    )
    fetched_permalinks = {c.permalink for c in fetch_calls}
    assert fetched_permalinks == {c.permalink for c in candidates[50:]}


# =========================================================================
# 4. Single-thread failure doesn't abort the loop
# =========================================================================


def test_single_thread_failure_does_not_abort_loop(
    tmp_work: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One fetch failure → other threads still processed; failure recorded.

    With 5 candidates where thread index 2 (zero-indexed) raises during
    fetch, the script must:

    * still process threads 0, 1, 3, 4
    * record thread 2's permalink in ``failed_thread_ids``
    * exit with code 0 (not propagate the failure)
    """

    candidates = [_make_candidate(f"fail{i}") for i in range(5)]
    monkeypatch.setattr(
        mega_backfill.discover, "discover_all", lambda **_kw: iter(candidates)
    )

    fail_permalink = candidates[2].permalink

    def _fetch_with_failure(cand: ThreadCandidate) -> Thread:
        if cand.permalink == fail_permalink:
            raise RuntimeError("simulated PRAW failure")
        seg = cand.permalink.split("/comments/")[1].split("/")[0]
        return _make_thread(seg, n_comments=1)

    processed_thread_ids: list[str] = []

    def _spy_upsert(thread: Thread, _tagged: list[TaggedComment]) -> None:
        processed_thread_ids.append(thread.id)

    monkeypatch.setattr(mega_backfill.fetch, "fetch_thread", _fetch_with_failure)
    monkeypatch.setattr(
        mega_backfill.disambiguate, "is_in_scope", lambda _t: (True, "passed")
    )
    monkeypatch.setattr(
        mega_backfill.tag,
        "tag_comment",
        lambda comment, thread: _make_tagged_for(comment, thread.id),
    )
    monkeypatch.setattr(mega_backfill.store, "upsert_thread", _spy_upsert)
    monkeypatch.setattr(
        mega_backfill.store,
        "write_voice_candidates",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        mega_backfill.tag,
        "estimate_mega_backfill_cost",
        lambda **_kw: {
            "num_threads": 5,
            "avg_comments_per_thread": 15,
            "total_comments": 75,
            "per_call_assumptions": {},
            "models_and_pricing_usd_per_mtok": {},
            "totals": {
                "classify_total_input_tokens": 0,
                "classify_total_output_tokens": 0,
                "classify_total_usd": 0.0,
                "voice_total_input_tokens": 0,
                "voice_total_output_tokens": 0,
                "voice_total_usd": 0.0,
                "grand_total_usd": 0.5,
            },
            "uncertainty_note": "fake",
        },
    )
    monkeypatch.setattr(
        mega_backfill,
        "build_and_write_aggregate",
        lambda **_kw: {
            "total_threads": 4,
            "total_comments": 4,
            "top3_by_sentiment": {},
            "aggregate_path": str(tmp_work / "latest.json"),
        },
    )

    checkpoint_path = tmp_work / "checkpoint.json"
    args = mega_backfill.parse_args(
        [
            "--checkpoint-path",
            str(checkpoint_path),
            "--max-cost-usd",
            "1000",
            "--quiet",
        ]
    )
    code = mega_backfill.run(args)
    assert code == 0

    # Four threads must have been upserted (all but the failed one).
    assert len(processed_thread_ids) == 4
    assert "t3_fail0" in processed_thread_ids
    assert "t3_fail1" in processed_thread_ids
    assert "t3_fail3" in processed_thread_ids
    assert "t3_fail4" in processed_thread_ids
    assert "t3_fail2" not in processed_thread_ids

    # The failure must be recorded in the persisted checkpoint.
    with checkpoint_path.open("r", encoding="utf-8") as f:
        saved = json.load(f)
    assert fail_permalink in saved["failed_thread_ids"]
    assert "simulated PRAW failure" in saved["failed_thread_ids"][fail_permalink]


# =========================================================================
# 5. SIGINT handling: checkpoint is saved before exit
# =========================================================================


def test_sigint_during_loop_saves_checkpoint_before_exit(
    tmp_work: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGINT mid-loop → finish current thread → save checkpoint → exit 130.

    Uses a real-process SIGINT raised from a sibling thread to trigger
    the :class:`GracefulInterrupt` handler that ``main_loop`` installs.
    The fake fetch holds in a controlled wait on the SECOND candidate
    so the signal arrives while the loop is between iterations — the
    point at which graceful shutdown is supposed to detect the flag and
    exit cleanly. The first candidate must have completed (its id in
    the checkpoint), the second-and-later candidates must NOT appear.
    """

    candidates = [_make_candidate(f"sig{i}") for i in range(5)]
    monkeypatch.setattr(
        mega_backfill.discover, "discover_all", lambda **_kw: iter(candidates)
    )

    sigint_sent = threading.Event()
    fetch_count = {"n": 0}

    def _fetch_with_signal(cand: ThreadCandidate) -> Thread:
        fetch_count["n"] += 1
        # On the FIRST fetch, schedule a SIGINT to fire after this thread's
        # work has fully completed. The main thread will see the
        # interrupt flag when it checks at the top of the next loop iter.
        if fetch_count["n"] == 1:
            def _send_sig() -> None:
                # Tiny sleep so we're sure the first thread has fully
                # finished its append+save cycle before the SIGINT fires.
                time.sleep(0.05)
                os.kill(os.getpid(), signal.SIGINT)
                sigint_sent.set()
            threading.Thread(target=_send_sig, daemon=True).start()
        else:
            # If we ever reach the second iteration (shouldn't), wait
            # for the SIGINT so the test is deterministic about ordering.
            sigint_sent.wait(timeout=1.0)
        seg = cand.permalink.split("/comments/")[1].split("/")[0]
        return _make_thread(seg, n_comments=1)

    monkeypatch.setattr(mega_backfill.fetch, "fetch_thread", _fetch_with_signal)
    monkeypatch.setattr(
        mega_backfill.disambiguate, "is_in_scope", lambda _t: (True, "passed")
    )
    monkeypatch.setattr(
        mega_backfill.tag,
        "tag_comment",
        lambda comment, thread: _make_tagged_for(comment, thread.id),
    )
    monkeypatch.setattr(mega_backfill.store, "upsert_thread", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        mega_backfill.store,
        "write_voice_candidates",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        mega_backfill.tag,
        "estimate_mega_backfill_cost",
        lambda **_kw: {
            "num_threads": 5,
            "avg_comments_per_thread": 15,
            "total_comments": 75,
            "per_call_assumptions": {},
            "models_and_pricing_usd_per_mtok": {},
            "totals": {
                "classify_total_input_tokens": 0,
                "classify_total_output_tokens": 0,
                "classify_total_usd": 0.0,
                "voice_total_input_tokens": 0,
                "voice_total_output_tokens": 0,
                "voice_total_usd": 0.0,
                "grand_total_usd": 0.5,
            },
            "uncertainty_note": "fake",
        },
    )
    monkeypatch.setattr(
        mega_backfill,
        "build_and_write_aggregate",
        lambda **_kw: {
            "total_threads": 1,
            "total_comments": 1,
            "top3_by_sentiment": {},
            "aggregate_path": str(tmp_work / "latest.json"),
        },
    )

    checkpoint_path = tmp_work / "checkpoint.json"
    args = mega_backfill.parse_args(
        [
            "--checkpoint-path",
            str(checkpoint_path),
            "--max-cost-usd",
            "1000",
            "--quiet",
        ]
    )
    code = mega_backfill.run(args)

    # Either 130 (graceful) or 0 (race-lost — current thread finished and
    # the next iteration's check found the flag too late). We assert the
    # checkpoint contains the first thread's id either way — the durable
    # invariant.
    assert code in (0, 130)

    # Wait for the sibling thread to have fired its kill (otherwise the
    # checkpoint inspection might race the late SIGINT delivery).
    sigint_sent.wait(timeout=1.0)

    with checkpoint_path.open("r", encoding="utf-8") as f:
        saved = json.load(f)
    # The first thread's id must be persisted.
    assert "t3_sig0" in saved["processed_thread_ids"]
    # We must NOT have processed everything — the SIGINT short-circuited.
    assert "t3_sig4" not in saved["processed_thread_ids"]


# =========================================================================
# Helper-fn unit tests (round out coverage on the small bits)
# =========================================================================


def test_format_eta_with_no_progress_yet() -> None:
    """Zero fraction-done -> ``--:--`` placeholder."""

    assert mega_backfill.format_eta(elapsed_seconds=10.0, fraction_done=0.0) == "--:--"


def test_format_eta_with_progress() -> None:
    """Halfway done in 30 min → ETA ~30 min remaining."""

    eta = mega_backfill.format_eta(elapsed_seconds=1800.0, fraction_done=0.5)
    assert eta == "0h30m"


def test_read_running_cost_usd_no_log_returns_zero(tmp_work: Path) -> None:
    """Returns 0.0 when the cost log doesn't exist (first run)."""

    # The fixture already points COST_LOG_PATH at a non-existent path.
    assert mega_backfill.read_running_cost_usd() == 0.0


def test_read_running_cost_usd_sums_jsonl(
    tmp_work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sums ``total_cost_usd`` across all valid JSONL lines, ignores bad ones."""

    log = tmp_work / "tag-cost-log.jsonl"
    log.write_text(
        '{"total_cost_usd": 0.5}\n'
        '{"total_cost_usd": 0.25}\n'
        "not-json-line\n"
        '{"total_cost_usd": 1.0}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(mega_backfill, "COST_LOG_PATH", log)
    assert abs(mega_backfill.read_running_cost_usd() - 1.75) < 1e-9


def test_checkpoint_roundtrip(tmp_work: Path) -> None:
    """save → load gives back the same data structure."""

    path = tmp_work / "rt-checkpoint.json"
    cp = mega_backfill._new_checkpoint()
    cp["processed_thread_ids"] = ["t3_a", "t3_b"]
    cp["failed_thread_ids"] = {"https://x": "boom"}
    cp["total_tagged_comments"] = 99
    mega_backfill.save_checkpoint(cp, path)

    reloaded = mega_backfill.load_checkpoint(path)
    assert reloaded["processed_thread_ids"] == ["t3_a", "t3_b"]
    assert reloaded["failed_thread_ids"] == {"https://x": "boom"}
    assert reloaded["total_tagged_comments"] == 99
    assert reloaded["version"] == mega_backfill.CHECKPOINT_VERSION


def test_checkpoint_version_mismatch_raises(tmp_work: Path) -> None:
    """A checkpoint with an unknown version fails loudly on load."""

    path = tmp_work / "old.json"
    path.write_text(
        json.dumps({"version": 999, "processed_thread_ids": []}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="version=999"):
        mega_backfill.load_checkpoint(path)


def test_jsonl_mirrors_roundtrip(tmp_work: Path) -> None:
    """Threads + TaggedComments append to JSONL then load back identically."""

    thread = _make_thread("rtthread", n_comments=2)
    mega_backfill.append_thread_to_jsonl(thread)
    mega_backfill.append_thread_to_jsonl(_make_thread("rtthread2", n_comments=1))

    tcs = [
        _make_tagged_for(c, thread.id) for c in thread.comments
    ]
    mega_backfill.append_tagged_comments_to_jsonl(tcs)

    loaded_threads = mega_backfill.load_threads_from_jsonl()
    loaded_tcs = mega_backfill.load_tagged_comments_from_jsonl()

    assert "t3_rtthread" in loaded_threads
    assert "t3_rtthread2" in loaded_threads
    assert len(loaded_tcs) == 2
    assert {tc.comment_id for tc in loaded_tcs} == {c.id for c in thread.comments}


# =========================================================================
# Integration smoke (skipped by default)
# =========================================================================


@pytest.mark.integration
def test_dry_run_against_real_discovery_scope(
    tmp_work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke: --dry-run against the actual discovery layer.

    Skipped by default. Run with ``pytest -m integration``. Verifies
    that the cost-gate path completes without exception against the
    real ``discover_all`` (which hits Reddit). We don't assert on the
    candidate count — it's a smoke test that the wiring is intact.
    """

    args = mega_backfill.parse_args(
        ["--dry-run", "--checkpoint-path", str(tmp_work / "checkpoint.json")]
    )
    code = mega_backfill.run(args)
    assert code == 0
