"""In-memory fake of the Immich API, used by tests and by scripts/dev_server.py."""

from __future__ import annotations

import json
import uuid

import httpx

ME = {"id": "11111111-1111-4111-8111-111111111111", "email": "alex@example.com", "name": "Alex"}
# Immich account passwords, for /auth/login
PASSWORDS = {"alex@example.com": "pw-alex", "ania@example.com": "pw-ania"}
OTHERS = [
    {"id": "22222222-2222-4222-8222-222222222222", "email": "ania@example.com", "name": "Ania"},
    {"id": "33333333-3333-4333-8333-333333333333", "email": "tomek@example.com", "name": "Tomek"},
]


def new_id() -> str:
    return str(uuid.uuid4())


class FakeImmich:
    """Implements just enough of the Immich API for this app.

    search_mode   "filter" (Immich >= 3.2) or "legacy"
    drop_links    skip the first N assets of each tagging batch (lost links)
    read_only     simulate a read-only external library: tag and description changes are
                  accepted, then silently reverted before the next read (as Immich does when
                  it cannot write the .xmp sidecar)
    members_only  albums are visible only to their owner and the people they are shared
                  with (as in Immich); by default the key "secret" (ME) sees every album

    API keys: "secret" belongs to ME with every permission. More keys are created with
    POST /api-keys (with a login session token); each acts as its user with its permissions.
    """

    def __init__(
        self,
        search_mode: str = "filter",
        drop_links: int = 0,
        read_only: bool = False,
        lag_searches: int = 0,
        broken_files: int = 0,
        members_only: bool = False,
    ):
        self.members_only = members_only
        self.search_mode = search_mode
        self.drop_links = drop_links
        self.read_only = read_only
        # Description updates of the last asset show up only after this many searches
        # (Immich's job queue still busy).
        self.lag_searches = lag_searches
        self._lagged: list[list] = []  # [asset, value, searches left]
        # The last N assets of each album cannot keep descriptions (unwritable files).
        self.broken_files = broken_files
        self._broken: set[str] = set()
        self.albums: dict[str, dict] = {}
        self.album_assets: dict[str, list[str]] = {}
        self.assets: dict[str, dict] = {}  # id -> {ownerId, libraryId, description}
        self.tags: dict[str, dict] = {}
        self.asset_tags: dict[str, set[str]] = {}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.login_sessions: dict[str, dict] = {}  # open Immich sessions: token -> user
        # secret -> {id, name, permissions, user}
        self.api_keys: dict[str, dict] = {
            "secret": {"id": new_id(), "name": "test key", "permissions": ["all"], "user": ME}
        }
        self._pending_revert: list[tuple[str, str, object]] = []

    # ------------------------------------------------------------------ seeding

    def add_album(
        self,
        name: str,
        n_assets: int,
        owner: dict | None = None,
        foreign: int = 0,
        external: bool = False,
        descriptions: list[str] | None = None,
        **extra,
    ) -> str:
        owner = owner or ME
        album_id = new_id()
        assets = [new_id() for _ in range(n_assets)]
        self.albums[album_id] = {
            "id": album_id,
            "albumName": name,
            "assetCount": n_assets,
            "description": extra.get("description", ""),
            "albumThumbnailAssetId": assets[0] if assets else None,
            "startDate": extra.get("startDate"),
            "endDate": extra.get("endDate"),
            "shared": False,
            "albumUsers": [{"user": dict(owner), "role": "owner"}],
        }
        self.album_assets[album_id] = assets
        if self.broken_files:
            self._broken.update(assets[-self.broken_files :])
        for index, asset in enumerate(assets):
            asset_owner = OTHERS[0]["id"] if index < foreign else owner["id"]
            text = descriptions[index] if descriptions and index < len(descriptions) else ""
            self.assets[asset] = {
                "originalFileName": f"DSC_{index:04d}.JPG",
                "ownerId": asset_owner,
                "libraryId": "lib-1" if external else None,
                "description": text,
            }
            self.asset_tags[asset] = set()
        return album_id

    # ------------------------------------------------------------------ helpers

    def _revert_pending(self) -> None:
        for kind, asset, value in self._pending_revert:
            if kind == "tags":
                self.asset_tags[asset] = value
            else:
                self.assets[asset]["description"] = value
        self._pending_revert.clear()

    def _upsert(self, value: str, user_id: str = ME["id"]) -> dict:
        parent = None
        tag = None
        path = []
        for part in value.split("/"):
            path.append(part)
            full = "/".join(path)
            tag = next(
                (t for t in self._user_tags(user_id) if t["value"] == full),
                None,
            )
            if tag is None:
                tag = {
                    "id": new_id(),
                    "name": part,
                    "value": full,
                    "parentId": parent,
                    "createdAt": "x",
                    "updatedAt": "x",
                    "userId": user_id,
                }
                self.tags[tag["id"]] = tag
            parent = tag["id"]
        return tag

    def _user_tags(self, user_id: str) -> list[dict]:
        return [t for t in self.tags.values() if t.get("userId", ME["id"]) == user_id]

    def keys_of(self, user: dict) -> list[dict]:
        return [k for k in self.api_keys.values() if k["user"]["id"] == user["id"]]

    def _can_see(self, album: dict, user: dict) -> bool:
        if not self.members_only:
            return True
        return any(u["user"]["id"] == user["id"] for u in album["albumUsers"])

    def _item(self, asset_id: str, with_exif: bool) -> dict:
        info = self.assets[asset_id]
        item = {
            "id": asset_id,
            "ownerId": info["ownerId"],
            "libraryId": info["libraryId"],
            "originalFileName": info["originalFileName"],
        }
        if with_exif:
            item["exifInfo"] = {"description": info["description"]}
        return item

    def _page(self, ids: list[str], body: dict) -> dict:
        size = body.get("size", 250)
        with_exif = bool(body.get("withExif"))
        if self.search_mode == "filter":
            start = int(body.get("cursor", "0"))
            chunk = ids[start : start + size]
            nxt = str(start + size) if start + size < len(ids) else None
            return {
                "assets": {
                    "items": [self._item(i, with_exif) for i in chunk],
                    "nextCursor": nxt,
                    "count": len(chunk),
                    "total": len(ids),
                    "facets": [],
                    "nextPage": None,
                }
            }
        page = body.get("page", 1)
        chunk = ids[(page - 1) * size : page * size]
        nxt = str(page + 1) if page * size < len(ids) else None
        return {
            "assets": {
                "items": [self._item(i, with_exif) for i in chunk],
                "nextPage": nxt,
                "count": len(chunk),
                "total": len(ids),
                "facets": [],
            }
        }

    # ------------------------------------------------------------------ routing

    def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: C901 - a router
        path = request.url.path.removeprefix("/api")
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if path == "/auth/login":  # public, no API key
            everyone = {u["email"]: u for u in [ME, *OTHERS]}
            user = everyone.get(body.get("email", ""))
            if not user or PASSWORDS.get(user["email"]) != body.get("password"):
                return httpx.Response(401, json={"message": "Incorrect email or password"})
            token = f"tok-{new_id()}"
            self.login_sessions[token] = user
            return httpx.Response(
                201,
                json={
                    "accessToken": token,
                    "userId": user["id"],
                    "userEmail": user["email"],
                    "name": user["name"],
                    "isAdmin": False,
                },
            )
        if path == "/auth/logout":
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            self.login_sessions.pop(token, None)
            return httpx.Response(200, json={"successful": True})
        if path == "/server/version":  # public
            return httpx.Response(200, json={"major": 3, "minor": 2, "patch": 1})
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        key = self.api_keys.get(request.headers.get("x-api-key", ""))
        if token in self.login_sessions:
            user, granted = self.login_sessions[token], ["all"]
        elif key:
            user, granted = key["user"], key["permissions"]
        else:
            return httpx.Response(401, json={"message": "Invalid API key"})
        method = request.method
        parts = path.strip("/").split("/")
        needed = _permission(method, path, parts)
        if needed and "all" not in granted and needed not in granted:
            return httpx.Response(403, json={"message": f"Missing required permission: {needed}"})

        if parts[0] == "api-keys":
            via_key = None if token in self.login_sessions else key
            return self._api_keys(method, parts, body, user, via_key)
        if path == "/users/me":
            return httpx.Response(200, json=user)
        if path == "/users":
            return httpx.Response(200, json=[ME, *OTHERS])

        if path == "/albums":
            visible = [a for a in self.albums.values() if self._can_see(a, user)]
            return httpx.Response(200, json=visible)
        if parts[0] == "albums":
            album = self.albums.get(parts[1])
            if album is None or not self._can_see(album, user):
                return httpx.Response(404, json={"message": "Not found"})
            if len(parts) == 2 and method == "GET":
                return httpx.Response(200, json=album)
            if len(parts) == 2 and method == "PATCH":
                album.update({k: v for k, v in body.items() if k in ("description", "albumName")})
                return httpx.Response(200, json=album)
            if parts[2:] == ["users"] and method == "PUT":
                everyone = {u["id"]: u for u in [ME, *OTHERS]}
                for entry in body["albumUsers"]:
                    album["albumUsers"].append(
                        {"user": everyone[entry["userId"]], "role": entry.get("role", "editor")}
                    )
                album["shared"] = True
                return httpx.Response(200, json=album)
            if len(parts) == 4 and parts[2] == "user" and method == "DELETE":
                album["albumUsers"] = [
                    u for u in album["albumUsers"] if u["user"]["id"] != parts[3]
                ]
                return httpx.Response(204)

        if path == "/search/metadata":
            self._revert_pending()
            for entry in self._lagged:
                entry[2] -= 1
                if entry[2] < 0:
                    self.assets[entry[0]]["description"] = entry[1]
            self._lagged = [e for e in self._lagged if e[2] >= 0]
            if self.search_mode == "legacy" and "filter" in body:
                return httpx.Response(400, json={"message": ["property filter should not exist"]})
            flt = body.get("filter") or {}
            album_ids = flt.get("albumIds", {}).get("any") or body.get("albumIds") or []
            tag_ids = flt.get("tagIds", {}).get("any") or body.get("tagIds") or []
            ids = [a for album in album_ids for a in self.album_assets[album]]
            if tag_ids:
                ids = [a for a in ids if self.asset_tags[a] & set(tag_ids)]
            return httpx.Response(200, json=self._page(ids, body))

        if path == "/tags" and method == "GET":
            return httpx.Response(200, json=self._user_tags(user["id"]))
        if path == "/tags" and method == "PUT":
            return httpx.Response(200, json=[self._upsert(v, user["id"]) for v in body["tags"]])
        if parts[0] == "tags" and parts[2:] == ["assets"] and method == "DELETE":
            tag_id = parts[1]
            if tag_id not in self.tags:
                return httpx.Response(400, json={"message": "Tag not found"})
            results = []
            for asset in body["ids"]:
                if self.assets[asset]["ownerId"] != user["id"]:
                    results.append({"id": asset, "success": False, "error": "no_permission"})
                    continue
                if self.read_only:
                    self._pending_revert.append(("tags", asset, set(self.asset_tags[asset])))
                found = tag_id in self.asset_tags[asset]
                self.asset_tags[asset].discard(tag_id)
                results.append({"id": asset, "success": found})
            return httpx.Response(200, json=results)
        if path == "/tags/assets":
            count = 0
            assets = body["assetIds"][self.drop_links :] if self.drop_links else body["assetIds"]
            for asset in assets:
                if self.assets[asset]["ownerId"] != user["id"]:
                    continue  # Immich skips assets the user does not own
                if self.read_only:
                    self._pending_revert.append(("tags", asset, set(self.asset_tags[asset])))
                for tag in body["tagIds"]:
                    if tag not in self.asset_tags[asset]:
                        self.asset_tags[asset].add(tag)
                        count += 1
            return httpx.Response(200, json={"count": count})

        if path == "/assets" and method == "PUT":
            for asset in body["ids"]:
                if self.assets[asset]["ownerId"] != user["id"]:
                    return httpx.Response(400, json={"message": "Not found or no asset.update"})
                if self.read_only or asset in self._broken:
                    self._pending_revert.append(("desc", asset, self.assets[asset]["description"]))
                if self.lag_searches and asset == body["ids"][-1]:
                    self._lagged.append([asset, body["description"], self.lag_searches])
                    continue
                self.assets[asset]["description"] = body["description"]
            return httpx.Response(204)
        if path.endswith("/thumbnail"):
            return httpx.Response(
                200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"}
            )
        return httpx.Response(404, json={"message": f"no route {method} {path}"})

    def _api_keys(self, method, parts, body, user, via_key) -> httpx.Response:
        """/api-keys of ``user``; ``via_key`` is the calling key (None for a session)."""
        public = ("id", "name", "permissions")
        if method == "GET" and len(parts) == 1:
            listed = [{k: v[k] for k in public} for v in self.keys_of(user)]
            return httpx.Response(200, json=listed)
        if method == "POST" and len(parts) == 1:
            wanted = body["permissions"]
            granted = via_key["permissions"] if via_key else ["all"]
            if "all" not in granted and not set(wanted) <= set(granted):
                return httpx.Response(
                    400, json={"message": "Cannot grant permissions you do not have"}
                )
            secret = f"key-{new_id()}"
            key = {"id": new_id(), "name": body.get("name") or "API Key", "permissions": wanted}
            self.api_keys[secret] = {**key, "user": user}
            return httpx.Response(201, json={**key, "secret": secret, "apiKey": key})
        if method == "DELETE" and len(parts) == 2:
            for secret, key in list(self.api_keys.items()):
                if key["id"] == parts[1] and key["user"]["id"] == user["id"]:
                    del self.api_keys[secret]
                    return httpx.Response(204)
            return httpx.Response(400, json={"message": "API Key not found"})
        return httpx.Response(404, json={"message": "no route"})


def _permission(method: str, path: str, parts: list[str]) -> str | None:
    """The permission Immich requires for an endpoint used by the app."""
    if parts[0] == "api-keys":
        return {"GET": "apiKey.read", "POST": "apiKey.create", "DELETE": "apiKey.delete"}[method]
    if parts[0] == "users":
        return "user.read"
    if parts[0] == "albums":
        if len(parts) > 2:
            return "albumUser.create" if method == "PUT" else "albumUser.delete"
        return "album.update" if method == "PATCH" else "album.read"
    if path == "/search/metadata":
        return "asset.read"
    if path == "/tags":
        return "tag.read" if method == "GET" else "tag.create"
    if path == "/tags/assets" or (parts[0] == "tags" and parts[2:] == ["assets"]):
        return "tag.asset"
    if path.endswith("/thumbnail"):
        return "asset.view"
    if path == "/assets":
        return "asset.update"
    return None
