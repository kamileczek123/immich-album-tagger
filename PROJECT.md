# Immich Album Tagger — project brief

This document explains why the project exists, what it must do, what it deliberately does
not do, and where it can go next. Read it before changing behaviour; read
[docs/DEVELOPER.md](docs/DEVELOPER.md) before changing code.

## 1. Problem

An Immich library has been organised over the years into several hundred albums. The
albums carry real meaning — an event, a trip, a place, a year — but that meaning lives
only in the album title. Individual photos know nothing about it:

- Immich has no "album tags". Tags exist only on assets (photos and videos).
- Tagging in the Immich UI is per selection of assets. Doing it album by album for
  hundreds of albums is hours of repetitive clicking, and easy to get wrong.
- Search, smart albums and workflows in Immich filter by tags, not by album membership
  semantics, so the knowledge stored in album titles is not usable there.
- Album titles are inconsistent: `2019-07 Chorwacja`, `Wedding Monica and Tom 2021`,
  `Durres - apartment`. A single fixed rule cannot turn all of them into good tags.

The goal: **move the meaning of each album onto every photo in it, with a human deciding
the words — quickly enough that doing it for hundreds of albums is realistic — and make it
findable by the people the albums are shared with, not only by the owner.**

The second half turned out to matter as much as the first (see section 5): Immich tags are
private to one account, so tags alone do not reach anyone the album is shared with.

## 2. What was considered and rejected

| Option | Why not |
| --- | --- |
| Official `immich-cli` | Uploads files only; no tag or album operations on existing assets. |
| `immich-folder-album-creator` | Works on filesystem folders, not existing albums. "Tag = album name" is an open feature request there. |
| `immich-autotag` | Rule engine driven by config. Good for regular naming patterns, but every pattern must be written by hand and there is no per-album review or suggestion step. |
| Immich workflows / Wasm plugins (3.0+) | Server-side and event-driven: react to new assets, cannot provide a review UI, and do not naturally process an existing backlog. |
| Patching the Immich web UI | A native button inside Immich, but a fork to maintain against a fast release cadence. |
| Browser userscript | Button inside Immich without a fork, but brittle against DOM changes and tied to one browser. |

**Chosen:** a small companion web app next to Immich that talks to the public REST API.
It never modifies Immich itself, survives Immich upgrades, and can later gain a userscript
front-end that calls the same backend (see Roadmap).

## 3. Users and use cases

**Primary user:** the owner/administrator of a self-hosted Immich instance, technical,
comfortable with Docker and YAML, curating their own (and family) photos.

### UC-1 Tag one album with reviewed suggestions
1. User opens the app and finds the album by title.
2. User clicks **Suggest tags**. The app proposes several variants: album name, rule
   matches (year, month, event...), title keywords and, if configured, LLM proposals.
3. User picks a variant (**Use**), combines variants (**Add**), edits, removes or types
   tags, with autocomplete from tags already in Immich.
4. User clicks **Apply**. Every photo in the album receives every tag. The app verifies
   the result in Immich and reports gaps.

### UC-2 Work through the backlog
1. User filters to **Not tagged yet** and processes albums one after another (UC-1).
2. The list shows which albums were tagged, when, and with what.

### UC-3 Bulk-tag hundreds of albums offline
1. `immich-album-tagger suggest-all --out suggestions.json --only-untagged` writes suggestions
   for every album, with one variant pre-selected.
2. User reviews/edits the `selected` field in a text editor.
3. `immich-album-tagger apply --file suggestions.json --dry-run`, then without `--dry-run`.

### UC-4 Re-apply after the album grows
Photos added to an already-tagged album are untagged. Opening the album pre-fills the
previously applied tags; **Apply** again tags the new photos. Existing links are not
duplicated.

### UC-5 Tune the suggestions
Admin edits `tagging.yaml` (rules, stopwords, prompt, normalisation) and restarts.
Suggestions change; tags already applied do not.

### UC-7 Make an album findable for the people it is shared with
1. In the panel the user ticks **Photo descriptions** and/or **Album description**.
2. Album keywords offer each title word and its base form ("Zielonej" / "zielona") plus
   related words; the user keeps either or both.
3. Apply writes `#tag` lines into photo descriptions and a keyword line into the album
   description. Recipients find the album on their *Albums* page by keyword and the photos
   inside it by description.

### UC-8 Share an album
**Share album** shows who has access, adds an Immich user by e-mail as viewer or editor,
and removes access.

### UC-6 Check connectivity
`immich-album-tagger check` confirms the Immich URL, API key, album/tag visibility and that the
LLM endpoint answers.

## 4. Requirements

