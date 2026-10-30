"""FastAPI application: JSON API, login, and the static pages (albums, logs, login)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from . import __version__
from .auth import (
    COOKIE,
    AuthError,
    Credentials,
    ImmichLogin,
    KeySession,
    KeySessions,
    Sessions,
    load_credentials,
)
from .config import KEY_PERMISSIONS, AppConfig, load_config
from .i18n import Translator, sync_language_files
from .immich import ImmichError
from .immich_theme import ImmichTheme
from .oplog import actor
from .service import TaggerService, Targets

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

# Reachable without logging in.
PUBLIC_PATHS = {
    "/login",
    "/api/login",
    "/api/logout",
    "/api/health",
    "/api/i18n",
    "/immich-theme.css",
}
PUBLIC_PREFIXES = ("/static/", "/immich-theme/")


class TargetsModel(BaseModel):
    tags: bool = True
    photo_description: bool = False
    album_description: bool = False


class ApplyRequest(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=50)
    keywords: list[str] = Field(default_factory=list, max_length=100)
    targets: TargetsModel = Field(default_factory=TargetsModel)
    dry_run: bool = False


class UsersModel(BaseModel):
    """``users`` (list) or the older single ``user``."""

    users: list[str] = Field(default_factory=list, max_length=50)
    user: str | None = None
    role: str = "viewer"

    @model_validator(mode="after")
    def _merge(self) -> UsersModel:
        if self.user:
            self.users = [*self.users, self.user]
        return self


class BulkShareRequest(UsersModel):
    album_ids: list[str] = Field(..., min_length=1, max_length=2000)
    remove: bool = False


class RemoveRequest(BaseModel):
    dry_run: bool = False


class FocusRequest(BaseModel):
    """Album ids in the order shown (filtered list); empty = the whole list."""

    album_ids: list[str] = Field(default_factory=list, max_length=20_000)


class LoginRequest(BaseModel):
    username: str = Field(..., max_length=200)
    password: str = Field(..., max_length=500)


def _page(name: str) -> str:
    """A static page whose assets are linked as ?v=<version>, so upgrades are never stale.

    Links are relative ("static/app.js"), so the app also works under a path prefix of a
    reverse proxy (https://immich.example.com/IAT/) without knowing it.
    """
    html = (STATIC_DIR / name).read_text(encoding="utf-8")
    return re.sub(r'"(static/[\w.-]+\.(?:js|css))"', rf'"\1?v={__version__}"', html)


def _credentials(config: AppConfig) -> Credentials | None:
    """credentials.yaml in CONFIG_DIR; APP_USERNAME/APP_PASSWORD as a fallback."""
    try:
        creds = load_credentials(config.config_dir / "credentials.yaml")
    except AuthError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    if creds is None and os.environ.get("APP_USERNAME"):
        creds = Credentials(os.environ["APP_USERNAME"], os.environ.get("APP_PASSWORD", ""), False)
    return creds


def _check_key_permissions(permissions: list[str]) -> None:
    """Warn about permissions the per-login keys lack, naming what will not work."""
    missing = [name for name in KEY_PERMISSIONS if name not in permissions]
    for name in missing:
        log.warning("auth.key_permissions lacks %s: %s will not work", name, KEY_PERMISSIONS[name])


def _theme(config: AppConfig) -> ImmichTheme | None:
    ui = config.app.ui
    if not ui.immich_theme:
        return None
    url = ui.immich_theme_url or config.immich.url.removesuffix("/api")
    return ImmichTheme(
        url,
        verify_tls=config.immich.verify_tls,
        timeout=min(config.immich.timeout_seconds, 15),
        ttl=ui.immich_theme_refresh_minutes * 60,
    )


def create_app(
    config: AppConfig | None = None,
    service_factory: Callable[[AppConfig, Translator], TaggerService] | None = None,
    theme: ImmichTheme | None = None,
) -> FastAPI:
    """Build the app. ``config``, ``service_factory`` and ``theme`` are injectable for tests."""
    config = config or load_config()
    theme = theme or _theme(config)
    try:
        sync_language_files(config.language_dir, __version__)
    except OSError as exc:
        log.warning("Cannot install language files into %s: %s", config.language_dir, exc)
    translator = Translator.load(config.language_dir, config.app.language)
    factory = service_factory or (lambda cfg, tr: TaggerService.from_config(cfg, tr))
    auth_cfg = config.app.auth
    creds = _credentials(config) if auth_cfg.mode in ("auto", "credentials") else None
    if auth_cfg.mode == "credentials" and creds is None:
        raise SystemExit(
            "Configuration error: auth.mode is 'credentials' but "
            f"{config.config_dir / 'credentials.yaml'} does not exist"
        )
    immich_login = auth_cfg.mode == "immich"
    keys_mode = auth_cfg.mode == "immich_keys"
    if (immich_login or keys_mode) and (config.config_dir / "credentials.yaml").exists():
        log.info("auth.mode is '%s': credentials.yaml is ignored", auth_cfg.mode)
    if keys_mode:
        _check_key_permissions(auth_cfg.key_permissions)
        if not config.immich.api_key:
            log.info("immich.api_key is not set: fine for the web UI, the CLI needs it")
    if immich_login or keys_mode:
        auth_mode = auth_cfg.mode
    else:
        auth_mode = "credentials" if creds else "none"
    sessions = (
        Sessions(config.data_dir / "session.key", auth_cfg.session_hours)
        if auth_mode != "none"
        else None
    )
    log.info("Web UI login: %s", auth_mode)
    pages = {name: _page(f"{name}.html") for name in ("index", "logs", "login")}

    def start_session(session: KeySession) -> None:
        """A login in mode immich_keys: the person's own service, with their key."""
        service = app.state.service
        claimed = service.history.claim_unowned(session.user_id)
        if claimed:
            log.info("%d history entries from before 0.6.0 given to %s", claimed, session.email)
        session.service = service.for_immich(session.client)
        session.service.start_background()

    def stop_session(session: KeySession) -> None:
        if session.service:
            session.service.stop_background()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if theme:  # read Immich's theme now, so the first page does not wait for it
            threading.Thread(target=theme.css, name="immich-theme", daemon=True).start()
        app.state.service = factory(config, translator)
        app.state.key_sessions = None
        if keys_mode:
            app.state.key_sessions = KeySessions(
                app.state.service.immich,
                auth_cfg.key_permissions,
                auth_cfg.session_hours,
                auth_cfg.allowed_users,
                on_open=start_session,
                on_close=stop_session,
            )
            app.state.key_sessions.start(auth_cfg.key_sweep_minutes)
        else:
            app.state.service.start_background()
        yield
        if app.state.key_sessions:
            app.state.key_sessions.stop()
            await asyncio.to_thread(app.state.key_sessions.close_all)
        app.state.service.close()
        if theme:
            theme.close()

    app = FastAPI(title="Immich Album Tagger", version=__version__, lifespan=lifespan)

    def svc(request: Request) -> TaggerService:
        session = getattr(request.state, "key_session", None)
        return session.service if session else request.app.state.service

    def session_user(request: Request) -> tuple[str | None, KeySession | None, bool]:
        """(user, key session, lost): user is "" without login and None if a login is
        needed; lost = valid cookie whose key session is gone (restart, expiry)."""
        if sessions is None:
            return "", None, False
        value = sessions.user(request.cookies.get(COOKIE))
        if not keys_mode or value is None:
            return value, None, False
        session = request.app.state.key_sessions.get(value)
        if session is None:
            return None, None, True
        return session.email, session, False

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        user, key_session, lost = session_user(request)
        request.state.user = user or ""
        request.state.key_session = key_session
        actor.set(user or "")  # recorded in the operation log
        if not (path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES) or user is not None):
            if path.startswith("/api/"):
                detail = translator("error.session_lost" if lost else "error.session")
                return JSONResponse({"detail": detail, "lost": lost}, status_code=401)
            lost_flag = "&lost=1" if lost else ""
            # Relative, so it stays under the proxy's path prefix.
            return RedirectResponse(f"login?next={path.lstrip('/')}{lost_flag}", status_code=303)
        response = await call_next(request)
        # Browsers cache heuristically without Cache-Control and could pair new HTML with
        # an old script after an upgrade; pages and assets are revalidated on each load.
        if path in ("/", "/logs", "/login") or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(ImmichError)
    async def immich_error(_: Request, exc: ImmichError) -> JSONResponse:
        code = 404 if exc.status == 404 else 502
        return JSONResponse({"detail": str(exc)}, status_code=code)

    @app.exception_handler(ValueError)
    async def value_error(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    # ------------------------------------------------------------------ public

    @app.get("/api/health", include_in_schema=False)
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/api/i18n")
    def i18n() -> dict:
        return {
            "lang": translator.language,
            "lang_code": translator.meta.get("code", "en"),
            "messages": translator.all(),
            "settings": {
                "version": __version__,
                "language": translator.language,
                "ui": config.app.ui.model_dump(),
                "sharing": config.app.sharing.model_dump(),
                "auth_mode": auth_mode,
            },
        }

    @app.post("/api/login")
    async def login(body: LoginRequest, request: Request) -> Response:
        if sessions is None:
            return JSONResponse({"ok": True})
        client = request.client.host if request.client else "?"
        key_session = None
        if keys_mode:
            key_sessions = request.app.state.key_sessions
            result, key_session, user = await asyncio.to_thread(
                key_sessions.open, body.username, body.password
            )
            identity = user["email"] if user else body.username
            messages = {
                "invalid": translator("error.login_immich"),
                "not_allowed": translator("error.login_not_allowed", user=identity),
                "unreachable": translator("error.login_unreachable"),
                "key_failed": translator("error.key_create_failed"),
            }
        elif immich_login:
            checker = ImmichLogin(request.app.state.service.immich, auth_cfg.allowed_users)
            result, user = await asyncio.to_thread(checker.check, body.username, body.password)
            identity = user["email"] if user else body.username
            messages = {
                "invalid": translator("error.login_immich"),
                "not_allowed": translator("error.login_not_allowed", user=identity),
                "unreachable": translator("error.login_unreachable"),
            }
        else:
            result = "ok" if creds.check(body.username, body.password) else "invalid"
            identity = body.username
            messages = {"invalid": translator("error.login")}
        if result != "ok":
            await asyncio.sleep(0.7)  # slow down guessing
            log.warning("Login refused (%s) for %r from %s", result, identity, client)
            code = {"unreachable": 503, "key_failed": 502}.get(result, 401)
            return JSONResponse({"detail": messages[result]}, status_code=code)
        log.info("Login of %r from %s", identity, client)
        response = JSONResponse({"ok": True, "user": identity})
        response.set_cookie(
            COOKIE,
            sessions.issue(key_session.id if key_session else identity),
            max_age=sessions.max_age,
            httponly=True,
            samesite="lax",
            secure=config.app.auth.secure_cookie,
        )
        return response

    @app.post("/api/logout")
    async def logout(request: Request) -> Response:
        if keys_mode and sessions is not None:
            session_id = sessions.user(request.cookies.get(COOKIE))
            await asyncio.to_thread(request.app.state.key_sessions.close, session_id)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    @app.get("/immich-theme.css", include_in_schema=False)
    def immich_theme_css() -> Response:
        """Immich's colors and font as CSS variables; empty if off or Immich unreadable."""
        css = theme.css() if theme else "/* immich_theme is off: built-in colors */\n"
        return Response(css, media_type="text/css", headers={"Cache-Control": "no-cache"})

    @app.get("/immich-theme/fonts/{name}", include_in_schema=False)
    def immich_font(name: str) -> Response:
        font = theme.font(name) if theme else None
        if font is None:
            return Response(status_code=404)
        content, media_type = font
        # Immich's font file names carry a content hash: safe to cache for long.
        headers = {"Cache-Control": "public, max-age=604800, immutable"}
        return Response(content, media_type=media_type, headers=headers)

    @app.get("/login", include_in_schema=False)
    def login_page() -> HTMLResponse:
        return HTMLResponse(pages["login"])

    # ------------------------------------------------------------------ pages

    @app.get("/", include_in_schema=False)
    def index() -> HTMLResponse:
        return HTMLResponse(pages["index"])

    @app.get("/logs", include_in_schema=False)
    def logs_page() -> HTMLResponse:
        return HTMLResponse(pages["logs"])

    # ------------------------------------------------------------------ API

    @app.get("/api/session")
    def get_session(request: Request) -> dict:
        """Who is logged in; cheap (no Immich call), for the header of every page."""
        auth = {"enabled": sessions is not None, "mode": auth_mode, "user": request.state.user}
        return {"version": __version__, "auth": auth}

    @app.get("/api/status")
    def get_status(request: Request, service: TaggerService = Depends(svc)) -> dict:
        auth = {"enabled": sessions is not None, "mode": auth_mode, "user": request.state.user}
        theme_status = theme.status() if theme else {"loaded": False, "source": None}
        return {"version": __version__, "auth": auth, "theme": theme_status, **service.status()}

    @app.get("/api/albums")
    def list_albums(service: TaggerService = Depends(svc)) -> list[dict]:
        return service.list_albums()

    @app.get("/api/tags")
    def list_tags(service: TaggerService = Depends(svc)) -> list[str]:
        return service.existing_tags()

    @app.post("/api/albums/{album_id}/suggestions")
    def suggest(
        album_id: str, refresh: bool = False, service: TaggerService = Depends(svc)
    ) -> dict:
        album, result, prepared = service.suggest(album_id, refresh=refresh)
        return {"album": album.to_dict(), "prepared": prepared, **result.to_dict()}

    @app.post("/api/albums/{album_id}/apply")
    def apply(album_id: str, body: ApplyRequest, service: TaggerService = Depends(svc)) -> dict:
        targets = Targets(**body.targets.model_dump())
        result = service.apply(album_id, body.tags, body.keywords, targets, dry_run=body.dry_run)
        return result.to_dict()

    @app.post("/api/albums/{album_id}/remove")
    def remove(album_id: str, body: RemoveRequest, service: TaggerService = Depends(svc)) -> dict:
        """Take back everything the app wrote to the album (tags, description lines)."""
        return service.remove(album_id, dry_run=body.dry_run).to_dict()

    @app.get("/api/prefetch")
    def prefetch_state(service: TaggerService = Depends(svc)) -> dict:
        if not service.prefetcher:
            return {"enabled": False, "ready_ids": []}
        ready_ids = sorted(service.prefetcher.ready_ids())
        return {**service.prefetcher.status(), "ready_ids": ready_ids}

    @app.post("/api/prefetch/focus")
    def prefetch_focus(body: FocusRequest, service: TaggerService = Depends(svc)) -> dict:
        """Background preparation follows the list on screen (e.g. filtered by "2024")."""
        service.set_prefetch_focus(body.album_ids)
        return {"focus": len(body.album_ids)}

    @app.get("/api/users")
    def list_users(service: TaggerService = Depends(svc)) -> list[dict]:
        return service.list_users()

    @app.get("/api/albums/{album_id}/users")
    def album_users(album_id: str, service: TaggerService = Depends(svc)) -> list[dict]:
        return service.album_users(album_id)

    @app.post("/api/albums/{album_id}/users")
    def share(album_id: str, body: UsersModel, service: TaggerService = Depends(svc)) -> dict:
        out = service.share_many([album_id], body.users, body.role)
        return {**out, "album_users": service.album_users(album_id)}

    @app.delete("/api/albums/{album_id}/users/{user_id}")
    def unshare(album_id: str, user_id: str, service: TaggerService = Depends(svc)) -> list:
        return service.unshare(album_id, user_id)

    @app.post("/api/sharing/bulk")
    def share_bulk(body: BulkShareRequest, service: TaggerService = Depends(svc)) -> dict:
        return service.share_many(body.album_ids, body.users, body.role, remove=body.remove)

    @app.get("/api/log")
    def read_log(limit: int = 500, service: TaggerService = Depends(svc)) -> list[dict]:
        return service.oplog.entries(limit=min(max(limit, 1), 5000))

    @app.delete("/api/log")
    def clear_log(service: TaggerService = Depends(svc)) -> dict:
        service.oplog.clear()
        return {"ok": True}

    @app.get("/api/thumbnails/{asset_id}")
    def thumbnail(asset_id: str, service: TaggerService = Depends(svc)) -> Response:
        try:
            content, media_type = service.immich.thumbnail(asset_id)
        except ImmichError:
            return Response(status_code=404)
        return Response(
            content, media_type=media_type, headers={"Cache-Control": "private, max-age=86400"}
        )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
