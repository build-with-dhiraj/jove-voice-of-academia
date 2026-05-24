import Link from "next/link";
import { ArrowRight } from "lucide-react";

import { FrequencyTable } from "@/components/FrequencyTable";
import { VoicePanel } from "@/components/VoicePanel";
import { AntigravityBg } from "@/components/hero/AntigravityBg";
import SplitTextHeader from "@/components/text-animations/SplitTextHeader";
import { CountUp } from "@/components/ui/CountUp";
import { Separator } from "@/components/ui/separator";
import { loadLatest, loadTaxonomy } from "@/lib/data";

/**
 * Static / ISR — page rebuilds when Vercel redeploys, which the cron triggers
 * via a git commit to `dashboard/data/latest.json` each Sunday.
 *
 * `revalidate = false` means "build once per deploy, no time-based revalidate"
 * — the canonical refresh signal is the cron-pushed commit, not a TTL.
 */
export const revalidate = false;

function formatTimestamp(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
    timeZoneName: "short",
  });
}

function formatHorizon(horizon: string): string {
  const d = new Date(horizon);
  return d.toLocaleDateString("en-US", { year: "numeric", month: "long" });
}

export default async function HomePage() {
  // Parallel — these reads are independent. Avoids a serial waterfall.
  const [agg, taxonomy] = await Promise.all([loadLatest(), loadTaxonomy()]);

  return (
    <main className="flex w-full flex-col gap-10 pb-10 md:pb-16">
      <section className="relative isolate min-h-[480px] overflow-hidden border-b border-border">
        <AntigravityBg className="absolute inset-0 z-0" />
        <header className="relative z-10 mx-auto flex w-full max-w-6xl flex-col gap-4 px-6 pb-10 pt-16 md:px-10 md:pb-12 md:pt-24">
          <div className="flex flex-col gap-2">
            <p className="font-mono text-xs uppercase tracking-[0.18em] text-muted-foreground">
              Voice of Academia
            </p>
            <div className="min-h-[90px] sm:min-h-[110px] md:min-h-[140px]">
              <SplitTextHeader
                text="JoVE Research — Reddit sentiment landscape"
                tag="h1"
                className="font-heading text-3xl font-semibold tracking-tight text-foreground md:text-4xl"
              />
            </div>
            <p className="max-w-2xl text-base leading-relaxed text-muted-foreground">
              What academia says on Reddit about publishing in the JoVE Research
              line. Threads tagged for sentiment and theme, ranked breadth-first,
              with a separately-curated voice panel.
            </p>
          </div>

          <dl className="grid grid-cols-2 gap-x-6 gap-y-3 pt-4 text-sm sm:grid-cols-4">
            <div className="flex flex-col gap-1">
              <dt className="font-mono text-[10px] uppercase tracking-wide text-muted-foreground">
                Generated
              </dt>
              <dd className="font-mono tabular-nums text-foreground">
                {formatTimestamp(agg.generated_at)}
              </dd>
            </div>
            <div className="flex flex-col gap-1">
              <dt className="font-mono text-[10px] uppercase tracking-wide text-muted-foreground">
                Backfill horizon
              </dt>
              <dd className="font-mono tabular-nums text-foreground">
                {formatHorizon(agg.time_horizon)} &rarr; present
              </dd>
            </div>
            <div className="flex flex-col gap-1">
              <dt className="font-mono text-[10px] uppercase tracking-wide text-muted-foreground">
                Threads
              </dt>
              <dd className="font-mono tabular-nums text-foreground">
                <CountUp value={agg.total_threads} />
              </dd>
            </div>
            <div className="flex flex-col gap-1">
              <dt className="font-mono text-[10px] uppercase tracking-wide text-muted-foreground">
                Comments
              </dt>
              <dd className="font-mono tabular-nums text-foreground">
                <CountUp value={agg.total_comments} />
              </dd>
            </div>
          </dl>

          <Link
            href="/voices"
            className="mt-8 inline-flex items-center gap-2 self-start rounded-full bg-foreground px-6 py-3 text-background transition-opacity hover:opacity-90"
          >
            <span>Browse curated voices</span>
            <ArrowRight className="h-4 w-4" aria-hidden="true" />
          </Link>
        </header>
      </section>

      <div className="mx-auto flex w-full max-w-6xl flex-col gap-10 px-6 md:px-10">

      <FrequencyTable
        rows={agg.rows}
        taxonomy={taxonomy}
        voices={agg.voice_published}
      />

      <Separator />

      <VoicePanel cards={agg.voice_published} taxonomy={taxonomy} />

        <footer className="flex flex-col gap-3 border-t border-border pt-6 text-xs text-muted-foreground">
          <p>
            Built for D Rage&apos;s pre-join research at JoVE. Source comments
            remain on Reddit and link back to their original threads &mdash;
            attribution and back-linking per Reddit&apos;s Responsible Builder
            Policy. No content is rehosted; no model is fine-tuned on this data.
          </p>
          <p>
            <a
              href="https://github.com/in-quiz-ition/jove-voice-of-academia"
              target="_blank"
              rel="noopener noreferrer"
              className="font-mono text-foreground underline-offset-4 hover:underline"
            >
              github.com/in-quiz-ition/jove-voice-of-academia
            </a>
          </p>
        </footer>
      </div>
    </main>
  );
}
