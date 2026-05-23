"""Unit tests for :mod:`pipeline.voice_curate` (Phase 11 — curation core).

These tests lock down the round-trip contract between Phase 6's
Candidates markdown and Phase 7's Published-parser:

* Phase 6's :func:`pipeline.store.write_voice_candidates` output MUST
  parse back into :class:`VoiceCandidate` objects.
* :func:`pipeline.voice_curate.approve` output MUST be readable by
  Phase 7's :func:`pipeline.aggregate.parse_voice_published_dir`.

Test pattern: use a real synthetic ``Candidates/`` directory in
``tmp_path``, write blocks via the actual Phase 6 emitter (so we test
the literal format Phase 6 produces, not our guess at it), then verify
the parser + approver behave correctly. This is heavier than a pure
unit test but it's the only way to keep the cross-phase contract
honest — a regex change in Phase 6 that drifts from our parser would
otherwise silently break D's weekly ritual.

Tests also cover:

* ``load_candidates`` returns blocks in file order.
* ``approve`` creates Published files with frontmatter on first call,
  appends on subsequent calls.
* Rejected/skipped candidates never reach Published.
* ``curator_note`` is preserved and parseable by Phase 7's regex.
* ``list_weeks_with_candidates`` returns chronologically-sorted weeks
  and excludes weeks whose files are header-only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline.aggregate import parse_voice_published_dir, parse_voice_published_file
from pipeline.models import Comment, TaggedComment, ThemeAssignment, Thread
from pipeline.store import write_voice_candidates
from pipeline.voice_curate import (
    VoiceCandidate,
    approve,
    list_weeks_with_candidates,
    load_candidates,
)


# =========================================================================
# Fixtures — build real Candidates files via Phase 6's emitter
# =========================================================================


def _make_thread() -> Thread:
    """A simple JoVE-scope thread used as the parent for candidate fixtures."""

    return Thread(
        id="t3_seed",
        permalink="https://www.reddit.com/r/labrats/comments/seed/is_jove_respectable/",
        subreddit="labrats",
        title="Is JoVE respectable?",
        body="Curious about JoVE.",
        author="op_user",
        created_utc=datetime(2026, 4, 1, tzinfo=timezone.utc),
        score=42,
        num_comments=3,
        is_self=True,
        comments=[],
    )


def _make_comment(
    *,
    comment_id: str,
    body: str,
    author: str = "somePostdoc",
    score: int = 247,
) -> Comment:
    return Comment(
        id=comment_id,
        parent_id="t3_seed",
        permalink=(
            "https://www.reddit.com/r/labrats/comments/seed/_/" + comment_id + "/"
        ),
        body=body,
        author=author,
        created_utc=datetime(2026, 4, 12, tzinfo=timezone.utc),
        score=score,
        depth=0,
    )


def _make_tagged(
    *,
    comment_id: str,
    themes: list[ThemeAssignment] | None = None,
    sentiment: str = "negative",
    voice_candidate: bool = True,
    voice_candidate_reason: str = (
        "First-person + specific anecdote + named harm + journalistic story arc"
    ),
) -> TaggedComment:
    return TaggedComment(
        comment_id=comment_id,
        thread_id="t3_seed",
        permalink=(
            "https://www.reddit.com/r/labrats/comments/seed/_/" + comment_id + "/"
        ),
        sentiment=sentiment,  # type: ignore[arg-type]
        themes=themes
        or [ThemeAssignment(theme="author_fees", sentiment="negative")],
        product_attribution="journal",
        journal_named="JoVE",
        voice_candidate=voice_candidate,
        voice_candidate_reason=voice_candidate_reason,
        tagged_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
        tagged_by_model="claude-haiku-4-5-20251001+claude-sonnet-4-6",
    )


def _seed_candidates_file(
    candidates_dir: Path, week: str, n: int = 3
) -> Path:
    """Write ``n`` candidate blocks via Phase 6's actual emitter.

    Returns the resulting Candidates file path. The bodies are
    distinctive (numbered) so tests can assert per-block correctness.
    """

    thread = _make_thread()
    comments = []
    tagged = []
    for i in range(1, n + 1):
        cid = f"t1_c{i:02d}"
        body = (
            f"Candidate #{i}: Wasted 9 months. The editor never replied to emails. "
            "Career timeline took a real hit."
        )
        comments.append(_make_comment(comment_id=cid, body=body, score=200 + i))
        tagged.append(_make_tagged(comment_id=cid))
    thread.comments = comments

    now = datetime(2026, 5, 23, tzinfo=timezone.utc)
    return write_voice_candidates(  # type: ignore[return-value]
        tagged, thread, week=week, now=now
    ) or candidates_dir / f"{week}.md"


# =========================================================================
# load_candidates — happy path: parses 3 blocks
# =========================================================================


def test_load_candidates_parses_three_blocks(tmp_path: Path) -> None:
    """Phase 6's emitter writes 3 blocks; parser returns 3 candidates.

    Locks the most important cross-phase contract: the format
    :func:`pipeline.store.write_voice_candidates` produces is the
    format :func:`load_candidates` consumes. Any regex drift in Phase
    6's emitter or our parser surfaces here.
    """

    candidates_dir = tmp_path / "Candidates"
    candidates_dir.mkdir()

    # Phase 6 uses VAULT_VOICE_CANDIDATES at module-import time, so we
    # rewrite the resolved path via the same env-driven config — but
    # the simplest approach is to call write_voice_candidates with
    # explicit week + a monkeypatched module constant.
    import pipeline.store as store_module

    original = store_module.VAULT_VOICE_CANDIDATES
    store_module.VAULT_VOICE_CANDIDATES = candidates_dir
    try:
        _seed_candidates_file(candidates_dir, week="2026-W22", n=3)
    finally:
        store_module.VAULT_VOICE_CANDIDATES = original

    candidates = load_candidates(
        week="2026-W22", candidates_dir=candidates_dir
    )
    assert len(candidates) == 3
    # Per-field smoke checks on the first block.
    first = candidates[0]
    assert first.journal == "JoVE"
    assert first.sentiment == "NEGATIVE"
    # themes_pretty is "theme:sentiment" pair per Phase 6 formatter.
    assert "author_fees:negative" in first.themes_pretty
    assert first.author == "somePostdoc"
    assert first.thread_title == "Is JoVE respectable?"
    assert first.thread_url.startswith("https://www.reddit.com/")
    assert first.comment_url.endswith("t1_c01/")
    assert first.date == "2026-04-12"
    assert first.score == 201
    assert "Wasted 9 months" in first.body
    assert "First-person" in first.voice_rubric_reason
    assert first.week == "2026-W22"


def test_load_candidates_preserves_file_order(tmp_path: Path) -> None:
    """Candidates are returned in the order Phase 6 wrote them.

    Phase 6 writes one block per voice_candidate=True comment in the
    order the thread iterates its comments. The CLI walks candidates
    1/N → N/N so D sees them in the same order each session.
    """

    candidates_dir = tmp_path / "Candidates"
    candidates_dir.mkdir()

    import pipeline.store as store_module

    original = store_module.VAULT_VOICE_CANDIDATES
    store_module.VAULT_VOICE_CANDIDATES = candidates_dir
    try:
        _seed_candidates_file(candidates_dir, week="2026-W22", n=5)
    finally:
        store_module.VAULT_VOICE_CANDIDATES = original

    candidates = load_candidates(
        week="2026-W22", candidates_dir=candidates_dir
    )
    # Bodies are numbered "Candidate #1", "Candidate #2", ...
    for i, c in enumerate(candidates, start=1):
        assert f"Candidate #{i}:" in c.body


def test_load_candidates_returns_empty_for_missing_week(tmp_path: Path) -> None:
    """A week that has no Candidates file returns an empty list."""

    candidates_dir = tmp_path / "Candidates"
    candidates_dir.mkdir()
    result = load_candidates(week="2030-W01", candidates_dir=candidates_dir)
    assert result == []


# =========================================================================
# approve — creates Published file with frontmatter; appends on subsequent
# =========================================================================


def test_approve_creates_published_file_with_frontmatter(tmp_path: Path) -> None:
    """First approve() call creates a Published file with frontmatter.

    The frontmatter MUST include ``updated:`` because Phase 7's
    parser keys ``curated_at`` off that field.
    """

    candidates_dir = tmp_path / "Candidates"
    published_dir = tmp_path / "Published"
    candidates_dir.mkdir()

    import pipeline.store as store_module

    original = store_module.VAULT_VOICE_CANDIDATES
    store_module.VAULT_VOICE_CANDIDATES = candidates_dir
    try:
        _seed_candidates_file(candidates_dir, week="2026-W22", n=1)
    finally:
        store_module.VAULT_VOICE_CANDIDATES = original

    candidates = load_candidates(
        week="2026-W22", candidates_dir=candidates_dir
    )
    assert len(candidates) == 1

    published_path = approve(
        candidates[0],
        curator_note=None,
        published_dir=published_dir,
        now=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )

    assert published_path == published_dir / "2026-W22.md"
    assert published_path.exists()
    content = published_path.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    assert "updated: 2026-05-23" in content
    assert "created: 2026-05-23" in content
    assert "# Voice Published — Week 2026-W22" in content
    # The block should also be present.
    assert "## JoVE · NEGATIVE" in content
    assert "u/somePostdoc" in content


def test_approve_appends_to_existing_file(tmp_path: Path) -> None:
    """Second approve() call appends a new block; frontmatter survives.

    ``updated:`` bumps to the current day so Phase 7's ``curated_at``
    reflects the most-recent curation pass.
    """

    candidates_dir = tmp_path / "Candidates"
    published_dir = tmp_path / "Published"
    candidates_dir.mkdir()

    import pipeline.store as store_module

    original = store_module.VAULT_VOICE_CANDIDATES
    store_module.VAULT_VOICE_CANDIDATES = candidates_dir
    try:
        _seed_candidates_file(candidates_dir, week="2026-W22", n=2)
    finally:
        store_module.VAULT_VOICE_CANDIDATES = original

    candidates = load_candidates(
        week="2026-W22", candidates_dir=candidates_dir
    )
    assert len(candidates) == 2

    approve(
        candidates[0],
        published_dir=published_dir,
        now=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    # Second approval on a later day: updated: should bump.
    approve(
        candidates[1],
        published_dir=published_dir,
        now=datetime(2026, 5, 30, tzinfo=timezone.utc),
    )

    content = (published_dir / "2026-W22.md").read_text(encoding="utf-8")
    # Both candidates' bodies appear.
    assert "Candidate #1:" in content
    assert "Candidate #2:" in content
    # The created date is preserved from the first write.
    assert "created: 2026-05-23" in content
    # The updated date has been bumped to the second day.
    assert "updated: 2026-05-30" in content


# =========================================================================
# Rejected candidates do NOT appear in Published
# =========================================================================


def test_rejected_candidates_do_not_appear_in_published(tmp_path: Path) -> None:
    """Skip/reject paths never call approve(), so Published stays absent.

    Tests the negative-space contract of the CLI flow: only the
    explicit approve() invocation writes to Published. We simulate the
    CLI's reject path by simply not calling approve() on a candidate.
    """

    candidates_dir = tmp_path / "Candidates"
    published_dir = tmp_path / "Published"
    candidates_dir.mkdir()

    import pipeline.store as store_module

    original = store_module.VAULT_VOICE_CANDIDATES
    store_module.VAULT_VOICE_CANDIDATES = candidates_dir
    try:
        _seed_candidates_file(candidates_dir, week="2026-W22", n=3)
    finally:
        store_module.VAULT_VOICE_CANDIDATES = original

    candidates = load_candidates(
        week="2026-W22", candidates_dir=candidates_dir
    )

    # Approve only candidates[1]; "reject" 0 and 2 by not calling approve.
    approve(
        candidates[1],
        published_dir=published_dir,
        now=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )

    content = (published_dir / "2026-W22.md").read_text(encoding="utf-8")
    # Only candidate #2's body is in Published.
    assert "Candidate #2:" in content
    assert "Candidate #1:" not in content
    assert "Candidate #3:" not in content


# =========================================================================
# Curator note round-trips through Phase 7's parser
# =========================================================================


def test_curator_note_preserved_in_published_markdown(tmp_path: Path) -> None:
    """``curator_note`` is written and is parseable by Phase 7's regex.

    This is the cross-phase contract check on the Curator note feature
    (the ``[n]`` keystroke): if the note line drifts out of the shape
    Phase 7's ``_CURATOR_NOTE_RE`` expects, the dashboard's panel
    footer goes blank. We round-trip through Phase 7 to catch that.
    """

    candidates_dir = tmp_path / "Candidates"
    published_dir = tmp_path / "Published"
    candidates_dir.mkdir()

    import pipeline.store as store_module

    original = store_module.VAULT_VOICE_CANDIDATES
    store_module.VAULT_VOICE_CANDIDATES = candidates_dir
    try:
        _seed_candidates_file(candidates_dir, week="2026-W22", n=1)
    finally:
        store_module.VAULT_VOICE_CANDIDATES = original

    candidates = load_candidates(
        week="2026-W22", candidates_dir=candidates_dir
    )
    note = "Former JoVE author per thread context; concrete dollar figures."
    approve(
        candidates[0],
        curator_note=note,
        published_dir=published_dir,
        now=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )

    # Raw-text assertion: the note line is present.
    content = (published_dir / "2026-W22.md").read_text(encoding="utf-8")
    assert "**Curator note**:" in content
    assert note in content

    # Round-trip via Phase 7's parser — ensures the regex contract holds.
    rows = parse_voice_published_file(published_dir / "2026-W22.md")
    assert len(rows) == 1
    assert rows[0].curator_note == note


# =========================================================================
# list_weeks_with_candidates — chronological order, excludes empty files
# =========================================================================


def test_list_weeks_returns_chronological_order(tmp_path: Path) -> None:
    """Weeks sort lexicographically (= chronologically for ISO-week names)."""

    candidates_dir = tmp_path / "Candidates"
    candidates_dir.mkdir()

    import pipeline.store as store_module

    original = store_module.VAULT_VOICE_CANDIDATES
    store_module.VAULT_VOICE_CANDIDATES = candidates_dir
    try:
        # Write three weeks out of order to verify sorting.
        _seed_candidates_file(candidates_dir, week="2026-W23", n=1)
        _seed_candidates_file(candidates_dir, week="2025-W52", n=1)
        _seed_candidates_file(candidates_dir, week="2026-W22", n=1)
    finally:
        store_module.VAULT_VOICE_CANDIDATES = original

    weeks = list_weeks_with_candidates(candidates_dir=candidates_dir)
    assert weeks == ["2025-W52", "2026-W22", "2026-W23"]


def test_list_weeks_excludes_files_with_only_frontmatter(tmp_path: Path) -> None:
    """A file with frontmatter + header but no blocks is excluded.

    Otherwise the CLI would propose D a no-op week.
    """

    candidates_dir = tmp_path / "Candidates"
    candidates_dir.mkdir()
    # Hand-write a "header-only" file (no candidate blocks).
    (candidates_dir / "2026-W20.md").write_text(
        "---\ntype: note\nupdated: 2026-05-23\n---\n\n"
        "# Voice Candidates — Week 2026-W20\n\n"
        "> [!info] **No qualifying candidates this week.**\n",
        encoding="utf-8",
    )

    # And a "real" file so we can confirm the empty one was skipped, not
    # all files.
    import pipeline.store as store_module

    original = store_module.VAULT_VOICE_CANDIDATES
    store_module.VAULT_VOICE_CANDIDATES = candidates_dir
    try:
        _seed_candidates_file(candidates_dir, week="2026-W22", n=1)
    finally:
        store_module.VAULT_VOICE_CANDIDATES = original

    weeks = list_weeks_with_candidates(candidates_dir=candidates_dir)
    assert weeks == ["2026-W22"]


def test_list_weeks_handles_missing_directory(tmp_path: Path) -> None:
    """Directory doesn't exist → empty list (no exceptions)."""

    nonexistent = tmp_path / "DoesNotExist"
    assert list_weeks_with_candidates(candidates_dir=nonexistent) == []


