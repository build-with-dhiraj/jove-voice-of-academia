import { ArrowUpRight, ChevronUp, MessageSquareQuote } from "lucide-react";

import type { ThemeDef, VoicePanelRow } from "@/lib/types";
import { Card, CardContent } from "@/components/ui/card";

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
 */
function subredditFromPermalink(permalink: string): string | null {
  const match = permalink.match(/\/r\/([^/]+)\//);
  return match ? `r/${match[1]}` : null;
}

interface VoiceCardProps {
  card: VoicePanelRow;
  themeDef: ThemeDef | undefined;
}

/**
 * A single curated voice quote. Built as a Card with semantic blockquote at
 * the center, attribution metadata below, and an open-in-Reddit affordance.
 */
export function VoiceCard({ card, themeDef }: VoiceCardProps) {
  const subreddit = subredditFromPermalink(card.permalink);
  const themeLabel = themeDef?.label ?? card.theme;

  return (
    <Card className="gap-3">
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <SentimentBadge sentiment={card.sentiment} />
          {themeLabel ? (
            <span className="inline-flex h-5 items-center rounded-full bg-muted px-2 text-xs font-medium text-muted-foreground ring-1 ring-inset ring-border">
              {themeLabel}
            </span>
          ) : null}
          {card.product_attribution !== "out-of-scope" ? (
            <span className="inline-flex h-5 items-center rounded-full bg-muted px-2 font-mono text-[10px] uppercase tracking-wide text-muted-foreground ring-1 ring-inset ring-border">
              {card.product_attribution}
            </span>
          ) : null}
        </div>

        <blockquote className="relative pl-6 text-[15px] leading-7 text-foreground">
          <MessageSquareQuote
            aria-hidden="true"
            className="absolute left-0 top-1 size-4 text-muted-foreground/60"
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
            aria-label="Open on Reddit (opens in new tab)"
          >
            Open on Reddit
            <ArrowUpRight className="size-3.5" aria-hidden="true" />
          </a>
        </div>

        {card.curator_note ? (
          <div className="border-t border-border pt-3 text-xs italic leading-relaxed text-muted-foreground">
            <span className="font-medium not-italic uppercase tracking-wide">
              Curator note &middot;
            </span>{" "}
            {card.curator_note}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
