"""Translations from ``.lan`` files.

Format (UTF-8, one entry per line)::

    # comment
    @language = Polski          metadata: display name
    @version = 0.4.0            metadata: app version the file belongs to
    apply.button = Zastosuj
    toast.applied = {album}: zaktualizowano {photos} zdjęć

Placeholders use ``{name}``. A literal brace is written ``{{`` / ``}}``. Missing keys fall
back to English, then to the key itself, so a partial translation never breaks the UI.

The files shipped with the app live in ``immich_album_tagger/languages``. On start-up they are
copied to ``LANGUAGE_DIR`` (a mounted volume) when missing there or when the copy there is
from an older app version; an older copy is kept as ``<name>.lan.bak-<version>``. A copy
with the current version is left alone, so it can be edited in place.
"""

from __future__ import annotations

import logging
import shutil
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)

EXTENSION = ".lan"
FALLBACK = "english"


def parse_lan(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(messages, metadata)``."""
    messages: dict[str, str] = {}
    meta: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            log.warning("Ignoring line %d without '=' in language file: %r", number, raw)
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key.startswith("@"):
            meta[key[1:]] = value
        elif key:
            messages[key] = value.replace("\\n", "\n")
    return messages, meta


def bundled_dir() -> Path:
    return Path(str(resources.files("immich_album_tagger") / "languages"))


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("+")[0].split("."))
    except ValueError:
        return (0,)


def sync_language_files(target: Path, app_version: str, source: Path | None = None) -> list[str]:
    """Copy shipped language files into ``target``; returns the names copied."""
    source = source or bundled_dir()
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for shipped in sorted(source.glob(f"*{EXTENSION}")):
        dest = target / shipped.name
        if dest.exists():
            _, meta = parse_lan(dest.read_text(encoding="utf-8"))
            current = meta.get("version", "0")
            if _version_tuple(current) >= _version_tuple(app_version):
                continue
            backup = dest.with_name(f"{dest.name}.bak-{current}")
            shutil.copy2(dest, backup)
            log.info("Language file %s is from %s; saved as %s", dest.name, current, backup.name)
        shutil.copyfile(shipped, dest)
        copied.append(shipped.name)
    if copied:
        log.info("Language files installed in %s: %s", target, ", ".join(copied))
    return copied


class Translator:
    def __init__(
        self,
        messages: dict[str, str],
        fallback: dict[str, str],
        language: str,
        meta: dict[str, str] | None = None,
    ):
        self.messages = messages
        self.fallback = fallback
        self.language = language
        self.meta = meta or {}

    @classmethod
    def load(cls, directory: Path, language: str) -> Translator:
        def read(name: str) -> tuple[dict[str, str], dict[str, str]]:
            for folder in (directory, bundled_dir()):
                path = folder / f"{name}{EXTENSION}"
                if path.exists():
                    return parse_lan(path.read_text(encoding="utf-8"))
            return {}, {}

        fallback, fallback_meta = read(FALLBACK)
        messages, meta = read(language)
        if not messages:
            log.warning("Language file %s%s not found; using %s", language, EXTENSION, FALLBACK)
            messages, meta, language = fallback, fallback_meta, FALLBACK
        return cls(messages, fallback, language, meta)

    @classmethod
    def english(cls) -> Translator:
        """Shipped English only; for tests and the CLI."""
        messages, meta = parse_lan((bundled_dir() / f"{FALLBACK}{EXTENSION}").read_text("utf-8"))
        return cls(messages, messages, FALLBACK, meta)

    def all(self) -> dict[str, str]:
        return {**self.fallback, **self.messages}

    def __call__(self, key: str, **params: object) -> str:
        template = self.messages.get(key) or self.fallback.get(key) or key
        try:
            return template.format(**params)
        except (KeyError, IndexError, ValueError):
            log.warning("Translation %r does not match its parameters %s", key, list(params))
            return template
