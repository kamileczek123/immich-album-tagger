# Changelog

## 0.9.2

### Fixed
- The app starts without any LLM settings. The shipped `llm.yaml` now has
  `enabled: ${LLM_ENABLED:-auto}`: the LLM is on when `LLM_BASE_URL` and `LLM_MODEL` are set,
  otherwise off, and suggestions come from the regex rules and the album title (the log says
  so). Before, a missing `LLM_BASE_URL` stopped the app unless `LLM_ENABLED=false` was set.
  `LLM_ENABLED=true` without an endpoint is still an error.

### Changed
- GitHub workflow: the Docker Hub description is updated with its own optional token
  `DOCKERHUB_README_TOKEN` (*Read, Write, Delete*); a failure there no longer fails the
  release.

## 0.9.1

First public release (GitHub mirror, Docker Hub image). No change in behaviour.

### Added
- MIT license.
- Docker Hub image `kamileczek123/immich-album-tagger` for `linux/amd64` and `linux/arm64`
  (tags `X.Y.Z`, `X.Y`, `latest`), built by the GitHub workflow
  `.github/workflows/dockerhub.yml` from the mirror of the Gitea repository.
- Trademark note in the README and the UI: not affiliated with or endorsed by Immich or
  FUTO; Immich is a trademark of FUTO.

### Changed
- The Python package is `immich_album_tagger` (was `album_tagger`); the command
  `immich-album-tagger` and the image entrypoint are unchanged.
- `docker-compose.yml` is shipped as `docker-compose.yml.example` (copy it; your copy is
  ignored by git) and uses the Docker Hub image; building from source is a commented
  alternative.
- Examples and the test data use neutral names (`alex@example.com`).

## 0.9.0

### Added
- **Change and remove tagging.** With *Tags on photos*, Apply now syncs: tags the app put on
  the album earlier and taken out of the selection come off its photos. The button and a
  red note say so beforehand (*Apply: 3 tags, remove 1*). Photos that another album tagged
  with the same tag keep it; tags added by hand in Immich are never touched.
- **Remove tagging** button in the panel (with confirmation), `POST /api/albums/{id}/remove`
  and CLI `remove --match/--album-id/--all [--dry-run]`: takes back the app's tags, its
  `#tag` line in photo descriptions and the keyword line of the album; the album is
  untagged again. Verified and logged like Apply.
- `preselect` in `tagging.yaml`: which variants fill *Tags to apply* when an untagged album
  opens (`first`, `album-name`, `rules`, `keywords`, `keywords-base`, `llm`, `llm-all`).

### Changed
- Notifications appear in the top left corner, no longer over the tagging panel.
- An album's *Tagged* state shows its current tags (applied minus removed), no longer every
  tag ever applied; the keywords shown are the latest ones.

## 0.8.0

### Added
- **Preparation follows the list on screen.** Type `2024` in the search field (or pick a
  filter) and the first untagged albums of that filtered list are prepared next, in its
  order; clear it and the whole list is used again. Albums prepared before stay ready
  (`POST /api/prefetch/focus`).
- **Faster suggestions.** The two LLM requests of an album (tag variants, base forms) are
  sent together, and background preparation works on several albums at once
  (`prefetch.workers`, default: as many as the LLM takes). New in `llm.yaml`:
  `max_concurrent` (requests at once per endpoint, e.g. llama-server `--parallel`) and
  `endpoints`, more models / endpoints used side by side, round robin with failover (each
  entry needs `model`; the rest is inherited, so for a gateway the model name is enough).
  What the user opens goes before background work. `check` tests every endpoint;
  `/api/status` shows them. In a test with a 1-second model: 8 albums ready in 4 s instead
  of 16 s (2 slots, 2 models).
- A click on an album cover opens the album in Immich in a new tab. Immich's address comes
  from the new `ui.immich_public_url`, or automatically from the URL when the app runs
  under a path of Immich's host name (`/IAT/`). Without it the cover opens the tagging
  panel, as before.

## 0.7.0

### Changed
- **Looks like Immich**: navbar with search, sidebar (Albums, Logs, Open Immich), albums
  as cards with square covers, tags as Immich-style chips, pill buttons, Immich's colors.
  Selecting albums for bulk sharing works like in Immich (click the covers).
- **Colors and font from the running Immich** (`ui.immich_theme`, default on): the
  server reads Immich's design tokens (`--immich-ui-*` CSS variables, GoogleSans) from its
  web UI every hour and serves them as `immich-theme.css`, so the app follows Immich's look
  after upgrades. Built-in Immich-like colors when Immich cannot be read.
  New settings `ui.immich_theme`, `ui.immich_theme_url`, `ui.immich_theme_refresh_minutes`.
- **Light / dark mode** with a toggle; on Immich's domain it shares Immich's setting
  (`localStorage` `immich-ui-theme`).
- The page title and the login page show **Immich Album Tagger**.
- **Runs under a path prefix**, e.g. `https://immich.example.com/IAT/` behind Immich's
  nginx, as well as on its own host name: all links, API calls and redirects are relative.
  New admin guide section 3a with both nginx layouts and the recommended
  `auth.secure_cookie: true` behind HTTPS; complete example in
  `config/nginx-reverse-proxy.conf.example`.

