import { CountUp } from "@/components/ui/CountUp";

/**
 * Isolated preview for the CountUp primitive.
 *
 * Mirrors the <dl> layout of the main page header (app/page.tsx:54-87) so the
 * visual + perf gate runs against the exact composition the integration phase
 * will land. Generated / Backfill horizon stay static (CountUp is for numeric
 * values, not dates). Threads + Comments are animated.
 *
 * Sample values are hard-coded — the real numbers in v1.1 are 47 threads /
 * 812 comments, per the plan brief.
 */
export const dynamic = "force-static";

const SAMPLE_GENERATED = "May 19, 2026, 04:00 AM UTC";
const SAMPLE_HORIZON = "January 2010 → present";
const SAMPLE_THREADS = 47;
const SAMPLE_COMMENTS = 812;

export default function CountUpPreviewPage() {
  return (
    <main className="mx-auto flex w-full max-w-6xl flex-col gap-10 px-6 py-10 md:px-10 md:py-16">
      <header className="flex flex-col gap-4 border-b border-border pb-8">
        <div className="flex flex-col gap-2">
          <p className="font-mono text-xs uppercase tracking-[0.18em] text-muted-foreground">
            Preview &mdash; CountUp
          </p>
          <h1 className="font-heading text-3xl font-semibold tracking-tight text-foreground md:text-4xl">
            Animated stat counters &mdash; isolated preview
          </h1>
          <p className="max-w-2xl text-base leading-relaxed text-muted-foreground">
            Mirrors the main page header layout. Threads and Comments animate
            from 0 to target on viewport entry; Generated and Backfill horizon
            stay static. Reduce-motion users see final values immediately.
          </p>
        </div>

        <dl className="grid grid-cols-2 gap-x-6 gap-y-3 pt-4 text-sm sm:grid-cols-4">
          <div className="flex flex-col gap-1">
            <dt className="font-mono text-[10px] uppercase tracking-wide text-muted-foreground">
              Generated
            </dt>
            <dd className="font-mono tabular-nums text-foreground">
              {SAMPLE_GENERATED}
            </dd>
          </div>
          <div className="flex flex-col gap-1">
            <dt className="font-mono text-[10px] uppercase tracking-wide text-muted-foreground">
              Backfill horizon
            </dt>
            <dd className="font-mono tabular-nums text-foreground">
              {SAMPLE_HORIZON}
            </dd>
          </div>
          <div className="flex flex-col gap-1">
            <dt className="font-mono text-[10px] uppercase tracking-wide text-muted-foreground">
              Threads
            </dt>
            <dd className="font-mono tabular-nums text-foreground">
              <CountUp value={SAMPLE_THREADS} />
            </dd>
          </div>
          <div className="flex flex-col gap-1">
            <dt className="font-mono text-[10px] uppercase tracking-wide text-muted-foreground">
              Comments
            </dt>
            <dd className="font-mono tabular-nums text-foreground">
              <CountUp value={SAMPLE_COMMENTS} />
            </dd>
          </div>
        </dl>
      </header>

      <section className="flex flex-col gap-4">
        <h2 className="font-mono text-xs uppercase tracking-[0.18em] text-muted-foreground">
          Below-the-fold (verifies IntersectionObserver gating)
        </h2>
        <div className="flex min-h-[120vh] flex-col items-start justify-end gap-4 rounded-lg border border-dashed border-border p-6">
          <p className="text-sm text-muted-foreground">
            The counter below sits below the initial viewport. It should remain
            at 0 (or its SSR final value, then reset to 0 on hydrate) until
            scrolled into view, then animate.
          </p>
          <p className="font-mono text-5xl font-semibold tabular-nums text-foreground">
            <CountUp value={12345} />
          </p>
        </div>
      </section>
    </main>
  );
}
