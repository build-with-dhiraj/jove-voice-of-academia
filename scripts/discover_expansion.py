"""v1.3 discovery EXPANSION sweep.

Append-only expansion of the existing 132-candidate set in
``data/discovery-candidates.jsonl``. Writes new candidates to a NEW file
``data/discovery-candidates-expansion.jsonl`` so the running main pipeline
(PID 59302 at time of authoring) is not disturbed.

Strategy
--------
The original ``pipeline/discover_unauth.py`` dropped Ring 3 entirely
(``FIRECRAWL_API_KEY`` not present on this machine), so historical depth
(2010-2017) is thin. This expansion adds:

1. **Ring 1 expansion** — enumerate ``/r/<sub>/new.json`` for ~25 new
   academia/medicine subreddits that were absent from the original
   ``data/seed-threads.json`` ``subreddits_core``/``subreddits_broad`` lists.
   Filter on JoVE alias substring match.

2. **Ring 2 expansion** — per-subreddit reddit search via
   ``/r/<sub>/search.json?restrict_sr=on`` for each JoVE alias × sort, for
   BOTH the original 18 subs AND the new ~25 subs. Critically, this picks
   up threads where JoVE appears in the title but the sub was missed by
   the original /r/all-scoped Ring 2 (which has shallower indexing per-sub).

3. **Ring 3 substitute (no Firecrawl)** — global Reddit search via
   ``/search.json`` for JoVE + topic-keyword combinations and 2010-2017
   year-anchored queries. Reddit's search doesn't take ``t=2014`` directly
   but accepts ``timestamp:`` cloudsearch operator on the legacy endpoint
   AND honors year-substring queries in title-search. We page deep
   (10 pages × 100 = 1000 results per query) and accept threads from
   ANY subreddit (the new subs list), letting downstream disambig
   reject false positives.

Dedup
-----
- Cross-reference all new candidates against the existing 132 by Reddit
  thread id, drop overlaps.
- Internal dedup by thread id during the sweep.
- Disambiguation kill-term + blacklist subreddit filter applied at
  discovery time (cheap title/sub-only signal) to drop obvious noise.

Output schema mirrors the existing JSONL exactly:
``{permalink, subreddit, discovered_via, discovered_at, seed_query}``
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# Make pipeline imports work
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.config import TIME_HORIZON, load_data_json  # noqa: E402
from pipeline.discover import extract_thread_id, extract_subreddit  # noqa: E402
from pipeline.models import ThreadCandidate  # noqa: E402

# ---- Logging setup ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("discover_expansion")

# ---- Tuning ----
_UNAUTH_REQUEST_SPACING_S = 1.6  # ~30 req/min comfortably under unauth ceiling
_UA = (
    "voa-v1.3-mega-expansion/0.1 by /u/in-quiz-ition "
    "(https://github.com/jove-publishing)"
)
_RING1_MAX_PAGES = 50  # 50 * 100 = up to 5k posts per sub (tighter for expansion)
_RING2_MAX_PAGES = 10  # Reddit search caps at ~1000 results

# Ring 2 sort orders. Original uses 3 (relevance/new/top), but for per-sub
# expansion we drop sort=top — relevance + new together already surface
# JoVE-relevance hits and sort=top adds ~33% runtime for marginal recall.
_RING2_PER_SUB_SORTS = ("relevance", "new")

# ---- Subreddit lists ----

# Existing subs (from seed-threads.json) — DO NOT re-enumerate Ring 1 here
# (the original pipeline already covered them). We DO re-do Ring 2 per-sub
# scoped searches against them because the original Ring 2 used /r/all.
EXISTING_SUBS = {
    "AskAcademia",
    "labrats",
    "PhD",
    "GradSchool",
    "postdoc",  # Note: present in seed-threads but produced 0 ring1 hits — re-attempting
    "biology",
    "chemistry",
    "biotech",
    "medicalschool",
    "neuroscience",
    "research",
    "Professors",
    "AskScience",
    "science",
    "scihub",
    "Scholar",
    "EngineeringStudents",
    "biostasis",
    "cogsci",
    "evopsych",
}

# NEW subreddits not in the original 18 — expansion target
NEW_SUBS = [
    # Direct asks from the dispatch brief:
    "medicine",
    "academia",
    "sciencepublishing",
    "academicpublishing",
    "ResearchAdvice",
    "AcademicPsychology",
    # Additional high-signal academia/medicine
    "Nursing",
    "premed",
    "biochemistry",
    "microbiology",
    "Microbiome",
    "immunology",
    "AskMedicine",
    "askscientists",
    "MedicalResidents",
    "doctorate",
    "MedSchool",
    "MedicalSchoolEU",
    "Mcat",
    "Optometry",
    "Residency",
    "Pathology",
    "pharmacology",
    "Physiology",
    "molecularbiology",
    "geneticist",
    "Genetics",
    "genome",
    "bioinformatics",
    "EvolutionaryBiology",
    "ecology",
    "marinebiology",
    "BioStatistics",
    "Statistics",
    "DataScience",
    "AskStatistics",
    "AcademicQuant",
    "ScienceBasedParenting",
    "academiceconomics",
    "AskHistorians",  # often discuss methodology/sources
    "labtech",
    "biotech_research",
    "BiotechCareers",
    "EngineeringPhDs",
    "EngineeringResearch",
    "BiomedicalEngineers",
    "AskEngineers",
    "ChemistryHelp",
    "AskChemistry",
    "physicsphd",
    "physicsstudents",
    "AskPhysics",
    "Physics",
    "biostatistics",
    "epidemiology",
    "publichealth",
    "PublicHealth",
    "thesis",
    "GradAdmissions",
    "AskProfessors",
    "Academia",  # case variant
    "professors",  # case variant
    "ResearchPaper",
    "psychology",
    "AcademicBiblical",
    "AcademicQuran",
    "academicchatter",
    "Astrobiology",
    "OpenAccessJournal",
    "openaccess",
    "scholar",  # case variant
    "GradResearch",
    "GraduateSchool",
    "compbio",
    "computationalbiology",
    "Neuropsychology",
    "neuro",
    "Neuroscientists",
    "biophysics",
    "CellBiology",
    "DevelopmentalBiology",
    "Endocrinology",
    "Cardiology",
    "ScienceCareers",
    "scienceisawesome",
    "everythingscience",
    "labwork",
    "PhDStress",
    "PhdProductivity",
    "PhDLife",
    "JournalClub",  # high-signal: this is literally about journals!
    "AskAcademiaUK",
    "PhDUK",
    "ScienceUK",
    "phdstudents",
    "AcademicWriting",
    "AcademicLibrarians",
    "Librarianship",
    "OpenScience",
    "ReproducibleResearch",
    "datasets",
    "researchchemicals",  # noise filter — likely produces 0
    "researchers",
]

# Dedupe NEW_SUBS preserving order, normalize case-insensitively against EXISTING_SUBS
def _normalize_new_subs() -> list[str]:
    existing_lc = {s.lower() for s in EXISTING_SUBS}
    seen: set[str] = set()
    out: list[str] = []
    for s in NEW_SUBS:
        key = s.lower()
        if key in existing_lc or key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


# ---- Queries ----

# Ring 3 substitute (no Firecrawl) — global Reddit search queries chosen
# to maximize historical-depth recall. Year-anchored queries exploit
# Reddit's case-insensitive substring match on titles+body.
_RING3_SUB_QUERIES = [
    # Core aliases (most basic)
    'JoVE',
    '"Journal of Visualized Experiments"',
    'jove.com',
    'myjove',
    # Topic-keyed (predatory, peer review, fees, etc.) — already covered
    # in main Ring 2, but re-run here without /r/all sub-scope so we catch
    # threads from non-seed subreddits.
    'JoVE legit',
    'JoVE respectable',
    'JoVE predatory',
    'JoVE peer review',
    'JoVE special issue',
    'JoVE guest editor',
    'JoVE request',
    'JoVE invitation',
    'JoVE fees',
    'JoVE author',
    'JoVE access',
    'JoVE video',
    'JoVE protocol',
    'JoVE subscription',
    'JoVE impact factor',
    'JoVE PubMed',
    'JoVE manuscript',
    'JoVE submission',
    'JoVE publish',
    'JoVE editor',
    'JoVE expensive',
    'JoVE worth',
    # Year-anchored historical-depth queries (Reddit doesn't have a true
    # date-range search, but year-tokens in title/selftext often appear)
    'JoVE 2014',
    'JoVE 2015',
    'JoVE 2016',
    'JoVE 2013',
    'JoVE 2012',
    'JoVE 2011',
    'JoVE 2010',
    'JoVE 2017',
    'JoVE 2018',
    'JoVE 2019',
    'JoVE 2020',
]

# Per-sub Ring 2 search queries — narrower set since /r/<sub>/search is
# already scope-limited, so we don't need topic-keyed variants
_PER_SUB_QUERIES = [
    "JoVE",
    '"Journal of Visualized Experiments"',
    "jove.com",
    "myjove",
    "video journal",
    "video protocol",
]


# ---- HTTP ----

_last_request_at = 0.0


def _http_get_json(url: str, *, params: dict[str, Any] | None = None, max_retries: int = 3) -> Any:
    """GET a Reddit .json URL with the project user-agent.

    Sleeps the unauth spacing before the call so callers can't accidentally
    burst. On 429 backs off harder.
    """

    global _last_request_at
    headers = {"User-Agent": _UA}

    for attempt in range(max_retries):
        # Throttle
        elapsed = time.monotonic() - _last_request_at
        if elapsed < _UNAUTH_REQUEST_SPACING_S:
            time.sleep(_UNAUTH_REQUEST_SPACING_S - elapsed)

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            _last_request_at = time.monotonic()
        except requests.RequestException as exc:
            logger.warning("HTTP transport error (attempt %d): %s", attempt + 1, exc)
            time.sleep(5.0 * (attempt + 1))
            continue

        if resp.status_code == 429:
            logger.warning("429 from Reddit — backing off 60s (attempt %d)", attempt + 1)
            time.sleep(60.0)
            continue
        if resp.status_code in (500, 502, 503, 504):
            logger.warning("Reddit %d (attempt %d) — backing off", resp.status_code, attempt + 1)
            time.sleep(10.0 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()

    raise RuntimeError(f"Exhausted retries for {url}")


# ---- Filters ----


def _v1_journal_aliases() -> list[str]:
    journals = load_data_json("journals.json")
    return list(journals.get("jove_aliases_to_match", []))


def _matches_aliases(text: str, aliases: Iterable[str]) -> bool:
    if not text:
        return False
    haystack = text.lower()
    return any(alias.lower() in haystack for alias in aliases)


def _load_disambig_kill_terms() -> list[str]:
    raw = load_data_json("disambiguation.json")
    return [t.lower() for t in raw["false_positive_kill_terms"]["terms"]]


def _load_subreddit_blacklist() -> set[str]:
    raw = load_data_json("disambiguation.json")
    return {s.lower() for s in raw["subreddit_blacklist"]["subreddits"]}


def _passes_cheap_disambig(
    title: str,
    subreddit: str,
    kill_terms: list[str],
    blacklist: set[str],
    selftext: str = "",
    require_strong_signal: bool = False,
) -> tuple[bool, str]:
    """Cheap pre-filter applied at discovery time.

    Mirrors stages 1-2 of pipeline.disambiguate (the only ones we can run
    at discovery time without the full Thread + comments fetch).

    When ``require_strong_signal`` is True (used by the global Ring 3
    substitute), require that title OR selftext contains one of the
    strong positive signals — drops noise like KerbalSpaceProgram /
    Barotrauma threads where "jove" appears in a non-journal context.
    """
    title_lc = title.lower()
    for kt in kill_terms:
        if kt in title_lc:
            return False, f"killed_by_title_term:{kt}"
    if subreddit.lower() in blacklist:
        return False, "subreddit_blacklist"

    if require_strong_signal:
        haystack = (title + " " + selftext).lower()
        # Strong positive signals — must hit at least one
        strong_signals = [
            "jove",  # case-insensitive substring
            "journal of visualized experiments",
            "journal of visual experiments",
            "jove.com",
            "myjove",
            "video protocol",
            "video journal",
        ]
        # "jove" alone is too weak by itself — require co-mention of an
        # academia context term OR an explicit JoVE alias variant.
        haystack_has_jove = any(s in haystack for s in strong_signals if s != "jove")
        if not haystack_has_jove:
            # Fall back to requiring "jove" + an academia context cue
            if "jove" not in haystack:
                return False, "no_strong_signal"
            academia_cues = [
                "journal", "peer review", "peer-review", "manuscript", "submission",
                "impact factor", "pubmed", "medline", "indexed", "doi.org",
                "open access", "predator", "predatory", "academic",
                "methods paper", "video protocol", "video journal",
                "guest editor", "special issue", "publication", "publish",
            ]
            if not any(cue in haystack for cue in academia_cues):
                return False, "jove_without_academia_context"
    return True, "passed"


def _load_existing_thread_ids() -> set[str]:
    """Load thread IDs already in data/discovery-candidates.jsonl (don't re-add)."""
    path = Path(__file__).parent.parent / "data" / "discovery-candidates.jsonl"
    if not path.exists():
        return set()
    ids: set[str] = set()
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            tid = extract_thread_id(d.get("permalink", ""))
            if tid:
                ids.add(tid)
    return ids


# ---- Ring 1 expansion: enumerate new subs ----


def ring1_expansion(
    subs: list[str],
    time_horizon: datetime = TIME_HORIZON,
    kill_terms: list[str] | None = None,
    blacklist: set[str] | None = None,
) -> Iterator[ThreadCandidate]:
    aliases = _v1_journal_aliases()
    if not aliases:
        logger.warning("Ring 1: no JoVE aliases — skipping")
        return
    kill_terms = kill_terms or _load_disambig_kill_terms()
    blacklist = blacklist or _load_subreddit_blacklist()
    horizon_ts = time_horizon.timestamp()

    for sub in subs:
        if sub.lower() in blacklist:
            logger.info("Ring 1 expansion: skipping blacklisted r/%s", sub)
            continue
        logger.info("Ring 1 expansion: enumerating r/%s (max %d pages)", sub, _RING1_MAX_PAGES)
        after: str | None = None
        page_count = 0
        hit_count = 0
        skipped_404 = False

        while page_count < _RING1_MAX_PAGES:
            page_count += 1
            url = f"https://www.reddit.com/r/{sub}/new.json"
            params: dict[str, Any] = {"limit": 100, "raw_json": 1}
            if after:
                params["after"] = after

            try:
                data = _http_get_json(url, params=params)
            except requests.HTTPError as exc:
                status = getattr(exc.response, "status_code", "?")
                if status in (403, 404):
                    logger.info("Ring 1 expansion: r/%s %s — sub does not exist or is private", sub, status)
                    skipped_404 = True
                else:
                    logger.warning("Ring 1 expansion: r/%s page %d HTTP %s", sub, page_count, status)
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("Ring 1 expansion: r/%s page %d error: %s", sub, page_count, exc)
                break

            children = data.get("data", {}).get("children", []) or []
            if not children:
                break

            stop_paging = False
            for child in children:
                d = child.get("data", {})
                created = d.get("created_utc", 0)
                if created < horizon_ts:
                    stop_paging = True
                    break
                title = d.get("title", "")
                selftext = d.get("selftext", "")
                if not _matches_aliases(title + " " + selftext, aliases):
                    continue
                sub_actual = d.get("subreddit", sub)
                ok, _reason = _passes_cheap_disambig(
                    title, sub_actual, kill_terms, blacklist, selftext=selftext
                )
                if not ok:
                    continue
                permalink = "https://www.reddit.com" + d.get("permalink", "")
                # NOTE: discovered_via is a Pydantic Literal — must use one of
                # the 3 original values. We encode the expansion provenance
                # in seed_query so downstream audit can distinguish.
                yield ThreadCandidate(
                    permalink=permalink,
                    subreddit=sub_actual,
                    discovered_via="ring1_subreddit_enum",
                    discovered_at=datetime.now(timezone.utc),
                    seed_query="expansion:ring1_subreddit_enum",
                )
                hit_count += 1

            after = data.get("data", {}).get("after")
            if not after or stop_paging:
                break

        if not skipped_404:
            logger.info("Ring 1 expansion: r/%s done — %d pages, %d hits", sub, page_count, hit_count)


# ---- Ring 2 expansion: per-sub scoped search ----


def ring2_per_sub_search(
    subs: list[str],
    kill_terms: list[str] | None = None,
    blacklist: set[str] | None = None,
) -> Iterator[ThreadCandidate]:
    """Search EACH subreddit individually using /r/<sub>/search.json.

    Compared to the main Ring 2 (which scoped to /r/all and filtered),
    this hits per-sub search which has DIFFERENT indexing semantics —
    catches threads that /r/all search ranked low or missed.
    """
    kill_terms = kill_terms or _load_disambig_kill_terms()
    blacklist = blacklist or _load_subreddit_blacklist()

    for sub in subs:
        if sub.lower() in blacklist:
            continue
        for query in _PER_SUB_QUERIES:
            for sort in _RING2_PER_SUB_SORTS:
                logger.info("Ring 2 expansion: r/%s search sort=%s q=%s", sub, sort, query)
                after: str | None = None
                page_count = 0
                kept = 0

                while page_count < _RING2_MAX_PAGES:
                    page_count += 1
                    url = f"https://www.reddit.com/r/{sub}/search.json"
                    params: dict[str, Any] = {
                        "q": query,
                        "sort": sort,
                        "t": "all",
                        "limit": 100,
                        "restrict_sr": "on",
                        "raw_json": 1,
                    }
                    if after:
                        params["after"] = after

                    try:
                        data = _http_get_json(url, params=params)
                    except requests.HTTPError as exc:
                        status = getattr(exc.response, "status_code", "?")
                        if status in (403, 404):
                            logger.info("Ring 2 expansion: r/%s %s — skipping", sub, status)
                        else:
                            logger.warning("Ring 2 expansion: r/%s page %d HTTP %s", sub, page_count, status)
                        break
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Ring 2 expansion: r/%s page %d error: %s", sub, page_count, exc)
                        break

                    children = data.get("data", {}).get("children", []) or []
                    if not children:
                        break

                    for child in children:
                        d = child.get("data", {})
                        sub_actual = d.get("subreddit", sub)
                        title = d.get("title", "")
                        selftext = d.get("selftext", "")
                        created = d.get("created_utc", 0)
                        if created and created < TIME_HORIZON.timestamp():
                            continue
                        ok, _reason = _passes_cheap_disambig(
                            title, sub_actual, kill_terms, blacklist,
                            selftext=selftext,
                            # Per-sub search already scopes by sub, so we don't
                            # need the strong-signal filter for the existing
                            # 18 academia-core subs. Apply it for new subs
                            # whose academia-context is weaker.
                            require_strong_signal=sub.lower() not in {s.lower() for s in EXISTING_SUBS},
                        )
                        if not ok:
                            continue
                        permalink = "https://www.reddit.com" + d.get("permalink", "")
                        yield ThreadCandidate(
                            permalink=permalink,
                            subreddit=sub_actual,
                            discovered_via="ring2_reddit_search",
                            discovered_at=datetime.now(timezone.utc),
                            seed_query=f"expansion:ring2_per_sub r/{sub} q={query} sort={sort}",
                        )
                        kept += 1

                    after = data.get("data", {}).get("after")
                    if not after:
                        break

                logger.info("Ring 2 expansion: r/%s q=%s sort=%s — %d pages, %d hits",
                            sub, query, sort, page_count, kept)


# ---- Ring 3 substitute: global Reddit search w/o sub restriction ----


def ring3_substitute_global_search(
    kill_terms: list[str] | None = None,
    blacklist: set[str] | None = None,
) -> Iterator[ThreadCandidate]:
    """Global Reddit search across ALL subreddits, NO sub-scope filter.

    The original Ring 2 in discover_unauth.py filtered hits to the seed
    subreddit universe (18 subs) via _in_scope_subreddits(). That throws
    away hits from any sub not in the seed list. This expansion drops
    that filter — accepts hits from ANY sub, lets downstream disambig
    reject false positives via subreddit_blacklist + content gates.

    Includes year-anchored queries to surface 2010-2017 historical threads.
    """
    kill_terms = kill_terms or _load_disambig_kill_terms()
    blacklist = blacklist or _load_subreddit_blacklist()

    sorts = ("relevance", "new", "top")

    for query in _RING3_SUB_QUERIES:
        for sort in sorts:
            logger.info("Ring 3 sub: GLOBAL search sort=%s q=%s", sort, query)
            after: str | None = None
            page_count = 0
            kept = 0

            while page_count < _RING2_MAX_PAGES:
                page_count += 1
                url = "https://www.reddit.com/search.json"
                params: dict[str, Any] = {
                    "q": query,
                    "sort": sort,
                    "t": "all",
                    "limit": 100,
                    "raw_json": 1,
                }
                if after:
                    params["after"] = after

                try:
                    data = _http_get_json(url, params=params)
                except requests.HTTPError as exc:
                    status = getattr(exc.response, "status_code", "?")
                    logger.warning("Ring 3 sub: HTTP %s on q=%s sort=%s p=%d", status, query, sort, page_count)
                    break
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Ring 3 sub: error on q=%s sort=%s p=%d: %s", query, sort, page_count, exc)
                    break

                children = data.get("data", {}).get("children", []) or []
                if not children:
                    break

                for child in children:
                    d = child.get("data", {})
                    sub_actual = d.get("subreddit", "")
                    title = d.get("title", "")
                    selftext = d.get("selftext", "")
                    created = d.get("created_utc", 0)
                    if not sub_actual:
                        continue
                    # Time horizon filter — brief is "2010 till date".
                    # Reddit's global search returns 2007-2009 results too;
                    # filter them out at discovery to save downstream fetch.
                    if created and created < TIME_HORIZON.timestamp():
                        continue
                    ok, _reason = _passes_cheap_disambig(
                        title, sub_actual, kill_terms, blacklist,
                        selftext=selftext,
                        # Global Ring 3 is unscoped — strict signal required
                        # to avoid Barotrauma / KerbalSpaceProgram noise.
                        require_strong_signal=True,
                    )
                    if not ok:
                        continue
                    permalink = "https://www.reddit.com" + d.get("permalink", "")
                    # NOTE: we use ring3_firecrawl_google as the discovered_via
                    # value even though this is a global-search substitute (no
                    # Firecrawl). The seed_query encodes the actual mechanism
                    # for audit. Done this way because ThreadCandidate.discovered_via
                    # is a Pydantic Literal and the downstream pipeline expects
                    # one of the 3 ring names.
                    yield ThreadCandidate(
                        permalink=permalink,
                        subreddit=sub_actual,
                        discovered_via="ring3_firecrawl_google",
                        discovered_at=datetime.now(timezone.utc),
                        seed_query=f"expansion:ring3_global_search q={query} sort={sort}",
                    )
                    kept += 1

                after = data.get("data", {}).get("after")
                if not after:
                    break

            logger.info("Ring 3 sub: q=%s sort=%s — %d pages, %d hits", query, sort, page_count, kept)


# ---- Orchestrator ----


def main() -> int:
    out_path = Path(__file__).parent.parent / "data" / "discovery-candidates-expansion.jsonl"

    # Build sub list AFTER dedup against existing
    new_subs = _normalize_new_subs()
    logger.info("Expansion target: %d new subreddits (filtered against existing %d)",
                len(new_subs), len(EXISTING_SUBS))

    existing_ids = _load_existing_thread_ids()
    logger.info("Loaded %d existing thread IDs to dedup against", len(existing_ids))

    kill_terms = _load_disambig_kill_terms()
    blacklist = _load_subreddit_blacklist()

    # Internal dedup across the three rings of this expansion
    seen_in_run: set[str] = set()

    # Stats
    stats = {
        "ring1_expansion_emitted": 0,
        "ring2_expansion_emitted": 0,
        "ring3_substitute_emitted": 0,
        "dedupe_overlap_with_existing": 0,
        "dedupe_overlap_within_expansion": 0,
        "killed_by_disambig": 0,
    }

    def _emit(cand: ThreadCandidate, source_key: str) -> bool:
        tid = extract_thread_id(cand.permalink)
        if tid is None:
            return False
        if tid in existing_ids:
            stats["dedupe_overlap_with_existing"] += 1
            return False
        if tid in seen_in_run:
            stats["dedupe_overlap_within_expansion"] += 1
            return False
        seen_in_run.add(tid)
        stats[source_key] += 1
        return True

    with out_path.open("w") as out:
        # Order: cheapest-highest-payoff first so partial runs are still useful.
        # Ring 3 (global search, ~10 min) → Ring 2 (per-sub search, ~40 min) →
        # Ring 1 (full enumeration of new subs, ~80 min).

        # Ring 3 substitute (global search, no sub-scope filter) — historical depth
        logger.info("=== RING 3 SUBSTITUTE (global Reddit search, no sub-scope) ===")
        for cand in ring3_substitute_global_search(kill_terms=kill_terms, blacklist=blacklist):
            if _emit(cand, "ring3_substitute_emitted"):
                out.write(cand.model_dump_json() + "\n")
                out.flush()

        # Ring 2 expansion (per-sub search) — run on BOTH existing + new subs
        logger.info("=== RING 2 EXPANSION (per-sub search) ===")
        all_subs_for_ring2 = list(EXISTING_SUBS) + new_subs
        for cand in ring2_per_sub_search(all_subs_for_ring2, kill_terms=kill_terms, blacklist=blacklist):
            if _emit(cand, "ring2_expansion_emitted"):
                out.write(cand.model_dump_json() + "\n")
                out.flush()

        # Ring 1 expansion (new subs only) — most expensive
        logger.info("=== RING 1 EXPANSION (enumerate new subreddits) ===")
        for cand in ring1_expansion(new_subs, kill_terms=kill_terms, blacklist=blacklist):
            if _emit(cand, "ring1_expansion_emitted"):
                out.write(cand.model_dump_json() + "\n")
                out.flush()

    logger.info("=== EXPANSION COMPLETE ===")
    for k, v in stats.items():
        logger.info("  %s: %d", k, v)
    total = stats["ring1_expansion_emitted"] + stats["ring2_expansion_emitted"] + stats["ring3_substitute_emitted"]
    logger.info("  TOTAL NEW CANDIDATES: %d", total)
    logger.info("  Output: %s", out_path)

    # Write stats sidecar
    stats_path = out_path.with_suffix(".stats.json")
    with stats_path.open("w") as f:
        json.dump(
            {
                "run_at": datetime.now(timezone.utc).isoformat(),
                "new_subs_targeted": new_subs,
                "existing_subs_for_ring2_rerun": sorted(EXISTING_SUBS),
                "ring1_queries": ["new feed enum"],
                "ring2_queries": _PER_SUB_QUERIES,
                "ring3_queries": _RING3_SUB_QUERIES,
                "stats": stats,
                "total_new_candidates": total,
            },
            f,
            indent=2,
        )
    logger.info("  Stats: %s", stats_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
