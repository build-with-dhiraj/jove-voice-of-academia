"""Tests for :mod:`pipeline.disambiguate`.

Four test sets, mirroring the dispatch:

* **Set 1** — D's 6 seed threads MUST pass (``in_scope == True``). Fixtures
  are abbreviated to the fields the filter actually touches (subreddit,
  title, body, top-3 comments).
* **Set 2** — Known false-positive patterns MUST fail. Roman-god "by Jove"
  references, JoJo's Bizarre Adventure threads, "what to name my daughter
  Jo" threads, etc.
* **Set 3** — Subreddit-context edge cases. An academia-core sub passes on
  signal alone; an ambiguous sub needs a context co-mention.
* **Set 4** — Reason-string contracts. Downstream audit code keys on these
  exact strings; lock them in tests.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pipeline.disambiguate import DisambigRules, is_in_scope
from pipeline.models import Comment, Thread


# ---------- Fixture helpers ----------

UTC_EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _comment(body: str, idx: int = 0) -> Comment:
    """Build a minimal :class:`Comment` for fixture composition.

    The filter only touches ``body`` (for the top-3 haystack), so the
    surrounding fields are stable placeholders that satisfy the Pydantic
    contract without leaking into assertions.
    """

    return Comment(
        id=f"t1_fake{idx:03d}",
        parent_id="t3_fakethread",
        permalink=f"https://www.reddit.com/r/fake/comments/x/_/t1_fake{idx:03d}/",
        body=body,
        author="fake_user",
        created_utc=UTC_EPOCH,
        score=1,
        depth=0,
    )


def _thread(
    *,
    subreddit: str,
    title: str,
    body: str | None = None,
    comments: list[str] | None = None,
) -> Thread:
    """Build a minimal :class:`Thread` from just the fields the filter reads.

    ``comments`` is a list of comment bodies (the filter never looks at
    score, author, or timestamps).
    """

    comment_objs = [_comment(b, i) for i, b in enumerate(comments or [])]
    return Thread(
        id="t3_fakethread",
        permalink=f"https://www.reddit.com/r/{subreddit}/comments/x/_/",
        subreddit=subreddit,
        title=title,
        body=body,
        author="fake_op",
        created_utc=UTC_EPOCH,
        score=1,
        num_comments=len(comment_objs),
        is_self=body is not None,
        comments=comment_objs,
    )


@pytest.fixture(scope="module")
def rules() -> DisambigRules:
    """Real rules from ``data/disambiguation.json``.

    Scoped ``module`` so the JSON parse happens once per test run, mirroring
    the production lazy-singleton behavior.
    """

    return DisambigRules()


# ---------- Set 1: D's 6 seed threads MUST pass ----------


def test_seed_thread_29o6fu_jove_is_this_legit(rules: DisambigRules) -> None:
    """r/AskAcademia 'JoVE: Is this legit?' (D's seed #1, 2014-07-03)."""

    thread = _thread(
        subreddit="AskAcademia",
        title="JoVE: Is this legit?",
        body=(
            "I've been invited to submit to JoVE (Journal of Visualized "
            "Experiments). Has anyone published with them? Is the peer "
            "review process legitimate?"
        ),
        comments=[
            "JoVE is a legitimate indexed journal but the author fees are "
            "high — around $2400 last I checked.",
            "They're indexed in PubMed and MEDLINE, so legit, but the video "
            "format isn't for everyone.",
        ],
    )

    in_scope, reason = is_in_scope(thread, rules)
    assert in_scope is True, f"Seed #1 should pass; got reason: {reason}"
    assert reason == "passed"


def test_seed_thread_1nkbbm3_special_issue_invitation(rules: DisambigRules) -> None:
    """r/AskAcademia 'Special Issue Invitation form Jove. They can't be serious...' (#2)."""

    thread = _thread(
        subreddit="AskAcademia",
        title="Special Issue Invitation form Jove. They can't be serious...",
        body=(
            "Got an unsolicited guest editor invitation from JoVE today. "
            "The aggressive marketing makes the whole journal feel "
            "predatory. Anyone else getting these emails?"
        ),
        comments=[
            "Yeah they spam everyone. The publication itself is legit but "
            "the outreach feels like a scam.",
            "I got three of these in a month. Hard pass.",
        ],
    )

    in_scope, reason = is_in_scope(thread, rules)
    assert in_scope is True, f"Seed #2 should pass; got reason: {reason}"


def test_seed_thread_1s7vs2b_is_jove_respectable(rules: DisambigRules) -> None:
    """r/labrats 'Is JoVE respectable?' (#3, 2026-03-30)."""

    thread = _thread(
        subreddit="labrats",
        title="Is JoVE respectable?",
        body=(
            "My PI suggested submitting a methodology paper to JoVE. Is the "
            "video journal taken seriously by other labs?"
        ),
        comments=[
            "JoVE is fine for methods papers, especially complex protocols "
            "that benefit from a video walkthrough. Peer review is real.",
            "Reputation varies by field. In biology it's pretty respected; "
            "in chemistry less so.",
            "The author fees are steep ($2400+) but legitimate work.",
        ],
    )

    in_scope, reason = is_in_scope(thread, rules)
    assert in_scope is True, f"Seed #3 should pass; got reason: {reason}"


def test_seed_thread_hjg41o_guest_editor_invitation(rules: DisambigRules) -> None:
    """r/AskAcademia 'JoVE Guest Editor invitation' (#4, 2020-07-02)."""

    thread = _thread(
        subreddit="AskAcademia",
        title="JoVE Guest Editor invitation",
        body=(
            "Received an invitation to be a guest editor for a JoVE special "
            "issue. The journal claims peer review on all video protocols. "
            "Worth the time?"
        ),
        comments=[
            "Depends on your field. JoVE is indexed and legit, but guest "
            "editor work is unpaid.",
            "I did one. Took 9 months. Reasonable manuscript submissions.",
        ],
    )

    in_scope, reason = is_in_scope(thread, rules)
    assert in_scope is True, f"Seed #4 should pass; got reason: {reason}"


def test_seed_thread_1d9dnze_guest_editing_for_jove(rules: DisambigRules) -> None:
    """r/AskAcademia 'Guest editing for JoVE' (#5, 2024-06-06)."""

    thread = _thread(
        subreddit="AskAcademia",
        title="Guest editing for JoVE",
        body=(
            "Was approached to guest edit a JoVE issue on neuroscience "
            "methodology. The journal is paywalled but indexed. Anyone done "
            "this and would recommend?"
        ),
        comments=[
            "Did it. Author fees are real ($2400+) and the access paywall "
            "limits readership, but the publication credit is solid.",
            "Skip unless your institution has a JoVE subscription.",
        ],
    )

    in_scope, reason = is_in_scope(thread, rules)
    assert in_scope is True, f"Seed #5 should pass; got reason: {reason}"


def test_seed_thread_95pq3y_just_got_a_jove_request(rules: DisambigRules) -> None:
    """r/AskAcademia 'Just got a JoVE request' (#6, 2018-08-09)."""

    thread = _thread(
        subreddit="AskAcademia",
        title="Just got a JoVE request",
        body=(
            "JoVE reached out about turning my published paper into a video "
            "protocol. They want $2400 author fees. Is this how the video "
            "journal works?"
        ),
        comments=[
            "Yes, that's the standard fee structure. Video protocols are "
            "their core publication format.",
            "Peer review is real on video submissions but the fees are "
            "steep.",
        ],
    )

    in_scope, reason = is_in_scope(thread, rules)
    assert in_scope is True, f"Seed #6 should pass; got reason: {reason}"


# ---------- Set 2: Known false positives MUST fail ----------


def test_historymemes_by_jove_kills_on_title_term(rules: DisambigRules) -> None:
    """r/HistoryMemes 'No flag? ... By jove, it's free real estate!'

    The kill term ``"by jove"`` matches in the title before the subreddit
    blacklist gets a chance — that's fine, both stages are hard-kills.
    """

    thread = _thread(
        subreddit="HistoryMemes",
        title="No flag? Valuable treasures? By jove, it's free real estate!",
        body=None,
        comments=["lmao Romans were ruthless"],
    )

    in_scope, reason = is_in_scope(thread, rules)
    assert in_scope is False
    # Confirm one of the kill paths fired — exact order may be title-first.
    assert reason.startswith("killed_by_title_term:") or reason == "subreddit_blacklist"


def test_stardustcrusaders_jojos_bizarre_oc_tournament(rules: DisambigRules) -> None:
    """r/StardustCrusaders 'Jojo's Bizarre OC Tournament' — JoJo fandom, not JoVE."""

    thread = _thread(
        subreddit="StardustCrusaders",
        title="Jojo's Bizarre OC Tournament",
        body="Round 3 of the OC tournament! Vote in the comments.",
        comments=["My favorite stand is...", "JoJo references everywhere"],
    )

    in_scope, reason = is_in_scope(thread, rules)
    assert in_scope is False
    assert reason.startswith("killed_by_title_term:") or reason == "subreddit_blacklist"


def test_names_subreddit_picking_a_first_name(rules: DisambigRules) -> None:
    """r/Names 'middle name Jo' — name-shopping, not a journal mention."""

    thread = _thread(
        subreddit="Names",
        title="Need help picking a girl first name that goes with the middle name Jo",
        body="Looking for suggestions for a first name to pair with Jo.",
        comments=["How about Mary Jo?", "I like Sarah Jo"],
    )

    in_scope, reason = is_in_scope(thread, rules)
    assert in_scope is False
    assert reason.startswith("killed_by_title_term:") or reason == "subreddit_blacklist"


def test_funny_subreddit_no_jove_context(rules: DisambigRules) -> None:
    """r/funny 'Local hardware store has this posted' — blacklisted sub, no signal."""

    thread = _thread(
        subreddit="funny",
        title="Local hardware store has this posted",
        body="Found this on the door at Joe's hardware.",
        comments=["lol", "classic"],
    )

    in_scope, reason = is_in_scope(thread, rules)
    assert in_scope is False
    # r/funny is in the blacklist; that should fire before signal check.
    assert reason == "subreddit_blacklist"


def test_jove_band_thread_kills_on_title_term(rules: DisambigRules) -> None:
    """A thread mentioning ``Jove (band)`` should die on the kill term."""

    thread = _thread(
        subreddit="indieheads",
        title="Anyone listened to Jove (band)? Their new album slaps",
        body="Indie rock band Jove dropped a record last week.",
        comments=["love their sound"],
    )

    in_scope, reason = is_in_scope(thread, rules)
    assert in_scope is False
    assert reason.startswith("killed_by_title_term:jove (band)")


def test_jove_fm_music_kills_on_title_term(rules: DisambigRules) -> None:
    """A thread mentioning ``jove.fm`` (music site) should die on the kill term."""

    thread = _thread(
        subreddit="WeAreTheMusicMakers",
        title="Uploading my tracks to jove.fm — worth it?",
        body="The platform jove.fm just opened submissions.",
        comments=["I tried it last year"],
    )

    in_scope, reason = is_in_scope(thread, rules)
    assert in_scope is False
    assert reason.startswith("killed_by_title_term:jove.fm")


# ---------- Set 3: Subreddit-context edge cases ----------


def test_academia_core_sub_passes_on_signal_alone(rules: DisambigRules) -> None:
    """r/AskAcademia + ``JoVE`` in title + zero context terms → pass."""

    thread = _thread(
        subreddit="AskAcademia",
        title="JoVE",
        body=None,
        comments=[],
    )

    in_scope, reason = is_in_scope(thread, rules)
    assert in_scope is True, f"Academia-core bypass should fire; got: {reason}"
    assert reason == "passed"


def test_ambiguous_sub_with_signal_but_no_context_fails(rules: DisambigRules) -> None:
    """r/programming + ``JoVE`` in title, no context terms → fail on Stage 4."""

    thread = _thread(
        subreddit="programming",
        title="JoVE has a cool video player",
        body=None,
        comments=["interesting"],
    )

    in_scope, reason = is_in_scope(thread, rules)
    assert in_scope is False
    assert reason == "no_context_in_ambiguous_sub"


def test_ambiguous_sub_with_signal_and_context_passes(rules: DisambigRules) -> None:
    """r/programming + ``JoVE`` in title + ``journal`` / ``peer review`` in body → pass."""

    thread = _thread(
        subreddit="programming",
        title="JoVE has a cool video player",
        body=(
            "Looking at the JoVE journal site — they're peer-reviewed and "
            "the paper viewer is built well."
        ),
        comments=[],
    )

    in_scope, reason = is_in_scope(thread, rules)
    assert in_scope is True, f"Context terms should unlock pass; got: {reason}"
    assert reason == "passed"


def test_no_positive_signal_fails(rules: DisambigRules) -> None:
    """A thread with no JoVE signal anywhere — even in an academia sub — fails Stage 3."""

    thread = _thread(
        subreddit="AskAcademia",
        title="Best journals for sociology methods papers?",
        body="Looking for recommendations on methods-oriented sociology venues.",
        comments=[
            "Try Sociological Methods & Research",
            "Sage publishes a few good ones",
        ],
    )

    in_scope, reason = is_in_scope(thread, rules)
    assert in_scope is False
    assert reason == "no_positive_signal"


# ---------- Set 4: Reason-string contracts ----------


def test_reason_string_subreddit_blacklist_is_exact(rules: DisambigRules) -> None:
    """``subreddit_blacklist`` reason is exactly that string — downstream code keys on it."""

    thread = _thread(
        subreddit="funny",
        title="Some completely unrelated meme",
        body="No JoVE here at all.",
        comments=[],
    )

    _, reason = is_in_scope(thread, rules)
    assert reason == "subreddit_blacklist"


def test_reason_string_no_positive_signal_is_exact(rules: DisambigRules) -> None:
    """``no_positive_signal`` reason is exactly that string."""

    thread = _thread(
        subreddit="AskAcademia",
        title="How to write a methods paper",
        body="Just general advice please.",
        comments=["use clear language"],
    )

    _, reason = is_in_scope(thread, rules)
    assert reason == "no_positive_signal"


def test_reason_string_no_context_in_ambiguous_sub_is_exact(
    rules: DisambigRules,
) -> None:
    """``no_context_in_ambiguous_sub`` reason is exactly that string."""

    thread = _thread(
        subreddit="programming",
        title="JoVE",
        body=None,
        comments=[],
    )

    _, reason = is_in_scope(thread, rules)
    assert reason == "no_context_in_ambiguous_sub"


def test_reason_string_killed_by_title_term_is_prefixed(
    rules: DisambigRules,
) -> None:
    """``killed_by_title_term:<term>`` includes the lowercased term that matched."""

    thread = _thread(
        subreddit="indieheads",
        title="The new Jove (album) is amazing",
        body=None,
        comments=[],
    )

    _, reason = is_in_scope(thread, rules)
    assert reason.startswith("killed_by_title_term:")
    assert "jove (album)" in reason


def test_reason_string_passed_is_exact(rules: DisambigRules) -> None:
    """``passed`` reason is exactly that string — the success contract."""

    thread = _thread(
        subreddit="AskAcademia",
        title="JoVE is this legit?",
        body="JoVE journal peer-review paper.",
        comments=[],
    )

    in_scope, reason = is_in_scope(thread, rules)
    assert in_scope is True
    assert reason == "passed"


# ---------- Idempotency check ----------


def test_is_in_scope_is_idempotent(rules: DisambigRules) -> None:
    """Same Thread + same rules → same (in_scope, reason) every call.

    Catches accidental state mutation in :class:`DisambigRules` or the
    haystack-building hot path.
    """

    thread = _thread(
        subreddit="AskAcademia",
        title="JoVE: is this legit?",
        body="JoVE journal peer review paper",
        comments=["JoVE is fine"],
    )

    results = [is_in_scope(thread, rules) for _ in range(5)]
    assert all(r == results[0] for r in results)


def test_default_rules_singleton_works_without_explicit_rules() -> None:
    """``is_in_scope(thread)`` (no rules arg) uses the lazy-singleton.

    Smoke-test: covers the production code path the backfill loop uses.
    """

    thread = _thread(
        subreddit="AskAcademia",
        title="JoVE",
        body=None,
        comments=[],
    )

    in_scope, reason = is_in_scope(thread)
    assert in_scope is True
    assert reason == "passed"
