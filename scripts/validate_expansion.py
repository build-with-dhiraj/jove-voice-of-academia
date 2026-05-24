"""Validation pass for the expansion sweep output.

Checks:
1. No overlap (by Reddit thread id) between the existing 132-candidate set
   and the new expansion set.
2. Schema integrity — every line is valid ThreadCandidate JSON.
3. Permalink sample — 10 random new permalinks return HTTP 200.
4. Disambiguation sample — 10 random new candidates eyeballed for false
   positives (printed for human review).
5. Date span — counts of threads per year by sampling 50 random new
   candidates and fetching their created_utc.

Prints a summary report; exits 0 on validation pass, 1 on any failure.
"""

from __future__ import annotations

import json
import random
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.discover import extract_thread_id  # noqa: E402

ROOT = Path(__file__).parent.parent
EXISTING = ROOT / "data" / "discovery-candidates.jsonl"
EXPANSION = ROOT / "data" / "discovery-candidates-expansion.jsonl"

_UA = (
    "voa-v1.3-mega-validation/0.1 by /u/in-quiz-ition "
    "(https://github.com/jove-publishing)"
)


def _load_ids_and_records(path: Path) -> tuple[set[str], list[dict]]:
    ids: set[str] = set()
    recs: list[dict] = []
    if not path.exists():
        return ids, recs
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            recs.append(d)
            tid = extract_thread_id(d.get("permalink", ""))
            if tid:
                ids.add(tid)
    return ids, recs


