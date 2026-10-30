import pytest
from conftest import make_service
from fakes import ME, OTHERS, FakeImmich
from fastapi.testclient import TestClient

from immich_album_tagger.app import create_app
from immich_album_tagger.service import Targets

ALL = Targets(tags=True, photo_description=True, album_description=True)


def test_apply_tags_every_owned_asset_and_verifies(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    album_id = fake.add_album("2019 Wesele", 5)
    service = make_service(fake, immich_config, tagging_config, tmp_path)

    result = service.apply(album_id, ["Rok/2019", "wydarzenie/wesele", "rok/2019"])

    assert result.tags == ["rok/2019", "wydarzenie/wesele"]
    assert result.own_count == 5 and result.new_links == 10
    assert result.complete is True and result.warnings == []
    for asset in fake.album_assets[album_id]:
        values = {fake.tags[t]["value"] for t in fake.asset_tags[asset]}
        assert values == {"rok/2019", "wydarzenie/wesele"}
    assert service.list_albums()[0]["applied"]["tags"] == result.tags


def test_foreign_photos_are_skipped_and_reported(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    album_id = fake.add_album("Shared", 4, foreign=1)
    result = make_service(fake, immich_config, tagging_config, tmp_path).apply(album_id, ["x"])
    assert result.asset_count == 4 and result.own_count == 3
    assert result.complete is True
    assert "1 of 4 photos belong to other users" in result.warnings[0]


def test_read_only_library_is_detected(immich_config, tagging_config, tmp_path):
    fake = FakeImmich(read_only=True)
    album_id = fake.add_album("Sylwester", 3, external=True)
    service = make_service(fake, immich_config, tagging_config, tmp_path)
    result = service.apply(album_id, ["x"], targets=Targets(tags=True, photo_description=True))
    assert result.complete is False
    assert result.verified_tags == {"x": 0} and result.verified_descriptions == 0
    assert any("external library" in w and "read-only" in w for w in result.warnings)


def test_lost_links_suggest_retry(immich_config, tagging_config, tmp_path):
    fake = FakeImmich(drop_links=1)
    album_id = fake.add_album("A", 4)
    result = make_service(fake, immich_config, tagging_config, tmp_path).apply(album_id, ["x"])
    assert result.complete is False
    warning = result.warnings[0]
    assert "2 of 4 photos did not have tag “x”" in warning and "DSC_0000.JPG" in warning
    assert "apply again" in warning and "read-only" not in warning


def test_descriptions_keep_user_text_and_are_idempotent(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    album_id = fake.add_album(
        "Sylwester", 3, description="Impreza u Ani.", descriptions=["Fajerwerki", "", ""]
    )
    service = make_service(fake, immich_config, tagging_config, tmp_path)
    first = service.apply(
        album_id, ["wydarzenie/sylwester", "zielona"], ["sylwester", "zielona"], ALL
    )
    assert first.complete is True and first.descriptions_updated == 3
    photos = [fake.assets[a]["description"] for a in fake.album_assets[album_id]]
    assert photos[0] == "Fajerwerki\n#wydarzenie/sylwester #zielona"
    assert photos[1] == "#wydarzenie/sylwester #zielona"
    album = fake.albums[album_id]["description"]
    assert album == "Impreza u Ani.\nSłowa kluczowe: sylwester zielona"

    second = service.apply(album_id, ["wydarzenie/sylwester"], ["sylwester", "nowy rok"], ALL)
    photos = [fake.assets[a]["description"] for a in fake.album_assets[album_id]]
    assert photos[0] == "Fajerwerki\n#wydarzenie/sylwester"
    assert (
        fake.albums[album_id]["description"] == "Impreza u Ani.\nSłowa kluczowe: sylwester nowy rok"
    )
    assert second.complete is True
    bulk = [b for m, p, b in fake.calls if m == "PUT" and p == "/assets"]
    assert len(bulk) == 4  # two description groups per run: with and without user text


def test_album_of_other_owner_is_not_edited(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    album_id = fake.add_album("Theirs", 2, owner=OTHERS[0], description="x")
    result = make_service(fake, immich_config, tagging_config, tmp_path).apply(
        album_id, [], ["słowo"], Targets(tags=False, album_description=True)
    )
    assert fake.albums[album_id]["description"] == "x"
    assert not result.album_description_updated and "only the owner" in result.warnings[0]


def test_validation(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    album_id = fake.add_album("A", 1)
    service = make_service(fake, immich_config, tagging_config, tmp_path)
    for args in (
        ([], [], Targets(tags=False)),
        ([" / "], [], Targets()),
        (["x"], [], Targets(tags=False, album_description=True)),
    ):
        try:
            service.apply(album_id, *args)
        except ValueError:
            continue
        raise AssertionError(f"no error for {args}")


def test_dry_run_changes_nothing(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    album_id = fake.add_album("A", 3)
    service = make_service(fake, immich_config, tagging_config, tmp_path)
    result = service.apply(album_id, ["x"], ["y"], ALL, dry_run=True)
    assert result.own_count == 3 and not fake.tags
    assert fake.albums[album_id]["description"] == ""


def test_share_and_unshare(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    album_id = fake.add_album("A", 1)
    service = make_service(fake, immich_config, tagging_config, tmp_path)
    out = service.share_many([album_id], ["ANIA@example.com", "tomek@example.com"], "viewer")
    assert [r["status"] for r in out["results"]] == ["shared", "shared"]
    assert [(u["email"], u["role"]) for u in service.album_users(album_id)] == [
        (ME["email"], "owner"),
        ("ania@example.com", "viewer"),
        ("tomek@example.com", "viewer"),
    ]
    again = service.share_many([album_id], "ania@example.com", "viewer")
    assert again["results"][0]["status"] == "unchanged"
    for bad, role in (("nobody@x", "viewer"), (ME["email"], "viewer"), (OTHERS[1]["id"], "admin")):
        try:
            service.share_many([album_id], bad, role)
        except ValueError:
            continue
        raise AssertionError(bad)
    assert len(service.unshare(album_id, OTHERS[0]["id"])) == 2
    log = service.oplog.entries()
    assert log[0]["action"] == "unshare" and log[0]["status"] == "success"
    assert any(e["status"] == "error" and "nobody@x" in e["message"] for e in log)
    assert [u["email"] for u in service.list_users()] == ["ania@example.com", "tomek@example.com"]


def app_client(fake, immich_config, tagging_config, tmp_path, credentials=None, app=None):
    """TestClient around the real app; ``credentials`` writes a credentials.yaml first."""
    from immich_album_tagger.config import AppSettings

    probe = make_service(fake, immich_config, tagging_config, tmp_path)
    settings = {"ui": {"immich_theme": False}, **(app or {})}  # no network in tests
    config = probe.config.model_copy(update={"app": AppSettings(**settings)})
    config.config_dir.mkdir(parents=True, exist_ok=True)
    if credentials:
        (config.config_dir / "credentials.yaml").write_text(credentials)

    def factory(cfg, translator):
        service = make_service(fake, immich_config, tagging_config, tmp_path)
        service.t = service.suggester.t = translator
        service.start_background = lambda: None  # no worker thread in tests
        return service

    return TestClient(create_app(config, factory))


def test_api_flow(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    album_id = fake.add_album("Lato 2021 Chorwacja", 3)
    with app_client(fake, immich_config, tagging_config, tmp_path) as http:
        assert http.get("/api/health").json()["status"] == "ok"
        status = http.get("/api/status").json()
        assert status["immich"]["ok"] is True and status["apply_defaults"]["tags"] is True
        assert http.get("/api/albums").json()[0]["owner_email"] == ME["email"]

        suggestions = http.post(f"/api/albums/{album_id}/suggestions").json()
        rules = next(v for v in suggestions["variants"] if v["source"] == "rules")
        assert rules["tags"] == ["rok/2021", "pora-roku/lato"]
        assert [k["word"] for k in suggestions["keywords"]] == ["Lato", "Chorwacja"]

        body = {
            "tags": rules["tags"],
            "keywords": ["lato", "chorwacja"],
            "targets": {"tags": True, "photo_description": True, "album_description": True},
        }
        applied = http.post(f"/api/albums/{album_id}/apply", json=body).json()
        assert applied["complete"] is True
        album = http.get("/api/albums").json()[0]
        assert album["applied"]["keywords"] == ["lato", "chorwacja"]
        assert album["description"].endswith("Słowa kluczowe: lato chorwacja")

        none = {"tags": ["x"], "targets": {"tags": False}}
        assert http.post(f"/api/albums/{album_id}/apply", json=none).status_code == 400
        missing = "00000000-0000-4000-8000-000000000000"
        assert http.post(f"/api/albums/{missing}/suggestions").status_code == 404

        shared = http.post(f"/api/albums/{album_id}/users", json={"users": ["tomek@example.com"]})
        assert shared.status_code == 200 and shared.json()["album_users"][-1]["role"] == "viewer"
        assert http.get(f"/api/albums/{album_id}/users").json()[-1]["email"] == "tomek@example.com"
        left = http.delete(f"/api/albums/{album_id}/users/{OTHERS[1]['id']}").json()
        assert len(left) == 1
        assert http.get("/").status_code == 200


def test_login_with_credentials_file(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    creds = "encrypted: false\nusername: alex\npassword: tajne hasło\n"
    with app_client(fake, immich_config, tagging_config, tmp_path, credentials=creds) as http:
        stored = (tmp_path / "config" / "credentials.yaml").read_text()
        assert "encrypted: true" in stored and "alex" not in stored and "tajne" not in stored
        assert http.get("/api/health").status_code == 200
        assert http.get("/api/i18n").status_code == 200  # login page needs translations
        assert http.get("/api/albums").status_code == 401
        page = http.get("/", follow_redirects=False)
        assert page.status_code == 303 and page.headers["location"] == "login?next="
        bad = http.post("/api/login", json={"username": "alex", "password": "nope"})
        assert bad.status_code == 401
        ok = http.post("/api/login", json={"username": "alex", "password": "tajne hasło"})
        assert ok.status_code == 200
        assert http.get("/api/albums").status_code == 200
        assert http.get("/logs").status_code == 200
        http.post("/api/logout")
        assert http.get("/api/albums").status_code == 401


def test_assets_are_versioned_and_revalidated(immich_config, tagging_config, tmp_path):
    from immich_album_tagger import __version__

    fake = FakeImmich()
    with app_client(fake, immich_config, tagging_config, tmp_path) as http:
        page = http.get("/")
        assert f'"static/app.js?v={__version__}"' in page.text
        assert f'"static/style.css?v={__version__}"' in page.text
        assert f'"static/theme.js?v={__version__}"' in page.text
        assert page.headers["cache-control"] == "no-cache"
        assert http.get("/static/app.js").headers["cache-control"] == "no-cache"
        assert http.get("/api/session").json() == {
            "version": __version__,
            "auth": {"enabled": False, "mode": "none", "user": ""},
        }


def test_urls_are_relative_for_a_proxy_path_prefix(immich_config, tagging_config, tmp_path):
    """Behind https://immich.example.com/IAT/ every link must stay under /IAT/."""
    import re

    from immich_album_tagger.app import STATIC_DIR

    for path in STATIC_DIR.iterdir():
        text = path.read_text(encoding="utf-8")
        absolute = re.findall(r'(?:href|src|fetch\(|api\()\s*=?\s*["`]/(?!/)', text)
        assert not absolute, f"{path.name} links to an absolute path"
        assert not re.search(r'["`]/(api|static|login|logs)\b', text), path.name
    creds = "encrypted: false\nusername: alex\npassword: x\n"
    with app_client(
        FakeImmich(), immich_config, tagging_config, tmp_path, credentials=creds
    ) as http:
        page = http.get("/logs", follow_redirects=False)
        assert page.headers["location"] == "login?next=logs"


def test_slow_job_queue_is_waited_for(immich_config, tagging_config, tmp_path):
    # The last description becomes visible only on the 3rd search; retries absorb it.
    fake = FakeImmich(lag_searches=4)
    album_id = fake.add_album("Narty", 6)
    sleeps = []
    service = make_service(fake, immich_config, tagging_config, tmp_path)
    service._sleep = sleeps.append
    result = service.apply(album_id, ["x"], targets=Targets(tags=True, photo_description=True))
    assert result.complete is True and result.warnings == []
    assert len(sleeps) >= 2  # initial delay + at least one retry


def test_one_unwritable_file_is_named(immich_config, tagging_config, tmp_path):
    fake = FakeImmich(broken_files=1)
    album_id = fake.add_album("Narty", 4, external=True)
    service = make_service(fake, immich_config, tagging_config, tmp_path)
    result = service.apply(album_id, ["x"], targets=Targets(tags=False, photo_description=True))
    assert result.complete is False and result.verified_descriptions == 3
    warning = result.warnings[0]
    assert "1 of 4 photos did not have the description line" in warning
    assert "DSC_0003.JPG" in warning and "read-only mount" not in warning


def test_prefetch_prepares_window_and_moves_on(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    ids = [fake.add_album(f"Album {i}", 2) for i in range(4)]
    fake.add_album("Not mine", 2, owner=OTHERS[0])
    tagging_config.prefetch.albums = 2
    service = make_service(fake, immich_config, tagging_config, tmp_path)
    worker = service.prefetcher

    assert worker.run_once() == ids[0] and worker.run_once() == ids[1]
    assert worker.run_once() is None  # window of 2 is ready
    assert worker.status()["ready"] == 2
    searches = len(fake.calls)
    album, result, prepared = service.suggest(ids[0])
    assert prepared and album.id == ids[0] and len(fake.calls) == searches  # no Immich call

    service.apply(ids[0], ["x"])  # album leaves the window, the next one slides in
    assert worker.run_once() == ids[2]
    assert {a["id"] for a in service.list_albums() if a["prepared"]} == {ids[1], ids[2]}

    _, _, prepared = service.suggest(ids[1], refresh=True)
    assert prepared is False


def test_prefetch_retries_failed_llm_later(immich_config, tagging_config, tmp_path):
    from immich_album_tagger.llm import LLMError

    class DownLLM:
        def complete(self, system, user):
            raise LLMError("down")

    fake = FakeImmich()
    album_id = fake.add_album("Sylwester", 1)
    tagging_config.prefetch.albums = 1
    now = [0.0]
    service = make_service(fake, immich_config, tagging_config, tmp_path, llm=DownLLM())
    service.prefetcher._clock = lambda: now[0]
    assert service.prefetcher.run_once() == album_id  # rules-only result kept, with warning
    assert service.prefetcher.get(album_id).result.warnings
    assert service.prefetcher.run_once() is None  # not retried immediately
    now[0] += 6 * 60
    assert service.prefetcher.run_once() == album_id  # retried after retry_after_minutes


def test_bulk_share_and_remove(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    a = fake.add_album("A", 1)
    b = fake.add_album("B", 1)
    theirs = fake.add_album("C", 1, owner=OTHERS[1])
    service = make_service(fake, immich_config, tagging_config, tmp_path)
    service.share_many([b], "ania@example.com", "viewer")

    out = service.share_many([a, b, theirs, "missing-id"], "ania@example.com", "editor")
    status = {r["album_id"]: r["status"] for r in out["results"]}
    assert status == {a: "shared", b: "unchanged", theirs: "skipped", "missing-id": "error"}
    assert out["counts"] == {"shared": 1, "unchanged": 1, "skipped": 1, "error": 1}
    assert fake.albums[a]["albumUsers"][-1]["role"] == "editor"

    out = service.share_many([a, b], "ania@example.com", "viewer", remove=True)
    assert out["counts"] == {"removed": 2}
    assert all(len(fake.albums[x]["albumUsers"]) == 1 for x in (a, b))


def test_bulk_share_api(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    ids = [fake.add_album(f"A{i}", 1) for i in range(3)]
    with app_client(fake, immich_config, tagging_config, tmp_path) as http:
        body = {"album_ids": ids, "users": ["tomek@example.com"], "role": "viewer"}
        assert http.post("/api/sharing/bulk", json=body).json()["counts"] == {"shared": 3}
        bad = {"album_ids": ids, "users": ["nobody@example.com"]}
        assert http.post("/api/sharing/bulk", json=bad).status_code == 400
        assert http.post("/api/sharing/bulk", json={**body, "role": "admin"}).status_code == 400
        status = http.get("/api/status").json()
        assert status["prefetch"]["enabled"] is True and "version" in status


def test_operation_log_api_and_polish(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    album_id = fake.add_album("Lato 2021", 2, foreign=1)
    settings = {"language": "polish", "ui": {"async_actions": True, "toast_ms": 3000}}
    with app_client(fake, immich_config, tagging_config, tmp_path, app=settings) as http:
        i18n = http.get("/api/i18n").json()
        assert i18n["lang"] == "polish" and i18n["lang_code"] == "pl"
        assert i18n["messages"]["logs.status.error"] == "BŁĄD"
        assert i18n["settings"]["ui"]["toast_ms"] == 3000
        assert (tmp_path / "language" / "polish.lan").exists()

        result = http.post(f"/api/albums/{album_id}/apply", json={"tags": ["x"]}).json()
        assert "1 z 2 zdjęć należy do innych użytkowników" in result["warnings"][0]
        error = http.post(f"/api/albums/{album_id}/apply", json={"tags": [" / "]})
        assert error.status_code == 400 and "Po normalizacji" in error.json()["detail"]

        entries = http.get("/api/log").json()
        assert [e["status"] for e in entries] == ["error", "success"]
        assert entries[1]["id"] == result["log_id"] and entries[1]["details"]["tags"] == ["x"]
        assert (tmp_path / "logs" / "operations.log").read_text().count("\n") == 2
        assert http.delete("/api/log").json() == {"ok": True}
        assert http.get("/api/log").json() == []


def test_login_with_immich_accounts(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    album_id = fake.add_album("A", 1)
    settings = {"auth": {"mode": "immich"}}
    with app_client(fake, immich_config, tagging_config, tmp_path, app=settings) as http:
        assert http.get("/api/i18n").json()["settings"]["auth_mode"] == "immich"
        assert http.get("/api/albums").status_code == 401
        wrong = http.post("/api/login", json={"username": "alex@example.com", "password": "x"})
        assert wrong.status_code == 401 and "Immich" in wrong.json()["detail"]
        other = http.post(
            "/api/login", json={"username": "ania@example.com", "password": "pw-ania"}
        )
        assert other.status_code == 401 and "ania@example.com" in other.json()["detail"]
        ok = http.post("/api/login", json={"username": "alex@example.com", "password": "pw-alex"})
        assert ok.status_code == 200 and ok.json()["user"] == "alex@example.com"
        assert not fake.login_sessions  # Immich sessions of the checks were closed
        status = http.get("/api/status").json()
        assert status["auth"] == {"enabled": True, "mode": "immich", "user": "alex@example.com"}
        http.post(f"/api/albums/{album_id}/apply", json={"tags": ["x"]})
        assert http.get("/api/log").json()[0]["by"] == "alex@example.com"


def test_immich_login_allowed_users(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    settings = {"auth": {"mode": "immich", "allowed_users": ["ANIA@example.com"]}}
    with app_client(fake, immich_config, tagging_config, tmp_path, app=settings) as http:
        ania = http.post("/api/login", json={"username": "ania@example.com", "password": "pw-ania"})
        assert ania.status_code == 200
        alex = http.post("/api/login", json={"username": "alex@example.com", "password": "pw-alex"})
        assert alex.status_code == 401  # not on the list, even as key owner


def test_credentials_mode_requires_file(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    with pytest.raises(SystemExit, match="credentials"):
        app_client(
            fake, immich_config, tagging_config, tmp_path, app={"auth": {"mode": "credentials"}}
        )


def test_prefetch_follows_the_filtered_list(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    old = [fake.add_album(f"Album {i} 2019", 1) for i in range(3)]
    new = [fake.add_album(f"Wakacje {i} 2024", 1) for i in range(3)]
    tagging_config.prefetch.albums = 2
    with app_client(fake, immich_config, tagging_config, tmp_path) as http:
        worker = http.app.state.service.prefetcher
        assert worker.run_once() == old[0]  # whole list: list order

        # The user types "2024": the first albums of that list come next.
        focus = http.post("/api/prefetch/focus", json={"album_ids": new})
        assert focus.json() == {"focus": 3}
        assert worker.run_once() == new[0] and worker.run_once() == new[1]
        assert worker.run_once() is None
        assert worker.status()["window"] == 2
        prepared = {a["id"] for a in http.get("/api/albums").json() if a["prepared"]}
        assert prepared == {old[0], new[0], new[1]}  # the one outside the filter is kept

        http.post("/api/prefetch/focus", json={"album_ids": []})  # filter cleared
        assert worker.run_once() == old[1]
