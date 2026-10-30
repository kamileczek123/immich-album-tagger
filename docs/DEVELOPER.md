# Developer guide

How the code is organised, how data flows, and how to change it safely. The *why* is in
[PROJECT.md](../PROJECT.md).

## Setup

```bash
make install        # .venv with the package in editable mode + pytest + ruff
make test           # full test suite, no network access
make lint           # ruff check + format check
make dev            # UI on :8080 against a fake Immich (:2283) and fake LLM (:8081)
```

`make dev` needs no `.env`; it seeds six sample albums in memory. Use it for all UI work.

## Architecture

```
 Browser (static/index.html + app.js)
    │  JSON over HTTP, optional login (session cookie)
    ▼
 app.py  ── FastAPI routes, error mapping, thumbnail proxy
    │
    ▼
 service.py  TaggerService ── orchestrates one use case per method
    ├── immich.py   ImmichClient  ── REST calls to Immich (httpx)
    ├── suggest.py  Suggester     ── generators, keyword base forms, normalisation
    │      └── llm.py LLMPool    ── OpenAI-compatible chat completions, several endpoints
    ├── prefetch.py Prefetcher    ── background workers keeping suggestions ready
    ├── oplog.py    OperationLog  ── operations.log (JSON lines) behind the Logs page
    ├── i18n.py     Translator    ── .lan files: parse, install into LANGUAGE_DIR, translate
    ├── descriptions.py           ── the app-owned lines in photo/album descriptions
    └── history.py  History       ── SQLite record of what was applied
 config.py ── loads/validates immich.yaml, llm.yaml, tagging.yaml, app.yaml
 auth.py   ── credentials.yaml (hashed on first start), signed session cookies,
              KeySessions: per-login Immich API keys (auth.mode immich_keys)
 __main__.py ── CLI (serve, check, suggest-all, apply) on top of TaggerService
```

Rules of thumb:

- `app.py` and `__main__.py` are thin. Logic goes into `TaggerService` so UI and CLI
  behave the same.
- Only `immich.py` knows Immich URLs and payload shapes. Only `llm.py` knows the chat
  completions format.
- Everything in `suggest.py` is pure (no I/O except the injected LLM client) and easy to
  test.
- Handlers are synchronous; FastAPI runs them in a thread pool. httpx `Client` and the
  SQLite wrapper (one connection per call, plus a lock for writes) are thread-safe.

## Module map

