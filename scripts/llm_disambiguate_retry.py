"""Retry the rate-limited candidates from the first disambiguation pass.

Reads data/discovery-candidates-expansion-REJECTED.jsonl, isolates the records
that failed at the 'fetch' stage (HTTP 429 etc.), refetches with patient
backoff, runs LLM disambiguation, and merges results into the VALIDATED /
REJECTED files. Records that failed at the 'llm' stage remain rejected.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from openai import AzureOpenAI

ROOT = Path(__file__).parent.parent
VALIDATED_PATH = ROOT / "data" / "discovery-candidates-expansion-VALIDATED.jsonl"
REJECTED_PATH = ROOT / "data" / "discovery-candidates-expansion-REJECTED.jsonl"

UA_POOL = [
    "voa-validator-retry/2.0 by /u/in-quiz-ition (https://github.com/jove-publishing)",
    "voa-research/1.1 by /u/in-quiz-ition (academic-research)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
]

SYSTEM_PROMPT = """You are validating Reddit threads for relevance to the academic journal "JoVE" (Journal of Visualized Experiments, https://jove.com).

Reject the thread if "JoVE" or "Jove" refers to:
- Jove / Jupiter the Roman god
- A faction or place in EVE Online
- A character name in Warhammer / Tarkov / any game / fiction
- A person's name (first name "Jove" appears in some non-English contexts)
- A music/album/band named "Jove" or jove.fm
- An unrelated acronym (e.g., "JOVE" as a software/project name)

Accept only if the thread is about the academic journal JoVE: its articles, peer review, fees, video format, access/paywalls, indexing, reputation, or any author/reader experience with the journal.

Return a tool call to `validate_thread`."""

TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "validate_thread",
            "parameters": {
                "type": "object",
                "properties": {
                    "is_jove_journal": {"type": "boolean"},
                    "confidence": {"type": "integer", "minimum": 1, "maximum": 5},
                    "reason": {"type": "string"},
                },
                "required": ["is_jove_journal", "confidence", "reason"],
            },
        },
    }
]


def fetch_thread_meta_patient(permalink: str, max_attempts: int = 4) -> dict[str, Any]:
    """Fetch with exponential backoff on 429, UA rotation, both reddit.com and old.reddit.com."""
    paths_to_try = [
        permalink,
        permalink.replace("www.reddit.com", "old.reddit.com"),
    ]
    last_err: dict[str, Any] = {"fetch_failed": True, "fetch_error": "no attempts ran"}
    for attempt in range(max_attempts):
        for url_base in paths_to_try:
            json_url = url_base.rstrip("/") + ".json?raw_json=1&limit=3"
            ua = random.choice(UA_POOL)
            try:
                r = requests.get(json_url, headers={"User-Agent": ua}, timeout=20)
            except requests.RequestException as e:
                last_err = {"fetch_failed": True, "fetch_error": str(e)}
                time.sleep(2)
                continue
            if r.status_code == 200:
                try:
                    data = r.json()
                    post = data[0]["data"]["children"][0]["data"]
                    title = post.get("title", "")
                    selftext = (post.get("selftext") or "")[:1000]
                    created_utc = post.get("created_utc")
                    comments_list = data[1]["data"]["children"] if len(data) > 1 else []
                    top_comments = []
                    for child in comments_list[:2]:
                        if child.get("kind") == "t1":
                            body = (child.get("data", {}).get("body") or "")[:500]
                            if body:
                                top_comments.append(body)
                    return {
                        "title": title,
                        "selftext": selftext,
                        "top_comments": top_comments,
                        "created_utc": created_utc,
                    }
                except (KeyError, IndexError, TypeError, ValueError) as e:
                    last_err = {"fetch_failed": True, "parse_error": str(e)}
                    break  # don't retry parse errors
            elif r.status_code in (429, 503):
                last_err = {"fetch_failed": True, "fetch_status": r.status_code}
                # exponential backoff: 8s, 20s, 60s, 120s
                delay = [8, 20, 60, 120][min(attempt, 3)]
                print(f"      [retry] 429 attempt {attempt + 1}/{max_attempts}, sleeping {delay}s before next attempt")
                time.sleep(delay)
                break  # break inner UA loop, retry outer attempt
            elif r.status_code in (404, 403):
                # permanent failures
                return {"fetch_failed": True, "fetch_status": r.status_code}
            else:
                last_err = {"fetch_failed": True, "fetch_status": r.status_code}
                time.sleep(2)
    return last_err


def build_user_message(c: dict[str, Any]) -> str:
    sub = c.get("subreddit", "")
    title = c.get("title", "")
    selftext = c.get("selftext", "")
    comments = c.get("top_comments", []) or []
    comments_str = "\n\n".join(f"[Comment {i+1}]: {b}" for i, b in enumerate(comments)) if comments else "(no comments captured)"
    return (
        f"Subreddit: r/{sub}\n"
        f"Title: {title}\n\n"
        f"Body excerpt:\n{selftext or '(empty)'}\n\n"
        f"Top comments:\n{comments_str}"
    )


def classify(client: AzureOpenAI, deployment: str, c: dict[str, Any]) -> dict[str, Any]:
    msg = build_user_message(c)
    try:
        resp = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": msg},
            ],
            tools=TOOL_SCHEMA,
            tool_choice={"type": "function", "function": {"name": "validate_thread"}},
            temperature=0.0,
            max_tokens=300,
        )
    except Exception as e:  # noqa: BLE001
        return {"llm_failed": True, "llm_error": str(e)}
    try:
        tcalls = resp.choices[0].message.tool_calls or []
        if not tcalls:
            return {"llm_failed": True, "llm_error": "no tool call returned"}
        args = json.loads(tcalls[0].function.arguments)
        return {
            "is_jove_journal": bool(args.get("is_jove_journal")),
            "confidence": int(args.get("confidence", 0)),
            "reason": str(args.get("reason", "")),
            "usage": {
                "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(resp.usage, "completion_tokens", 0),
            },
        }
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as e:
        return {"llm_failed": True, "llm_error": f"parse error: {e}"}


def main() -> int:
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]
    api_key = os.environ["AZURE_OPENAI_API_KEY"]
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1")
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
    client = AzureOpenAI(api_key=api_key, api_version=api_version, azure_endpoint=endpoint)

    # Read existing outputs
    validated: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    if VALIDATED_PATH.exists():
        validated = [json.loads(l) for l in VALIDATED_PATH.read_text().splitlines() if l.strip()]
    if REJECTED_PATH.exists():
        rejected = [json.loads(l) for l in REJECTED_PATH.read_text().splitlines() if l.strip()]

    # Identify fetch-failure records to retry; keep llm-stage rejections as final
    retryable = [r for r in rejected if r["_validation"]["stage"] == "fetch"]
    final_rejected = [r for r in rejected if r["_validation"]["stage"] != "fetch"]
    print(f"Loaded: {len(validated)} validated, {len(rejected)} rejected ({len(retryable)} retryable, {len(final_rejected)} final)")

    if not retryable:
        print("No retryable records; nothing to do.")
        return 0

    total_prompt_tokens = 0
    total_completion_tokens = 0
    newly_validated: list[dict[str, Any]] = []
    still_rejected: list[dict[str, Any]] = []

    for i, c in enumerate(retryable, 1):
        sub = c.get("subreddit", "?")
        permalink = c.get("permalink", "")
        print(f"\n[{i:2d}/{len(retryable)}] r/{sub:25s} {permalink[:70]}")

        meta = fetch_thread_meta_patient(permalink)
        if meta.get("fetch_failed"):
            print(f"    FETCH FAIL: {meta}")
            # Update the existing _validation record to reflect retry exhaustion
            c["_validation"] = {
                "is_jove_journal": None,
                "confidence": None,
                "reason": f"fetch_failed_after_retry: {meta}",
                "stage": "fetch",
            }
            still_rejected.append(c)
            continue

        # Successful fetch — strip prior _validation and re-classify
        c = {k: v for k, v in c.items() if k != "_validation"}
        c.update(meta)
        title_preview = (meta.get("title") or "")[:60]
        print(f"    Title: {title_preview}")

        result = classify(client, deployment, c)
        if result.get("llm_failed"):
            print(f"    LLM FAIL: {result.get('llm_error')}")
            c["_validation"] = {
                "is_jove_journal": None,
                "confidence": None,
                "reason": f"llm_failed: {result.get('llm_error')}",
                "stage": "llm",
            }
            still_rejected.append(c)
            continue

        is_jove = result["is_jove_journal"]
        conf = result["confidence"]
        reason = result["reason"]
        usage = result.get("usage", {})
        total_prompt_tokens += usage.get("prompt_tokens", 0)
        total_completion_tokens += usage.get("completion_tokens", 0)

        verdict_marker = "ACCEPT" if (is_jove and conf >= 4) else "REJECT"
        print(f"    {verdict_marker} | is_jove={is_jove} conf={conf} | {reason[:90]}")

        validation_meta = {
            "is_jove_journal": is_jove,
            "confidence": conf,
            "reason": reason,
            "stage": "llm",
        }

        if is_jove and conf >= 4:
            out = {
                k: v
                for k, v in c.items()
                if k in {"permalink", "subreddit", "discovered_via", "discovered_at", "seed_query"}
            }
            if c.get("created_utc"):
                out["_created_utc"] = c["created_utc"]
            out["_validation"] = validation_meta
            newly_validated.append(out)
        else:
            c["_validation"] = validation_meta
            still_rejected.append(c)

        # Inter-call pacing: ~3s between requests to be conservative
        time.sleep(3)

    # Merge
    final_validated = validated + newly_validated
    final_all_rejected = final_rejected + still_rejected

    with VALIDATED_PATH.open("w") as f:
        for r in final_validated:
            f.write(json.dumps(r) + "\n")
    with REJECTED_PATH.open("w") as f:
        for r in final_all_rejected:
            f.write(json.dumps(r) + "\n")

    # Report
    print("\n" + "=" * 72)
    print("RETRY REPORT")
    print("=" * 72)
    print(f"\nRetried: {len(retryable)} fetch-failure records")
    print(f"Newly validated: {len(newly_validated)}")
    print(f"Still rejected:  {len(still_rejected)}")

    print(f"\nFINAL TOTALS (across both passes):")
    print(f"  Validated: {len(final_validated)}")
    print(f"  Rejected:  {len(final_all_rejected)}")

    print("\nPer-subreddit breakdown — FINAL VALIDATED:")
    sub_val = Counter(r["subreddit"] for r in final_validated)
    for s, n in sub_val.most_common():
        print(f"  {n:3d}  r/{s}")

    print("\nPer-subreddit breakdown — FINAL REJECTED:")
    sub_rej = Counter(r["subreddit"] for r in final_all_rejected)
    for s, n in sub_rej.most_common():
        print(f"  {n:3d}  r/{s}")

    # Confidence dist
    print("\nConfidence distribution (validated):")
    conf_dist = Counter(r["_validation"]["confidence"] for r in final_validated)
    for c_val in sorted(conf_dist.keys(), reverse=True):
        print(f"  conf={c_val}: {conf_dist[c_val]}")

    print("\nConfidence distribution (rejected, where present):")
    conf_dist_rej = Counter(
        r["_validation"]["confidence"] for r in final_all_rejected if r["_validation"]["confidence"] is not None
    )
    for c_val in sorted(conf_dist_rej.keys(), reverse=True):
        print(f"  conf={c_val}: {conf_dist_rej[c_val]}")

    # Stage breakdown for rejections
    print("\nRejection stage breakdown:")
    stage_dist = Counter(r["_validation"]["stage"] for r in final_all_rejected)
    for s, n in stage_dist.most_common():
        print(f"  {s}: {n}")

    cost = (total_prompt_tokens / 1_000_000) * 2.0 + (total_completion_tokens / 1_000_000) * 8.0
    print(f"\nThis-retry token usage: prompt={total_prompt_tokens} completion={total_completion_tokens}")
    print(f"This-retry cost estimate: ${cost:.4f}")

    # Date span
    print("\nDate span (validated):")
    years = Counter()
    earliest_ts = None
    latest_ts = None
    for r in final_validated:
        ts = r.get("_created_utc")
        if not ts:
            continue
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        years[dt.year] += 1
        if earliest_ts is None or ts < earliest_ts:
            earliest_ts = ts
        if latest_ts is None or ts > latest_ts:
            latest_ts = ts
    if earliest_ts is not None and latest_ts is not None:
        print(f"  Earliest: {datetime.fromtimestamp(earliest_ts, tz=timezone.utc).date()}")
        print(f"  Latest:   {datetime.fromtimestamp(latest_ts, tz=timezone.utc).date()}")
        for y in sorted(years.keys()):
            print(f"  {y}: {years[y]}")
    else:
        print("  (no created_utc data captured)")

    print(f"\nOutputs updated:")
    print(f"  {VALIDATED_PATH}")
    print(f"  {REJECTED_PATH}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
