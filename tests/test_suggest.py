import pytest

from immich_album_tagger.config import NormalizationConfig
from immich_album_tagger.immich import Album
from immich_album_tagger.llm import LLMError, parse_variants
from immich_album_tagger.suggest import Suggester, normalize_tag, normalize_tags


def album(name, **kw):
    return Album(id="a1", name=name, asset_count=kw.pop("asset_count", 10), **kw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Miejsce / Chorwacja /  Stare Miasto ", "miejsce/chorwacja/stare-miasto"),
        ("  ", None),
        ("a//b/", "a/b"),
        ("-Wesele-", "wesele"),
        ("Durrës - mieszkanie, odbiór (2024)", "durrës-mieszkanie-odbiór-2024"),
        ("2015-12-31 # 01-01 Sylwester", "2015-12-31-01-01-sylwester"),
    ],
)
def test_normalize_tag(raw, expected):
    assert normalize_tag(raw, NormalizationConfig()) == expected


def test_strip_diacritics_handles_polish_letters():
    cfg = NormalizationConfig(strip_diacritics=True)
    assert normalize_tag("Łódź Żółć", cfg) == "lodz-zolc"


def test_max_depth_and_length():
    cfg = NormalizationConfig(max_depth=2, max_length=7)
    assert normalize_tag("a/b/c/d", cfg) == "a/b"
    assert normalize_tag("abcdefghij", cfg) == "abcdefg"


def test_normalize_tags_dedupes_case_insensitively():
    cfg = NormalizationConfig(lowercase=False)
    assert normalize_tags(["Rok/2019", "rok/2019", "Lato"], cfg) == ["Rok/2019", "Lato"]


def test_rule_based_variants(tagging_config):
    result = Suggester(tagging_config).suggest(album("2019-07 Wesele Ani w Krakowie"))
    by_source = {v.source: v.tags for v in result.variants}
    assert by_source["album-name"] == ["album/2019-07-wesele-ani-w-krakowie"]
    assert by_source["rules"] == ["rok/2019", "miesiac/lipiec", "wydarzenie/wesele"]
    assert by_source["keywords"] == ["wesele", "ani", "krakowie"]
    assert result.warnings == []


def test_slash_in_title_does_not_create_hierarchy(tagging_config):
    variant = Suggester(tagging_config).album_name_variant(album("Chorwacja/Split"))
    assert variant.tags == ["album/chorwacja-split"]


class FakeLLM:
    """Records every prompt; ``prompts`` is the tag-variant one (requests run in parallel)."""

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    @property
    def prompts(self):
        return next(
            (c for c in self.calls if "variants" in c[0]), self.calls[-1] if self.calls else None
        )

    def complete(self, system, user):
        self.calls.append((system, user))
        if self.error:
            raise self.error
        return self.reply


def test_llm_variants_are_normalised_and_capped(tagging_config):
    reply = (
        "<think>hmm</think>```json\n"
        '{"variants": [{"label": "Minimal", "tags": ["Miejsce/Chorwacja", "Rok/2019"]},'
        ' {"label": "Detailed", "tags": ["a","b","c","d","e","f","g","h","i","j"]}]}\n```'
    )
    llm = FakeLLM(reply)
    result = Suggester(tagging_config, llm).suggest(album("Chorwacja 2019"), ["miejsce/polska"])
    llm_variants = [v for v in result.variants if v.source == "llm"]
    assert llm_variants[0].tags == ["miejsce/chorwacja", "rok/2019"]
    assert len(llm_variants[1].tags) == tagging_config.llm.max_tags_per_variant
    system, user = llm.prompts
    assert "Polish" in system and "exactly 3 variants" in system
    assert "Chorwacja 2019" in user and "miejsce/polska" in user


def test_llm_failure_becomes_warning(tagging_config):
    result = Suggester(tagging_config, FakeLLM(error=LLMError("timeout"))).suggest(album("Lato"))
    assert result.variants  # rule-based variants still there
    assert "timeout" in result.warnings[0]


def test_identical_variants_are_merged(tagging_config):
    tagging_config.album_name.template = "$name"
    result = Suggester(tagging_config).suggest(album("Wakacje"))
    assert [v.source for v in result.variants] == ["album-name", "rules"]


@pytest.mark.parametrize(
    "reply",
    [
        '{"variants": [{"label": "x", "tags": ["a"]}]}',
        '[{"label": "x", "tags": ["a"]}]',
        'Sure! Here you go: {"variants": [{"tags": ["a"]}]} Hope it helps.',
        '[["a"]]',
    ],
)
def test_parse_variants_accepts_common_shapes(reply):
    assert parse_variants(reply)[0][1] == ["a"]


def test_parse_variants_rejects_garbage():
    with pytest.raises(LLMError):
        parse_variants("no json here")


class ScriptedLLM:
    """Answers the keyword prompt and the variants prompt differently."""

    def __init__(self, keywords_reply, variants_reply='{"variants": []}'):
        self.keywords_reply, self.variants_reply = keywords_reply, variants_reply

    def complete(self, system, user):
        return self.keywords_reply if "Words:" in user else self.variants_reply


def test_keywords_get_base_forms_and_related(tagging_config):
    reply = (
        '{"keywords": [{"word": "Sylwester", "base": "sylwester"},'
        ' {"word": "Zielonej", "base": "Zielona"}],'
        ' "related": ["nowy rok", "Sylwester", "impreza"]}'
    )
    result = Suggester(tagging_config, ScriptedLLM(reply)).suggest(album("Sylwester na Zielonej"))
    assert result.keywords == [
        {"word": "Sylwester", "base": "sylwester"},
        {"word": "Zielonej", "base": "zielona"},
    ]
    assert result.related == ["nowy rok", "impreza"]
    base = next(v for v in result.variants if v.source == "keywords-base")
    assert base.tags == ["sylwester", "zielona"]


def test_keywords_without_llm_are_plain(tagging_config):
    result = Suggester(tagging_config).suggest(album("Sylwester na Zielonej"))
    assert result.keywords == [
        {"word": "Sylwester", "base": "sylwester"},
        {"word": "Zielonej", "base": "zielonej"},
    ]
    assert not any(v.source == "keywords-base" for v in result.variants)


def test_keyword_llm_failure_is_a_warning(tagging_config):
    result = Suggester(tagging_config, ScriptedLLM("garbage")).suggest(album("Sylwester"))
    assert result.keywords == [{"word": "Sylwester", "base": "sylwester"}]
    assert any("Base forms" in w for w in result.warnings)