### Functional
| ID | Requirement |
| --- | --- |
| F-1 | List all albums visible to the API key, with title, photo count, date range, cover. |
| F-2 | Filter albums by title text and by tagged / not-tagged state. |
| F-3 | Generate suggestion variants for an album from: album name template, regex rules, title keywords, LLM. Each generator can be disabled. |
| F-4 | The LLM receives title, description, photo count, date range and existing Immich tags, and must return several distinct variants. |
| F-5 | The user can use, merge, edit, remove and add tags before applying; existing tags autocomplete. |
| F-6 | All tags from all sources (including manual input) are normalised by one configurable function. |
| F-7 | Hierarchical tags (`a/b/c`) are supported; parents are created as needed. |
| F-8 | Applying tags attaches every selected tag to every asset of the album, including archived ones (configurable). |
| F-9 | After applying, the app verifies per tag how many album assets carry it and reports gaps. |
| F-10 | The app records what was applied to which album and shows it in the list. |
| F-11 | A CLI supports connectivity check, bulk suggestion export and bulk apply with dry run. |
| F-12 | Per apply the user chooses the targets: tags, photo descriptions, album description, in any combination; defaults are configurable and the UI remembers the last choice. |
| F-13 | Photo descriptions get one app-owned line of `#tags`; album descriptions one app-owned keyword line. Text written by people is never changed; re-applying replaces the app's line. |
| F-14 | Album keywords show each title word with its base form from the LLM, both selectable, plus related words and free input. |
| F-15 | Only photos owned by the API key's user are changed; the result reports skipped photos of other users. The album description is changed only for own albums. |
| F-16 | Verification runs after a configurable delay and names a read-only external library as the likely cause when changes disappear. |
| F-17 | The owner can share an album with an Immich user by e-mail (viewer or editor), list and remove access. |
| F-18 | Suggestions for the next N untagged own albums are prepared in the background, one at a time, kept in memory; applying an album moves the window on. |
| F-19 | Many albums can be shared with (or unshared from) one user in one action, from the UI (selection + filter) and the CLI (title regex). |
| F-20 | The UI shows the app version. |
| F-21 | Fast mode: Apply and Share do not block the UI; results arrive as timed notifications (green on success, red with a link to the log on errors), stacked FIFO. Switchable in `app.yaml`. |
| F-22 | Default people to share with are configured and pre-filled; several people per share. |
| F-23 | Every action is written to a persistent operation log with SUCCESS/ERROR, action, album and values; a Logs page shows and clears it. |
| F-24 | Optional login from `credentials.yaml`, hashed one-way on first start. |
| F-26 | The UI can use Immich accounts for login (password checked by Immich, session closed at once), limited to allowed accounts; the actor is recorded in the operation log. |
| F-30 | Apply syncs Immich tags: tags the app applied to the album earlier and no longer selected are taken off its photos (photos of another album the app tagged the same way keep them), announced before applying. *Remove tagging* takes back all of the app's tags and description lines; both are verified and logged; CLI `remove`. |
| F-31 | The variants pre-selected when an untagged album opens are configurable (`preselect`). |
| F-27 | Optional per-person mode: each login works with a temporary API key of that person's Immich account (created at login with configured permissions, held in memory only, deleted at logout or expiry, leftovers deleted at the next login). History and prepared suggestions are per person; the operation log is shared and filterable by person. |
| F-28 | The UI looks like part of Immich: layout styled after Immich's web UI; colors and font read from the running Immich at run time (built-in fallback), light / dark shared with Immich on the same domain. |
| F-29 | The app works on its own host name and under a path prefix of a reverse proxy (e.g. Immich's `/IAT/`) without configuration. |
| F-25 | All texts (UI, log, server messages) come from versioned `.lan` translation files; English and Polish shipped. |

### Non-functional
| ID | Requirement |
| --- | --- |
| N-1 | No modification of Immich: public REST API only, with a scoped API key. |
| N-2 | Works with Immich before and after the 3.2 search API change (auto-detected). |
| N-3 | Works without an LLM; LLM failure degrades to rule-based suggestions with a warning. |
| N-4 | Any OpenAI-compatible endpoint; no vendor SDK. Local models preferred, so photo metadata stays at home. |
| N-5 | Configuration in three readable YAML files; secrets only via environment variables. |
| N-6 | One container, no external database, no frontend build step. |
| N-7 | Readable, small codebase in Python; tests run without network access. |
| N-8 | Idempotent apply: running it twice is safe. |
| N-9 | Optional login with one-way hashed credentials and signed session cookies; designed to sit behind a reverse proxy or on a LAN. |
| N-10 | No user-visible text is hard-coded: UI, operation log and server messages come from versioned translation files. |
| N-11 | In fast mode long operations never block the UI; their outcome is always recorded in the operation log. |

## 5. Assumptions and constraints

### Immich constraints (verified in Immich's source and docs, 3.x)

These shape the design; re-check them when Immich changes.

| Constraint | Consequence |
| --- | --- |
| Tagging and editing a photo require **ownership** of the photo (`AssetUpdate` -> owner check); other photos are skipped **silently**. | The key must belong to the photos' owner; the app splits own/foreign itself and reports. |
| Tags are **per user** (`tag.userId`), readable only by their owner. | Tags are not visible in the tag list/filters of people the album is shared with. Descriptions and album keywords exist for them. |
| On a **read-only external library**, Immich cannot write `.xmp` sidecars, re-reads metadata and **reverts** tag/description edits within seconds, without an error. | The library must be writable (admin guide 2a); verification is delayed to detect the revert. |
| Global search covers the user's own and partners' photos only; photos in albums shared with you are searchable only within that album. | Album keywords (Albums page filter, matches name **and** description) are the global entry point for recipients. |
| Description search and the Albums filter match **one substring**, ignoring case and accents. | Tags in descriptions use a `#` prefix and fixed order; keywords use base forms separated by spaces. |
| Sharing roles: viewer, editor. Only photo owners can delete photos; editors can only add/remove album membership. | Sharing does not let recipients delete originals, even on a writable library. |

### Other assumptions

- Album titles carry most of the meaning. Album descriptions and dates are used when present.
- One API key = one Immich user's view. Shared albums are visible if shared with that user;
  tagging assets owned by other users depends on Immich permissions.
- Tags are global in Immich per user. Removing tags is out of scope; Immich's own UI
  handles that.
- The history database is a convenience. Losing it resets "Tagged" markers but not tags.
- Thumbnails are proxied through the app so the browser never sees the API key.
- Single-user tool. No concurrency control beyond what Immich provides.

## 6. Key decisions

| Decision | Reason |
| --- | --- |
| Python + FastAPI + httpx, no Immich SDK | Small dependency surface; only ~8 endpoints needed; easy to follow. |
| Vanilla JS UI served as static files | No Node toolchain; the UI is one screen and one panel. |
| Suggestions as independent "variants" | The user compares alternatives instead of fixing one opaque result. |
| Rules + LLM side by side | Rules are predictable and free; the LLM handles messy titles. Neither alone was enough. |
| `string.Template` (`$name`) in config | Prompts contain JSON braces; `$` placeholders avoid escaping. |
| Verification after apply, after a delay | Immich can skip links silently and revert edits in background jobs; counting a few seconds later makes both visible. |
| Text targets next to tags | Tags are private to the owner; descriptions are the only per-photo field everyone with access can search. |
| App-owned single lines in descriptions | Users keep their own text; re-applying is idempotent. |
| Separate LLM call for keyword base forms | Keeps the variant prompt unchanged and lets either fail independently. |
| SQLite history in the app, not in Immich | Immich has no place for album-level metadata that the app could own safely. |

## 7. Roadmap

Ordered roughly by value for effort. None of this is implemented, except where marked.

1. ~~**Remove tags from an album** — the inverse of apply, for mistakes (tags, and the app's
   description lines).~~ Done in 0.9.0 (Apply syncs, *Remove tagging*).
2. **Batch mode in the UI** — select many albums, review prepared suggestions in a table,
   apply all (prepared suggestions exist since 0.3.0).
3. **Auto-tag new photos in tagged albums** — scheduled job (or Immich workflow trigger)
   that re-applies the last tags when an album gains photos.
4. **Userscript front-end** — a "Suggest tags" button inside the Immich album page,
   calling this app's API. The backend is already shaped for it.
5. **Use more context for the LLM** — EXIF places (city/country), people names from
   face recognition, a few CLIP captions of sample photos.
5a. **Own search page** — AND/OR across several tags or keywords via Immich's structured
   search API, which the Immich UI does not expose in one field.
5b. **Share by tag** — e.g. all albums tagged `osoby/ania` to Ania (bulk sharing by title
   and selection exists since 0.3.0).
5c. **Upstream** — feature request in Immich for shared or album-inherited tags; would make
   description lines unnecessary.
6. **Tag vocabulary management** — merge near-duplicate tags, enforce a list of allowed
   top-level groups.
7. ~~**Multi-user** — per-user API keys via login, history per user.~~ Done in 0.6.0
   (`auth.mode: immich_keys`).
8. **Async apply with progress** — for albums with tens of thousands of photos.

## 8. Glossary

- **Asset** — a photo or video in Immich.
- **Variant** — one complete list of suggested tags from one generator.
- **Generator** — a source of variants: album name, rules, keywords, LLM.
- **Apply** — attach a list of tags to every asset of an album.
- **Link** — one (tag, asset) pair in Immich.
