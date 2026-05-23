"""Phase 11 — Voice curation CLI (D's weekly ~15-min ritual).

D runs ``uv run voice-curate`` from her terminal once a week. The CLI
loads the most-recent Candidates file the pipeline wrote, walks D
through each candidate one-at-a-time with a clean rich-rendered panel,
and persists her decisions to Published markdown (which Phase 7 will
read on the next aggregate build).

Per-candidate keystrokes:

* ``a`` — approve, write to Published, advance
* ``r`` — reject, count it but don't write, advance
* ``s`` — skip, count it but don't write; candidate stays in
  Candidates for next week's review
* ``n`` — prompt for a one-line curator note, then approve with note
* ``q`` — quit; print summary of approved/rejected/skipped + path of
  the Published file

The CLI is intentionally thin: parsing and persistence live in
:mod:`pipeline.voice_curate`. This file owns terminal IO only — the
rich panels, the keypress loop, the summary.

Reference: ``Voice of Academia — Implementation Plan.md`` § Phase 11.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Sequence

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from pipeline.voice_curate import (
    VoiceCandidate,
    approve,
    list_weeks_with_candidates,
    load_candidates,
)

logger = logging.getLogger(__name__)

# Choices accepted at the per-candidate prompt. ``rich.prompt.Prompt`` is
# case-insensitive when ``choices=`` is set, so D can hit ``A`` or ``a``.
_CHOICES = ["a", "r", "s", "n", "q"]


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the CLI flags.

    ``--week`` lets D explicitly pick a week (otherwise we use the most-
    recent week with candidates). ``--candidates-dir`` / ``--published-dir``
    are mostly for tests but also let D drop the CLI into a copy of the
    vault for a dry-run.
    """

    parser = argparse.ArgumentParser(
        prog="voice-curate",
        description=(
            "Voice of Academia — weekly voice-panel curation CLI. "
            "Reviews LLM-surfaced candidates from "
            "Voice of Academia/Candidates/<week>.md and promotes "
            "approved ones to Voice of Academia/Published/<week>.md."
        ),
    )
    parser.add_argument(
        "--week",
        type=str,
        default=None,
        help=(
            "ISO week tag (YYYY-WWW, e.g. 2026-W22). "
            "If omitted, the most-recent week with candidates is used."
        ),
    )
    parser.add_argument(
        "--candidates-dir",
        type=Path,
        default=None,
        help=(
            "Override the Candidates folder. Defaults to "
            "VAULT_ROOT/Voice of Academia/Candidates as configured in "
            "pipeline.config."
        ),
    )
    parser.add_argument(
        "--published-dir",
        type=Path,
        default=None,
        help=(
            "Override the Published folder. Defaults to "
            "VAULT_ROOT/Voice of Academia/Published as configured in "
            "pipeline.config."
        ),
    )
    return parser.parse_args(argv)


def _render_candidate(
    console: Console,
    candidate: VoiceCandidate,
    index: int,
    total: int,
) -> None:
    """Print one candidate as a rich-styled Panel.

    Layout (rendered example):

        [1/34] NEGATIVE · fees / predatory feel · JoVE
        u/somePostdoc · 2026-04-12 · ↑247
        ──────────────────────────────────────────────
        "Wasted 9 months. The editor never replied to emails…"

        Voice rubric reason: …
    """

    # Headline coloring by sentiment — helps D scan the queue fast.
    sentiment_upper = candidate.sentiment.upper()
    color = {
        "POSITIVE": "green",
        "NEGATIVE": "red",
        "NEUTRAL": "yellow",
        "MIXED": "magenta",
        "UNKNOWN": "white",
    }.get(sentiment_upper, "white")

    headline = Text()
    headline.append(f"[{index}/{total}] ", style="dim")
    headline.append(sentiment_upper, style=f"bold {color}")
    headline.append(" · ", style="dim")
    headline.append(candidate.themes_pretty, style="bold")
    headline.append(" · ", style="dim")
    headline.append(candidate.journal, style="bold cyan")

    meta = Text()
    meta.append(f"u/{candidate.author}", style="bright_blue")
    meta.append("  ·  ", style="dim")
    meta.append(candidate.date, style="white")
    meta.append("  ·  ", style="dim")
    meta.append(f"↑{candidate.score}", style="green")

    body_text = Text(candidate.body, style="white")

    rubric = Text()
    rubric.append("Voice rubric reason: ", style="dim italic")
    rubric.append(candidate.voice_rubric_reason or "(none)", style="italic")

    thread_url_text = Text()
    thread_url_text.append("Thread: ", style="dim")
    thread_url_text.append(candidate.thread_title, style="white")
    thread_url_text.append(f"\n        {candidate.thread_url}", style="dim cyan")

    panel_body = Text("\n").join(
        [
            meta,
            Text(""),
            body_text,
            Text(""),
            rubric,
            Text(""),
            thread_url_text,
        ]
    )

    console.print(
        Panel(
            panel_body,
            title=headline,
            border_style=color,
            padding=(1, 2),
            title_align="left",
        )
    )


