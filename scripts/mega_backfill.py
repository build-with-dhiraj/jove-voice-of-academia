"""Phase 8 — Mega backfill orchestrator.

One-shot historical scan (2010 → present) that wires together the six
pipeline phases (``discover`` → ``fetch`` → ``disambiguate`` → ``tag`` →
``store`` → ``aggregate``) into a single resumable script. Designed for
D to run manually on a Mac that can stay awake overnight; the script
checkpoints after every thread so a crash loses at most one thread's
work.

Run via::

    uv run python scripts/mega_backfill.py [--time-horizon 2010-01-01] \
        [--dry-run] [--checkpoint-path data/checkpoint.json] \
        [--max-cost-usd 100] [--quiet]

Phases the script orchestrates (one thread at a time, in order):

1. **Discovery** — :func:`pipeline.discover.discover_all` collects every
   ``ThreadCandidate`` from Rings 1+2+3 (deduped). Count is recorded
   before any LLM tagging begins so the cost forecast can sanity-check
   against ``--max-cost-usd``.
2. **Cost gate** — :func:`pipeline.tag.estimate_mega_backfill_cost`
   forecasts API spend assuming 15 comments/thread average. If the
   forecast exceeds ``--max-cost-usd``, the script aborts with a
   non-zero exit code. ``--dry-run`` prints the forecast and exits 0
   without fetching or tagging.
3. **Main loop** — for each not-yet-processed candidate: fetch, scope-
   check, tag, upsert to Pinecone, write voice candidates to the vault,
   append the thread + tagged-comments to durable JSONL mirrors, mark
   processed in the checkpoint. Single-thread failures are logged into
   ``failed_thread_ids`` and the loop continues — the script never
   bails out on one thread's error.
4. **Aggregate** — at end of run, build the dashboard
   :class:`Aggregate` from the durable JSONL mirrors and write to
   ``dashboard/data/latest.json``.

Resumability contract: the checkpoint file is the single source of
truth for "what's done." Re-running the script after a crash (or a
clean shutdown via SIGINT) picks up at the first un-processed candidate.
Pinecone upserts are idempotent by ``_id`` (thread/comment fullnames),
so re-processing a thread is a no-op rather than a duplicate write.

Files this script writes/reads:

* ``data/checkpoint.json`` — processed thread IDs + failure log (gitignored).
* ``data/tagged-comments.jsonl`` — append-only TaggedComments (gitignored).
* ``data/threads.jsonl`` — append-only fetched Threads (gitignored).
* ``data/tag-cost-log.jsonl`` — read-only; written by ``pipeline.tag``.
* ``dashboard/data/latest.json`` — final aggregate (committed; triggers
  Vercel redeploy when the cron pushes it).
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline import aggregate, disambiguate, discover, fetch, store, tag
from pipeline.config import REPO_ROOT
from pipeline.models import TaggedComment, Thread, ThreadCandidate

logger = logging.getLogger("mega_backfill")


# ---------------------------------------------------------------------------
# Constants & defaults
# ---------------------------------------------------------------------------

#: Checkpoint schema version. Bump when the on-disk shape changes so
#: old checkpoints fail loudly rather than parse-with-skew.
CHECKPOINT_VERSION: int = 1

#: Default path for the checkpoint file (relative to repo root).
DEFAULT_CHECKPOINT_PATH: Path = REPO_ROOT / "data" / "checkpoint.json"

#: Append-only mirror of every TaggedComment emitted. Re-read at aggregate
#: time so we don't have to round-trip through Pinecone (which would force
#: us to know every comment id in advance).
TAGGED_COMMENTS_PATH: Path = REPO_ROOT / "data" / "tagged-comments.jsonl"

#: Append-only mirror of every fetched Thread. Same rationale as
#: ``TAGGED_COMMENTS_PATH`` — aggregate.build_aggregate needs Thread
#: objects to compute reply counts and recency timestamps.
THREADS_PATH: Path = REPO_ROOT / "data" / "threads.jsonl"

#: tag.py writes one record per Claude call; we sum the cumulative cost
#: column for the progress report.
COST_LOG_PATH: Path = REPO_ROOT / "data" / "tag-cost-log.jsonl"

#: Final dashboard aggregate. Committed to the repo; the Vercel cron
#: (Phase 10) commits a new copy weekly and triggers a redeploy.
AGGREGATE_OUTPUT_PATH: Path = REPO_ROOT / "dashboard" / "data" / "latest.json"

#: Cost-estimation assumption — the spec quotes "~15 comments/thread" as
#: the historical JoVE corpus average. The estimator surfaces this as
#: ``per_call_assumptions`` so the operator can sanity-check.
DEFAULT_AVG_COMMENTS_PER_THREAD: int = 15

#: Progress report cadence — every N threads, print to stderr.
PROGRESS_REPORT_EVERY: int = 10


# ---------------------------------------------------------------------------
# Checkpoint I/O
# ---------------------------------------------------------------------------


def _new_checkpoint() -> dict[str, Any]:
    """Construct a fresh checkpoint dict with current schema version.

    Used when no checkpoint file exists yet — first run, or after
    deliberate reset.
    """

    return {
        "version": CHECKPOINT_VERSION,
        "started_at": datetime.now(tz=timezone.utc).isoformat(),
        "discovery_count": 0,
        "processed_thread_ids": [],
        "failed_thread_ids": {},
        "total_tagged_comments": 0,
        "estimated_cost_usd": 0.0,
    }


def load_checkpoint(path: Path) -> dict[str, Any]:
    """Load a checkpoint from disk, or return a fresh one if absent.

    Performs a lightweight version check — older or newer schema
    versions fail loudly so a partial-run state isn't silently
    misinterpreted.
    """

    if not path.exists():
        return _new_checkpoint()
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Checkpoint at {path} is not valid JSON: {exc}. "
            "Delete it to start fresh, or restore from a known-good copy."
        ) from exc

    version = data.get("version")
    if version != CHECKPOINT_VERSION:
        raise RuntimeError(
            f"Checkpoint at {path} has version={version}; expected "
            f"{CHECKPOINT_VERSION}. Delete the checkpoint to start fresh, "
            "or migrate the file by hand."
        )

    # Defensive defaults — older partial-writes may be missing newer keys.
    data.setdefault("discovery_count", 0)
    data.setdefault("processed_thread_ids", [])
    data.setdefault("failed_thread_ids", {})
    data.setdefault("total_tagged_comments", 0)
    data.setdefault("estimated_cost_usd", 0.0)
    return data


def save_checkpoint(checkpoint: dict[str, Any], path: Path) -> None:
    """Atomically persist the checkpoint to disk.

    Writes to a ``.tmp`` sidecar first, then renames into place — so a
    crash mid-write can't leave a half-written checkpoint that a future
    run would refuse to parse. ``os.replace`` is atomic on POSIX.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(checkpoint, f, indent=2, sort_keys=True)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Cost reporting
