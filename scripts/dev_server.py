"""Run the UI against a fake Immich and a fake LLM - no real servers needed.

    python scripts/dev_server.py            # then open http://127.0.0.1:8080

The fake Immich (port 2283) holds a few sample albums in memory; the fake LLM (port 8081)
returns canned suggestions. Everything is lost on exit.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from fakes import OTHERS, FakeImmich  # noqa: E402

# Crude stand-in for what a real model does with Polish inflection.
BASE_FORMS = {
    "zielonej": "zielona",
    "zakopanem": "zakopane",
    "zosi": "zosia",
    "ani": "ania",
    "tomka": "tomek",
    "dawida": "dawid",
}

SAMPLE_ALBUMS = [
    ("2015-12-31 # 01-01 Sylwester na Zielonej", 24),
    ("2019-07 Wakacje Chorwacja Split", 214),
    ("Wesele Ani i Tomka 2021", 532),
    ("Zima w Zakopanem 2023", 88),
    ("Urodziny Zosi", 41),
    ("Durrës - mieszkanie, odbiór 2024", 17),
    ("Koncert Dawida Podsiadło", 23),
]

TINY_JPEG_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
    '<rect width="64" height="64" fill="#{c}"/></svg>'
)


def immich_app(fake: FakeImmich) -> Starlette:
    async def handle(request: Request) -> Response:
        if request.url.path.endswith("/thumbnail"):
            color = request.url.path.split("/")[3][:6]
            return Response(TINY_JPEG_SVG.format(c=color), media_type="image/svg+xml")
        body = await request.body()
        req = httpx.Request(
            request.method, str(request.url), headers=request.headers.raw, content=body
        )
        res = fake.handler(req)
        return Response(res.content, res.status_code, media_type="application/json")

    return Starlette(
        routes=[Route("/{path:path}", handle, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])]
    )


def llm_app() -> Starlette:
    async def chat(request: Request) -> JSONResponse:
        await asyncio.sleep(float(os.environ.get("DEV_LLM_DELAY", "1")))  # feel of a real model
        body = await request.json()
        user = body["messages"][1]["content"]
        if "Words:" in user:  # keyword normalisation prompt
            words = json.loads(user.split("Words:", 1)[1])
            reply = {
                "keywords": [
                    {"word": w, "base": BASE_FORMS.get(w.lower(), w.lower())} for w in words
                ],
                "related": ["nowy rok", "impreza"] if "sylwester" in user.lower() else ["rodzina"],
            }
            return JSONResponse({"choices": [{"message": {"content": json.dumps(reply)}}]})
        title = user.splitlines()[0].removeprefix("Album title: ")
        words = [w.lower() for w in title.split() if w.isalpha() and len(w) > 3][:3]
        reply = {
            "variants": [
                {"label": "Minimalny", "tags": [f"temat/{w}" for w in words[:1]]},
                {
                    "label": "Szczegółowy",
                    "tags": [f"temat/{w}" for w in words] + ["miejsce/polska"],
                },
            ]
        }
        content = "<think>fake reasoning</think>" + json.dumps(reply, ensure_ascii=False)
        return JSONResponse({"choices": [{"message": {"content": content}}]})

    async def models(_: Request) -> JSONResponse:
        return JSONResponse({"data": [{"id": "fake-model"}]})

    return Starlette(
        routes=[Route("/v1/chat/completions", chat, methods=["POST"]), Route("/v1/models", models)]
    )


def background(app: Starlette, port: int) -> None:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()


def main() -> None:
    fake = FakeImmich()
    for name, count in SAMPLE_ALBUMS:
        fake.add_album(
            name, count, startDate="2023-01-14T10:00:00Z", endDate="2023-01-16T18:00:00Z"
        )
    # An album shared with you by someone else, and one of yours with a few foreign photos.
    fake.add_album("Ferie u Ani 2022", 12, owner=OTHERS[0])
    fake.add_album("Wspólne: Mazury 2020", 30, foreign=6)
    background(immich_app(fake), 2283)
    background(llm_app(), 8081)
    time.sleep(0.5)

    os.environ.update(
        {
            "IMMICH_URL": "http://127.0.0.1:2283",
            "IMMICH_API_KEY": "secret",
            "LLM_BASE_URL": "http://127.0.0.1:8081/v1",
            "LLM_MODEL": "fake-model",
            "LLM_ENABLED": "true",
            "DATA_DIR": os.environ.get("DATA_DIR", tempfile.mkdtemp(prefix="immich-album-tagger-")),
        }
    )
    # DEV_CONFIG_DIR: try other app.yaml / credentials.yaml without touching ./config
    config_dir = os.environ.get("DEV_CONFIG_DIR", str(ROOT / "config"))
    os.environ.setdefault("LANGUAGE_DIR", os.path.join(os.environ["DATA_DIR"], "language"))
    os.environ.setdefault("LOG_DIR", os.path.join(os.environ["DATA_DIR"], "logs"))
    from immich_album_tagger.__main__ import main as cli

    port = os.environ.get("PORT", "8080")
    sys.exit(cli(["--config-dir", config_dir, "serve", "--host", "127.0.0.1", "--port", port]))


if __name__ == "__main__":
    main()
