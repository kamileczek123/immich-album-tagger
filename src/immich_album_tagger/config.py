"""Configuration loading and validation.

YAML files in ``CONFIG_DIR`` (default ``./config``):

* ``immich.yaml``      - how to reach the Immich server
* ``llm.yaml``         - the OpenAI-compatible LLM endpoint used for suggestions (optional)
* ``tagging.yaml``     - how suggestions are generated and how tags are normalised (optional)
* ``app.yaml``         - language, UI behaviour, sharing defaults, login session (optional)
* ``credentials.yaml`` - login for the web UI (optional, see auth.py)

Any string value may reference environment variables as ``${NAME}`` or
``${NAME:-default}`` (upper-case names only), so secrets never have to be written into the
files. Unknown keys are rejected, so typos and misplaced sections fail loudly.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from string import Template
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# Only UPPER_CASE names are environment variables. Lower-case ${name} is left alone so tag
# templates such as "czas/rok/${year}" keep working.
_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid."""


class StrictModel(BaseModel):
    """Base for all config sections: unknown keys are an error, not silently ignored."""

    model_config = ConfigDict(extra="forbid")


def expand_env(value: Any) -> Any:
    """Recursively replace ``${NAME}`` / ``${NAME:-default}`` with environment values."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    return value


# --------------------------------------------------------------------------- immich


class ImmichConfig(StrictModel):
    url: str
    # Required, except with app.auth.mode immich_keys, where the web UI creates a key per
    # login and only the CLI needs this one.
    api_key: str = ""
    verify_tls: bool = True
    timeout_seconds: float = 30.0
    # "filter" = search API from Immich 3.2+, "legacy" = older albumIds/page search,
    # "auto" = try "filter" first and fall back to "legacy" if the server rejects it.
    search_api: Literal["auto", "filter", "legacy"] = "auto"
    page_size: int = Field(1000, ge=1, le=1000)
    tag_batch_size: int = Field(1000, ge=1)
    include_archived: bool = True
    verify_after_apply: bool = True
    # Immich processes tag and description changes in background jobs (sidecar write,
    # metadata re-read). Verify only after this delay, otherwise a library that silently
    # drops the changes (e.g. a read-only external library) looks fine.
    verify_delay_seconds: float = Field(5.0, ge=0, le=120)
    # If something is still missing, check again this many times, this far apart, before
    # warning. Large albums keep Immich's job queue busy for a while.
    verify_retries: int = Field(3, ge=0, le=20)
    verify_retry_seconds: float = Field(5.0, ge=0, le=120)

    @field_validator("url")
    @classmethod
    def _normalise_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            raise ValueError("immich.url is empty (set IMMICH_URL)")
        if not value.startswith(("http://", "https://")):
            raise ValueError("immich.url must start with http:// or https://")
        return value if value.endswith("/api") else f"{value}/api"

    @field_validator("api_key")
    @classmethod
    def _strip_key(cls, value: str) -> str:
        return value.strip()


# --------------------------------------------------------------------------- llm


class LLMEndpoint(StrictModel):
    """One more model / endpoint used next to the main one (llm.endpoints)."""

    model: str
    # Missing values come from the main endpoint (same gateway, other model).
    base_url: str = ""
    api_key: str = ""
    max_concurrent: int | None = Field(None, ge=1, le=32)
    extra_headers: dict[str, str] | None = None
    extra_body: dict[str, Any] | None = None

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")


class LLMConfig(StrictModel):
    # true / false, or "auto": on when base_url and model are both set (the shipped llm.yaml
    # uses auto, so the app starts without any LLM settings and simply has no LLM).
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.3
    max_tokens: int = 2048
    timeout_seconds: float = 90.0
    verify_tls: bool = True
    # Send response_format={"type": "json_object"}. Only enable if your server supports it.
    json_mode: bool = False
    extra_headers: dict[str, str] = Field(default_factory=dict)
    # Merged into the request body, e.g. {"chat_template_kwargs": {"enable_thinking": false}}.
    extra_body: dict[str, Any] = Field(default_factory=dict)
    # Requests sent to this endpoint at the same time (llama-server: its --parallel slots).
    max_concurrent: int = Field(2, ge=1, le=32)
    # More models / endpoints used side by side with the one above: round robin, and the
    # next one when one fails. Each needs a model; the rest defaults to the values above.
    endpoints: list[LLMEndpoint] = Field(default_factory=list)

    def all_endpoints(self) -> list[LLMConfig]:
        """The main endpoint and every entry of ``endpoints`` as complete configs."""
        result = [self.model_copy(update={"endpoints": []})]
        for extra in self.endpoints:
            update: dict[str, Any] = {"model": extra.model, "endpoints": []}
            for name in ("base_url", "api_key", "max_concurrent", "extra_headers", "extra_body"):
                value = getattr(extra, name)
                if value not in (None, ""):
                    update[name] = value
            result.append(self.model_copy(update=update))
        return result

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @model_validator(mode="before")
    @classmethod
    def _resolve_auto(cls, data: Any) -> Any:
        if isinstance(data, dict) and str(data.get("enabled", "")).strip().lower() == "auto":
            configured = (
                str(data.get("base_url") or "").strip() and str(data.get("model") or "").strip()
            )
            data = {**data, "enabled": bool(configured)}
        return data

    @model_validator(mode="after")
    def _check_enabled(self) -> LLMConfig:
        if self.enabled and (not self.base_url or not self.model):
            raise ValueError("llm.enabled is true but base_url or model is empty")
        return self


# --------------------------------------------------------------------------- tagging

DEFAULT_SYSTEM_PROMPT = """\
You are a photo librarian. You receive the title (and sometimes a description and date range)
of a photo album. Propose tags that will be attached to EVERY photo in that album.

