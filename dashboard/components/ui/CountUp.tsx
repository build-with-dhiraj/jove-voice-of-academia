"use client";

import { useEffect, useRef, useState, useSyncExternalStore } from "react";

type CountUpProps = {
  value: number;
  duration?: number;
  className?: string;
  /** Locale used by Intl.NumberFormat — matches the rest of the dashboard. */
  locale?: string;
};

const REDUCED_MOTION_QUERY = "(prefers-reduced-motion: reduce)";

function subscribeReducedMotion(callback: () => void): () => void {
  const mql = window.matchMedia(REDUCED_MOTION_QUERY);
  mql.addEventListener("change", callback);
  return () => mql.removeEventListener("change", callback);
}

function getReducedMotion(): boolean {
  return window.matchMedia(REDUCED_MOTION_QUERY).matches;
}

/**
 * Animated number counter — counts 0 → value once the element scrolls into view.
 *
 * Pure RAF + IntersectionObserver, no external deps. Honors
 * `prefers-reduced-motion: reduce` (reactively) by rendering the final value
 * with no animation. Preserves `tabular-nums` styling so digits don't reflow.
 *
 * State design: we track a single animation progress `t` in [0, 1]. The
 * displayed number is derived during render — no setState in branches inside
 * effects (passes react-hooks/set-state-in-effect). SSR + reduced-motion
 * clients render the final value with t=1.
 */
export function CountUp({
  value,
  duration = 1200,
  className,
  locale = "en-US",
}: CountUpProps) {
  const ref = useRef<HTMLSpanElement | null>(null);

  // Reactive: re-renders if the user toggles the OS setting at runtime.
  // SSR returns false; rule is checked again at render-time so reduced clients
  // always derive t=1.
  const reducedMotion = useSyncExternalStore(
    subscribeReducedMotion,
    getReducedMotion,
    () => false,
  );

  // Single source of truth for progress. SSR/reduced-motion → 1 → final value.
  const [t, setT] = useState<number>(1);

  useEffect(() => {
    // Reduced motion or SSR replay → snap to final, no RAF.
    if (reducedMotion) return;

    const node = ref.current;
    if (!node) return;

    // Reset progress only on the animation path.
    setT(0);

    let raf = 0;
    let start: number | null = null;

    const tick = (ts: number) => {
      if (start === null) start = ts;
      const elapsed = ts - start;
      const next = Math.min(elapsed / duration, 1);
      setT(next);
      if (next < 1) raf = requestAnimationFrame(tick);
    };

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          raf = requestAnimationFrame(tick);
          observer.disconnect();
        }
      },
      { threshold: 0.1 },
    );
    observer.observe(node);

    return () => {
      observer.disconnect();
      cancelAnimationFrame(raf);
    };
  }, [value, duration, reducedMotion]);

  // ease-out cubic, derived during render — no extra state, no branch-setState.
  const eased = reducedMotion ? 1 : 1 - Math.pow(1 - t, 3);
  const display = Math.round(value * eased);

  return (
    <span ref={ref} className={className}>
      {display.toLocaleString(locale)}
    </span>
  );
}