| Module | Main types | Notes |
| --- | --- | --- |
| `config.py` | `AppConfig`, `ImmichConfig`, `LLMConfig`, `TaggingConfig`, `load_config`, `ConfigError` | Pydantic v2 models. `${ENV}` expansion before validation. Rule templates are checked against regex groups at load time. |
| `immich.py` | `ImmichClient`, `Album`, `AlbumUser`, `AssetInfo`, `ImmichError` | `ImmichError.status` carries the HTTP status. Search mode auto-detection lives in `album_assets`. `me()` is cached. |
| `i18n.py` | `Translator`, `parse_lan`, `sync_language_files` | One translator per app (language from `app.yaml`), used by the service for server messages and served to the browser via `/api/i18n`. |
| `auth.py` | `Credentials`, `Sessions`, `load_credentials`, `ImmichLogin`, `KeySessions`, `KeySession` | scrypt hashes from the stdlib; HMAC-signed session cookie `iat_session`; key in `DATA_DIR/session.key`. `KeySessions` (mode `immich_keys`): in-memory registry `session id -> {user, key id, secret, expiry, client, service}` with a lock; `open` / `get` / `close` / `sweep` / `close_all`, a sweep thread; `on_open` / `on_close` callbacks let the app attach a service per session. |
| `oplog.py` | `OperationLog` | Append-only JSON lines with a lock; `entries()` newest first; written by `TaggerService.apply/share_many/unshare`, so the CLI logs too. |
| `static/common.js` | `t`, `plural`, `api`, `toast`, `icon`, `initChrome` | Shared by all pages: translations (`data-i18n*` attributes), API calls with login redirect, notifications, the header / sidebar (theme button, account menu). |
| `immich_theme.py` | `ImmichTheme`, `extract`, `render` | Reads Immich's index page and stylesheets, keeps the `--immich*` variables (plus the ones they refer to, per light / `.dark` scope; first value wins over the `@supports` lab() repeat) and GoogleSans `@font-face`, renders `immich-theme.css` with font URLs rewritten to the app. Cached with a TTL, last good copy kept on errors; `font(name)` serves only fonts named in the stylesheet. |
| `static/theme.js` | `window.iatTheme` | Classic script in `<head>`: light / dark from Immich's `localStorage` key `immich-ui-theme`, so there is no flash and the app matches Immich on the same domain. |
| `prefetch.py` | `Prefetcher`, `Prepared` | `workers` daemon threads. `run_once()` prepares at most one album from the window, never one another worker is on (testable without threads); `forget()` after apply wakes them. Window and computation are callbacks from `TaggerService`. Workers set `llm.background`, so their requests give way to interactive ones. |
| `descriptions.py` | `merge_tag_line`, `merge_keyword_line`, `normalize_keywords` | Pure functions. A photo line is "app-owned" if every word starts with the prefix; an album line if it starts with the label. |
| `llm.py` | `LLMClient`, `LLMPool`, `background`, `parse_variants`, `extract_json`, `LLMError` | `LLMClient` = one endpoint. `LLMPool` = the main endpoint + `llm.endpoints` (`LLMConfig.all_endpoints()`), same `complete()` interface: per-endpoint slot limits (`max_concurrent`) under one `Condition`, round robin, failover with a cooldown, interactive before background (`background` context variable). `Suggester.suggest()` sends its two requests from two threads (`contextvars.copy_context()` keeps the flag). Lenient JSON extraction (think blocks, fences, prose). |
| `suggest.py` | `Suggester`, `Variant`, `SuggestionResult`, `normalize_tag(s)` | One method per generator. `keyword_suggestions()` makes a separate small LLM call for base forms. LLM errors become warnings. Identical variants are merged. |
| `service.py` | `TaggerService`, `Targets`, `ApplyResult` | `apply()` see below; `share()` / `unshare()`; `sleep` is injectable so tests skip the verify delay. `for_immich(client)` makes a per-login copy: own `ImmichClient` and prefetcher, shared config, suggester, LLM, history, log and a `compute_lock` (one background LLM job at a time across all copies). |
| `history.py` | `History` | Append-only table `applications` (tags, keywords, targets, `user_id`); columns added in place on old databases. `latest_by_album(user_id)` returns that user's entries plus unowned ones; `claim_unowned(user_id)` gives pre-0.6.0 entries to the first `immich_keys` login. |
| `app.py` | `create_app(service_factory, username, password)` | Factory injection makes the app testable without network. |
| `static/` | — | Vanilla JS, no build. DOM built with `createElement`/`textContent` (no `innerHTML` with data). |

## Immich API usage

All paths are relative to `<IMMICH_URL>/api`, header `x-api-key`.

| Call | Endpoint | Notes |
| --- | --- | --- |
| check, owner | `GET /server/version`, `GET /users/me` | the key's user id decides which photos can be changed |
| users | `GET /users` | for sharing by e-mail |
| albums | `GET /albums` | Owned + shared; de-duplicated by id. `AlbumResponseDto` no longer embeds assets in 3.x. |
| album | `GET /albums/{id}` | Metadata and `albumUsers` (first entry is the owner) |
| album description | `PATCH /albums/{id}` `{"description"}` | owner only |
| share / unshare | `PUT /albums/{id}/users` `{"albumUsers": [{"userId", "role"}]}`, `DELETE /albums/{id}/user/{userId}` | roles `viewer`, `editor` |
| album assets | `POST /search/metadata` | **≥ 3.2:** `{"filter": {"albumIds": {"any": [id]}, "visibility": {"in": [...]}, "tagIds": {"any": [tag]}}, "size": N, "cursor": c}` -> `assets.items`, `assets.nextCursor`. **< 3.2:** `{"albumIds": [id], "tagIds": [tag], "page": p, "size": N}` -> `assets.nextPage`. In `auto` mode a 400/422 on the first form switches the client to the second for the rest of the process. |
| tags | `GET /tags` | `value` is the full path, `name` the leaf |
| upsert | `PUT /tags` `{"tags": ["a/b"]}` | Creates parents; mapping back to ids by `value` (case-insensitive), with a `GET /tags` fallback |
| tag assets | `PUT /tags/assets` `{"tagIds": [...], "assetIds": [...]}` | Returns `{"count"}` of new links; batched by `tag_batch_size`. Server keeps only assets the user owns (`AssetUpdate` -> `checkOwnerAccess`), silently. |
| untag assets | `DELETE /tags/{id}/assets` `{"ids": [...]}` | Sync and remove; batched by `tag_batch_size`; permission `tag.asset` |
| photo descriptions | `PUT /assets` `{"ids": [...], "description"}` | bulk; one call per distinct resulting description |
| thumbnail | `GET /assets/{id}/thumbnail?size=thumbnail` | Proxied by `/api/thumbnails/{id}` |
| login | `POST /auth/login`, `POST /auth/logout` | public; `login()` returns `{token, id, email, name}`, `verify_login()` (mode `immich`) logs out at once |
| session keys | `GET /api-keys`, `POST /api-keys` `{name, permissions}`, `DELETE /api-keys/{id}` | mode `immich_keys`. Listed and created with the login session token (`Authorization: Bearer`; Immich checks no key permissions for sessions); deleted by the key itself (needs `apiKey.delete`; Immich only checks the owner). The secret is returned only by `POST`. |

