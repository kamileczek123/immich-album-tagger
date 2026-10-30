"""Managed lines inside free-text descriptions.

The app owns exactly one line in a photo description (the tag line) and one line in an
album description (the keyword line). Everything else the user wrote is kept as is, and
applying again replaces the app's line instead of adding a second one.

Photo, tags ["miejsce/polska", "zielona"], prefix "#":

    Fajerwerki o północy
    #miejsce/polska #zielona

Album, keywords ["sylwester", "zielona"], label "Słowa kluczowe: ":

    Sylwester u znajomych.
    Słowa kluczowe: sylwester zielona
"""

from __future__ import annotations

from collections.abc import Sequence


def render_tag_line(tags: Sequence[str], prefix: str) -> str:
    return " ".join(f"{prefix}{tag}" for tag in tags)


def is_tag_line(line: str, prefix: str) -> bool:
    words = line.split()
    return bool(words) and all(
        word.startswith(prefix) and len(word) > len(prefix) for word in words
    )


def render_keyword_line(keywords: Sequence[str], label: str) -> str:
    return f"{label}{' '.join(keywords)}" if keywords else ""


def is_keyword_line(line: str, label: str) -> bool:
    return line.strip().casefold().startswith(label.strip().casefold())


def _merge(text: str, new_line: str, owned) -> str:
    kept = [line for line in (text or "").splitlines() if not owned(line)]
    while kept and not kept[-1].strip():
        kept.pop()
    if new_line:
        kept.append(new_line)
    return "\n".join(kept)


def merge_tag_line(description: str, tags: Sequence[str], prefix: str) -> str:
    """Replace the tag line in a photo description (or add it)."""
    return _merge(
        description, render_tag_line(tags, prefix), lambda line: is_tag_line(line, prefix)
    )


def merge_keyword_line(description: str, keywords: Sequence[str], label: str) -> str:
    """Replace the keyword line in an album description (or add it)."""
    return _merge(
        description, render_keyword_line(keywords, label), lambda line: is_keyword_line(line, label)
    )


def tag_line_of(description: str, prefix: str) -> str:
    lines = [line for line in (description or "").splitlines() if is_tag_line(line, prefix)]
    return lines[-1] if lines else ""


def normalize_keywords(raw: Sequence[str]) -> list[str]:
    """Trim, lower-case, collapse whitespace, de-duplicate; keep phrases like "nowy rok"."""
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        word = " ".join(str(item).replace("\n", " ").split()).lower()
        if word and word.casefold() not in seen:
            seen.add(word.casefold())
            result.append(word)
    return result
