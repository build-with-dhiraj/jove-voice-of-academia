"""Unit tests for :mod:`pipeline.tag` (Phase 5 — Claude tagging).

Strategy: mock the ``anthropic.Anthropic`` client so unit tests run offline
and zero-cost. The integration test at the bottom hits the live Anthropic
API and is skipped unless ``pytest -m integration`` is passed.

Each unit test verifies one contract:

* multi-theme — comment expressing two ideas yields two theme assignments
  with different sentiments.
* voice candidate — Sonnet returns YES → ``voice_candidate is True``.
* emerging_other — when no canonical theme fits, fallback bucket is used.
* classification failure — both retries fail → default fallback fires
  with reason ``"classification_failed"`` and ``voice_candidate=False``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from anthropic import APIConnectionError

from pipeline.models import Comment, TaggedComment, Thread
from pipeline.tag import (
    HAIKU_MODEL,
    SONNET_MODEL,
    estimate_mega_backfill_cost,
    tag_comment,
)


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture(autouse=True)
def _isolate_cost_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect cost-log writes to a tmp file so unit tests don't pollute
    ``data/tag-cost-log.jsonl``."""

    log_path = tmp_path / "tag-cost-log.jsonl"
    monkeypatch.setattr("pipeline.tag.COST_LOG_PATH", log_path)
    return log_path


@pytest.fixture
def sample_thread() -> Thread:
    """A representative in-scope JoVE thread."""

    return Thread(
        id="t3_seedthread",
        permalink="https://www.reddit.com/r/labrats/comments/seedthread/is_jove_respectable/",
        subreddit="labrats",
        title="Is JoVE respectable?",
        body="Wondering whether to publish in JoVE.",
        author="op_user",
        created_utc=datetime(2025, 6, 1, tzinfo=timezone.utc),
        score=42,
        num_comments=10,
        is_self=True,
        comments=[],
    )


def _make_comment(body: str, *, comment_id: str = "t1_test01") -> Comment:
    return Comment(
        id=comment_id,
        parent_id="t3_seedthread",
        permalink=f"https://www.reddit.com/r/labrats/comments/seedthread/_/{comment_id}/",
        body=body,
        author="some_user",
        created_utc=datetime(2025, 6, 2, tzinfo=timezone.utc),
        score=12,
        depth=0,
    )


# =========================================================================
# Mock Anthropic client helpers
# =========================================================================