`ImmichClient(config, transport, api_key=None)` acts with `api_key` (default: the one from
`immich.yaml`); `with_key(secret)` makes a client with the same settings for another key.

The OpenAPI spec is at `open-api/immich-openapi-specs.json` in the Immich repository.
When Immich changes, check these endpoints there first.

## App HTTP API

| Method | Path | Body / result |
| --- | --- | --- |
| GET | `/api/health` | `{"status","version"}`, no auth |
| GET | `/immich-theme.css`, `/immich-theme/fonts/{name}` | public: Immich's design tokens (empty without them) and its font files |
| GET | `/api/session` | `{version, auth: {enabled, mode, user}}`; cheap, for the header |
| GET | `/api/status` | Immich version/user or error, LLM model |
| GET | `/api/albums` | albums + `applied` (`{tags, applied_at, asset_count, verified}` or null) |
| GET | `/api/tags` | existing tag values |
| POST | `/api/albums/{id}/suggestions[?refresh=true]` | `{album, prepared, variants: [{source,label,tags}], keywords: [{word, base}], related, warnings}`; without `refresh` a prepared result is returned if there is one |
| POST | `/api/albums/{id}/remove` | body `{dry_run?}` -> `ApplyResult` (`removed_tags`, `untagged`, `kept_elsewhere`, `verified_removed`, …); 400 if the app has not tagged the album |
| POST | `/api/albums/{id}/apply` | body `{tags, keywords, targets: {tags, photo_description, album_description}, dry_run?}` -> `ApplyResult` (`own_count`, `asset_count`, `verified_tags`, `verified_descriptions`, `complete`, `warnings`, `history`, ...) |
| GET | `/api/i18n` | public: `{lang, lang_code, messages, settings: {version, ui, sharing}}` |
| POST | `/api/login`, `/api/logout` | public: `{username, password}` -> session cookie. Mode `immich_keys`: login creates the person's key (502 if that fails), logout deletes it; a valid cookie without a session in memory gets 401 `{"detail", "lost": true}` (pages redirect to `/login?…&lost=1`) |
| GET / DELETE | `/api/log` | operation log entries (newest first) / clear |
| GET | `/api/users` | Immich users except yourself |
| POST | `/api/prefetch/focus` | `{album_ids}` in the order shown (filtered list), `[]` = whole list -> `{focus}`; the prefetch window follows it (`TaggerService.set_prefetch_focus`) |
| GET | `/api/prefetch` | `{enabled, window, ready, preparing, ready_ids}` |
| POST | `/api/sharing/bulk` | `{album_ids, user, role, remove?}` -> `{user, counts, results: [{album_id, album_name, status, detail}]}`; status `shared`, `removed`, `unchanged`, `skipped` (not your album), `error` |
| GET / POST | `/api/albums/{id}/users` | who has access / share `{user: email-or-id, role}` |
| DELETE | `/api/albums/{id}/users/{user_id}` | remove access |
| GET | `/api/thumbnails/{asset_id}` | image bytes |
| GET | `/docs` | FastAPI's OpenAPI UI |

Errors: `ImmichError` -> 502 (404 if Immich said 404), `ValueError` -> 400, request
validation -> 422. All as `{"detail": ...}`.

