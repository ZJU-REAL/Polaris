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
