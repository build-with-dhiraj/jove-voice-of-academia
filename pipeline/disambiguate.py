"""JoVE-vs-noise disambiguation filter.

Phase 4 of the Voice of Academia pipeline. Given a fetched :class:`Thread`
(from Phase 3 fetch), decide whether it's actually about *JoVE the scientific
journal* — or a false positive (the Roman god, JoJo's Bizarre Adventure,
jove.fm music, given/middle names containing "Jo", etc.).

The filter is a deterministic 4-stage gate driven by rules in
``data/disambiguation.json``:

1. **Title kill terms** — hard-fail if the title contains any
   ``false_positive_kill_terms`` (e.g. "by Jove", "JoJo's", "Stardust
   Crusaders", "name Jo"). Cheapest stage, runs first.
2. **Subreddit blacklist** — hard-fail if the thread's subreddit is in
   ``subreddit_blacklist`` (case-insensitive).
3. **Positive-signal requirement** — at least one of the
   ``true_positive_signals`` terms (e.g. "JoVE", "Journal of Visualized
   Experiments", "jove.com") must appear in the title, body, or top-3
   comments. Without this, the thread is presumed off-topic.
4. **Subreddit-context allowance** — if the subreddit is NOT an
   ``academia_core`` sub (AskAcademia, labrats, PhD, ...), then at least
   one ``context_required`` term ("journal", "paper", "peer review", ...)
   must also appear in the haystack. Academia-core subs get a pass.

The function returns ``(in_scope, reason)`` so the dashboard / audit log
can surface *why* each thread was accepted or rejected. The reason string
is one of a small documented set — see :func:`is_in_scope`'s docstring.

The function is **idempotent and stateless**: same Thread + same rules
yields the same result. Rules are loaded once at module import time via
:func:`_default_rules` (lazy singleton) so the discover→fetch→disambiguate
backfill loop doesn't re-parse JSON for each thread.
"""

from __future__ import annotations

import functools

from pipeline.config import load_data_json
from pipeline.models import Thread

__all__ = ["DisambigRules", "is_in_scope"]


class DisambigRules:
    """In-memory representation of ``data/disambiguation.json``.

    Loaded once and reused across many :func:`is_in_scope` calls. All
    term/subreddit collections are lowercased at construction time so the
    hot loop does ``substring in haystack_lower`` without per-call casing.

    Attributes:
        true_positive_signals: Lowercased terms that signal *journal* context.
        false_positive_kill_terms: Lowercased terms that kill a thread on
            title match (e.g. ``"by jove"``, ``"jojo's"``).
        subreddit_blacklist: Lowercased set of subreddits to hard-skip.
        academia_core_subs: Lowercased set of subreddits where signal match
            alone is enough (no extra context required).
        context_required: Lowercased terms (e.g. ``"journal"``, ``"paper"``)
            required in ambiguous (non-core, non-blacklist) subreddits.
    """

    def __init__(self) -> None:
        raw = load_data_json("disambiguation.json")
        self.true_positive_signals: list[str] = [
            t.lower() for t in raw["true_positive_signals"]["terms"]
        ]
        self.false_positive_kill_terms: list[str] = [
            t.lower() for t in raw["false_positive_kill_terms"]["terms"]
        ]
        self.subreddit_blacklist: set[str] = {
            s.lower() for s in raw["subreddit_blacklist"]["subreddits"]
        }
        self.academia_core_subs: set[str] = {
            s.lower() for s in raw["academia_core_subreddits"]["subreddits"]
        }
        self.context_required: list[str] = [
            t.lower() for t in raw["context_required_for_ambiguous_subs"]["terms"]
        ]


@functools.cache
def _default_rules() -> DisambigRules:
    """Lazy-singleton accessor for the default rules.

    Cached so that the backfill loop calls :func:`is_in_scope` thousands
    of times without re-parsing ``data/disambiguation.json``. Tests that
    want fresh rules can construct :class:`DisambigRules` directly.
    """

    return DisambigRules()


def is_in_scope(
    thread: Thread, rules: DisambigRules | None = None
) -> tuple[bool, str]:
    """Decide whether ``thread`` is about JoVE-the-journal.

    Returns a ``(in_scope, reason)`` tuple. ``reason`` is always populated —
    it tells the dashboard / audit log exactly which gate fired:

    * ``"passed"`` — accepted, in scope.
    * ``"killed_by_title_term:<term>"`` — title contained a kill term.
    * ``"subreddit_blacklist"`` — subreddit is on the hard-skip list.
    * ``"no_positive_signal"`` — no journal-context signal in title, body,
      or top-3 comments.
    * ``"no_context_in_ambiguous_sub"`` — signal present but the subreddit
      isn't academia-core and the thread doesn't co-mention any
      ``context_required`` term ("journal", "paper", "peer review", ...).

    Args:
        thread: The fetched :class:`Thread` to evaluate.
        rules: Optional pre-constructed :class:`DisambigRules`. When ``None``
            the module-level lazy singleton is used. Passing rules
            explicitly is useful in tests for isolation.

    The function is **pure**: it does not log, mutate, or perform I/O.
    """

    if rules is None:
        rules = _default_rules()

    title_lower = thread.title.lower()

    # Stage 1 — title kill terms (cheapest, runs first).
    for kill_term in rules.false_positive_kill_terms:
        if kill_term in title_lower:
            return (False, f"killed_by_title_term:{kill_term}")

    # Stage 2 — subreddit blacklist.
    if thread.subreddit.lower() in rules.subreddit_blacklist:
        return (False, "subreddit_blacklist")

    # Stage 3 — positive signal required in title, body, or top-3 comments.
    haystack_parts: list[str] = [thread.title, thread.body or ""]
    haystack_parts.extend(c.body for c in thread.comments[:3])
    haystack = " ".join(haystack_parts).lower()

    if not any(signal in haystack for signal in rules.true_positive_signals):
        return (False, "no_positive_signal")

    # Stage 4 — subreddit-context allowance.
    # Academia-core subs accept on signal alone. Everything else needs a
    # co-mention of "journal" / "paper" / "peer review" / etc.
    if thread.subreddit.lower() not in rules.academia_core_subs:
        if not any(ctx in haystack for ctx in rules.context_required):
            return (False, "no_context_in_ambiguous_sub")

    return (True, "passed")
