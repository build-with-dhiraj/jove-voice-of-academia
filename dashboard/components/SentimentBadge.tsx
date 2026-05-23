import { cn } from "@/lib/utils";
import type { Sentiment } from "@/lib/types";

/**
 * Sentiment-coded badge. Uses semantic emerald / slate / rose tones rather
 * than the global Badge variants, so the dashboard's sentiment language is
 * stable across themes and easy to scan at a glance.
 *
 * Color choices are intentionally muted (50/700 stops) to read as
 * "informational" rather than "alarm" — this is a CEO-shareable artifact,
 * not an incident dashboard.
 */
const SENTIMENT_CLASSES: Record<Sentiment, string> = {
  positive:
    "bg-emerald-50 text-emerald-700 ring-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-300 dark:ring-emerald-900/60",
  neutral:
    "bg-slate-50 text-slate-700 ring-slate-200 dark:bg-slate-900/40 dark:text-slate-300 dark:ring-slate-800/60",
  negative:
    "bg-rose-50 text-rose-700 ring-rose-200 dark:bg-rose-950/40 dark:text-rose-300 dark:ring-rose-900/60",
  // ``mixed`` only appears on Voice panel rows (per Phase 7 widening of
  // Sentiment to 4-value). Amber reads as "competing signals" without
  // implying alarm — matches the voice-panel design intent of surfacing
  // nuance rather than escalating it.
  mixed:
    "bg-amber-50 text-amber-700 ring-amber-200 dark:bg-amber-950/40 dark:text-amber-300 dark:ring-amber-900/60",
};

const SENTIMENT_LABELS: Record<Sentiment, string> = {
  positive: "Positive",
  neutral: "Neutral",
  negative: "Negative",
  mixed: "Mixed",
};

export function SentimentBadge({
  sentiment,
  className,
}: {
  sentiment: Sentiment;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex h-5 items-center rounded-full px-2 text-xs font-medium uppercase tracking-wide ring-1 ring-inset",
        SENTIMENT_CLASSES[sentiment],
        className,
      )}
      aria-label={`Sentiment: ${SENTIMENT_LABELS[sentiment]}`}
    >
      {SENTIMENT_LABELS[sentiment]}
    </span>
  );
}
