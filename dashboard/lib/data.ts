import { promises as fs } from "node:fs";
import path from "node:path";

import type { Aggregate, Taxonomy, ThemeDef } from "./types";

/**
 * Loads the latest aggregate produced by the pipeline.
 *
 * In dev: reads `dashboard/data/latest.json` (mock data prior to backfill).
 * In prod: the same file, but rewritten by the weekly cron commit and shipped
 * statically via ISR.
 *
 * Server-only. Do not import from a client component.
 */
export async function loadLatest(): Promise<Aggregate> {
  const file = path.join(process.cwd(), "data", "latest.json");
  const raw = await fs.readFile(file, "utf8");
  return JSON.parse(raw) as Aggregate;
}

/**
 * Loads the canonical taxonomy from the repo's locked data/ folder. Used to
 * resolve theme ids -> human-readable labels and descriptions in the UI.
 *
 * The taxonomy file lives at the repo root (`<repo>/data/taxonomy.json`),
 * one level up from the dashboard cwd.
 */
export async function loadTaxonomy(): Promise<Taxonomy> {
  const file = path.join(process.cwd(), "..", "data", "taxonomy.json");
  const raw = await fs.readFile(file, "utf8");
  return JSON.parse(raw) as Taxonomy;
}

/**
 * Index a Taxonomy by theme id for O(1) lookups during render.
 */
export function indexThemes(taxonomy: Taxonomy): Map<string, ThemeDef> {
  const map = new Map<string, ThemeDef>();
  for (const t of taxonomy.themes) {
    map.set(t.id, t);
  }
  map.set(taxonomy.emerging_bucket.id, {
    id: taxonomy.emerging_bucket.id,
    label: taxonomy.emerging_bucket.label,
    description: taxonomy.emerging_bucket.description,
    applies_to_sentiments: ["positive", "neutral", "negative"],
  });
  return map;
}