This API is what a future userscript front-end would call (roadmap item 4).

## URLs and look

All links, API calls and redirects are **relative** (`api/albums`, `static/app.js`,
`login?next=logs`), so the app works under any path prefix of a reverse proxy
(`/IAT/`) without knowing it. Never write an absolute path (`"/api/..."`) in the pages or
scripts; `test_urls_are_relative_for_a_proxy_path_prefix` checks this.

The look follows Immich's web UI: neutral grays, pill buttons, `immich-form-input`-like
fields, album cards, a sidebar with rounded active items. `static/style.css` defines its
own variables (`--primary`, `--bg`, `--gray-100`, …) from Immich's tokens with built-in
fallbacks, e.g. `--primary: var(--immich-ui-primary-500, #4250af)`; `:root.dark` maps the
dark values. Immich's tokens come from `immich-theme.css` (see `immich_theme.py`), so color
and font changes in Immich apply without touching the app; only the layout is ours. Use
the app's variables in new CSS, never raw colors.

## Translations

Every user-visible text has a key. Static HTML uses `data-i18n="key"` (text),
`data-i18n-placeholder`, `data-i18n-title`, `data-i18n-aria`; scripts call `t("key",
{param})` or `plural(n, "count.photos")` (keys `count.photos.one/few/many/other`, chosen by
`Intl.PluralRules` with the file's `@code`). Server messages use `self.t("key", param=...)`.

When you add a key, add it to **every** file in `src/immich_album_tagger/languages/`. A test checks
that all files have the English keys and that `@version` equals the app version — bump
`@version` in all `.lan` files together with `pyproject.toml`, otherwise installed copies are
not refreshed.

## Data flow of "Apply"

1. Validate: at least one target; tags needed for tags/photo descriptions, keywords for the
   album description. `normalize_tags()` / `normalize_keywords()` clean the input.
2. `me()`, `get_album()`, `album_assets()` (with EXIF when photo descriptions are on).
   Split assets into own and foreign; foreign ones are reported, never sent.
3. Tags: `upsert_tags()` -> `tag_assets(own)`. Sync: tags in the album's history state
   (`History.latest_by_album`) but not selected are `removed_tags`; `_untag()` takes each
   off the own photos carrying it, minus photos of other albums whose state has the tag.
   `remove()` is the same run with nothing selected and all targets (`removing=True`).
4. Photo descriptions: `merge_tag_line()` per asset, grouped by resulting text ->
   `set_descriptions()` per group (usually one or two calls).
5. Album description: only if the key's user owns the album; `merge_keyword_line()` ->
   `PATCH /albums/{id}`.
6. Verify after `verify_delay_seconds`: count own assets carrying each tag and own assets
   whose tag line matches, and own unprotected assets still carrying a removed tag.
   Shortfalls become warnings; if the photos are in an external library, the read-only
   cause is named.
7. `History.record()` with `removed` and `action` (`apply` / `remove`). The state of an
   album is every tag applied minus the removed ones; a `remove` run clears it.

Everything is idempotent: links already present are ignored by Immich; description lines
are replaced, not appended.

## Extending

### Add a suggestion generator
1. Add a config model in `config.py` (with `enabled` and `label`) and a field on
   `TaggingConfig`; document it in `config/tagging.yaml` and the admin guide.
2. Add a method `xxx_variant(self, album) -> Variant | None` to `Suggester` and include
   it in the tuple in `suggest()`. Always pass tags through `normalize_tags`.
3. Add a display name for the new `source` in `SOURCE_NAMES` in `static/app.js`.
4. Add tests in `tests/test_suggest.py`.

Generators needing Immich data (e.g. EXIF cities of sample photos) should receive it via
`TaggerService.suggest()`, not call Immich themselves — keep `suggest.py` I/O-free.

Rendering of a new target in descriptions belongs in `descriptions.py` (pure, tested in
`tests/test_descriptions.py`).

### Add a setting

Add the field to the model in `config.py`, then add it with a comment and its default to the
matching `config/*.yaml.example` — `tests/test_config_examples.py` fails if an example lacks a
setting or shows a different default. Document it in the admin guide tables.

### Add an Immich call
Add a method to `ImmichClient` using `_request()` (it maps transport and HTTP errors to
`ImmichError`). Extend `tests/fakes.py::FakeImmich.handler` with the endpoint and add a
test in `tests/test_immich.py`.

### Change the UI
Edit `static/*` and reload; no build. Keep data out of `innerHTML`. Colours are CSS
variables at the top of `style.css`, with a dark-mode block. Check with `make dev` in
light and dark mode and at mobile width.

## Testing

- `tests/fakes.py` — `FakeImmich`, an in-memory Immich covering the endpoints above, in
  `filter` or `legacy` search mode. `drop_links` simulates skipped links, `read_only`
  simulates a read-only external library (changes reverted before the next read), `foreign`
  seeds photos owned by another user. Like Immich, it ignores tagging of foreign photos.
- `tests/conftest.py` — fixtures; loads the real `config/tagging.yaml` so shipped config
  is tested too.
- Tests use `httpx.MockTransport` and FastAPI's `TestClient`; nothing touches the network.
- `scripts/dev_server.py` serves the same fake over HTTP plus a fake LLM for manual tests.

Before a release, also run `immich-album-tagger check` and tag one throwaway album against a
real Immich.

## Style

- Python 3.11+, type hints, `from __future__ import annotations`.
- `ruff format` and `ruff check` (config in `pyproject.toml`), line length 100.
- English for code, comments and docs. Polish only in default tag vocabulary and examples.

## Versioning and releasing

The app version lives in `version` in `pyproject.toml`; the app reads it at runtime
(`importlib.metadata`), so the UI, `/api/health` and `immich-album-tagger --version` always
show the installed version. The translation files carry the same number in `@version`, which
decides when installed copies in `LANGUAGE_DIR` are replaced. Tests check both.

Releases are plain numeric git tags `X.Y.Z` - **no `v` prefix**. The source of truth is
the maintainer's Gitea; the GitHub repository is a push mirror of it. Each side has its own
workflow and publishes to its own registry:

| Workflow | Runs on | Publishes to |
| --- | --- | --- |
| `.gitea/workflows/docker.yml` | Gitea Actions (Gitea ignores `.github/` when `.gitea/workflows` exists) | the Gitea container registry, tags `X.Y.Z`, `latest`, commit SHA |
| `.github/workflows/dockerhub.yml` | GitHub Actions (GitHub ignores `.gitea/`) | Docker Hub `kamileczek123/immich-album-tagger`, `linux/amd64` + `linux/arm64`, tags `X.Y.Z`, `X.Y`, `latest`; also updates the Docker Hub description from `README.md` |

Both behave the same way:

| Event | `test` job | `build` job |
| --- | --- | --- |
| push to `master` | runs `pytest` | skipped |
| push of a tag | runs `pytest` | runs if tests pass |

The `build` job:

1. rejects tags that are not `^[0-9]+.[0-9]+.[0-9]+$` (e.g. `v0.2.1`, `0.2`, `0.2.1-rc1`);
2. rejects tags that differ from `version` in `pyproject.toml`;
3. builds the image and pushes it to its registry (see the table above).

The GitHub workflow also runs `ruff` and the tests on Python 3.11 and 3.12.

Release steps (example for 0.4.1):

```bash
# 1. bump the version everywhere it is declared, and describe the changes
sed -i '' 's/^version = ".*"/version = "0.4.1"/' pyproject.toml           # GNU sed: drop the ''
sed -i '' 's/^@version = .*/@version = 0.4.1/' src/immich_album_tagger/languages/*.lan
$EDITOR CHANGELOG.md
make test                    # fails if a .lan @version or key is out of step
git commit -am "Release 0.4.1"
git push

# 2. tag exactly the same version, without "v"
git tag 0.4.1
git push origin 0.4.1
```

Secrets: Gitea needs `IMMICH_ALBUM_TAGGER_TOKEN` (a Gitea token with package write
access); GitHub needs `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (a Docker Hub personal
access token with *Read & Write*), and optionally `DOCKERHUB_README_TOKEN` (*Read, Write,
Delete*) to update the Docker Hub description from `README.md`; without it that step is
skipped with a warning. Deploy by changing the image tag in the compose file; What's Up
Docker can watch for new `X.Y.Z` tags (`wud.tag.include=^\d+\.\d+\.\d+$`).

The image is built in two stages (wheels, then a slim runtime), runs as UID 10001, ships
the default `config/`, stores data in `/data`, and its entrypoint is `immich-album-tagger`.
