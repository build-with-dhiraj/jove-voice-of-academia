import { ExternalLink } from "lucide-react";

import type { FrequencyRow, ThemeDef, VoicePanelRow } from "@/lib/types";

import { InlineVoiceExcerpt } from "./InlineVoiceExcerpt";

/** Max curated voice excerpts rendered per theme inside the expansion. */
const MAX_VOICES_PER_THEME = 3;

/**
 * Render an ISO datetime as a compact short-form date for CEO-skim scanning.
 * Server-side only — runs at build time, so no hydration mismatch.
 */
function formatShortDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

/**
 * Display the human-readable label for a theme id, falling back to the id
 * itself if we don't know it (defensive — should not happen if the pipeline
 * uses the locked taxonomy).
 */
function themeLabel(themeId: string, themeDef: ThemeDef | undefined): string {
  return themeDef?.label ?? themeId;
}

interface ThemeRowProps {
  row: FrequencyRow;
  themeDef: ThemeDef | undefined;
  /**
   * Voices already filtered to this theme by the parent FrequencyTable.
   * Empty array is valid (renders the empty-state copy).
   */
  voices: VoicePanelRow[];
}

/**
 * A single row of the FrequencyTable, expandable inline (native <details>)
 * to reveal: (1) up to 3 curated voice excerpts for this theme, (2) supporting
 * thread URLs as a secondary section.
 *
 * No client-side JS required — the disclosure is a native <details>/<summary>.
 */
export function ThemeRow({ row, themeDef, voices }: ThemeRowProps) {
  const label = themeLabel(row.theme, themeDef);
  // Cap inline excerpts at MAX_VOICES_PER_THEME so a high-volume theme can't
  // explode a row vertically — D's curation already orders by editorial
  // preference, so the first N are the strongest.
  const topVoices = voices.slice(0, MAX_VOICES_PER_THEME);

  return (
    <details className="group/row border-b border-border last:border-b-0">
      <summary className="grid cursor-pointer list-none grid-cols-[1.5fr_repeat(4,minmax(0,0.6fr))_repeat(2,minmax(0,0.9fr))_auto] items-center gap-3 px-3 py-3 transition-colors hover:bg-muted/40 [&::-webkit-details-marker]:hidden">
        <div className="flex min-w-0 flex-col">
          <span className="whitespace-normal break-words text-sm font-medium text-foreground">
            {label}
          </span>
        </div>
        <span className="text-right font-mono text-sm tabular-nums text-foreground">
          {row.thread_count}
        </span>
        <span className="text-right font-mono text-sm tabular-nums text-muted-foreground">
          {row.comment_count}
        </span>
        <span className="text-right font-mono text-sm tabular-nums text-muted-foreground">
          {row.upvote_sum}
        </span>
        <span className="text-right font-mono text-sm tabular-nums text-muted-foreground">
          {row.reply_count}
        </span>
        <span className="text-right font-mono text-xs tabular-nums text-muted-foreground">
          {formatShortDate(row.last_comment_at)}
        </span>
        <span className="text-right font-mono text-xs tabular-nums text-muted-foreground">
          {formatShortDate(row.newest_thread_at)}
        </span>
        <svg
          aria-hidden="true"
          viewBox="0 0 12 12"
          className="size-3 shrink-0 text-muted-foreground transition-transform group-open/row:rotate-90"
        >
          <path
            d="M4 2l4 4-4 4"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </summary>

      <div className="flex flex-col gap-5 bg-muted/30 px-3 py-4 sm:px-5 sm:py-5">
        {/* Section 1 — top voice excerpts (or empty state) */}
        <div className="flex flex-col gap-2">
          <div className="flex items-baseline justify-between gap-3">
            <p className="font-mono text-[10px] uppercase tracking-wide text-muted-foreground">
              What people said
            </p>
            {voices.length > 0 ? (
              <p className="font-mono text-[10px] tabular-nums text-muted-foreground">
                {topVoices.length} of {voices.length} curated
              </p>
            ) : null}
          </div>
          {topVoices.length > 0 ? (
            <div className="divide-y divide-border rounded-lg bg-background/60 px-4 ring-1 ring-inset ring-border">
              {topVoices.map((voice) => (
                <InlineVoiceExcerpt key={voice.permalink} card={voice} />
              ))}
            </div>
          ) : (
            <p className="rounded-lg bg-background/60 px-4 py-3 text-xs leading-relaxed text-muted-foreground ring-1 ring-inset ring-border">
              No curated voices for this theme yet. Approve candidates via{" "}
              <code className="rounded bg-muted px-1 py-0.5 font-mono text-[11px] text-foreground">
                voice-curate
              </code>{" "}
              to populate, or scan the supporting threads below.
            </p>
          )}
        </div>

        {/* Section 2 — supporting threads, demoted to secondary */}
        <div className="flex flex-col gap-2 border-t border-border/60 pt-4">
          <p className="font-mono text-[10px] uppercase tracking-wide text-muted-foreground">
            Supporting Reddit threads ({row.thread_urls.length})
          </p>
          <ul className="space-y-1">
            {row.thread_urls.map((url) => (
              <li key={url} className="text-sm">
                <a
                  href={url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex max-w-full items-center gap-1.5 text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
                >
                  <ExternalLink className="size-3.5 shrink-0" />
                  <span className="truncate font-mono text-xs">
                    {url.replace(/^https:\/\/(www\.)?reddit\.com/, "")}
                  </span>
                </a>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </details>
  );
}
