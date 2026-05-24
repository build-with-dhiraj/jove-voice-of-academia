"use client";

/**
 * Shared performance hooks for v1.2 heavy components.
 *
 * Every 3D / canvas / GSAP component in v1.2 must use these to enforce the
 * Lighthouse-≥-90-desktop, ≥-75-mobile floor from
 * `~/.claude/plans/parsed-hatching-peach.md`:
 *
 *   - useReducedMotion → swap to static fallback (no anim, no mount)
 *   - useIsVisible    → pause RAF / WebGL when scrolled off-screen
 *   - useIsMobile     → reduce particle counts on small viewports
 *
 * Compose-phase unification note:
 *
 * The Antigravity exp branch and the Dome exp branch each introduced their
 * own `useIsVisible` with incompatible signatures. The unified version here
 * uses the more idiomatic React API — consumer owns the ref — to match
 * `useIsVisible` patterns elsewhere in the ecosystem. AntigravityBg was
 * updated to create its own ref and pass it in.
 */

import { useEffect, useRef, useState, type RefObject } from "react";

const MOBILE_BREAKPOINT_PX = 768;

/**
 * Media query hook. Subscribes once, cleans up on unmount.
 *
 * SSR-safe: returns `false` on first render to match the server (no `window`),
 * then re-renders on the client with the real value. Prevents hydration drift.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia(query);
    setMatches(mq.matches);
    const onChange = (e: MediaQueryListEvent) => setMatches(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [query]);

  return matches;
}

/**
 * `prefers-reduced-motion: reduce` — when true, components MUST render a
 * static fallback in place of any motion / 3D / WebGL surface.
 */
export function useReducedMotion(): boolean {
  return useMediaQuery("(prefers-reduced-motion: reduce)");
}

/**
 * Convenience wrapper. Mobile breakpoint matches Tailwind's `md:` (768px).
 * Used to scale down Antigravity particle count to 300 (vs 830 desktop).
 */
export function useIsMobile(
  breakpointPx: number = MOBILE_BREAKPOINT_PX,
): boolean {
  return useMediaQuery(`(max-width: ${breakpointPx - 1}px)`);
}

/**
 * IntersectionObserver-based visibility hook. Returns true when the ref's
 * element is intersecting the viewport (default threshold 0).
 *
 * Use this to pause expensive RAF loops / canvas renders / gesture listeners
 * when a component is scrolled off-screen. The R3F `<Canvas>` is set to
 * `frameloop="demand"` and `frameloop="always"` is toggled based on this.
 *
 * Initial value defaults to `true` so the first paint is "visible" — the
 * canvas mounts immediately if the user lands on a hero. The observer
 * corrects within one frame if it's actually off-screen.
 */
export function useIsVisible<T extends Element>(
  ref: RefObject<T | null>,
  options: IntersectionObserverInit = { threshold: 0 },
): boolean {
  const [visible, setVisible] = useState(true);
  // Stable options reference — we only care about identity changing if the
  // caller actually swaps objects.
  const optsRef = useRef(options);
  optsRef.current = options;

  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(
      (entries) => setVisible(entries[0]?.isIntersecting ?? false),
      optsRef.current,
    );
    io.observe(el);
    return () => io.disconnect();
  }, [ref]);

  return visible;
}
