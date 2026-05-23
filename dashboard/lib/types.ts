/**
 * TypeScript types mirroring the pipeline's Pydantic models.
 *
 * Authoritative source: Voice of Academia — System Design (§ Data model — dashboard).
 * Keep these in sync with `pipeline/models.py` when the backend lands.
 */

export type Sentiment = "positive" | "neutral" | "negative";

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
  sentiment: Sentiment;
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
  applies_to_sentiments: Sentiment[];
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