def _prompt_action(console: Console) -> str:
    """Show the action menu and return the user's choice (one of _CHOICES).

    Re-prompts on bad input. ``rich.prompt.Prompt`` returns lowercase
    when ``choices`` is provided.
    """

    console.print(
        "[bold]\\[a][/bold] Approve  "
        "[bold]\\[r][/bold] Reject  "
        "[bold]\\[s][/bold] Skip  "
        "[bold]\\[n][/bold] Note + Approve  "
        "[bold]\\[q][/bold] Quit",
        highlight=False,
    )
    choice = Prompt.ask(
        ">",
        choices=_CHOICES,
        default="s",
        show_choices=False,
        show_default=False,
    )
    return choice.lower()


def _print_header(console: Console, week: str, candidate_count: int) -> None:
    """Print the session opening banner."""

    console.rule("[bold cyan]VoA Voice Curation[/bold cyan]")
    console.print(
        f"[bold]Week:[/bold] [cyan]{week}[/cyan]   "
        f"[bold]Candidates:[/bold] [yellow]{candidate_count}[/yellow]"
    )
    console.print()


def _print_summary(
    console: Console,
    approved: int,
    rejected: int,
    skipped: int,
    remaining: int,
    published_path: Path | None,
) -> None:
    """Print the end-of-session summary table."""

    console.print()
    console.rule("[bold cyan]Session summary[/bold cyan]")

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("label", style="bold")
    table.add_column("count", justify="right")
    table.add_row("Approved", f"[green]{approved}[/green]")
    table.add_row("Rejected", f"[red]{rejected}[/red]")
    table.add_row("Skipped", f"[yellow]{skipped}[/yellow]")
    if remaining > 0:
        table.add_row("Remaining (not reviewed)", f"[dim]{remaining}[/dim]")
    console.print(table)

    if approved and published_path is not None:
        console.print()
        console.print(
            f"[green]✓[/green] Approved quotes written to "
            f"[bold cyan]{published_path}[/bold cyan]"
        )


def run(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``uv run voice-curate``.

    Returns an exit code suitable for ``sys.exit``: ``0`` on a clean
    session (even when D quits mid-way — quitting is a normal exit
    path), ``1`` on configuration errors (e.g. ``--week`` references
    a file that doesn't exist).
    """

    args = _parse_args(argv)
    console = Console()

    # Resolve which week to load.
    if args.week is None:
        weeks = list_weeks_with_candidates(candidates_dir=args.candidates_dir)
        if not weeks:
            console.print(
                "[yellow]No Candidates files with content found.[/yellow] "
                "Run the pipeline first, or pass [bold]--week YYYY-WWW[/bold]."
            )
            return 0
        week = weeks[-1]
    else:
        week = args.week

    candidates = load_candidates(week, candidates_dir=args.candidates_dir)
    if not candidates:
        console.print(
            f"[yellow]No candidates parsed for week {week}.[/yellow] "
            "(File missing, empty, or all blocks failed to parse — see "
            "logs.)"
        )
        return 0

    _print_header(console, week, len(candidates))

    approved = 0
    rejected = 0
    skipped = 0
    published_path: Path | None = None

    for i, candidate in enumerate(candidates, start=1):
        _render_candidate(console, candidate, i, len(candidates))
        action = _prompt_action(console)

        if action == "a":
            published_path = approve(
                candidate, curator_note=None, published_dir=args.published_dir
            )
            approved += 1
            console.print(
                f"[green]✓ Approved[/green] -> {published_path}\n"
            )
        elif action == "n":
            note = Prompt.ask("[bold]Curator note[/bold] (one line)")
            note = note.strip() or None
            published_path = approve(
                candidate,
                curator_note=note,
                published_dir=args.published_dir,
            )
            approved += 1
            console.print(
                f"[green]✓ Approved with note[/green] -> {published_path}\n"
            )
        elif action == "r":
            rejected += 1
            console.print("[red]✗ Rejected[/red]\n")
        elif action == "s":
            skipped += 1
            console.print("[yellow]→ Skipped[/yellow]\n")
        elif action == "q":
            remaining = len(candidates) - i
            _print_summary(
                console,
                approved=approved,
                rejected=rejected,
                skipped=skipped,
                remaining=remaining,
                published_path=published_path,
            )
            return 0

    _print_summary(
        console,
        approved=approved,
        rejected=rejected,
        skipped=skipped,
        remaining=0,
        published_path=published_path,
    )
    return 0


def main() -> None:
    """``[project.scripts]`` entry point — wraps ``run`` with sys.exit."""

    sys.exit(run())


if __name__ == "__main__":
    main()
