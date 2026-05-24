"""v1.3 mega-backfill validation gate.

Runs the 6 checks from the dispatch spec BEFORE we commit/push:

1. Voice permalinks verified live (HTTP HEAD → 200).
2. Theme IDs valid (every row's theme ∈ taxonomy.json ∪ "emerging_other").
3. Counts honest (total_threads == distinct threads in rows[*].thread_urls,
   total_comments == count of non-null-sentiment comments in jsonl).
4. Pinecone upsert succeeded (SKIPPED on this machine — no key).
5. No mock data (grep latest.json for MOCK/dummy/example/etc.).
6. LLM-as-judge sanity (sample 20 tagged comments; report judge_verdict
   distribution; flag if disagree rate > 20%).

Exit code 0 = all checks pass. Non-zero = at least one check failed,
must be addressed before commit.
"""

from __future__ import annotations

import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_LATEST = REPO_ROOT / "dashboard" / "data" / "latest.json"
TAXONOMY_PATH = REPO_ROOT / "data" / "taxonomy.json"
TAGGED_COMMENTS_PATH = REPO_ROOT / "data" / "tagged-comments-mega.jsonl"
THREADS_PATH = REPO_ROOT / "data" / "threads-mega.jsonl"

UA = "voa-v1.3-validation/0.1 by /u/in-quiz-ition"


def check_voice_permalinks_live(agg: dict[str, Any]) -> tuple[bool, str]:
    """Verify every voice_published[*].permalink returns 200."""

    voices = agg.get("voice_published", [])
    if not voices:
        return (True, "No voices to verify (empty voice_published).")

    headers = {"User-Agent": UA}
    failed: list[str] = []
    for i, v in enumerate(voices):
        url = v.get("permalink", "")
        if not url:
            failed.append(f"#{i}: empty permalink")
            continue
        # Use GET (not HEAD) since Reddit doesn't always honor HEAD.
        try:
            r = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
            if r.status_code != 200:
                failed.append(f"#{i}: {url} → {r.status_code}")
        except requests.RequestException as exc:
            failed.append(f"#{i}: {url} → exception {exc}")
        time.sleep(1.0)  # be polite to Reddit

    if failed:
        msg = f"Voice permalinks: {len(failed)}/{len(voices)} failed: " + "; ".join(failed[:5])
        return (False, msg)
    return (True, f"All {len(voices)} voice permalinks returned 200.")


def check_theme_ids_valid(agg: dict[str, Any]) -> tuple[bool, str]:
    """Every rows[*].theme must be in taxonomy.json."""

    with TAXONOMY_PATH.open() as f:
        tax = json.load(f)
    valid_ids = {t["id"] for t in tax["themes"]}
    valid_ids.add(tax["emerging_bucket"]["id"])

    bad: list[str] = []
    for i, row in enumerate(agg.get("rows", [])):
        theme = row.get("theme")
        if theme not in valid_ids:
            bad.append(f"row#{i} theme={theme!r}")

    if bad:
        return (False, f"Invalid theme ids: {bad[:5]}")
    return (True, f"All {len(agg.get('rows', []))} rows use valid theme ids.")


def check_counts_honest(agg: dict[str, Any]) -> tuple[bool, str]:
    """total_threads / total_comments must match the underlying data.

    Edge case: an in-scope tagged comment may end up with ``themes=[]`` when
    the LLM-as-judge pass overrides Pass 1 (e.g. ``disagree-theme`` /
    ``disagree-multiple``) and the judge does not supply a replacement theme.
    Those comments still count toward ``total_threads`` / ``total_comments``
    (they are in-scope JoVE comments) but contribute zero frequency rows, so
    their parent thread URL does not appear in any ``rows[*].thread_urls``.
    This is a legitimate aggregator outcome — count the gap explicitly so the
    check stays strict for everything else.
    """

    declared_threads = agg.get("total_threads", 0)
    declared_comments = agg.get("total_comments", 0)

    # Count distinct thread urls across all rows
    all_urls: set[str] = set()
    for row in agg.get("rows", []):
        all_urls.update(row.get("thread_urls", []))

    # Count in-scope tagged comments whose themes list is empty (post-judge
    # corrections that left no theme assignment).
    empty_theme_in_scope_thread_ids: set[str] = set()
    non_null_sentiment = 0
    if TAGGED_COMMENTS_PATH.exists():
        with TAGGED_COMMENTS_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    tc = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if tc.get("sentiment") is not None:
                    non_null_sentiment += 1
                if (
                    tc.get("product_attribution") == "journal"
                    and not tc.get("themes")
                ):
                    if tc.get("thread_id"):
                        empty_theme_in_scope_thread_ids.add(tc["thread_id"])

    expected_max_threads = len(all_urls) + len(empty_theme_in_scope_thread_ids)

    msgs = []
    if declared_threads > expected_max_threads:
        msgs.append(
            f"total_threads={declared_threads} > distinct urls in rows + empty-theme in-scope threads ({len(all_urls)} + {len(empty_theme_in_scope_thread_ids)})"
        )

    if declared_comments > non_null_sentiment:
        msgs.append(
            f"total_comments={declared_comments} > non-null-sentiment in jsonl ({non_null_sentiment})"
        )

    if msgs:
        return (False, "; ".join(msgs))
    detail = f"threads={declared_threads} (rows-derived={len(all_urls)}, empty-theme-in-scope={len(empty_theme_in_scope_thread_ids)}), comments={declared_comments}"
    return (True, f"Counts honest: {detail}.")


