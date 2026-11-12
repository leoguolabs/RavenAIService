"""Unit tests for application Settings.

Covers the package_search_* fields introduced by the
``rebuild-package-search-with-claude-agent-sdk`` OpenSpec change and ensures
the legacy RAG fields have been fully removed.
"""

from __future__ import annotations

import pytest

from app.config import Settings


def test_api_docs_disabled_by_default_and_explicit_env_override(monkeypatch):
    monkeypatch.delenv("API_DOCS_ENABLED", raising=False)
    assert Settings(_env_file=None).api_docs_enabled is False
    monkeypatch.setenv("API_DOCS_ENABLED", "true")
    assert Settings(_env_file=None).api_docs_enabled is True
    monkeypatch.setenv("API_DOCS_ENABLED", "false")
    assert Settings(_env_file=None).api_docs_enabled is False


def test_package_search_defaults():
    s = Settings()
    assert s.package_search_max_turns == 8
    assert s.package_search_default_limit == 5
    assert s.package_search_max_limit == 50


def test_general_agent_turn_bound_default_and_env_override(monkeypatch):
    assert Settings().general_agent_max_turns == 6
    monkeypatch.setenv("GENERAL_AGENT_MAX_TURNS", "9")
    assert Settings().general_agent_max_turns == 9


def test_legacy_rag_fields_removed():
    s = Settings()
    for legacy in ("raven_vector_store_path", "rag_embedding_provider", "rag_embedding_model"):
        assert not hasattr(s, legacy), f"Settings still exposes legacy field {legacy!r}"


@pytest.mark.parametrize(
    "env_name,attr,expected",
    [
        ("PACKAGE_SEARCH_MAX_TURNS", "package_search_max_turns", 12),
        ("PACKAGE_SEARCH_DEFAULT_LIMIT", "package_search_default_limit", 7),
        ("PACKAGE_SEARCH_MAX_LIMIT", "package_search_max_limit", 100),
    ],
)
def test_package_search_overrides_from_env(monkeypatch, env_name, attr, expected):
    monkeypatch.setenv(env_name, str(expected))
    s = Settings()
    assert getattr(s, attr) == expected
