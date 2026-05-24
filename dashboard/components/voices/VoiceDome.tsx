"use client";

/**
 * VoiceDome — a forked DomeGallery (reactbits.dev) that renders curated voice
 * quotes as text tiles on a CSS-3D dome. Includes a sentiment filter
 * (Positive · Neutral · Negative · All) above the dome and falls back to a
 * plain flex grid when `prefers-reduced-motion: reduce`.
 *
 * Optimisations baked in (per v1.2 perf budget):
 *   - 15² segments desktop, 10² mobile (vs the 35² canonical default)
 *   - Drag inertia RAF disconnects when the dome leaves the viewport
 *   - No images — pure DOM/text, so no `<img>` decode work
 *   - The sentiment filter remaps the existing `items` array in place,
 *     never tears down the dome
 *
 * The 3D math (radius, perspective, segment angles, item rotations) and the
 * `@use-gesture/react` wiring follow the reactbits original; the rendering
 * loop and `openItem` flow are replaced.
 */

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { useGesture } from "@use-gesture/react";

import type { Sentiment, VoicePanelRow } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useIsMobile, useIsVisible, useReducedMotion } from "@/lib/perf";

import { VoiceTile, VoicePlaceholderTile, excerpt } from "./VoiceTile";

import "./voice-dome.css";

// ---- Sentiment filter ----------------------------------------------------

type SentimentFilter = "all" | "positive" | "neutral" | "negative";

const FILTER_OPTIONS: { value: SentimentFilter; label: string }[] = [
  { value: "all", label: "All voices" },
  { value: "positive", label: "Positive" },
  { value: "neutral", label: "Neutral" },
  { value: "negative", label: "Negative" },
];

function matchesFilter(v: VoicePanelRow, f: SentimentFilter): boolean {
  if (f === "all") return true;
  // `mixed` is grouped under whichever bucket the curator note implies, but
  // by default we include `mixed` in both positive and negative reads so
  // it surfaces under either lens.
  if (f === "positive")
    return v.sentiment === "positive" || v.sentiment === "mixed";
  if (f === "negative")
    return v.sentiment === "negative" || v.sentiment === "mixed";
  return v.sentiment === f;
}

// ---- 3D math (lifted verbatim from reactbits DomeGallery) ----------------

type TilePos = { x: number; y: number; sizeX: number; sizeY: number };

const clamp = (v: number, min: number, max: number) =>
  Math.min(Math.max(v, min), max);
const wrapAngleSigned = (deg: number) => {
  const a = (((deg + 180) % 360) + 360) % 360;
  return a - 180;
};

/**
 * Build the lattice of tile coordinates for a given segment count. Each
 * column alternates its y-offsets so neighbouring tiles don't sit on the
 * same horizontal line (hex-ish packing → reads as a sphere, not a grid).
 *
 * We use wider tiles (sizeX=3, sizeY=2) than the canonical 2×2 because
 * text needs more horizontal real estate than a photo thumbnail.
 */
function buildLattice(segments: number, sizeX: number, sizeY: number): TilePos[] {
  // The canonical reactbits buildItems uses xCols of length `seg` starting
  // at -37 and stepping by +2. For lower segment counts we still want the
  // dome to span roughly the same angular range, so we keep the same
  // formula but accept a denser cluster around 0.
  const halfRange = (segments - 1);
  const xCols = Array.from(
    { length: segments },
    (_, i) => -halfRange + i * 2,
  );
  const evenYs = [-4, -2, 0, 2, 4];
  const oddYs = [-3, -1, 1, 3, 5];
  return xCols.flatMap((x, c) => {
    const ys = c % 2 === 0 ? evenYs : oddYs;
    return ys.map((y) => ({ x, y, sizeX, sizeY }));
  });
}

// ---- Component -----------------------------------------------------------

interface VoiceDomeProps {
  voices: VoicePanelRow[];
}

const TILE_SIZE_X_DESKTOP = 3;
const TILE_SIZE_X_MOBILE = 2;
const TILE_SIZE_Y = 2;
const DESKTOP_SEGMENTS = 15;
const MOBILE_SEGMENTS = 10;