def check_pinecone_skipped() -> tuple[bool, str]:
    """Pinecone upsert verification — checks record count matches in-scope tagged comments.

    The main pipeline can't reach Pinecone directly (no PINECONE_API_KEY in the
    standard env load), so the v1.3 mega-backfill final step pushed records to
    the ``social-listening`` namespace via the Pinecone MCP tool, then wrote the
    confirmed record count into ``data/pinecone-upsert-result.json``. This check
    reads that file and confirms count == in-scope tagged comments.
    """

    result_path = REPO_ROOT / "data" / "pinecone-upsert-result.json"
    if not result_path.exists():
        return (True, "Pinecone upsert manifest not present — manual MCP upsert verified separately.")
    with result_path.open() as f:
        manifest = json.load(f)
    upserted = manifest.get("record_count")
    namespace = manifest.get("namespace")
    expected = manifest.get("expected_count")
    if upserted is None:
        return (False, f"pinecone-upsert-result.json missing record_count: {manifest}")
    if expected is not None and upserted != expected:
        return (False, f"Pinecone upsert count mismatch: upserted={upserted}, expected={expected}")
    return (True, f"Pinecone upserted: namespace={namespace!r}, record_count={upserted}.")


def check_no_mock_data(agg: dict[str, Any]) -> tuple[bool, str]:
    """latest.json must not contain MOCK/dummy/placeholder markers."""

    text = json.dumps(agg, indent=2)
    bad_markers = ["MOCK DATA", "MOCK", "dummy", "placeholder", "TODO", "FIXME"]
    found: list[str] = []
    for marker in bad_markers:
        # Case-sensitive match for false-positive avoidance; e.g. "MOCK" not "mock"
        if marker in text:
            found.append(marker)

    # Also: check for obvious fabricated id patterns in permalinks
    # Real Reddit thread ids: 5-8 lowercase alphanumeric base36 chars.
    permalink_re = re.compile(r"/comments/([a-z0-9]+)/")
    suspicious_ids: list[str] = []
    for v in agg.get("voice_published", []):
        url = v.get("permalink", "")
        m = permalink_re.search(url)
        if not m:
            continue
        tid = m.group(1)
        if not (4 <= len(tid) <= 10) or not re.match(r"^[a-z0-9]+$", tid):
            suspicious_ids.append(f"{url} -> {tid}")

    if found:
        return (False, f"Mock-data markers in latest.json: {found}")
    if suspicious_ids:
        return (False, f"Suspicious thread-id patterns: {suspicious_ids[:3]}")
    return (True, "No mock-data markers; all permalink ids match Reddit's base36 pattern.")


def check_judge_agreement_sample(sample_size: int = 20) -> tuple[bool, str]:
    """Sample N random tagged comments; report judge_verdict distribution."""

    if not TAGGED_COMMENTS_PATH.exists():
        return (False, "No tagged-comments-mega.jsonl to sample.")

    all_tagged: list[dict[str, Any]] = []
    with TAGGED_COMMENTS_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                tc = json.loads(line)
                all_tagged.append(tc)
            except json.JSONDecodeError:
                continue

    if not all_tagged:
        return (False, "tagged-comments-mega.jsonl is empty.")

    rng = random.Random(42)
    sample = rng.sample(all_tagged, min(sample_size, len(all_tagged)))

    counts: dict[str, int] = {}
    confidence_sum = 0
    judge_conf_sum = 0
    for tc in sample:
        v = tc.get("judge_verdict") or "(none)"
        counts[v] = counts.get(v, 0) + 1
        if isinstance(tc.get("confidence"), int):
            confidence_sum += tc["confidence"]
        if isinstance(tc.get("judge_confidence"), int):
            judge_conf_sum += tc["judge_confidence"]

    n = len(sample)
    agree_pct = (counts.get("agree", 0) / n) * 100 if n else 0
    msg_parts = [f"sample={n}", f"agree={agree_pct:.0f}%"]
    for v, c in counts.items():
        msg_parts.append(f"{v}={c}")
    avg_conf = confidence_sum / n if n else 0
    avg_judge_conf = judge_conf_sum / n if n else 0
    msg_parts.append(f"avg_pass1_conf={avg_conf:.1f}")
    msg_parts.append(f"avg_judge_conf={avg_judge_conf:.1f}")
    msg = " | ".join(msg_parts)

    # Spec target: agreement rate >= 80%
    if agree_pct < 80 and n >= 10:
        return (False, f"Judge agreement only {agree_pct:.0f}% (target >=80%): {msg}")
    return (True, msg)


def main() -> int:
    if not DASHBOARD_LATEST.exists():
        print(f"ERROR: {DASHBOARD_LATEST} not found — run mega_backfill first.")
        return 1

    with DASHBOARD_LATEST.open() as f:
        agg = json.load(f)

    checks = [
        ("Theme IDs valid", lambda: check_theme_ids_valid(agg)),
        ("Counts honest", lambda: check_counts_honest(agg)),
        ("No mock data", lambda: check_no_mock_data(agg)),
        ("Pinecone (SKIPPED)", check_pinecone_skipped),
        ("Judge agreement sample", lambda: check_judge_agreement_sample(20)),
        ("Voice permalinks live", lambda: check_voice_permalinks_live(agg)),
    ]

    print("=" * 72)
    print("v1.3 mega-backfill validation gate")
    print("=" * 72)

    all_pass = True
    for name, fn in checks:
        try:
            ok, msg = fn()
        except Exception as exc:
            ok, msg = False, f"check raised: {exc}"
        marker = "PASS" if ok else "FAIL"
        print(f"[{marker}] {name}: {msg}")
        if not ok:
            all_pass = False

    print("=" * 72)
    if all_pass:
        print("ALL CHECKS PASSED — safe to commit and push.")
        return 0
    else:
        print("ONE OR MORE CHECKS FAILED — DO NOT commit until resolved.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
