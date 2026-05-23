"""Minimal smoke tests for :mod:`scripts.voice_curate_cli`.

The interactive TUI is hard to test exhaustively without a fake
terminal; per the Phase 11 brief we keep this file thin and verify only
that:

* The module imports cleanly (entry-point wiring sanity).
* ``--help`` exits cleanly via argparse's standard behavior.
* Running against an empty candidates dir exits 0 with a friendly
  message (no exception).

End-to-end interactive coverage of the prompt loop is intentionally
out of scope — that's verified by D running ``uv run voice-curate``
against the live vault during the weekly ritual, which is the
ground-truth integration test.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_module_imports() -> None:
    """The CLI module is importable (entry-point wiring sanity)."""

    import scripts.voice_curate_cli as cli  # noqa: F401

    assert hasattr(cli, "main")
    assert hasattr(cli, "run")


def test_help_exits_cleanly() -> None:
    """``--help`` exits with code 0 via argparse's SystemExit."""

    from scripts.voice_curate_cli import run

    with pytest.raises(SystemExit) as exc:
        run(argv=["--help"])
    # argparse exits 0 on --help.
    assert exc.value.code == 0


def test_run_with_empty_candidates_dir_returns_zero(
    tmp_path: Path,
) -> None:
    """An empty Candidates folder is handled gracefully (no exception).

    The CLI prints a friendly message and exits with code 0 — quitting
    on "nothing to do" is not an error condition.
    """

    from scripts.voice_curate_cli import run

    empty_dir = tmp_path / "Candidates"
    empty_dir.mkdir()
    code = run(argv=["--candidates-dir", str(empty_dir)])
    assert code == 0
