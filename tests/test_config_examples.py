"""The *.example files in config/ must list every setting, with the code's default values."""

from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

from immich_album_tagger.config import (
    AppSettings,
    ImmichConfig,
    LLMConfig,
    RuleConfig,
    TaggingConfig,
    expand_env,
)

CONFIG = Path(__file__).resolve().parents[1] / "config"

# Values in the examples that deliberately differ from the code defaults.
NOT_DEFAULT = {
    "llm.enabled",  # true in the file, false when the file is missing
    "tagging.rules.rules",  # example rules
    "tagging.keywords.stopwords",  # example stopwords
}


def fields(model: type[BaseModel], prefix: str = "") -> dict[str, object]:
    out = {}
    for name, info in model.model_fields.items():
        annotation = info.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            out.update(fields(annotation, f"{prefix}{name}."))
        else:
            out[f"{prefix}{name}"] = info.get_default(call_default_factory=True)
    return out


def flatten(data: dict, prefix: str = "") -> dict[str, object]:
    out = {}
    for key, value in data.items():
        if (
            isinstance(value, dict)
            and value
            and not key.startswith("extra_")
            and key != "value_maps"
        ):
            out.update(flatten(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = value
    return out


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("immich", ImmichConfig),
        ("llm", LLMConfig),
        ("tagging", TaggingConfig),
        ("app", AppSettings),
    ],
)
def test_example_lists_every_setting_with_its_default(name, model, monkeypatch):
    for var in ("IMMICH_URL", "IMMICH_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.setenv(var, "http://example/v1" if "URL" in var else "x")
    raw = yaml.safe_load((CONFIG / f"{name}.yaml.example").read_text(encoding="utf-8"))
    model.model_validate(expand_env(raw))  # loads cleanly
    present = flatten(raw)
    expected = fields(model)
    missing = sorted(set(expected) - set(present))
    assert not missing, f"{name}.yaml.example lacks {missing}"
    for key, default in expected.items():
        value = present[key]
        if f"{name}.{key}" in NOT_DEFAULT or (isinstance(value, str) and "${" in value):
            continue
        if isinstance(default, str):
            value, default = str(value).strip(), default.strip()
        assert value == default, f"{name}.{key}: example {value!r} != default {default!r}"


def test_example_rules_document_every_rule_key():
    raw = yaml.safe_load((CONFIG / "tagging.yaml.example").read_text(encoding="utf-8"))
    first = raw["rules"]["rules"][0]
    assert set(first) == set(RuleConfig.model_fields)


def test_credentials_example():
    raw = yaml.safe_load((CONFIG / "credentials.yaml.example").read_text(encoding="utf-8"))
    assert set(raw) == {"encrypted", "username", "password"} and raw["encrypted"] is False
