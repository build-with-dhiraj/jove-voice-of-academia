# Voice of Academia (VoA)

Weekly-running autonomous Reddit sentiment pipeline + Vercel dashboard for academic publishing discourse around JoVE's Research line — JoVE Journal, Encyclopedia of Experiments, Visualize, and Topical Collections.

> v1 scope is **JoVE-only**. The data model is journal-agnostic for forward-compatibility with v2 (comparative analysis across Nature, Cell, eLife, etc.) but v1 builds and dashboards target JoVE alone.

## Architecture

```
Reddit  ──┐
Firecrawl ├──>  discover  ─>  fetch  ─>  disambiguate  ─>  tag (Claude)  ─>  store
Google   ──┘                                                                    │
                                                                                ├─>  Pinecone (HD, social-listening namespace)
                                                                                ├─>  Obsidian vault (RAM, Voice candidates)
                                                                                └─>  aggregate JSON  ─>  Next.js dashboard on Vercel
```

- **Pipeline**: Python 3.11+, `uv` package manager
- **Dashboard**: Next.js 15+ (App Router), TypeScript strict, Tailwind, shadcn/ui
- **Infra**: GitHub Actions (cron), Vercel hosting, Pinecone (`jove-memory` index, `social-listening` namespace)

See `/Users/pw/Obsidian/JoVE/50 — Decisions & Bets/Voice of Academia — System Design.md` and `Voice of Academia — Implementation Plan.md` for the locked design and phase plan.

## Quickstart

```bash
# Python pipeline
uv sync
cp .env.example .env  # then populate secrets locally; .env is gitignored
uv run python -c "import praw, firecrawl, anthropic, pinecone"

# Dashboard
cd dashboard
pnpm install
pnpm dev   # http://localhost:3000
```

## Status

Phase 0 (foundational scaffold) — complete.
Phases 1–11 — see `Voice of Academia — Implementation Plan.md` in the JoVE vault.
