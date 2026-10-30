"""Colors and font read from the running Immich (ui.immich_theme)."""

import httpx
from conftest import make_service
from fakes import FakeImmich
from fastapi.testclient import TestClient

from immich_album_tagger.app import create_app
from immich_album_tagger.config import AppSettings
from immich_album_tagger.immich_theme import ImmichTheme, extract

# Shaped like Immich's compiled stylesheet: plain colors first, lab() colors again in
# @supports, Tailwind's palette in :root,:host, utilities that must be ignored.
IMMICH_CSS = (
    "@layer theme{:root,:host{--color-neutral-100:#f5f5f5;--color-neutral-900:#171717;"
    "--color-red-500:red;--font-sans:x}}"
    "@layer base{:root,.light{--immich-ui-primary-500:#123456;"
    "--immich-ui-light-100:var(--color-neutral-100);--immich-ui-light:#fff}"
    ".dark{--immich-ui-primary-500:#abcdef;--immich-ui-light-100:var(--color-neutral-900);"
    "--immich-ui-light:#000}}"
    "@supports (color:lab(0% 0 0)){:root,.light{--immich-ui-primary-500:lab(30% 10 -50)}}"
    ":root{--immich-bg:255 255 255;--immich-dark-bg:10 10 10}"
    ".bg-primary{--tw-x:1;background:var(--immich-ui-primary-500)}"
    "@font-face{font-family:GoogleSans;src:url(/_app/immutable/assets/GoogleSans.H4sh.ttf)"
    'format("truetype-variations");font-weight:410 900}'
    "@font-face{font-family:Other;src:url(/_app/immutable/assets/Other.x.ttf)}"
)
INDEX = (
    '<link rel="preload" as="font" href="/_app/immutable/assets/GoogleSans.H4sh.ttf">'
    '<link href="/_app/immutable/assets/0.Ab12.css" rel="stylesheet">'
)


class FakeWeb:
    """Immich's web UI: index page, stylesheet, font."""

    def __init__(self):
        self.up = True
        self.css = IMMICH_CSS
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        if not self.up:
            raise httpx.ConnectError("down")
        path = request.url.path
        if path == "/":
            return httpx.Response(200, text=INDEX)
        if path == "/_app/immutable/assets/0.Ab12.css":
            return httpx.Response(200, text=self.css)
        if path == "/_app/immutable/assets/GoogleSans.H4sh.ttf":
            return httpx.Response(200, content=b"TTF")
        return httpx.Response(404)


def make_theme(web, clock=None):
    clock = clock or [0.0]
    theme = ImmichTheme(
        "http://immich.test:2283",
        transport=httpx.MockTransport(web.handler),
        ttl=3600,
        retry=300,
        clock=lambda: clock[0],
    )
    return theme, clock


def test_extract_keeps_immich_tokens_and_what_they_need():
    tokens, faces = extract([IMMICH_CSS])
    assert tokens["light"] == {
        "--color-neutral-100": "#f5f5f5",
        "--color-neutral-900": "#171717",
        "--immich-ui-primary-500": "#123456",  # plain color, not the lab() one
        "--immich-ui-light-100": "var(--color-neutral-100)",
        "--immich-ui-light": "#fff",
        "--immich-bg": "255 255 255",
        "--immich-dark-bg": "10 10 10",
    }
    assert tokens["dark"]["--immich-ui-primary-500"] == "#abcdef"
    assert "--color-red-500" not in tokens["light"] and "--tw-x" not in tokens["light"]
    assert len(faces) == 1 and "GoogleSans" in faces[0]


def test_theme_css_and_fonts():
    web = FakeWeb()
    theme, _ = make_theme(web)
    css = theme.css()
    assert ":root {" in css and ":root.dark {" in css
    assert "--immich-ui-primary-500: #abcdef;" in css
    assert 'url("immich-theme/fonts/GoogleSans.H4sh.ttf")' in css  # relative, via the app
    assert "/_app/" not in css and "Other" not in css
    assert theme.font("GoogleSans.H4sh.ttf") == (b"TTF", "font/ttf")
    assert theme.font("Other.x.ttf") is None  # only fonts named in the stylesheet
    assert theme.font("../../etc/passwd") is None
    assert theme.status()["loaded"] is True


def test_theme_is_cached_refreshed_and_survives_immich_down():
    web = FakeWeb()
    theme, clock = make_theme(web)
    first = theme.css()
    calls = len(web.requests)
    assert theme.css() == first and len(web.requests) == calls  # cached

    web.css = IMMICH_CSS.replace("#abcdef", "#fedcba")  # Immich upgraded
    clock[0] += 3601
    assert "#fedcba" in theme.css()

    web.up = False
    clock[0] += 3601
    assert "#fedcba" in theme.css()  # last good result kept
    assert theme.status()["error"]


def test_unreachable_immich_gives_empty_theme_and_retries_later():
    web = FakeWeb()
    web.up = False
    theme, clock = make_theme(web)
    assert theme.css() == "" and theme.status()["loaded"] is False
    web.up = True
    assert theme.css() == ""  # not retried before `retry` seconds
    clock[0] += 301
    assert "--immich-ui-primary-500" in theme.css()


def test_theme_routes_are_public(immich_config, tagging_config, tmp_path):
    web = FakeWeb()
    theme, _ = make_theme(web)
    settings = AppSettings(auth={"mode": "immich"})
    config = make_service(
        FakeImmich(), immich_config, tagging_config, tmp_path, app=settings
    ).config

    def factory(cfg, translator):
        return make_service(FakeImmich(), immich_config, tagging_config, tmp_path, app=settings)

    with TestClient(create_app(config, factory, theme=theme)) as http:
        assert http.get("/api/albums").status_code == 401  # login required...
        css = http.get("/immich-theme.css")  # ...but the login page needs the theme
        assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
        assert "--immich-ui-primary-500" in css.text
        font = http.get("/immich-theme/fonts/GoogleSans.H4sh.ttf")
        assert font.status_code == 200 and font.content == b"TTF"
        assert "immutable" in font.headers["cache-control"]
        assert http.get("/immich-theme/fonts/nope.ttf").status_code == 404
        assert '<link rel="stylesheet" href="immich-theme.css">' in http.get("/login").text


def test_theme_off(immich_config, tagging_config, tmp_path):
    settings = AppSettings(ui={"immich_theme": False})
    config = make_service(
        FakeImmich(), immich_config, tagging_config, tmp_path, app=settings
    ).config

    def factory(cfg, translator):
        return make_service(FakeImmich(), immich_config, tagging_config, tmp_path, app=settings)

    with TestClient(create_app(config, factory)) as http:
        assert "--immich" not in http.get("/immich-theme.css").text
        assert http.get("/api/status").json()["theme"]["loaded"] is False
