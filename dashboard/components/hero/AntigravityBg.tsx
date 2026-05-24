"use client";

/**
 * AntigravityBg
 * -------------
 * Production wrapper around the Three.js Antigravity scene.
 *
 * Enforces every gate from `~/.claude/plans/parsed-hatching-peach.md`
 * "Mandatory optimizations every 3D/heavy component must ship with":
 *
 *   1. Dynamic import + ssr:false   → AntigravityScene loaded lazily
 *   2. Viewport gating              → IntersectionObserver pauses canvas
 *   3. prefers-reduced-motion       → static CSS-gradient fallback, no Three.js fetched
 *   4. DPR cap at 1.5               → set on the <Canvas> in AntigravityScene
 *   5. Mobile particle reduction    → 830 desktop / 300 mobile via useIsMobile
 *   6. Loading skeleton             → CSS-gradient placeholder during chunk load
 *
 * Props are LOCKED by the user (see brief). Do not tune these without
 * sign-off — the perf budget assumes 830 desktop / 300 mobile.
 */

import dynamic from "next/dynamic";
import { useRef } from "react";

import { useIsMobile, useIsVisible, useReducedMotion } from "@/lib/perf";

import type { AntigravityProps } from "./AntigravityScene";

const PARTICLE_COLOR = "#FF9FFC";

// Locked user values. Mobile only modifies `count` (830 → 300).
const LOCKED_PROPS: Omit<AntigravityProps, "count" | "paused"> = {
  magnetRadius: 17,
  ringRadius: 5,
  waveSpeed: 1.5,
  waveAmplitude: 2.3,
  particleSize: 0.5,
  lerpSpeed: 0.06,
  color: PARTICLE_COLOR,
  autoAnimate: false,
  particleVariance: 1,
  rotationSpeed: 0,
  depthFactor: 1,
  pulseSpeed: 3,
  particleShape: "capsule",
  fieldStrength: 10,
};

/**
 * Static gradient placeholder shown:
 *   - during the lazy-chunk load (Suspense fallback)
 *   - permanently when `prefers-reduced-motion: reduce` is set
 *
 * Color and radial position chosen to evoke the live particle cloud so
 * the page composition doesn't shift between fallback and live.
 */
function AntigravityFallback() {
  return (
    <div
      aria-hidden="true"
      className="absolute inset-0 h-full w-full"
      style={{
        background: `radial-gradient(ellipse 60% 50% at 50% 50%, ${PARTICLE_COLOR}33 0%, ${PARTICLE_COLOR}15 35%, transparent 70%)`,
      }}
    />
  );
}

const AntigravityScene = dynamic(() => import("./AntigravityScene"), {
  ssr: false,
  loading: () => <AntigravityFallback />,
});

export interface AntigravityBgProps {
  /** Optional extra className applied to the wrapping div. */
  className?: string;
}

export function AntigravityBg({ className }: AntigravityBgProps) {
  const reduced = useReducedMotion();
  const isMobile = useIsMobile();
  const ref = useRef<HTMLDivElement | null>(null);
  // Trigger 200px before scrolling on/off so the canvas resumes ahead of
  // becoming visible (avoids a noticeable "snap to first frame").
  const isVisible = useIsVisible(ref, { rootMargin: "200px" });

  // The hero container always renders. The expensive Three.js work is
  // gated; the static gradient fallback is shown otherwise.
  return (
    <div
      ref={ref}
      aria-hidden="true"
      className={`pointer-events-none absolute inset-0 h-full w-full overflow-hidden ${className ?? ""}`}
    >
      {reduced ? (
        <AntigravityFallback />
      ) : (
        <AntigravityScene
          {...LOCKED_PROPS}
          count={isMobile ? 300 : 830}
          paused={!isVisible}
        />
      )}
    </div>
  );
}

export default AntigravityBg;
