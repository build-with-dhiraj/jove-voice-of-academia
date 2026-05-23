"""Phase 10 — weekly-delta orchestrator (lighter cousin of mega_backfill).

Where :mod:`scripts.mega_backfill` is the one-shot historical scan that
walks the entire 2010+ corpus, this module is the *recurring* delta-scan
the GitHub Actions cron runs each Sunday. The two scripts share the same
public pipeline API (``discover`` → ``fetch`` → ``disambiguate`` → ``tag``
→ ``store`` → ``aggregate``) but differ in three important ways:

1. **Time horizon is short** — the delta scan filters discovery to
   threads with activity since ``data/last_run.json``'s ``last_run_at``
   (or a 7-day fallback if the checkpoint is missing). The corpus per
   weekly run is typically 10-50 threads vs the backfill's 2000+.

2. **Re-fetches threads with new activity** — JoVE-discussing threads
   can attract late comments on old posts (this happens in academia —
   someone googles a journal name months later and replies). On every
   weekly run we re-fetch any thread whose ``updated_at`` (best-effort:
   max ``created_utc`` across its comments at last fetch) is newer than
   ``last_run_at``, and re-tag the delta. Pinecone upserts are idempotent
   so unchanged comments are no-ops.

3. **Lower cost ceiling** — defaults to ``--max-cost-usd 30`` since the
   delta corpus is small. Bumping above this requires explicit operator
   override (a sign the discovery filter isn't tight enough).

CLI::

    uv run python -m pipeline.weekly_delta [--max-cost-usd 30] \\
        [--dry-run] [--force-time-horizon ISO_DATETIME]

The GitHub Actions workflow (``.github/workflows/weekly-scan.yml``)
invokes this with no extra flags. After a successful run the workflow
commits ``dashboard/data/latest.json`` *and* ``data/last_run.json`` to
``main`` — the push triggers Vercel auto-deploy.

Operations contract with Phase 8 (the manual mega-backfill):

* Phase 8 runs once (Week 1) on D's machine. It writes
  ``data/threads.jsonl`` + ``data/tagged-comments.jsonl`` durable
  mirrors but does NOT write ``data/last_run.json``.
* Before the cron is enabled (Week 2), the operator runs
  ``uv run python -m pipeline.weekly_delta --force-time-horizon <T>``
  where ``<T>`` is the ISO timestamp of Phase 8's completion. That run
  initialises ``data/last_run.json`` so the first cron run picks up
  from a known anchor instead of the 7-day fallback.
* Alternatively the operator can let the first cron run use the 7-day
  fallback. With Phase 8 fresh, this means at most one week of overlap
  — which is fine because Pinecone upserts are idempotent.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pipeline import aggregate, disambiguate, discover, fetch, store, tag
from pipeline.config import REPO_ROOT
from pipeline.models import TaggedComment, Thread, ThreadCandidate

# Reuse the durable-mirror helpers from Phase 8 — same JSONL files
# (``data/threads.jsonl``, ``data/tagged-comments.jsonl``) are appended
# to so the aggregate covers backfill+delta corpus inseparably. The
# weekly delta isn't a separate corpus, it's the *continuation* of the
# mega-backfill's corpus.
#
# The four ``*_PATH`` constants are re-exported (used by tests to
# monkeypatch in-place); ruff sees them as "unused" within this module
# body because the tests address them via ``weekly_delta.THREADS_PATH``.
from scripts.mega_backfill import (
    AGGREGATE_OUTPUT_PATH,
    COST_LOG_PATH,  # noqa: F401 - re-exported for test monkeypatch
    TAGGED_COMMENTS_PATH,  # noqa: F401 - re-exported for test monkeypatch
    THREADS_PATH,  # noqa: F401 - re-exported for test monkeypatch
    append_tagged_comments_to_jsonl,
    append_thread_to_jsonl,
    load_tagged_comments_from_jsonl,
    load_threads_from_jsonl,
    read_running_cost_usd,
)

logger = logging.getLogger("weekly_delta")


# ---------------------------------------------------------------------------
# Constants & defaults
# ---------------------------------------------------------------------------

#: Path to the ``last_run.json`` checkpoint. Single source of truth for
#: "when was the last successful weekly scan?". Committed to the repo
#: (it's a tiny file) so the GH Actions runner doesn't need a persistent
#: workspace. Gitignored at the time of writing because we wanted to
#: keep it private during the build phase, but the cron workflow's
#: ``git add data/last_run.json`` step bypasses that — the workflow
#: uses ``-f`` if needed (see the workflow YAML for the exact command).
LAST_RUN_PATH: Path = REPO_ROOT / "data" / "last_run.json"

#: Default lookback when ``data/last_run.json`` is missing (first run
#: ever, or operator deleted it). 7 days is the cadence of the cron, so
#: the fallback covers exactly the window we'd expect a healthy run to
#: discover.
LAST_RUN_FALLBACK_DAYS: int = 7

#: Cost ceiling default for weekly runs. Much lower than the backfill's
#: typical $100-700 ceiling because the delta corpus is tiny. A delta
#: forecast above $30 is a red flag — usually means the discovery
#: filter isn't excluding stale Ring 3 hits properly, or the operator
#: forgot to set ``--force-time-horizon`` after a long gap.
DEFAULT_MAX_COST_USD: float = 30.0

#: Last-run checkpoint schema version. Bump if the on-disk shape
#: changes.
LAST_RUN_VERSION: int = 1

#: Cost-estimation assumption — mirror Phase 8's value so the forecast
#: stays comparable across the two scripts.
DEFAULT_AVG_COMMENTS_PER_THREAD: int = 15


# ---------------------------------------------------------------------------
# last_run.json I/O
# ---------------------------------------------------------------------------


def load_last_run(path: Path | None = None) -> datetime | None:
    """Return the ``last_run_at`` UTC datetime, or ``None`` if absent/invalid.

    Returns ``None`` when:

    * The file doesn't exist (first run ever).
    * The JSON parses but is missing ``last_run_at``.
    * The ``last_run_at`` value isn't a parseable ISO timestamp.

    Each missing-or-invalid case is logged but doesn't raise — the
    caller falls back to ``LAST_RUN_FALLBACK_DAYS``.

    A version mismatch *does* raise, because that means an operator (or
    a future-self bump) has changed the schema and the current code
    can't safely interpret the file.

    ``path`` defaults to the module-level :data:`LAST_RUN_PATH` resolved
    at call time, so test monkeypatches that swap the constant after
    import take effect.
    """

    if path is None:
        path = LAST_RUN_PATH
    if not path.exists():
        logger.info("No last_run.json at %s — first weekly run.", path)
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Could not parse %s (%s) — falling back to %d-day lookback.",
            path,
            exc,
            LAST_RUN_FALLBACK_DAYS,
        )
        return None

    version = data.get("version")
    if version != LAST_RUN_VERSION:
        raise RuntimeError(
            f"last_run.json at {path} has version={version}; expected "
            f"{LAST_RUN_VERSION}. Delete the file to start fresh, or "
            "migrate the schema by hand."
        )

    raw = data.get("last_run_at")
    if not raw:
        logger.warning(
            "last_run.json missing ``last_run_at`` — falling back to "
            "%d-day lookback.",
            LAST_RUN_FALLBACK_DAYS,
        )
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        logger.warning(
            "last_run.json ``last_run_at=%r`` not parseable — "
            "falling back to %d-day lookback.",
            raw,
            LAST_RUN_FALLBACK_DAYS,
        )
        return None

    # Normalize naive -> UTC. The cron writes ISO with explicit ``+00:00``
    # offset so this is mostly defensive against hand-edited files.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def save_last_run(
    last_run_at: datetime,
    path: Path | None = None,
    *,
    threads_processed: int = 0,
    comments_tagged: int = 0,
    cost_usd: float = 0.0,
) -> None:
    """Atomically write the ``last_run.json`` checkpoint.

    Writes to a ``.tmp`` sidecar and renames into place (atomic on POSIX)
    so a crash mid-write can't leave a half-formed file the next run
    refuses to parse.

    The extra fields (``threads_processed``, ``comments_tagged``,
    ``cost_usd``) are informational — they make the file diff in the
    cron's auto-commit human-skimmable for the operator reviewing what
    each week did.

    ``path`` defaults to the module-level :data:`LAST_RUN_PATH` resolved
    at call time, so test monkeypatches take effect.
    """

    if path is None:
        path = LAST_RUN_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": LAST_RUN_VERSION,
        "last_run_at": last_run_at.isoformat(),
        "threads_processed": threads_processed,
        "comments_tagged": comments_tagged,
        "cost_usd": round(cost_usd, 4),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Delta selection — which threads to process this run?
# ---------------------------------------------------------------------------


def resolve_time_horizon(
    last_run: datetime | None,
    *,
    now: datetime | None = None,
    fallback_days: int = LAST_RUN_FALLBACK_DAYS,
) -> datetime:
    """Decide the discovery time horizon for this delta run.

    * If ``last_run`` is set → use it directly.
    * Otherwise fall back to ``now - fallback_days``.

    Extracted as a pure helper so tests don't need to monkey-patch
    ``datetime.now``.
    """

    if last_run is not None:
        return last_run
    current = now if now is not None else datetime.now(tz=timezone.utc)
    return current - timedelta(days=fallback_days)


def known_thread_ids_needing_refetch(
    last_run: datetime | None,
    *,
    threads_path: Path | None = None,
    tagged_comments_path: Path | None = None,
) -> set[str]:
    """Return ids of threads whose comment set may have grown since last run.

    Heuristic: for each thread in the durable JSONL mirror, find the
    most recent ``created_utc`` of any tagged comment. If we have *no*
    ``last_run`` (first delta scan), every known thread is a candidate
    for re-fetch — but that's an extreme case the caller can handle by
    skipping re-fetch entirely. With a known ``last_run``, a thread is a
    re-fetch candidate iff none of its tagged comments have
    ``created_utc >= last_run`` AND the thread itself is younger than
    180 days (we don't keep re-fetching ancient threads forever).

    Returns a set of thread ids (PRAW fullnames like ``t3_abc123``).

    Note: this is a *best-effort* freshness signal. The fetch layer
    pulls the full comment tree regardless, so a re-fetched thread that
    happens to have nothing new costs us one PRAW request and zero LLM
    calls (each re-fetched comment is already tagged in the durable
    mirror — see the dedupe filter in ``run`` below).
    """

    if last_run is None:
        # No anchor — don't try to be clever. The discovery layer's
        # time-horizon filter will catch new threads; we leave older
        # threads alone on the first run.
        return set()

    threads = load_threads_from_jsonl(path=threads_path)
    if not threads:
        return set()
    tagged = load_tagged_comments_from_jsonl(path=tagged_comments_path)

    # Build "most recent comment in each thread" from the tagged mirror.
    latest_by_thread: dict[str, datetime] = {}
    for tc in tagged:
        tid = tc.thread_id
        if not tid:
            continue
        # ``tc.tagged_at`` is when WE tagged it; we want the comment's
        # ``created_utc``. The tagged-comments JSONL doesn't carry that
        # directly, so we conservatively pick threads with NO tagged
        # comment newer than last_run.
        prev = latest_by_thread.get(tid)
        if prev is None or tc.tagged_at > prev:
            latest_by_thread[tid] = tc.tagged_at

    # A thread is a refetch candidate iff:
    #  - we know about it (it's in ``threads``)
    #  - its newest tagged comment predates ``last_run`` (so there might
    #    be new ones since)
    #  - the thread itself is less than ~180 days old (avoid endless
    #    re-fetch of ancient threads — the long-tail of new comments on
    #    old posts is real but bounded)
    refetch_horizon = last_run - timedelta(days=180)
    out: set[str] = set()
    for tid, thread in threads.items():
        if thread.created_utc < refetch_horizon:
            continue
        newest_tagged = latest_by_thread.get(tid)
        if newest_tagged is None:
            # Untagged thread in the mirror — treat as a refetch candidate.
            out.add(tid)
            continue
        if newest_tagged < last_run:
            out.add(tid)
    return out


# ---------------------------------------------------------------------------
# Per-thread processing
# ---------------------------------------------------------------------------


def process_one_thread(
    candidate: ThreadCandidate,
    *,
    already_tagged_comment_ids: set[str],
) -> tuple[str, str, list[TaggedComment], Thread | None]:
    """Fetch → disambiguate → tag (only new comments) → store one candidate.

    Mirrors :func:`scripts.mega_backfill.process_one_thread` but skips
    LLM calls for comments already in ``already_tagged_comment_ids`` —
    that's the dedupe path that makes re-fetching cheap.

    Returns ``(thread_id, status, NEW tagged_comments only, thread)``.
    The status is ``"processed"``, ``"skipped:<reason>"``, or
    ``"nothing_new"`` (thread re-fetched but every comment was already
    tagged — no LLM cost incurred).
    """

    thread = fetch.fetch_thread(candidate)
    in_scope, reason = disambiguate.is_in_scope(thread)
    if not in_scope:
        return (thread.id, f"skipped:{reason}", [], thread)

    new_tagged: list[TaggedComment] = []
    for comment in thread.comments:
        if comment.id in already_tagged_comment_ids:
            continue
        tagged_comment = tag.tag_comment(comment, thread)
        new_tagged.append(tagged_comment)

    # Even when there are no NEW tagged comments, we re-upsert the thread
    # record (its score/num_comments may have changed). Pinecone upserts
    # by id so this is idempotent.
    store.upsert_thread(thread, new_tagged)

    # Voice candidates only for the new comments — older ones already
    # had their voice-write at original tagging time.
    voice_yeses = [tc for tc in new_tagged if tc.voice_candidate]
    if voice_yeses:
        store.write_voice_candidates(voice_yeses, thread)

    status = "processed" if new_tagged else "nothing_new"
    return (thread.id, status, new_tagged, thread)


# ---------------------------------------------------------------------------
# Final aggregate build
# ---------------------------------------------------------------------------


def build_and_write_aggregate(
    *,
    time_horizon: str,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Rebuild the dashboard aggregate from the durable JSONL mirrors.

    Identical logic to :func:`scripts.mega_backfill.build_and_write_aggregate`
    — at end-of-run we re-read every Thread + TaggedComment ever
    written (backfill + every weekly delta to date) and project to the
    dashboard's :class:`Aggregate`. ``time_horizon`` here is the
    dashboard's *display* horizon (the spec-D7 2010 cutoff), not the
    delta scan's discovery horizon.
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
    return {
        "total_threads": agg.total_threads,
        "total_comments": agg.total_comments,
        "aggregate_path": str(target),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build and parse the CLI argument namespace."""

    parser = argparse.ArgumentParser(
        prog="weekly_delta",
        description=(
            "Voice of Academia weekly-delta scan — Phase 10. "
            "Discovers Reddit threads with activity since last run, "
            "re-fetches active threads from prior weeks, tags new "
            "comments, refreshes the dashboard aggregate. Designed to "
            "run inside a GitHub Actions cron weekly."
        ),
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=DEFAULT_MAX_COST_USD,
        help=(
            "Maximum estimated LLM spend (USD) before aborting. "
            f"Default: {DEFAULT_MAX_COST_USD}. Weekly delta should rarely "
            "exceed this — a forecast above default is a sign the "
            "discovery horizon is set too far back or last_run.json is "
            "stale."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the discovery summary + cost forecast and exit 0 "
            "without fetching or tagging any threads."
        ),
    )
    parser.add_argument(
        "--force-time-horizon",
        type=str,
        default=None,
        help=(
            "Override ``last_run.json`` and force a specific ISO 8601 "
            "timestamp as the discovery horizon. Use to seed the first "
            "post-Phase-8 cron run with the backfill's completion time, "
            "or to redo a missed week without waiting for the cron."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-thread progress lines on stderr.",
    )
    return parser.parse_args(argv)


def _parse_iso_horizon(value: str) -> datetime:
    """Parse an ISO 8601 string from the CLI into an aware UTC datetime."""

    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def run(args: argparse.Namespace) -> int:
    """Top-level entry: returns the process exit code.

    Exit codes:

    * ``0`` — normal completion (or ``--dry-run`` success).
    * ``1`` — generic failure (unexpected error during setup/aggregate).
    * ``2`` — cost gate aborted (forecast exceeded ``--max-cost-usd``).
    """

    logging.basicConfig(
        level=logging.INFO if not args.quiet else logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    # 1. Resolve time horizon.
    if args.force_time_horizon:
        try:
            horizon = _parse_iso_horizon(args.force_time_horizon)
        except ValueError as exc:
            print(
                f"ERROR: --force-time-horizon must be ISO 8601 "
                f"(e.g. 2026-05-23T00:00:00+00:00); got {args.force_time_horizon!r} "
                f"({exc})",
                file=sys.stderr,
            )
            return 1
        logger.info(
            "Using forced time horizon %s (overriding last_run.json).", horizon
        )
        last_run_anchor: datetime | None = horizon
    else:
        last_run_anchor = load_last_run()
        horizon = resolve_time_horizon(last_run_anchor)
        if last_run_anchor is not None:
            logger.info(
                "Using last_run.json anchor: %s (delta scan since this).",
                horizon,
            )
        else:
            logger.info(
                "No last_run.json — falling back to %d-day lookback (%s).",
                LAST_RUN_FALLBACK_DAYS,
                horizon,
            )

    # 2. Discovery — collect candidates from rings + refetch known active.
    logger.info("Phase 10 weekly delta starting — running discovery...")
    discovered = list(discover.discover_all(time_horizon=horizon))
    logger.info("Discovery: %d new candidate(s) from rings", len(discovered))

    # Build known-thread refetch set (threads we already have whose
    # comment set may have grown).
    refetch_ids = known_thread_ids_needing_refetch(last_run_anchor)
    logger.info(
        "Refetch: %d known thread(s) flagged for refresh", len(refetch_ids)
    )

    # Compose a single candidate list. For refetch candidates we
    # synthesize a ThreadCandidate from the known thread's permalink so
    # the fetch layer can handle it through the same path.
    known_threads = load_threads_from_jsonl()
    refetch_candidates: list[ThreadCandidate] = []
    discovered_ids = {
        discover.extract_thread_id(c.permalink) for c in discovered
    }
    for tid in refetch_ids:
        thread = known_threads.get(tid)
        if thread is None:
            continue
        # Skip a refetch that discovery already surfaced (avoid double-
        # processing the same thread).
        if discover.extract_thread_id(thread.permalink) in discovered_ids:
            continue
        refetch_candidates.append(
            ThreadCandidate(
                permalink=thread.permalink,
                subreddit=thread.subreddit,
                discovered_via="ring1_subreddit_enum",
                discovered_at=datetime.now(tz=timezone.utc),
                seed_query=None,
            )
        )
    all_candidates = discovered + refetch_candidates
    n_total = len(all_candidates)
    logger.info(
        "Total candidates to process: %d "
        "(%d newly discovered + %d known-thread refetch)",
        n_total,
        len(discovered),
        len(refetch_candidates),
    )

    if n_total == 0:
        # Nothing to do — but we still write last_run.json and rebuild
        # the aggregate. The aggregate may have changed if voice panel
        # was curated since last run.
        logger.info(
            "No candidates — touching last_run.json and rebuilding "
            "aggregate from existing corpus."
        )
        if not args.dry_run:
            try:
                summary = build_and_write_aggregate(time_horizon="2010-01-01")
                save_last_run(
                    last_run_at=datetime.now(tz=timezone.utc),
                    threads_processed=0,
                    comments_tagged=0,
                    cost_usd=0.0,
                )
                logger.info(
                    "Aggregate rewritten: %d threads / %d comments at %s",
                    summary["total_threads"],
                    summary["total_comments"],
                    summary["aggregate_path"],
                )
            except Exception as exc:
                logger.exception("Aggregate build failed: %s", exc)
                return 1
        return 0

    # 3. Cost gate — estimate before any LLM tagging.
    forecast = tag.estimate_mega_backfill_cost(
        num_threads=n_total,
        avg_comments_per_thread=DEFAULT_AVG_COMMENTS_PER_THREAD,
    )
    grand_total = forecast["totals"]["grand_total_usd"]
    logger.info(
        "Cost forecast: $%.2f (n=%d threads, %d comments avg)",
        grand_total,
        n_total,
        DEFAULT_AVG_COMMENTS_PER_THREAD,
    )

    if args.dry_run:
        print("=" * 72)
        print("Weekly delta dry-run")
        print("=" * 72)
        print(f"  Time horizon:               {horizon.isoformat()}")
        print(f"  New discoveries:            {len(discovered):>6}")
        print(f"  Refetch candidates:         {len(refetch_candidates):>6}")
        print(f"  Total candidates:           {n_total:>6}")
        print(f"  Forecast LLM cost (est.):   ${grand_total:>7.2f}")
        print(f"  Max-cost ceiling:           ${args.max_cost_usd:>7.2f}")
        print()
        print("  [--dry-run] No threads fetched or tagged.")
        return 0

    if grand_total > args.max_cost_usd:
        print(
            f"ABORT: estimated cost ${grand_total:.2f} exceeds "
            f"--max-cost-usd ${args.max_cost_usd:.2f}.\n"
            f"  Discovered:   {len(discovered)}\n"
            f"  Refetch:      {len(refetch_candidates)}\n"
            f"  Total:        {n_total}\n"
            "Investigate before re-running — usually means discovery is "
            "pulling stale results because last_run.json is missing or "
            "older than expected.",
            file=sys.stderr,
        )
        return 2

    # 4. Build the already-tagged-comment-id set for dedupe-on-refetch.
    already_tagged: set[str] = {
        tc.comment_id for tc in load_tagged_comments_from_jsonl()
    }
    logger.info(
        "Loaded %d already-tagged comment id(s) for dedupe", len(already_tagged)
    )

    # 5. Main loop — fetch / disambiguate / tag (new only) / store.
    run_started_at = datetime.now(tz=timezone.utc)
    processed_count = 0
    tagged_count = 0
    failed: dict[str, str] = {}

    for cand in all_candidates:
        try:
            thread_id, status, new_tagged, thread = process_one_thread(
                cand, already_tagged_comment_ids=already_tagged
            )
            # process_one_thread wraps every disambiguate rejection reason
            # with a ``skipped:`` prefix (see line 364), so the JSONL mirror
            # excludes ALL out-of-scope threads — not just one specific reason.
            if thread is not None and not status.startswith("skipped:"):
                append_thread_to_jsonl(thread)
            if new_tagged:
                append_tagged_comments_to_jsonl(new_tagged)
                # Update the running dedupe set so the same comment id
                # showing up via a refetch + discovery surface only gets
                # tagged once.
                for tc in new_tagged:
                    already_tagged.add(tc.comment_id)
            tagged_count += len(new_tagged)
            processed_count += 1
            if not args.quiet:
                logger.info(
                    "[%d/%d] %s %s (+%d new tagged)",
                    processed_count,
                    n_total,
                    thread_id,
                    status,
                    len(new_tagged),
                )
        except Exception as exc:
            logger.exception(
                "Thread %s failed: %s", cand.permalink, exc
            )
            failed[cand.permalink] = str(exc)
            # Continue with the next candidate — one bad thread shouldn't
            # take down the whole weekly run.

    # 6. Build aggregate and write to dashboard/data/latest.json.
    logger.info("Main loop complete — building dashboard aggregate...")
    try:
        summary = build_and_write_aggregate(time_horizon="2010-01-01")
    except Exception as exc:
        logger.exception("Aggregate build failed: %s", exc)
        return 1

    # 7. Write last_run.json so the NEXT run starts where this one ended.
    run_cost = read_running_cost_usd()
    save_last_run(
        last_run_at=run_started_at,
        threads_processed=processed_count,
        comments_tagged=tagged_count,
        cost_usd=run_cost,
    )

    # 8. Final report.
    print()
    print("=" * 72)
    print("Weekly delta complete")
    print("=" * 72)
    print(f"  Threads processed:           {processed_count:>6}")
    print(f"  New comments tagged:         {tagged_count:>6}")
    print(f"  Failures (recorded):         {len(failed):>6}")
    print(f"  Cumulative cost this run:    ${run_cost:>7.2f}")
    print(f"  Aggregate threads (total):   {summary['total_threads']:>6}")
    print(f"  Aggregate comments (total):  {summary['total_comments']:>6}")
    print(f"  Aggregate path:              {summary['aggregate_path']}")
    print(f"  last_run.json updated:       {LAST_RUN_PATH}")
    print("=" * 72)

    return 0


def main() -> None:
    """Process entrypoint — parses argv and exits with the run() exit code."""

    args = parse_args()
    sys.exit(run(args))


if __name__ == "__main__":  # pragma: no cover - module-as-script guard
    main()
