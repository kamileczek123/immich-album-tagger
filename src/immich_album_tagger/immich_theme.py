"""Colors and font taken from the running Immich, so the app follows Immich's look.

Immich's web UI is one SvelteKit build; its main stylesheet (``/_app/immutable/assets/*.css``,
linked from the index page, names change with every release) defines the design tokens as
CSS variables::

    :root,.light { --immich-ui-primary-500:#4250af; --immich-ui-light-100:var(--color-neutral-100) }
    .dark        { --immich-ui-primary-500:#accbfa; ... }
    :root,:host  { --color-neutral-100:#f5f5f5; ... }      (Tailwind palette they refer to)
    :root        { --immich-bg:255 255 255; --immich-dark-bg:10 10 10; ... }
    @font-face   { font-family:GoogleSans; src:url(/_app/immutable/assets/GoogleSans.<hash>.ttf) }

:class:`ImmichTheme` fetches the index page and its stylesheets, keeps only those variables
(the ``--immich*`` ones plus every variable they refer to) and the GoogleSans font faces,
and serves them as a small stylesheet. ``static/style.css`` uses the variables with built-in
fallbacks (``var(--immich-ui-primary-500, #4250af)``), so the app looks right with or without
Immich, and follows Immich when its colors or font change. Fonts are passed through the app
(``font(name)``), because the browser may not reach ``IMMICH_URL``; only files named in the
stylesheet are served. Each block appears twice in Immich's CSS (plain colors, then lab()
colors inside ``@supports``); the first, universally supported value is used.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from urllib.parse import urljoin

import httpx

log = logging.getLogger(__name__)

FONT_URL = "immich-theme/fonts/"  # relative to the app's root, like every other URL

_CSS_LINK = re.compile(r"""(?:href|src)=["']?([^"' >]*/_app/immutable/assets/[^"' >]+\.css)""")
_BLOCK = re.compile(r"([^{}]+)\{([^{}]*)\}")
_DECL = re.compile(r"(--[\w-]+)\s*:\s*([^;{}]+)")
_VAR_REF = re.compile(r"var\(\s*(--[\w-]+)")
_FONT_FACE = re.compile(r"@font-face\s*\{([^{}]*)\}")
_FONT_URL = re.compile(r"url\(\s*[\"']?([^\"')]+)[\"']?\s*\)")
_FONT_FILE = re.compile(r"^[\w.-]+\.(?:ttf|otf|woff2?)$")
_FONT_TYPES = {"ttf": "font/ttf", "otf": "font/otf", "woff": "font/woff", "woff2": "font/woff2"}


def _scope(selector: str) -> str | None:
    """'light' for :root / .light blocks, 'dark' for .dark, None for anything else."""
    parts = {part.strip() for part in selector.split(",")}
    if parts == {".dark"}:
        return "dark"
    if parts and parts <= {":root", ":host", ".light"}:
        return "light"
    return None


def extract(css_files: list[str]) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Variables per scope ({"light": {...}, "dark": {...}}) and GoogleSans font faces."""
    found: dict[str, dict[str, str]] = {"light": {}, "dark": {}}
    faces: list[str] = []
    for css in css_files:
        for selector, body in _BLOCK.findall(css):
            selector = selector.strip()
            if selector.startswith("@font-face") or selector.endswith("@font-face"):
                continue
            scope = _scope(selector.rsplit("{", 1)[-1].rsplit("}", 1)[-1])
            if scope is None:
                continue
            for name, value in _DECL.findall(body):
                found[scope].setdefault(name, value.strip())  # first = plain color
        for body in _FONT_FACE.findall(css):
            if re.search(r"font-family\s*:\s*[\"']?GoogleSans", body) and body not in faces:
                faces.append(body)

    # Keep the Immich tokens and, transitively, whatever they refer to.
    wanted = {n for scope in found.values() for n in scope if n.startswith("--immich")}
    todo = list(wanted)
    while todo:
        name = todo.pop()
        for scope in found.values():
            for ref in _VAR_REF.findall(scope.get(name, "")):
                if ref not in wanted:
                    wanted.add(ref)
                    todo.append(ref)
    tokens = {
        scope: {n: v for n, v in values.items() if n in wanted} for scope, values in found.items()
    }
    return tokens, faces


