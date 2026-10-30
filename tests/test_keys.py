"""auth.mode immich_keys: everyone works with a temporary API key of their own account."""

import logging

import httpx
import pytest
from conftest import make_service
from fakes import ME, OTHERS, FakeImmich
from fastapi.testclient import TestClient

from immich_album_tagger.app import create_app
from immich_album_tagger.auth import KeySessions, is_session_key
from immich_album_tagger.config import (
    KEY_PERMISSIONS,
    AppConfig,
    AppSettings,
    ConfigError,
    load_config,
)
from immich_album_tagger.immich import ImmichClient, ImmichError

ANIA = OTHERS[0]
ALEX_LOGIN = {"username": ME["email"], "password": "pw-alex"}
ANIA_LOGIN = {"username": ANIA["email"], "password": "pw-ania"}


def keys_app(fake, immich_config, tagging_config, tmp_path, prefetch=False, **auth):
    """The real app in mode immich_keys, without an API key in immich.yaml."""
    tagging = tagging_config.model_copy(
        update={"prefetch": tagging_config.prefetch.model_copy(update={"enabled": prefetch})}
    )
    no_key = immich_config.model_copy(update={"api_key": ""})
    settings = AppSettings(auth={"mode": "immich_keys", **auth}, ui={"immich_theme": False})
    config = make_service(fake, no_key, tagging, tmp_path, app=settings).config

    def factory(cfg, translator):
        service = make_service(fake, no_key, tagging, tmp_path, app=settings)
        service.t = service.suggester.t = translator
        return service

    return create_app(config, factory)


def session_keys(fake, user):
    return [k for k in fake.keys_of(user) if is_session_key(k["name"])]


