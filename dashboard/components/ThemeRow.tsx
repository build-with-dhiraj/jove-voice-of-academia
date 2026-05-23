import { ExternalLink } from "lucide-react";

import type { FrequencyRow, ThemeDef } from "@/lib/types";

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

function themeDescription(themeDef: ThemeDef | undefined): string | null {
  return themeDef?.description ?? null;
}

interface ThemeRowProps {
  row: FrequencyRow;
  themeDef: ThemeDef | undefined;
}

/**
 * A single row of the FrequencyTable, expandable inline (native <details>)
 * to reveal supporting thread URLs. No client-side JS required.
 */
export function ThemeRow({ row, themeDef }: ThemeRowProps) {
  const label = themeLabel(row.theme, themeDef);
  const description = themeDescription(themeDef);

  return (
    <details className="group/row border-b border-border last:border-b-0">
      <summary className="grid cursor-pointer list-none grid-cols-[1.5fr_repeat(4,minmax(0,0.6fr))_repeat(2,minmax(0,0.9fr))_auto] items-center gap-3 px-3 py-3 transition-colors hover:bg-muted/40 [&::-webkit-details-marker]:hidden">
        <div className="flex min-w-0 flex-col">
          <span className="truncate text-sm font-medium text-foreground">
            {label}
          </span>
          {description ? (
            <span className="line-clamp-1 text-xs text-muted-foreground">
              {description}
            </span>
          ) : null}
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
      <div className="bg-muted/30 px-3 pb-4 pt-1">
        <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Supporting threads ({row.thread_urls.length})
        </p>
        <ul className="space-y-1.5">
          {row.thread_urls.map((url) => (
            <li key={url} className="text-sm">
              <a
                href={url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 text-foreground underline-offset-4 hover:underline"
              >
                <ExternalLink className="size-3.5 text-muted-foreground" />
                <span className="truncate font-mono text-xs">
                  {url.replace(/^https:\/\/(www\.)?reddit\.com/, "")}
                </span>
              </a>
            </li>
          ))}
        </ul>
      </div>
    </details>
  );
}