Rules:
- Reply with ONLY a JSON object, no prose and no markdown fences.
- Schema: {"variants": [{"label": "<short name of the approach>", "tags": ["tag", "..."]}]}
- Return exactly $variants variants that differ meaningfully, for example minimal vs detailed,
  or place-centred vs event-centred.
- At most $max_tags tags per variant.
- Write tags in $language.
- Tags may be hierarchical with "/" as separator, e.g. "place/croatia/split", "event/wedding".
  Use at most 3 levels.
- Prefer tags from the existing tag list when they fit. Never invent near-duplicates of them.
- Use only information from the title, description and dates, plus very safe generalisations
  (a city implies its country). Do not guess people's names or facts not given.
"""

DEFAULT_KEYWORDS_PROMPT = """\
You normalise words from a photo album title so they can be found by a simple text search.
Language: $language.

Reply with ONLY a JSON object, no prose and no markdown fences:
{"keywords": [{"word": "<word exactly as in the input>", "base": "<base form>"}],
 "related": ["<word or short phrase>", "..."]}

- "keywords": one entry per input word, in the same order. "base" is the dictionary form
  (nominative singular for nouns, e.g. "Zielonej" -> "zielona", "Krakowie" -> "kraków"),
  lower case, keeping diacritics. For names of places use their usual name.
