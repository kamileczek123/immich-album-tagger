# Admin guide

Installing, configuring and operating Immich Album Tagger. For day-to-day tagging see
the [user guide](USER_GUIDE.md).

## 1. Requirements

- Immich reachable over HTTP(S) from where the app runs. Tested against the Immich 3.x
  API; older servers are handled by the legacy search mode.
- Docker with Compose, **or** Python 3.11+.
- Optional: an OpenAI-compatible LLM endpoint (`POST /v1/chat/completions`). A small
  model (3–8B) is enough; requests are short.
- Resources: ~100 MB RAM, negligible CPU. Data: a small SQLite file.

## 2. Create the Immich API key

In Immich: **Account settings -> API Keys -> New API Key**. Create it **while logged in as
the user who owns the photos**. Immich lets only the owner of a photo change its tags and
description; photos of other users are skipped, so a key of the wrong account (for example
the admin account) changes nothing.

Grant these permissions (or *All*). Each line lists the Immich endpoint that requires it,
as declared in Immich's OpenAPI spec (3.x):

| Permission | Endpoint | Used for | Needed by |
| --- | --- | --- | --- |
| `user.read` | `GET /users/me`, `GET /users` | which user the key belongs to (decides which photos can be changed); users to share with | always |
| `album.read` | `GET /albums`, `GET /albums/{id}` | album list, details, owner and people with access | always |
| `asset.read` | `POST /search/metadata` | photos of an album, their owners and descriptions; verification | always |
| `asset.view` | `GET /assets/{id}/thumbnail` | cover thumbnails in the UI | UI (tagging works without it) |
| `tag.read` | `GET /tags` | existing tags: autocomplete, LLM context, verification | target *Tags on photos* |
| `tag.create` | `PUT /tags` | create tags (upsert, parents included) | target *Tags on photos* |
| `tag.asset` | `PUT /tags/assets` | attach tags to photos | target *Tags on photos* |
| `asset.update` | `PUT /assets` | write the `#tag` line into photo descriptions | target *Photo descriptions* |
| `album.update` | `PATCH /albums/{id}` | write the keyword line into the album description | target *Album description* |
| `albumUser.create` | `PUT /albums/{id}/users` | Share album: add a person | Share album |
| `albumUser.delete` | `DELETE /albums/{id}/user/{userId}` | Share album: remove a person | Share album |

`GET /server/version` needs no permission.

**Not needed** (the app never calls the endpoints that require them; leave them off for a
least-privilege key):

| Permission | Why not |
| --- | --- |
| `album.share` | Immich checks it internally as *"is the user the owner or an editor of this album"*, not as an API key scope; sharing works with `albumUser.create`. |
| `albumUser.update` | Only for changing the role of someone already in the album (`PUT /albums/{id}/user/{userId}`). The app adds and removes people; to change a role, remove and add again, or use Immich's UI. |
| `tag.update` | Only for renaming or recolouring a tag (`PUT /tags/{id}`). Creating tags uses `tag.create`. |

Minimal keys per use:

| Use | Permissions |
| --- | --- |
| Tags only | `user.read album.read asset.read asset.view tag.read tag.create tag.asset` |
| Everything in the app | the above + `asset.update album.update albumUser.create albumUser.delete` |

A permission that is missing shows up as `HTTP 403` from Immich in the app's message,
naming the endpoint.

The app sees exactly what this user sees: own albums and albums shared with them.

With `auth.mode: immich_keys` (section 5) the web UI does not use this key: it creates a key
**per login** with the permissions in `auth.key_permissions` (the table above; default: all
eleven) plus `apiKey.delete`. The key from `.env` is then optional and used by the CLI only.

## 2a. External libraries must be writable

Tags and descriptions of photos in an **external library** only stick if Immich can write
`.xmp` sidecar files next to the photos. On a read-only mount (`:ro` in Immich's compose
file, or a read-only NFS/SMB export) Immich accepts the change, fails to write the sidecar,
re-reads the metadata from the file within seconds and **silently removes the change** —
for edits made in Immich's own UI too. Immich's documentation describes this; there is no
setting to store sidecars elsewhere.

Symptoms: right after applying, the app warns *Tag 'x' is on 0 of N photos … external
library*; Immich's server log shows `Error writing exif data (… .xmp): Error creating file`.

Check from the host:

```bash
docker inspect immich_server --format '{{range .Mounts}}{{.Destination}} RW={{.RW}}{{"\n"}}{{end}}'
docker exec immich_server touch "/path/to/library/some-album/.write-test" && echo writable
```

