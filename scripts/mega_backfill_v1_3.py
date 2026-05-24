"""v1.3 mega-backfill orchestrator.

Differences vs. ``scripts/mega_backfill.py``:

* **Tagger**: Azure OpenAI GPT-4.1 Pass 1 + GPT-4.1-as-judge Pass 2
  (instead of Anthropic Claude Haiku + Sonnet).
* **Discovery**: direct Reddit .json endpoints (no PRAW, no OAuth) for
  Rings 1 + 2; Ring 3 (Firecrawl-on-Google) is DROPPED on this machine
  because ``FIRECRAWL_API_KEY`` is unavailable. Ring 2 query set is
  expanded to compensate.
* **Fetch**: direct .json endpoints (no PRAW, no Firecrawl).
* **Store**: Pinecone HD upsert is SKIPPED on this machine because
  ``PINECONE_API_KEY`` is unavailable. The durable JSONL mirrors
  (``data/threads.jsonl`` and ``data/tagged-comments-mega.jsonl``) ARE
  the archive for v1.3. The aggregator reads these directly.

All other phases (disambiguate, aggregate, voice candidate vault write)
are unchanged.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline import aggregate, disambiguate, fetch_unauth, store
from pipeline.config import REPO_ROOT
from pipeline.discover_unauth import discover_all_unauth
from pipeline.models import TaggedComment, Thread, ThreadCandidate
from pipeline.tag_azure import install_as_default_tagger, tag_comment_with_judge

logger = logging.getLogger("mega_backfill_v1_3")


CHECKPOINT_VERSION: int = 3  # bumped from v1.2's CHECKPOINT_VERSION=1
DEFAULT_CHECKPOINT_PATH: Path = REPO_ROOT / "data" / "checkpoint-v1-3.json"

TAGGED_COMMENTS_PATH: Path = REPO_ROOT / "data" / "tagged-comments-mega.jsonl"
THREADS_PATH: Path = REPO_ROOT / "data" / "threads-mega.jsonl"
RAW_COMMENTS_PATH: Path = REPO_ROOT / "data" / "raw-comments-mega.jsonl"

DISCOVERY_CACHE_PATH: Path = REPO_ROOT / "data" / "discovery-candidates.jsonl"

COST_LOG_PATH: Path = REPO_ROOT / "data" / "tag-cost-log.jsonl"
AGGREGATE_OUTPUT_PATH: Path = REPO_ROOT / "dashboard" / "data" / "latest.json"

PROGRESS_REPORT_EVERY: int = 5  # Per-5-thread progress lines for visibility.


# =========================================================================
# Checkpoint I/O
# =========================================================================


def _new_checkpoint() -> dict[str, Any]:
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
    if not path.exists():
        return _new_checkpoint()
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("version") != CHECKPOINT_VERSION:
        raise RuntimeError(
            f"Checkpoint {path} has version={data.get('version')}, expected "
            f"{CHECKPOINT_VERSION}. Delete it to start fresh."
        )
    data.setdefault("discovery_count", 0)
    data.setdefault("processed_thread_ids", [])
    data.setdefault("failed_thread_ids", {})
    data.setdefault("total_tagged_comments", 0)
    data.setdefault("estimated_cost_usd", 0.0)
    return data


def save_checkpoint(checkpoint: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(checkpoint, f, indent=2, sort_keys=True)
    tmp.replace(path)


def read_running_cost_usd() -> float:
    if not COST_LOG_PATH.exists():
        return 0.0
    total = 0.0
    with COST_LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                total += float(json.loads(line).get("total_cost_usd") or 0.0)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
    return total


def emit_progress(
    *,
    started_at: float,
    processed_count: int,
    total_count: int,
    tagged_comment_count: int,
    quiet: bool,
) -> None:
    if quiet:
        return
    elapsed = time.monotonic() - started_at
    elapsed_hms = time.strftime("%H:%M:%S", time.gmtime(elapsed))
    fraction = (processed_count / total_count) if total_count > 0 else 0.0
    pct = fraction * 100.0
    cost_usd = read_running_cost_usd()
    if fraction > 0:
        total_est = elapsed / fraction
        remaining = max(0.0, total_est - elapsed)
        eta_hms = time.strftime("%Hh%Mm", time.gmtime(remaining))
    else:
        eta_hms = "--:--"
    print(
        f"[{elapsed_hms}] {processed_count}/{total_count} threads "
        f"({pct:.1f}%), {tagged_comment_count} tagged comments, "
        f"${cost_usd:.2f} spent, ETA {eta_hms}",
        file=sys.stderr,
        flush=True,
    )


# =========================================================================
# Append-only JSONL mirrors
# =========================================================================


def append_thread_to_jsonl(thread: Thread) -> None:
    THREADS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with THREADS_PATH.open("a", encoding="utf-8") as f:
        f.write(thread.model_dump_json() + "\n")


def append_tagged_comments_to_jsonl(tagged_comments: list[TaggedComment]) -> None:
    if not tagged_comments:
        return
    TAGGED_COMMENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TAGGED_COMMENTS_PATH.open("a", encoding="utf-8") as f:
        for tc in tagged_comments:
            f.write(tc.model_dump_json() + "\n")


def append_raw_comments_to_jsonl(thread: Thread) -> None:
    """Write raw Reddit comments (pre-tagging) so they're recoverable.

    One JSONL line per comment: ``{thread_id, subreddit, comment: {...}}``.
    """

    RAW_COMMENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RAW_COMMENTS_PATH.open("a", encoding="utf-8") as f:
        for c in thread.comments:
            rec = {
                "thread_id": thread.id,
                "subreddit": thread.subreddit,
                "comment": json.loads(c.model_dump_json()),
            }
            f.write(json.dumps(rec) + "\n")


def load_tagged_comments_from_jsonl() -> list[TaggedComment]:
    if not TAGGED_COMMENTS_PATH.exists():
        return []
    out: list[TaggedComment] = []
    with TAGGED_COMMENTS_PATH.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(TaggedComment.model_validate_json(line))
            except Exception as exc:
                logger.warning(
                    "Skipping malformed tagged-comment at %s:%d (%s)",
                    TAGGED_COMMENTS_PATH,
                    line_no,
                    exc,
                )
    return out


def load_threads_from_jsonl() -> dict[str, Thread]:
    if not THREADS_PATH.exists():
        return {}
    out: dict[str, Thread] = {}
    with THREADS_PATH.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                t = Thread.model_validate_json(line)
                out[t.id] = t
            except Exception as exc:
                logger.warning(
                    "Skipping malformed thread at %s:%d (%s)",
                    THREADS_PATH,
                    line_no,
                    exc,
                )
    return out


# =========================================================================
# Discovery cache (to make re-runs cheap)
# =========================================================================


def cache_discovery(candidates: list[ThreadCandidate]) -> None:
    DISCOVERY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DISCOVERY_CACHE_PATH.open("w", encoding="utf-8") as f:
        for c in candidates:
            f.write(c.model_dump_json() + "\n")


def load_cached_discovery() -> list[ThreadCandidate]:
    if not DISCOVERY_CACHE_PATH.exists():
        return []
    out: list[ThreadCandidate] = []
    with DISCOVERY_CACHE_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(ThreadCandidate.model_validate_json(line))
            except Exception:
                continue
    return out


# =========================================================================
# SIGINT-safe interrupt flag
# =========================================================================


class GracefulInterrupt:
    def __init__(self) -> None:
        self.triggered = False
        self._original_handler: Any = None

    def __enter__(self) -> GracefulInterrupt:
        self._original_handler = signal.signal(signal.SIGINT, self._handle)
        return self

    def __exit__(self, *_exc_info: Any) -> None:
        signal.signal(signal.SIGINT, self._original_handler)

    def _handle(self, _signum: int, _frame: Any) -> None:
        if self.triggered:
            signal.signal(signal.SIGINT, signal.default_int_handler)
            raise KeyboardInterrupt
        self.triggered = True
        print(
            "\n[mega_backfill_v1_3] SIGINT received — finishing current "
            "thread and saving checkpoint. Press Ctrl+C again to hard-quit.",
            file=sys.stderr,
            flush=True,
        )


# =========================================================================
# Single-thread processor
# =========================================================================


def process_one_thread(
    candidate: ThreadCandidate,
) -> tuple[str, str, list[TaggedComment], Thread | None]:
    """Fetch → disambiguate → tag → JSONL-append one candidate.

    Returns ``(thread_id, status, tagged_comments, thread)``. Status is
    one of ``"processed"`` or ``"skipped:<reason>"``. Raises on fetch/tag
    failure; caller wraps to record the failure and continue.
    """

    thread = fetch_unauth.fetch_thread_unauth(candidate)
    in_scope, reason = disambiguate.is_in_scope(thread)
    if not in_scope:
        return (thread.id, f"skipped:{reason}", [], thread)

    tagged_comments: list[TaggedComment] = []
    for comment in thread.comments:
        tagged = tag_comment_with_judge(comment, thread)
        tagged_comments.append(tagged)

    # Vault voice-candidate write (only if PINECONE/VAULT are available)
    voice_yeses = [tc for tc in tagged_comments if tc.voice_candidate]
    if voice_yeses:
        try:
            store.write_voice_candidates(voice_yeses, thread)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Voice candidate vault write failed for %s: %s", thread.id, exc)

    return (thread.id, "processed", tagged_comments, thread)


# =========================================================================
# Main loop
# =========================================================================


def main_loop(
    candidates: list[ThreadCandidate],
    checkpoint: dict[str, Any],
    *,
    checkpoint_path: Path,
    quiet: bool = False,
    interrupt: GracefulInterrupt | None = None,
) -> None:
    processed_ids: set[str] = set(checkpoint["processed_thread_ids"])
    processed_permalinks: set[str] = {p for p in processed_ids if p.startswith("http")}
    started_at = time.monotonic()
    total = len(candidates)
    iteration = 0
    tagged_count_so_far = checkpoint.get("total_tagged_comments", 0)

    for candidate in candidates:
        if interrupt is not None and interrupt.triggered:
            logger.info("SIGINT received — exiting main loop")
            return

        if candidate.permalink in processed_permalinks:
            continue

        iteration += 1
        try:
            thread_id, status, tagged_comments, thread = process_one_thread(candidate)

            # Post-fetch dedup
            if thread_id in processed_ids:
                processed_ids.add(candidate.permalink)
                processed_permalinks.add(candidate.permalink)
                checkpoint["processed_thread_ids"] = sorted(processed_ids)
                save_checkpoint(checkpoint, checkpoint_path)
                continue

            # Append durable mirrors BEFORE marking processed.
            if thread is not None:
                append_thread_to_jsonl(thread)
                append_raw_comments_to_jsonl(thread)
            if tagged_comments:
                append_tagged_comments_to_jsonl(tagged_comments)

            tagged_count_so_far += len(tagged_comments)

            processed_ids.add(thread_id)
            processed_ids.add(candidate.permalink)
            processed_permalinks.add(candidate.permalink)
            checkpoint["processed_thread_ids"] = sorted(processed_ids)
            checkpoint["total_tagged_comments"] = tagged_count_so_far
            checkpoint["estimated_cost_usd"] = read_running_cost_usd()
            save_checkpoint(checkpoint, checkpoint_path)

            logger.info(
                "Thread %s %s (tagged=%d, in r/%s)",
                thread_id,
                status,
                len(tagged_comments),
                candidate.subreddit,
            )
        except Exception as exc:
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

    emit_progress(
        started_at=started_at,
        processed_count=iteration,
        total_count=total,
        tagged_comment_count=tagged_count_so_far,
        quiet=quiet,
    )


# =========================================================================
# Aggregate build
# =========================================================================


def build_and_write_aggregate(
    *,
    time_horizon: str,
    output_path: Path | None = None,
) -> dict[str, Any]:
    target = output_path if output_path is not None else AGGREGATE_OUTPUT_PATH
    tagged_comments = load_tagged_comments_from_jsonl()
    threads = load_threads_from_jsonl()

    # Build voice_published from voice_candidate=True comments directly.
    # (D's weekly curation isn't possible inside this single dispatch — we
    # surface the LLM-judged candidates as voice_published with curator_note
    # set to the model's reasoning.)
    voice_panel = _build_voice_panel_from_tagged(tagged_comments, threads)

    agg = aggregate.build_aggregate(
        tagged_comments=tagged_comments,
        threads=threads,
        voice_published=voice_panel,
        time_horizon=time_horizon,
    )
    store.write_aggregate_json(agg.model_dump(mode="json"), target)

    # Summary for the final stdout report.
    by_sentiment: dict[str, list[tuple[str, int, int]]] = {}
    for row in agg.rows:
        by_sentiment.setdefault(row.sentiment, []).append(
            (row.theme, row.thread_count, row.comment_count)
        )
    top3: dict[str, list[dict[str, Any]]] = {}
    for sentiment, rows in by_sentiment.items():
        rows.sort(key=lambda r: (-r[1], -r[2]))
        top3[sentiment] = [
            {"theme": t, "thread_count": tc, "comment_count": cc}
            for t, tc, cc in rows[:3]
        ]

    return {
        "total_threads": agg.total_threads,
        "total_comments": agg.total_comments,
        "voice_published_count": len(agg.voice_published),
        "rows_count": len(agg.rows),
        "top3_by_sentiment": top3,
        "aggregate_path": str(target),
    }


def _build_voice_panel_from_tagged(
    tagged: list[TaggedComment], threads: dict[str, Thread]
) -> list[Any]:
    """Curate voice_published from LLM voice_candidate=True with judge agreement.

    Pick comments where:
    * voice_candidate == True (Pass 1 said YES)
    * judge_verdict == "agree" OR (judge corrected to voice_candidate=True)
    * judge_confidence >= 4

    Sort by (judge_confidence DESC, comment.score DESC), distributed across
    sentiments to keep the panel balanced.
    """

    from pipeline.models import VoicePanelRow, ProductAttribution

    # First filter
    candidates = [
        tc for tc in tagged
        if tc.voice_candidate
        and (tc.judge_confidence or 0) >= 4
        and tc.judge_verdict in ("agree", "disagree-voice", "disagree-multiple", "disagree-sentiment", "disagree-theme")
    ]
    # For non-"agree" verdicts, only keep if the judge's correction kept
    # voice_candidate=True (which it implicitly did — we already applied
    # the override before persistence).

    # Look up Comment + Thread for each
    comment_lookup: dict[str, tuple[Any, Thread]] = {}
    for t in threads.values():
        for c in t.comments:
            comment_lookup[c.id] = (c, t)

    rows: list[VoicePanelRow] = []
    sentiment_quota = {"positive": 0, "neutral": 0, "negative": 0, "mixed": 0}
    target_per_sentiment = 10  # Aim for ~40 total (20-40 per spec)

    # Sort candidates by quality: judge_confidence first, then Reddit score
    candidates.sort(
        key=lambda tc: (
            -(tc.judge_confidence or 0),
            -(tc.confidence or 0),
            -(comment_lookup.get(tc.comment_id, (None, None))[0].score if comment_lookup.get(tc.comment_id) and comment_lookup[tc.comment_id][0] else 0),
        )
    )

    for tc in candidates:
        if tc.comment_id not in comment_lookup:
            continue
        comment, thread = comment_lookup[tc.comment_id]
        sentiment = tc.sentiment or "neutral"
        if sentiment_quota.get(sentiment, 0) >= target_per_sentiment:
            continue

        # First theme (if any)
        theme: str | None = None
        if tc.themes:
            theme = tc.themes[0].theme

        product: ProductAttribution = tc.product_attribution
        if product == "out-of-scope":
            continue  # never curate out-of-scope as a voice

        # Author may be None
        author = comment.author or "[deleted]"

        rows.append(VoicePanelRow(
            sentiment=sentiment,
            theme=theme,
            product_attribution=product,
            author=author,
            permalink=comment.permalink,
            body=comment.body,
            score=comment.score,
            created_utc=comment.created_utc,
            curated_at=datetime.now(tz=timezone.utc),
            curator_note=(tc.voice_candidate_reason or None),
        ))
        sentiment_quota[sentiment] = sentiment_quota.get(sentiment, 0) + 1

        # Hard cap at 40 total
        if len(rows) >= 40:
            break

    logger.info(
        "Voice panel built: %d total (positive=%d, neutral=%d, negative=%d, mixed=%d)",
        len(rows),
        sentiment_quota.get("positive", 0),
        sentiment_quota.get("neutral", 0),
        sentiment_quota.get("negative", 0),
        sentiment_quota.get("mixed", 0),
    )
    return rows


# =========================================================================
# CLI
# =========================================================================


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mega_backfill_v1_3",
        description=(
            "Voice of Academia v1.3 mega-backfill — Azure OpenAI GPT-4.1 + "
            "GPT-4.1-as-judge. Uses unauth Reddit .json endpoints "
            "(no PRAW, no Firecrawl, no Pinecone)."
        ),
    )
    parser.add_argument("--time-horizon", default="2010-01-01")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-discovery",
        action="store_true",
        help="Skip discovery (use cached candidates from discovery-candidates.jsonl)",
    )
    parser.add_argument(
        "--discovery-only",
        action="store_true",
        help="Run discovery, cache the candidates, then exit",
    )
    parser.add_argument(
        "--checkpoint-path", type=Path, default=DEFAULT_CHECKPOINT_PATH,
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--max-threads",
        type=int,
        default=None,
        help="Cap on number of new threads to process this run (debugging)",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO if not args.quiet else logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    # Install the Azure tagger as the default tag.tag_comment (so any
    # downstream callers also use it).
    install_as_default_tagger()

    # 1. Discovery (or load from cache)
    if args.skip_discovery:
        candidates = load_cached_discovery()
        logger.info("Loaded %d cached discovery candidates", len(candidates))
    else:
        logger.info("Discovery: starting 3-ring scan (Ring 3 disabled — no Firecrawl)")
        horizon_dt = datetime.strptime(args.time_horizon, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
        candidates = list(discover_all_unauth(time_horizon=horizon_dt))
        cache_discovery(candidates)
        logger.info("Discovery: cached %d candidates to %s", len(candidates), DISCOVERY_CACHE_PATH)

    n = len(candidates)
    if n == 0:
        print("No candidates discovered.", file=sys.stderr)
        return 0

    if args.discovery_only:
        print(f"Discovery only: {n} candidates cached.")
        return 0

    if args.dry_run:
        print(f"Would process {n} candidates.")
        print(f"Expected cost @ ~$0.025/comment × ~15 comments/thread = ~${n * 15 * 0.025:.2f}")
        return 0

    if args.max_threads:
        candidates = candidates[: args.max_threads]
        logger.info("max_threads cap applied: %d → %d", n, len(candidates))
        n = len(candidates)

    # 2. Checkpoint load
    checkpoint = load_checkpoint(args.checkpoint_path)
    checkpoint["discovery_count"] = n
    save_checkpoint(checkpoint, args.checkpoint_path)

    already_done = len(
        [p for p in checkpoint["processed_thread_ids"] if p.startswith("t3_")]
    )
    logger.info(
        "Checkpoint: %d thread(s) already processed, %d failure(s)",
        already_done,
        len(checkpoint["failed_thread_ids"]),
    )

    # 3. Main loop
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
            logger.warning("Second SIGINT — exiting hard. Re-run to resume.")
            return 130

        if interrupt.triggered:
            logger.warning(
                "Graceful shutdown complete. Re-run to resume (%d/%d done).",
                len(checkpoint["processed_thread_ids"]),
                n,
            )
            return 130

    # 4. Aggregate build
    logger.info("Main loop done — building dashboard aggregate")
    try:
        summary = build_and_write_aggregate(time_horizon=args.time_horizon)
    except Exception as exc:
        logger.exception("Aggregate build failed: %s", exc)
        return 1

    # 5. Final stdout report
    print()
    print("=" * 72)
    print("v1.3 mega-backfill complete")
    print("=" * 72)
    print(f"  Total threads:           {summary['total_threads']:>10,}")
    print(f"  Total tagged comments:   {summary['total_comments']:>10,}")
    print(f"  Rows:                    {summary['rows_count']:>10,}")
    print(f"  Voice published:         {summary['voice_published_count']:>10,}")
    print(f"  Cumulative cost:         ${read_running_cost_usd():>9,.2f}")
    print(f"  Aggregate JSON:          {summary['aggregate_path']}")
    print()
    print("  Top 3 themes by frequency (per sentiment):")
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

    return 0


def main() -> None:
    args = parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
