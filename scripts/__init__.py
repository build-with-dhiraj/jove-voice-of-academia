"""Operational scripts (CLIs, mega-backfill orchestrators).

These are user-facing entry points distinct from the importable
``pipeline`` package. The package marker exists so
``[project.scripts]`` entries like
``voice-curate = "scripts.voice_curate_cli:main"`` resolve at uv-sync
time.
"""
