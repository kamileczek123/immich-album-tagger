"""Business logic shared by the web UI and the CLI."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import AppConfig
from .descriptions import merge_keyword_line, merge_tag_line, normalize_keywords, tag_line_of
from .history import History
from .i18n import Translator
from .immich import Album, AssetInfo, ImmichClient, ImmichError
from .llm import LLMPool
from .oplog import ERROR, SUCCESS, OperationLog
from .prefetch import Prefetcher
from .suggest import Suggester, SuggestionResult, normalize_tags

log = logging.getLogger(__name__)

SHARE_ROLES = ("viewer", "editor")


@dataclass
class Targets:
    """Where the chosen tags/keywords are written."""

    tags: bool = True
    photo_description: bool = False
    album_description: bool = False

    def any(self) -> bool:
        return self.tags or self.photo_description or self.album_description


@dataclass
class ApplyResult:
    album_id: str
    album_name: str
    tags: list[str]
    keywords: list[str]
    targets: Targets
    asset_count: int  # photos in the album
    own_count: int  # of which owned by the API key's user (only these can be changed)
    external_count: int = 0  # owned photos that live in an external library
    new_links: int = 0
    descriptions_updated: int = 0
    album_description_updated: bool = False
    dry_run: bool = False
    # Tags the app had put on the album earlier and Apply took off again (sync), how many
    # photos lost them, and how many kept them because another tagged album holds them too.
    removed_tags: list[str] = field(default_factory=list)
    untagged: int = 0  # photos that lost at least one tag
    kept_elsewhere: int = 0  # photos that kept a removed tag
    # After the verify delay: tag -> owned photos carrying it, and photos with the tag line.
    verified_tags: dict[str, int] | None = None
    verified_descriptions: int | None = None
    # Removed tag -> photos that still carry it although they should not.
    verified_removed: dict[str, int] | None = None
    warnings: list[str] = field(default_factory=list)
    history: dict[str, Any] | None = None
    log_id: str | None = None  # entry in the operation log

    @property
    def complete(self) -> bool | None:
        checks: list[bool] = []
        if self.verified_tags is not None:
            checks += [n >= self.own_count for n in self.verified_tags.values()]
        if self.verified_descriptions is not None:
            checks.append(self.verified_descriptions >= self.own_count)
        if self.verified_removed is not None:
            checks += [n == 0 for n in self.verified_removed.values()]
        return all(checks) if checks else None

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "complete": self.complete}


class TaggerService:
    def __init__(
        self,
        config: AppConfig,
        immich: ImmichClient,
        suggester: Suggester,
        history: History,
        llm: LLMPool | None = None,
        sleep: Callable[[float], None] = time.sleep,
        translator: Translator | None = None,
        oplog: OperationLog | None = None,
    ):
        self.config = config
        self.immich = immich
        self.suggester = suggester
        self.history = history
        self.llm = llm
        self._sleep = sleep
        self.t = translator or Translator.english()
        self.suggester.t = self.t
        self.oplog = oplog or OperationLog(config.log_dir)
        self._names: dict[str, str] = {}  # asset id -> file name, for messages
        self._owns_shared = True  # closes the LLM client; False for per-login copies
        self._focus: list[str] | None = None  # album order the user is looking at
        prefetch = config.tagging.prefetch
        # Albums prepared at once: as many as the LLM endpoints take, unless configured.
        workers = prefetch.workers or (getattr(llm, "capacity", 1) if llm else 1)
        self.prefetcher = (
            Prefetcher(prefetch, self._prefetch_window, self._compute_suggestions, workers=workers)
            if prefetch.enabled
            else None
        )

    def for_immich(self, immich: ImmichClient) -> TaggerService:
        """A copy acting with another Immich client (one per login in auth.mode immich_keys).

        Config, suggester, LLM pool, history and operation log are shared; the prefetch
        workers are the copy's own and prepare that user's albums (the shared LLM pool
        limits what runs at once). close() leaves the shared parts.
        """
        copy = TaggerService(
            self.config,
            immich,
            self.suggester,
            self.history,
            self.llm,
            sleep=self._sleep,
            translator=self.t,
            oplog=self.oplog,
        )
        copy._owns_shared = False
        return copy

    def start_background(self) -> None:
        """Start preparing suggestions in the background (web UI only)."""
        if self.prefetcher:
            self.prefetcher.start()

    def stop_background(self) -> None:
        if self.prefetcher:
            self.prefetcher.stop()

    @classmethod
    def from_config(cls, config: AppConfig, translator: Translator | None = None) -> TaggerService:
        immich = ImmichClient(config.immich)
        llm = LLMPool(config.llm) if config.llm.enabled else None
        suggester = Suggester(config.tagging, llm)
        history = History(config.data_dir / "history.sqlite3")
        oplog = OperationLog(config.log_dir)
        return cls(config, immich, suggester, history, llm, translator=translator, oplog=oplog)

    def close(self) -> None:
        if self.prefetcher:
            self.prefetcher.stop()
        self.immich.close()
        if self.llm and self._owns_shared:
            self.llm.close()

    def _me(self) -> str:
        """Id of the Immich user this service acts as."""
        return self.immich.me()["id"]

    # ------------------------------------------------------------------ queries

    def status(self) -> dict[str, Any]:
        status: dict[str, Any] = {
            "llm": {
                "enabled": self.llm is not None,
                "model": ", ".join(self.llm.models) if self.llm else None,
                "endpoints": self.llm.status() if self.llm else [],
            },
            "apply_defaults": self.config.tagging.apply.model_dump(),
            "preselect": list(self.config.tagging.preselect),
            "descriptions": self.config.tagging.descriptions.model_dump(),
            "prefetch": self.prefetcher.status() if self.prefetcher else {"enabled": False},
        }
        try:
            status["immich"] = {"ok": True, **self.immich.check()}
        except ImmichError as exc:
            status["immich"] = {"ok": False, "error": str(exc)}
        return status

    def list_albums(self) -> list[dict[str, Any]]:
        applied = self.history.latest_by_album(self._me())
        ready = self.prefetcher.ready_ids() if self.prefetcher else set()
        return [
            {**album.to_dict(), "applied": applied.get(album.id), "prepared": album.id in ready}
            for album in self.immich.list_albums()
        ]

    def existing_tags(self) -> list[str]:
        return self.immich.tag_values()

    def suggest(self, album_id: str, refresh: bool = False) -> tuple[Album, SuggestionResult, bool]:
        """Suggestions for an album; ``True`` as third value if they were prepared earlier."""
        if self.prefetcher and not refresh:
            prepared = self.prefetcher.get(album_id)
            if prepared:
                return prepared.album, prepared.result, True
        album, result = self._compute_suggestions(album_id)
        if self.prefetcher:
            self.prefetcher.put(album_id, album, result)
        return album, result, False

    def set_prefetch_focus(self, album_ids: list[str]) -> None:
        """Prepare albums in this order (the filtered list on screen); [] = whole list."""
        self._focus = list(dict.fromkeys(album_ids)) or None
        if self.prefetcher:
            self.prefetcher.wake()

    def _prefetch_window(self, size: int) -> list[str]:
        """The next ``size`` own albums not tagged yet, in the order of the list on screen
        (see :meth:`set_prefetch_focus`), else in album-list order."""
        me = self._me()
        applied = self.history.latest_by_album(me)
        albums = self.immich.list_albums()
        if self._focus is not None:
            by_id = {album.id: album for album in albums}
            albums = [by_id[i] for i in self._focus if i in by_id]
        window = []
        for album in albums:
            owner = album.owner
            if album.id in applied or (owner and owner.id != me) or not album.asset_count:
                continue
            window.append(album.id)
            if len(window) == size:
                break
        return window

    def _compute_suggestions(self, album_id: str) -> tuple[Album, SuggestionResult]:
        album = self.immich.get_album(album_id)
        existing: list[str] = []
        if self.llm is not None and self.config.tagging.llm.existing_tags_limit:
            try:
                existing = self.existing_tags()
            except ImmichError as exc:
                log.warning("Could not load existing tags: %s", exc)
        return album, self.suggester.suggest(album, existing)

    # ------------------------------------------------------------------ apply

    def apply(
        self,
        album_id: str,
        raw_tags: list[str],
        raw_keywords: list[str] | None = None,
        targets: Targets | None = None,
        dry_run: bool = False,
    ) -> ApplyResult:
        """Write tags/keywords to the album; every real run is recorded in the operation log."""
        targets = targets or Targets()
        details = {
            "tags": list(raw_tags),
            "keywords": list(raw_keywords or []),
            "targets": asdict(targets),
        }
        return self._logged("apply", album_id, details, dry_run, raw_tags, raw_keywords, targets)

    def remove(self, album_id: str, dry_run: bool = False) -> ApplyResult:
        """Take back everything the app wrote to the album: its tags on the photos (kept on
        photos another tagged album holds), its line in photo descriptions and in the album
        description. The album is "not tagged" afterwards."""
        details = {"targets": asdict(Targets(True, True, True))}
        return self._logged("remove", album_id, details, dry_run, [], [], None)

    def _logged(self, action, album_id, details, dry_run, raw_tags, raw_keywords, targets):
        """Run apply / remove and record the outcome in the operation log."""
        try:
            if action == "remove":
                result = self._apply(album_id, [], [], Targets(True, True, True), dry_run, True)
            else:
                result = self._apply(album_id, raw_tags, raw_keywords, targets, dry_run)
        except (ValueError, ImmichError) as exc:
            if not dry_run:
                self.oplog.add(
                    ERROR, action, album_id, self._album_name(album_id), details, str(exc)
                )
            raise
        if not dry_run:
            details.update(
                tags=result.tags,
                keywords=result.keywords,
                photos=result.own_count,
                album_photos=result.asset_count,
            )
            if result.removed_tags:
                details["removed"] = result.removed_tags
            status = ERROR if result.complete is False else SUCCESS
            entry = self.oplog.add(
                status,
                action,
                result.album_id,
                result.album_name,
                details,
                " ".join(result.warnings),
            )
            result.log_id = entry["id"]
        return result

    def _album_name(self, album_id: str) -> str | None:
        try:
            return self.immich.get_album(album_id).name
        except ImmichError:
            return None

    def _apply(
        self,
        album_id: str,
        raw_tags: list[str],
        raw_keywords: list[str] | None,
        targets: Targets,
        dry_run: bool,
        removing: bool = False,
    ) -> ApplyResult:
        """Apply (``removing``: take back) tags and description lines.

        Apply is a sync for Immich tags: tags the app put on this album earlier (history)
        that are not selected any more are taken off the album's photos, except photos
        that another album tagged with the same tag holds.
        """
        t = self.t
        if not targets.any():
            raise ValueError(t("error.no_target"))
        tags = normalize_tags(raw_tags, self.config.tagging.normalization)
        keywords = normalize_keywords(raw_keywords or [])
        if not removing:
            if (targets.tags or targets.photo_description) and not tags:
                raise ValueError(t("error.no_tags"))
            if targets.album_description and not keywords:
                raise ValueError(t("error.no_keywords"))

        me = self._me()
        state = self.history.latest_by_album(me)
        if removing and album_id not in state:
            raise ValueError(t("error.not_tagged"))
        owned = state.get(album_id, {}).get("tags", [])
        selected = {tag.casefold() for tag in tags}
        removed_tags = [tag for tag in owned if tag.casefold() not in selected]
        if not targets.tags:
            removed_tags = []  # Immich tags are only synced when they are written
        album = self.immich.get_album(album_id)
        assets = self.immich.album_assets(album_id, with_exif=targets.photo_description)
        own = [a for a in assets if a.owner_id == me]
        result = ApplyResult(
            album.id,
            album.name,
            tags,
            keywords,
            targets,
            asset_count=len(assets),
            own_count=len(own),
            external_count=sum(1 for a in own if a.library_id),
            dry_run=dry_run,
            removed_tags=removed_tags,
        )
        foreign = len(assets) - len(own)
        if foreign and (targets.tags or targets.photo_description):
            result.warnings.append(t("warning.foreign_photos", foreign=foreign, total=len(assets)))
        album_owner = album.owner
        can_edit_album = album_owner is None or album_owner.id == me
        if targets.album_description and not can_edit_album:
            result.warnings.append(t("warning.album_not_owned", owner=album_owner.email))
        if dry_run:
            return result

        tag_ids: dict[str, str] = {}
        if targets.tags and own and tags:
            tag_ids = self.immich.upsert_tags(tags)
            result.new_links = self.immich.tag_assets(list(tag_ids.values()), [a.id for a in own])
        removals: dict[str, tuple[str, set[str]]] = {}
        if removed_tags and own:
            removals = self._untag(result, {a.id for a in own}, state)
        if targets.photo_description and own:
            result.descriptions_updated = self._write_photo_descriptions(own, tags)
        if targets.album_description and can_edit_album:
            label = self.config.tagging.descriptions.album.label
            new = merge_keyword_line(album.description, keywords, label)
            if new != album.description:
                self.immich.update_album_description(album.id, new)
                result.album_description_updated = True
            elif keywords:
                result.album_description_updated = True  # already right

        if self.config.immich.verify_after_apply and own:
            self._verify(result, tag_ids, tags, removals)

        if self.prefetcher:
            self.prefetcher.forget(album.id)  # next album slides into the window
        result.history = self.history.record(
            album.id,
            album.name,
            tags if (targets.tags or targets.photo_description) else [],
            keywords if targets.album_description else [],
            asdict(targets),
            len(own),
            result.new_links,
            result.complete,
            user_id=me,
            removed=removed_tags,
            action="remove" if removing else "apply",
        )
        log.info(
            "%s album %r (%d own photos): tags=%s removed=%s keywords=%s targets=%s",
            "Removed tagging of" if removing else "Applied to",
            album.name,
            len(own),
            tags,
            removed_tags,
            keywords,
            asdict(targets),
        )
        return result

    def _untag(
        self, result: ApplyResult, own_ids: set[str], state: dict[str, dict]
    ) -> dict[str, tuple[str, set[str]]]:
        """Take ``result.removed_tags`` off the album's own photos. A photo keeps a tag when
        another album the app tagged with it holds that photo. Returns {tag: (tag id,
        protected photo ids)} for the verification."""
        tag_ids = {tag["value"].casefold(): tag["id"] for tag in self.immich.list_tags()}
        removals: dict[str, tuple[str, set[str]]] = {}
        untagged: set[str] = set()
        kept: set[str] = set()
        for name in result.removed_tags:
            tag_id = tag_ids.get(name.casefold())
            if tag_id is None:
                continue  # deleted in Immich meanwhile: nothing to take off
            carrying = {a.id for a in self.immich.album_assets(result.album_id, tag_id)}
            carrying &= own_ids
            protected: set[str] = set()
            try:
                for other_id, entry in state.items():
                    if other_id != result.album_id and name.casefold() in {
                        t.casefold() for t in entry["tags"]
                    }:
                        protected |= {a.id for a in self.immich.album_assets(other_id, tag_id)}
            except ImmichError as exc:  # cannot tell what to keep: keep the tag everywhere
                result.warnings.append(self.t("warning.untag_skipped", tag=name, error=exc))
                continue
            to_remove = sorted(carrying - protected)
            if to_remove:
                self.immich.untag_assets(tag_id, to_remove)
            untagged.update(to_remove)
            kept.update(carrying & protected)
            removals[name] = (tag_id, protected)
        result.untagged, result.kept_elsewhere = len(untagged), len(kept)
        if result.kept_elsewhere:
            result.warnings.append(self.t("warning.kept_elsewhere", count=result.kept_elsewhere))
        return removals

    def _write_photo_descriptions(self, assets: list[AssetInfo], tags: list[str]) -> int:
        prefix = self.config.tagging.descriptions.photo.tag_prefix
        groups: dict[str, list[str]] = defaultdict(list)
        for asset in assets:
            new = merge_tag_line(asset.description, tags, prefix)
            if new != asset.description:
                groups[new].append(asset.id)
        for description, ids in groups.items():  # usually one group: photos without own text
            self.immich.set_descriptions(description, ids)
        return sum(len(ids) for ids in groups.values())

    def _check(
        self,
        result: ApplyResult,
        tag_ids: dict[str, str],
        tags: list[str],
        removals: dict[str, tuple[str, set[str]]] | None = None,
    ) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        """Own photos still missing each change: {what: {asset ids}}, and own photos still
        carrying a removed tag: {tag: {asset ids}}."""
        me = self._me()
        own = {
            a.id: a
            for a in self.immich.album_assets(result.album_id, with_exif=True)
            if a.owner_id == me
        }
        self._names = {asset_id: a.file_name or asset_id for asset_id, a in own.items()}
        missing: dict[str, set[str]] = {}
        result.verified_tags = {}
        for name, tag_id in tag_ids.items():
            tagged = {a.id for a in self.immich.album_assets(result.album_id, tag_id)}
            result.verified_tags[name] = len(own.keys() & tagged)
            missing[self.t("what.tag", tag=name)] = set(own) - tagged
        if not tag_ids:
            result.verified_tags = None
        if result.targets.photo_description:
            prefix = self.config.tagging.descriptions.photo.tag_prefix
            expected = merge_tag_line("", tags, prefix)
            ok = {i for i, a in own.items() if tag_line_of(a.description, prefix) == expected}
            result.verified_descriptions = len(ok)
            missing[self.t("what.description")] = set(own) - ok
        remaining: dict[str, set[str]] = {}
        if removals:
            result.verified_removed = {}
            for name, (tag_id, protected) in removals.items():
                still = {a.id for a in self.immich.album_assets(result.album_id, tag_id)}
                still = (still & set(own)) - protected
                result.verified_removed[name] = len(still)
                remaining[name] = still
        return (
            {what: ids for what, ids in missing.items() if ids},
            {name: ids for name, ids in remaining.items() if ids},
        )

    def _verify(
        self,
        result: ApplyResult,
        tag_ids: dict[str, str],
        tags: list[str],
        removals: dict[str, tuple[str, set[str]]] | None = None,
    ) -> None:
        cfg = self.config.immich
        waited = cfg.verify_delay_seconds
        if waited:
            self._sleep(waited)
        try:
            missing, remaining = self._check(result, tag_ids, tags, removals)
            for _ in range(cfg.verify_retries):
                if not missing and not remaining:
                    break
                self._sleep(cfg.verify_retry_seconds)
                waited += cfg.verify_retry_seconds
                missing, remaining = self._check(result, tag_ids, tags, removals)
        except ImmichError as exc:
            result.warnings.append(self.t("warning.verify_failed", error=exc))
            return
        for name, ids in remaining.items():
            files = sorted(self._names.get(i, i) for i in ids)
            shown = ", ".join(files[:5])
            if len(files) > 5:
                shown += " " + self.t("warning.and_more", count=len(files) - 5)
            result.warnings.append(
                self.t(
                    "warning.not_removed",
                    tag=name,
                    count=len(ids),
                    seconds=f"{waited:g}",
                    files=shown,
                )
            )
        if not missing:
            return

        affected = set().union(*missing.values())
        names = sorted(self._names.get(i, i) for i in affected)
        shown = ", ".join(names[:5])
        if len(names) > 5:
            shown += " " + self.t("warning.and_more", count=len(names) - 5)
        what = f" {self.t('join.and')} ".join(sorted(missing))
        if len(affected) == result.own_count and result.external_count:
            result.warnings.append(
                self.t(
                    "warning.nothing_kept", total=result.own_count, what=what, seconds=f"{waited:g}"
                )
            )
        else:
            result.warnings.append(
                self.t(
                    "warning.partly_kept",
                    count=len(affected),
                    total=result.own_count,
                    what=what,
                    seconds=f"{waited:g}",
                    files=shown,
                )
            )

    # ------------------------------------------------------------------ sharing

    def list_users(self) -> list[dict[str, str]]:
        me = self._me()
        return [u for u in self.immich.list_users() if u["id"] != me]

    def album_users(self, album_id: str) -> list[dict[str, str]]:
        return [asdict(u) for u in self.immich.get_album(album_id).users]

    def _resolve_users(self, users: list[str], action: str) -> list[dict[str, str]]:
        wanted = [u.strip() for u in users if u and u.strip()]
        if not wanted:
            raise ValueError(self.t("error.no_user"))
        me = self._me()
        resolved, problems = [], []
        for value in dict.fromkeys(wanted):
            user = self.immich.find_user(value)
            if user is None:
                problems.append(self.t("error.user_not_found", user=value))
            elif user["id"] == me:
                problems.append(self.t("error.share_self", user=value))
            else:
                resolved.append(user)
        if problems:
            message = " ".join(problems)
            self.oplog.add(ERROR, action, details={"users": wanted}, message=message)
            raise ValueError(message)
        return resolved

    def share_many(
        self,
        album_ids: list[str],
        users: list[str] | str,
        role: str,
        remove: bool = False,
    ) -> dict[str, Any]:
        """Share (or with ``remove`` unshare) albums with one or more users.

        Every album and person is handled and logged separately; one failure does not stop
        the rest. Users are checked first: an unknown e-mail stops everything.
        """
        t = self.t
        action = "unshare" if remove else "share"
        if role not in SHARE_ROLES:
            raise ValueError(t("error.role", roles=", ".join(SHARE_ROLES)))
        if not album_ids:
            raise ValueError(t("error.no_albums"))
        people = self._resolve_users([users] if isinstance(users, str) else users, action)
        me = self._me()
        results = []
        for album_id in dict.fromkeys(album_ids):
            try:
                album = self.immich.get_album(album_id)
            except ImmichError as exc:
                for user in people:
                    results.append(
                        self._share_result(album_id, None, user, role, action, "error", str(exc))
                    )
                continue
            for user in people:
                present = any(u.id == user["id"] for u in album.users)
                try:
                    if album.owner and album.owner.id != me:
                        status, detail = "skipped", t("share.not_owner", owner=album.owner.email)
                    elif remove and not present:
                        status, detail = "unchanged", t("share.had_no_access")
                    elif not remove and present:
                        status, detail = "unchanged", t("share.already_shared")
                    elif remove:
                        self.immich.unshare_album(album_id, user["id"])
                        status, detail = "removed", ""
                    else:
                        self.immich.share_album(album_id, user["id"], role)
                        status, detail = "shared", ""
                        album = self.immich.get_album(album_id)  # users changed
                except ImmichError as exc:
                    status, detail = "error", str(exc)
                results.append(
                    self._share_result(album_id, album.name, user, role, action, status, detail)
                )
        counts: dict[str, int] = {}
        for entry in results:
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        log.info("%s for %s: %s", action, [u["email"] for u in people], counts)
        return {
            "users": people,
            "role": role,
            "remove": remove,
            "counts": counts,
            "results": results,
        }

    def _share_result(self, album_id, album_name, user, role, action, status, detail) -> dict:
        ok = status in ("shared", "removed", "unchanged")
        details = {"user": user["email"], "role": role, "result": status}
        entry = self.oplog.add(
            SUCCESS if ok else ERROR, action, album_id, album_name, details, detail
        )
        return {
            "album_id": album_id,
            "album_name": album_name,
            "user": user["email"],
            "status": status,
            "detail": detail,
            "log_id": entry["id"],
        }

    def unshare(self, album_id: str, user_id: str) -> list[dict[str, str]]:
        album = self.immich.get_album(album_id)
        user = next((u for u in album.users if u.id == user_id), None)
        email = user.email if user else user_id
        try:
            self.immich.unshare_album(album_id, user_id)
        except ImmichError as exc:
            self.oplog.add(ERROR, "unshare", album_id, album.name, {"user": email}, str(exc))
            raise
        self.oplog.add(
            SUCCESS, "unshare", album_id, album.name, {"user": email, "result": "removed"}
        )
        return self.album_users(album_id)
