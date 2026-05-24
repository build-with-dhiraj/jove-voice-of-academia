"use client";

import { useEffect, useState, useRef, type RefObject } from "react";

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
 */
export function useIsMobile(): boolean {
  return useMediaQuery("(max-width: 768px)");
}

/**
 * IntersectionObserver-based visibility hook. Returns true when the ref's
 * element is intersecting the viewport (any pixel visible by default).
 *
 * Use this to pause expensive RAF loops / canvas renders / gesture listeners
 * when a component is scrolled off-screen.
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
