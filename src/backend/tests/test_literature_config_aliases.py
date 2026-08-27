"""YFR-compatible environment aliases for literature providers."""

from app.core.config import Settings


def test_semantic_scholar_and_openalex_aliases(monkeypatch):
    monkeypatch.delenv("POLARIS_S2_API_KEY", raising=False)
    monkeypatch.delenv("POLARIS_OPENALEX_MAILTO", raising=False)
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "s2-legacy-key")
    monkeypatch.setenv("OPENALEX_MAILTO", "research@example.org")

    settings = Settings(_env_file=None)

    assert settings.s2_api_key == "s2-legacy-key"
    assert settings.openalex_mailto == "research@example.org"


def test_source_runtime_aliases(monkeypatch):
    monkeypatch.setenv("PAPER_SEARCH_SOURCE_CONCURRENCY", "7")
    monkeypatch.setenv("PAPER_SEARCH_SOURCE_TIMEOUT_SECONDS", "18")
    monkeypatch.setenv("PAPER_SEARCH_SOURCE_RETRIES", "3")

    settings = Settings(_env_file=None)

    assert settings.literature_source_concurrency == 7
    assert settings.literature_source_timeout_seconds == 18
    assert settings.literature_source_retries == 3
