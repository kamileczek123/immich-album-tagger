"""Generate tag suggestions ("variants") for an album.

A variant is one complete proposal - a list of tags - produced by one generator:

* ``album-name`` - the album title itself, via a template (default ``album/$name``)
* ``rules``      - regex rules from ``tagging.yaml`` (years, months, events, ...)
* ``keywords``   - significant words from the title minus stopwords
* ``keywords-base`` - the same words in their base form ("Zielonej" -> "zielona"), via LLM
* ``llm``        - one or more proposals from the configured LLM

Besides tag variants, a suggestion carries *keywords* for the album description: each title
word with its base form, plus a few related words, so the user can keep either or both.

All tags go through :func:`normalize_tag`, so every generator produces tags in the same
shape. Adding a new generator means adding one method to :class:`Suggester`.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from string import Template

from .config import NormalizationConfig, TaggingConfig
from .immich import Album
from .llm import LLMClient, LLMError, LLMPool, parse_keywords, parse_variants

log = logging.getLogger(__name__)

# Letters NFKD does not decompose into base letter + accent.
_TRANSLITERATE = str.maketrans(
    {
        "ł": "l",
        "Ł": "L",
        "ø": "o",
        "Ø": "O",
        "đ": "d",
        "Đ": "D",
        "ß": "ss",
        "æ": "ae",
        "Æ": "AE",
        "œ": "oe",
        "Œ": "OE",
        "ı": "i",
    }
)
_WORD = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*")
_EDGE_CHARS = " -_.,;:"
# "#" is removed too: it marks tags in photo descriptions, and titles like
# "2015-12-31 # 01-01 Sylwester" use it as a plain separator.
_PUNCTUATION = re.compile(r"[,;:!?#\"“”„«»()\[\]{}]+")


def _english(key: str, **params: object) -> str:
    """Default translator; replaced by the service with the configured language."""
    from .i18n import Translator

    return Translator.english()(key, **params)


@dataclass
class Variant:
    source: str
    label: str
    tags: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SuggestionResult:
    variants: list[Variant] = field(default_factory=list)
    # [{"word": "Zielonej", "base": "zielona"}, ...] in title order
    keywords: list[dict[str, str]] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "variants": [v.to_dict() for v in self.variants],
            "keywords": [dict(k) for k in self.keywords],
            "related": list(self.related),
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- normalisation


def strip_diacritics(text: str) -> str:
    text = text.translate(_TRANSLITERATE)
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_tag(raw: str, cfg: NormalizationConfig) -> str | None:
    """Clean one tag. ``/`` separates hierarchy levels, as in Immich."""
    parts: list[str] = []
    for part in str(raw).split("/"):
        text = unicodedata.normalize("NFC", part)
        text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
        text = " ".join(_PUNCTUATION.sub(" ", text).split())
        if cfg.strip_diacritics:
            text = strip_diacritics(text)
        if cfg.lowercase:
            text = text.lower()
        if cfg.space_replacement != " ":
            text = text.replace(" ", cfg.space_replacement)
            if cfg.space_replacement:
                # "a - b" -> "a---b" -> "a-b"
                run = f"(?:{re.escape(cfg.space_replacement)}|-)+"
                text = re.sub(run, cfg.space_replacement, text)
        text = text.strip(_EDGE_CHARS)
        if text:
            parts.append(text)
    if not parts:
        return None
    tag = "/".join(parts[: cfg.max_depth])
    if len(tag) > cfg.max_length:
        tag = tag[: cfg.max_length].rstrip("/" + _EDGE_CHARS)
    return tag or None


def normalize_tags(raw_tags: Iterable[str], cfg: NormalizationConfig) -> list[str]:
    """Normalise and de-duplicate (case-insensitively), keeping the original order."""
    result: list[str] = []
    seen: set[str] = set()
    for raw in raw_tags:
        tag = normalize_tag(raw, cfg)
        if tag and tag.casefold() not in seen:
            seen.add(tag.casefold())
            result.append(tag)
    return result


def _flat(value: str) -> str:
    """Values inserted into templates must not create hierarchy levels by accident."""
    return " ".join(value.replace("/", " ").split())


# --------------------------------------------------------------------------- suggester


class Suggester:
    def __init__(self, config: TaggingConfig, llm: LLMClient | LLMPool | None = None):
        self.config = config
        self.llm = llm
        self.t: Callable[..., str] = _english
        self._rules = [(rule, rule.compiled()) for rule in config.rules.rules]
        self._stopwords = {word.casefold() for word in config.keywords.stopwords}

    def suggest(self, album: Album, existing_tags: Sequence[str] = ()) -> SuggestionResult:
        result = SuggestionResult()
        for build in (self.album_name_variant, self.rules_variant, self.keywords_variant):
            variant = build(album)
            if variant and variant.tags:
                result.variants.append(variant)

        # The two LLM requests (base forms of title words, tag variants) run side by side;
        # the LLM pool decides how many requests really go out at once.
        keyword_warnings: list[str] = []
        llm_job = None
        with ThreadPoolExecutor(max_workers=1) as executor:
            if self.llm is not None:
                llm_job = executor.submit(
                    contextvars.copy_context().run, self.llm_variants, album, existing_tags
                )
            result.keywords, result.related = self.keyword_suggestions(album, keyword_warnings)
            result.warnings.extend(keyword_warnings)
            base_variant = self.base_forms_variant(result.keywords)
            if base_variant:
                result.variants.append(base_variant)
            if llm_job is not None:
                try:
                    result.variants.extend(llm_job.result())
                except LLMError as exc:
                    log.warning("LLM suggestion failed for %r: %s", album.name, exc)
                    result.warnings.append(self.t("warning.llm_unavailable", error=exc))
        result.variants = _dedupe_variants(result.variants)
        return result

    # ------------------------------------------------------------------ generators

    def album_name_variant(self, album: Album) -> Variant | None:
        cfg = self.config.album_name
        if not cfg.enabled or not album.name.strip():
            return None
        raw = Template(cfg.template).safe_substitute(name=_flat(album.name))
        return Variant("album-name", cfg.label, normalize_tags([raw], self.config.normalization))

    def rules_variant(self, album: Album) -> Variant | None:
        cfg = self.config.rules
        if not cfg.enabled or not self._rules:
            return None
        raw_tags: list[str] = []
        for rule, regex in self._rules:
            match = regex.search(album.name)
            if not match:
                continue
            values = {"name": _flat(album.name)}
            for group, value in match.groupdict().items():
                if value is None:
                    continue
                mapping = rule.value_maps.get(group, {})
                mapped = mapping.get(value, mapping.get(value.casefold(), value))
                values[group] = _flat(mapped)
            for template in rule.tags:
                tpl = Template(template)
                if all(name in values for name in tpl.get_identifiers()):
                    raw_tags.append(tpl.substitute(values))
        return Variant("rules", cfg.label, normalize_tags(raw_tags, self.config.normalization))

    def title_words(self, album: Album) -> list[str]:
        """Significant words of the title, original spelling, first occurrence only."""
        cfg = self.config.keywords
        words: list[str] = []
        seen: set[str] = set()
        for word in _WORD.findall(album.name):
            if len(word) < cfg.min_length or word.casefold() in self._stopwords:
                continue
            if cfg.skip_numbers and not any(ch.isalpha() for ch in word):  # 2019, 2019-07
                continue
            if word.casefold() not in seen:
                seen.add(word.casefold())
                words.append(word)
        return words

    def keywords_variant(self, album: Album) -> Variant | None:
        cfg = self.config.keywords
        if not cfg.enabled:
            return None
        raw_tags = [Template(cfg.template).safe_substitute(word=w) for w in self.title_words(album)]
        tags = normalize_tags(raw_tags, self.config.normalization)[: cfg.max_tags]
        return Variant("keywords", cfg.label, tags)

    def keyword_suggestions(
        self, album: Album, warnings: list[str]
    ) -> tuple[list[dict[str, str]], list[str]]:
        """Title words with base forms ("Zielonej" -> "zielona") and related words."""
        words = self.title_words(album)
        plain = [{"word": w, "base": w.lower()} for w in words]
        cfg = self.config.llm
        if self.llm is None or not cfg.normalize_keywords or not words:
            return plain, []
        system = Template(cfg.keywords_prompt).safe_substitute(
            language=cfg.language, related=cfg.related_keywords
        )
        user = f"Album title: {album.name}\nWords: {json.dumps(words, ensure_ascii=False)}"
        try:
            keywords, related = parse_keywords(self.llm.complete(system, user), words)
        except LLMError as exc:
            log.warning("Keyword normalisation failed for %r: %s", album.name, exc)
            warnings.append(self.t("warning.keywords_unavailable", error=exc))
            return plain, []
        known = {k["base"].casefold() for k in keywords} | {k["word"].casefold() for k in keywords}
        related = [r for r in dict.fromkeys(related) if r.casefold() not in known]
        return keywords, related[: cfg.related_keywords]

    def base_forms_variant(self, keywords: list[dict[str, str]]) -> Variant | None:
        """Keyword tags in base form, only when that differs from the plain keyword variant."""
        cfg = self.config.keywords
        if not cfg.enabled or all(k["base"] == k["word"].lower() for k in keywords):
            return None
        raw = [Template(cfg.template).safe_substitute(word=k["base"]) for k in keywords]
        tags = normalize_tags(raw, self.config.normalization)[: cfg.max_tags]
        return Variant("keywords-base", f"{cfg.label} (base forms)", tags)

    def llm_variants(self, album: Album, existing_tags: Sequence[str]) -> list[Variant]:
        if self.llm is None:
            return []
        cfg = self.config.llm
        existing = list(existing_tags)[: cfg.existing_tags_limit]
        system = Template(cfg.system_prompt).safe_substitute(
            variants=cfg.variants, max_tags=cfg.max_tags_per_variant, language=cfg.language
        )
        user = Template(cfg.user_prompt).safe_substitute(
            title=album.name,
            description=album.description or "(none)",
            asset_count=album.asset_count,
            date_range=album.date_range or "(unknown)",
            existing_tags=", ".join(existing) if existing else "(none yet)",
            language=cfg.language,
        )
        reply = self.llm.complete(system, user)
        variants = []
        for label, raw_tags in parse_variants(reply)[: cfg.variants]:
            tags = normalize_tags(raw_tags, self.config.normalization)
            tags = tags[: cfg.max_tags_per_variant]
            if tags:
                variants.append(Variant("llm", label, tags))
        return variants


def _dedupe_variants(variants: list[Variant]) -> list[Variant]:
    seen: set[frozenset[str]] = set()
    unique = []
    for variant in variants:
        key = frozenset(tag.casefold() for tag in variant.tags)
        if key and key not in seen:
            seen.add(key)
            unique.append(variant)
    return unique
