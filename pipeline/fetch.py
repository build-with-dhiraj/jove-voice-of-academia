"""Thread + comment-tree fetching with deleted-content exclusion (Phase 3).

Given a :class:`pipeline.models.ThreadCandidate` produced by discovery
(Phase 2), :func:`fetch_thread` returns a fully-hydrated
:class:`pipeline.models.Thread` — the submission plus every public comment in
the tree.

Key behaviours
--------------

* **Auth mode** mirrors discovery: :func:`pipeline.config.reddit_auth_mode`
  picks the constructor. PRAW 7.8 unauth mode requires empty strings, not
  ``None``.
* **Full comment expansion** uses ``submission.comments.replace_more(limit=
  None)``. This is the slow, rate-limit-burning path — wrapped in tenacity
  retries with exponential backoff on the prawcore network exceptions.
* **Deleted/removed exclusion** is mandatory per the spec's D12 Reddit ToS
  posture. Comments whose ``body`` is ``[deleted]`` / ``[removed]`` or whose
  ``author`` is ``None`` (deleted account) are dropped from the output. The
  same rule applies to the submission's ``author`` field — kept as ``None``
  rather than the string ``"[deleted]"``, so downstream code can distinguish.
* **Rate limiting** is enforced by an in-process token-spacing limiter pegged
  to :data:`pipeline.config.RATE_LIMIT_REQ_PER_MIN`. PRAW does its own
  rate-limiting internally, but having a second guard rail at the call-site
  protects against bursts and makes the behaviour observable in tests.
* **Firecrawl fallback**: when PRAW raises for a thread (deleted submission,
  suspended user, rare 5xx that exhausts retries), the function falls back to
  fetching the canonical ``.json`` URL through Firecrawl's scrape API and
  rebuilds the :class:`Thread` from that payload. This is a *safety net* —
  the vast majority of fetches will succeed via PRAW. Firecrawl is consulted
  only when a client was passed in (or ``FIRECRAWL_API_KEY`` is set so one
  can be constructed); without it, PRAW failures propagate.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import prawcore.exceptions
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from pipeline.config import (
    FIRECRAWL_API_KEY,
    RATE_LIMIT_REQ_PER_MIN,
    REDDIT_CLIENT_ID,
    REDDIT_CLIENT_SECRET,
    REDDIT_USER_AGENT,
    reddit_auth_mode,
)
from pipeline.models import Comment, Thread, ThreadCandidate

if TYPE_CHECKING:  # pragma: no cover - typing only
    import praw

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Thread-safe, sleep-based fixed-rate limiter.

    Maintains a minimum spacing of ``60 / requests_per_minute`` seconds
    between calls to :meth:`wait`. Uses ``time.monotonic`` for the clock
    (not affected by wall-clock adjustments) and ``time.sleep`` for the
    pause. Both are patched in tests to verify the limiter is engaged
    without actually waiting.

    The PRAW client already does internal rate-limiting based on response
    headers; this is a belt-and-braces second guard at the call site. It
    also makes the behaviour observable from outside PRAW so we can assert
    it in tests.
    """

    def __init__(self, requests_per_minute: int) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        self._min_interval: float = 60.0 / float(requests_per_minute)
        self._last_call: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Block until enough time has elapsed since the last :meth:`wait` call."""

        with self._lock:
            now = time.monotonic()
            if self._last_call is not None:
                elapsed = now - self._last_call
                deficit = self._min_interval - elapsed
                if deficit > 0:
                    time.sleep(deficit)
                    now = time.monotonic()
            self._last_call = now


# Module-level limiter — one instance protects all calls in this process.
_RATE_LIMITER = RateLimiter(RATE_LIMIT_REQ_PER_MIN)


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


# Network/rate-limit error classes worth retrying. ``ResponseException`` is the
# parent of ``TooManyRequests`` and ``ServerError``; we still list the
# children for clarity in the failure log.
_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    prawcore.exceptions.ServerError,
    prawcore.exceptions.RequestException,
    prawcore.exceptions.TooManyRequests,
)

_retry_network = retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
    reraise=True,
)


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


def _build_reddit_client() -> praw.Reddit:
    """Construct a PRAW Reddit client honouring v1's unauth-as-default posture.

    Same shape as :func:`pipeline.discover._build_reddit_client` — duplicated
    rather than imported to keep ``fetch`` independent of ``discover`` (they
    are sibling layers, not stacked).
    """

    import praw  # local import keeps test imports light

    if reddit_auth_mode() == "authenticated":
        logger.info("PRAW (fetch): authenticated mode (~100 req/min)")
        return praw.Reddit(
            client_id=REDDIT_CLIENT_ID,
            client_secret=REDDIT_CLIENT_SECRET,
            user_agent=REDDIT_USER_AGENT,
            check_for_async=False,
        )

    logger.warning(
        "PRAW (fetch): read-only/unauth mode (~10 req/min) — set "
        "REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET to switch to authenticated mode."
    )
    return praw.Reddit(
        client_id="",
        client_secret="",
        user_agent=REDDIT_USER_AGENT,
        check_for_async=False,
    )


def _build_firecrawl_client() -> Any | None:
    """Construct a Firecrawl client, or return ``None`` if the API key is unset.

    Used by the fallback path. The fallback is best-effort: if Firecrawl isn't
    configured, PRAW errors propagate unchanged.
    """

    if not FIRECRAWL_API_KEY:
        return None
    from firecrawl import Firecrawl

    return Firecrawl(api_key=FIRECRAWL_API_KEY)


# ---------------------------------------------------------------------------
# Deleted-content predicates
# ---------------------------------------------------------------------------


def _is_deleted_body(body: str | None) -> bool:
    """``True`` if Reddit's body sentinel indicates the comment was removed.

    Reddit replaces the body of deleted/removed comments with the literal
    strings ``"[deleted]"`` (user-deleted) or ``"[removed]"`` (mod-removed).
    A ``None`` body is treated the same.
    """

    if body is None:
        return True
    return body in ("[deleted]", "[removed]")


def _stringify_author(author: Any) -> str | None:
    """Return ``str(author)`` or ``None`` when the account was deleted.

    PRAW's ``Comment.author`` and ``Submission.author`` are ``Redditor``
    instances (or ``None`` for deleted accounts). ``str(redditor)`` yields
    the display name.
    """

    if author is None:
        return None
    return str(author)


# ---------------------------------------------------------------------------
# Comment + Thread construction from PRAW objects
# ---------------------------------------------------------------------------


def _comment_to_model(praw_comment: Any) -> Comment | None:
    """Convert one PRAW comment to a :class:`Comment`, or ``None`` if excluded.

    The ``None`` return signals "drop this comment per D12 deleted-content
    rule" — the caller filters them out. We don't raise because deleted
    comments are an expected, frequent, non-error case.
    """

    if _is_deleted_body(getattr(praw_comment, "body", None)):
        return None
    author = _stringify_author(getattr(praw_comment, "author", None))
    if author is None:
        # Deleted-account exclusion. We rely on PRAW returning None for these;
        # if a future PRAW version returns a placeholder Redditor object we'll
        # need to revisit. For now: belt-and-braces against the documented case.
        return None

    return Comment(
        id=praw_comment.fullname,
        parent_id=praw_comment.parent_id,
        permalink=f"https://www.reddit.com{praw_comment.permalink}",
        body=praw_comment.body,
        author=author,
        created_utc=datetime.fromtimestamp(praw_comment.created_utc, tz=timezone.utc),
        score=praw_comment.score,
        depth=praw_comment.depth,
    )


def _submission_to_thread(praw_submission: Any, comments: list[Comment]) -> Thread:
    """Build a :class:`Thread` from a PRAW submission + already-filtered comments."""

    return Thread(
        id=praw_submission.fullname,
        permalink=f"https://www.reddit.com{praw_submission.permalink}",
        subreddit=praw_submission.subreddit.display_name,
        title=praw_submission.title,
        body=(praw_submission.selftext or None) if praw_submission.is_self else None,
        author=_stringify_author(getattr(praw_submission, "author", None)),
        created_utc=datetime.fromtimestamp(
            praw_submission.created_utc, tz=timezone.utc
        ),
        score=praw_submission.score,
        num_comments=praw_submission.num_comments,
        is_self=praw_submission.is_self,
        comments=comments,
    )


# ---------------------------------------------------------------------------
# PRAW fetch path
# ---------------------------------------------------------------------------


@_retry_network
def _praw_expand_and_collect(submission: Any) -> list[Comment]:
    """Fully expand the comment tree and return the public-comments-only list.

    Wrapped in tenacity retry for the rate-limit-burning bit. We do *one*
    rate-limiter wait at the call site before invoking this so the throttle
    accounts for each fetch attempt.
    """

    _RATE_LIMITER.wait()
    # ``limit=None`` means "expand every MoreComments stub, however deep".
    # This is the canonical PRAW pattern for fully hydrating a comment tree.
    submission.comments.replace_more(limit=None)
    comments: list[Comment] = []
    for praw_comment in submission.comments.list():
        modeled = _comment_to_model(praw_comment)
        if modeled is not None:
            comments.append(modeled)
    return comments


def _fetch_via_praw(candidate: ThreadCandidate, reddit: praw.Reddit) -> Thread:
    """Fetch and hydrate a Thread via PRAW.

    Raises whatever PRAW raises — the caller is responsible for fallback.
    """

    _RATE_LIMITER.wait()
    submission = reddit.submission(url=candidate.permalink)
    # Touch a lazy attribute to force the HTTP fetch of the submission body
    # under our retry/rate-limiter umbrella (PRAW's lazy attributes would
    # otherwise fire HTTP at random times).
    _ = submission.title
    comments = _praw_expand_and_collect(submission)
    return _submission_to_thread(submission, comments)


# ---------------------------------------------------------------------------
# Firecrawl fallback
# ---------------------------------------------------------------------------


def _json_url_from_permalink(permalink: str) -> str:
    """Convert a Reddit permalink to its ``.json`` equivalent.

    Reddit serves a JSON representation of any thread at
    ``<permalink>.json``. Strips a trailing slash if present so the suffix
    appends cleanly.
    """

    stripped = permalink.rstrip("/")
    return f"{stripped}.json"


def _thread_from_reddit_json(payload: list[dict[str, Any]]) -> Thread:
    """Build a :class:`Thread` from Reddit's ``.json`` payload shape.

    Reddit returns a two-element list: ``[submission_listing, comments_listing]``.
    Each listing has ``data.children``, each child has ``data`` with the
    actual fields. Comment trees are nested via ``data.replies`` which is
    another listing recursively. ``kind == "more"`` items are "load more"
    stubs from the JSON endpoint — we can't expand them without a second
    request, so the fallback path will miss the tail of very deep threads.
    That's an acknowledged limitation of the fallback; PRAW is the primary
    path precisely because it can chase ``MoreComments``.
    """

    submission_data = payload[0]["data"]["children"][0]["data"]
    comments_listing = payload[1]["data"]["children"]

    flat_comments: list[Comment] = []

    def _walk(children: list[dict[str, Any]], depth: int) -> None:
        for child in children:
            kind = child.get("kind")
            if kind != "t1":  # skip MoreComments stubs ("more")
                continue
            data = child["data"]
            if _is_deleted_body(data.get("body")):
                continue
            author_raw = data.get("author")
            # JSON endpoint returns the literal string "[deleted]" rather than null
            if author_raw in (None, "[deleted]"):
                continue
            flat_comments.append(
                Comment(
                    id=f"t1_{data['id']}",
                    parent_id=data.get("parent_id", ""),
                    permalink=f"https://www.reddit.com{data['permalink']}",
                    body=data["body"],
                    author=author_raw,
                    created_utc=datetime.fromtimestamp(
                        data["created_utc"], tz=timezone.utc
                    ),
                    score=data.get("score", 0),
                    depth=depth,
                )
            )
            replies = data.get("replies")
            if isinstance(replies, dict):
                _walk(replies["data"]["children"], depth + 1)

    _walk(comments_listing, depth=0)

    author_raw = submission_data.get("author")
    if author_raw == "[deleted]":
        author = None
    else:
        author = author_raw

    is_self = bool(submission_data.get("is_self", False))
    selftext = submission_data.get("selftext")
    body = selftext if (is_self and selftext) else None

    return Thread(
        id=f"t3_{submission_data['id']}",
        permalink=f"https://www.reddit.com{submission_data['permalink']}",
        subreddit=submission_data["subreddit"],
        title=submission_data["title"],
        body=body,
        author=author,
        created_utc=datetime.fromtimestamp(
            submission_data["created_utc"], tz=timezone.utc
        ),
        score=submission_data.get("score", 0),
        num_comments=submission_data.get("num_comments", len(flat_comments)),
        is_self=is_self,
        comments=flat_comments,
    )


def _fetch_via_firecrawl(
    candidate: ThreadCandidate, fc_client: Any
) -> Thread:
    """Fetch a thread by scraping its ``.json`` URL through Firecrawl.

    Best-effort fallback. The Firecrawl ``scrape`` API returns a
    :class:`firecrawl.v2.types.Document`; we ask for ``rawHtml`` so the JSON
    payload comes through unmolested. Some Firecrawl deployments return the
    JSON in ``.markdown`` instead — we try ``raw_html`` first, then
    ``markdown``, then ``html``.
    """

    json_url = _json_url_from_permalink(candidate.permalink)
    logger.info("Fetch: PRAW failed for %s, trying Firecrawl on %s", candidate.permalink, json_url)
    _RATE_LIMITER.wait()
    doc = fc_client.scrape(json_url, formats=["rawHtml"])

    # Try the raw_html field first — that's where Firecrawl puts the
    # untransformed page body, which for a .json endpoint is JSON-as-text.
    body_text: str | None = None
    for attr in ("raw_html", "markdown", "html"):
        candidate_text = getattr(doc, attr, None)
        if candidate_text:
            body_text = candidate_text
            break
    if not body_text:
        raise RuntimeError(
            f"Firecrawl returned an empty document for {json_url} (raw_html/markdown/html all empty)"
        )

    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Firecrawl payload for {json_url} is not JSON: {exc}"
        ) from exc

    if not isinstance(payload, list) or len(payload) < 2:
        raise RuntimeError(
            f"Unexpected Reddit .json shape for {json_url}: expected list of 2, got {type(payload).__name__}"
        )

    return _thread_from_reddit_json(payload)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_thread(
    candidate: ThreadCandidate,
    *,
    reddit: praw.Reddit | None = None,
    fc_client: Any | None = None,
) -> Thread:
    """Return a fully-hydrated :class:`Thread` for ``candidate``.

    Primary path is PRAW with full comment-tree expansion, retries, and
    deleted/removed content excluded per spec D12. If PRAW raises after
    exhausting retries (or with a non-retryable error like ``NotFound`` for
    a deleted submission), Firecrawl is tried as a fallback against the
    permalink's ``.json`` representation. If Firecrawl isn't configured, the
    PRAW error propagates.

    Both ``reddit`` and ``fc_client`` accept injected stubs for unit
    testing. In production, leave them ``None`` and the function builds
    real clients from env.
    """

    if reddit is None:
        reddit = _build_reddit_client()

    try:
        return _fetch_via_praw(candidate, reddit)
    except Exception as praw_exc:
        # We don't catch BaseException — keep KeyboardInterrupt et al. flowing.
        # The PRAW path already exhausted its tenacity retries by now; this
        # is the last-resort fallback.
        if fc_client is None:
            fc_client = _build_firecrawl_client()
        if fc_client is None:
            logger.error(
                "Fetch: PRAW failed for %s and no Firecrawl client available — re-raising",
                candidate.permalink,
            )
            raise
        try:
            return _fetch_via_firecrawl(candidate, fc_client)
        except Exception as fc_exc:
            logger.error(
                "Fetch: both PRAW and Firecrawl failed for %s — PRAW=%s; Firecrawl=%s",
                candidate.permalink,
                praw_exc,
                fc_exc,
            )
            raise fc_exc from praw_exc
