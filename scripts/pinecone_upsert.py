"""Pinecone HD archive upsert.

Reads ``data/threads-mega.jsonl`` + ``data/tagged-comments-mega.jsonl``
and upserts every Thread + tagged Comment to the ``jove-memory`` index's
``social-listening`` namespace using :func:`pipeline.store.upsert_thread`.

PRECONDITION: ``PINECONE_API_KEY`` must be set in the environment.

On the v1.3 backfill machine the key is NOT available, so this script
exits 0 with a documented skip message. To run for real:

    export PINECONE_API_KEY=...
    uv run python scripts/pinecone_upsert.py

The upsert is idempotent (Pinecone upserts by ``_id``), so running this
multiple times overwrites in place.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
THREADS_PATH = REPO_ROOT / "data" / "threads-mega.jsonl"
TAGGED_COMMENTS_PATH = REPO_ROOT / "data" / "tagged-comments-mega.jsonl"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("pinecone_upsert")


def main() -> int:
    if not os.environ.get("PINECONE_API_KEY"):
        print(
            "SKIP: PINECONE_API_KEY not set in environment.\n"
            "The HD archive for the v1.3 mega-backfill lives in:\n"
            f"  - {THREADS_PATH}\n"
            f"  - {TAGGED_COMMENTS_PATH}\n"
            "To upsert to Pinecone, set PINECONE_API_KEY and re-run."
        )
        return 0

    if not THREADS_PATH.exists() or not TAGGED_COMMENTS_PATH.exists():
        print(f"ERROR: missing inputs ({THREADS_PATH}, {TAGGED_COMMENTS_PATH}).")
        return 1

    from pipeline import store
    from pipeline.models import TaggedComment, Thread

    threads: dict[str, Thread] = {}
    with THREADS_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            t = Thread.model_validate_json(line)
            threads[t.id] = t

    tagged_by_thread: dict[str, list[TaggedComment]] = {}
    with TAGGED_COMMENTS_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            tc = TaggedComment.model_validate_json(line)
            tagged_by_thread.setdefault(tc.thread_id or "_orphan", []).append(tc)

    upsert_count = 0
    for tid, thread in threads.items():
        tcs = tagged_by_thread.get(tid, [])
        try:
            store.upsert_thread(thread, tcs)
            upsert_count += 1
            if upsert_count % 10 == 0:
                logger.info("Upserted %d/%d threads", upsert_count, len(threads))
        except Exception as exc:
            logger.warning("Upsert failed for thread %s: %s", tid, exc)

    logger.info("Total threads upserted: %d / %d", upsert_count, len(threads))
    return 0


if __name__ == "__main__":
    sys.exit(main())