def test_login_creates_key_and_logout_deletes_it(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    app = keys_app(fake, immich_config, tagging_config, tmp_path)
    with TestClient(app) as http:
        assert http.get("/api/i18n").json()["settings"]["auth_mode"] == "immich_keys"
        assert http.get("/api/albums").status_code == 401
        wrong = http.post("/api/login", json={**ALEX_LOGIN, "password": "x"})
        assert wrong.status_code == 401 and not session_keys(fake, ME)

        ok = http.post("/api/login", json=ALEX_LOGIN)
        assert ok.status_code == 200 and ok.json()["user"] == ME["email"]
        [key] = session_keys(fake, ME)
        assert key["permissions"] == [*KEY_PERMISSIONS, "apiKey.delete"]
        assert not fake.login_sessions  # the Immich session of the login was closed
        status = http.get("/api/status").json()
        assert status["auth"] == {"enabled": True, "mode": "immich_keys", "user": ME["email"]}
        assert status["immich"]["user_id"] == ME["id"]

        http.post("/api/logout")
        assert not session_keys(fake, ME)
        assert http.get("/api/albums").status_code == 401
        assert [k["name"] for k in fake.keys_of(ME)] == ["test key"]  # others untouched


def test_everyone_works_on_their_own_photos(immich_config, tagging_config, tmp_path):
    fake = FakeImmich(members_only=True)
    mine = fake.add_album("Alex 2021", 2)
    theirs = fake.add_album("Ania 2022", 2, owner=ANIA)
    app = keys_app(fake, immich_config, tagging_config, tmp_path)
    with TestClient(app) as alex:
        ania = TestClient(app)  # second browser; the lifespan already runs
        assert alex.post("/api/login", json=ALEX_LOGIN).status_code == 200
        assert ania.post("/api/login", json=ANIA_LOGIN).status_code == 200
        assert [a["id"] for a in alex.get("/api/albums").json()] == [mine]
        assert [a["id"] for a in ania.get("/api/albums").json()] == [theirs]

        result = ania.post(f"/api/albums/{theirs}/apply", json={"tags": ["ania-tag"]}).json()
        assert result["complete"] is True and result["own_count"] == 2
        assert alex.post(f"/api/albums/{theirs}/suggestions").status_code == 404
        [tag] = [t for t in fake.tags.values() if t["value"] == "ania-tag"]
        assert tag["userId"] == ANIA["id"]  # created with Ania's key
        assert all(tag["id"] in fake.asset_tags[a] for a in fake.album_assets[theirs])
        assert not any(fake.asset_tags[a] for a in fake.album_assets[mine])
        assert alex.get("/api/tags").json() == []  # tags are per user

        shared = ania.post(f"/api/albums/{theirs}/users", json={"users": [ME["email"]]})
        assert shared.status_code == 200
        assert {a["id"] for a in alex.get("/api/albums").json()} == {mine, theirs}
        log = alex.get("/api/log").json()
        assert {e["by"] for e in log} == {ANIA["email"]}  # shared log, who did what

        # History: the "tagged" marker is per person.
        assert ania.get("/api/albums").json()[0]["applied"]["tags"] == ["ania-tag"]
        assert all(a["applied"] is None for a in alex.get("/api/albums").json())
        assert len(session_keys(fake, ME)) == len(session_keys(fake, ANIA)) == 1
    # Shutting down deletes every key still held.
    assert not session_keys(fake, ME) and not session_keys(fake, ANIA)


def test_lost_session_needs_a_new_login(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    app = keys_app(fake, immich_config, tagging_config, tmp_path)
    with TestClient(app) as http:
        http.post("/api/login", json=ALEX_LOGIN)
        assert http.get("/api/albums").status_code == 200
        app.state.key_sessions._live.clear()  # as after a restart: cookie valid, no session
        response = http.get("/api/albums")
        assert response.status_code == 401 and response.json()["lost"] is True
        assert "log in again" in response.json()["detail"]
        page = http.get("/", follow_redirects=False)
        assert page.status_code == 303 and page.headers["location"].endswith("&lost=1")


def test_allowed_users_and_empty_list(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    app = keys_app(fake, immich_config, tagging_config, tmp_path, allowed_users=[ANIA["email"]])
    with TestClient(app) as http:
        refused = http.post("/api/login", json=ALEX_LOGIN)
        assert refused.status_code == 401 and ME["email"] in refused.json()["detail"]
        assert not session_keys(fake, ME) and not fake.login_sessions
        assert http.post("/api/login", json=ANIA_LOGIN).status_code == 200

    fake = FakeImmich()
    app = keys_app(fake, immich_config, tagging_config, tmp_path / "open")
    with TestClient(app) as http:  # empty list: every Immich account
        assert http.post("/api/login", json=ANIA_LOGIN).status_code == 200


def test_old_history_goes_to_the_first_login(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    album_id = fake.add_album("Lato 2021", 1)
    old = make_service(fake, immich_config, tagging_config, tmp_path)
    old.history.record(album_id, "Lato 2021", ["x"], [], {"tags": True}, 1, 1, True)  # 0.5.0
    app = keys_app(fake, immich_config, tagging_config, tmp_path)
    with TestClient(app) as http:
        ania = TestClient(app)
        http.post("/api/login", json=ALEX_LOGIN)
        ania.post("/api/login", json=ANIA_LOGIN)
    assert old.history.latest_by_album(ME["id"])[album_id]["tags"] == ["x"]
    assert old.history.latest_by_album(ANIA["id"]) == {}


def test_prefetch_runs_per_person(immich_config, tagging_config, tmp_path):
    fake = FakeImmich(members_only=True)
    mine = fake.add_album("Alex 2021", 1)
    theirs = fake.add_album("Ania 2022", 1, owner=ANIA)
    app = keys_app(fake, immich_config, tagging_config, tmp_path, prefetch=True)
    with TestClient(app) as http:
        ania = TestClient(app)
        http.post("/api/login", json=ALEX_LOGIN)
        ania.post("/api/login", json=ANIA_LOGIN)
        sessions = {s.email: s for s in app.state.key_sessions.active()}
        alex_pf = sessions[ME["email"]].service.prefetcher
        ania_pf = sessions[ANIA["email"]].service.prefetcher
        assert alex_pf is not ania_pf
        alex_pf.stop()
        ania_pf.stop()  # deterministic: run the workers by hand
        alex_pf.run_once()
        ania_pf.run_once()
        assert alex_pf.ready_ids() == {mine} and ania_pf.ready_ids() == {theirs}
        ania.post("/api/logout")
        assert [s.id for s in app.state.key_sessions.active()] == [sessions[ME["email"]].id]


# ---------------------------------------------------------------------- KeySessions


def make_sessions(fake, immich_config, clock, **kwargs):
    client = ImmichClient(
        immich_config.model_copy(update={"api_key": ""}),
        transport=httpx.MockTransport(fake.handler),
    )
    return KeySessions(client, list(KEY_PERMISSIONS), hours=1, clock=lambda: clock[0], **kwargs)


def test_sweep_deletes_keys_of_expired_sessions(immich_config):
    fake = FakeImmich()
    clock = [1_000_000.0]
    sessions = make_sessions(fake, immich_config, clock)
    result, session, _ = sessions.open(ME["email"], "pw-alex")
    assert result == "ok" and sessions.get(session.id) is session
    assert sessions.sweep() == 0 and len(session_keys(fake, ME)) == 1
    clock[0] += 3601
    assert sessions.get(session.id) is None  # expired: no longer usable
    assert sessions.sweep() == 1 and not session_keys(fake, ME)


def test_failed_deletion_is_retried(immich_config):
    fake = FakeImmich()
    sessions = make_sessions(fake, immich_config, [0.0])
    _, session, _ = sessions.open(ME["email"], "pw-alex")
    real_delete = session.client.delete_api_key

    def down(key_id, token=None):
        raise ImmichError("Immich is down", status=502)

    session.client.delete_api_key = down
    assert sessions.close(session.id) is True
    assert sessions.get(session.id) is None and len(session_keys(fake, ME)) == 1
    session.client.delete_api_key = real_delete
    assert sessions.sweep() == 1 and not session_keys(fake, ME)


def test_leftover_keys_are_deleted_at_next_login(immich_config):
    fake = FakeImmich()
    clock = [0.0]
    before_restart = make_sessions(fake, immich_config, clock)
    before_restart.open(ME["email"], "pw-alex")
    before_restart.open(ANIA["email"], "pw-ania")
    # Keys made by hand stay, even when the name starts like the app's.
    for name in ("immich-album-tagger", "immich-album-tagger session mine", "phone"):
        fake.api_keys[f"manual-{name}"] = {
            "id": name,
            "name": name,
            "permissions": ["all"],
            "user": ME,
        }

    after_restart = make_sessions(fake, immich_config, clock)
    _, session, _ = after_restart.open(ME["email"], "pw-alex")
    assert [k["id"] for k in session_keys(fake, ME)] == [session.key_id]
    names = {k["name"] for k in fake.keys_of(ME)}
    assert {"immich-album-tagger", "immich-album-tagger session mine", "phone"} <= names
    assert len(session_keys(fake, ANIA)) == 1  # someone else's leftover waits for their login


def test_key_creation_failure_is_reported(immich_config):
    fake = FakeImmich()
    sessions = make_sessions(fake, immich_config, [0.0])
    sessions.immich.create_api_key = lambda *a: (_ for _ in ()).throw(ImmichError("no", 400))
    result, session, user = sessions.open(ME["email"], "pw-alex")
    assert (result, session, user["email"]) == ("key_failed", None, ME["email"])
    assert not fake.login_sessions


# ---------------------------------------------------------------------- configuration


def test_api_key_optional_only_in_immich_keys(immich_config):
    no_key = immich_config.model_copy(update={"api_key": ""})
    AppConfig(immich=no_key, app=AppSettings(auth={"mode": "immich_keys"}))
    with pytest.raises(ValueError, match="IMMICH_API_KEY"):
        AppConfig(immich=no_key, app=AppSettings(auth={"mode": "immich"}))


def test_cli_needs_the_api_key(tmp_path, monkeypatch, capsys):
    from immich_album_tagger.__main__ import main

    (tmp_path / "immich.yaml").write_text("url: http://x\n")
    (tmp_path / "app.yaml").write_text("auth:\n  mode: immich_keys\n")
    assert load_config(tmp_path, tmp_path).immich.api_key == ""
    assert main(["--config-dir", str(tmp_path), "check"]) != 0
    assert "IMMICH_API_KEY" in capsys.readouterr().err
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing")


def test_missing_key_permissions_are_warned_about(immich_config, tagging_config, tmp_path, caplog):
    fake = FakeImmich()
    permissions = [p for p in KEY_PERMISSIONS if p != "asset.update"]
    with caplog.at_level(logging.WARNING):
        keys_app(fake, immich_config, tagging_config, tmp_path, key_permissions=permissions)
    assert "lacks asset.update: photo descriptions will not work" in caplog.text
