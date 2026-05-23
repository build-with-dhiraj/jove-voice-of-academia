"""Tests for :mod:`pipeline.fetch`.

Three layers:

1. **Unit tests** with PRAW + Firecrawl stubs — full thread hydration,
   deleted-body exclusion, deleted-author exclusion, retry exhaustion ⇒
   fallback to Firecrawl, parent_id and depth wiring.
2. **Rate-limiter tests** — mock ``time.sleep``/``time.monotonic`` and verify
   the limiter spaces requests at the configured cadence.
3. **Integration test** (skipped by default) — fetch r/AskAcademia post
   ``95pq3y`` ("Just got a JoVE request") and confirm shape.

Run unit + rate-limiter only::

    uv run pytest tests/pipeline/test_fetch.py -v

Include the live integration test::

    uv run pytest tests/pipeline/test_fetch.py -v -m integration
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import prawcore.exceptions
import pytest

import pipeline.fetch as fetch_module
from pipeline.fetch import (
    RateLimiter,
    _comment_to_model,
    _is_deleted_body,
    _json_url_from_permalink,
    _stringify_author,
    _thread_from_reddit_json,
    fetch_thread,
)
from pipeline.models import Thread, ThreadCandidate


@pytest.fixture(autouse=True)
def _reset_module_rate_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the module-level rate limiter with a fresh, no-op-fast one per test.

    Without this, each test pays the 10-second-per-call rate-limit tax because
    the module-level ``_RATE_LIMITER`` persists ``_last_call`` between tests.
    Tests that specifically want to exercise the limiter override this fixture's
    behaviour by patching ``pipeline.fetch._RATE_LIMITER`` themselves *after*
    this fixture runs.

    We use a no-op limiter (a stub whose ``wait()`` does nothing) rather than
    just resetting state — this keeps the unit tests below ~1s total.
    """

    class _NoopLimiter:
        def wait(self) -> None:
            pass

    monkeypatch.setattr(fetch_module, "_RATE_LIMITER", _NoopLimiter())


# ---------- Helpers / stubs ----------


class _StubAuthor:
    """Mimics PRAW's Redditor — ``str(author)`` yields the display name."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __str__(self) -> str:
        return self._name


class _StubSubreddit:
    """Mimics PRAW's Subreddit — only ``display_name`` is touched."""

    def __init__(self, name: str) -> None:
        self.display_name = name


class _StubComment:
    """Mimics enough of PRAW's Comment for :func:`_comment_to_model`."""

    def __init__(
        self,
        *,
        fullname: str = "t1_abc",
        parent_id: str = "t3_xyz",
        permalink: str = "/r/AskAcademia/comments/xyz/_/abc/",
        body: str = "hello",
        author: Any = None,
        created_utc: float = 1_500_000_000.0,
        score: int = 1,
        depth: int = 0,
    ) -> None:
        self.fullname = fullname
        self.parent_id = parent_id
        self.permalink = permalink
        self.body = body
        self.author = author if author is not None else _StubAuthor("u_test")
        self.created_utc = created_utc
        self.score = score
        self.depth = depth


class _StubMoreComments:
    """Mimics PRAW's MoreComments — we never read it; it should be replaced."""

    body = "(more)"


class _StubCommentForest:
    """Mimics PRAW's CommentForest — ``replace_more`` + ``list``."""

    def __init__(self, comments: list[Any]) -> None:
        self._comments = comments
        self.replace_more_call_count = 0
        self.last_replace_more_limit: int | None | object = object()

    def replace_more(self, limit: int | None = 0) -> None:
        self.replace_more_call_count += 1
        self.last_replace_more_limit = limit

    def list(self) -> list[Any]:
        return self._comments


