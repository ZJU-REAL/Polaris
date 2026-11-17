"""Claude 的 key 与地址从哪个环境变量来（core/config.py）。"""

import pytest

from app.core.config import Settings, normalize_claude_base_url

_NAMES = (
    "POLARIS_CLAUDE_API_KEY",
    "POLARIS_CLAUDE_BASE_URL",
    "POLARIS_CLAUDE_MODEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "LITELLM_API_KEY",
    "LITELLM_API_BASE",
)


@pytest.fixture
def env(monkeypatch):
    for name in _NAMES:
        monkeypatch.delenv(name, raising=False)

    def build(**values: str) -> Settings:
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        return Settings(_env_file=None)

    return build


def test_no_key_means_not_configured(env):
    settings = env()
    assert settings.claude_configured is False
    assert settings.claude_api_key == "" and settings.claude_key_source == ""
    assert settings.claude_base_host == "api.anthropic.com"
    assert settings.gateway_openai_base_url is None


def test_defaults_name_the_models(env):
    settings = env()
    assert settings.claude_model == "claude-opus-5"
    assert settings.embedding_model == "BGE-M3"
    assert settings.rerank_model == "BGE-Reranker-V2-M3"


def test_litellm_pair_is_the_last_resort(env):
    settings = env(LITELLM_API_KEY="sk-gw", LITELLM_API_BASE="http://10.0.0.1:8010")
    assert settings.claude_api_key == "sk-gw"
    assert settings.claude_key_source == "LITELLM_API_KEY"
    assert settings.claude_base_url == "http://10.0.0.1:8010"
    assert settings.claude_base_host == "10.0.0.1:8010"
    assert settings.gateway_openai_base_url == "http://10.0.0.1:8010/v1"


def test_anthropic_key_wins_over_litellm_and_keeps_its_own_address(env):
    """官方 key 和网关变量同时在：用官方 key，而且**不**把它发给网关。"""
    settings = env(
        ANTHROPIC_API_KEY="sk-ant",
        LITELLM_API_KEY="sk-gw",
        LITELLM_API_BASE="http://10.0.0.1:8010",
    )
    assert settings.claude_api_key == "sk-ant"
    assert settings.claude_key_source == "ANTHROPIC_API_KEY"
    assert settings.claude_base_url == ""
    assert settings.claude_base_host == "api.anthropic.com"
    # 直连官方没有向量 / 重排
    assert settings.gateway_openai_base_url is None


def test_anthropic_base_url_travels_with_the_anthropic_key(env):
    settings = env(ANTHROPIC_API_KEY="sk-ant", ANTHROPIC_BASE_URL="https://relay.example/v1")
    assert settings.claude_base_url == "https://relay.example"


def test_polaris_key_wins_over_everything(env):
    settings = env(
        POLARIS_CLAUDE_API_KEY="sk-polaris",
        ANTHROPIC_API_KEY="sk-ant",
        ANTHROPIC_BASE_URL="https://relay.example",
        LITELLM_API_KEY="sk-gw",
        LITELLM_API_BASE="http://10.0.0.1:8010",
    )
    assert settings.claude_api_key == "sk-polaris"
    assert settings.claude_key_source == "POLARIS_CLAUDE_API_KEY"
    assert settings.claude_base_url == ""


def test_explicit_polaris_base_url_overrides_the_sources_own(env):
    settings = env(
        POLARIS_CLAUDE_BASE_URL="http://override:9000/",
        LITELLM_API_KEY="sk-gw",
        LITELLM_API_BASE="http://10.0.0.1:8010",
    )
    assert settings.claude_api_key == "sk-gw"
    assert settings.claude_base_url == "http://override:9000"


def test_an_empty_higher_source_does_not_shadow_a_lower_one(env):
    settings = env(POLARIS_CLAUDE_API_KEY="", ANTHROPIC_API_KEY="  ", LITELLM_API_KEY="sk-gw")
    assert settings.claude_api_key == "sk-gw"


def test_model_can_be_overridden_by_operators(env):
    assert env(POLARIS_CLAUDE_MODEL="claude-fable-5-1").claude_model == "claude-fable-5-1"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://gw:8010", "http://gw:8010"),
        ("http://gw:8010/", "http://gw:8010"),
        ("http://gw:8010/v1", "http://gw:8010"),
        ("http://gw:8010/v1/", "http://gw:8010"),
        ("http://gw:8010/v1/messages", "http://gw:8010"),
        ("", ""),
    ],
)
def test_base_url_is_normalised_to_the_root(raw, expected):
    assert normalize_claude_base_url(raw) == expected


def test_base_host_never_carries_credentials(env):
    settings = env(LITELLM_API_KEY="sk-gw", LITELLM_API_BASE="http://user:pw@gw.example:8010/v1")
    assert settings.claude_base_host == "gw.example:8010"
