import Link from "next/link";
import { ChevronLeft } from "lucide-react";

import { VoiceDomeClient } from "@/components/voices/VoiceDomeClient";
import { loadLatest } from "@/lib/data";

/**
 * The dome itself is lazy-loaded inside `VoiceDomeClient` (a 'use client'
 * boundary). The route stays a server component so `latest.json` is read
 * at build time and serialised as a prop — no client-side fetch.
 *
 * Why the indirection: Next.js 16 forbids `ssr: false` inside server
 * components. `VoiceDomeClient` is a small wrapper that handles the
 * dynamic import + skeleton.
 */

/**
 * Static at build time (no per-request data, no cookies/headers used).
 * The cron pushes a new `latest.json` weekly; the next deploy rebuilds.
 */
export const revalidate = false;

export const metadata = {
  title: "Voice constellation — VoA",
  description:
    "Curated voice quotes from academic Reddit, laid out as a navigable 3D dome with sentiment filtering.",
};

export default async function VoicesPage() {
  const agg = await loadLatest();

  return (
    <main className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-6 py-10 md:px-10 md:py-14">
      <nav className="flex items-center gap-2 text-sm text-muted-foreground">
        <Link
          href="/"
          className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 hover:bg-muted hover:text-foreground"
        >
          <ChevronLeft className="size-4" aria-hidden="true" />
          Back to dashboard
        </Link>
      </nav>

      <VoiceDomeClient voices={agg.voice_published} />
    </main>
  );
}