class _StubSubmission:
    """Mimics PRAW's Submission well enough for ``_fetch_via_praw``."""

    def __init__(
        self,
        *,
        fullname: str = "t3_xyz",
        permalink: str = "/r/AskAcademia/comments/xyz/sample_post/",
        subreddit: str = "AskAcademia",
        title: str = "Sample post",
        selftext: str = "Body of the post.",
        is_self: bool = True,
        author: Any = None,
        created_utc: float = 1_500_000_000.0,
        score: int = 42,
        num_comments: int = 3,
        comments: list[Any] | None = None,
    ) -> None:
        self.fullname = fullname
        self.permalink = permalink
        self.subreddit = _StubSubreddit(subreddit)
        self.title = title
        self.selftext = selftext
        self.is_self = is_self
        self.author = author if author is not None else _StubAuthor("u_op")
        self.created_utc = created_utc
        self.score = score
        self.num_comments = num_comments
        self.comments = _StubCommentForest(comments or [])


class _StubReddit:
    """Mimics PRAW's Reddit — returns a pre-baked submission for any URL."""

    def __init__(self, submission: _StubSubmission) -> None:
        self._submission = submission
        self.last_url: str | None = None

    def submission(self, *, url: str) -> _StubSubmission:
        self.last_url = url
        return self._submission


def _make_candidate(
    permalink: str = "https://www.reddit.com/r/AskAcademia/comments/xyz/sample_post/",
) -> ThreadCandidate:
    return ThreadCandidate(
        permalink=permalink,
        subreddit="AskAcademia",
        discovered_via="ring1_subreddit_enum",
        discovered_at=datetime.now(timezone.utc),
        seed_query=None,
    )


# ---------- _is_deleted_body / _stringify_author ----------


@pytest.mark.parametrize(
    "body,expected",
    [
        ("hello", False),
        ("[deleted]", True),
        ("[removed]", True),
        ("", False),  # An empty body is "vacuous", not "deleted"
        (None, True),
    ],
)
def test_is_deleted_body_matches_sentinels(body: str | None, expected: bool) -> None:
    assert _is_deleted_body(body) is expected


def test_stringify_author_returns_name() -> None:
    assert _stringify_author(_StubAuthor("alice")) == "alice"


def test_stringify_author_returns_none_for_none() -> None:
    assert _stringify_author(None) is None


# ---------- _comment_to_model ----------


def test_comment_to_model_populates_all_fields() -> None:
    praw_c = _StubComment(
        fullname="t1_abc123",
        parent_id="t3_xyz",
        permalink="/r/AskAcademia/comments/xyz/_/abc123/",
        body="This is a comment.",
        author=_StubAuthor("alice"),
        created_utc=1_700_000_000.0,
        score=5,
        depth=1,
    )
    c = _comment_to_model(praw_c)
    assert c is not None
    assert c.id == "t1_abc123"
    assert c.parent_id == "t3_xyz"
    assert c.permalink == "https://www.reddit.com/r/AskAcademia/comments/xyz/_/abc123/"
    assert c.body == "This is a comment."
    assert c.author == "alice"
    assert c.created_utc.tzinfo is not None  # tz-aware
    assert c.score == 5
    assert c.depth == 1


def test_comment_to_model_excludes_deleted_body() -> None:
    praw_c = _StubComment(body="[deleted]")
    assert _comment_to_model(praw_c) is None


def test_comment_to_model_excludes_removed_body() -> None:
    praw_c = _StubComment(body="[removed]")
    assert _comment_to_model(praw_c) is None


def test_comment_to_model_excludes_deleted_author() -> None:
    """Per D12: ``author is None`` ⇒ account deleted ⇒ drop the comment."""

    praw_c = _StubComment(body="still here", author=None)
    # Note: passing author=None to the constructor would trigger the default;
    # set it explicitly after construction to mimic PRAW's deleted-account state.
    praw_c.author = None
    assert _comment_to_model(praw_c) is None


# ---------- fetch_thread (PRAW happy path) ----------