def _http_get_json(url: str) -> dict | None:
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=20)
        time.sleep(1.6)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    failures = 0

    print("=" * 70)
    print("EXPANSION VALIDATION")
    print("=" * 70)

    # 1. Load both sets
    if not EXPANSION.exists():
        print(f"FAIL: expansion file does not exist at {EXPANSION}")
        return 1

    existing_ids, _existing_recs = _load_ids_and_records(EXISTING)
    expansion_ids, expansion_recs = _load_ids_and_records(EXPANSION)

    print(f"\nExisting candidates: {len(existing_ids)} unique thread IDs")
    print(f"Expansion candidates: {len(expansion_ids)} unique thread IDs")
    print(f"Total expansion lines: {len(expansion_recs)}")

    # 2. Overlap check
    overlap = existing_ids & expansion_ids
    print(f"\n[1] Overlap check: {len(overlap)} threads in BOTH sets")
    if overlap:
        print(f"    FAIL: expected 0 overlap, got {len(overlap)}")
        print(f"    First 10 overlapping IDs: {list(overlap)[:10]}")
        failures += 1
    else:
        print("    PASS")

    # 3. Schema check — every line must parse and have the right fields
    print(f"\n[2] Schema integrity check on {len(expansion_recs)} records")
    schema_errors = 0
    REQUIRED = {"permalink", "subreddit", "discovered_via", "discovered_at"}
    VALID_RINGS = {"ring1_subreddit_enum", "ring2_reddit_search", "ring3_firecrawl_google"}
    for r in expansion_recs:
        missing = REQUIRED - set(r.keys())
        if missing:
            schema_errors += 1
            continue
        if r["discovered_via"] not in VALID_RINGS:
            schema_errors += 1
    if schema_errors:
        print(f"    FAIL: {schema_errors} records with schema errors")
        failures += 1
    else:
        print("    PASS")

    # 4. Permalink sample — 10 random
    print("\n[3] Permalink validity sample (10 random)")
    sample = random.sample(expansion_recs, min(10, len(expansion_recs)))
    perma_ok = 0
    perma_404 = 0
    for r in sample:
        url = r["permalink"]
        if not url.endswith(".json"):
            url_json = url.rstrip("/") + ".json"
        else:
            url_json = url
        data = _http_get_json(url_json)
        if data is None:
            perma_404 += 1
            print(f"    BAD: {url}")
        else:
            perma_ok += 1
    print(f"    {perma_ok}/{len(sample)} return valid JSON (HTTP 200)")
    if perma_ok < len(sample) * 0.7:  # >30% failure is suspicious
        print("    FAIL: too many bad permalinks")
        failures += 1
    else:
        print("    PASS")

    # 5. False-positive eyeball (10 random titles + subs)
    print("\n[4] Disambiguation eyeball sample (10 random — review titles)")
    eyeball_sample = random.sample(expansion_recs, min(10, len(expansion_recs)))
    for r in eyeball_sample:
        url = r["permalink"].rstrip("/") + ".json"
        data = _http_get_json(url)
        if data is None:
            print(f"    [SKIP] {r['permalink'][:80]} — fetch failed")
            continue
        try:
            post = data[0]["data"]["children"][0]["data"]
            title = post["title"]
            sub = post["subreddit"]
            created = datetime.fromtimestamp(post["created_utc"], tz=timezone.utc).date()
            print(f"    r/{sub:25s} | {created} | {title[:80]}")
        except (KeyError, IndexError, TypeError):
            print(f"    [PARSE_ERR] {r['permalink'][:80]}")
    print("    [Human review — do these look like real JoVE-journal threads?]")

    # 6. Date span — sample 50, count by year
    print("\n[5] Date span (sample of 50)")
    date_sample = random.sample(expansion_recs, min(50, len(expansion_recs)))
    year_counter: Counter[int] = Counter()
    earliest = None
    latest = None
    for r in date_sample:
        url = r["permalink"].rstrip("/") + ".json"
        data = _http_get_json(url)
        if data is None:
            continue
        try:
            post = data[0]["data"]["children"][0]["data"]
            ts = datetime.fromtimestamp(post["created_utc"], tz=timezone.utc)
            year_counter[ts.year] += 1
            if earliest is None or ts < earliest:
                earliest = ts
            if latest is None or ts > latest:
                latest = ts
        except (KeyError, IndexError, TypeError):
            continue

    print(f"    Earliest: {earliest}")
    print(f"    Latest: {latest}")
    print("    Year distribution (sample of 50):")
    for year in sorted(year_counter.keys()):
        print(f"      {year}: {year_counter[year]}")

    # Subreddit diversity — what new subs surfaced?
    print("\n[6] Subreddit diversity in expansion set")
    sub_counter = Counter(r["subreddit"] for r in expansion_recs)
    print(f"    Distinct subs: {len(sub_counter)}")
    print("    Top 25 by hit count:")
    for sub, count in sub_counter.most_common(25):
        marker = " (new)" if sub.lower() not in {
            "askacademia", "labrats", "phd", "gradschool", "postdoc",
            "biology", "chemistry", "biotech", "medicalschool", "neuroscience",
            "research", "professors", "askscience", "science", "scihub",
            "scholar", "engineeringstudents", "biostasis", "cogsci", "evopsych"
        } else ""
        print(f"      {count:4d}  r/{sub}{marker}")

    # Discovered_via breakdown
    print("\n[7] Discovered-via breakdown")
    via_counter = Counter(r["discovered_via"] for r in expansion_recs)
    for via, count in via_counter.most_common():
        print(f"    {count:4d}  {via}")

    # Seed_query expansion-marker breakdown
    seed_counter = Counter()
    for r in expansion_recs:
        sq = r.get("seed_query") or "(none)"
        # Group by expansion marker
        if sq.startswith("expansion:ring1"):
            seed_counter["expansion:ring1"] += 1
        elif sq.startswith("expansion:ring2"):
            seed_counter["expansion:ring2"] += 1
        elif sq.startswith("expansion:ring3"):
            seed_counter["expansion:ring3"] += 1
        else:
            seed_counter["other"] += 1
    print("\n[8] Seed-query expansion marker breakdown")
    for sq, count in seed_counter.most_common():
        print(f"    {count:4d}  {sq}")

    # Final verdict
    print("\n" + "=" * 70)
    if failures == 0:
        print("VALIDATION PASSED — 0 failures")
    else:
        print(f"VALIDATION FAILED — {failures} failure(s)")
    print("=" * 70)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