def _tool_use_message(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    input_tokens: int = 8500,
    output_tokens: int = 200,
) -> SimpleNamespace:
    """Build a fake :class:`anthropic.types.Message` that returned a tool_use.

    SimpleNamespace mimics the SDK's response object closely enough for
    :func:`pipeline.tag._extract_tool_input` — which only touches
    ``.stop_reason``, ``.content[i].type``, ``.content[i].name``,
    ``.content[i].input``, and ``.usage.input_tokens|output_tokens``.
    """

    tool_use_block = SimpleNamespace(
        type="tool_use",
        id="toolu_test_id",
        name=tool_name,
        input=tool_input,
    )
    return SimpleNamespace(
        id="msg_test",
        stop_reason="tool_use",
        content=[tool_use_block],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _text_only_message(
    *, input_tokens: int = 100, output_tokens: int = 50
) -> SimpleNamespace:
    """A response that returned text instead of calling a tool — for the
    retry-fallback path test."""

    text_block = SimpleNamespace(type="text", text="I think this is positive.")
    return SimpleNamespace(
        id="msg_test_text",
        stop_reason="end_turn",
        content=[text_block],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class FakeMessagesEndpoint:
    """Programmable fake for ``client.messages.create``.

    Pop responses off ``self.responses`` in FIFO order. Raises if the call
    count exceeds queued responses (so tests fail loudly on unexpected
    extra calls).
    """

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError(
                "FakeMessagesEndpoint ran out of responses — test queued "
                f"too few. Calls so far: {len(self.calls)}"
            )
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeAnthropicClient:
    """Mimics ``anthropic.Anthropic`` with a ``.messages.create`` method."""

    def __init__(self, responses: list[Any]) -> None:
        self.messages = FakeMessagesEndpoint(responses)


# =========================================================================
# Test 1: multi-theme assignment per spec D1
# =========================================================================


def test_multi_theme_assignment(sample_thread: Thread) -> None:
    """Spec D1: a comment with two ideas yields two theme assignments."""

    comment = _make_comment(
        "the videos are great but the fee is outrageous", comment_id="t1_multi"
    )

    classify_response = _tool_use_message(
        tool_name="classify_comment",
        tool_input={
            "sentiment": "mixed",
            "themes": [
                {"theme": "video_format", "sentiment": "positive"},
                {"theme": "author_fees", "sentiment": "negative"},
            ],
            "product_attribution": "journal",
            "journal_named": "JoVE",
        },
    )
    voice_response = _tool_use_message(
        tool_name="judge_voice_candidate",
        tool_input={
            "yes": False,
            "reasoning": "too brief; no first-person specific detail",
        },
    )
    client = FakeAnthropicClient([classify_response, voice_response])

    result = tag_comment(comment, sample_thread, client=client)  # type: ignore[arg-type]

    assert isinstance(result, TaggedComment)
    assert result.sentiment == "mixed"
    assert result.product_attribution == "journal"
    assert result.journal_named == "JoVE"
    assert result.voice_candidate is False

    themes_by_name = {(t.theme, t.sentiment) for t in result.themes}
    assert ("video_format", "positive") in themes_by_name, themes_by_name
    assert ("author_fees", "negative") in themes_by_name, themes_by_name

    # Two API calls (classify + voice), one each.
    assert len(client.messages.calls) == 2
    assert client.messages.calls[0]["model"] == HAIKU_MODEL
    assert client.messages.calls[1]["model"] == SONNET_MODEL


# =========================================================================
# Test 2: voice candidate YES
# =========================================================================


def test_voice_candidate_yes(sample_thread: Thread) -> None:
    """A strong first-person anecdote should propagate Sonnet's YES through."""

    comment = _make_comment(
        "They used my responses to spoof an email from me to my librarian "
        "requesting that we subscribe.",
        comment_id="t1_yesvoice",
    )

    classify_response = _tool_use_message(
        tool_name="classify_comment",
        tool_input={
            "sentiment": "negative",
            "themes": [{"theme": "aggressive_marketing", "sentiment": "negative"}],
            "product_attribution": "journal",
            "journal_named": "JoVE",
        },
    )
    voice_response = _tool_use_message(
        tool_name="judge_voice_candidate",
        tool_input={
            "yes": True,
            "reasoning": (
                "first-person specific anecdote + named harm (spoofed "
                "librarian email) — matches YES-1 pattern"
            ),
        },
    )
    client = FakeAnthropicClient([classify_response, voice_response])

    result = tag_comment(comment, sample_thread, client=client)  # type: ignore[arg-type]

    assert result.voice_candidate is True
    assert result.voice_candidate_reason is not None
    assert "YES-1" in result.voice_candidate_reason
    assert result.sentiment == "negative"
    assert len(result.themes) == 1
    assert result.themes[0].theme == "aggressive_marketing"


# =========================================================================
# Test 3: emerging_other fallback
# =========================================================================


def test_emerging_other_fallback(sample_thread: Thread) -> None:
    """Comment about an obscure topic should pass through as emerging_other."""

    comment = _make_comment(
        "I really wish JoVE had better RSS feed support for protocol updates.",
        comment_id="t1_emerging",
    )

    classify_response = _tool_use_message(
        tool_name="classify_comment",
        tool_input={
            "sentiment": "neutral",
            "themes": [{"theme": "emerging_other", "sentiment": "neutral"}],
            "product_attribution": "journal",
            "journal_named": "JoVE",
        },
    )
    voice_response = _tool_use_message(
        tool_name="judge_voice_candidate",
        tool_input={
            "yes": False,
            "reasoning": "specific but minor feature request, no broader claim",
        },
    )
    client = FakeAnthropicClient([classify_response, voice_response])

    result = tag_comment(comment, sample_thread, client=client)  # type: ignore[arg-type]

    assert len(result.themes) == 1
    assert result.themes[0].theme == "emerging_other"
    assert result.themes[0].sentiment == "neutral"
    assert result.sentiment == "neutral"
    assert result.voice_candidate is False


# =========================================================================
# Test 4: classification failure — both retries fail → default fallback
# =========================================================================


def test_classification_failure_fallback(
    sample_thread: Thread,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both classification retries raise APIError → fallback fires."""

    # Strip tenacity backoff so the test isn't slow (5×exponential ≈ 30s).
    # We patch _call_anthropic to wrap the original logic but with zero
    # waits. Simplest path: monkeypatch the wait_exponential by clearing
    # the retry's wait attribute on the wrapper.
    from pipeline import tag as tag_module

    monkeypatch.setattr(tag_module._call_anthropic.retry, "wait", lambda *a, **kw: 0)

    comment = _make_comment("Some comment that will trigger an API failure.")

    # The tenacity retry wrapper inside _call_anthropic retries up to 5
    # times per call. To exhaust all retries we raise APIConnectionError
    # repeatedly. We queue 5 exceptions covering tenacity's retry budget
    # for the first classify call; tenacity reraises after 5 and the outer
    # try/except in tag_comment catches the surfaced error.
    fake_request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    api_error = APIConnectionError(request=fake_request)

    # The voice call still needs to be queued because the classify failure
    # takes the fallback path — but the voice call still happens, hitting
    # API failures too is fine to test. To keep this focused we let voice
    # succeed so we know classification failure alone produces the fallback
    # reason. We queue 5 classify failures, then a voice success.
    voice_success = _tool_use_message(
        tool_name="judge_voice_candidate",
        tool_input={"yes": False, "reasoning": "no substantive claim"},
    )

    client = FakeAnthropicClient(
        [api_error, api_error, api_error, api_error, api_error, voice_success]
    )

    with caplog.at_level(logging.WARNING, logger="pipeline.tag"):
        result = tag_comment(comment, sample_thread, client=client)  # type: ignore[arg-type]

    # Fallback shape:
    assert result.sentiment is None
    assert len(result.themes) == 1
    assert result.themes[0].theme == "emerging_other"
    assert result.themes[0].sentiment == "neutral"
    assert result.product_attribution == "out-of-scope"
    assert result.voice_candidate is False
    assert result.voice_candidate_reason == "classification_failed"

    # Warning was logged so the failure is visible in ops logs.
    assert any(
        "Classification API failure" in record.message for record in caplog.records
    ), caplog.records


# =========================================================================
# Test 5: cost log is written on successful tag
# =========================================================================


def test_cost_log_appended(
    sample_thread: Thread, _isolate_cost_log: Path
) -> None:
    """Verify ``tag_comment`` appends one JSONL line per call with token + cost metadata."""

    comment = _make_comment("JoVE is great for protocols.", comment_id="t1_costlog")
    classify_response = _tool_use_message(
        tool_name="classify_comment",
        tool_input={
            "sentiment": "positive",
            "themes": [{"theme": "video_format", "sentiment": "positive"}],
            "product_attribution": "journal",
            "journal_named": "JoVE",
        },
        input_tokens=8500,
        output_tokens=200,
    )
    voice_response = _tool_use_message(
        tool_name="judge_voice_candidate",
        tool_input={"yes": False, "reasoning": "too brief; no specifics"},
        input_tokens=4000,
        output_tokens=100,
    )
    client = FakeAnthropicClient([classify_response, voice_response])

    tag_comment(comment, sample_thread, client=client)  # type: ignore[arg-type]

    assert _isolate_cost_log.exists()
    lines = _isolate_cost_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "expected exactly one cost-log entry per tag_comment call"
    rec = json.loads(lines[0])
    assert rec["comment_id"] == "t1_costlog"
    assert rec["classify_model"] == HAIKU_MODEL
    assert rec["voice_model"] == SONNET_MODEL
    assert rec["classify_input_tokens"] == 8500
    assert rec["classify_output_tokens"] == 200
    assert rec["voice_input_tokens"] == 4000
    assert rec["voice_output_tokens"] == 100
    # Cost is dominated by Sonnet's $3/$15 rate on the voice call.
    assert rec["total_cost_usd"] > 0
    assert rec["classification_failed"] is False
    assert rec["voice_failed"] is False


# =========================================================================
# Test 6: cost estimator produces a sensible forecast
# =========================================================================


def test_estimate_mega_backfill_cost_smoke() -> None:
    """Sanity-check the cost estimator: 3000 comments shouldn't be free
    and shouldn't be five-figures-of-dollars either."""

    est = estimate_mega_backfill_cost(num_threads=200, avg_comments_per_thread=15)

    assert est["total_comments"] == 3000
    grand = est["totals"]["grand_total_usd"]
    assert 1.0 < grand < 1000.0, f"Implausible cost estimate: {grand}"

    # Sonnet dominates because $15/MTok output and 4k input × 3000 comments.
    assert (
        est["totals"]["voice_total_usd"] > est["totals"]["classify_total_usd"]
    ), est["totals"]


# =========================================================================
# Integration test — hits live Anthropic API. Skipped by default.
# =========================================================================


@pytest.mark.integration
def test_tag_comment_live_anthropic(sample_thread: Thread) -> None:
    """Smoke-test against the live Anthropic API. Costs ~$0.001 per run.

    Only runs when ``pytest -m integration`` is passed (see conftest.py).
    Requires ``ANTHROPIC_API_KEY`` in the environment.
    """

    from pipeline.config import ANTHROPIC_API_KEY

    if ANTHROPIC_API_KEY is None:
        pytest.skip("ANTHROPIC_API_KEY not set; live integration test cannot run.")

    comment = _make_comment(
        "I published in JoVE last year. The production team came to our lab "
        "for three days and the result was professional, though it cost us "
        "around 4 thousand dollars in publication fees.",
        comment_id="t1_live_integration",
    )

    result = tag_comment(comment, sample_thread)

    assert isinstance(result, TaggedComment)
    assert result.comment_id == "t1_live_integration"
    assert result.sentiment in {"positive", "neutral", "negative", "mixed"}
    assert len(result.themes) >= 1
    assert result.tagged_by_model == f"{HAIKU_MODEL}+{SONNET_MODEL}"
    assert isinstance(result.voice_candidate, bool)