def test_fetch_thread_returns_hydrated_thread_with_all_fields() -> None:
    """End-to-end with a stubbed PRAW client — verify every field on Thread."""

    comments = [
        _StubComment(
            fullname="t1_c1",
            parent_id="t3_xyz",
            body="First comment",
            depth=0,
            score=10,
            created_utc=1_700_000_100.0,
        ),
        _StubComment(
            fullname="t1_c2",
            parent_id="t1_c1",
            body="A reply",
            depth=1,
            score=3,
            created_utc=1_700_000_200.0,
        ),
    ]
    submission = _StubSubmission(
        fullname="t3_xyz",
        permalink="/r/AskAcademia/comments/xyz/sample_post/",
        subreddit="AskAcademia",
        title="Sample post",
        selftext="Body of the post.",
        is_self=True,
        author=_StubAuthor("u_op"),
        created_utc=1_700_000_000.0,
        score=42,
        num_comments=2,
        comments=comments,
    )
    reddit = _StubReddit(submission)
    candidate = _make_candidate()

    thread = fetch_thread(candidate, reddit=reddit)

    assert isinstance(thread, Thread)
    assert thread.id == "t3_xyz"
    assert thread.permalink == "https://www.reddit.com/r/AskAcademia/comments/xyz/sample_post/"
    assert thread.subreddit == "AskAcademia"
    assert thread.title == "Sample post"
    assert thread.body == "Body of the post."
    assert thread.author == "u_op"
    assert thread.created_utc.tzinfo is not None
    assert thread.score == 42
    assert thread.num_comments == 2
    assert thread.is_self is True
    assert len(thread.comments) == 2
    assert thread.comments[0].id == "t1_c1"
    assert thread.comments[1].id == "t1_c2"
    # The PRAW client received the candidate's permalink as the URL arg.
    assert reddit.last_url == candidate.permalink
    # And replace_more(limit=None) was invoked (full expansion).
    assert submission.comments.replace_more_call_count == 1
    assert submission.comments.last_replace_more_limit is None


def test_fetch_thread_link_post_has_none_body() -> None:
    """Link posts have ``is_self=False`` ⇒ ``body`` should be None."""

    submission = _StubSubmission(is_self=False, selftext="")
    reddit = _StubReddit(submission)
    thread = fetch_thread(_make_candidate(), reddit=reddit)
    assert thread.is_self is False
    assert thread.body is None


def test_fetch_thread_excludes_deleted_comments_from_output() -> None:
    """Mixed comments — deleted bodies and deleted authors are dropped."""

    comments = [
        _StubComment(fullname="t1_keep1", body="good", depth=0),
        _StubComment(fullname="t1_drop_deleted", body="[deleted]", depth=0),
        _StubComment(fullname="t1_drop_removed", body="[removed]", depth=0),
        _StubComment(fullname="t1_keep2", body="also good", depth=0),
    ]
    submission = _StubSubmission(comments=comments)
    reddit = _StubReddit(submission)
    thread = fetch_thread(_make_candidate(), reddit=reddit)

    kept_ids = [c.id for c in thread.comments]
    assert kept_ids == ["t1_keep1", "t1_keep2"]
    # The deleted bodies should not leak through verbatim either.
    assert all(c.body not in ("[deleted]", "[removed]") for c in thread.comments)


def test_fetch_thread_excludes_deleted_author_comments() -> None:
    """A comment with ``author=None`` (account deleted) is excluded."""

    keep = _StubComment(fullname="t1_keep", body="still here", author=_StubAuthor("alice"))
    drop = _StubComment(fullname="t1_drop", body="orphaned text")
    drop.author = None  # Force the deleted-account case post-construction.

    submission = _StubSubmission(comments=[keep, drop])
    reddit = _StubReddit(submission)
    thread = fetch_thread(_make_candidate(), reddit=reddit)
    assert [c.id for c in thread.comments] == ["t1_keep"]


# ---------- Submission-author deleted ⇒ thread.author is None ----------


def test_fetch_thread_submission_author_deleted_becomes_none() -> None:
    """OP account deletion ⇒ ``Thread.author == None`` (not the string)."""

    submission = _StubSubmission()
    submission.author = None
    reddit = _StubReddit(submission)
    thread = fetch_thread(_make_candidate(), reddit=reddit)
    assert thread.author is None


# ---------- Rate limiter ----------