- "related": at most $related other words or short phrases a person would likely type to
  find this album (e.g. "nowy rok" for a New Year's Eve album). Only safe, obvious ones.
"""

DEFAULT_USER_PROMPT = """\
Album title: $title
Album description: $description
Number of photos: $asset_count
Date range: $date_range
Existing tags (reuse when they fit): $existing_tags
"""


class NormalizationConfig(StrictModel):
    lowercase: bool = True
    space_replacement: str = "-"
    strip_diacritics: bool = False
    max_length: int = Field(100, ge=1)
    max_depth: int = Field(4, ge=1)


class AlbumNameVariantConfig(StrictModel):
    enabled: bool = True
    label: str = "Album name"
    # string.Template; available placeholder: $name
    template: str = "album/$name"


class RuleConfig(StrictModel):
    name: str
    pattern: str
    tags: list[str]
    ignore_case: bool = True
    # Translate captured values, e.g. {"month": {"07": "july"}}.
    value_maps: dict[str, dict[str, str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self) -> RuleConfig:
        try:
            regex = re.compile(self.pattern, re.IGNORECASE if self.ignore_case else 0)
        except re.error as exc:
            raise ValueError(f"rule {self.name!r}: invalid regex: {exc}") from exc
        groups = set(regex.groupindex) | {"name"}
        for template in self.tags:
            unknown = set(Template(template).get_identifiers()) - groups
            if unknown:
                raise ValueError(
                    f"rule {self.name!r}: template {template!r} uses unknown "
                    f"placeholders {sorted(unknown)}; named groups are {sorted(groups)}"
                )
        return self

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern, re.IGNORECASE if self.ignore_case else 0)


class RulesVariantConfig(StrictModel):
    enabled: bool = True
    label: str = "Rules"
    rules: list[RuleConfig] = Field(default_factory=list)


class KeywordsVariantConfig(StrictModel):
    enabled: bool = True
    label: str = "Title keywords"
    template: str = "$word"
    min_length: int = Field(3, ge=1)
    skip_numbers: bool = True
    max_tags: int = Field(8, ge=1)
    stopwords: list[str] = Field(default_factory=list)


class LLMTaggingConfig(StrictModel):
    variants: int = Field(3, ge=1, le=6)
    max_tags_per_variant: int = Field(8, ge=1, le=30)
    language: str = "English"
    existing_tags_limit: int = Field(300, ge=0)
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    user_prompt: str = DEFAULT_USER_PROMPT
    # Base forms of title words ("Zielonej" -> "zielona") for album keywords.
    normalize_keywords: bool = True
    related_keywords: int = Field(4, ge=0, le=10)
    keywords_prompt: str = DEFAULT_KEYWORDS_PROMPT


class PhotoDescriptionConfig(StrictModel):
    # Each tag is written as <prefix><tag>, e.g. "#miejsce/polska". The app owns every
    # description line made only of such words and replaces it on the next apply.
    tag_prefix: str = Field("#", min_length=1)


class AlbumDescriptionConfig(StrictModel):
    # The app owns the album description line starting with this label.
    label: str = Field("Słowa kluczowe: ", min_length=1)


class DescriptionsConfig(StrictModel):
    photo: PhotoDescriptionConfig = Field(default_factory=PhotoDescriptionConfig)
    album: AlbumDescriptionConfig = Field(default_factory=AlbumDescriptionConfig)


class ApplyDefaultsConfig(StrictModel):
    """Which targets are ticked when the panel opens (the user can change them)."""

    tags: bool = True
    photo_description: bool = False
    album_description: bool = False


class PrefetchConfig(StrictModel):
    """Prepare suggestions in the background for the next albums not tagged yet."""

    enabled: bool = True
    # How many untagged albums (in list order) to keep ready. After an album is applied it
    # leaves the window and the next one is prepared.
    albums: int = Field(10, ge=1, le=100)
    # Prepared suggestions older than this are prepared again.
    max_age_minutes: int = Field(240, ge=1)
    # After a failed attempt (e.g. LLM down), wait this long before retrying that album.
    retry_after_minutes: int = Field(5, ge=1)
    # Albums prepared at the same time. 0 = as many as the LLM endpoints take at once
    # (sum of max_concurrent), 1 without LLM.
    workers: int = Field(0, ge=0, le=16)


class TaggingConfig(StrictModel):
    normalization: NormalizationConfig = Field(default_factory=NormalizationConfig)
    album_name: AlbumNameVariantConfig = Field(default_factory=AlbumNameVariantConfig)
    rules: RulesVariantConfig = Field(default_factory=RulesVariantConfig)
    keywords: KeywordsVariantConfig = Field(default_factory=KeywordsVariantConfig)
    llm: LLMTaggingConfig = Field(default_factory=LLMTaggingConfig)
    descriptions: DescriptionsConfig = Field(default_factory=DescriptionsConfig)
    apply: ApplyDefaultsConfig = Field(default_factory=ApplyDefaultsConfig)
    prefetch: PrefetchConfig = Field(default_factory=PrefetchConfig)
    # Variants put into "Tags to apply" when the panel opens an album not tagged yet, merged
    # in this order: first (the first variant shown), album-name, rules, keywords,
    # keywords-base, llm (the first LLM variant), llm-all (every LLM variant). [] = none.
    preselect: list[
        Literal["first", "album-name", "rules", "keywords", "keywords-base", "llm", "llm-all"]
    ] = Field(default_factory=lambda: ["first"])


# --------------------------------------------------------------------------- app.yaml


class UIConfig(StrictModel):
    # true: Apply/Share return at once, the panel closes and the result appears as a
    # notification in the top left corner. false: wait in the panel for the result.
    async_actions: bool = True
    # How long a notification stays on screen.
    toast_ms: int = Field(5000, ge=1000, le=120_000)
    # Take colors and font from the running Immich (its web UI's CSS variables), so the app
    # follows Immich's look; built-in Immich-like colors are used when it cannot be read.
    immich_theme: bool = True
    # Where to read them: an Immich web address the app's server can reach. Empty = the
    # immich.url (without /api).
    immich_theme_url: str = ""
    # Minutes between re-reads of the theme (picks up Immich upgrades without a restart).
    immich_theme_refresh_minutes: float = Field(60, ge=1, le=24 * 60)
    # Immich's address as the browser sees it, for links (album covers open the album in
    # Immich). Empty: the domain root when the app runs under a path of Immich's host name
    # (/IAT/), else no links.
    immich_public_url: str = ""


class SharingConfig(StrictModel):
    # E-mails filled in automatically in the share panel and the bulk bar.
    default_users: list[str] = Field(default_factory=list)
    default_role: Literal["viewer", "editor"] = "viewer"


# Permissions of the API keys created per login (mode immich_keys), with the features that
# need them. "apiKey.delete" is always added: a session key deletes itself on logout.
KEY_PERMISSIONS: dict[str, str] = {
    "user.read": "the app itself (who is logged in), sharing",
    "album.read": "album list",
    "album.update": "album description",
    "albumUser.create": "sharing",
    "albumUser.delete": "removing access",
    "asset.read": "photos of an album",
    "asset.update": "photo descriptions",
    "asset.view": "thumbnails",
    "tag.read": "tags",
    "tag.create": "tags",
    "tag.asset": "tags",
}


class AuthConfig(StrictModel):
    # How the web UI is protected:
    #   auto        - credentials.yaml if it exists (or APP_USERNAME), otherwise no login
    #   immich      - log in with the e-mail and password of an Immich account
    #   immich_keys - like immich, but everyone works with their own Immich account: a
    #                 temporary API key is created at login and deleted at logout
    #   credentials - credentials.yaml in CONFIG_DIR (required)
    #   none        - no login (protect the port another way)
    mode: Literal["auto", "immich", "immich_keys", "credentials", "none"] = "auto"
    # E-mails or user ids allowed to log in. Empty: mode immich - only the owner of the API
    # key (everyone allowed acts with that key); mode immich_keys - every Immich account.
    allowed_users: list[str] = Field(default_factory=list)
    # mode immich_keys: permissions of the key created at login.
    key_permissions: list[str] = Field(default_factory=lambda: list(KEY_PERMISSIONS))
    # mode immich_keys: how often keys of expired sessions are deleted.
    key_sweep_minutes: float = Field(5, gt=0, le=24 * 60)
    session_hours: float = Field(12, gt=0, le=24 * 365)
    # Send the session cookie only over HTTPS (enable behind a TLS reverse proxy).
    secure_cookie: bool = False


class AppSettings(StrictModel):
    # Name of the translation file in LANGUAGE_DIR without ".lan", e.g. polish, english.
    language: str = "english"
    ui: UIConfig = Field(default_factory=UIConfig)
    sharing: SharingConfig = Field(default_factory=SharingConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)

    @field_validator("language")
    @classmethod
    def _plain_name(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]+", value):
            raise ValueError("language must be a file name like 'polish' (letters, digits, - _)")
        return value


# --------------------------------------------------------------------------- app


class AppConfig(StrictModel):
    immich: ImmichConfig
    llm: LLMConfig = Field(default_factory=LLMConfig)
    tagging: TaggingConfig = Field(default_factory=TaggingConfig)
    app: AppSettings = Field(default_factory=AppSettings)
    config_dir: Path = Path("config")
    data_dir: Path = Path("data")
    language_dir: Path = Path("language")
    log_dir: Path = Path("logs")

    @model_validator(mode="after")
    def _require_key(self) -> AppConfig:
        if not self.immich.api_key and self.app.auth.mode != "immich_keys":
            raise ValueError("immich.api_key is empty (set IMMICH_API_KEY)")
        return self


def _read_yaml(path: Path, required: bool, section: str) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigError(f"Missing config file: {path}")
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    # Accept files wrapped in their own section name ("immich:" in immich.yaml).
    if list(data) == [section] and isinstance(data[section], dict):
        data = data[section]
    return expand_env(data)


def load_config(
    config_dir: str | Path | None = None,
    data_dir: str | Path | None = None,
    language_dir: str | Path | None = None,
    log_dir: str | Path | None = None,
) -> AppConfig:
    """Load and validate the config files (immich.yaml required, the others optional)."""
    config_dir = Path(config_dir or os.environ.get("CONFIG_DIR", "config"))
    data_dir = Path(data_dir or os.environ.get("DATA_DIR", "data"))
    raw = {
        "immich": _read_yaml(config_dir / "immich.yaml", True, "immich"),
        "llm": _read_yaml(config_dir / "llm.yaml", False, "llm"),
        "tagging": _read_yaml(config_dir / "tagging.yaml", False, "tagging"),
        "app": _read_yaml(config_dir / "app.yaml", False, "app"),
        "config_dir": config_dir,
        "data_dir": data_dir,
        "language_dir": Path(language_dir or os.environ.get("LANGUAGE_DIR", "language")),
        "log_dir": Path(log_dir or os.environ.get("LOG_DIR", "logs")),
    }
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            location = ".".join(str(part) for part in err["loc"])
            lines.append(f"  {location}: {err['msg']}")
        raise ConfigError("Invalid configuration:\n" + "\n".join(lines)) from exc