export default function VoiceDome({ voices }: VoiceDomeProps) {
  const [filter, setFilter] = useState<SentimentFilter>("all");
  const isMobile = useIsMobile();
  const reducedMotion = useReducedMotion();

  const segments = isMobile ? MOBILE_SEGMENTS : DESKTOP_SEGMENTS;

  const filteredVoices = useMemo(
    () => voices.filter((v) => matchesFilter(v, filter)),
    [voices, filter],
  );

  // ---- Reduced motion: flat fallback. No gesture lib, no 3D. -------------
  if (reducedMotion) {
    return (
      <section
        aria-labelledby="voice-dome-heading"
        className="flex flex-col gap-6"
      >
        <DomeHeader filter={filter} setFilter={setFilter} />
        <ul className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {filteredVoices.map((v) => (
            <li
              key={v.permalink}
              className="rounded-xl border border-border bg-card p-4 text-sm leading-relaxed"
            >
              <a
                href={v.permalink}
                target="_blank"
                rel="noopener noreferrer"
                className="block hover:underline"
              >
                <p className="mb-2">{excerpt(v.body, 240)}</p>
                <p className="font-mono text-xs text-muted-foreground">
                  u/{v.author} · {v.sentiment} · ▲ {v.score}
                </p>
              </a>
            </li>
          ))}
          {filteredVoices.length === 0 ? (
            <li className="col-span-full rounded-xl bg-muted/40 px-4 py-6 text-sm text-muted-foreground">
              No voices match this filter.
            </li>
          ) : null}
        </ul>
      </section>
    );
  }

  return (
    <section
      aria-labelledby="voice-dome-heading"
      className="flex flex-col gap-6"
    >
      <DomeHeader filter={filter} setFilter={setFilter} />
      <DomeSurface
        voices={filteredVoices}
        segments={segments}
        isMobile={isMobile}
      />
    </section>
  );
}

// ---- Header (segmented control) -----------------------------------------

function DomeHeader({
  filter,
  setFilter,
}: {
  filter: SentimentFilter;
  setFilter: (f: SentimentFilter) => void;
}) {
  return (
    <header className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <h2
          id="voice-dome-heading"
          className="font-heading text-2xl font-semibold tracking-tight"
        >
          Voice constellation
        </h2>
        <p className="max-w-xl text-sm text-muted-foreground">
          Spin the dome. Each tile is a curated quote from a real Reddit
          thread, colour-coded by sentiment. Click a tile to open the
          original comment.
        </p>
      </div>
      <div
        role="radiogroup"
        aria-label="Filter voices by sentiment"
        className="inline-flex w-fit gap-1 rounded-lg border border-border bg-card p-1"
      >
        {FILTER_OPTIONS.map((opt) => {
          const active = filter === opt.value;
          return (
            <button
              key={opt.value}
              type="button"
              role="radio"
              aria-checked={active}
              onClick={() => setFilter(opt.value)}
              className={cn(
                "rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
                "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring",
                active
                  ? "bg-foreground text-background"
                  : "text-muted-foreground hover:bg-muted hover:text-foreground",
              )}
            >
              {opt.label}
            </button>
          );
        })}
      </div>
    </header>
  );
}

// ---- The dome surface itself --------------------------------------------

