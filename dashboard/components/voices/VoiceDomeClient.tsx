"use client";

import dynamic from "next/dynamic";

import type { VoicePanelRow } from "@/lib/types";

/**
 * Thin client boundary that owns the dynamic import of `VoiceDome`. Next.js
 * 16 forbids `ssr: false` inside server components, so the route imports
 * this wrapper instead and the wrapper handles the lazy mount + skeleton.
 *
 * Keeping this file tiny is intentional — anything heavy here would defeat
 * the point of dynamic-importing the dome.
 */

const VoiceDome = dynamic(
  () => import("./VoiceDome").then((m) => m.default),
  {
    ssr: false,
    loading: () => (
      <div className="relative h-[460px] w-full overflow-hidden rounded-3xl bg-gradient-to-br from-muted/30 to-muted/10 md:h-[680px]">
        <div className="absolute inset-0 animate-pulse bg-gradient-to-tr from-transparent via-foreground/[0.03] to-transparent" />
        <div className="absolute inset-x-0 bottom-6 text-center font-mono text-xs text-muted-foreground">
          Loading voice constellation…
        </div>
      </div>
    ),
  },
);

interface VoiceDomeClientProps {
  voices: VoicePanelRow[];
}

export function VoiceDomeClient({ voices }: VoiceDomeClientProps) {
  return <VoiceDome voices={voices} />;
}
