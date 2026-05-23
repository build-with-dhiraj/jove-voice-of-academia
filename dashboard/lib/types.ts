/**
 * TypeScript types mirroring the pipeline's Pydantic models.
 *
 * Authoritative source: Voice of Academia — System Design (§ Data model — dashboard).
 * Keep these in sync with `pipeline/models.py` when the backend lands.
 */

/**
 * Per-theme sentiment used in the frequency table.
 *
 * Always 3-value: a comment carrying a `mixed` overall sentiment is
 * decomposed in the pipeline into one row per `(theme, polarity)`
 * (spec D1), so the frequency table never sees `mixed`.
 */
export type ThemeSentiment = "positive" | "neutral" | "negative";

/**
 * Overall comment sentiment used in the Voice of Academia panel.
 *
 * 4-value (includes `mixed`) because some genuinely valuable Voice quotes
 * carry competing claims (e.g. *"the videos are great but the fees are
 * awful"*). Forcing such comments into a single polarity would collapse
 * real signal — and the panel exists exactly to surface nuance. Widened
 * from 3-value at Phase 7 in lockstep with `VoicePanelRow.sentiment` in
 * `pipeline/models.py` (code-review item 9.4).
 */
export type Sentiment = ThemeSentiment | "mixed";

export type ProductAttribution =
  | "journal"
  | "eoe"
  | "visualize"
  | "research-general"
  | "other-journal"
  | "out-of-scope";

/**
 * A single (sentiment × theme × journal) row in the frequency table.
 * Ranking is breadth-first → depth → recency (spec D4).
 */
export interface FrequencyRow {
  sentiment: ThemeSentiment;
  theme: string; // canonical theme id from data/taxonomy.json
  journal: string; // "JoVE" in v1; reserved for v2 expansion
  thread_count: number;
  comment_count: number;
  upvote_sum: number;
  reply_count: number;
  last_comment_at: string; // ISO datetime
  newest_thread_at: string; // ISO datetime
  thread_urls: string[];
  rank: number; // 1-indexed within (sentiment, journal) bucket
}

/**
 * A single curated voice quote in the Voice of Academia panel.
 * D's weekly curation promotes candidates from vault into this list.
 *
 * Note `sentiment` is 4-value (includes `mixed`) — Voice quotes
 * preserve the source comment's overall polarity faithfully.
 */
export interface VoicePanelRow {
  sentiment: Sentiment;
  theme: string | null;
  product_attribution: ProductAttribution;
  author: string;
  permalink: string;
  body: string;
  score: number;
  created_utc: string; // ISO datetime
  curated_at: string; // ISO datetime
  curator_note: string | null;
}

/**
 * The single aggregate document produced by the pipeline each week.
 * Read by the dashboard at build time (ISR), shipped as static HTML.
 */
export interface Aggregate {
  generated_at: string; // ISO datetime
  time_horizon: string; // e.g. "2010-01-01"
  total_threads: number;
  total_comments: number;
  rows: FrequencyRow[];
  voice_published: VoicePanelRow[];
}

/**
 * Theme metadata loaded from data/taxonomy.json. Used to render human-readable
 * labels alongside the FrequencyRow.theme id.
 */
export interface ThemeDef {
  id: string;
  label: string;
  description: string;
  applies_to_sentiments: ThemeSentiment[];
  observed_sentiment_skew?: string;
  example_phrasings?: string[];
}

export interface Taxonomy {
  version: string;
  themes: ThemeDef[];
  emerging_bucket: {
    id: string;
    label: string;
    description: string;
  };
}