### Added
- `GET /api/session` (version and logged-in user, without calling Immich).
- `GET /immich-theme.css`, `GET /immich-theme/fonts/{name}` (public).

## 0.6.0

### Added
- **Everyone with their own Immich account** (`app.yaml` -> `auth.mode: immich_keys`). At
  login the app creates an API key for that person (`auth.key_permissions` plus
  `apiKey.delete`), keeps it in memory only and deletes it at logout, when the session
  expires (`auth.key_sweep_minutes`) or, after a restart, at that person's next login. Each
  person sees their own albums and tags, describes and shares their own photos. With an
  empty `auth.allowed_users`, every Immich account may log in.
- Prepared suggestions run per logged-in person; at most one background LLM request at a
  time across everyone.
- *Tagged* markers are per person (history records the Immich user). Entries from before
  0.6.0 are given to the first person who logs in with `immich_keys`.
- Logs page: *Person* filter (shown when the log has entries of several people).
- Start-up warning for every permission missing in `auth.key_permissions`, naming the
  feature that will not work.

### Changed
- `IMMICH_API_KEY` is optional with `auth.mode: immich_keys`; the CLI still needs it and
  says so.
- The login page explains a lost session (*The session has ended…*) instead of only asking
  to log in.

### Notes
- Modes `auto`, `immich`, `credentials` and `none` work as in 0.5.0.
- Keys left in Immich by people who never log in again can be deleted by hand under
  *Account settings -> API Keys*; they are named `immich-album-tagger session <expiry> <id>`.

## 0.5.0

### Added
- **Log in with Immich accounts** (`app.yaml` -> `auth.mode: immich`). The login page asks for
  the e-mail and password of an Immich account; Immich checks them (`POST /auth/login`) and the
  session it opens is closed again at once. Who may log in: `auth.allowed_users` (e-mails or
  user ids), by default only the owner of the API key. No `credentials.yaml` is needed and the
  config directory may stay read-only.
- `auth.mode` also makes the other choices explicit: `auto` (previous behaviour), `credentials`
  (refuses to start without `credentials.yaml`), `none`.
- The header shows who is logged in; every operation-log entry records it (*By*).

### Notes
- Everyone who logs in still works with the API key, i.e. on the key owner's photos and
  albums. Per-person access (each user with their own Immich account) is on the roadmap.
- Sessions from 0.4.x are no longer valid: log in once again after the upgrade.

## 0.4.1

### Changed
- `credentials.yaml` may use environment variables (`${UI_PASS_HASH}`), so hashes made once
  can be kept in a secret store and deployed with `encrypted: true` on every deploy.
- Hashes are written as `scrypt:<salt>:<digest>` (no `$`, safe in `.env` files and Docker
  Compose). Hashes from 0.4.0 (`scrypt$…`) keep working.

## 0.4.0

### Added
- **`app.yaml`**, a new optional config file for the app itself: language, UI behaviour,
  sharing defaults, login session.
- **Fast mode** (`ui.async_actions: true`, default). *Apply* and *Share* close the panel at
  once; the result appears as a notification in the top right corner for `ui.toast_ms`
  milliseconds: green and small on success, red and bold with a *Details* link to the
  operation log on errors. Several results (bulk sharing) stack and leave in order (FIFO).
  `false` keeps the previous behaviour (wait in the panel).
- **Default people to share with** (`sharing.default_users`, `default_role`): filled in the
  Share panel and the bulk bar. Several e-mails can be entered, separated by commas.
- **Operation log**: every Apply, Share and Remove access (per album and person) is written
  to `LOG_DIR/operations.log` (JSON lines, created when missing, kept across restarts).
  New page **Logs** with SUCCESS/ERROR entries, filters, a link target per entry and *Clear*.
- **Login page** with `credentials.yaml` in the config directory. Plain values are replaced by
  one-way scrypt hashes on the first start (`encrypted: true`); sessions are signed cookies.
  Log out in the header.
- **Commented examples of every config file** in `config/` (`*.yaml.example`): every
  setting with its purpose and default value. A test keeps them in step with the code.
- **Translations**: every text of the UI, the operation log and the server messages comes from
  `<language>.lan` files (`app.yaml` -> `language`). English and Polish are shipped; they are
  installed into `LANGUAGE_DIR` and replaced (with a backup) when the app version changes.
  Plural forms follow the language (1 zdjęcie, 2 zdjęcia, 5 zdjęć).

### Changed
- `POST /api/albums/{id}/users` and `/api/sharing/bulk` take `users: [...]` (a single `user`
  is still accepted) and return per-person results with log ids.
- HTTP basic auth was replaced by the login page; `APP_USERNAME`/`APP_PASSWORD` still work as
  plain credentials when there is no `credentials.yaml`.
- Variant titles in the panel are translated (the `label` keys in `tagging.yaml` are only used
  by the CLI suggestions file).