# ---------------------------------------------------------------------------


def read_running_cost_usd() -> float:
    """Sum the ``total_cost_usd`` column from ``data/tag-cost-log.jsonl``.

    Each line is one Claude API call (one classification + one voice
    judgment per comment, so two lines per comment). The cost log is
    written by ``pipeline.tag`` independently of this script's
    checkpoint — so a running total is always available even if the
    checkpoint hasn't been updated yet this iteration.

    Returns ``0.0`` if the cost log doesn't exist yet (first run).
    Errors reading individual lines are tolerated — we skip the
    malformed line and continue.
    """

    if not COST_LOG_PATH.exists():
        return 0.0
    total = 0.0
    try:
        with COST_LOG_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    # Mid-write line that's not yet flushed; ignore.
                    continue
                total += float(rec.get("total_cost_usd") or 0.0)
    except OSError as exc:
        logger.warning("Could not read cost log %s: %s", COST_LOG_PATH, exc)
    return total


def format_eta(elapsed_seconds: float, fraction_done: float) -> str:
    """Format an ETA string from elapsed seconds + fraction-of-work-done.

    Returns ``"--:--"`` while ``fraction_done`` is zero (avoid division
    by zero on the very first progress line, before any thread has
    completed).
    """

    if fraction_done <= 0:
        return "--:--"
    total_estimate = elapsed_seconds / fraction_done
    remaining = max(0.0, total_estimate - elapsed_seconds)
    hours = int(remaining // 3600)
    minutes = int((remaining % 3600) // 60)
    return f"{hours}h{minutes:02d}m"


def emit_progress(
    *,
    started_at: float,
    processed_count: int,
    total_count: int,
    tagged_comment_count: int,
    quiet: bool,
) -> None:
    """Print a progress line to stderr.

    Format::

        [HH:MM:SS] 47/1234 threads processed (3.8%), 247 tagged comments,
        $4.12 spent, 23h12m ETA
    """

    if quiet:
        return
    elapsed = time.monotonic() - started_at
    elapsed_hms = time.strftime("%H:%M:%S", time.gmtime(elapsed))
    fraction = (processed_count / total_count) if total_count > 0 else 0.0
    pct = fraction * 100.0
    cost_usd = read_running_cost_usd()
    eta = format_eta(elapsed, fraction)
    print(
        f"[{elapsed_hms}] {processed_count}/{total_count} threads processed "
        f"({pct:.1f}%), {tagged_comment_count} tagged comments, "
        f"${cost_usd:.2f} spent, {eta} ETA",
        file=sys.stderr,
        flush=True,
    )


# ---------------------------------------------------------------------------
# Append-only durable mirrors
# ---------------------------------------------------------------------------


def append_thread_to_jsonl(thread: Thread, path: Path | None = None) -> None:
    """Append one Thread to ``data/threads.jsonl`` as a single JSON line.

    Uses pydantic's ``model_dump_json`` so the JSON encodes datetimes
    as ISO strings — same shape ``aggregate.build_aggregate`` will read
    back at end-of-run.

    Path is resolved at call time (defaulting to the module-level
    ``THREADS_PATH``) so test monkeypatches that swap the constant
    after import take effect.
    """

    target = path if path is not None else THREADS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        f.write(thread.model_dump_json() + "\n")


def append_tagged_comments_to_jsonl(
    tagged_comments: list[TaggedComment], path: Path | None = None
) -> None:
    """Append every TaggedComment to ``data/tagged-comments.jsonl``.

    One JSON object per line so the file is durable to crashes — even
    if the script SIGKILLs mid-batch, every comment up to the last
    fsynced line is preserved. ``aggregate.build_aggregate`` reads this
    file back at end-of-run.

    Path is resolved at call time so test monkeypatches take effect.
    """

    if not tagged_comments:
        return
    target = path if path is not None else TAGGED_COMMENTS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        for tc in tagged_comments:
            f.write(tc.model_dump_json() + "\n")


def load_tagged_comments_from_jsonl(
    path: Path | None = None,
) -> list[TaggedComment]:
    """Read the entire ``data/tagged-comments.jsonl`` back into memory.

    Used at end-of-run to feed ``aggregate.build_aggregate``. Malformed
    lines are logged and skipped — the aggregate is a best-effort
    summary, not a strict-mode contract.

    Path is resolved at call time so test monkeypatches take effect.
    """

    target = path if path is not None else TAGGED_COMMENTS_PATH
    if not target.exists():
        return []
    out: list[TaggedComment] = []
    with target.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(TaggedComment.model_validate_json(line))
            except Exception as exc:
                logger.warning(
                    "Skipping malformed tagged-comment at %s:%d (%s)",
                    target,
                    line_no,
                    exc,
                )
    return out


def load_threads_from_jsonl(path: Path | None = None) -> dict[str, Thread]:
    """Read the entire ``data/threads.jsonl`` back into a ``{id: Thread}`` dict.

    Multiple appends of the same thread id (e.g. a script re-run that
    re-processed a thread because Pinecone state was lost) collapse to
    the latest entry — last writer wins.

    Path is resolved at call time so test monkeypatches take effect.
    """

    target = path if path is not None else THREADS_PATH
    if not target.exists():
        return {}
    out: dict[str, Thread] = {}
    with target.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                thread = Thread.model_validate_json(line)
                out[thread.id] = thread
            except Exception as exc:
                logger.warning(
                    "Skipping malformed thread at %s:%d (%s)",
                    target,
                    line_no,
                    exc,
                )
    return out


# ---------------------------------------------------------------------------
# SIGINT handling
# ---------------------------------------------------------------------------


class GracefulInterrupt:
    """Convert SIGINT into a polite "finish current thread" flag.

    Used in ``main_loop`` to detect Ctrl+C between iterations: the
    in-flight thread completes its fetch/tag/store cycle, the checkpoint
    is saved, then the loop exits with code 130 (the conventional
    SIGINT exit code).

    If the user hits Ctrl+C a *second* time during this graceful
    period, the second SIGINT is allowed to raise KeyboardInterrupt as
    usual — that's the operator escalation path when the current
    thread is itself stuck.
    """

    def __init__(self) -> None:
        self.triggered: bool = False
        self._original_handler: Any = None

    def __enter__(self) -> GracefulInterrupt:
        self._original_handler = signal.signal(signal.SIGINT, self._handle)
        return self

    def __exit__(self, *_exc_info: Any) -> None:
        signal.signal(signal.SIGINT, self._original_handler)

    def _handle(self, _signum: int, _frame: Any) -> None:
        if self.triggered:
            # Second SIGINT — escalate to the default handler so the
            # operator can hard-quit a stuck thread.
            signal.signal(signal.SIGINT, signal.default_int_handler)
            raise KeyboardInterrupt
        self.triggered = True
        print(
            "\n[mega_backfill] SIGINT received — finishing current thread and "
            "saving checkpoint. Press Ctrl+C again to hard-quit.",
            file=sys.stderr,
            flush=True,
        )


# ---------------------------------------------------------------------------
# Main backfill loop (extracted for testability)
# ---------------------------------------------------------------------------


def process_one_thread(
    candidate: ThreadCandidate,
    *,
    thread_id_hint: str | None = None,
) -> tuple[str, str, list[TaggedComment], Thread | None]:
    """Fetch → disambiguate → tag → store one candidate.

    Returns a four-tuple ``(thread_id, status, tagged_comments, thread)``
    where:

    * ``thread_id`` is the PRAW fullname, used as the checkpoint key.
      Falls back to the permalink when fetch fails before we can read
      ``thread.id`` (which is what ``thread_id_hint`` is for — the
      caller can pass the permalink).
    * ``status`` is one of ``"processed"``, ``"skipped:<reason>"``.
    * ``tagged_comments`` is the list of TaggedComments emitted for
      this thread (empty on skip).
    * ``thread`` is the fetched Thread, or ``None`` when fetching was
      skipped.

    Raises exceptions on fetch/tag/store failures — the caller wraps
    this in a broad ``try/except`` to record the failure and continue.
    """

    thread = fetch.fetch_thread(candidate)
    in_scope, reason = disambiguate.is_in_scope(thread)
    if not in_scope:
        return (thread.id, f"skipped:{reason}", [], thread)

    tagged_comments: list[TaggedComment] = []
    for comment in thread.comments:
        tagged = tag.tag_comment(comment, thread)
        tagged_comments.append(tagged)

    # Pinecone upsert (idempotent by id, so a re-processed thread is a no-op).
    store.upsert_thread(thread, tagged_comments)

    # Vault voice-candidate write (only emits when at least one is YES).
    voice_yeses = [tc for tc in tagged_comments if tc.voice_candidate]
    if voice_yeses:
        store.write_voice_candidates(voice_yeses, thread)

    _ = thread_id_hint  # Reserved — used by failure path in caller.
    return (thread.id, "processed", tagged_comments, thread)


def main_loop(
    candidates: list[ThreadCandidate],
    checkpoint: dict[str, Any],
    *,
    checkpoint_path: Path,
    quiet: bool = False,
    interrupt: GracefulInterrupt | None = None,
) -> None:
    """Drive the discovery → fetch → tag → store loop with checkpoint saves.

    Iterates ``candidates`` from front to back, skipping any whose
    parent-thread id is already in ``checkpoint["processed_thread_ids"]``.
    The skip-set is keyed by both permalink (cheap pre-fetch check) and
    thread id (definitive post-fetch check) so unauth retries don't
    re-fetch threads we already finished.

    Failures are recorded into ``checkpoint["failed_thread_ids"]`` with
    a one-line message — full traceback goes to the logger.
    """

    processed_ids: set[str] = set(checkpoint["processed_thread_ids"])
    processed_permalinks: set[str] = {
        p for p in processed_ids if p.startswith("http")
    }
    started_at = time.monotonic()
    total = len(candidates)
    iteration = 0
    tagged_count_so_far = checkpoint.get("total_tagged_comments", 0)

    for candidate in candidates:
        if interrupt is not None and interrupt.triggered:
            logger.info("SIGINT received — exiting main loop.")
            return

        # Cheap pre-fetch skip on permalink. The post-fetch ``thread.id``
        # check is the definitive one, but this avoids a network call for
        # the obvious already-processed cases.
        if candidate.permalink in processed_permalinks:
            continue

        iteration += 1
        try:
            (
                thread_id,
                status,
                tagged_comments,
                thread,
            ) = process_one_thread(
                candidate, thread_id_hint=candidate.permalink
            )

            # Post-fetch dedupe: did we already process this thread under a
            # different permalink? Mark it processed and continue.
            if thread_id in processed_ids:
                processed_ids.add(candidate.permalink)
                processed_permalinks.add(candidate.permalink)
                checkpoint["processed_thread_ids"] = sorted(processed_ids)
                save_checkpoint(checkpoint, checkpoint_path)
                continue

            # Append to durable mirrors *before* marking processed so a
            # crash between the JSONL append and the checkpoint save
            # leaves the work re-discoverable on the next run.
            if thread is not None:
                append_thread_to_jsonl(thread)
            if tagged_comments:
                append_tagged_comments_to_jsonl(tagged_comments)

            tagged_count_so_far += len(tagged_comments)

            processed_ids.add(thread_id)
            processed_ids.add(candidate.permalink)
            processed_permalinks.add(candidate.permalink)
            checkpoint["processed_thread_ids"] = sorted(processed_ids)
            checkpoint["total_tagged_comments"] = tagged_count_so_far

            # Update the running cost estimate from the cost log.
            checkpoint["estimated_cost_usd"] = read_running_cost_usd()
            save_checkpoint(checkpoint, checkpoint_path)

            logger.debug(
                "Thread %s %s (tagged=%d)",
                thread_id,
                status,
                len(tagged_comments),
            )
        except Exception as exc:
            # Catch broadly — keep the loop alive on a single-thread error.
            # KeyboardInterrupt is a BaseException subclass so it's NOT
            # caught here, which lets the GracefulInterrupt path remain
            # responsive.
            tb = traceback.format_exc()
            logger.error(
                "Thread %s failed: %s\n%s",
                candidate.permalink,
                exc,
                tb,
            )
            checkpoint["failed_thread_ids"][candidate.permalink] = str(exc)
            save_checkpoint(checkpoint, checkpoint_path)

        if iteration % PROGRESS_REPORT_EVERY == 0:
            emit_progress(
                started_at=started_at,
                processed_count=iteration,
                total_count=total,
                tagged_comment_count=tagged_count_so_far,
                quiet=quiet,
            )

    # Final progress line at end-of-loop.
    emit_progress(
        started_at=started_at,
        processed_count=iteration,
        total_count=total,
        tagged_comment_count=tagged_count_so_far,
        quiet=quiet,
    )


# ---------------------------------------------------------------------------
# Final-report aggregate build
# ---------------------------------------------------------------------------


def build_and_write_aggregate(
    *,
    time_horizon: str,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Read the durable JSONL mirrors and write the final aggregate JSON.

    Returns a slim summary dict for the final stdout report — total
    threads / comments and the top-3 themes by frequency per sentiment.
    """

    target = output_path if output_path is not None else AGGREGATE_OUTPUT_PATH
    tagged_comments = load_tagged_comments_from_jsonl()
    threads = load_threads_from_jsonl()

    agg = aggregate.build_aggregate(
        tagged_comments=tagged_comments,
        threads=threads,
        time_horizon=time_horizon,
    )
    store.write_aggregate_json(agg.model_dump(mode="json"), target)

    # Build the top-3-themes-per-sentiment summary.
    by_sentiment: dict[str, list[tuple[str, int, int]]] = {}
    for row in agg.rows:
        by_sentiment.setdefault(row.sentiment, []).append(
            (row.theme, row.thread_count, row.comment_count)
        )
    top3: dict[str, list[dict[str, Any]]] = {}
    for sentiment, rows in by_sentiment.items():
        # Sort by thread_count desc, then comment_count desc.
        rows.sort(key=lambda r: (-r[1], -r[2]))
        top3[sentiment] = [
            {"theme": t, "thread_count": tc, "comment_count": cc}
            for t, tc, cc in rows[:3]
        ]

    return {
        "total_threads": agg.total_threads,
        "total_comments": agg.total_comments,
        "top3_by_sentiment": top3,
        "aggregate_path": str(target),
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build and parse the CLI argument namespace.

    Extracted so tests can inject argv directly. Keeps :func:`run`
    pure-data: it takes the parsed namespace and never re-parses.
    """

    parser = argparse.ArgumentParser(
        prog="mega_backfill",
        description=(
            "Voice of Academia mega-backfill orchestrator — Phase 8. "
            "Runs the full discover→fetch→tag→store→aggregate pipeline "
            "over the 2010+ historical JoVE corpus. Resumable; "
            "checkpoint-driven."
        ),
    )
    parser.add_argument(
        "--time-horizon",
        default="2010-01-01",
        help=(
            "Earliest thread date to include (ISO ``YYYY-MM-DD``). "
            "Default: 2010-01-01 per spec D7."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the cost forecast and discovery count, then exit 0 "
            "without fetching or tagging any threads."
        ),
    )
    parser.add_argument(
        "--checkpoint-path",
        type=Path,
        default=DEFAULT_CHECKPOINT_PATH,
        help=(
            "Path to the checkpoint JSON file. "
            f"Default: {DEFAULT_CHECKPOINT_PATH}."
        ),
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=100.0,
        help=(
            "Maximum estimated LLM spend (USD) before aborting. "
            "Default: 100. The estimate is per "
            "``pipeline.tag.estimate_mega_backfill_cost`` and carries "
            "±40%% uncertainty — set this with margin for surprises."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-10-thread progress lines on stderr.",
    )
    return parser.parse_args(argv)


def _print_cost_forecast(forecast: dict[str, Any]) -> None:
    """Pretty-print the cost forecast dict to stdout.

    Used in both ``--dry-run`` and the normal cost-gate path so the
    operator sees the same breakdown whatever the entry point.
    """

    totals = forecast["totals"]
    print("=" * 72)
    print("Mega-backfill cost forecast")
    print("=" * 72)
    print(f"  Threads discovered:          {forecast['num_threads']:>10,}")
    print(
        f"  Avg comments/thread (est.):  {forecast['avg_comments_per_thread']:>10,}"
    )
    print(f"  Total comments (est.):       {forecast['total_comments']:>10,}")
    print()
    print(
        f"  Haiku classification:        ${totals['classify_total_usd']:>10,.2f} "
        f"USD ({totals['classify_total_input_tokens']:>14,} in / "
        f"{totals['classify_total_output_tokens']:>10,} out tokens)"
    )
    print(
        f"  Sonnet voice judgment:       ${totals['voice_total_usd']:>10,.2f} "
        f"USD ({totals['voice_total_input_tokens']:>14,} in / "
        f"{totals['voice_total_output_tokens']:>10,} out tokens)"
    )
    print(
        f"  GRAND TOTAL (est.):          ${totals['grand_total_usd']:>10,.2f} USD"
    )
    print()
    print(f"  {forecast['uncertainty_note']}")
    print("=" * 72)


def run(args: argparse.Namespace) -> int:
    """Top-level entry: returns the process exit code.

    Exit codes:

    * ``0`` — normal completion (or ``--dry-run`` success).
    * ``1`` — generic failure (unexpected error during setup/aggregate).
    * ``2`` — cost gate aborted (forecast > ``--max-cost-usd``).
    * ``130`` — SIGINT received and graceful shutdown completed.

    Keeps :func:`main` thin so tests can call ``run(parsed_args)``
    without going through ``sys.argv``.
    """

    logging.basicConfig(
        level=logging.INFO if not args.quiet else logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    # 1. Discovery — collect every candidate into a list.
    logger.info("Phase 8 mega-backfill starting — running discovery...")
    horizon_dt = datetime.strptime(args.time_horizon, "%Y-%m-%d").replace(
        tzinfo=timezone.utc
    )
    candidates = list(discover.discover_all(time_horizon=horizon_dt))
    n = len(candidates)
    logger.info("Discovery complete: %d candidate(s)", n)

    if n == 0:
        print(
            "No candidates discovered — nothing to do. Check Reddit "
            "credentials and seed-threads.json.",
            file=sys.stderr,
        )
        return 0

    # 2. Cost gate — estimate before any LLM tagging.
    forecast = tag.estimate_mega_backfill_cost(
        num_threads=n,
        avg_comments_per_thread=DEFAULT_AVG_COMMENTS_PER_THREAD,
    )
    grand_total = forecast["totals"]["grand_total_usd"]

    if args.dry_run:
        _print_cost_forecast(forecast)
        print("\n[--dry-run] No threads fetched or tagged.")
        return 0

    if grand_total > args.max_cost_usd:
        _print_cost_forecast(forecast)
        print(
            f"\nABORT: estimated cost ${grand_total:.2f} exceeds "
            f"--max-cost-usd ${args.max_cost_usd:.2f}.\n"
            "Either raise --max-cost-usd, narrow --time-horizon, or "
            "review the discovery scope before re-running.",
            file=sys.stderr,
        )
        return 2

    # Print the forecast even in the happy path so the operator has it
    # in their terminal scrollback.
    _print_cost_forecast(forecast)

    # 3. Checkpoint load / init.
    checkpoint = load_checkpoint(args.checkpoint_path)
    checkpoint["discovery_count"] = n
    save_checkpoint(checkpoint, args.checkpoint_path)

    already_done = len(
        [p for p in checkpoint["processed_thread_ids"] if p.startswith("t3_")]
    )
    logger.info(
        "Checkpoint loaded: %d thread(s) already processed, "
        "%d failure(s) recorded",
        already_done,
        len(checkpoint["failed_thread_ids"]),
    )

    # 4. Main loop (SIGINT-safe).
    exit_code = 0
    with GracefulInterrupt() as interrupt:
        try:
            main_loop(
                candidates=candidates,
                checkpoint=checkpoint,
                checkpoint_path=args.checkpoint_path,
                quiet=args.quiet,
                interrupt=interrupt,
            )
        except KeyboardInterrupt:
            # Second SIGINT during the in-flight thread's wrap-up —
            # checkpoint may be slightly behind reality but the JSONL
            # mirrors are durable.
            logger.warning(
                "Second SIGINT received — exiting hard. "
                "Re-run to resume from checkpoint."
            )
            return 130

        if interrupt.triggered:
            logger.warning(
                "Graceful shutdown complete. Re-run to resume from "
                "checkpoint (%d/%d threads processed).",
                len(checkpoint["processed_thread_ids"]),
                n,
            )
            return 130

    # 5. Build aggregate and write to dashboard/data/latest.json.
    logger.info("Main loop complete — building dashboard aggregate...")
    try:
        summary = build_and_write_aggregate(time_horizon=args.time_horizon)
    except Exception as exc:
        logger.exception("Aggregate build failed: %s", exc)
        return 1

    # 6. Final report to stdout.
    print()
    print("=" * 72)
    print("Mega-backfill complete")
    print("=" * 72)
    print(f"  Total threads:              {summary['total_threads']:>10,}")
    print(f"  Total tagged comments:      {summary['total_comments']:>10,}")
    print(f"  Cumulative cost:            ${read_running_cost_usd():>10,.2f}")
    print(f"  Aggregate JSON:             {summary['aggregate_path']}")
    print()
    print("  Top 3 themes by frequency (by sentiment):")
    for sentiment in ("positive", "neutral", "negative"):
        rows = summary["top3_by_sentiment"].get(sentiment, [])
        if not rows:
            print(f"    {sentiment:<8}  (no rows)")
            continue
        for r in rows:
            print(
                f"    {sentiment:<8}  {r['theme']:<30}  "
                f"threads={r['thread_count']:>4}  comments={r['comment_count']:>5}"
            )
    print("=" * 72)

    return exit_code


def main() -> None:
    """Process entrypoint — parses argv and exits with the run() exit code."""

    args = parse_args()
    sys.exit(run(args))


if __name__ == "__main__":  # pragma: no cover - module-as-script guard
    main()
