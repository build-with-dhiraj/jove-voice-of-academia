"""Phase 11 — Voice-panel curation core (D's weekly ritual).

This module is the parsing + persistence engine behind the
``voice-curate`` CLI (``scripts/voice_curate_cli.py``). The CLI owns the
interactive TUI; this module owns:

1. **Load** parsing of Phase 6's ``Candidates/YYYY-WWW.md`` files into
   :class:`VoiceCandidate` Pydantic instances.
2. **Approve** persistence of an approved candidate (plus optional
   curator note) into ``Published/YYYY-WWW.md`` in the format Phase 7's
   ``parse_voice_published_dir`` reads.
3. **List** weeks that have a non-empty Candidates file, so the CLI can
   pick the most-recent unprocessed week without D specifying it.

Contract obligations:

* ``load_candidates()`` MUST round-trip with Phase 6's
  :func:`pipeline.store._format_voice_block` output — the block shape
  emitted there is what we parse here.
* ``approve()`` MUST emit Published markdown that Phase 7's
  :func:`pipeline.aggregate.parse_voice_published_file` can read. That
  parser keys on:
    - frontmatter delimited by ``---\\n...\\n---\\n`` containing
      ``updated: YYYY-MM-DD`` (used as ``curated_at`` for all rows in
      the file).
    - Block headings shaped ``## {journal} · {SENTIMENT} · {themes}``
      (the ``·`` MUST be the unicode middle-dot ``\\u00b7``).
    - Field lines ``**Author**:``, ``**Comment**:``, ``**Date**:``,
      ``**Score**:``, ``**Voice rubric**:``, optional
      ``**Curator note**:``.
    - A body blockquote (lines beginning with ``> ``).

If you change the emitted format here, you MUST update the parser regexes
in :mod:`pipeline.aggregate` and re-verify the round-trip test in
:mod:`tests.pipeline.test_voice_curate`.

References:

* ``Voice of Academia — Implementation Plan.md`` § Phase 11
* ``Voice of Academia — System Design.md`` § D6 (voice panel filter)
* :func:`pipeline.store._format_voice_block` (the Candidate format we read)
* :func:`pipeline.aggregate.parse_voice_published_file` (the Published
  format we write)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from pipeline.config import VAULT_VOICE_CANDIDATES, VAULT_VOICE_PUBLISHED

logger = logging.getLogger(__name__)


# =========================================================================
# VoiceCandidate model — the round-trip-able shape between Candidates and
# Published
# =========================================================================


class VoiceCandidate(BaseModel):
    """One parsed voice-candidate block from ``Candidates/YYYY-WWW.md``.

    Fields mirror exactly what Phase 6's ``_format_voice_block`` writes,
    plus the source path + week tag for re-emission to Published. The
    model is intentionally tolerant of optional fields (``score`` can be
    missing on some legacy hand-edits) so D's curation doesn't crash on
    minor format drift; the parser logs and skips truly broken blocks.

    The ``themes_pretty`` field is preserved verbatim as it appears in
    the Candidates heading — Phase 6 emits ``"theme:sentiment, theme:..."``
    (or the literal ``"other"``). We round-trip it unchanged so Phase 7's
    parser sees the same string it would have seen if D had hand-copied
    the block.
    """

    model_config = ConfigDict(extra="forbid")

    # Heading-derived fields
    journal: str = Field(
        description="Journal name from the heading, e.g. ``JoVE`` or "
        "``Unknown journal``."
    )
    sentiment: str = Field(
        description="UPPERCASE sentiment string as emitted by Phase 6 "
        "(``POSITIVE|NEUTRAL|NEGATIVE|MIXED|UNKNOWN``). Preserved "
        "verbatim so Published heading reads identically."
    )
    themes_pretty: str = Field(
        description="The ``theme:sentiment, theme:sentiment, ...`` "
        "string (or ``other``) from the heading. Preserved verbatim."
    )

    # Field-line values
    author: str = Field(description="Reddit author without the ``u/`` prefix.")
    thread_title: str = Field(description="Title of the parent thread.")
    thread_url: str = Field(description="Reddit URL to the parent thread.")
    comment_url: str = Field(description="Reddit URL to the comment itself.")
    date: str = Field(description="ISO-format date (``YYYY-MM-DD``).")
    score: int = Field(description="Net upvotes at scrape time.")
    voice_rubric_reason: str = Field(
        description="LLM's one-line voice-candidacy rationale."
    )

    # Body (verbatim, with the ``> `` blockquote prefix already stripped).
    body: str = Field(description="Comment body text, blockquote-prefix stripped.")

    # Provenance — not in the markdown itself; set by the parser.
    week: str = Field(
        description="ISO week tag the candidate was loaded from (``YYYY-WWW``)."
    )
    source_path: Path = Field(
        description="Absolute path to the Candidates file this came from."
    )


# =========================================================================
# Block parser — reads Phase 6's `_format_voice_block` output
# =========================================================================


# Heading match: ``## journal · SENTIMENT · themes_pretty``.
# Journal is greedy-to-first-middot, sentiment is uppercase letters only
# (matches Phase 6's ``.upper()`` cast), themes is rest-of-line.
_HEADING_RE = re.compile(
    r"^##\s+(?P<journal>.+?)\s+·\s+(?P<sentiment>[A-Z]+)\s+·\s+(?P<themes>.+?)\s*$",
    re.MULTILINE,
)
# Author line: ``**Author**: u/USERNAME``
_AUTHOR_RE = re.compile(
    r"\*\*Author\*\*\s*:\s*u/(?P<author>[^\s]+)", re.IGNORECASE
)
# Thread line: ``**Thread**: [TITLE](URL)``
_THREAD_RE = re.compile(
    r"\*\*Thread\*\*\s*:\s*\[(?P<title>[^\]]*)\]\((?P<url>[^)]+)\)",
    re.IGNORECASE,
)
# Comment line: ``**Comment**: [link](URL)``
_COMMENT_RE = re.compile(
    r"\*\*Comment\*\*\s*:\s*\[[^\]]*\]\((?P<url>[^)]+)\)", re.IGNORECASE
)
# Date line: ``**Date**: YYYY-MM-DD``
_DATE_RE = re.compile(
    r"\*\*Date\*\*\s*:\s*(?P<date>\d{4}-\d{2}-\d{2})", re.IGNORECASE
)
# Score line: ``**Score**: N`` (may be negative).
_SCORE_RE = re.compile(
    r"\*\*Score\*\*\s*:\s*(?P<score>-?\d+)", re.IGNORECASE
)
# Voice rubric line: greedy until next ``\n\n`` paragraph break or
# the body blockquote starts (``\n>``).
_RUBRIC_RE = re.compile(
    r"\*\*Voice rubric\*\*\s*:\s*(?P<reason>.+?)(?:\n\n|\n>)",
    re.DOTALL | re.IGNORECASE,
)
# Body blockquote: one or more consecutive lines starting with ``>``.
_BODY_RE = re.compile(r"(?:^|\n)(?P<body>(?:>[^\n]*\n?)+)", re.MULTILINE)
# Block-start anchor (used to split a Candidates file into per-block chunks).
_BLOCK_START_RE = re.compile(
    r"^##\s+\S.*?\s+·\s+[A-Z]+\s+·\s+.+$", re.MULTILINE
)

_ISO_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


def _strip_frontmatter(content: str) -> str:
    """Remove a leading ``---\\n...\\n---\\n`` frontmatter block.

    Mirrors :func:`pipeline.aggregate._strip_frontmatter` — duplicated
    here so this module stays independent of the aggregate layer.
    Returns the original content if no frontmatter is present.
    """

    if not content.startswith("---"):
        return content
    end = content.find("\n---", 3)
    if end == -1:
        return content
    return content[end + 4 :].lstrip("\n")


def _split_blocks(body: str) -> list[str]:
    """Split a Candidates file body into per-block strings.

    Block boundaries are heading lines matching ``_BLOCK_START_RE``;
    each block runs from one heading to the next (or to EOF). The
    trailing ``---\\n`` separator that Phase 6 emits between blocks is
    intentionally NOT used as a delimiter — heading-based splitting is
    robust to hand-edits that drop the separator and lets the same
    splitter handle Published files (which we may want to re-load later).
    """

    starts = [m.start() for m in _BLOCK_START_RE.finditer(body)]
    if not starts:
        return []
    boundaries = starts + [len(body)]
    blocks: list[str] = []
    for i in range(len(starts)):
        block = body[boundaries[i] : boundaries[i + 1]]
        # Strip the trailing ``---`` separator if present so it doesn't
        # leak into the body blockquote regex on the final block.
        block = re.sub(r"\n---\s*\n*\Z", "\n", block).strip()
        if block:
            blocks.append(block)
    return blocks


def _strip_blockquote_prefix(body_match: str) -> str:
    """Remove the leading ``> `` (or bare ``>``) from each line.

    Mirrors :func:`pipeline.aggregate._parse_voice_block`'s body
    handling. Multi-line bodies are recomposed with ``\\n`` joins.
    Trailing whitespace is stripped — Phase 6 sometimes leaves blank
    ``>`` lines as paragraph spacers; we keep them but trim the right
    edge of the whole body.
    """

    out_lines: list[str] = []
    for raw_line in body_match.splitlines():
        if raw_line.startswith("> "):
            out_lines.append(raw_line[2:])
        elif raw_line.startswith(">"):
            out_lines.append(raw_line[1:])
        else:
            # Defensive: a hand-edit may have dropped the prefix on a
            # continuation line. Keep it verbatim.
            out_lines.append(raw_line)
    return "\n".join(out_lines).rstrip()


def _parse_block(block: str, week: str, source_path: Path) -> VoiceCandidate | None:
    """Parse a single Markdown block into a :class:`VoiceCandidate`.

    Returns ``None`` (with a warning log) when a required field is
    missing or unparseable — the CLI surfaces the count of skipped
    blocks to D so partial-parse problems are visible.
    """

    head = _HEADING_RE.search(block)
    if not head:
        logger.warning("Block missing heading; skipping. First 80 chars: %r", block[:80])
        return None

    author_m = _AUTHOR_RE.search(block)
    thread_m = _THREAD_RE.search(block)
    comment_m = _COMMENT_RE.search(block)
    date_m = _DATE_RE.search(block)
    score_m = _SCORE_RE.search(block)
    rubric_m = _RUBRIC_RE.search(block)
    body_m = _BODY_RE.search(block)

    missing = [
        name
        for name, m in [
            ("author", author_m),
            ("thread", thread_m),
            ("comment", comment_m),
            ("date", date_m),
            ("score", score_m),
            ("body", body_m),
        ]
        if m is None
    ]
    if missing:
        logger.warning(
            "Block missing required fields %s; skipping. Heading=%r",
            missing,
            head.group(0),
        )
        return None
    # mypy/type-narrowing: the missing-list check above guarantees these.
    assert author_m and thread_m and comment_m and date_m and score_m and body_m

    try:
        score = int(score_m.group("score"))
    except ValueError:
        logger.warning(
            "Block has unparseable score %r; skipping.", score_m.group("score")
        )
        return None

    rubric = rubric_m.group("reason").strip() if rubric_m else ""

    return VoiceCandidate(
        journal=head.group("journal").strip(),
        sentiment=head.group("sentiment").strip(),
        themes_pretty=head.group("themes").strip(),
        author=author_m.group("author").strip().rstrip(","),
        thread_title=thread_m.group("title").strip(),
        thread_url=thread_m.group("url").strip(),
        comment_url=comment_m.group("url").strip(),
        date=date_m.group("date").strip(),
        score=score,
        voice_rubric_reason=rubric,
        body=_strip_blockquote_prefix(body_m.group("body")),
        week=week,
        source_path=source_path,
    )


# =========================================================================
# Public API — load_candidates
# =========================================================================


def _candidates_path(week: str, candidates_dir: Path | None = None) -> Path:
    """Return the absolute Candidates file path for an ISO week."""

    root = candidates_dir if candidates_dir is not None else VAULT_VOICE_CANDIDATES
    return root / f"{week}.md"


def _published_path(week: str, published_dir: Path | None = None) -> Path:
    """Return the absolute Published file path for an ISO week."""

    root = published_dir if published_dir is not None else VAULT_VOICE_PUBLISHED
    return root / f"{week}.md"


def _current_week_iso(now: datetime | None = None) -> str:
    """Return the current ISO-week string ``YYYY-WWW``.

    Uses ``%G-W%V`` so year boundaries follow ISO-week semantics
    (matches Phase 6's choice).
    """

    if now is None:
        now = datetime.now(tz=timezone.utc)
    return now.strftime("%G-W%V")


def load_candidates(
    week: str | None = None,
    *,
    candidates_dir: Path | None = None,
) -> list[VoiceCandidate]:
    """Read ``Candidates/YYYY-WWW.md`` and return parsed candidates.

    :param week: ISO week tag ``YYYY-WWW``. If ``None``, the most
        recent week with a non-empty Candidates file is selected (so
        D's CLI invocation ``uv run voice-curate`` "just works" on
        the file last written by the cron). If no weeks have
        candidates, returns an empty list.
    :param candidates_dir: optional override of the Candidates folder
        (tests use a temp dir). Defaults to
        :data:`pipeline.config.VAULT_VOICE_CANDIDATES`.
    :returns: candidates in the order they appear in the file (which is
        the order Phase 6 wrote them — chronological by thread fetch).
        Empty list when the file is missing or contains only frontmatter.
    """

    if week is None:
        weeks = list_weeks_with_candidates(candidates_dir=candidates_dir)
        if not weeks:
            return []
        week = weeks[-1]  # Most-recent week.

    path = _candidates_path(week, candidates_dir=candidates_dir)
    if not path.exists():
        return []

    raw = path.read_text(encoding="utf-8")
    body = _strip_frontmatter(raw)
    blocks = _split_blocks(body)

    parsed: list[VoiceCandidate] = []
    for block in blocks:
        candidate = _parse_block(block, week=week, source_path=path)
        if candidate is not None:
            parsed.append(candidate)
    return parsed


# =========================================================================
# Public API — list_weeks_with_candidates
# =========================================================================


def list_weeks_with_candidates(
    *, candidates_dir: Path | None = None
) -> list[str]:
    """Return weeks (``YYYY-WWW``) whose Candidates file has ≥1 block.

    Sorted chronologically (ISO-week filenames sort lexicographically =
    chronologically — ``2026-W22.md`` < ``2026-W23.md``).

    A file is "non-empty" iff at least one heading-style block parses
    out. A file with only frontmatter and a ``# Voice Candidates``
    title — Phase 6's initial header — is treated as empty so the CLI
    doesn't offer D a no-op week.

    :param candidates_dir: optional override; defaults to
        :data:`pipeline.config.VAULT_VOICE_CANDIDATES`.
    """

    root = (
        candidates_dir if candidates_dir is not None else VAULT_VOICE_CANDIDATES
    )
    if not root.exists() or not root.is_dir():
        return []

    weeks: list[str] = []
    for md in sorted(root.glob("*.md")):
        stem = md.stem
        if not _ISO_WEEK_RE.match(stem):
            # Skip files that aren't ISO-week-named (e.g. README.md, a
            # stray index file). Phase 6 only writes ``YYYY-WWW.md``.
            continue
        try:
            content = md.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read %s: %s", md, exc)
            continue
        body = _strip_frontmatter(content)
        if _BLOCK_START_RE.search(body):
            weeks.append(stem)
    return weeks


# =========================================================================
# Approve — append to Published/YYYY-WWW.md
# =========================================================================


_PUBLISHED_FRONTMATTER_TEMPLATE = """\
---
type: note
status: active
domain: [research]
tags: [type/note, status/active, jove, research, voice-of-academia, voice-published, src/research]
created: {created}
updated: {updated}
source_urls: []
confidence: verified
aliases: []
---

# Voice Published — Week {week}

> [!info] **D-curated voice quotes for week {week}.** Approved from ``Voice of Academia/Candidates/{week}.md`` during the weekly curation ritual. Each block was reviewed and promoted by D; the LLM's candidacy rationale is preserved for auditability.

"""


def _format_published_block(
    candidate: VoiceCandidate, curator_note: str | None
) -> str:
    """Render an approved candidate as a Published-folder Markdown block.

    The shape matches Phase 6's ``_format_voice_block`` output so
    Phase 7's parser handles both folders with the same regex set.
    When ``curator_note`` is non-empty, it's inserted as
    ``**Curator note**: {note}`` BEFORE the body blockquote — matching
    :data:`pipeline.aggregate._CURATOR_NOTE_RE`'s expected position.

    The body re-emits with ``> `` blockquote prefixes on each line
    (with bare ``>`` for empty lines, matching Phase 6's choice) so
    Phase 7's body-extraction regex sees the same shape it sees in
    Candidates files.
    """

    # Re-apply the blockquote prefix to every line of body. We honor
    # Phase 6's "empty line becomes bare ``>``" convention so multi-
    # paragraph quotes round-trip cleanly.
    if not candidate.body:
        body_lines = ["> (empty body)"]
    else:
        body_lines = [
            f"> {line}" if line else ">" for line in candidate.body.splitlines()
        ]
    body_block = "\n".join(body_lines)

    # Curator note: inserted between **Voice rubric**: ... and the body
    # blockquote. Phase 7's _RUBRIC_RE consumes content up to ``\n\n``
    # or ``\n>``, and _CURATOR_NOTE_RE then finds the curator-note line
    # independently — so a blank line after the rubric is required to
    # cleanly terminate the rubric match.
    note_line = ""
    if curator_note:
        # Single-line note: strip embedded newlines so we don't break
        # the blockquote-detection downstream.
        cleaned = " ".join(curator_note.split())
        note_line = f"**Curator note**: {cleaned}\n\n"

    return (
        f"## {candidate.journal} · {candidate.sentiment} · {candidate.themes_pretty}\n"
        f"**Author**: u/{candidate.author}  \n"
        f"**Thread**: [{candidate.thread_title}]({candidate.thread_url})  \n"
        f"**Comment**: [link]({candidate.comment_url})  \n"
        f"**Date**: {candidate.date}  \n"
        f"**Score**: {candidate.score}  \n"
        f"**Voice rubric**: {candidate.voice_rubric_reason}\n\n"
        f"{note_line}"
        f"{body_block}\n\n"
        f"---\n\n"
    )


def _update_frontmatter_updated(content: str, today: str) -> str:
    """Replace the ``updated:`` line in the frontmatter with ``today``.

    No-op when the file has no frontmatter (returns input unchanged).
    Used on every approve() call so Phase 7 picks the correct
    ``curated_at`` timestamp for the file.
    """

    if not content.startswith("---"):
        return content
    end = content.find("\n---", 3)
    if end == -1:
        return content
    # Frontmatter block (exclusive of delimiters)
    frontmatter = content[3:end]
    new_fm = re.sub(
        r"^updated:\s*\d{4}-\d{2}-\d{2}.*$",
        f"updated: {today}",
        frontmatter,
        count=1,
        flags=re.MULTILINE,
    )
    return content[:3] + new_fm + content[end:]


def approve(
    candidate: VoiceCandidate,
    curator_note: str | None = None,
    *,
    published_dir: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Append an approved candidate to its week's Published file.

    First write to a week creates the file with the Published-folder
    frontmatter template (mirroring Phase 6's Candidates template
    shape so Phase 7's parser handles both identically). Subsequent
    writes append a new block separated by ``---\\n\\n``; on each
    write the frontmatter's ``updated:`` field is bumped to today so
    Phase 7's ``curated_at`` reflects the most-recent curation pass.

    :param candidate: the parsed :class:`VoiceCandidate` from
        :func:`load_candidates`.
    :param curator_note: optional one-line context D types during the
        ``[n]`` keystroke; inserted as ``**Curator note**: ...`` and
        consumed by Phase 7's ``_CURATOR_NOTE_RE`` regex.
    :param published_dir: optional override of the Published folder
        (tests use a temp dir). Defaults to
        :data:`pipeline.config.VAULT_VOICE_PUBLISHED`.
    :param now: optional clock injection for tests (controls the
        ``created``/``updated`` frontmatter dates).
    :returns: the :class:`Path` of the Published file that was
        written/appended-to.
    """

    today = (now or datetime.now(tz=timezone.utc)).strftime("%Y-%m-%d")
    path = _published_path(candidate.week, published_dir=published_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    block = _format_published_block(candidate, curator_note)

    if not path.exists():
        header = _PUBLISHED_FRONTMATTER_TEMPLATE.format(
            created=today, updated=today, week=candidate.week
        )
        path.write_text(header + block, encoding="utf-8")
    else:
        # Read-modify-write so we can update the ``updated:`` field
        # atomically with the append. The file is small (one week of
        # curated quotes, low tens of KB at most) so the round-trip is
        # cheap.
        existing = path.read_text(encoding="utf-8")
        updated = _update_frontmatter_updated(existing, today)
        # Ensure exactly one blank line between previous content and the
        # new block — Phase 6's writes already end with ``---\n\n``, so
        # a plain append is correct. Defensive rstrip + newline keeps the
        # invariant even if a hand-edit left trailing whitespace.
        if not updated.endswith("\n"):
            updated += "\n"
        path.write_text(updated + block, encoding="utf-8")

    logger.info(
        "Approved candidate (author=%s, comment=%s) -> %s",
        candidate.author,
        candidate.comment_url,
        path,
    )
    return path
