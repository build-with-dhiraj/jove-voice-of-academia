"""Phase 5 (v1.3 mega-backfill variant) — Azure OpenAI GPT-4.1 tagging.

This is the v1.3 replacement for :mod:`pipeline.tag`, which uses Anthropic
Claude. The v1.3 mega-backfill dispatch specifies:

* **Pass 1 (tagger)**: Azure OpenAI GPT-4.1 with function calling. Emits
  sentiment + themes + product_attribution + journal_named +
  voice_candidate + voice_candidate_reason + a self-reported confidence
  1-5.
* **Pass 2 (judge)**: Azure OpenAI GPT-4.1 re-reads the comment and Pass
  1's tag; emits agree/disagree, optional corrected_tags, judge_confidence
  1-5, and a one-sentence rationale.

Decision logic per the dispatch spec:

* ``judgment == "agree"`` AND ``judge_confidence >= 4`` → ACCEPT Pass 1.
* ``judgment != "agree"`` AND ``judge_confidence >= 4`` → ACCEPT judge's
  corrected tags.
* Either confidence < 4 → FLAG for review (written to
  ``data/low-confidence-tags.jsonl``); still emit the best-current tag so
  the aggregate has a row.

Cost tracking writes one record per call to ``data/tag-cost-log.jsonl``
(same path as the Anthropic tagger so the running-cost reader in
``scripts/mega_backfill.py`` continues to work).

Auth: reads ``AZURE_OPENAI_*`` env vars at construction. Sourced from
``~/.voa-azure.env`` for the mega-backfill run.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any

from openai import APIError, APIStatusError, AzureOpenAI, RateLimitError
from pydantic import ValidationError
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from pipeline.config import DATA_DIR, REPO_ROOT, load_data_json
from pipeline.models import (
    Comment,
    TaggedComment,
    ThemeAssignment,
    Thread,
)

logger = logging.getLogger(__name__)


# ---- Model identifiers (Azure deployment name from env) ------------------

AZURE_DEPLOYMENT_DEFAULT = "gpt-4.1"

# GPT-4.1 (Azure) pricing per 1M tokens (2026-05-24 official pricing).
# https://azure.microsoft.com/en-us/pricing/details/cognitive-services/openai-service/
GPT41_INPUT_USD_PER_MTOK = 2.00
GPT41_OUTPUT_USD_PER_MTOK = 8.00


# ---- Paths ---------------------------------------------------------------

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
CLASSIFY_PROMPT_PATH = PROMPTS_DIR / "classify.txt"
VOICE_PROMPT_PATH = PROMPTS_DIR / "voice.txt"
VOICE_RUBRIC_PATH = DATA_DIR / "voice-rubric.md"
TAXONOMY_EXAMPLES_PATH = DATA_DIR / "taxonomy-examples.md"
COST_LOG_PATH = REPO_ROOT / "data" / "tag-cost-log.jsonl"
LOW_CONFIDENCE_LOG_PATH = REPO_ROOT / "data" / "low-confidence-tags.jsonl"

# ---- Body truncation -----------------------------------------------------

COMMENT_BODY_MAX_CHARS = 6000
THREAD_BODY_SNIPPET_MAX_CHARS = 800

# ---- Acceptance gate -----------------------------------------------------

#: Minimum judge_confidence required to accept (or override-accept) a tag
#: without flagging. Per spec: confidence < 4 → flag for review.
JUDGE_CONFIDENCE_THRESHOLD: int = 4


# =========================================================================
# Taxonomy / theme-id loading (shared with Anthropic tagger)
# =========================================================================

_TAXONOMY_CACHE: dict[str, Any] | None = None


def _load_taxonomy() -> dict[str, Any]:
    global _TAXONOMY_CACHE
    if _TAXONOMY_CACHE is None:
        _TAXONOMY_CACHE = load_data_json("taxonomy.json")
    return _TAXONOMY_CACHE


def _valid_theme_ids() -> list[str]:
    tax = _load_taxonomy()
    ids = [t["id"] for t in tax["themes"]]
    ids.append(tax["emerging_bucket"]["id"])
    return ids


# =========================================================================
# OpenAI tool/function schemas (chat.completions function-calling shape)
# =========================================================================


def _pass1_tool_schema() -> dict[str, Any]:
    """Function schema for the combined Pass 1 tag call.

    Per the dispatch spec the single Pass 1 call emits BOTH the
    classification (sentiment/themes/product/journal) AND the voice
    candidacy judgment, plus a self-confidence 1-5.
    """

    return {
        "type": "function",
        "function": {
            "name": "emit_tags",
            "description": (
                "Tag a Reddit comment for sentiment, themes, voice "
                "candidacy, and self-confidence. Must be called exactly "
                "once per comment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sentiment": {
                        "type": "string",
                        "enum": ["positive", "neutral", "negative", "mixed"],
                        "description": (
                            "Overall comment sentiment. Use 'mixed' when the "
                            "comment expresses both positive and negative "
                            "claims overall."
                        ),
                    },
                    "themes": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "theme": {
                                    "type": "string",
                                    "enum": _valid_theme_ids(),
                                },
                                "sentiment": {
                                    "type": "string",
                                    "enum": ["positive", "neutral", "negative"],
                                },
                            },
                            "required": ["theme", "sentiment"],
                            "additionalProperties": False,
                        },
                        "description": (
                            "List of (theme, sentiment) assignments. "
                            "Multi-theme per spec D1 when the comment "
                            "expresses distinct ideas across themes. Use "
                            "'emerging_other' when no canonical theme fits."
                        ),
                    },
                    "product_attribution": {
                        "type": "string",
                        "enum": [
                            "journal",
                            "eoe",
                            "visualize",
                            "research-general",
                            "other-journal",
                            "out-of-scope",
                        ],
                        "description": (
                            "v1 = JoVE-only: use 'journal' for JoVE, "
                            "'out-of-scope' otherwise."
                        ),
                    },
                    "journal_named": {
                        "type": ["string", "null"],
                        "description": (
                            "v1 admits only 'JoVE' (under any alias) or null."
                        ),
                    },
                    "voice_candidate": {
                        "type": "boolean",
                        "description": (
                            "True if an experienced journalist would quote "
                            "this verbatim in an article about JoVE's "
                            "reputation in academia. Lean False when "
                            "borderline (asymmetric trust: one tepid voice "
                            "damages the whole panel)."
                        ),
                    },
                    "voice_candidate_reason": {
                        "type": "string",
                        "description": (
                            "One-sentence reasoning citing which YES or NO "
                            "criterion (or which exemplar pattern, e.g. "
                            "'matches YES-1 pattern') drove the call. "
                            "Required regardless of YES/NO."
                        ),
                    },
                    "confidence": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": (
                            "Self-reported confidence in this tag, 1-5. "
                            "1 = wild guess (ambiguous comment, weak theme "
                            "fit); 5 = clear-cut classification."
                        ),
                    },
                },
                "required": [
                    "sentiment",
                    "themes",
                    "product_attribution",
                    "journal_named",
                    "voice_candidate",
                    "voice_candidate_reason",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        },
    }


def _pass2_judge_tool_schema() -> dict[str, Any]:
    """Function schema for the Pass 2 judge call.

    The judge receives (1) the original comment + thread context and (2)
    Pass 1's full tag output, then returns a verdict. ``corrected_tags``
    is required only when verdict != 'agree' — the schema permits it
    always (set null on agree) so the call surface is uniform.
    """

    return {
        "type": "function",
        "function": {
            "name": "emit_judgment",
            "description": (
                "Judge Pass 1's tag against the original comment. Must be "
                "called exactly once per comment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "judgment": {
                        "type": "string",
                        "enum": [
                            "agree",
                            "disagree-sentiment",
                            "disagree-theme",
                            "disagree-voice",
                            "disagree-multiple",
                        ],
                        "description": (
                            "agree = accept Pass 1 as-is. disagree-* = the "
                            "named field is wrong; supply corrected_tags."
                        ),
                    },
                    "corrected_tags": {
                        "type": ["object", "null"],
                        "description": (
                            "When judgment != 'agree', the full corrected "
                            "tag object with all six Pass-1 fields "
                            "(sentiment, themes, product_attribution, "
                            "journal_named, voice_candidate, "
                            "voice_candidate_reason). Null on agree."
                        ),
                        "properties": {
                            "sentiment": {
                                "type": "string",
                                "enum": [
                                    "positive",
                                    "neutral",
                                    "negative",
                                    "mixed",
                                ],
                            },
                            "themes": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "theme": {
                                            "type": "string",
                                            "enum": _valid_theme_ids(),
                                        },
                                        "sentiment": {
                                            "type": "string",
                                            "enum": [
                                                "positive",
                                                "neutral",
                                                "negative",
                                            ],
                                        },
                                    },
                                    "required": ["theme", "sentiment"],
                                    "additionalProperties": False,
                                },
                            },
                            "product_attribution": {
                                "type": "string",
                                "enum": [
                                    "journal",
                                    "eoe",
                                    "visualize",
                                    "research-general",
                                    "other-journal",
                                    "out-of-scope",
                                ],
                            },
                            "journal_named": {"type": ["string", "null"]},
                            "voice_candidate": {"type": "boolean"},
                            "voice_candidate_reason": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                    "judge_confidence": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": (
                            "Confidence in this judgment, 1-5. Verdicts "
                            "with confidence < 4 are flagged for review."
                        ),
                    },
                    "rationale": {
                        "type": "string",
                        "description": (
                            "One-sentence explanation of why you agreed "
                            "or disagreed with Pass 1."
                        ),
                    },
                },
                "required": [
                    "judgment",
                    "judge_confidence",
                    "rationale",
                ],
                "additionalProperties": False,
            },
        },
    }


# =========================================================================
# Prompt rendering
# =========================================================================


def _read_template(path: Path) -> Template:
    raw = path.read_text(encoding="utf-8")
    placeholders = (
        "taxonomy_themes_block",
        "taxonomy_examples_block",
        "thread_title",
        "thread_subreddit",
        "thread_body_snippet",
        "comment_author",
        "comment_body",
        "voice_rubric_yes_criteria",
        "voice_rubric_no_criteria",
        "voice_rubric_yes_exemplars",
        "voice_rubric_no_exemplars",
        "pass1_tag_json",
    )
    for p in placeholders:
        raw = raw.replace("{" + p + "}", "$" + p)
    return Template(raw)


_CLASSIFY_TEMPLATE: Template | None = None
_VOICE_TEMPLATE: Template | None = None
_VOICE_RUBRIC_TEXT: str | None = None
_TAXONOMY_EXAMPLES_TEXT: str | None = None


def _get_classify_template() -> Template:
    global _CLASSIFY_TEMPLATE
    if _CLASSIFY_TEMPLATE is None:
        _CLASSIFY_TEMPLATE = _read_template(CLASSIFY_PROMPT_PATH)
    return _CLASSIFY_TEMPLATE


def _get_voice_template() -> Template:
    global _VOICE_TEMPLATE
    if _VOICE_TEMPLATE is None:
        _VOICE_TEMPLATE = _read_template(VOICE_PROMPT_PATH)
    return _VOICE_TEMPLATE


def _get_voice_rubric_text() -> str:
    global _VOICE_RUBRIC_TEXT
    if _VOICE_RUBRIC_TEXT is None:
        _VOICE_RUBRIC_TEXT = VOICE_RUBRIC_PATH.read_text(encoding="utf-8")
    return _VOICE_RUBRIC_TEXT


def _get_taxonomy_examples_text() -> str:
    global _TAXONOMY_EXAMPLES_TEXT
    if _TAXONOMY_EXAMPLES_TEXT is None:
        if TAXONOMY_EXAMPLES_PATH.exists():
            _TAXONOMY_EXAMPLES_TEXT = TAXONOMY_EXAMPLES_PATH.read_text(
                encoding="utf-8"
            )
        else:
            _TAXONOMY_EXAMPLES_TEXT = "(no exemplars file available)"
    return _TAXONOMY_EXAMPLES_TEXT


def _render_taxonomy_themes_block(taxonomy: dict[str, Any]) -> str:
    lines: list[str] = []
    for idx, theme in enumerate(taxonomy["themes"], start=1):
        lines.append(f"## Theme {idx}: ``{theme['id']}`` — {theme['label']}")
        lines.append("")
        lines.append(theme["description"])
        lines.append("")
        if theme.get("example_phrasings"):
            lines.append("Example phrasings observed in real data:")
            for ph in theme["example_phrasings"]:
                lines.append(f"- {ph}")
            lines.append("")
    eb = taxonomy["emerging_bucket"]
    lines.append(f"## Fallback: ``{eb['id']}`` — {eb['label']}")
    lines.append("")
    lines.append(eb["description"])
    return "\n".join(lines)


def _slice_voice_rubric(rubric: str, section_header: str) -> str:
    marker = f"## {section_header}"
    idx = rubric.find(marker)
    if idx == -1:
        return rubric
    body_start = rubric.find("\n", idx) + 1
    next_idx = rubric.find("\n## ", body_start)
    if next_idx == -1:
        return rubric[body_start:].strip()
    return rubric[body_start:next_idx].strip()


def _build_pass1_prompt(comment: Comment, thread: Thread) -> str:
    """Combined classification + voice prompt (single-call Pass 1).

    The existing prompt templates were authored for two separate Claude
    calls. For Azure GPT-4.1 we fold them into a single call to halve the
    token cost (and the per-call latency). The prompt body interleaves
    both jobs but keeps each tool-schema field's instructions self-
    contained so the model has unambiguous guidance per field.
    """

    taxonomy = _load_taxonomy()
    classify_body = _get_classify_template().safe_substitute(
        taxonomy_themes_block=_render_taxonomy_themes_block(taxonomy),
        taxonomy_examples_block=_get_taxonomy_examples_text(),
        thread_title=thread.title,
        thread_subreddit=thread.subreddit,
        thread_body_snippet=(
            (thread.body or "")[:THREAD_BODY_SNIPPET_MAX_CHARS]
            or "(no thread body)"
        ),
        comment_author=comment.author or "[deleted]",
        comment_body=comment.body[:COMMENT_BODY_MAX_CHARS],
    )

    rubric = _get_voice_rubric_text()
    voice_block = _get_voice_template().safe_substitute(
        voice_rubric_yes_criteria=_slice_voice_rubric(
            rubric, "YES criteria (general principles)"
        ),
        voice_rubric_no_criteria=_slice_voice_rubric(
            rubric, "NO criteria (general principles)"
        ),
        voice_rubric_yes_exemplars=_slice_voice_rubric(rubric, "YES exemplars"),
        voice_rubric_no_exemplars=_slice_voice_rubric(rubric, "NO exemplars"),
        thread_title=thread.title,
        thread_subreddit=thread.subreddit,
        comment_author=comment.author or "[deleted]",
        comment_body=comment.body[:COMMENT_BODY_MAX_CHARS],
    )

    # Combine: classify body first (so themes context is present before
    # voice rubric), then voice block. Final instruction is the unified
    # tool call.
    combined = (
        classify_body
        + "\n\n"
        + "=" * 72
        + "\nALSO judge voice candidacy in the SAME tool call.\n"
        + "=" * 72
        + "\n\n"
        + voice_block
        + "\n\n"
        + "=" * 72
        + "\n"
        + "FINAL INSTRUCTION: Call the ``emit_tags`` tool ONCE with all "
        + "seven fields populated (sentiment, themes, product_attribution, "
        + "journal_named, voice_candidate, voice_candidate_reason, "
        + "confidence). Do not return any text outside the tool call."
    )
    return combined


def _build_pass2_judge_prompt(
    comment: Comment, thread: Thread, pass1_tag: dict[str, Any]
) -> str:
    """Judge prompt — re-reads the comment and audits Pass 1's tag."""

    pass1_pretty = json.dumps(pass1_tag, indent=2, ensure_ascii=False)
    return f"""You are auditing another LLM's tag of a Reddit comment about JoVE for the "Voice of Academia" project.

Your job: agree or disagree with the Pass 1 tag, on each of these fields:
- sentiment (positive | neutral | negative | mixed)
- themes (list of theme:sentiment pairs)
- product_attribution (journal | out-of-scope for v1)
- journal_named (JoVE | null)
- voice_candidate (true if a journalist would quote it verbatim)

For each disagreement, supply corrected_tags. For each agreement, set corrected_tags to null.

Verdicts:
- "agree" — Pass 1 got everything right.
- "disagree-sentiment" — only sentiment is wrong.
- "disagree-theme" — only themes are wrong.
- "disagree-voice" — only voice_candidate is wrong.
- "disagree-multiple" — two or more fields are wrong.

Self-confidence (judge_confidence) is 1-5; verdicts below 4 are flagged for human review, so calibrate honestly.

# Voice rubric (the YES/NO call you're auditing against)

The judgment question is: "Would an experienced journalist quote this verbatim in an article about JoVE's reputation in academia?"

YES = first-person experience with specific detail, concrete anecdote with story arc, strong opinion with specific reasoning, multi-theme structured argument, insider perspective, named harm or named benefit.

NO = generic agreement, one-word reactions ("Pass.", "this", "agreed"), hearsay without first-person grounding, jokes/sarcasm without substance, pure questions, bare assertions, off-topic, internally inconsistent vague endorsement.

When borderline → NO.

# Thread context

**Thread title:** {thread.title}

**Thread subreddit:** r/{thread.subreddit}

# The original comment

**Author:** u/{comment.author or "[deleted]"}

**Comment body:**

{comment.body[:COMMENT_BODY_MAX_CHARS]}

# Pass 1's tag (to audit)

```json
{pass1_pretty}
```

# Decision procedure

1. Read the comment from scratch. Form your own opinion of each field.
2. Compare against Pass 1's tag.
3. If you agree on everything: judgment="agree", corrected_tags=null.
4. If you disagree on ONE field only: judgment="disagree-{{field}}", corrected_tags=full corrected object.
5. If you disagree on multiple: judgment="disagree-multiple", corrected_tags=full corrected object.
6. Set judge_confidence honestly: 5 only when the verdict is unambiguous.
7. Provide a one-sentence rationale.

Call the ``emit_judgment`` tool now. Do not return text outside the tool call.
"""


