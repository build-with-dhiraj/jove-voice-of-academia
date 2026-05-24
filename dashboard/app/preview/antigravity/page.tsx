import type { Metadata } from "next";
import { AntigravityBg } from "@/components/hero/AntigravityBg";

/**
 * /preview/antigravity
 * --------------------
 * Isolated preview route for the v1.2 Antigravity hero experiment.
 *
 * NOT linked from the main page. Exists purely as a Lighthouse target
 * during the v1.2 visual redesign loop (see
 * `~/.claude/plans/parsed-hatching-peach.md`).
 *
 * Page shape:
 *   - Full-viewport hero section bounded to `h-screen` (so the
 *     IntersectionObserver inside AntigravityBg can detect when the user
 *     has scrolled past it and pause the canvas RAF)
 *   - Page title overlaid via z-index (mimics the eventual main-page hero)
 *   - Below-the-fold scroll target so the off-screen pause path can be
 *     visually + behaviorally verified
 *   - No data fetching, no Suspense, no third-party widgets — keeps the
 *     Lighthouse number a clean reflection of the Antigravity cost alone.
 */

export const metadata: Metadata = {
  title: "Preview — Antigravity hero",
  robots: { index: false, follow: false },
};

export default function AntigravityPreviewPage() {
  return (
    <main className="relative isolate w-full bg-background text-foreground">
      <section className="relative isolate h-screen w-full overflow-hidden">
        {/* z-0 — particle field sits behind the title content */}
        <div className="absolute inset-0 z-0">
          <AntigravityBg />
        </div>

        {/* z-10 — page content overlay */}
        <div className="relative z-10 mx-auto flex h-full w-full max-w-6xl flex-col justify-center gap-6 px-6 md:px-10">
          <p className="font-mono text-xs uppercase tracking-[0.18em] text-muted-foreground">
            Voice of Academia
          </p>
          <h1 className="font-heading text-4xl font-semibold tracking-tight text-foreground md:text-6xl lg:text-7xl">
            JoVE Research &mdash; Reddit sentiment landscape
          </h1>
          <p className="max-w-2xl text-base leading-relaxed text-muted-foreground md:text-lg">
            What academia says on Reddit about publishing in the JoVE
            Research line. Threads tagged for sentiment and theme, ranked
            breadth-first.
          </p>
        </div>
      </section>

      {/* Below-the-fold content. Scrolling past the hero must pause the
          canvas RAF — Antigravity is bounded to the hero section so its
          IntersectionObserver flips `paused=true` once the section is
          fully out of view. */}
      <section className="relative z-10 mx-auto w-full max-w-6xl px-6 py-32 md:px-10">
        <div className="space-y-6 text-sm text-muted-foreground">
          <h2 className="text-2xl font-semibold text-foreground">
            Scroll target
          </h2>
          <p>
            This block exists so the Antigravity canvas can be scrolled out
            of the viewport. When scrolled past the hero, the canvas&apos;
            frameloop pauses (verified via IntersectionObserver +
            <code className="ml-1 rounded bg-muted px-1 py-0.5 font-mono text-xs">
              frameloop=&quot;never&quot;
            </code>
            ) so the RAF cost is fully released.
          </p>
          {Array.from({ length: 30 }).map((_, i) => (
            <p key={i}>
              Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed
              do eiusmod tempor incididunt ut labore et dolore magna aliqua.
              Ut enim ad minim veniam, quis nostrud exercitation ullamco
              laboris nisi ut aliquip ex ea commodo consequat. Duis aute
              irure dolor in reprehenderit in voluptate velit esse cillum
              dolore eu fugiat nulla pariatur. Excepteur sint occaecat
              cupidatat non proident, sunt in culpa qui officia deserunt
              mollit anim id est laborum.
            </p>
          ))}
        </div>
      </section>
    </main>
  );
}
