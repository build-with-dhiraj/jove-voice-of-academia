import { ArrowUpRight, ChevronUp, MessageSquareQuote } from "lucide-react";

import type { VoicePanelRow } from "@/lib/types";

import { SentimentBadge } from "./SentimentBadge";

function formatLongDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

/**
 * Extract subreddit slug from a Reddit permalink for compact attribution.
 * Returns `null` if the URL doesn't match the expected shape.
 *
 * Duplicated from VoiceCard.tsx by design — both are display-layer helpers
 * with the same one-line implementation and no shared util module exists
 * yet. Pulling them out into `lib/format.ts` is a 1-line factoring that the
 * plan calls out as "either is acceptable"; keeping them local here keeps
 * the v1.1 diff small.
 */
function subredditFromPermalink(permalink: string): string | null {
  const match = permalink.match(/\/r\/([^/]+)\//);
  return match ? `r/${match[1]}` : null;
}

interface InlineVoiceExcerptProps {
  card: VoicePanelRow;
}

/**
 * Lean inline variant of VoiceCard for use inside ThemeRow's expansion area.
 *
 * Visual contract: no card border, no card padding, single column. The
 * containing ThemeRow already provides a `bg-muted/30` panel — wrapping each
 * excerpt in another Card would feel cluttered when 3 are stacked. Instead,
 * each excerpt is a borderless blockquote with attribution underneath,
 * separated from siblings by a thin divider.
 *
 * Server component — no hooks, no client state. Renders once at build time.
 */
export function InlineVoiceExcerpt({ card }: InlineVoiceExcerptProps) {
  const subreddit = subredditFromPermalink(card.permalink);

  return (
    <article className="flex flex-col gap-2 py-3 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-center gap-2">
        <SentimentBadge sentiment={card.sentiment} />
        {card.product_attribution !== "out-of-scope" ? (
          <span className="inline-flex h-5 items-center rounded-full bg-background px-2 font-mono text-[10px] uppercase tracking-wide text-muted-foreground ring-1 ring-inset ring-border">
            {card.product_attribution}
          </span>
        ) : null}
      </div>

      <blockquote className="relative pl-6 text-sm leading-6 text-foreground">
        <MessageSquareQuote
          aria-hidden="true"
          className="absolute left-0 top-0.5 size-3.5 text-muted-foreground/60"
        />
        {card.body}
      </blockquote>

      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span className="font-mono">u/{card.author}</span>
          {subreddit ? <span className="font-mono">{subreddit}</span> : null}
          <span>{formatLongDate(card.created_utc)}</span>
          <span className="inline-flex items-center gap-1 font-mono tabular-nums">
            <ChevronUp className="size-3" aria-hidden="true" />
            {card.score}
          </span>
        </div>
        <a
          href={card.permalink}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-foreground underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
          aria-label="Open original Reddit comment in a new tab"
        >
          Open on Reddit
          <ArrowUpRight className="size-3.5" aria-hidden="true" />
        </a>
      </div>
    </article>
  );
}
