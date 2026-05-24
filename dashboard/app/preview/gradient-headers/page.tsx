import type { Metadata } from "next";

import { GradientText } from "@/components/ui/GradientText";

/**
 * Isolation preview for the GradientText primitive
 * (v1.2/exp/gradient-buckets).
 *
 * This route exists to:
 * 1. Demonstrate the three sentiment-tinted bucket headers side-by-side
 *    against realistic body-copy density.
 * 2. Give the per-component Lighthouse gate a target URL that is NOT the
 *    main page — so we measure the cost of THIS component alone before
 *    deciding KEEP / DROP.
 *
 * Not linked from the main nav. Not indexed. Lives under /preview/* by
 * convention with the v1.2 loop (see plan §loop architecture).
 */
export const metadata: Metadata = {
  title: "GradientText preview — VoA v1.2",
  robots: { index: false, follow: false },
};

// Realistic placeholder copy so the preview shows the gradient header
// reading against body text density, not floating in whitespace.
const SAMPLE_BODY =
  "Below each bucket header, the frequency table lists every theme ranked breadth-first within this sentiment — distinct threads, distinct comments, most recent activity. The gradient header is the surface divider that tells you which bucket you are reading.";

const BUCKETS = [
  {
    sentiment: "positive" as const,
    heading: "What academia values about JoVE",
    sub: "Emerald → teal → cyan gradient — fresh, scholarly, additive tone.",
  },
  {
    sentiment: "neutral" as const,
    heading: "What academia is debating about JoVE",
    sub: "Slate → blue → indigo gradient — cool, deliberative, neutral tone.",
  },
  {
    sentiment: "negative" as const,
    heading: "What academia complains about JoVE",
    sub: "Rose → coral → pink gradient — warm without crimson, composes with the Antigravity hero's #FF9FFC.",
  },
];

export default function GradientHeadersPreviewPage() {
  return (
    <main className="mx-auto flex w-full max-w-4xl flex-col gap-12 px-6 py-12 md:px-10 md:py-16">
      <header className="flex flex-col gap-2 border-b border-border pb-8">
        <p className="font-mono text-xs uppercase tracking-[0.18em] text-muted-foreground">
          v1.2 / exp / gradient-buckets
        </p>
        <h1 className="font-heading text-2xl font-semibold tracking-tight text-foreground md:text-3xl">
          Sentiment-tinted bucket headers — isolation preview
        </h1>
        <p className="max-w-2xl text-sm text-muted-foreground">
          Three CSS gradients clipped to glyph shapes via{" "}
          <code className="font-mono text-xs">background-clip: text</code>. No
          JS, no animation, no canvas — pure custom properties from{" "}
          <code className="font-mono text-xs">app/globals.css</code>.
        </p>
      </header>

      {BUCKETS.map(({ sentiment, heading, sub }) => (
        <section
          key={sentiment}
          aria-labelledby={`bucket-${sentiment}-heading`}
          className="flex flex-col gap-4"
        >
          <GradientText
            sentiment={sentiment}
            as="h2"
            className="font-heading text-3xl font-bold leading-tight tracking-tight md:text-4xl"
            // The `as="h2"` prop forwards to a real <h2>, so we attach the
            // id for the aria-labelledby relationship via the className flow.
            // (React forwards id through {...rest} on `as` polymorphism;
            // see GradientText source — we keep it simple here by not
            // wrapping in another heading element.)
          >
            <span id={`bucket-${sentiment}-heading`}>{heading}</span>
          </GradientText>
          <p className="text-xs font-mono uppercase tracking-wide text-muted-foreground">
            {sentiment} · {sub}
          </p>
          <p className="max-w-2xl text-base leading-relaxed text-foreground/80">
            {SAMPLE_BODY}
          </p>
        </section>
      ))}

      <footer className="flex flex-col gap-2 border-t border-border pt-6 text-xs text-muted-foreground">
        <p>
          Composition target:{" "}
          <code className="font-mono">
            dashboard/components/FrequencyTable.tsx
          </code>{" "}
          lines 119–129 — current bucket header bar is{" "}
          <code className="font-mono">{`<SentimentBadge> + <span>`}</code>; the
          integration phase wraps the description text in{" "}
          <code className="font-mono">{`<GradientText sentiment={sentiment}>`}</code>
          .
        </p>
      </footer>
    </main>
  );
}