def render(tokens: dict[str, dict[str, str]], faces: list[str], source: str) -> tuple[str, dict]:
    """The stylesheet for the browser and {served font name: Immich path}."""
    fonts: dict[str, str] = {}

    def local(match: re.Match) -> str:
        path = match.group(1)
        name = path.rsplit("/", 1)[-1]
        if not _FONT_FILE.match(name):
            return "url()"
        fonts[name] = path
        return f'url("{FONT_URL}{name}")'

    lines = [f"/* Design tokens of Immich ({source}), served by Immich Album Tagger. */"]
    for selector, scope in ((":root", "light"), (":root.dark", "dark")):
        if tokens.get(scope):
            body = "".join(f"\n  {name}: {value};" for name, value in tokens[scope].items())
            lines.append(f"{selector} {{{body}\n}}")
    for face in faces:
        lines.append("@font-face {" + _FONT_URL.sub(local, face) + "}")
    return "\n".join(lines) + "\n", fonts


class ImmichTheme:
    """Fetches and caches Immich's tokens; refreshes them every ``ttl`` seconds."""

    def __init__(
        self,
        base_url: str,
        verify_tls: bool = True,
        timeout: float = 10.0,
        ttl: float = 3600.0,
        retry: float = 300.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self.ttl = ttl
        self.retry = retry
        self._clock = clock
        self._http = httpx.Client(
            timeout=timeout, verify=verify_tls, transport=transport, follow_redirects=True
        )
        self._lock = threading.Lock()
        self._css = ""
        self._fonts: dict[str, str] = {}
        self._font_cache: dict[str, tuple[bytes, str]] = {}
        self._next = 0.0  # when to (re)fetch
        self.loaded_at: float | None = None
        self.error: str | None = None

    def css(self) -> str:
        """The stylesheet; empty (the built-in colors apply) until Immich could be read."""
        with self._lock:
            if self._clock() >= self._next:
                self._refresh()
            return self._css

    def _refresh(self) -> None:
        """Caller holds the lock. Keeps the last good result if Immich is unreachable."""
        try:
            index = self._get(self.base_url).text
            links = list(dict.fromkeys(_CSS_LINK.findall(index)))
            if not links:
                raise ValueError("no stylesheet linked from Immich's index page")
            css_files = [self._get(urljoin(self.base_url, link)).text for link in links]
            tokens, faces = extract(css_files)
            if not tokens["light"]:
                raise ValueError("no --immich-* variables in Immich's stylesheets")
            css, fonts = render(tokens, faces, self.base_url)
        except (httpx.HTTPError, ValueError) as exc:
            self.error = str(exc)
            self._next = self._clock() + self.retry
            log.warning(
                "Immich theme not loaded from %s (built-in colors used): %s", self.base_url, exc
            )
            return
        changed = css != self._css
        self._css, self._fonts, self.error = css, fonts, None
        self.loaded_at = self._clock()
        self._next = self.loaded_at + self.ttl
        if changed:
            log.info(
                "Immich theme loaded from %s: %d light and %d dark variables, fonts: %s",
                self.base_url,
                len(tokens["light"]),
                len(tokens["dark"]),
                ", ".join(fonts) or "none",
            )

    def font(self, name: str) -> tuple[bytes, str] | None:
        """A font file named in the stylesheet: (bytes, media type), or None."""
        self.css()  # make sure the list of fonts is known
        with self._lock:
            path = self._fonts.get(name)
            cached = self._font_cache.get(name)
        if path is None:
            return None
        if cached:
            return cached
        try:
            response = self._get(urljoin(self.base_url, path))
        except httpx.HTTPError as exc:
            log.warning("Immich font %s not loaded: %s", name, exc)
            return None
        media = _FONT_TYPES.get(name.rsplit(".", 1)[-1], "application/octet-stream")
        with self._lock:
            self._font_cache[name] = (response.content, media)
        return response.content, media

    def status(self) -> dict:
        return {"source": self.base_url, "loaded": self.loaded_at is not None, "error": self.error}

    def _get(self, url: str) -> httpx.Response:
        response = self._http.get(url)
        response.raise_for_status()
        return response

    def close(self) -> None:
        self._http.close()
