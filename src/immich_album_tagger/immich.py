"""Thin client for the parts of the Immich REST API this tool needs.

Endpoints used (all relative to ``<IMMICH_URL>/api``):

* ``GET  /server/version``, ``GET /users/me``   - connectivity, who the API key belongs to
* ``GET  /users``                               - users to share albums with
* ``GET  /albums``, ``GET|PATCH /albums/{id}``  - album list, details, album description
* ``PUT  /albums/{id}/users``, ``DELETE /albums/{id}/user/{userId}`` - sharing
* ``POST /search/metadata``                     - assets of an album (optionally by tag)
* ``GET  /tags``, ``PUT /tags``, ``PUT /tags/assets`` - tags
* ``DELETE /tags/{id}/assets``                  - take a tag off photos (sync, remove)
* ``PUT  /assets``                              - photo descriptions (bulk)
* ``GET  /assets/{id}/thumbnail``               - album cover for the UI
* ``POST /auth/login``, ``POST /auth/logout``   - web UI login with an Immich account
* ``GET|POST /api-keys``, ``DELETE /api-keys/{id}`` - per-login keys (auth.mode immich_keys)
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from .config import ImmichConfig

log = logging.getLogger(__name__)

_MAX_PAGES = 10_000  # safety net against a server that never stops paginating


class ImmichError(RuntimeError):
    """An Immich API call failed."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class AlbumUser:
    id: str
    email: str
    name: str
    role: str  # owner | editor | viewer


@dataclass(frozen=True)
class Album:
    id: str
    name: str
    asset_count: int
    description: str = ""
    thumbnail_asset_id: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    shared: bool = False
    users: tuple[AlbumUser, ...] = field(default=())

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Album:
        def day(value: Any) -> str | None:
            return str(value)[:10] if value else None

        users = tuple(
            AlbumUser(
                id=item["user"]["id"],
                email=item["user"].get("email", ""),
                name=item["user"].get("name", ""),
                role=item.get("role", ""),
            )
            for item in data.get("albumUsers") or []
            if item.get("user")
        )
        return cls(
            id=data["id"],
            name=data.get("albumName") or "",
            asset_count=int(data.get("assetCount") or 0),
            description=data.get("description") or "",
            thumbnail_asset_id=data.get("albumThumbnailAssetId"),
            start_date=day(data.get("startDate")),
            end_date=day(data.get("endDate")),
            shared=bool(data.get("shared")),
            users=users,
        )

    @property
    def owner(self) -> AlbumUser | None:
        return next((u for u in self.users if u.role == "owner"), None)

    @property
    def date_range(self) -> str:
        if self.start_date and self.end_date and self.start_date != self.end_date:
            return f"{self.start_date} - {self.end_date}"
        return self.start_date or self.end_date or ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        owner = self.owner
        data.update(
            date_range=self.date_range,
            owner_id=owner.id if owner else None,
            owner_email=owner.email if owner else None,
        )
        return data


