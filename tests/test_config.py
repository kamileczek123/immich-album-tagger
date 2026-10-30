from pathlib import Path

import pytest

from immich_album_tagger.config import ConfigError, ImmichConfig, LLMConfig, expand_env, load_config

REPO = Path(__file__).resolve().parents[1]


def test_expand_env_with_defaults(monkeypatch):
    monkeypatch.setenv("A", "one")
    monkeypatch.delenv("B", raising=False)
    assert expand_env({"x": "${A}", "y": ["${B:-two}", "${B}"]}) == {"x": "one", "y": ["two", ""]}


@pytest.mark.parametrize("url", ["http://h:2283", "http://h:2283/", "http://h:2283/api"])
def test_immich_url_gets_api_suffix(url):
    assert ImmichConfig(url=url, api_key="k").url == "http://h:2283/api"


def test_llm_enabled_requires_endpoint():
    with pytest.raises(ValueError):
        LLMConfig(enabled=True, base_url="", model="m")


def test_shipped_config_loads(monkeypatch, tmp_path):
    monkeypatch.setenv("IMMICH_URL", "http://immich:2283")
    monkeypatch.setenv("IMMICH_API_KEY", "key")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm:8080/v1")
    monkeypatch.setenv("LLM_MODEL", "some-model")
    monkeypatch.delenv("LLM_ENABLED", raising=False)
    config = load_config(REPO / "config", tmp_path)
    assert config.immich.url == "http://immich:2283/api"
    assert config.llm.enabled is True and config.llm.model == "some-model"
    assert config.tagging.llm.language == "Polish"
    assert {rule.name for rule in config.tagging.rules.rules} >= {"year", "month", "event"}


def test_missing_key_gives_readable_error(monkeypatch, tmp_path):
    monkeypatch.setenv("IMMICH_URL", "http://immich:2283")
    monkeypatch.delenv("IMMICH_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENABLED", "false")
    with pytest.raises(ConfigError, match="IMMICH_API_KEY"):
        load_config(REPO / "config", tmp_path)


def test_rule_with_unknown_placeholder_is_rejected(tmp_path):
    (tmp_path / "immich.yaml").write_text("url: http://x\napi_key: k\n")
    (tmp_path / "tagging.yaml").write_text(
        "rules:\n  rules:\n    - name: bad\n"
        "      pattern: '(?P<year>\\d{4})'\n      tags: [x/$month]\n"
    )
    with pytest.raises(ConfigError, match="unknown placeholders"):
        load_config(tmp_path, tmp_path)


def test_section_wrapper_is_accepted(tmp_path):
    (tmp_path / "immich.yaml").write_text("immich:\n  url: http://x\n  api_key: k\n")
    (tmp_path / "llm.yaml").write_text("llm:\n  enabled: false\n")
    assert load_config(tmp_path, tmp_path).immich.url == "http://x/api"


def test_unknown_keys_are_rejected(tmp_path):
    (tmp_path / "immich.yaml").write_text("url: http://x\napi_key: k\n")
    (tmp_path / "tagging.yaml").write_text("tagging:\n  rules: {}\n  albm_name: {}\n")
    with pytest.raises(ConfigError, match="albm_name"):
        load_config(tmp_path, tmp_path)


def test_lowercase_placeholders_are_not_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("year", "WRONG")
    (tmp_path / "immich.yaml").write_text("url: http://x\napi_key: k\n")
    (tmp_path / "tagging.yaml").write_text(
        "rules:\n  rules:\n    - name: rok\n"
        "      pattern: '(?P<year>\\d{4})'\n      tags: ['czas/rok/${year}']\n"
    )
    config = load_config(tmp_path, tmp_path)
    assert config.tagging.rules.rules[0].tags == ["czas/rok/${year}"]
