import httpx
import pytest
from fakes import FakeImmich

from immich_album_tagger.immich import ImmichClient, ImmichError


def client(fake, config):
    return ImmichClient(config, transport=httpx.MockTransport(fake.handler))


def test_album_assets_paginate_with_cursor(immich_config):
    fake = FakeImmich("filter")
    album_id = fake.add_album("A", 5)
    ids = client(fake, immich_config).album_asset_ids(album_id)
    assert ids == fake.album_assets[album_id]
    searches = [body for method, path, body in fake.calls if path == "/search/metadata"]
    assert len(searches) == 3 and searches[0]["filter"]["albumIds"] == {"any": [album_id]}


def test_auto_mode_falls_back_to_legacy_search(immich_config):
    fake = FakeImmich("legacy")
    album_id = fake.add_album("A", 3)
    c = client(fake, immich_config)
    assert c.album_asset_ids(album_id) == fake.album_assets[album_id]
    assert c._search_mode == "legacy"


def test_filter_mode_does_not_fall_back(immich_config):
    fake = FakeImmich("legacy")
    album_id = fake.add_album("A", 3)
    with pytest.raises(ImmichError):
        client(fake, immich_config.model_copy(update={"search_api": "filter"})).album_asset_ids(
            album_id
        )


def test_upsert_returns_leaf_ids_for_nested_tags(immich_config):
    fake = FakeImmich()
    ids = client(fake, immich_config).upsert_tags(["miejsce/chorwacja", "rok/2019"])
    assert {fake.tags[i]["value"] for i in ids.values()} == {"miejsce/chorwacja", "rok/2019"}
    assert len(fake.tags) == 4  # parents created too


def test_tag_assets_is_batched(immich_config):
    fake = FakeImmich()
    album_id = fake.add_album("A", 5)
    c = client(fake, immich_config)
    tag_ids = list(c.upsert_tags(["x"]).values())
    assert c.tag_assets(tag_ids, fake.album_assets[album_id]) == 5
    assert len([1 for _, path, _ in fake.calls if path == "/tags/assets"]) == 3


def test_bad_key_raises_with_status(immich_config):
    fake = FakeImmich()
    c = client(fake, immich_config.model_copy(update={"api_key": "wrong"}))
    with pytest.raises(ImmichError) as info:
        c.check()
    assert info.value.status == 401
