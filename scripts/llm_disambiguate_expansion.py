"""LLM-based disambiguation of the 47 expansion candidates.

For each candidate:
1. Fetch Reddit thread metadata (title, body, top-2 comments) — unauth, rate-limited.
2. Send to GPT-4.1 via Azure with strict tool-call schema.
3. Accept only if is_jove_journal == True AND confidence >= 4.

Writes:
- data/discovery-candidates-expansion-VALIDATED.jsonl  (clean subset)
- data/discovery-candidates-expansion-REJECTED.jsonl   (with _validation field for audit)
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from openai import AzureOpenAI

ROOT = Path(__file__).parent.parent
INPUT_PATH = ROOT / "data" / "discovery-candidates-expansion-UNVALIDATED.jsonl"
VALIDATED_PATH = ROOT / "data" / "discovery-candidates-expansion-VALIDATED.jsonl"
REJECTED_PATH = ROOT / "data" / "discovery-candidates-expansion-REJECTED.jsonl"

UA = (
    "voa-v1.3-validator/1.0 by /u/in-quiz-ition "
    "(https://github.com/jove-publishing)"
)

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
            "description": "Validate whether a Reddit thread is about the JoVE academic journal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "is_jove_journal": {
                        "type": "boolean",
                        "description": "True if the thread is about the academic journal JoVE; False if it refers to anything else (Roman god, EVE Online, game character, person name, unrelated acronym, etc.).",
                    },
                    "confidence": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": "Confidence on a 1-5 scale. 5 = absolutely certain, 4 = highly confident, 1-3 = uncertain.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Brief explanation (1-2 sentences).",
                    },
                },
                "required": ["is_jove_journal", "confidence", "reason"],
            },
        },
    }
]


def fetch_thread_meta(permalink: str) -> dict[str, Any]:
    """Fetch title, selftext, top-2 comments, and created_utc from Reddit JSON API."""
    json_url = permalink.rstrip("/") + ".json?raw_json=1&limit=3"
    try:
        r = requests.get(json_url, headers={"User-Agent": UA}, timeout=15)
    except requests.RequestException as e:
        return {"fetch_failed": True, "fetch_error": str(e)}
    if r.status_code != 200:
        return {"fetch_failed": True, "fetch_status": r.status_code}
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
        return {"fetch_failed": True, "parse_error": str(e)}


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
    """Run GPT-4.1 disambiguation with strict tool-call output."""
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
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")

    client = AzureOpenAI(
        api_key=api_key,
        api_version=api_version,
        azure_endpoint=endpoint,
    )

    candidates = [json.loads(line) for line in INPUT_PATH.read_text().splitlines() if line.strip()]
    print(f"Loaded {len(candidates)} candidates from {INPUT_PATH.name}")

    validated: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    total_prompt_tokens = 0
    total_completion_tokens = 0

    accept_examples: list[dict[str, Any]] = []
    reject_examples: list[dict[str, Any]] = []

    for i, c in enumerate(candidates, 1):
        sub = c.get("subreddit", "?")
        permalink = c.get("permalink", "")
        print(f"\n[{i:2d}/{len(candidates)}] r/{sub:25s} {permalink[:70]}")

        # 1. Fetch thread metadata (rate-limited)
        meta = fetch_thread_meta(permalink)
        time.sleep(1.6)  # ~37 req/min, safely under 60/min unauth limit

        if meta.get("fetch_failed"):
            print(f"    FETCH FAIL: {meta}")
            rec = dict(c)
            rec["_validation"] = {
                "is_jove_journal": None,
                "confidence": None,
                "reason": f"fetch_failed: {meta}",
                "stage": "fetch",
            }
            rejected.append(rec)
            continue

        c.update(meta)
        title_preview = (meta.get("title") or "")[:60]
        print(f"    Title: {title_preview}")

        # 2. LLM disambiguation
        result = classify(client, deployment, c)

        if result.get("llm_failed"):
            print(f"    LLM FAIL: {result.get('llm_error')}")
            rec = dict(c)
            rec["_validation"] = {
                "is_jove_journal": None,
                "confidence": None,
                "reason": f"llm_failed: {result.get('llm_error')}",
                "stage": "llm",
            }
            rejected.append(rec)
            continue

        is_jove = result["is_jove_journal"]
        conf = result["confidence"]
        reason = result["reason"]
        usage = result.get("usage", {})
        total_prompt_tokens += usage.get("prompt_tokens", 0)
        total_completion_tokens += usage.get("completion_tokens", 0)

        verdict_marker = "ACCEPT" if (is_jove and conf >= 4) else "REJECT"
        print(f"    {verdict_marker} | is_jove={is_jove} conf={conf} | {reason[:90]}")

        rec = dict(c)
        rec["_validation"] = {
            "is_jove_journal": is_jove,
            "confidence": conf,
            "reason": reason,
            "stage": "llm",
        }

        if is_jove and conf >= 4:
            # strip the LLM-only meta fields from the output (preserve original schema + add _validation for trace)
            out = {
                k: v
                for k, v in rec.items()
                if k in {"permalink", "subreddit", "discovered_via", "discovered_at", "seed_query"}
            }
            # also retain created_utc as ISO if we have it (useful for downstream date-span report)
            if rec.get("created_utc"):
                out["_created_utc"] = rec["created_utc"]
            out["_validation"] = rec["_validation"]
            validated.append(out)
            if len(accept_examples) < 3:
                accept_examples.append(
                    {"subreddit": sub, "title": title_preview, "reason": reason, "conf": conf}
                )
        else:
            rejected.append(rec)
            if len(reject_examples) < 3:
                reject_examples.append(
                    {"subreddit": sub, "title": title_preview, "reason": reason, "is_jove": is_jove, "conf": conf}
                )

    # Write outputs
    with VALIDATED_PATH.open("w") as f:
        for r in validated:
            f.write(json.dumps(r) + "\n")
    with REJECTED_PATH.open("w") as f:
        for r in rejected:
            f.write(json.dumps(r) + "\n")

    # ============== REPORT ==============
    print("\n" + "=" * 72)
    print("VALIDATION REPORT")
    print("=" * 72)

    n_total = len(candidates)
    n_val = len(validated)
    n_rej = len(rejected)
    print(f"\nTotal validated: {n_val}/{n_total}")
    print(f"Total rejected:  {n_rej}/{n_total}")

    print("\nPer-subreddit breakdown — VALIDATED:")
    sub_val = Counter(r["subreddit"] for r in validated)
    for s, n in sub_val.most_common():
        print(f"  {n:3d}  r/{s}")

    print("\nPer-subreddit breakdown — REJECTED:")
    sub_rej = Counter(r["subreddit"] for r in rejected)
    for s, n in sub_rej.most_common():
        print(f"  {n:3d}  r/{s}")

    print("\nConfidence distribution (validated):")
    conf_dist = Counter(r["_validation"]["confidence"] for r in validated)
    for c_val in sorted(conf_dist.keys(), reverse=True):
        print(f"  conf={c_val}: {conf_dist[c_val]}")

    print("\nConfidence distribution (rejected, where present):")
    conf_dist_rej = Counter(
        r["_validation"]["confidence"] for r in rejected if r["_validation"]["confidence"] is not None
    )
    for c_val in sorted(conf_dist_rej.keys(), reverse=True):
        print(f"  conf={c_val}: {conf_dist_rej[c_val]}")

    # Cost: gpt-4.1 Azure ~ $2/Mtok input, $8/Mtok output (current at time of writing)
    cost_estimate = (total_prompt_tokens / 1_000_000) * 2.0 + (total_completion_tokens / 1_000_000) * 8.0
    print(f"\nToken usage: prompt={total_prompt_tokens} completion={total_completion_tokens}")
    print(f"Estimated cost: ${cost_estimate:.4f} (gpt-4.1 Azure rates: $2/Mtok input, $8/Mtok output)")

    print("\nNotable ACCEPT examples:")
    for ex in accept_examples:
        print(f"  r/{ex['subreddit']} (conf={ex['conf']}): {ex['title']}")
        print(f"    -> {ex['reason']}")

    print("\nNotable REJECT examples:")
    for ex in reject_examples:
        print(f"  r/{ex['subreddit']} (is_jove={ex['is_jove']}, conf={ex['conf']}): {ex['title']}")
        print(f"    -> {ex['reason']}")

    # Date span for validated
    print("\nDate span (validated):")
    years = Counter()
    earliest_ts = None
    latest_ts = None
    for r in validated:
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
        print("  (no created_utc data captured for any validated record)")

    print(f"\nOutputs written:")
    print(f"  {VALIDATED_PATH}")
    print(f"  {REJECTED_PATH}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