# =========================================================================
# Azure OpenAI client construction
# =========================================================================


def _build_client() -> AzureOpenAI:
    """Construct an AzureOpenAI client from env. Raises if creds missing."""

    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY")
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
    if not endpoint or not api_key:
        raise RuntimeError(
            "AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY must be set in "
            "the environment. Source ~/.voa-azure.env first."
        )
    return AzureOpenAI(
        api_key=api_key,
        api_version=api_version,
        azure_endpoint=endpoint,
    )


def _deployment_name() -> str:
    return os.environ.get("AZURE_OPENAI_DEPLOYMENT", AZURE_DEPLOYMENT_DEFAULT)


# =========================================================================
# API call wrapper with retries
# =========================================================================


_RETRYABLE = (APIError, APIStatusError, RateLimitError)


@retry(
    retry=retry_if_exception_type(_RETRYABLE),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    reraise=True,
)
def _call_chat_completion(
    *,
    client: AzureOpenAI,
    deployment: str,
    prompt: str,
    tool_schema: dict[str, Any],
    max_tokens: int = 1024,
) -> Any:
    """One Azure chat-completion call with forced tool use."""

    return client.chat.completions.create(
        model=deployment,
        messages=[{"role": "user", "content": prompt}],
        tools=[tool_schema],
        tool_choice={
            "type": "function",
            "function": {"name": tool_schema["function"]["name"]},
        },
        max_tokens=max_tokens,
    )