# =========================================================================
# Round-trip via Phase 7's parser
# =========================================================================


def test_approve_output_roundtrips_through_phase7_parser(tmp_path: Path) -> None:
    """Published file produced by approve() is fully parseable by Phase 7.

    This is the gold-standard cross-phase contract test:
    Phase 6 → load_candidates → approve → Phase 7's
    parse_voice_published_dir all chain together without data loss.
    """

    candidates_dir = tmp_path / "Candidates"
    published_dir = tmp_path / "Published"
    candidates_dir.mkdir()

    import pipeline.store as store_module

    original = store_module.VAULT_VOICE_CANDIDATES
    store_module.VAULT_VOICE_CANDIDATES = candidates_dir
    try:
        _seed_candidates_file(candidates_dir, week="2026-W22", n=3)
    finally:
        store_module.VAULT_VOICE_CANDIDATES = original

    candidates = load_candidates(
        week="2026-W22", candidates_dir=candidates_dir
    )

    # Approve all three; one with a note.
    approve(
        candidates[0],
        published_dir=published_dir,
        now=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    approve(
        candidates[1],
        curator_note="Mid-career postdoc; concrete number.",
        published_dir=published_dir,
        now=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    approve(
        candidates[2],
        published_dir=published_dir,
        now=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )

    rows = parse_voice_published_dir(published_dir)
    assert len(rows) == 3

    # Verify per-field round-trip.
    assert all(r.product_attribution == "journal" for r in rows)
    assert all(r.sentiment == "negative" for r in rows)
    assert all(r.author == "somePostdoc" for r in rows)
    # The candidate with a note must carry it through.
    notes = [r.curator_note for r in rows if r.curator_note is not None]
    assert len(notes) == 1
    assert "Mid-career postdoc" in notes[0]
    # curated_at comes from the file's frontmatter ``updated:`` line.
    for r in rows:
        assert r.curated_at.date().isoformat() == "2026-05-23"


# =========================================================================
# Optional: validate model schema is import-safe
# =========================================================================


def test_voice_candidate_model_basics() -> None:
    """Smoke test on :class:`VoiceCandidate`'s Pydantic shape.

    Construction must work with the minimal field set the parser
    populates; extras are forbidden (catches accidental field-drift in
    future refactors).
    """

    vc = VoiceCandidate(
        journal="JoVE",
        sentiment="NEGATIVE",
        themes_pretty="author_fees:negative",
        author="somePostdoc",
        thread_title="Is JoVE respectable?",
        thread_url="https://www.reddit.com/r/labrats/comments/seed/",
        comment_url="https://www.reddit.com/r/labrats/comments/seed/_/t1_c01/",
        date="2026-04-12",
        score=247,
        voice_rubric_reason="reason",
        body="body",
        week="2026-W22",
        source_path=Path("/tmp/2026-W22.md"),
    )
    assert vc.score == 247
    with pytest.raises(Exception):
        VoiceCandidate(  # type: ignore[call-arg]
            journal="JoVE",
            sentiment="NEGATIVE",
            themes_pretty="x",
            author="x",
            thread_title="x",
            thread_url="x",
            comment_url="x",
            date="x",
            score=0,
            voice_rubric_reason="x",
            body="x",
            week="x",
            source_path=Path("/tmp/x.md"),
            unknown_extra="boom",  # type: ignore[arg-type]
        )