def test_rate_limiter_sleeps_when_called_too_soon(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second call within the minimum interval triggers ``time.sleep``."""

    sleeps: list[float] = []
    monkeypatch.setattr("pipeline.fetch.time.sleep", lambda s: sleeps.append(s))

    # Fake monotonic clock — call N returns ``clock_vals[N]``.
    clock_vals = iter([0.0, 1.0, 11.0])  # 1s gap, then 10s gap
    monkeypatch.setattr("pipeline.fetch.time.monotonic", lambda: next(clock_vals))

    # 6 req/min ⇒ 10s spacing.
    limiter = RateLimiter(6)
    limiter.wait()  # First call: clock=0.0, no sleep
    limiter.wait()  # Second call: clock=1.0, deficit=9s → sleep(9), then clock=11.0

    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(9.0)


def test_rate_limiter_first_call_does_not_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first call falls through immediately — there is no previous time."""

    sleeps: list[float] = []
    monkeypatch.setattr("pipeline.fetch.time.sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr("pipeline.fetch.time.monotonic", lambda: 0.0)

    RateLimiter(6).wait()
    assert sleeps == []


def test_rate_limiter_no_sleep_when_interval_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    """If enough time has elapsed naturally, no sleep is added."""

    sleeps: list[float] = []
    monkeypatch.setattr("pipeline.fetch.time.sleep", lambda s: sleeps.append(s))
    clock_vals = iter([0.0, 100.0])  # 100s gap, well beyond 10s spacing
    monkeypatch.setattr("pipeline.fetch.time.monotonic", lambda: next(clock_vals))

    limiter = RateLimiter(6)
    limiter.wait()
    limiter.wait()
    assert sleeps == []


def test_rate_limiter_rejects_non_positive_rate() -> None:
    """A zero or negative rate is a programmer error, not a runtime condition."""

    with pytest.raises(ValueError):
        RateLimiter(0)
    with pytest.raises(ValueError):
        RateLimiter(-1)


def test_rate_limiter_engaged_in_fetch_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies the module-level limiter is invoked during a successful fetch.

    Specifically: two ``_RATE_LIMITER.wait`` calls — one for the submission
    fetch, one for the comment-tree expansion. We swap in a fresh limiter
    with a stub clock; if the limiter is wired correctly the stub clock will
    have been touched (we count ``time.monotonic`` calls).
    """

    sleeps: list[float] = []
    monkeypatch.setattr("pipeline.fetch.time.sleep", lambda s: sleeps.append(s))

    monotonic_calls: list[float] = []

    # Always advance the clock past the spacing window
    big_clock = iter(range(0, 10_000, 1000))

    def fake_monotonic() -> float:
        val = float(next(big_clock))
        monotonic_calls.append(val)
        return val

    monkeypatch.setattr("pipeline.fetch.time.monotonic", fake_monotonic)
    # Replace the module-level limiter with a fresh one whose ``_last_call``
    # is None (we want to test wiring, not residual state from other tests).
    monkeypatch.setattr("pipeline.fetch._RATE_LIMITER", RateLimiter(6))

    submission = _StubSubmission(comments=[_StubComment(fullname="t1_a")])
    reddit = _StubReddit(submission)
    fetch_thread(_make_candidate(), reddit=reddit)

    # The limiter should have invoked time.monotonic at least twice (once per
    # rate-limited call site: submission fetch + comment expansion).
    assert len(monotonic_calls) >= 2
    # Clock is gapped wide and the first call has no prior timestamp ⇒
    # no sleeps fired.
    assert sleeps == []


# ---------- PRAW failure → Firecrawl fallback ----------


def _reddit_json_payload_for(thread_id: str = "abc", num_comments: int = 1) -> list[dict]:
    """Build a minimal Reddit ``.json`` payload that ``_thread_from_reddit_json`` accepts."""

    return [
        {
            "kind": "Listing",
            "data": {
                "children": [
                    {
                        "kind": "t3",
                        "data": {
                            "id": thread_id,
                            "permalink": f"/r/AskAcademia/comments/{thread_id}/sample/",
                            "subreddit": "AskAcademia",
                            "title": "Recovered via Firecrawl",
                            "selftext": "Body text",
                            "is_self": True,
                            "author": "u_op",
                            "created_utc": 1_700_000_000.0,
                            "score": 7,
                            "num_comments": num_comments,
                        },
                    }
                ]
            },
        },
        {
            "kind": "Listing",
            "data": {
                "children": [
                    {
                        "kind": "t1",
                        "data": {
                            "id": "c1",
                            "parent_id": f"t3_{thread_id}",
                            "permalink": f"/r/AskAcademia/comments/{thread_id}/sample/c1/",
                            "body": "JSON-fallback comment",
                            "author": "u_replier",
                            "created_utc": 1_700_000_100.0,
                            "score": 4,
                            "replies": "",  # empty replies — common on shallow comments
                        },
                    },
                    {
                        "kind": "more",  # MoreComments stub — should be skipped silently
                        "data": {"id": "more1", "children": ["c2", "c3"]},
                    },
                    {
                        "kind": "t1",
                        "data": {
                            "id": "c_deleted",
                            "parent_id": f"t3_{thread_id}",
                            "permalink": f"/r/AskAcademia/comments/{thread_id}/sample/cdel/",
                            "body": "[deleted]",
                            "author": "[deleted]",
                            "created_utc": 1_700_000_200.0,
                            "score": 0,
                            "replies": "",
                        },
                    },
                ]
            },
        },
    ]


class _StubFirecrawlDoc:
    def __init__(self, raw_html: str) -> None:
        self.raw_html = raw_html
        self.markdown = None
        self.html = None


class _StubFirecrawl:
    def __init__(self, raw_html: str) -> None:
        self._raw_html = raw_html
        self.last_url: str | None = None
        self.last_formats: list[str] | None = None

    def scrape(self, url: str, *, formats: list[str] | None = None, **kwargs: Any) -> _StubFirecrawlDoc:
        self.last_url = url
        self.last_formats = formats
        return _StubFirecrawlDoc(self._raw_html)


class _RaisingReddit:
    """Reddit stub whose ``submission()`` raises a 404 (deleted thread case)."""

    def __init__(self) -> None:
        self.calls: int = 0

    def submission(self, *, url: str) -> Any:
        self.calls += 1
        # Build a NotFound that doesn't require a real response object.
        raise prawcore.exceptions.NotFound(_FakeResponse())


class _FakeResponse:
    status_code = 404


def test_fetch_thread_falls_back_to_firecrawl_on_praw_failure() -> None:
    """When PRAW raises a non-retryable error, Firecrawl reconstructs the thread."""

    import json

    payload = _reddit_json_payload_for(thread_id="abc")
    fc = _StubFirecrawl(raw_html=json.dumps(payload))
    candidate = _make_candidate(
        permalink="https://www.reddit.com/r/AskAcademia/comments/abc/sample/"
    )

    thread = fetch_thread(candidate, reddit=_RaisingReddit(), fc_client=fc)

    assert thread.id == "t3_abc"
    assert thread.title == "Recovered via Firecrawl"
    assert thread.subreddit == "AskAcademia"
    # Only the public, non-deleted comment makes it through.
    assert [c.id for c in thread.comments] == ["t1_c1"]
    assert thread.comments[0].body == "JSON-fallback comment"
    # Firecrawl was hit at the .json URL.
    assert fc.last_url == "https://www.reddit.com/r/AskAcademia/comments/abc/sample.json"
    assert fc.last_formats == ["rawHtml"]


def test_fetch_thread_reraises_when_praw_fails_and_no_fc(monkeypatch: pytest.MonkeyPatch) -> None:
    """No Firecrawl client + no API key ⇒ PRAW errors propagate."""

    monkeypatch.setattr("pipeline.fetch.FIRECRAWL_API_KEY", None)
    with pytest.raises(prawcore.exceptions.NotFound):
        fetch_thread(_make_candidate(), reddit=_RaisingReddit(), fc_client=None)


# ---------- _thread_from_reddit_json (Firecrawl payload parser) ----------


def test_thread_from_reddit_json_skips_more_stubs_and_deleted() -> None:
    """JSON payload with one good comment, one 'more' stub, one deleted ⇒ 1 comment."""

    payload = _reddit_json_payload_for("abc", num_comments=3)
    thread = _thread_from_reddit_json(payload)
    assert thread.num_comments == 3  # reported count preserved
    assert len(thread.comments) == 1  # but our filtered list has only 1
    assert thread.comments[0].body == "JSON-fallback comment"


def test_thread_from_reddit_json_handles_nested_replies() -> None:
    """A reply nested under a top-level comment lifts depth=1 properly."""

    payload = [
        {
            "kind": "Listing",
            "data": {
                "children": [
                    {
                        "kind": "t3",
                        "data": {
                            "id": "x1",
                            "permalink": "/r/AskAcademia/comments/x1/n/",
                            "subreddit": "AskAcademia",
                            "title": "Nested test",
                            "selftext": "",
                            "is_self": True,
                            "author": "u_op",
                            "created_utc": 1.0,
                            "score": 1,
                            "num_comments": 2,
                        },
                    }
                ]
            },
        },
        {
            "kind": "Listing",
            "data": {
                "children": [
                    {
                        "kind": "t1",
                        "data": {
                            "id": "top",
                            "parent_id": "t3_x1",
                            "permalink": "/r/AskAcademia/comments/x1/n/top/",
                            "body": "Top level",
                            "author": "u_a",
                            "created_utc": 2.0,
                            "score": 1,
                            "replies": {
                                "kind": "Listing",
                                "data": {
                                    "children": [
                                        {
                                            "kind": "t1",
                                            "data": {
                                                "id": "reply",
                                                "parent_id": "t1_top",
                                                "permalink": "/r/AskAcademia/comments/x1/n/reply/",
                                                "body": "A reply",
                                                "author": "u_b",
                                                "created_utc": 3.0,
                                                "score": 1,
                                                "replies": "",
                                            },
                                        }
                                    ]
                                },
                            },
                        },
                    }
                ]
            },
        },
    ]
    thread = _thread_from_reddit_json(payload)
    assert [c.id for c in thread.comments] == ["t1_top", "t1_reply"]
    assert [c.depth for c in thread.comments] == [0, 1]


def test_json_url_from_permalink_appends_dot_json() -> None:
    base = "https://www.reddit.com/r/AskAcademia/comments/abc/sample/"
    assert _json_url_from_permalink(base) == "https://www.reddit.com/r/AskAcademia/comments/abc/sample.json"

    base_no_slash = "https://www.reddit.com/r/AskAcademia/comments/abc/sample"
    assert _json_url_from_permalink(base_no_slash) == "https://www.reddit.com/r/AskAcademia/comments/abc/sample.json"


# ---------- Integration (skipped by default) ----------


@pytest.mark.integration
def test_fetch_thread_against_live_askacademia_95pq3y() -> None:
    """Hit a small real seed thread and verify the fetched shape.

    Run with::

        uv run pytest tests/pipeline/test_fetch.py -v -m integration

    Uses the smallest seed thread ('Just got a JoVE request') to keep
    integration runtime under PRAW unauth rate limits manageable.
    """

    candidate = ThreadCandidate(
        permalink="https://www.reddit.com/r/AskAcademia/comments/95pq3y/just_got_a_jove_request/",
        subreddit="AskAcademia",
        discovered_via="ring1_subreddit_enum",
        discovered_at=datetime.now(timezone.utc),
        seed_query=None,
    )

    thread = fetch_thread(candidate)
    assert isinstance(thread, Thread)
    assert thread.subreddit == "AskAcademia"
    assert thread.id.startswith("t3_")
    assert thread.title  # non-empty
    assert thread.created_utc.year >= 2018  # 95pq3y was 2018
    # At least one public comment in our output
    assert len(thread.comments) >= 1
    # And no deleted/removed leaked through the filter
    for c in thread.comments:
        assert c.body not in ("[deleted]", "[removed]")
        assert c.author is not None
        assert c.id.startswith("t1_")
        assert c.depth >= 0