def _extract_tool_args(response: Any, expected_name: str) -> dict[str, Any] | None:
    """Pull the parsed arguments from the first matching tool_call."""

    try:
        choice = response.choices[0]
        msg = choice.message
        tool_calls = msg.tool_calls or []
    except (AttributeError, IndexError):
        return None

    for tc in tool_calls:
        fn = getattr(tc, "function", None)
        if fn is None:
            continue
        if fn.name == expected_name:
            try:
                return json.loads(fn.arguments)
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Tool args were not JSON-decodable: %r",
                    (fn.arguments or "")[:200],
                )
                return None
    return None


# =========================================================================
# Cost logging
# =========================================================================


def _append_cost_log(record: dict[str, Any]) -> None:
    COST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with COST_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _append_low_confidence_log(record: dict[str, Any]) -> None:
    LOW_CONFIDENCE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOW_CONFIDENCE_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _usd_for_call(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * GPT41_INPUT_USD_PER_MTOK / 1_000_000.0) + (
        output_tokens * GPT41_OUTPUT_USD_PER_MTOK / 1_000_000.0
    )


# =========================================================================
# Fallback path
# =========================================================================


def _default_fallback(
    comment: Comment,
    *,
    reason: str,
    thread_id: str | None = None,
    model_id: str = "gpt-4.1+gpt-4.1-judge",
) -> TaggedComment:
    """Safe default when Pass 1 fails entirely."""

    return TaggedComment(
        comment_id=comment.id,
        thread_id=thread_id,
        permalink=comment.permalink,
        sentiment=None,
        themes=[ThemeAssignment(theme="emerging_other", sentiment="neutral")],
        product_attribution="out-of-scope",
        journal_named=None,
        voice_candidate=False,
        voice_candidate_reason=reason,
        tagged_at=datetime.now(tz=timezone.utc),
        tagged_by_model=model_id,
        confidence=None,
        judge_verdict=None,
        judge_confidence=None,
        judge_rationale=None,
    )