@dataclass(frozen=True)
class AssetInfo:
    """The bits of an asset needed to tag it and edit its description."""

    id: str
    owner_id: str
    library_id: str | None = None
    description: str = ""
    file_name: str = ""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> AssetInfo:
        return cls(
            id=data["id"],
            owner_id=data.get("ownerId") or "",
            library_id=data.get("libraryId"),
            description=(data.get("exifInfo") or {}).get("description") or "",
            file_name=data.get("originalFileName") or "",
        )


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class ImmichClient:
    def __init__(
        self,
        config: ImmichConfig,
        transport: httpx.BaseTransport | None = None,
        api_key: str | None = None,
    ):
        """Acts with ``api_key``, by default the one from immich.yaml."""
        self.config = config
        self._transport = transport
        self._search_mode = config.search_api
        self._me: dict[str, Any] | None = None
        headers = {"Accept": "application/json"}
        key = config.api_key if api_key is None else api_key
        if key:
            headers["x-api-key"] = key
        self._http = httpx.Client(
            base_url=config.url,
            headers=headers,
            timeout=config.timeout_seconds,
            verify=config.verify_tls,
            transport=transport,
        )

        # Without the API key: for checking a person's own Immich login.
        self._anonymous = httpx.Client(
            base_url=config.url,
            headers={"Accept": "application/json"},
            timeout=config.timeout_seconds,
            verify=config.verify_tls,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()
        self._anonymous.close()

    def with_key(self, api_key: str) -> ImmichClient:
        """A new client with the same settings that acts with another API key."""
        return ImmichClient(self.config, self._transport, api_key=api_key)

    # ------------------------------------------------------------------ login, API keys

    def login(self, email: str, password: str) -> dict[str, str] | None:
        """Log in with an Immich account: {token, id, email, name}, or None if wrong."""
        try:
            response = self._anonymous.post(
                "/auth/login", json={"email": email.strip(), "password": password}
            )
        except httpx.HTTPError as exc:
            raise ImmichError(f"POST /auth/login: cannot reach Immich ({exc})") from exc
        if response.status_code in (400, 401, 403):
            return None
        if response.status_code >= 300:
            raise ImmichError(
                f"POST /auth/login -> HTTP {response.status_code}", status=response.status_code
            )
        data = response.json()
        return {
            "token": data.get("accessToken", ""),
            "id": data["userId"],
            "email": data.get("userEmail", ""),
            "name": data.get("name", ""),
        }

    def logout(self, token: str) -> None:
        """Close an Immich session opened by :meth:`login` (best effort)."""
        if not token:
            return
        try:
            self._anonymous.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            log.warning("Could not close the Immich session of the login: %s", exc)

    def verify_login(self, email: str, password: str) -> dict[str, str] | None:
        """Check an Immich e-mail/password. Returns {id, email, name} or None if wrong.

        The session Immich opens for the login is closed again right away.
        """
        user = self.login(email, password)
        if user is None:
            return None
        self.logout(user.pop("token"))
        return user

    def _session_request(self, token: str, method: str, path: str, **kwargs: Any) -> Any:
        """A request with a login session token instead of the API key."""
        headers = {"Authorization": f"Bearer {token}"}
        return self._request(method, path, client=self._anonymous, headers=headers, **kwargs)

    def list_api_keys(self, token: str) -> list[dict[str, Any]]:
        """API keys of the logged-in user: [{id, name, permissions, ...}] (no secrets)."""
        return self._session_request(token, "GET", "/api-keys") or []

    def create_api_key(self, token: str, name: str, permissions: list[str]) -> dict[str, str]:
        """Create a key for the logged-in user. Returns {id, secret}; Immich shows the secret
        only this once."""
        data = self._session_request(
            token, "POST", "/api-keys", json={"name": name, "permissions": permissions}
        )
        key_id = data.get("id") or data["apiKey"]["id"]  # also under "apiKey"
        return {"id": key_id, "secret": data["secret"]}

    def delete_api_key(self, key_id: str, token: str | None = None) -> None:
        """Delete a key of the user, with a session token or (no token) with this client's
        key, which may be the key itself (needs apiKey.delete)."""
        if token:
            self._session_request(token, "DELETE", f"/api-keys/{key_id}")
        else:
            self._request("DELETE", f"/api-keys/{key_id}")

    # ------------------------------------------------------------------ plumbing

    def _request(
        self, method: str, path: str, client: httpx.Client | None = None, **kwargs: Any
    ) -> Any:
        try:
            response = (client or self._http).request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ImmichError(f"{method} {path}: cannot reach Immich ({exc})") from exc
        if response.status_code >= 400:
            detail = response.text[:300].strip()
            raise ImmichError(
                f"{method} {path} -> HTTP {response.status_code}: {detail}",
                status=response.status_code,
            )
        if not response.content:
            return None
        if "json" in response.headers.get("content-type", ""):
            return response.json()
        return response.content

    # ------------------------------------------------------------------ server, users

    def me(self) -> dict[str, Any]:
        """The user the API key belongs to (cached)."""
        if self._me is None:
            self._me = self._request("GET", "/users/me")
        return self._me

    def check(self) -> dict[str, Any]:
        version = self._request("GET", "/server/version")
        user = self.me()
        return {
            "version": "{major}.{minor}.{patch}".format(**version),
            "user": user.get("email") or user.get("name"),
            "user_id": user.get("id"),
        }

    def list_users(self) -> list[dict[str, str]]:
        users = self._request("GET", "/users") or []
        return sorted(
            (
                {"id": u["id"], "email": u.get("email", ""), "name": u.get("name", "")}
                for u in users
            ),
            key=lambda u: u["email"].casefold(),
        )

    def find_user(self, email_or_id: str) -> dict[str, str] | None:
        wanted = email_or_id.strip().casefold()
        for user in self.list_users():
            if wanted in (user["id"].casefold(), user["email"].casefold()):
                return user
        return None

    # ------------------------------------------------------------------ albums

    def list_albums(self) -> list[Album]:
        data = self._request("GET", "/albums")
        seen: dict[str, Album] = {}
        for item in data:
            album = Album.from_api(item)
            seen.setdefault(album.id, album)
        return sorted(seen.values(), key=lambda a: a.name.casefold())

    def get_album(self, album_id: str) -> Album:
        return Album.from_api(self._request("GET", f"/albums/{album_id}"))

    def update_album_description(self, album_id: str, description: str) -> None:
        self._request("PATCH", f"/albums/{album_id}", json={"description": description})

    def share_album(self, album_id: str, user_id: str, role: str) -> None:
        self._request(
            "PUT",
            f"/albums/{album_id}/users",
            json={"albumUsers": [{"userId": user_id, "role": role}]},
        )

    def unshare_album(self, album_id: str, user_id: str) -> None:
        self._request("DELETE", f"/albums/{album_id}/user/{user_id}")

    # ------------------------------------------------------------------ album assets

    def album_assets(
        self, album_id: str, tag_id: str | None = None, with_exif: bool = False
    ) -> list[AssetInfo]:
        """All assets of an album; with ``tag_id`` only those carrying that tag."""
        if self._search_mode in ("auto", "filter"):
            try:
                items = self._search_filter(album_id, tag_id, with_exif)
                self._search_mode = "filter"
                return items
            except ImmichError as exc:
                if self._search_mode == "filter" or exc.status not in (400, 422):
                    raise
                log.info("Server rejected filter search (%s); using legacy search", exc.status)
                self._search_mode = "legacy"
        return self._search_legacy(album_id, tag_id, with_exif)

    def album_asset_ids(self, album_id: str, tag_id: str | None = None) -> list[str]:
        return [asset.id for asset in self.album_assets(album_id, tag_id)]

    def _visibility(self) -> list[str]:
        return ["timeline", "archive"] if self.config.include_archived else ["timeline"]

    def _search_filter(self, album_id: str, tag_id: str | None, with_exif: bool) -> list[AssetInfo]:
        """Immich >= 3.2: structured ``filter`` with cursor pagination."""
        search_filter: dict[str, Any] = {
            "albumIds": {"any": [album_id]},
            "visibility": {"in": self._visibility()},
        }
        if tag_id:
            search_filter["tagIds"] = {"any": [tag_id]}
        body: dict[str, Any] = {"filter": search_filter, "size": self.config.page_size}
        if with_exif:
            body["withExif"] = True
        return self._paginate(body, cursor_mode=True)

    def _search_legacy(self, album_id: str, tag_id: str | None, with_exif: bool) -> list[AssetInfo]:
        """Immich < 3.2: ``albumIds`` / ``tagIds`` with page numbers."""
        body: dict[str, Any] = {"albumIds": [album_id], "size": self.config.page_size, "page": 1}
        if tag_id:
            body["tagIds"] = [tag_id]
        if with_exif:
            body["withExif"] = True
        return self._paginate(body, cursor_mode=False)

    def _paginate(self, body: dict[str, Any], cursor_mode: bool) -> list[AssetInfo]:
        assets: list[AssetInfo] = []
        for _ in range(_MAX_PAGES):
            data = self._request("POST", "/search/metadata", json=body)["assets"]
            assets.extend(AssetInfo.from_api(item) for item in data["items"])
            token = data.get("nextCursor") if cursor_mode else data.get("nextPage")
            if not token or not data["items"]:
                return assets
            if cursor_mode:
                body["cursor"] = token
            else:
                body["page"] = int(token)
        raise ImmichError("Pagination did not finish; aborting")

    # ------------------------------------------------------------------ tags

    def list_tags(self) -> list[dict[str, Any]]:
        return self._request("GET", "/tags") or []

    def tag_values(self) -> list[str]:
        return sorted({tag["value"] for tag in self.list_tags()}, key=str.casefold)

    def upsert_tags(self, names: Sequence[str]) -> dict[str, str]:
        """Create missing tags (parents included) and return ``{name: tag_id}``."""
        if not names:
            return {}
        returned = self._request("PUT", "/tags", json={"tags": list(names)}) or []
        by_value = {tag["value"].casefold(): tag["id"] for tag in returned}
        result: dict[str, str] = {}
        missing: list[str] = []
        for name in names:
            tag_id = by_value.get(name.casefold())
            if tag_id:
                result[name] = tag_id
            else:
                missing.append(name)
        if missing:
            all_tags = {tag["value"].casefold(): tag["id"] for tag in self.list_tags()}
            for name in missing:
                if name.casefold() not in all_tags:
                    raise ImmichError(f"Tag {name!r} was not created by the server")
                result[name] = all_tags[name.casefold()]
        return result

    def tag_assets(self, tag_ids: Sequence[str], asset_ids: Sequence[str]) -> int:
        """Attach every tag to every asset. Returns the number of new links reported.

        Immich silently skips assets the key's user does not own.
        """
        total = 0
        for chunk in _chunks(list(asset_ids), self.config.tag_batch_size):
            data = self._request(
                "PUT", "/tags/assets", json={"tagIds": list(tag_ids), "assetIds": list(chunk)}
            )
            total += int((data or {}).get("count", 0))
        return total

    def untag_assets(self, tag_id: str, asset_ids: Sequence[str]) -> None:
        """Take one tag off the given assets (batched)."""
        for chunk in _chunks(list(asset_ids), self.config.tag_batch_size):
            self._request("DELETE", f"/tags/{tag_id}/assets", json={"ids": list(chunk)})

    # ------------------------------------------------------------------ assets

    def set_descriptions(self, description: str, asset_ids: Sequence[str]) -> None:
        """Give all ``asset_ids`` the same description (bulk update)."""
        for chunk in _chunks(list(asset_ids), self.config.tag_batch_size):
            self._request("PUT", "/assets", json={"ids": list(chunk), "description": description})

    def thumbnail(self, asset_id: str) -> tuple[bytes, str]:
        try:
            response = self._http.get(f"/assets/{asset_id}/thumbnail", params={"size": "thumbnail"})
        except httpx.HTTPError as exc:
            raise ImmichError(f"thumbnail {asset_id}: {exc}") from exc
        if response.status_code >= 400:
            raise ImmichError(
                f"thumbnail {asset_id}: HTTP {response.status_code}", status=response.status_code
            )
        return response.content, response.headers.get("content-type", "image/jpeg")
