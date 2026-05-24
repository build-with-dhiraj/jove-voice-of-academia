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
import { useEffect, useState } from "react";

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
  const Tag = (tag ?? "h1") as React.ElementType;
  return (
    <Tag
      className={className}
      style={{
        textAlign,
        display: "inline-block",
        whiteSpace: "normal",
        wordWrap: "break-word",
      }}
    >
      {text}
    </Tag>
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
  // Tri-state: null = SSR / pre-mount unknown, true = reduced, false = full motion.
  // We must not assume either branch on the server — both have to render the same
  // initial HTML, then the client takes over once we know the user's preference.
  //
  // The initial value is derived synchronously via the useState initializer
  // (runs once per mount, never on the server) rather than written from inside
  // an effect, so it doesn't trip React 19's `set-state-in-effect` rule.
  const [reduced, setReduced] = useState<boolean | null>(() => {
    if (typeof window === "undefined" || !window.matchMedia) return null;
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  });

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
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
