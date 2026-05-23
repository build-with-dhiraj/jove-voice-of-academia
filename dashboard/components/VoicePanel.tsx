import type { Taxonomy, VoicePanelRow } from "@/lib/types";

import { VoiceCard } from "./VoiceCard";
import { indexThemes } from "@/lib/data";

interface VoicePanelProps {
  cards: VoicePanelRow[];
  taxonomy: Taxonomy;
}

/**
 * D's curated Voice of Academia quotes. Distinct from the frequency table:
 * this is a hand-picked panel where each quote stands on its own merit.
 *
 * Server component. Static at build time.
 */
export function VoicePanel({ cards, taxonomy }: VoicePanelProps) {
  const themeIndex = indexThemes(taxonomy);

  return (
    <section
      aria-labelledby="voice-panel-heading"
      className="flex flex-col gap-6"
    >
      <header className="flex flex-col gap-1">
        <h2
          id="voice-panel-heading"
          className="font-heading text-2xl font-semibold tracking-tight text-foreground"
        >
          Editor&apos;s picks
        </h2>
        <p className="max-w-2xl text-sm text-muted-foreground">
          The same curated quotes shown per-theme above, here grouped as a
          standalone reading panel. Each one was flagged by the pipeline
          against a journalist-grade rubric and then human-approved &mdash;
          verbatim, no paraphrasing, no aggregation.
        </p>
      </header>

      {cards.length === 0 ? (
        <p className="rounded-xl bg-muted/40 px-4 py-6 text-sm text-muted-foreground ring-1 ring-foreground/10">
          No voices have been curated yet. Run the weekly curation CLI to
          promote candidates into the published panel.
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {cards.map((card) => (
            <VoiceCard
              key={card.permalink}
              card={card}
              themeDef={card.theme ? themeIndex.get(card.theme) : undefined}
            />
          ))}
        </div>
      )}
    </section>
  );
}
