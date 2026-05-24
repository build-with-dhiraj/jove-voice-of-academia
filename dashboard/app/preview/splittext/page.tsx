import type { Metadata } from "next";

import SplitTextHeader from "@/components/text-animations/SplitTextHeader";

export const metadata: Metadata = {
  title: "SplitText Preview · VoA v1.2",
  robots: { index: false, follow: false },
};

/**
 * Isolated preview route for the SplitText H1 reveal.
 *
 * v1.2/exp/split-title — this page is the perf-gate target. The hero
 * sits at the top, a static subtitle below it, then a calm slab of
 * dummy body copy so the page has realistic height for Lighthouse
 * to measure CLS / TBT against.
 *
 * No other v1.2 components on this page — keep the SplitText measurement clean.
 */
export default function SplitTextPreviewPage() {
  return (
    <main className="min-h-screen bg-background text-foreground">
      <section className="mx-auto flex max-w-5xl flex-col gap-6 px-6 pt-24 pb-16 sm:pt-32">
        <p className="font-mono text-xs uppercase tracking-[0.18em] text-muted-foreground">
          v1.2 · exp · split-title
        </p>
        <SplitTextHeader
          text="JoVE Research — Reddit sentiment landscape"
          tag="h1"
          splitType="chars"
          from={{ opacity: 0, y: 40 }}
          to={{ opacity: 1, y: 0 }}
          delay={50}
          duration={0.8}
          ease="power3.out"
          threshold={0.1}
          rootMargin="-100px"
          textAlign="left"
          className="text-4xl font-semibold leading-tight tracking-tight text-foreground sm:text-5xl md:text-6xl"
        />
        <p className="max-w-2xl text-base text-muted-foreground sm:text-lg">
          A weekly snapshot of how academics actually talk about JoVE on
          Reddit — curated, breadth-first, CEO-readable. This is the v1.2
          header treatment under evaluation.
        </p>
      </section>

      <section className="mx-auto max-w-5xl px-6 pb-32">
        <div className="prose prose-neutral max-w-none dark:prose-invert">
          <h2 className="text-2xl font-semibold tracking-tight">
            About this preview
          </h2>
          <p className="text-muted-foreground">
            The H1 above is the only animated element on this page. SplitText
            runs once per page load when the heading scrolls into view (the
            <code className="mx-1 rounded bg-muted px-1.5 py-0.5 text-sm">
              once: true
            </code>
            ScrollTrigger flag prevents repeat plays). On users with
            <code className="mx-1 rounded bg-muted px-1.5 py-0.5 text-sm">
              prefers-reduced-motion: reduce
            </code>
            set, the GSAP chunk is never loaded — the heading is rendered as
            static text with the identical typography.
          </p>
          <p className="text-muted-foreground">
            Everything below the hero is plain markup, intentionally calm, so
            Lighthouse can measure LCP / TBT / CLS against the animation
            cleanly without other v1.2 components in scope.
          </p>
          <h3 className="text-xl font-semibold tracking-tight">Acceptance gate</h3>
          <ul className="list-disc pl-6 text-muted-foreground marker:text-muted-foreground/60">
            <li>Desktop Performance ≥ 90, Mobile Performance ≥ 75</li>
            <li>LCP desktop ≤ 2.5s, mobile ≤ 3.5s</li>
            <li>TBT desktop ≤ 200ms, mobile ≤ 400ms</li>
            <li>Accessibility ≥ 90</li>
            <li>No CLS from the split (font-load gate active)</li>
          </ul>
        </div>
      </section>
    </main>
  );
}