# =========================================================================
# Public API: tag_comment_with_judge
# =========================================================================


def tag_comment_with_judge(
    comment: Comment,
    thread_context: Thread,
    *,
    client: AzureOpenAI | None = None,
    deployment: str | None = None,
) -> TaggedComment:
    """Tag one comment via Pass 1 + Pass 2 judge (both GPT-4.1).

    Returns a :class:`TaggedComment` with judge fields populated when the
    judge call succeeded.

    Decision logic per the v1.3 dispatch spec:

    * Pass 1 produces (sentiment, themes, product_attribution,
      journal_named, voice_candidate, voice_candidate_reason, confidence).
    * Pass 2 (judge) re-reads the comment + Pass 1's tag and emits
      (judgment, corrected_tags, judge_confidence, rationale).
    * If judgment == 'agree' AND judge_confidence >= 4 → ACCEPT Pass 1.
    * If judgment != 'agree' AND judge_confidence >= 4 → ACCEPT corrected.
    * Either confidence < 4 → APPEND to low-confidence log; still emit
      the best-current tag for the aggregate.

    Errors:

    * Pass 1 failure → :func:`_default_fallback`. No judge call attempted.
    * Pass 2 failure → Pass 1's tag stands; judge fields stay None.
    """

    if client is None:
        client = _build_client()
    if deployment is None:
        deployment = _deployment_name()

    model_id = f"{deployment}+{deployment}-judge"

    # ---- Pass 1 -----------------------------------------------------------
    pass1_response: Any = None
    pass1_args: dict[str, Any] | None = None
    pass1_input_tokens = 0
    pass1_output_tokens = 0
    pass1_failed = False

    try:
        pass1_prompt = _build_pass1_prompt(comment, thread_context)
        pass1_response = _call_chat_completion(
            client=client,
            deployment=deployment,
            prompt=pass1_prompt,
            tool_schema=_pass1_tool_schema(),
            max_tokens=1024,
        )
        pass1_args = _extract_tool_args(pass1_response, "emit_tags")

        # Retry once with a stronger nudge if the tool wasn't called.
        if pass1_args is None:
            logger.warning(
                "Pass 1 did not call emit_tags on first try (comment %s) — retrying",
                comment.id,
            )
            retry_prompt = (
                pass1_prompt
                + "\n\nIMPORTANT: You MUST call the emit_tags tool. "
                + "Do not return text. Call the tool exactly once now."
            )
            pass1_response = _call_chat_completion(
                client=client,
                deployment=deployment,
                prompt=retry_prompt,
                tool_schema=_pass1_tool_schema(),
                max_tokens=1024,
            )
            pass1_args = _extract_tool_args(pass1_response, "emit_tags")
    except (APIError, APIStatusError, RateLimitError, RetryError) as exc:
        logger.warning(
            "Pass 1 API failure on comment %s: %s — using fallback",
            comment.id,
            exc,
        )
        pass1_failed = True

    if pass1_response is not None:
        try:
            pass1_input_tokens = pass1_response.usage.prompt_tokens
            pass1_output_tokens = pass1_response.usage.completion_tokens
        except AttributeError:
            pass

    pass1_cost = _usd_for_call(pass1_input_tokens, pass1_output_tokens)

    if pass1_failed or pass1_args is None:
        tagged = _default_fallback(
            comment,
            reason="pass1_failed",
            thread_id=thread_context.id,
            model_id=model_id,
        )
        _append_cost_log(
            {
                "comment_id": comment.id,
                "tagged_at": tagged.tagged_at.isoformat(),
                "model": deployment,
                "pass1_input_tokens": pass1_input_tokens,
                "pass1_output_tokens": pass1_output_tokens,
                "pass1_cost_usd": pass1_cost,
                "pass2_input_tokens": 0,
                "pass2_output_tokens": 0,
                "pass2_cost_usd": 0.0,
                "total_cost_usd": pass1_cost,
                "pass1_failed": True,
                "pass2_failed": False,
                "judge_verdict": None,
            }
        )
        return tagged

    # ---- Pass 2 (judge) ---------------------------------------------------
    pass2_response: Any = None
    pass2_args: dict[str, Any] | None = None
    pass2_input_tokens = 0
    pass2_output_tokens = 0
    pass2_failed = False

    try:
        judge_prompt = _build_pass2_judge_prompt(comment, thread_context, pass1_args)
        pass2_response = _call_chat_completion(
            client=client,
            deployment=deployment,
            prompt=judge_prompt,
            tool_schema=_pass2_judge_tool_schema(),
            max_tokens=1024,
        )
        pass2_args = _extract_tool_args(pass2_response, "emit_judgment")

        if pass2_args is None:
            logger.warning(
                "Pass 2 did not call emit_judgment on first try (comment %s) — retrying",
                comment.id,
            )
            retry_prompt = (
                judge_prompt
                + "\n\nIMPORTANT: You MUST call the emit_judgment tool. "
                + "Do not return text. Call the tool exactly once now."
            )
            pass2_response = _call_chat_completion(
                client=client,
                deployment=deployment,
                prompt=retry_prompt,
                tool_schema=_pass2_judge_tool_schema(),
                max_tokens=1024,
            )
            pass2_args = _extract_tool_args(pass2_response, "emit_judgment")
    except (APIError, APIStatusError, RateLimitError, RetryError) as exc:
        logger.warning(
            "Pass 2 (judge) API failure on comment %s: %s — keeping Pass 1 tag",
            comment.id,
            exc,
        )
        pass2_failed = True

    if pass2_response is not None:
        try:
            pass2_input_tokens = pass2_response.usage.prompt_tokens
            pass2_output_tokens = pass2_response.usage.completion_tokens
        except AttributeError:
            pass

    pass2_cost = _usd_for_call(pass2_input_tokens, pass2_output_tokens)

    # ---- Apply judge verdict ---------------------------------------------
    final_tags = dict(pass1_args)
    judge_verdict: str | None = None
    judge_confidence: int | None = None
    judge_rationale: str | None = None

    if not pass2_failed and pass2_args is not None:
        judge_verdict = pass2_args.get("judgment")
        judge_confidence = int(pass2_args.get("judge_confidence", 0) or 0) or None
        judge_rationale = pass2_args.get("rationale")

        if (
            judge_verdict
            and judge_verdict != "agree"
            and isinstance(pass2_args.get("corrected_tags"), dict)
            and judge_confidence is not None
            and judge_confidence >= JUDGE_CONFIDENCE_THRESHOLD
        ):
            # Override: judge disagreed with high confidence — use its corrections
            corrected = pass2_args["corrected_tags"]
            # Merge corrected fields (judge may have included full tag or partial)
            for k in (
                "sentiment",
                "themes",
                "product_attribution",
                "journal_named",
                "voice_candidate",
                "voice_candidate_reason",
            ):
                if k in corrected and corrected[k] is not None:
                    final_tags[k] = corrected[k]

    # ---- Flag low confidence ---------------------------------------------
    pass1_conf = int(pass1_args.get("confidence", 0) or 0) or None
    low_conf_flagged = False
    if (
        pass1_conf is not None and pass1_conf < JUDGE_CONFIDENCE_THRESHOLD
    ) or (
        judge_confidence is not None
        and judge_confidence < JUDGE_CONFIDENCE_THRESHOLD
    ):
        low_conf_flagged = True
        _append_low_confidence_log(
            {
                "comment_id": comment.id,
                "permalink": comment.permalink,
                "body_preview": comment.body[:240],
                "thread_id": thread_context.id,
                "thread_title": thread_context.title,
                "pass1_tag": pass1_args,
                "pass1_confidence": pass1_conf,
                "judge_verdict": judge_verdict,
                "judge_confidence": judge_confidence,
                "judge_rationale": judge_rationale,
                "corrected_tags": (pass2_args or {}).get("corrected_tags")
                if not pass2_failed
                else None,
                "final_tags": final_tags,
                "tagged_at": datetime.now(tz=timezone.utc).isoformat(),
            }
        )

    # ---- Build TaggedComment ---------------------------------------------
    try:
        theme_assignments = [ThemeAssignment(**t) for t in final_tags["themes"]]
        tagged = TaggedComment(
            comment_id=comment.id,
            thread_id=thread_context.id,
            permalink=comment.permalink,
            sentiment=final_tags["sentiment"],
            themes=theme_assignments,
            product_attribution=final_tags["product_attribution"],
            journal_named=final_tags.get("journal_named"),
            voice_candidate=bool(final_tags.get("voice_candidate", False)),
            voice_candidate_reason=final_tags.get("voice_candidate_reason"),
            tagged_at=datetime.now(tz=timezone.utc),
            tagged_by_model=model_id,
            confidence=pass1_conf,
            judge_verdict=judge_verdict,
            judge_confidence=judge_confidence,
            judge_rationale=judge_rationale,
        )
    except (ValidationError, KeyError, TypeError) as exc:
        logger.warning(
            "TaggedComment validation failed on comment %s "
            "(final shape: %s) — using fallback",
            comment.id,
            exc,
        )
        tagged = _default_fallback(
            comment,
            reason=f"validation_failed:{exc}",
            thread_id=thread_context.id,
            model_id=model_id,
        )

    # ---- Cost log entry ---------------------------------------------------
    _append_cost_log(
        {
            "comment_id": comment.id,
            "tagged_at": tagged.tagged_at.isoformat(),
            "model": deployment,
            "pass1_input_tokens": pass1_input_tokens,
            "pass1_output_tokens": pass1_output_tokens,
            "pass1_cost_usd": pass1_cost,
            "pass2_input_tokens": pass2_input_tokens,
            "pass2_output_tokens": pass2_output_tokens,
            "pass2_cost_usd": pass2_cost,
            "total_cost_usd": pass1_cost + pass2_cost,
            "pass1_failed": False,
            "pass2_failed": pass2_failed,
            "pass1_confidence": pass1_conf,
            "judge_verdict": judge_verdict,
            "judge_confidence": judge_confidence,
            "low_confidence_flagged": low_conf_flagged,
        }
    )

    return tagged


# =========================================================================
# Drop-in shim so scripts.mega_backfill can use Azure tagger without edits
# =========================================================================


def install_as_default_tagger() -> None:
    """Monkey-patch :mod:`pipeline.tag` to delegate to the Azure tagger.

    The Phase 8 orchestrator (``scripts.mega_backfill``) calls
    ``pipeline.tag.tag_comment(comment, thread)``. Calling this function
    swaps that symbol so the same orchestrator drives the Azure GPT-4.1
    Pass 1 + Pass 2 judge instead of the Anthropic Haiku+Sonnet path.

    This is a deliberate runtime-monkeypatch (rather than importing a
    different module) so the v1.3 mega-backfill can reuse the existing
    checkpointing / cost-logging / SIGINT-handling code in
    ``scripts.mega_backfill`` without forking it.
    """

    from pipeline import tag as _orig_tag

    def _shim(
        comment: Comment,
        thread_context: Thread,
        **_legacy_kwargs: Any,
    ) -> TaggedComment:
        return tag_comment_with_judge(comment, thread_context)

    _orig_tag.tag_comment = _shim  # type: ignore[assignment]
    logger.info(
        "pipeline.tag.tag_comment monkey-patched to Azure GPT-4.1 + judge"
    )
