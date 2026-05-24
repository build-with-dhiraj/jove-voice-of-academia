"use client";

/**
 * SplitTextHeader — reduce-motion-aware wrapper around SplitText.
 *
 * Responsibilities:
 * 1. Read `prefers-reduced-motion: reduce` at mount.
 * 2. If reduced motion is requested, render the static H1 immediately —
 *    GSAP/SplitText are NEVER imported (the dynamic import call site sits
 *    inside the branch that only runs when motion is allowed).
 * 3. Otherwise lazy-load SplitText via `next/dynamic` with `ssr: false`,
 *    so GSAP doesn't enter the SSR bundle and doesn't ship as part of
 *    the initial JS payload.
 *
 * The static H1 markup is intentionally identical (same tag, same classes)
 * so layout/sizing matches between the two branches and there is no shift
 * when the dynamic chunk resolves.
 */

import dynamic from "next/dynamic";
import React, { useEffect, useState } from "react";

import type { SplitTextProps } from "./SplitText";

// Loading placeholder mirrors the final markup so the layout shape is stable
// while the GSAP chunk is fetched. Characters are visible from frame 1 of the
// page — the SplitText effect will revert them to opacity 0 just before the
// stagger plays. This avoids a blank-h1 flash on slow networks.
function StaticHeading({
  text,
  className,
  tag = "h1",
  textAlign = "left",
}: Pick<SplitTextProps, "text" | "className" | "tag" | "textAlign">) {
  // React.createElement avoids a TypeScript variance issue with JSX <Tag>
  // children inference when Tag is `React.ElementType` and the compile graph
  // includes ambient JSX module augmentations (@react-three/fiber via three).
  return React.createElement(
    (tag ?? "h1") as React.ElementType,
    {
      className,
      style: {
        textAlign,
        display: "inline-block",
        whiteSpace: "normal",
        wordWrap: "break-word",
      },
    },
    text,
  );
}

// Dynamic import lives at module scope (not inside the component) so the
// chunk-graph is established at build time. `ssr: false` keeps GSAP +
// SplitText plugin out of the server bundle.
const SplitTextDynamic = dynamic(() => import("./SplitText"), {
  ssr: false,
  loading: () => null, // StaticHeading already covers the no-motion + pre-mount paint
});

export default function SplitTextHeader(props: SplitTextProps) {
  // Tri-state: null = SSR / pre-hydration, true = reduced, false = full motion.
  //
  // SSR-safety contract: the FIRST client render MUST emit the same HTML as
  // the server. That means the useState initializer must NOT read
  // `window.matchMedia` — even with a `typeof window` guard, the initializer
  // also runs on the client during hydration and would diverge from the
  // server's `null` value, swapping <StaticHeading> for <SplitTextDynamic />
  // (which resolves to `null` because `loading: () => null`). That mismatch
  // is what flagged in the v1.2 console error on `/`.
  //
  // Pattern mirrors `lib/perf.ts:useMediaQuery` and `ui/CountUp.tsx`'s
  // `useSyncExternalStore` with a `() => false` server snapshot: pin the
  // first paint to a constant matching SSR, then transition via useEffect
  // *after* hydration is complete.
  const [reduced, setReduced] = useState<boolean | null>(null);

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    // Sync to the actual user preference once we're past hydration.
    setReduced(mq.matches);
    const onChange = (e: MediaQueryListEvent) => setReduced(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  // Pre-mount + reduced-motion paths both render the static heading.
  // The static heading is also what SSR emits, so hydration matches.
  if (reduced === null || reduced === true) {
    return (
      <StaticHeading
        text={props.text}
        className={props.className}
        tag={props.tag}
        textAlign={props.textAlign}
      />
    );
  }

  // Motion-OK path: dynamic GSAP chunk loads, animation plays once.
  return <SplitTextDynamic {...props} />;
}
