"use client";

import { ChevronUp } from "lucide-react";

import type { Sentiment, VoicePanelRow } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Sentiment-tinted tile face colors. Kept in lockstep with `SentimentBadge`
 * tokens (rose/emerald/slate/amber) so the dome reads as the same data
 * universe as the rest of the dashboard, just spatialised.
 *
 * Each variant carries:
 *  - `border`: left edge stripe (the dominant sentiment signal)
 *  - `bg`:      tile background gradient
 *  - `accent`:  metadata stripe color (handle + score)
 *  - `dot`:     placeholder tile dot color (for empty / repeated slots)
 */
const SENTIMENT_TILE: Record<
  Sentiment,
  { border: string; bg: string; accent: string; dot: string }
> = {
  positive: {
    border: "border-l-emerald-400",
    bg: "bg-gradient-to-br from-emerald-50 to-emerald-100/40 dark:from-emerald-950/70 dark:to-emerald-900/30",
    accent: "text-emerald-700 dark:text-emerald-300",
    dot: "bg-emerald-400/40",
  },
  neutral: {
    border: "border-l-slate-400",
    bg: "bg-gradient-to-br from-slate-50 to-slate-100/40 dark:from-slate-900/70 dark:to-slate-800/40",
    accent: "text-slate-700 dark:text-slate-300",
    dot: "bg-slate-400/40",
  },
  negative: {
    border: "border-l-rose-400",
    bg: "bg-gradient-to-br from-rose-50 to-rose-100/40 dark:from-rose-950/70 dark:to-rose-900/30",
    accent: "text-rose-700 dark:text-rose-300",
    dot: "bg-rose-400/40",
  },
  mixed: {
    border: "border-l-amber-400",
    bg: "bg-gradient-to-br from-amber-50 to-amber-100/40 dark:from-amber-950/70 dark:to-amber-900/30",
    accent: "text-amber-700 dark:text-amber-300",
    dot: "bg-amber-400/40",
  },
};

/**
 * Trim a quote to the first ~Nchars, then to the previous word boundary, and
 * append a real ellipsis. Returns the trimmed string. Pure — safe to call
 * during render.
 */
export function excerpt(body: string, max = 140): string {
  if (body.length <= max) return body;
  const cut = body.slice(0, max);
  const lastSpace = cut.lastIndexOf(" ");
  return (lastSpace > 60 ? cut.slice(0, lastSpace) : cut).trimEnd() + "…";
}

interface VoiceTileProps {
  voice: VoicePanelRow;
}

/**
 * A single voice-quote tile rendered on a face of the dome. Renders pure
 * text + a clickable anchor — no images, no SVG, no canvas. The 3D positioning
 * is owned by the parent `<VoiceDome>` (CSS transforms via `--offset-x/y`
 * custom props on the `.item` wrapper).
 *
 * Accessibility: the entire tile face is an `<a>` so keyboard navigation
 * works and the permalink is exposed to screen readers via `aria-label`.
 */
export function VoiceTile({ voice }: VoiceTileProps) {
  const tone = SENTIMENT_TILE[voice.sentiment];
  const text = excerpt(voice.body, 140);

  return (
    <a
      href={voice.permalink}
      target="_blank"
      rel="noopener noreferrer"
      draggable={false}
      onPointerDown={(e) => {
        // Let the dome's drag gesture absorb the pointer; only navigate on
        // a true click (no drag movement). Stopping propagation here would
        // break the dome spin.
        e.stopPropagation();
      }}
      onClick={(e) => {
        // Same — clicks bubble to the tile face which checks for drag.
        e.stopPropagation();
      }}
      aria-label={`Voice: ${voice.sentiment} — ${text} — by ${voice.author}, score ${voice.score}. Open on Reddit.`}
      className={cn(
        "absolute inset-0 flex flex-col justify-between gap-1 overflow-hidden rounded-[inherit]",
        "border-l-4 px-3 py-2.5",
        "text-foreground no-underline",
        "ring-1 ring-inset ring-black/5 dark:ring-white/5",
        "transition-transform duration-150 hover:scale-[1.02]",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring",
        tone.border,
        tone.bg,
      )}
    >
      <p
        className="text-[10.5px] leading-snug font-medium tracking-tight line-clamp-3"
        style={{
          // Help Safari render text crisply on rotated faces.
          WebkitFontSmoothing: "antialiased",
          textRendering: "optimizeLegibility",
        }}
      >
        {text}
      </p>
      <div
        className={cn(
          "flex items-center justify-between gap-2 text-[9px] font-mono tabular-nums",
          tone.accent,
        )}
      >
        <span className="truncate">u/{voice.author}</span>
        <span className="inline-flex items-center gap-0.5 shrink-0">
          <ChevronUp className="size-2.5" aria-hidden="true" />
          {voice.score}
        </span>
      </div>
    </a>
  );
}

interface VoicePlaceholderTileProps {
  sentiment: Sentiment;
}

/**
 * Empty-slot placeholder. The dome wants ~225 tiles; the dataset has 8 voices.
 * Rather than leaving gaps (which kill the sphere illusion), we render
 * sentiment-tinted dot placeholders. They participate in the 3D illusion
 * without competing with real content for attention.
 */
export function VoicePlaceholderTile({ sentiment }: VoicePlaceholderTileProps) {
  const tone = SENTIMENT_TILE[sentiment];
  return (
    <div
      aria-hidden="true"
      className={cn(
        "absolute inset-0 flex items-center justify-center rounded-[inherit]",
        "border-l-4",
        tone.border,
        tone.bg,
        "opacity-30",
      )}
    >
      <span className={cn("h-1.5 w-1.5 rounded-full", tone.dot)} />
    </div>
  );
}
