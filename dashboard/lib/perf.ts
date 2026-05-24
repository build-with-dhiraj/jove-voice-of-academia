/**
 * Shared performance hooks for v1.2 heavy components.
 *
 * Every 3D / canvas / GSAP component in v1.2 must use these to enforce the
 * Lighthouse-≥-90-desktop, ≥-75-mobile floor from
 * `~/.claude/plans/parsed-hatching-peach.md`:
 *
 *   - useReducedMotion → swap to static fallback (no anim, no mount)
 *   - useIsVisible    → pause RAF when scrolled off-screen
 *   - useIsMobile     → reduce particle counts on small viewports
 */

"use client";

import { useEffect, useRef, useState } from "react";

const MOBILE_BREAKPOINT_PX = 768;

/**
 * Returns true if the user has `prefers-reduced-motion: reduce` set.
 * SSR-safe: returns `false` during the initial server render, then resolves
 * on the client after mount. Components that key animation mount on this
 * value must therefore tolerate a one-frame flicker; for Antigravity we
 * gate the whole dynamic-import on this so the reduce-motion fallback
 * never even fetches the Three.js chunk.
 */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mql = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(mql.matches);
    update();
    mql.addEventListener("change", update);
    return () => mql.removeEventListener("change", update);
  }, []);

  return reduced;
}

/**
 * Returns true when the viewport width is below the mobile breakpoint.
 * Used to scale down Antigravity particle count to 300 (vs 830 desktop).
 */
export function useIsMobile(breakpointPx: number = MOBILE_BREAKPOINT_PX): boolean {
  const [isMobile, setIsMobile] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mql = window.matchMedia(`(max-width: ${breakpointPx - 1}px)`);
    const update = () => setIsMobile(mql.matches);
    update();
    mql.addEventListener("change", update);
    return () => mql.removeEventListener("change", update);
  }, [breakpointPx]);

  return isMobile;
}

/**
 * IntersectionObserver hook. Returns [ref, isVisible].
 * The R3F `<Canvas>` is set to `frameloop="demand"` and `frameloop="always"`
 * is toggled based on this. When off-screen the render loop pauses — zero
 * RAF cost, fully releases the main thread for whatever the user is scrolling
 * toward.
 */
export function useIsVisible<T extends Element = HTMLElement>(
  options: IntersectionObserverInit = { rootMargin: "100px" },
): [React.RefObject<T | null>, boolean] {
  const ref = useRef<T | null>(null);
  // Default to `true` so the first paint is "visible" — the canvas mounts
  // immediately if the user lands on a hero. The observer corrects within
  // one frame if it's actually off-screen.
  const [isVisible, setIsVisible] = useState(true);

  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const node = ref.current;
    if (!node) return;

    const obs = new IntersectionObserver(
      ([entry]) => setIsVisible(entry.isIntersecting),
      options,
    );
    obs.observe(node);
    return () => obs.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options.rootMargin, options.threshold]);

  return [ref, isVisible];
}