To make it writable: remove `:ro` from the library mount in Immich's compose file; for NFS
also mount the volume `rw` and export it `rw` to the Docker host (with `root_squash`, use
`all_squash` with `anonuid`/`anongid` of the files' owner, or `no_root_squash`). Recreate
the Immich container afterwards.

Trade-off: with a writable library, emptying Immich's trash **deletes the original file**.
Only owners can trash their photos (people you share with cannot), but keep snapshots or
backups of the library. Album descriptions live in Immich's database and work on read-only
libraries too.

## 3. Install with Docker Compose (recommended)

```bash
git clone <your-repo-url> immich-album-tagger
cd immich-album-tagger
cp .env.example .env
cp docker-compose.yml.example docker-compose.yml
$EDITOR .env
docker compose run --rm immich-album-tagger check
docker compose up -d
```

`check` prints one line per test and exits non-zero on failure:

```
Immich  OK   http://immich.lan:2283/api  version 3.2.1, user alex@example.com
        OK   412 albums visible to this API key
        OK   57 existing tags
LLM     OK   https://llm.example/v1
        OK   model replied: '{"ok": true}'
```

The UI listens on port 8080. Mounts in `docker-compose.yml` (a copy of the shipped `docker-compose.yml.example`, ignored by git, so your changes stay local):

| Container path | Host | Content |
| --- | --- | --- |
| `/config` | `./config` | `immich.yaml`, `llm.yaml`, `tagging.yaml`, `app.yaml`, `credentials.yaml`. **Read-write** if you use `credentials.yaml`, because it is hashed in place on the first start. |
| `/data` | volume `immich-album-tagger-data` | `history.sqlite3`, `session.key` |
| `/language` | `./language` | translation files, installed and upgraded by the app |
| `/logs` | `./logs` | `operations.log` |

All of them must be writable by UID 10001 (the container user), except `/config` when there
is no `credentials.yaml`.

To require a login, copy `config/credentials.yaml.example` to `config/credentials.yaml`
and set a user name and password before the first start (see "credentials.yaml" below).

### Reaching Immich by container name

If Immich runs in Compose on the same host, join its network and use the service name:

```yaml
services:
  immich-album-tagger:
    networks: [immich]
networks:
  immich:
    external: true
    name: immich_default        # docker network ls
```

and set `IMMICH_URL=http://immich_server:2283` (the name of the Immich server container).

### Image and building from source

Released versions are published on Docker Hub as
[`kamileczek123/immich-album-tagger`](https://hub.docker.com/r/kamileczek123/immich-album-tagger) for `linux/amd64` and `linux/arm64`, tagged
`X.Y.Z`, `X.Y` and `latest`. The example compose file uses it; pin a version for
predictable upgrades:

```yaml
    image: kamileczek123/immich-album-tagger:0.9.2
```

To run your own build, replace `image:` with `build: .` (see the comment in the compose
file) and use `docker compose up -d --build`.

Run commands in the container with the same name, e.g.
`docker compose run --rm immich-album-tagger check`.

## 3a. Reverse proxy and HTTPS

The app listens on plain HTTP (port 8080 in the container, 8081 on the host in the
examples). For anything beyond a test, put it behind a reverse proxy with TLS. All links,
API calls and redirects of the app are **relative**, so the same app, with no setting
changed, works in both layouts below, and also directly on its port.

A complete, commented nginx file with both layouts (HTTP->HTTPS redirect, headers,
timeouts) is in [`config/nginx-reverse-proxy.conf.example`](../config/nginx-reverse-proxy.conf.example);
the blocks below are the essential parts.

**A. Own host name**, e.g. `https://iat.example.com/`:

```nginx
server {
    listen 443 ssl http2;
    server_name iat.example.com;
    # ssl_certificate ...;

    location / {
        proxy_pass http://<docker host>:8081;
        proxy_set_header Host $host;
        proxy_read_timeout 600s;    # Apply waits for Immich's verification
    }
}
```

**B. Under a path of Immich's host name**, e.g. `https://immich.example.com/IAT/`. Add to
Immich's `server` block, before `location /`:

```nginx
location = /IAT { return 301 /IAT/; }
location /IAT/ {
    proxy_pass http://<docker host>:8081/;   # trailing slash: /IAT is stripped
    proxy_set_header Host $host;
    proxy_read_timeout 600s;
}
```

Any path works (`/tagger/`, …); it only has to end with `/` in the browser, which the
`return 301` takes care of. On Immich's host name the app shares the browser storage with
Immich, so it follows Immich's light / dark theme, and the sidebar shows *Open Immich*
(a link to the domain root); album covers open the album in Immich in a new tab. Under
layout A the app keeps its own theme setting; set `ui.immich_public_url` to get the Immich
links there too.

**HTTPS and the session cookie.** Behind HTTPS set in `app.yaml`:

```yaml
auth:
  secure_cookie: true
```

The browser then sends the session cookie only over HTTPS, so it can never leak over plain
HTTP. This is the recommended production setting. The default is `false` only so that a
first start and local tests over `http://host:8081` work: with `true`, a login over plain
HTTP is accepted but not kept (the next request asks again). With an Immich login
(`auth.mode: immich` / `immich_keys`) the password passes through the app, which is one
more reason to use HTTPS only.

## 4. Install without Docker

```bash
python3 -m venv .venv
.venv/bin/pip install .
set -a; . ./.env; set +a
.venv/bin/immich-album-tagger --config-dir ./config check
HOST=0.0.0.0 DATA_DIR=/var/lib/immich-album-tagger LANGUAGE_DIR=/var/lib/immich-album-tagger/language \
  LOG_DIR=/var/log/immich-album-tagger .venv/bin/immich-album-tagger --config-dir ./config serve
```

A minimal systemd unit:

```ini
[Unit]
Description=Immich Album Tagger
After=network-online.target

[Service]
User=immich-album-tagger
WorkingDirectory=/opt/immich-album-tagger
EnvironmentFile=/opt/immich-album-tagger/.env
Environment=CONFIG_DIR=/opt/immich-album-tagger/config DATA_DIR=/var/lib/immich-album-tagger HOST=127.0.0.1 PORT=8080
Environment=LANGUAGE_DIR=/var/lib/immich-album-tagger/language LOG_DIR=/var/log/immich-album-tagger
ExecStart=/opt/immich-album-tagger/.venv/bin/immich-album-tagger serve
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## 5. Configuration reference

Configuration is split into environment variables (secrets, deployment) and YAML files in
`CONFIG_DIR`: `immich.yaml` (required), `llm.yaml`, `tagging.yaml`, `app.yaml` and
`credentials.yaml` (all optional).

**Every file has a fully commented example in `config/`** — `immich.yaml.example`,
`llm.yaml.example`, `tagging.yaml.example`, `app.yaml.example`, `credentials.yaml.example` —
listing every setting with its purpose and default value (the default prompts included).
Copy one without `.example` and change only what you need; keys left out keep their
defaults. The tables below are a summary. Any YAML string can reference the environment as
`${NAME}` or `${NAME:-default}`. Invalid configuration stops the app with a message
naming the field.

### Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `IMMICH_URL` | — | Immich base URL; `/api` is appended if missing |
| `IMMICH_API_KEY` | — | API key (section 2) |
| `LLM_ENABLED` | `auto` | `auto` = on when `LLM_BASE_URL` and `LLM_MODEL` are set, else off; `true` / `false` force it (`true` without them is an error) |
| `LLM_BASE_URL` | — | OpenAI-compatible base URL including `/v1` |
| `LLM_API_KEY` | empty | Bearer token, if the endpoint needs one |
| `LLM_MODEL` | — | Model name as listed by `/v1/models` |
| `APP_USERNAME`, `APP_PASSWORD` | empty | Plain login when there is no `credentials.yaml` |
| `CONFIG_DIR` | `config` (`/config` in Docker) | Where the YAML files are |
| `DATA_DIR` | `data` (`/data` in Docker) | `history.sqlite3`, `session.key` |
| `LANGUAGE_DIR` | `language` (`/language` in Docker) | translation files, installed by the app |
| `LOG_DIR` | `logs` (`/logs` in Docker) | `operations.log` |
| `HOST`, `PORT` | `127.0.0.1`, `8080` (`0.0.0.0` in Docker) | Listen address |
| `LOG_LEVEL` | `INFO` | `DEBUG` shows every Immich/LLM request |

### `immich.yaml`

| Key | Default | Meaning |
| --- | --- | --- |
| `url`, `api_key` | from env | see above |
| `verify_tls` | `true` | `false` for self-signed certificates (prefer a proper CA) |
| `timeout_seconds` | `30` | per request |
| `search_api` | `auto` | `filter` (Immich ≥ 3.2), `legacy` (< 3.2), `auto` = try filter, fall back |
| `page_size` | `1000` | photos per search page (Immich max 1000) |
| `tag_batch_size` | `1000` | photos per tagging request |
| `include_archived` | `true` | also tag archived photos of the album (filter mode only) |
| `verify_after_apply` | `true` | count tagged photos and description lines after applying |
| `verify_delay_seconds` | `5` | wait before verifying, so changes Immich reverts in background jobs are caught; `0` = immediately |
| `verify_retries` | `3` | if something is still missing, check again this many times before warning |
| `verify_retry_seconds` | `5` | pause between those checks; worst case an apply waits `delay + retries × retry` seconds |

### `llm.yaml`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled`, `base_url`, `api_key`, `model` | from env | see above |
| `temperature` | `0.3` | higher = more varied suggestions |
| `max_tokens` | `2048` | replies cut off are not valid JSON; raise for reasoning models |
| `timeout_seconds` | `90` | slow local models may need more |
| `json_mode` | `false` | send `response_format: json_object`; only if the server supports it |
| `extra_headers` | `{}` | extra HTTP headers (gateways, routing) |
| `extra_body` | `{}` | merged into the request, e.g. `chat_template_kwargs: {enable_thinking: false}` for Qwen3-style models on llama-server |
| `max_concurrent` | `2` | requests sent to this endpoint at once; for llama-server its `--parallel` slots |
| `endpoints` | `[]` | more models / endpoints used side by side (below) |

Replies are parsed leniently: `<think>` blocks, code fences and text around the JSON are
ignored.

**Speed: parallel requests and several models.** Each album needs two LLM requests (tag
variants, base forms of the title words); they are sent together. All requests of the app
(background preparation, every login, the CLI) go through one pool:

- each endpoint gets at most `max_concurrent` requests at once; more wait in the app;
- with `endpoints`, requests go round robin to whichever endpoint has a free slot, so
  several models work side by side;
- a request that fails is sent to the next endpoint; the failing one is skipped for a
  minute while others work (failover);
- what the user opens goes before background preparation.

```yaml
model: aux/qwen35-4b        # the main endpoint, as above
max_concurrent: 2           # llama-server --parallel 2
endpoints:                  # same gateway (base_url, api_key inherited), other models
  - model: router/qwen35-4b
    max_concurrent: 1
  - model: big-model        # or another server
    base_url: http://gpu-host:8080/v1
    api_key: ${LLM_GPU_API_KEY}
```

Every entry needs `model`; `base_url`, `api_key`, `max_concurrent`, `extra_headers`,
`extra_body` default to the main endpoint's. More endpoints are faster only when they run
on separate hardware: two models on one CPU or GPU share its speed. Models can differ:
suggestions then come from whichever model was free (the tag rules and normalisation are
the same). `immich-album-tagger check` tests every endpoint on its own; `GET /api/status`
-> `llm.endpoints` shows each one's slots, calls and failures.

### `tagging.yaml`

**`normalization`** — applied to every tag from every source, including typed ones.
Always: control characters and punctuation (`, ; : ! ? " ( ) [ ] { }`) are removed,
whitespace is collapsed, empty hierarchy levels are dropped.

| Key | Default | Effect |
| --- | --- | --- |
| `lowercase` | `true` | `Chorwacja` -> `chorwacja` |
| `space_replacement` | `-` | `Stare Miasto` -> `stare-miasto`; `" "` keeps spaces |
| `strip_diacritics` | `false` | `Kraków` -> `krakow` (handles `ł`, `ø`, `ß`...) |
| `max_length` | `100` | whole tag, including parents |
| `max_depth` | `4` | hierarchy levels kept |

**`album_name`** — `template` with `$name` (the title; `/` inside titles is flattened so
it does not create levels). Default `album/$name`.

**`rules`** — list of regex rules matched against the title (`re.search`, case-insensitive
by default). Named groups become placeholders; `$name` is always available. A tag is
produced only if every placeholder in it was captured. `value_maps` translates captured
values. Example:

```yaml
- name: trip-country
  pattern: '\b(?P<country>chorwacja|włochy|albania|grecja)\b'
  tags: [miejsce/$country, wydarzenie/wyjazd]
  value_maps:
    country: {włochy: wlochy}
```

Placeholders that do not match a group fail at start-up, so typos are caught early.
Remember to quote regexes in single quotes in YAML.

**`keywords`** — every word of the title with at least `min_length` letters, minus
`stopwords`, minus tokens without letters (`2019`, `2019-07`) when `skip_numbers` is
true, capped at `max_tags`. `template` can group them, e.g. `slowo/$word`.
Quote stopwords: unquoted `on`, `no`, `yes` are booleans in YAML.

**`llm`** — `variants` (how many proposals), `max_tags_per_variant`, `language`,
`existing_tags_limit` (how many existing tags are sent for reuse; `0` = none),
`system_prompt` and `user_prompt`.

Prompt placeholders: system — `$variants $max_tags $language`; user — `$title
$description $asset_count $date_range $existing_tags $language`. Use `$$` for a literal
dollar sign. The top-level groups named in the default system prompt should match the
prefixes used by your rules, so LLM and rule suggestions fit together.

LLM keyword normalisation: `normalize_keywords` (default `true`) makes one extra short LLM
call per suggestion that returns base forms of the title words ("Zielonej" -> "zielona") and
up to `related_keywords` related words. `keywords_prompt` (placeholders `$language
$related`) controls it.

**`descriptions`** — the lines the app owns:

| Key | Default | Meaning |
| --- | --- | --- |
| `photo.tag_prefix` | `#` | every tag is written as `#tag` on one line; any description line made only of `#words` is treated as the app's line and replaced |
| `album.label` | `Słowa kluczowe: ` | the album description line starting with this label is the app's line |

Changing these after applying leaves old lines in place (they no longer match); apply again
and remove the old lines by hand, or keep the defaults.

**`prefetch`** — background preparation of suggestions:

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | `false` = suggestions only when an album is opened |
| `albums` | `10` | how many of the next untagged own albums (list order) to keep ready |
| `max_age_minutes` | `240` | prepared results older than this are prepared again |
| `retry_after_minutes` | `5` | after a failure or a result without the LLM, wait before retrying that album |
| `workers` | `0` | albums prepared at once; `0` = the LLM pool's slots (sum of `max_concurrent` in `llm.yaml`), `1` without LLM |

The window follows the list on the user's screen: with a search or filter, the browser
sends the shown album order (`POST /api/prefetch/focus`) and the first untagged albums of
that list are prepared next. Prepared albums that leave the window stay cached (up to
`albums` more). Without a per-person login (`auth.mode: immich_keys`) everyone shares one
queue, so the last filter wins.

The workers share the LLM pool, so the LLM never gets more requests than its
`max_concurrent` allows, and what the user opens goes first. Results are kept in memory
only and are lost on restart. Cost: up to `albums` × (one keyword call + one variants call)
to the LLM after start-up, then one album per apply.

**`apply`** — which targets are ticked when the panel opens and what the CLI `apply` writes
by default: `tags` (`true`), `photo_description` (`false`), `album_description` (`false`).

**`preselect`** — variants put into *Tags to apply* when the panel opens an album that is
not tagged yet, merged in this order: `first` (the first variant shown), `album-name`,
`rules`, `keywords`, `keywords-base`, `llm` (first LLM variant), `llm-all` (every LLM
variant); `[]` starts empty. Default `[first]`. Example: `preselect: [rules, llm]`.

**Changing and removing.** With the *Tags on photos* target, Apply is a sync: tags the app
applied to the album earlier (its history) and missing from the new selection are taken
off the album's own photos (`DELETE /tags/{id}/assets`, permission `tag.asset`), except
photos another album the app tagged with the same tag holds. *Remove tagging* (panel,
`POST /api/albums/{id}/remove`, CLI `remove`) takes back all the app's tags and its
description lines; the album is untagged again. Both are verified like Apply and logged.
Tags applied by 0.8.0 or older count as the app's (every tag it ever applied to the album).
Immich does not record who added a tag: a photo that had the tag before the app applied it
loses it as well. Tags stay in Immich's tag list.

Every config file may be wrapped in its own section name (`immich:` at the top of
`immich.yaml` etc.). Unknown keys stop the app with an error naming the key, so typos do not
go unnoticed. Only UPPER_CASE `${NAME}` is read from the environment; lower-case `${year}`
stays for tag templates.

Changes to YAML require a restart: `docker compose restart immich-album-tagger`.

### `app.yaml`

| Key | Default | Meaning |
| --- | --- | --- |
| `language` | `english` | translation file `LANGUAGE_DIR/<language>.lan`; shipped: `english`, `polish` |
| `ui.async_actions` | `true` | fast mode: Apply/Share close the panel at once, results come as notifications |
| `ui.toast_ms` | `5000` | how long a notification stays, in ms (1000–120000) |
| `ui.immich_theme` | `true` | take colors and font from the running Immich (see *Look* below) |
| `ui.immich_theme_url` | `""` | Immich web address to read them from, reachable from the app's server; empty = `immich.url` without `/api` |
| `ui.immich_theme_refresh_minutes` | `60` | how often the theme is read again (1–1440) |
| `ui.immich_public_url` | `""` | Immich's address in the browser (`https://immich.example.com`): album covers open the album there in a new tab. Empty: automatic under a path of Immich's host name (layout B), no links on an own host name (layout A) |
| `sharing.default_users` | `[]` | e-mails pre-filled in the Share panel and the bulk bar |
| `sharing.default_role` | `viewer` | `viewer` or `editor` |
| `auth.mode` | `auto` | `auto` = `credentials.yaml` if present, else none; `immich` = Immich accounts, shared API key; `immich_keys` = Immich accounts, each with their own temporary key; `credentials`; `none` |
| `auth.allowed_users` | `[]` | modes `immich`/`immich_keys`: e-mails or user ids allowed to log in; empty = only the API key owner (`immich`) or every Immich account (`immich_keys`) |
| `auth.key_permissions` | the 11 permissions of section 2 | `immich_keys`: permissions of the key created at login; `apiKey.delete` is always added |
| `auth.key_sweep_minutes` | `5` | `immich_keys`: how often keys of expired sessions are deleted |
| `auth.session_hours` | `12` | how long a login lasts |
| `auth.secure_cookie` | `false` | `true` behind an HTTPS proxy (recommended for production, section 3a): the session cookie is sent only over TLS |

### Look: colors and font from Immich (`ui.immich_theme`)

The app's layout (navbar, sidebar, album cards, panels) is its own, styled after Immich's
web UI. Colors and the font are **taken from the running Immich**, so they follow Immich
after an upgrade without a new app release:

- The app's server reads Immich's web page (`immich_theme_url`, default: `IMMICH_URL`
  without `/api`), finds its stylesheet and keeps only the design tokens: the CSS
  variables `--immich-ui-*` / `--immich-*` (light and dark), the Tailwind colors they refer
  to, and the GoogleSans font. The browser gets them as `immich-theme.css`; the font is
  passed through the app (`immich-theme/fonts/…`), so the browser never needs to reach
  `IMMICH_URL`. Only fonts named in Immich's stylesheet are served.
- They are read at start-up and again every `immich_theme_refresh_minutes`. If Immich
  cannot be read, the last good copy stays; with none, built-in Immich-like colors and the
  system font are used (log: *Immich theme not loaded …*). `GET /api/status` -> `theme`
  shows the state.
- `immich_theme: false` always uses the built-in colors.

The address must be the web UI of the same Immich (the `immich-server` container serves
it on the API port). The app reads only public files there: no key is used.

### Login with Immich accounts (`auth.mode: immich`)

```yaml
auth:
  mode: immich
  allowed_users: []          # empty = only the owner of the API key
```

The login page asks for the e-mail and password of an Immich account. The app sends them to
Immich (`POST /api/auth/login`, a public endpoint, no API key permission needed); Immich
checks them and opens a session, which the app closes again immediately
(`POST /api/auth/logout`), so no sessions pile up under *Authorized devices*. Then the app
checks `allowed_users` and issues its own session cookie. Passwords are never stored.

Things to know:

- Password login must be enabled in Immich. With OAuth-only Immich, use `credentials` or an
  authenticating reverse proxy in front of the app.
- The password passes through the app on its way to Immich: use HTTPS (and
  `secure_cookie: true`) when the app is reachable beyond the LAN.
- Everyone allowed works with the API key, so they tag and share the **key owner's** photos.
  Add other people to `allowed_users` only if that is intended.
- `credentials.yaml` is ignored in this mode, and the config directory may be read-only.
- If Immich is down, nobody can log in (existing sessions keep working).

### Everyone with their own Immich account (`auth.mode: immich_keys`)

```yaml
auth:
  mode: immich_keys
  allowed_users: []          # empty = every account of this Immich server
  key_permissions: [user.read, album.read, album.update, albumUser.create, albumUser.delete,
                    asset.read, asset.update, asset.view, tag.read, tag.create, tag.asset]
  key_sweep_minutes: 5
```

Each person logs in with their Immich e-mail and password and then works **on their own
account**: sees their own albums and the ones shared with them, and tags, describes and
shares their own photos. Tags, the *Tagged* markers and the prepared suggestions are per
person; the operation log is shared and can be filtered by person.

Life of the key:

1. **Login.** The app logs in to Immich (`POST /auth/login`), deletes keys left behind by
   earlier sessions of that person (see below), creates a new key
   (`POST /api-keys`, named `immich-album-tagger session <expiry, UTC> <random>`) with
   `key_permissions` + `apiKey.delete`, and closes the Immich session again.
   The key's secret is kept **in memory only**, never on disk.
2. **Work.** Every request of that session goes to Immich with that person's key.
3. **Logout.** The key deletes itself (`DELETE /api-keys/{id}`); it disappears from
   *Account settings -> API Keys* in Immich.
4. **Expiry.** Every `key_sweep_minutes` the app deletes the keys of sessions older than
   `session_hours`. If Immich is down, the deletion is retried at the next sweep.
5. **Restart or crash.** Sessions are lost (everyone logs in again; the login page says so)
   and their keys stay in Immich. They are deleted at that person's next login. Keys of
   people who never log in again stay until deleted by hand in Immich (*Account settings ->
   API Keys*); the expiry date in the name tells when they became useless. Only keys named
   exactly like above are touched; keys you made yourself are never deleted.

Things to know:

- **`apiKey.delete`** is needed so the key can delete itself at logout. It would also allow
  deleting the person's other API keys; the app never does that.
- The app warns at start-up about every permission from section 2 missing in
  `key_permissions`, naming the feature that will not work.
- `IMMICH_API_KEY` is optional in this mode. Without it the web UI works and the CLI
  refuses to run.
- History from before 0.6.0 (entries without a user) is given to the first person who logs
  in in this mode.
- As with `immich`: password login must be enabled in Immich, passwords pass through the
  app (use HTTPS beyond the LAN), `credentials.yaml` is ignored.

### `credentials.yaml` (login)

Without this file the UI is open to everyone who can reach the port. To require a login,
create it in the config directory (see `credentials.yaml.example`):

```yaml
encrypted: false
username: alex
password: a long passphrase
```

On the next start the app replaces both values with one-way scrypt hashes and sets
`encrypted: true`. Logins are checked against the hashes; the plain values are not kept. To
change the login, write plain values again and set `encrypted: false`. The config directory
must therefore be **writable**; if it is read-only, the login works but the file stays in
plain text and a warning is logged.

**Hashes from a secret store (recommended with Ansible / Infisical).** Values may reference
environment variables, so you can make the hashes once and deploy a file that never holds
a plain password:

```bash
docker run --rm kamileczek123/immich-album-tagger:0.9.2 \
  python -c "from immich_album_tagger.auth import hash_secret as h; print(h('alex')); print(h('passphrase'))"
```

```yaml
encrypted: true
username: ${UI_USER_HASH}
password: ${UI_PASS_HASH}
```

Pass `UI_USER_HASH` and `UI_PASS_HASH` to the container. Hashes look like
`scrypt:<salt>:<digest>` and contain no `$`, so they survive `.env` files and Docker Compose.
A file like this is read as is and never rewritten, so the config directory may stay
read-only.

Sessions are signed with a key in `DATA_DIR/session.key`; deleting it logs everyone out.
`APP_USERNAME` / `APP_PASSWORD` are still honoured as plain credentials when there is no
`credentials.yaml`.

If you deploy the config with Ansible or similar, template `credentials.yaml` only once (or
with `force: no`), otherwise every deploy writes the plain values back — they are hashed again
on start, which works, but the plain text sits on disk until then.

### Translations (`LANGUAGE_DIR`)

All texts of the UI, the operation log and messages from the server come from
`<language>.lan` files. On every start the app copies its shipped files into `LANGUAGE_DIR`
(`/language` in Docker) when a file is missing or its `@version` is older than the app; the
old copy is kept as `<name>.lan.bak-<version>`. A file with the current version is left
alone, so you can edit it in place (restart to apply). To add a language, copy
`english.lan` to e.g. `german.lan`, set `@language`, `@code` (for plural rules, e.g. `de`)
and `@version`, translate, and set `language: german`. Missing keys fall back to English.

### Operation log (`LOG_DIR`)

`LOG_DIR/operations.log` (`/logs` in Docker) gets one JSON line per action. It is created
when missing, appended to across restarts, and emptied by *Clear log* in the UI. Rotate it
with logrotate (`copytruncate`) if it grows large. Technical logs still go to stdout.

## 6. Security

- The API key can add tags to every photo the user can see. Treat the UI as having that
  power: **create `credentials.yaml` or put the app behind an authenticating reverse
  proxy** (Authelia, Authentik, oauth2-proxy) if anyone else can reach the port.
- The browser never receives the Immich key; thumbnails are proxied by the app.
- Keep `.env` out of git (it is in `.gitignore`). Prefer a scoped key over *All*.
- Use TLS on the proxy if the app is reachable beyond the LAN and set
  `auth.secure_cookie: true`; over plain HTTP the login and the session cookie travel in
  clear text.
- The container runs as an unprivileged user (UID 10001) with a read-only config mount.
- With `auth.mode: immich_keys` nobody acts with someone else's key: each login gets a key
  of its own account, held in memory and deleted at logout or expiry.

## 7. Operations

- **Health:** `GET /api/health` (no auth) returns `{"status": "ok"}`; the Docker image
  has a `HEALTHCHECK` using it. `GET /api/status` (auth) also checks Immich.
- **Logs:** stdout. `LOG_LEVEL=DEBUG` logs each HTTP request to Immich and the LLM.
- **Backup:** `DATA_DIR/history.sqlite3`, `CONFIG_DIR`, edited `.lan` files and, if you
  want the history of actions, `LOG_DIR/operations.log`. None is critical: tags and
  descriptions live in Immich.
- **Reset tagged markers:** stop the app and delete `history.sqlite3`.
- **Upgrade the app:** change the image tag in the compose file (or `docker compose pull` for
  `latest`), then `docker compose up -d`; with a source build `git pull && docker compose up -d --build`. The history schema is
  created automatically.
- **Immich upgrades:** no action needed. After a major Immich upgrade run `check` and tag
  one test album. If the search API changes again, set `search_api` explicitly.

## 8. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `Configuration error: immich.api_key is empty` | `IMMICH_API_KEY` not set or `.env` not loaded |
| `HTTP 401 Invalid API key` | wrong or revoked key |
| `HTTP 403` on some calls | key lacks a permission from section 2 |
| `cannot reach Immich` | URL/port/network; inside Docker `localhost` is the container itself |
| `0 albums visible` | the key belongs to a user without albums |
| *LLM suggestions are unavailable: ...* | see message; `immich-album-tagger check` tests the endpoint |
| `LLM reply is not valid JSON` | model too small or reasoning cut off: raise `max_tokens`, disable thinking via `extra_body`, lower `temperature` |
| LLM very slow | use a smaller model, reduce `existing_tags_limit`, disable thinking |
| *Tag 'x' is on 0 of N photos … external library* | read-only library, see section 2a |
| *Tag 'x' is on N of M photos* (some missing) | click Apply again; if it persists, check Immich server logs |
| *N of M photos belong to other users* | expected for shared albums; for your own albums check that the key belongs to the photos' owner |
| Nothing changes at all, no warning | the key belongs to another account; `immich-album-tagger check` prints its owner |
| `Extra inputs are not permitted` at start-up | unknown or misspelled key in a YAML file; the message names it |
| Share: *No Immich user with e-mail…* | the person needs an account on this Immich server first |
| Share: HTTP 403 | key lacks `albumUser.create` / `albumUser.delete` |
| Archived photos not tagged | `include_archived` applies only to the 3.2+ filter search; on older Immich the legacy search may skip archived photos |
| Thumbnails missing | key lacks `asset.view`; tagging still works |
| Changes in YAML have no effect | restart the app |
| Login page appears although no login is wanted | `credentials.yaml` exists in the config directory (or `APP_USERNAME` is set), or `auth.mode` is not `auto`/`none` |
| *… may not use the album tagger* (mode immich) | the account is not in `auth.allowed_users` (empty list = only the API key owner) |
| *… could not create an API key for this session* (mode immich_keys) | see the app log: usually an unknown name in `auth.key_permissions` (Immich answers HTTP 400) |
| *The session has ended … log in again* (mode immich_keys) | the app was restarted (keys are kept in memory only) or the session expired |
| Old `immich-album-tagger session …` keys in Immich | left by a restart; deleted at that person's next login, or delete them by hand |
| `immich.api_key is empty …; the CLI needs it` | mode immich_keys without `IMMICH_API_KEY`: set it to use the CLI |
| *Immich cannot be reached to check the login* | Immich is down or `IMMICH_URL` is wrong; existing sessions keep working |
| Immich login always fails although the password is right | password login is disabled in Immich (OAuth only); use `credentials` mode |
| *Cannot rewrite credentials.yaml* in the log, file stays plain | config directory is mounted read-only; mount it read-write or accept the plain file |
| Logged out after every restart | `DATA_DIR` is not persistent, so `session.key` is new each time |
| Login works, but the next request asks again | `auth.secure_cookie: true` while the app is opened over plain HTTP |
| UI in English although `language: polish` | `LANGUAGE_DIR/polish.lan` is missing or misspelled; the log says *Language file … not found* |
| Edited translation was replaced | the app version changed; your copy is kept as `<name>.lan.bak-<version>` — merge your changes into the new file |
| *Cannot install language files* / *Cannot create operation log* | `/language` or `/logs` not writable by UID 10001 |
| Notifications disappear too fast | raise `ui.toast_ms` in `app.yaml` |
| Log: *Immich theme not loaded from …* | the app's server cannot read Immich's web page at `ui.immich_theme_url` (or `IMMICH_URL`); the app keeps working with built-in colors |
| Page looks unstyled under `/IAT` | open `/IAT/` (with the slash); add the `location = /IAT { return 301 /IAT/; }` redirect (section 3a) |
