"""Apply takes back de-selected tags (sync); Remove takes back the whole tagging."""

import pytest
from conftest import make_service
from fakes import FakeImmich

from immich_album_tagger.service import Targets

ALL = Targets(tags=True, photo_description=True, album_description=True)


def tags_of(fake, asset_id):
    return {fake.tags[t]["value"] for t in fake.asset_tags[asset_id]}


def test_apply_takes_off_tags_no_longer_selected(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    album_id = fake.add_album("Wakacje 2019", 3)
    service = make_service(fake, immich_config, tagging_config, tmp_path)
    service.apply(album_id, ["rok/2019", "temat/plaza", "temat/las"])
    photos = fake.album_assets[album_id]
    manual = fake._upsert("osoby/ania")["id"]  # added by hand in Immich, not by the app
    fake.asset_tags[photos[0]].add(manual)

    dry = service.apply(album_id, ["rok/2019", "temat/morze"], dry_run=True)
    assert dry.removed_tags == ["temat/plaza", "temat/las"]
    assert tags_of(fake, photos[1]) == {"rok/2019", "temat/plaza", "temat/las"}  # untouched

    result = service.apply(album_id, ["rok/2019", "temat/morze"])
    assert result.removed_tags == ["temat/plaza", "temat/las"] and result.untagged == 3
    assert result.verified_removed == {"temat/plaza": 0, "temat/las": 0} and result.complete
    assert tags_of(fake, photos[0]) == {"rok/2019", "temat/morze", "osoby/ania"}
    assert tags_of(fake, photos[2]) == {"rok/2019", "temat/morze"}
    state = service.history.latest_by_album()[album_id]
    assert state["tags"] == ["rok/2019", "temat/morze"]  # current state, not every tag ever
    log = service.oplog.entries()[0]
    assert log["details"]["removed"] == ["temat/plaza", "temat/las"]

    again = service.apply(album_id, ["rok/2019", "temat/morze"])
    assert again.removed_tags == [] and again.untagged == 0


def test_photo_in_another_tagged_album_keeps_the_tag(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    trip = fake.add_album("Wakacje", 2)
    beaches = fake.add_album("Plaże", 1)
    shared = fake.album_assets[trip][0]
    fake.album_assets[beaches].append(shared)  # the same photo in both albums
    service = make_service(fake, immich_config, tagging_config, tmp_path)
    service.apply(trip, ["temat/plaza", "rok/2019"])
    service.apply(beaches, ["temat/plaza"])

    result = service.apply(trip, ["rok/2019"])
    assert result.untagged == 1 and result.kept_elsewhere == 1
    assert "temat/plaza" in tags_of(fake, shared)  # Plaże still holds it
    assert "temat/plaza" not in tags_of(fake, fake.album_assets[trip][1])
    assert result.complete and any("another tagged album" in w for w in result.warnings)


def test_no_sync_without_the_tags_target(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    album_id = fake.add_album("A", 2)
    service = make_service(fake, immich_config, tagging_config, tmp_path)
    service.apply(album_id, ["x", "y"])
    only_text = Targets(tags=False, photo_description=True)
    result = service.apply(album_id, ["x"], targets=only_text)
    assert result.removed_tags == []
    assert tags_of(fake, fake.album_assets[album_id][0]) == {"x", "y"}


def test_remove_takes_back_everything(immich_config, tagging_config, tmp_path):
    fake = FakeImmich()
    album_id = fake.add_album(
        "Sylwester", 2, description="Impreza u Ani", descriptions=["Fajerwerki", ""]
    )
    other = fake.add_album("Other", 1)
    service = make_service(fake, immich_config, tagging_config, tmp_path)
    service.apply(album_id, ["wydarzenie/sylwester"], ["sylwester", "zielona"], ALL)
    service.apply(other, ["x"])
    photos = fake.album_assets[album_id]
    assert "#wydarzenie/sylwester" in fake.assets[photos[0]]["description"]

    dry = service.remove(album_id, dry_run=True)
    assert dry.removed_tags == ["wydarzenie/sylwester"] and dry.dry_run
    assert tags_of(fake, photos[0]) == {"wydarzenie/sylwester"}

    result = service.remove(album_id)
    assert result.complete and result.untagged == 2
    assert all(tags_of(fake, p) == set() for p in photos)
    assert fake.assets[photos[0]]["description"] == "Fajerwerki"  # own text kept
    assert fake.assets[photos[1]]["description"] == ""
    assert fake.albums[album_id]["description"] == "Impreza u Ani"
    assert album_id not in service.history.latest_by_album()  # "not tagged" again
    assert other in service.history.latest_by_album()
    entry = service.oplog.entries()[0]
    assert entry["action"] == "remove" and entry["status"] == "success"

    with pytest.raises(ValueError, match="not tagged"):
        service.remove(album_id)

    # Tagging again afterwards starts from scratch.
    service.apply(album_id, ["nowy"])
    assert service.history.latest_by_album()[album_id]["tags"] == ["nowy"]


def test_tags_from_before_sync_are_all_taken_off(immich_config, tagging_config, tmp_path):
    """0.8.0 only added tags; every tag it ever applied counts as the app's."""
    fake = FakeImmich()
    album_id = fake.add_album("A", 1)
    service = make_service(fake, immich_config, tagging_config, tmp_path)
    for tags in (["a"], ["b"]):  # as two 0.8.0 runs recorded them
        service.history.record(album_id, "A", tags, [], {"tags": True}, 1, 1, True)
        tag_id = fake._upsert(tags[0])["id"]
        fake.asset_tags[fake.album_assets[album_id][0]].add(tag_id)
    assert service.history.latest_by_album()[album_id]["tags"] == ["a", "b"]
    result = service.apply(album_id, ["c"])
    assert result.removed_tags == ["a", "b"]
    assert tags_of(fake, fake.album_assets[album_id][0]) == {"c"}


def test_remove_api_and_cli(immich_config, tagging_config, tmp_path, monkeypatch, capsys):
    from test_service_and_app import app_client

    fake = FakeImmich()
    album_id = fake.add_album("Lato 2021", 1)
    with app_client(fake, immich_config, tagging_config, tmp_path) as http:
        http.post(f"/api/albums/{album_id}/apply", json={"tags": ["a", "b"]})
        synced = http.post(f"/api/albums/{album_id}/apply", json={"tags": ["a"]}).json()
        assert synced["removed_tags"] == ["b"] and synced["history"]["removed"] == ["b"]
        assert http.get("/api/albums").json()[0]["applied"]["tags"] == ["a"]
        assert http.get("/api/status").json()["preselect"] == ["first"]
        dry = http.post(f"/api/albums/{album_id}/remove", json={"dry_run": True}).json()
        assert dry["removed_tags"] == ["a"]
        done = http.post(f"/api/albums/{album_id}/remove", json={}).json()
        assert done["complete"] is True
        assert http.get("/api/albums").json()[0]["applied"] is None
        again = http.post(f"/api/albums/{album_id}/remove", json={})
        assert again.status_code == 400

    from immich_album_tagger.__main__ import main

    assert main(["--config-dir", str(tmp_path), "remove"]) == 2  # needs a selection
    assert "--match, --album-id or --all" in capsys.readouterr().err