function DomeSurface({
  voices,
  segments,
  isMobile,
}: {
  voices: VoicePanelRow[];
  segments: number;
  isMobile: boolean;
}) {
  // (isMobile is referenced below to size the dome.)
  const rootRef = useRef<HTMLDivElement>(null);
  const mainRef = useRef<HTMLDivElement>(null);
  const sphereRef = useRef<HTMLDivElement>(null);
  const rotationRef = useRef({ x: 0, y: 0 });
  const startRotRef = useRef({ x: 0, y: 0 });
  const startPosRef = useRef<{ x: number; y: number } | null>(null);
  const draggingRef = useRef(false);
  const inertiaRAF = useRef<number | null>(null);
  const visible = useIsVisible(rootRef, { threshold: 0 });

  // Reactbits constants. Mobile dome is a smaller, tighter sphere so the
  // whole shape fits inside a 375px viewport. Desktop fills the bigger
  // container with a richer dome.
  const fit = isMobile ? 0.32 : 0.5;
  const minRadius = isMobile ? 140 : 480;
  const padFactor = isMobile ? 0.06 : 0.14;
  const dragSensitivity = 20;
  const dragDampening = 2;
  const maxVerticalRotationDeg = 5;

  // Lattice of tile coords. Recomputed only when segment count changes
  // (i.e. on a resize across the mobile breakpoint).
  const tileSizeX = isMobile ? TILE_SIZE_X_MOBILE : TILE_SIZE_X_DESKTOP;
  const lattice = useMemo(
    () => buildLattice(segments, tileSizeX, TILE_SIZE_Y),
    [segments, tileSizeX],
  );

  // Distribute voices across the lattice. Filled slots get voice content;
  // remaining slots get sentiment-tinted placeholders so the dome retains
  // visual density even when there are only 8 real voices in 225 slots.
  const tiles = useMemo(() => {
    if (voices.length === 0) {
      return lattice.map((pos) => ({
        pos,
        voice: null as VoicePanelRow | null,
        placeholderSentiment: "neutral" as Sentiment,
      }));
    }
    return lattice.map((pos, i) => {
      const voice = voices[i % voices.length];
      // Round-trip indexing means the dome always has every voice on it,
      // just repeated. For placeholder coloring we cycle through the
      // sentiment set so the dome doesn't lean visually one way.
      return {
        pos,
        voice,
        placeholderSentiment: voice.sentiment as Sentiment,
      };
    });
  }, [lattice, voices]);

  // ---- Radius / viewer-pad sizing (lifted from reactbits) --------------
  // Depends on the sizing constants because they switch at the mobile
  // breakpoint — without them in the deps, an isMobile flip would keep the
  // stale desktop minRadius.
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const ro = new ResizeObserver((entries) => {
      const cr = entries[0].contentRect;
      const w = Math.max(1, cr.width);
      const h = Math.max(1, cr.height);
      const minDim = Math.min(w, h);
      const aspect = w / h;
      const basis = aspect >= 1.3 ? w : minDim;
      let radius = basis * fit;
      const heightGuard = h * 1.35;
      radius = Math.min(radius, heightGuard);
      radius = Math.max(radius, minRadius);
      const viewerPad = Math.max(8, Math.round(minDim * padFactor));
      root.style.setProperty("--radius", `${Math.round(radius)}px`);
      root.style.setProperty("--viewer-pad", `${viewerPad}px`);
      applyTransform(rotationRef.current.x, rotationRef.current.y);
    });
    ro.observe(root);
    return () => ro.disconnect();
  }, [fit, minRadius, padFactor]);

  const applyTransform = (xDeg: number, yDeg: number) => {
    const el = sphereRef.current;
    if (el) {
      el.style.transform = `translateZ(calc(var(--radius) * -1)) rotateX(${xDeg}deg) rotateY(${yDeg}deg)`;
    }
  };

  const stopInertia = () => {
    if (inertiaRAF.current) {
      cancelAnimationFrame(inertiaRAF.current);
      inertiaRAF.current = null;
    }
  };

  const startInertia = (vx: number, vy: number) => {
    const MAX_V = 1.4;
    let vX = clamp(vx, -MAX_V, MAX_V) * 80;
    let vY = clamp(vy, -MAX_V, MAX_V) * 80;
    let frames = 0;
    const d = clamp(dragDampening, 0, 1);
    const frictionMul = 0.94 + 0.055 * d;
    const stopThreshold = 0.015 - 0.01 * d;
    const maxFrames = Math.round(90 + 270 * d);

    const step = () => {
      vX *= frictionMul;
      vY *= frictionMul;
      if (Math.abs(vX) < stopThreshold && Math.abs(vY) < stopThreshold) {
        inertiaRAF.current = null;
        return;
      }
      if (++frames > maxFrames) {
        inertiaRAF.current = null;
        return;
      }
      const nextX = clamp(
        rotationRef.current.x - vY / 200,
        -maxVerticalRotationDeg,
        maxVerticalRotationDeg,
      );
      const nextY = wrapAngleSigned(rotationRef.current.y + vX / 200);
      rotationRef.current = { x: nextX, y: nextY };
      applyTransform(nextX, nextY);
      inertiaRAF.current = requestAnimationFrame(step);
    };
    stopInertia();
    inertiaRAF.current = requestAnimationFrame(step);
  };

  // ---- Gesture: drag to rotate (reactbits's pattern, condensed) -------
  useGesture(
    {
      onDragStart: ({ event }) => {
        stopInertia();
        const evt = event as PointerEvent;
        draggingRef.current = true;
        startRotRef.current = { ...rotationRef.current };
        startPosRef.current = { x: evt.clientX, y: evt.clientY };
      },
      onDrag: ({ event, last, velocity = [0, 0], direction = [0, 0] }) => {
        if (!draggingRef.current || !startPosRef.current) return;
        const evt = event as PointerEvent;
        const dxTotal = evt.clientX - startPosRef.current.x;
        const dyTotal = evt.clientY - startPosRef.current.y;
        const nextX = clamp(
          startRotRef.current.x - dyTotal / dragSensitivity,
          -maxVerticalRotationDeg,
          maxVerticalRotationDeg,
        );
        const nextY = wrapAngleSigned(
          startRotRef.current.y + dxTotal / dragSensitivity,
        );
        if (
          rotationRef.current.x !== nextX ||
          rotationRef.current.y !== nextY
        ) {
          rotationRef.current = { x: nextX, y: nextY };
          applyTransform(nextX, nextY);
        }
        if (last) {
          draggingRef.current = false;
          const [vMagX, vMagY] = velocity;
          const [dirX, dirY] = direction;
          const vx = vMagX * dirX;
          const vy = vMagY * dirY;
          if (Math.abs(vx) > 0.005 || Math.abs(vy) > 0.005) {
            startInertia(vx, vy);
          }
        }
      },
    },
    {
      target: mainRef,
      eventOptions: { passive: true },
      // Only attach the gesture when visible. When the dome scrolls
      // off-screen, the IntersectionObserver flips `visible` and
      // `useGesture` re-runs with `enabled: false`, detaching listeners.
      enabled: visible,
    },
  );

  // ---- Off-screen: also kill any running inertia RAF ------------------
  useEffect(() => {
    if (!visible && inertiaRAF.current) {
      stopInertia();
    }
  }, [visible]);

  return (
    <div
      ref={rootRef}
      className="sphere-root dome-surface"
      style={
        {
          ["--segments-x"]: segments,
          ["--segments-y"]: segments,
        } as CSSProperties
      }
    >
      <main ref={mainRef} className="sphere-main">
        <div className="stage">
          <div ref={sphereRef} className="sphere">
            {tiles.map((t, i) => (
              <div
                key={`${t.pos.x},${t.pos.y},${i}`}
                className="item"
                style={
                  {
                    ["--offset-x"]: t.pos.x,
                    ["--offset-y"]: t.pos.y,
                    ["--item-size-x"]: t.pos.sizeX,
                    ["--item-size-y"]: t.pos.sizeY,
                  } as CSSProperties
                }
              >
                <div className="item__face">
                  {t.voice ? (
                    <VoiceTile voice={t.voice} />
                  ) : (
                    <VoicePlaceholderTile sentiment={t.placeholderSentiment} />
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
        <div className="overlay" aria-hidden="true" />
        <div className="edge-fade edge-fade--top" aria-hidden="true" />
        <div className="edge-fade edge-fade--bottom" aria-hidden="true" />
      </main>
      {/* Sphere is decorative; the voice list lives in the DOM for SR users
          when reduced-motion is off but a screen reader is on. */}
      <ul className="sr-only">
        {voices.map((v) => (
          <li key={v.permalink}>
            <a href={v.permalink}>
              {v.author}: {excerpt(v.body, 200)}
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}

// Marker — this hint isn't read at runtime, but signals to bundlers /
// future maintainers that the perf hook & gesture lib must not enter
// the SSR bundle (the parent uses next/dynamic({ ssr: false })).
export const __ssrUnsafe = true;
