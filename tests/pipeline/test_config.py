"""Tests for :mod:`pipeline.config`.

Two concerns:

1. Config loads cleanly when *no* env vars are set (the v1 default — Reddit
   dev access is pending so the pipeline runs in unauth mode).
2. ``load_data_json`` resolves filenames against the repo ``data/`` directory
   and parses the canonical artifacts.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def fresh_config(monkeypatch: pytest.MonkeyPatch):
    """Return a freshly-imported :mod:`pipeline.config` with all VoA env vars unset.

    Re-importing isolates each test from sibling-test env-var setattrs and
    from the test process's ambient ``.env`` (which may exist on D's machine).
    """

    for var in (
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "REDDIT_USER_AGENT",
        "ANTHROPIC_API_KEY",
        "PINECONE_API_KEY",
        "FIRECRAWL_API_KEY",
        "PINECONE_INDEX",
        "PINECONE_NAMESPACE",
        "VAULT_ROOT",
        "VAULT_VOICE_CANDIDATES",
        "VAULT_VOICE_PUBLISHED",
        "VAULT_PULSE",
    ):
        monkeypatch.delenv(var, raising=False)

    # Prevent python-dotenv from re-populating from .env at the repo root.
    monkeypatch.setattr(
        "dotenv.main.load_dotenv",
        lambda *args, **kwargs: False,
    )

    import pipeline.config as config

    importlib.reload(config)
    return config


def test_unauth_mode_when_reddit_creds_missing(fresh_config) -> None:
    """With no env, Reddit creds resolve to None and the auth mode is 'unauth'."""

    assert fresh_config.REDDIT_CLIENT_ID is None
    assert fresh_config.REDDIT_CLIENT_SECRET is None
    assert fresh_config.reddit_auth_mode() == "unauth"


def test_pinecone_defaults_to_jove_memory_social_listening(fresh_config) -> None:
    """Even with no env, the Pinecone index + namespace constants are correct."""

    assert fresh_config.PINECONE_INDEX == "jove-memory"
    assert fresh_config.PINECONE_NAMESPACE == "social-listening"


def test_time_horizon_is_2010(fresh_config) -> None:
    """Spec D7 cutoff is 2010-01-01."""

    assert fresh_config.TIME_HORIZON.year == 2010
    assert fresh_config.TIME_HORIZON.month == 1
    assert fresh_config.TIME_HORIZON.day == 1


def test_user_agent_has_a_default(fresh_config) -> None:
    """User agent falls back to the project default when env is unset."""

    assert fresh_config.REDDIT_USER_AGENT.startswith("voa-pipeline/")


def test_load_data_json_returns_taxonomy_dict() -> None:
    """``taxonomy.json`` is one of the canonical artifacts and must parse with a ``themes`` key."""

    from pipeline.config import load_data_json

    data = load_data_json("taxonomy.json")
    assert isinstance(data, dict)
    assert "themes" in data


def test_load_data_json_returns_journals_dict() -> None:
    """``journals.json`` must have v1_active=['JoVE'] and the jove_aliases_to_match list."""

    from pipeline.config import load_data_json

    data = load_data_json("journals.json")
    assert data["v1_active"] == ["JoVE"]
    assert "JoVE" in data["jove_aliases_to_match"]


def test_load_data_json_raises_on_missing_file() -> None:
    """Missing artifact = explicit FileNotFoundError, not a silent empty dict."""

    from pipeline.config import load_data_json

    with pytest.raises(FileNotFoundError):
        load_data_json("does-not-exist.json")


def test_authenticated_mode_when_both_creds_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """When both Reddit creds are set, ``reddit_auth_mode()`` returns 'authenticated'."""

    monkeypatch.setenv("REDDIT_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setattr(
        "dotenv.main.load_dotenv",
        lambda *args, **kwargs: False,
    )

    import pipeline.config as config

    importlib.reload(config)
    try:
        assert config.reddit_auth_mode() == "authenticated"
    finally:
        # Reload again without the fake creds so other tests start clean.
        monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
        monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
        importlib.reload(config)