### Migration
- New directories: `LANGUAGE_DIR` (`/language` in Docker) and `LOG_DIR` (`/logs`). Mount them
  to keep edited translations and the log across container upgrades.
- The config directory must be writable if you use `credentials.yaml` (it is hashed in place).

## 0.3.0

### Added
- **Background preparation of suggestions** (`tagging.yaml` -> `prefetch`). A single worker
  keeps suggestions ready for the next 10 untagged albums you own, in list order. Opening
  such an album is instant; applying it lets the next one slide in. Prepared albums carry a
  *ready* label; the header shows *Suggestions ready: 7/10*. *Suggest again* bypasses it.
  Results that came without the LLM (e.g. it was down) are retried after 5 minutes.
- **Bulk sharing.** *Select albums* turns on checkboxes; *Select all shown* works with the
  title filter. Share the selection with one Immich user as viewer or editor, or remove their
  access, in one step. Albums owned by others are skipped; each album is reported.
- CLI `share --user EMAIL [--role] [--match REGEX] [--album-id ID] [--remove] [--dry-run]`.
- API `GET /api/prefetch`, `POST /api/sharing/bulk`; `?refresh=true` on suggestions.
- The app version is shown in the top right corner.

## 0.2.4

### Changed
- The result of *Apply* is shown under the button at full panel width instead of a narrow
  column next to it.

## 0.2.3

### Fixed
- Large albums could report e.g. *"The tag line is in 110 of 111 photo descriptions … read-only
  mount"* although everything was written: Immich's job queue had not finished within the
  verification delay. Verification now checks again (`verify_retries`, default 3,
  `verify_retry_seconds`, default 5) before warning.
- The warning names the files that did not keep the change. The read-only library
  explanation is shown only when no photo kept it.

## 0.2.2

### Fixed
- After upgrading, browsers could keep the previous version's `app.js` together with the new
  page: the status stayed at "Connecting to Immich…", albums appeared only after *Reload
  albums*, and *Apply* and the close button did nothing. The page now loads its assets with
  `?v=<version>` and all UI files are sent with `Cache-Control: no-cache`.

### Changed
- **Album keywords** are one editable line of words separated by spaces, filled
  automatically from the chosen tags and the base forms of the title words. Editing it stops
  the automatic filling; *Fill from tags* restores it. Suggestions (title words, base forms,
  related words) toggle words in the line.
- **Write to** lists the three targets one below the other, each with a short explanation.

## 0.2.1

### Changed
- The command, Docker entrypoint, compose service, container and data volume are named
  **`immich-album-tagger`** (was `album-tagger`). Update scripts calling the old command; a
  compose file you wrote yourself keeps working, the image entrypoint changes by itself.
- Version has a single source, `pyproject.toml`; the app reads it at runtime.
- CI is the Gitea workflow `.gitea/workflows/docker.yml`: numeric tags `X.Y.Z` only, and the
  tag must match `pyproject.toml`; images go to the Gitea container registry.
  The GitHub workflow was removed.
- Admin guide: full API key permission list mapped to Immich endpoints, permissions that are
  not needed (`album.share`, `albumUser.update`, `tag.update`) and minimal keys per use.

## 0.2.0

### Added
- **Write targets.** Per album choose any combination of: Immich tags on photos, a tag line
  in photo descriptions, a keyword line in the album description. Defaults in
  `tagging.yaml` -> `apply`; the UI remembers the last choice per browser.
- **Album keywords.** Title words with their base form from the LLM ("Zielonej" -> "zielona"),
  both selectable, plus related words ("nowy rok") and free input. Written to the album
  description so the Immich *Albums* filter finds the album, also for people it is shared with.
- **Tag line in photo descriptions** (`#czas/rok/2015 #miejsce/polska`), searchable in
  Immich with search type *Description* by everyone who can see the photo.
- **Share album** button: list who has access, add an Immich user by e-mail as viewer or
  editor, remove access.
- **Base-forms tag variant** ("Title keywords (base forms)").
- CLI `apply` flags `--tags/--photo-description/--album-description`; suggestions files
  carry `selected_keywords`, `keywords` and `related`.
- `check` shows which user the API key belongs to.

### Changed
- Only photos owned by the API key's user are changed (Immich ignores the rest); the result
  reports how many photos belong to other users.
- Verification waits `immich.verify_delay_seconds` (default 5 s) so changes that Immich
  reverts in background jobs are detected. A read-only external library is named as the
  likely cause.
- Messages count photos actually changed, not photos in the album.
- `#` is removed during tag normalisation (album titles use it as a separator).
- `llm.max_tokens` default 1024 -> 2048 (three variants were being cut off).

### Fixed (configuration)
- A file wrapped in its own section name (`immich:` in `immich.yaml`) is accepted.
- Unknown keys are an error instead of being silently ignored.
- Only UPPER_CASE `${NAME}` is environment interpolation, so `${year}` in tag templates works.

### Migration
- The history database gets two columns automatically. No action needed.
- New API key permissions are needed for the new features; see the admin guide.

## 0.1.0
- First version: suggestions (album name, rules, keywords, LLM), web UI, apply tags with
  verification, CLI, Docker image.
