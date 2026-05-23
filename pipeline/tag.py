"""Phase 5 — Claude-based tagging of in-scope Reddit comments.

For each :class:`pipeline.models.Comment` (already filtered as in-scope by
Phase 4 disambiguation), this module emits one
:class:`pipeline.models.TaggedComment` per the spec contract:

* sentiment (overall) + per-theme sentiment, multi-theme per spec D1
* product attribution (v1 = JoVE-only: ``journal`` or ``out-of-scope``)
* voice candidacy (binary YES/NO with reasoning) per spec D6

The architecture is **two Claude API calls per comment**:

1. **Classification call** — Claude Haiku 4.5
   (``claude-haiku-4-5-20251001``). Fast and cheap; handles sentiment +
   theme assignment + product attribution + journal_named.
2. **Voice judgment call** — Claude Sonnet 4.6 (``claude-sonnet-4-6``).
   The nuanced YES/NO call against ``data/voice-rubric.md``.

Both calls use **tool use with strict JSON schemas** for structured
output, avoiding free-text JSON-parsing failures. The Anthropic SDK API
surface verified against
https://platform.claude.com/docs/en/agents-and-tools/tool-use at build
time (2026-05-23).

Cost-tracking metadata for each tagged comment is appended to
``data/tag-cost-log.jsonl`` so the mega-backfill can be monitored in
real time.

This module DOES NOT batch comments. Each call is per-comment so
mid-batch crashes are resumable from the checkpoint pattern in
``scripts/mega_backfill.py``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any

from anthropic import Anthropic, APIError, APIStatusError
from anthropic.types import Message
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

# ---- Model identifiers (verified against Anthropic docs 2026-05-23) ------

HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"

# ---- Pricing (USD per 1M tokens, verified against Anthropic pricing page) -
# Source: https://platform.claude.com/docs/en/about-claude/models/overview
# (2026-05-23 fetch). The Implementation Plan listed older Haiku numbers
# ($0.80 input / $4 output) — current Haiku 4.5 pricing is below.
HAIKU_INPUT_USD_PER_MTOK = 1.00
HAIKU_OUTPUT_USD_PER_MTOK = 5.00
SONNET_INPUT_USD_PER_MTOK = 3.00
SONNET_OUTPUT_USD_PER_MTOK = 15.00

# ---- Paths ---------------------------------------------------------------

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
CLASSIFY_PROMPT_PATH = PROMPTS_DIR / "classify.txt"
VOICE_PROMPT_PATH = PROMPTS_DIR / "voice.txt"
VOICE_RUBRIC_PATH = DATA_DIR / "voice-rubric.md"
TAXONOMY_EXAMPLES_PATH = DATA_DIR / "taxonomy-examples.md"
COST_LOG_PATH = REPO_ROOT / "data" / "tag-cost-log.jsonl"

# ---- Thread-body and comment-body truncation -----------------------------
# Prevents very long Reddit threads from blowing up classification cost.
# A comment longer than this is rare on Reddit; threads with megapost bodies
# (very rare for the JoVE corpus) get their body snippet capped.
COMMENT_BODY_MAX_CHARS = 6000
THREAD_BODY_SNIPPET_MAX_CHARS = 800

# ---- Sentinel: jove-aliases handled at config-load time -------------------
# (Loaded lazily so import doesn't fail if data/ is missing in test envs.)


# =========================================================================
# Tool schemas (passed via ``tools=`` to ``client.messages.create``)
# =========================================================================

# Theme IDs are loaded from taxonomy.json at module load. The classification
# tool's ``input_schema`` enumerates them so the LLM can't emit a misspelled
# theme. ``emerging_other`` is appended per spec D2.

_TAXONOMY_CACHE: dict[str, Any] | None = None


def _load_taxonomy() -> dict[str, Any]:
    """Load and cache ``data/taxonomy.json``.

    Cached at module level so ``tag_comment`` doesn't re-read on every call.
    """

    global _TAXONOMY_CACHE
    if _TAXONOMY_CACHE is None:
        _TAXONOMY_CACHE = load_data_json("taxonomy.json")
    return _TAXONOMY_CACHE


def _valid_theme_ids() -> list[str]:
    """Return the 16 canonical theme IDs plus the ``emerging_other`` fallback.

    The classification tool's JSON schema enumerates these so the LLM cannot
    produce a misspelled theme (which would otherwise be silently dropped).
    """

    tax = _load_taxonomy()
    ids = [t["id"] for t in tax["themes"]]
    ids.append(tax["emerging_bucket"]["id"])
    return ids


def _classify_tool_schema() -> dict[str, Any]:
    """Tool definition for the Haiku classification call.

    Schema is built fresh on each call so taxonomy updates (e.g. adding a
    new theme) propagate without re-importing the module.
    """

    return {
        "name": "classify_comment",
        "description": (
            "Record the sentiment, theme assignments, product attribution, "
            "and journal name for one Reddit comment about scientific "
            "journal publishing. Must be called exactly once per comment "
            "with all required fields populated."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sentiment": {
                    "type": "string",
                    "enum": ["positive", "neutral", "negative", "mixed"],
                    "description": (
                        "Overall sentiment of the comment. Use 'mixed' when "
                        "the comment expresses both positive and negative "
                        "claims overall (e.g. praising one aspect and "
                        "criticizing another). Per-theme sentiments below "
                        "are always one of positive|neutral|negative."
                    ),
                },
                "themes": {
                    "type": "array",
                    "minItems": 1,
                    "description": (
                        "One or more (theme, sentiment) assignments. "
                        "Multi-theme is allowed (spec D1) when the comment "
                        "expresses distinct ideas across different themes. "
                        "Mutually exclusive within a sentiment bucket (spec "
                        "D2) — do not emit two themes at the same sentiment "
                        "unless the comment genuinely contains two ideas. "
                        "Use 'emerging_other' when no canonical theme fits."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "theme": {
                                "type": "string",
                                "enum": _valid_theme_ids(),
                                "description": (
                                    "Canonical theme id from taxonomy.json, "
                                    "or 'emerging_other' for the fallback "
                                    "per spec D2."
                                ),
                            },
                            "sentiment": {
                                "type": "string",
                                "enum": ["positive", "neutral", "negative"],
                                "description": (
                                    "Sentiment of the comment toward this "
                                    "specific theme."
                                ),
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
                    "description": (
                        "Which JoVE product line. v1 = JoVE-only: use "
                        "'journal' when the comment discusses JoVE, "
                        "'out-of-scope' otherwise. Do not use 'eoe', "
                        "'visualize', 'research-general', 'other-journal' "
                        "in v1."
                    ),
                },
                "journal_named": {
                    "type": ["string", "null"],
                    "description": (
                        "Journal explicitly named in the comment. v1 admits "
                        "only 'JoVE' (when JoVE is named under any alias) or "
                        "null (when no journal is named). Do not name other "
                        "journals in v1."
                    ),
                },
            },
            "required": [
                "sentiment",
                "themes",
                "product_attribution",
                "journal_named",
            ],
            "additionalProperties": False,
        },
    }


VOICE_TOOL_SCHEMA: dict[str, Any] = {
    "name": "judge_voice_candidate",
    "description": (
        "Record a binary YES/NO judgment of whether the comment could be "
        "quoted verbatim by an experienced journalist in an article about "
        "JoVE's reputation in academia, along with one sentence of "
        "reasoning that cites the matching YES/NO criterion or exemplar "
        "pattern. Must be called exactly once per comment."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "yes": {
                "type": "boolean",
                "description": (
                    "True if a journalist would quote this verbatim. "
                    "Lean False when borderline (spec D6: asymmetric trust "
                    "— one tepid voice damages the whole panel)."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "One-sentence reasoning citing which YES or NO criterion "
                    "(or which exemplar pattern, e.g. 'matches YES-1 "
                    "pattern') drove the judgment. This is the audit trail "
                    "for rubric drift over time."
                ),
            },
        },
        "required": ["yes", "reasoning"],
        "additionalProperties": False,
    },
}


# =========================================================================
# Prompt rendering (templates loaded at module import, data injected here)
# =========================================================================


def _read_template(path: Path) -> Template:
    """Read a prompt template file. Uses :class:`string.Template`.

    ``string.Template`` is used instead of ``str.format`` because the prompt
    bodies contain JSON-like braces in the exemplar text (e.g.
    ``{"theme": "video_format"}``) which break ``str.format``. ``Template``
    only interprets ``$name`` placeholders.

    The templates are written with ``{name}`` placeholders for readability
    (looks like Python-format), so we convert ``{name}`` → ``$name`` at load
    time. Anywhere the prompt body contains literal braces (JSON examples),
    they pass through ``Template`` untouched.
    """

    raw = path.read_text(encoding="utf-8")
    # Convert {placeholder} → $placeholder for string.Template safety.
    # We only target our known placeholders to avoid mangling JSON in prose.
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
        _TAXONOMY_EXAMPLES_TEXT = TAXONOMY_EXAMPLES_PATH.read_text(encoding="utf-8")
    return _TAXONOMY_EXAMPLES_TEXT


def _render_taxonomy_themes_block(taxonomy: dict[str, Any]) -> str:
    """Render the 16 themes for the classification prompt body.

    Each theme appears as a heading + description + example phrasings.
    Renders at template-fill time so taxonomy.json edits propagate without
    touching the prompt file.
    """

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
    # Emerging bucket
    eb = taxonomy["emerging_bucket"]
    lines.append(f"## Fallback: ``{eb['id']}`` — {eb['label']}")
    lines.append("")
    lines.append(eb["description"])
    return "\n".join(lines)


def _slice_voice_rubric(rubric: str, section_header: str) -> str:
    """Extract a section between markdown ``##`` headers.

    Used so the voice prompt template can pull individual sections (YES
    criteria, NO criteria, YES exemplars, NO exemplars) from the rubric
    file at render time. Sections are delimited by ``## `` headings; the
    extract stops at the next ``## ``.
    """

    marker = f"## {section_header}"
    idx = rubric.find(marker)
    if idx == -1:
        # Best-effort fallback — return the whole rubric so the prompt is
        # still informative. Surface a warning for debugging.
        logger.warning(
            "Voice rubric section %r not found; passing full rubric.",
            section_header,
        )
        return rubric
    body_start = rubric.find("\n", idx) + 1
    # Find the next "## " heading after this section
    next_idx = rubric.find("\n## ", body_start)
    if next_idx == -1:
        return rubric[body_start:].strip()
    return rubric[body_start:next_idx].strip()


def _build_classification_prompt(comment: Comment, thread: Thread) -> str:
    """Render the classification prompt for one comment + thread context."""

    taxonomy = _load_taxonomy()
    template = _get_classify_template()

    return template.safe_substitute(
        taxonomy_themes_block=_render_taxonomy_themes_block(taxonomy),
        taxonomy_examples_block=_get_taxonomy_examples_text(),
        thread_title=thread.title,
        thread_subreddit=thread.subreddit,
        thread_body_snippet=(
            (thread.body or "")[:THREAD_BODY_SNIPPET_MAX_CHARS] or "(no thread body)"
        ),
        comment_author=comment.author or "[deleted]",
        comment_body=comment.body[:COMMENT_BODY_MAX_CHARS],
    )


def _build_voice_prompt(comment: Comment, thread: Thread) -> str:
    """Render the voice-candidacy judgment prompt for one comment + thread."""

    rubric = _get_voice_rubric_text()
    template = _get_voice_template()

    return template.safe_substitute(
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


# =========================================================================
# Anthropic API calls (with retries and tool-use response parsing)
# =========================================================================


@retry(
    retry=retry_if_exception_type((APIError, APIStatusError)),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    reraise=True,
)
def _call_anthropic(
    *,
    client: Anthropic,
    model: str,
    prompt: str,
    tool_schema: dict[str, Any],
    max_tokens: int,
) -> Message:
    """One Anthropic message call with forced tool use + tenacity retries.

    ``tool_choice={"type": "tool", "name": ...}`` forces the model to call
    the specific tool — no free-text replies allowed.

    Retries are scoped to genuine API errors (rate limits, server errors,
    timeouts). Other exceptions (e.g. validation errors in our code) are
    not retried.
    """

    return client.messages.create(
        model=model,
        max_tokens=max_tokens,
        tools=[tool_schema],
        tool_choice={"type": "tool", "name": tool_schema["name"]},
        messages=[{"role": "user", "content": prompt}],
    )


def _extract_tool_input(
    message: Message, expected_tool_name: str
) -> dict[str, Any] | None:
    """Pull the first matching ``tool_use`` block's ``.input`` from a message.

    Returns ``None`` if the message didn't call the expected tool — the
    caller decides whether to retry, fall back, or raise.

    The Anthropic SDK exposes ``message.content`` as a list of typed blocks;
    a ``ToolUseBlock`` has ``.type == "tool_use"``, ``.name``, ``.id``, and
    ``.input`` (a ``dict``). Verified against
    https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls
    on 2026-05-23.
    """

    if message.stop_reason != "tool_use":
        return None
    for block in message.content:
        # Block can be TextBlock, ToolUseBlock, ThinkingBlock, etc.
        if getattr(block, "type", None) == "tool_use" and block.name == expected_tool_name:
            raw = block.input
            if isinstance(raw, dict):
                return raw
            # Defensive: some SDK versions may return a string. Try JSON-decode.
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    logger.warning(
                        "Tool input was a string and not JSON-decodable: %r", raw[:200]
                    )
                    return None
    return None


# =========================================================================
# Cost logging
# =========================================================================


def _append_cost_log(record: dict[str, Any]) -> None:
    """Append one JSONL line to ``data/tag-cost-log.jsonl``.

    Idempotent w.r.t. the directory — creates ``data/`` if missing
    (e.g. when running from a clean checkout in CI). The file itself is
    append-only; we never read it back.
    """

    COST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with COST_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _usd_for_call(
    *, model: str, input_tokens: int, output_tokens: int
) -> float:
    """USD cost for one API call given token counts. Pricing per
    https://platform.claude.com/docs/en/about-claude/models/overview.
    """

    if model == HAIKU_MODEL or model.startswith("claude-haiku-4-5"):
        in_rate = HAIKU_INPUT_USD_PER_MTOK
        out_rate = HAIKU_OUTPUT_USD_PER_MTOK
    elif model == SONNET_MODEL or model.startswith("claude-sonnet-4-6"):
        in_rate = SONNET_INPUT_USD_PER_MTOK
        out_rate = SONNET_OUTPUT_USD_PER_MTOK
    else:
        # Unknown model — log a warning, use Sonnet pricing as conservative upper bound.
        logger.warning("Unknown model %r in cost calc; using Sonnet rates.", model)
        in_rate = SONNET_INPUT_USD_PER_MTOK
        out_rate = SONNET_OUTPUT_USD_PER_MTOK
    return (input_tokens * in_rate / 1_000_000.0) + (
        output_tokens * out_rate / 1_000_000.0
    )


# =========================================================================
# Public API: tag_comment
# =========================================================================


def _default_fallback(
    comment: Comment, *, reason: str, thread_id: str | None = None
) -> TaggedComment:
    """Build the safe-default :class:`TaggedComment` when classification fails.

    Per the dispatch spec: ``sentiment=None``, single ``emerging_other``
    assignment at neutral sentiment, ``voice_candidate=False`` with a
    machine-readable reason. The caller logs a warning before invoking
    this — so the failure is visible in ops logs.

    ``thread_id`` is forwarded so the Phase 7 aggregator can still join
    even failed-tag rows back to their parent thread.
    """

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
        tagged_by_model=f"{HAIKU_MODEL}+{SONNET_MODEL}",
    )


def tag_comment(
    comment: Comment,
    thread_context: Thread,
    *,
    taxonomy: dict[str, Any] | None = None,
    voice_rubric: str | None = None,
    taxonomy_examples: str | None = None,
    client: Anthropic | None = None,
) -> TaggedComment:
    """Tag one comment via two Claude calls (Haiku classify + Sonnet voice).

    :param comment: the single comment to tag.
    :param thread_context: the parent :class:`Thread` (title + body
        provide disambiguation context — a one-word "Pass." means
        different things in different threads).
    :param taxonomy: optional pre-loaded taxonomy dict. If ``None``, loaded
        from ``data/taxonomy.json``. Accepted for callers who want to pin
        a snapshot for an entire backfill.
    :param voice_rubric: optional pre-loaded voice-rubric text. Reserved
        for future use; current implementation always reads from the file
        so rubric edits propagate within one process.
    :param taxonomy_examples: optional pre-loaded examples text. Reserved
        for future use; see above.
    :param client: optional ``anthropic.Anthropic`` instance. If ``None``,
        a default client is constructed (which reads ``ANTHROPIC_API_KEY``
        from the environment via :mod:`pipeline.config`).

    Returns a :class:`TaggedComment` matching the Pinecone schema in
    Phase 6 (store).
    """

    # Three optional kwargs are accepted for API symmetry with the spec.
    # The classification call always re-loads taxonomy/examples from disk
    # at module-cache layer; the kwargs let callers pre-bind these for
    # snapshot consistency in long-running backfills.
    _ = taxonomy
    _ = voice_rubric
    _ = taxonomy_examples

    if client is None:
        client = Anthropic()

    classify_schema = _classify_tool_schema()
    classify_prompt = _build_classification_prompt(comment, thread_context)
    voice_prompt = _build_voice_prompt(comment, thread_context)

    # ---- Call 1: classification (Haiku) ---------------------------------
    parsed_classification: dict[str, Any] | None = None
    classify_message: Message | None = None
    classify_cost_usd: float = 0.0
    classify_input_tokens = 0
    classify_output_tokens = 0
    classification_failed = False

    try:
        classify_message = _call_anthropic(
            client=client,
            model=HAIKU_MODEL,
            prompt=classify_prompt,
            tool_schema=classify_schema,
            max_tokens=1024,
        )
        parsed_classification = _extract_tool_input(
            classify_message, "classify_comment"
        )

        # Retry once with a stronger instruction if the tool was not called.
        if parsed_classification is None:
            logger.warning(
                "Classification did not call tool on first try (comment %s) — retrying.",
                comment.id,
            )
            retry_prompt = (
                classify_prompt
                + "\n\nIMPORTANT: You MUST call the classify_comment tool. "
                + "Do not return text. Call the tool exactly once now."
            )
            classify_message = _call_anthropic(
                client=client,
                model=HAIKU_MODEL,
                prompt=retry_prompt,
                tool_schema=classify_schema,
                max_tokens=1024,
            )
            parsed_classification = _extract_tool_input(
                classify_message, "classify_comment"
            )
    except (APIError, APIStatusError, RetryError) as exc:
        logger.warning(
            "Classification API failure on comment %s: %s — using fallback.",
            comment.id,
            exc,
        )
        classification_failed = True

    if classify_message is not None:
        try:
            classify_input_tokens = classify_message.usage.input_tokens
            classify_output_tokens = classify_message.usage.output_tokens
            classify_cost_usd = _usd_for_call(
                model=HAIKU_MODEL,
                input_tokens=classify_input_tokens,
                output_tokens=classify_output_tokens,
            )
        except AttributeError:
            # Usage shape varies across SDK versions; degrade gracefully.
            pass

    # ---- Call 2: voice judgment (Sonnet) --------------------------------
    voice_parsed: dict[str, Any] | None = None
    voice_message: Message | None = None
    voice_cost_usd: float = 0.0
    voice_input_tokens = 0
    voice_output_tokens = 0
    voice_failed = False

    try:
        voice_message = _call_anthropic(
            client=client,
            model=SONNET_MODEL,
            prompt=voice_prompt,
            tool_schema=VOICE_TOOL_SCHEMA,
            max_tokens=512,
        )
        voice_parsed = _extract_tool_input(voice_message, "judge_voice_candidate")

        if voice_parsed is None:
            logger.warning(
                "Voice judgment did not call tool on first try (comment %s) — retrying.",
                comment.id,
            )
            retry_voice_prompt = (
                voice_prompt
                + "\n\nIMPORTANT: You MUST call the judge_voice_candidate tool. "
                + "Do not return text. Call the tool exactly once now."
            )
            voice_message = _call_anthropic(
                client=client,
                model=SONNET_MODEL,
                prompt=retry_voice_prompt,
                tool_schema=VOICE_TOOL_SCHEMA,
                max_tokens=512,
            )
            voice_parsed = _extract_tool_input(voice_message, "judge_voice_candidate")
    except (APIError, APIStatusError, RetryError) as exc:
        logger.warning(
            "Voice judgment API failure on comment %s: %s — defaulting to NO.",
            comment.id,
            exc,
        )
        voice_failed = True

    if voice_message is not None:
        try:
            voice_input_tokens = voice_message.usage.input_tokens
            voice_output_tokens = voice_message.usage.output_tokens
            voice_cost_usd = _usd_for_call(
                model=SONNET_MODEL,
                input_tokens=voice_input_tokens,
                output_tokens=voice_output_tokens,
            )
        except AttributeError:
            pass

    # ---- Decide which result-path to take -------------------------------
    if classification_failed or parsed_classification is None:
        tagged = _default_fallback(
            comment, reason="classification_failed", thread_id=thread_context.id
        )
    else:
        try:
            theme_assignments = [
                ThemeAssignment(**t) for t in parsed_classification["themes"]
            ]
            voice_yes = (
                bool(voice_parsed.get("yes", False))
                if (voice_parsed and not voice_failed)
                else False
            )
            voice_reason = (
                voice_parsed.get("reasoning")
                if (voice_parsed and not voice_failed)
                else ("voice_judgment_failed" if voice_failed else None)
            )

            tagged = TaggedComment(
                comment_id=comment.id,
                thread_id=thread_context.id,
                permalink=comment.permalink,
                sentiment=parsed_classification["sentiment"],
                themes=theme_assignments,
                product_attribution=parsed_classification["product_attribution"],
                journal_named=parsed_classification.get("journal_named"),
                voice_candidate=voice_yes,
                voice_candidate_reason=voice_reason,
                tagged_at=datetime.now(tz=timezone.utc),
                tagged_by_model=f"{HAIKU_MODEL}+{SONNET_MODEL}",
            )
        except (ValidationError, KeyError, TypeError) as exc:
            logger.warning(
                "TaggedComment validation failed on comment %s "
                "(classifier returned malformed shape: %s) — using fallback.",
                comment.id,
                exc,
            )
            tagged = _default_fallback(
                comment,
                reason="classification_failed",
                thread_id=thread_context.id,
            )

    # ---- Append cost log (best-effort; never raise) ---------------------
    try:
        _append_cost_log(
            {
                "comment_id": comment.id,
                "tagged_at": tagged.tagged_at.isoformat(),
                "classify_model": HAIKU_MODEL,
                "classify_input_tokens": classify_input_tokens,
                "classify_output_tokens": classify_output_tokens,
                "classify_cost_usd": classify_cost_usd,
                "voice_model": SONNET_MODEL,
                "voice_input_tokens": voice_input_tokens,
                "voice_output_tokens": voice_output_tokens,
                "voice_cost_usd": voice_cost_usd,
                "total_cost_usd": classify_cost_usd + voice_cost_usd,
                "classification_failed": classification_failed
                or parsed_classification is None,
                "voice_failed": voice_failed,
            }
        )
    except OSError as exc:
        logger.warning("Failed to write cost log for comment %s: %s", comment.id, exc)

    return tagged


# =========================================================================
# Cost estimator (Phase 8 mega-backfill budgeting)
# =========================================================================


def estimate_mega_backfill_cost(
    num_threads: int,
    avg_comments_per_thread: int,
    *,
    # The shape of token usage is dominated by the prompt size for both
    # calls. Defaults below reflect a measurement on a typical JoVE comment
    # with our injected taxonomy + examples + rubric.
    classify_input_tokens: int = 8_500,
    classify_output_tokens: int = 200,
    voice_input_tokens: int = 4_000,
    voice_output_tokens: int = 100,
) -> dict[str, Any]:
    """Forecast mega-backfill API cost.

    The defaults are deliberate ballpark estimates:

    * **Classification prompt** is ~6–8k tokens (16-theme taxonomy + per-
      theme exemplars + thread context). Output is ~200 tokens (the JSON
      tool call). We assume 8.5k average input.
    * **Voice prompt** is ~3.5–4k tokens (rubric criteria + 16 exemplars +
      thread context). Output is ~100 tokens (yes/no + one-sentence
      reason). We assume 4k average input.

    These can be tuned by callers once we measure real backfill usage.

    Uncertainty: ±40% on actual cost because:

    * Comment-body length variance dominates input-token variance.
    * Prompt-caching (if enabled across calls) would knock 70% off input
      cost on the second-and-later comments per thread.
    * Anthropic pricing can change without warning.

    Returns a dict with the per-call and total estimates.
    """

    total_comments = num_threads * avg_comments_per_thread

    classify_total_input_tokens = total_comments * classify_input_tokens
    classify_total_output_tokens = total_comments * classify_output_tokens
    voice_total_input_tokens = total_comments * voice_input_tokens
    voice_total_output_tokens = total_comments * voice_output_tokens

    classify_total_usd = _usd_for_call(
        model=HAIKU_MODEL,
        input_tokens=classify_total_input_tokens,
        output_tokens=classify_total_output_tokens,
    )
    voice_total_usd = _usd_for_call(
        model=SONNET_MODEL,
        input_tokens=voice_total_input_tokens,
        output_tokens=voice_total_output_tokens,
    )

    return {
        "num_threads": num_threads,
        "avg_comments_per_thread": avg_comments_per_thread,
        "total_comments": total_comments,
        "per_call_assumptions": {
            "classify_input_tokens_per_comment": classify_input_tokens,
            "classify_output_tokens_per_comment": classify_output_tokens,
            "voice_input_tokens_per_comment": voice_input_tokens,
            "voice_output_tokens_per_comment": voice_output_tokens,
        },
        "models_and_pricing_usd_per_mtok": {
            "haiku_4_5": {
                "model_id": HAIKU_MODEL,
                "input": HAIKU_INPUT_USD_PER_MTOK,
                "output": HAIKU_OUTPUT_USD_PER_MTOK,
            },
            "sonnet_4_6": {
                "model_id": SONNET_MODEL,
                "input": SONNET_INPUT_USD_PER_MTOK,
                "output": SONNET_OUTPUT_USD_PER_MTOK,
            },
        },
        "totals": {
            "classify_total_input_tokens": classify_total_input_tokens,
            "classify_total_output_tokens": classify_total_output_tokens,
            "classify_total_usd": round(classify_total_usd, 2),
            "voice_total_input_tokens": voice_total_input_tokens,
            "voice_total_output_tokens": voice_total_output_tokens,
            "voice_total_usd": round(voice_total_usd, 2),
            "grand_total_usd": round(classify_total_usd + voice_total_usd, 2),
        },
        "uncertainty_note": (
            "Estimate carries roughly ±40% uncertainty. Comment-body length "
            "variance and prompt-caching usage (if enabled) are the largest "
            "swing factors. Verify against the first 200 tagged comments' "
            "tag-cost-log.jsonl entries before extrapolating."
        ),
    }
