import type {
  FrequencyRow,
  Taxonomy,
  ThemeSentiment,
  VoicePanelRow,
} from "@/lib/types";

import { SentimentBadge } from "./SentimentBadge";
import { ThemeRow } from "./ThemeRow";
import { indexThemes } from "@/lib/data";

// The frequency table only ever sees 3-value sentiment — per spec D1 a
// "mixed" comment is decomposed into one row per (theme, polarity) at
// aggregate-build time. ThemeSentiment is the right type here.
const SENTIMENT_ORDER: ThemeSentiment[] = ["positive", "neutral", "negative"];

const SENTIMENT_DESCRIPTION: Record<ThemeSentiment, string> = {
  positive: "What academia values about JoVE",
  neutral: "What academia is debating about JoVE",
  negative: "What academia complains about JoVE",
};

/**
 * Group rows by sentiment, preserving rank order within each group.
 * Returns sentiments in the canonical positive → neutral → negative order.
 */
function groupBySentiment(
  rows: FrequencyRow[],
): Map<ThemeSentiment, FrequencyRow[]> {
  const groups = new Map<ThemeSentiment, FrequencyRow[]>();
  for (const s of SENTIMENT_ORDER) groups.set(s, []);
  for (const row of rows) {
    const bucket = groups.get(row.sentiment);
    if (bucket) bucket.push(row);
  }
  // Within each sentiment, sort ascending by rank (lower = higher breadth).
  for (const bucket of groups.values()) {
    bucket.sort((a, b) => a.rank - b.rank);
  }
  return groups;
}

/**
 * Bucket curated voices by their `theme` id so each ThemeRow can receive its
 * own narrow subset without re-scanning the full voice list. Voices with a
 * null theme are skipped (they belong to the editor-picks panel only).
 */
function indexVoicesByTheme(
  voices: VoicePanelRow[],
): Map<string, VoicePanelRow[]> {
  const map = new Map<string, VoicePanelRow[]>();
  for (const v of voices) {
    if (!v.theme) continue;
    const bucket = map.get(v.theme);
    if (bucket) {
      bucket.push(v);
    } else {
      map.set(v.theme, [v]);
    }
  }
  return map;
}

interface FrequencyTableProps {
  rows: FrequencyRow[];
  taxonomy: Taxonomy;
  /**
   * All curated voices — the FrequencyTable filters them per theme and
   * passes the matching subset to each ThemeRow. v1.1: this is how the
   * CEO sees WHAT people said about each theme without scrolling away.
   */
  voices: VoicePanelRow[];
}

/**
 * The CEO's primary scanning surface: every (sentiment × theme) row in the
 * corpus, breadth-first ranked, grouped by sentiment. Each row expands inline
 * to show the full theme description, the top curated voice excerpts for
 * that theme, and supporting Reddit threads.
 *
 * Server component. Static at build time (ISR — see app/page.tsx).
 */
export function FrequencyTable({
  rows,
  taxonomy,
  voices,
}: FrequencyTableProps) {
  const groups = groupBySentiment(rows);
  const themeIndex = indexThemes(taxonomy);
  const voicesByTheme = indexVoicesByTheme(voices);

  return (
    <section
      aria-labelledby="frequency-table-heading"
      className="flex flex-col gap-8"
    >
      <header className="flex flex-col gap-1">
        <h2
          id="frequency-table-heading"
          className="font-heading text-2xl font-semibold tracking-tight text-foreground"
        >
          Frequency table
        </h2>
        <p className="text-sm text-muted-foreground">
          Themes ranked breadth-first within each sentiment bucket: distinct
          threads &rarr; distinct comments &rarr; most recent comment. Click
          any row to see what people actually said.
        </p>
      </header>

      {SENTIMENT_ORDER.map((sentiment) => {
        const bucketRows = groups.get(sentiment) ?? [];
        if (bucketRows.length === 0) return null;
        return (
          <div
            key={sentiment}
            className="overflow-hidden rounded-xl bg-card ring-1 ring-foreground/10"
          >
            <div className="flex items-center justify-between border-b border-border bg-muted/40 px-3 py-3">
              <div className="flex items-center gap-3">
                <SentimentBadge sentiment={sentiment} />
                <span className="text-sm text-muted-foreground">
                  {SENTIMENT_DESCRIPTION[sentiment]}
                </span>
              </div>
              <span className="font-mono text-xs tabular-nums text-muted-foreground">
                {bucketRows.length} theme{bucketRows.length === 1 ? "" : "s"}
              </span>
            </div>

            {/* Column header — grid layout matches ThemeRow exactly */}
            <div
              role="row"
              className="grid grid-cols-[1.5fr_repeat(4,minmax(0,0.6fr))_repeat(2,minmax(0,0.9fr))_auto] items-center gap-3 border-b border-border bg-muted/20 px-3 py-2 text-xs font-medium uppercase tracking-wide text-muted-foreground"
            >
              <span>Theme</span>
              <span className="text-right">Threads</span>
              <span className="text-right">Comments</span>
              <span className="text-right">Upvotes</span>
              <span className="text-right">Replies</span>
              <span className="text-right">Last comment</span>
              <span className="text-right">Newest thread</span>
              <span className="size-3" aria-hidden="true" />
            </div>

            <div role="rowgroup">
              {bucketRows.map((row) => (
                <ThemeRow
                  key={`${row.sentiment}-${row.theme}-${row.journal}`}
                  row={row}
                  themeDef={themeIndex.get(row.theme)}
                  voices={voicesByTheme.get(row.theme) ?? []}
                />
              ))}
            </div>
          </div>
        );
      })}
    </section>
  );
}
