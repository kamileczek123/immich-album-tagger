// Album tagger main page. No framework, no build step: the server serves this file as-is.
import { $, api, el, icon, immichUrl, initChrome, initI18n, plural, settings, t, toast } from "./common.js";

const TARGETS_KEY = "album-tagger.targets";

const state = {
  me: null,
  albums: [],
  filter: "all",
  query: "",
  status: null,
  album: null, // album open in the tagging panel
  variants: [],
  usedVariant: -1,
  tags: [],
  keywords: [], // title words: [{word, base}]
  related: [], // related words from the LLM
  kwTouched: false, // true once the user edited the keyword line; stops auto-filling
  shareAlbum: null,
  busy: false,
  selecting: false, // bulk selection mode
  selected: new Set(), // album ids
};

const asyncMode = () => settings.ui.async_actions;

// ------------------------------------------------------------------ helpers

function tapeLabel(tag) {
  const parts = tag.split("/");
  const leaf = parts.pop();
  const frag = document.createDocumentFragment();
  if (parts.length) frag.append(el("span", { class: "parent", text: parts.join("/") + "/" }));
  frag.append(leaf);
  return frag;
}

function tape(tag) {
  return el("li", { class: "tape", title: tag }, tapeLabel(tag));
}

function thumbUrl(assetId) {
  return assetId ? `api/thumbnails/${encodeURIComponent(assetId)}` : "";
}

function addUnique(list, values, normalise = (v) => v.trim()) {
  for (const raw of values) {
    const value = normalise(raw);
    if (value && !list.some((v) => v.toLowerCase() === value.toLowerCase())) list.push(value);
  }
}

function isMine(album) {
  return !state.me || !album.owner_id || album.owner_id === state.me;
}

function splitUsers(text) {
  return text.split(/[\s,;]+/).map((u) => u.trim()).filter(Boolean);
}

function defaultUsers() {
  return (settings.sharing?.default_users || []).join(", ");
}

// ------------------------------------------------------------------ targets

function loadTargets() {
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(TARGETS_KEY));
  } catch {
    /* ignore */
  }
  const defaults = state.status?.apply_defaults ?? { tags: true };
  const targets = { ...defaults, ...(saved || {}) };
  $("#t-tags").checked = !!targets.tags;
  $("#t-photo").checked = !!targets.photo_description;
  $("#t-album").checked = !!targets.album_description;
}

function currentTargets() {
  return {
    tags: $("#t-tags").checked,
    photo_description: $("#t-photo").checked,
    album_description: $("#t-album").checked,
  };
}

function saveTargets() {
  try {
    localStorage.setItem(TARGETS_KEY, JSON.stringify(currentTargets()));
  } catch {
    /* private mode etc. */
  }
}

// ------------------------------------------------------------------ album list

async function loadStatus() {
  const conn = $("#conn");
  try {
    const status = await api("api/status");
    state.status = status;
    if (!status.immich.ok) throw new Error(status.immich.error);
    state.me = status.immich.user_id;
    const llm = status.llm.enabled
      ? t("status.llm_on", { model: status.llm.model })
      : t("status.llm_off");
    conn.textContent = t("status.connected", {
      version: status.immich.version,
      user: status.immich.user,
      llm,
    });
    conn.dataset.state = "ok";
  } catch (error) {
    conn.textContent = t("status.unreachable", { error: error.message });
    conn.dataset.state = "error";
  }
  loadTargets();
}

async function pollPrefetch() {
  if (document.hidden) return;
  let data;
  try {
    data = await api("api/prefetch");
  } catch {
    return;
  }
  const badge = $("#prefetch");
  badge.hidden = !data.enabled;
  if (!data.enabled) return;
  badge.textContent = data.window
    ? t(data.preparing ? "prefetch.preparing" : "prefetch.ready", { ready: data.ready, window: data.window })
    : t("prefetch.nothing");
  const ready = new Set(data.ready_ids);
  let changed = false;
  for (const album of state.albums) {
    const prepared = ready.has(album.id);
    if (album.prepared !== prepared) {
      album.prepared = prepared;
      changed = true;
    }
  }
  if (changed) renderAlbums();
}

async function loadAlbums() {
  $("#count").textContent = t("list.loading");
  try {
    state.albums = await api("api/albums");
    renderAlbums();
  } catch (error) {
    state.albums = [];
    renderAlbums();
    $("#empty").hidden = false;
    $("#empty").textContent = t("list.load_failed", { error: error.message });
  }
}

async function loadLists() {
  try {
    const tags = await api("api/tags");
    $("#tag-list").replaceChildren(...tags.map((tag) => el("option", { value: tag })));
  } catch {
    /* autocomplete is optional */
  }
  try {
    const users = await api("api/users");
    $("#user-list").replaceChildren(
      ...users.map((u) => el("option", { value: u.email, label: u.name || u.email })),
    );
  } catch {
    /* autocomplete is optional */
  }
}

function visibleAlbums() {
  const q = state.query.trim().toLowerCase();
  return state.albums.filter((a) => {
    if (state.filter === "tagged" && !a.applied) return false;
    if (state.filter === "untagged" && a.applied) return false;
    return !q || a.name.toLowerCase().includes(q) || (a.description || "").toLowerCase().includes(q);
  });
}

// Background preparation follows the list on screen: with a search ("2024") or a filter,
// the first untagged albums of that list are prepared first.
let focusTimer = null;
let focusSent = null; // null: send once after loading, the server may hold an old filter

function sendFocus() {
  clearTimeout(focusTimer);
  focusTimer = setTimeout(async () => {
    const filtered = state.query.trim() || state.filter !== "all";
    const ids = filtered ? visibleAlbums().map((a) => a.id) : [];
    const key = ids.join(",");
    if (key === focusSent) return;
    focusSent = key;
    try {
      await api("api/prefetch/focus", { method: "POST", body: JSON.stringify({ album_ids: ids }) });
      setTimeout(pollPrefetch, 1500);
    } catch {
      /* preparation keeps its previous order */
    }
  }, 500);
}

function renderAlbums() {
  const albums = visibleAlbums();
  const tagged = state.albums.filter((a) => a.applied).length;
  $("#count").textContent = t("list.count", {
    shown: plural(albums.length, "count.albums"),
    tagged,
    total: state.albums.length,
  });

  const empty = $("#empty");
  empty.hidden = albums.length > 0;
  empty.textContent = state.albums.length ? t("list.no_match") : t("list.none");

  $("#albums").replaceChildren(...albums.map(albumRow));
}

const CARD_TAGS = 6; // tags shown on a card; the rest as "+N"

function toggleSelected(album) {
  if (state.selected.has(album.id)) state.selected.delete(album.id);
  else state.selected.add(album.id);
  renderBulk();
  renderAlbums();
}

function albumRow(album) {
  const mine = isMine(album);
  const meta = [plural(album.asset_count, "count.photos")];
  if (album.date_range) meta.push(album.date_range);
  const others = album.users.filter((u) => u.role !== "owner");
  if (!mine) meta.push(t("list.shared_by", { owner: album.owner_email }));
  else if (others.length) meta.push(t("list.shared_with", { people: plural(others.length, "count.people") }));

  const applied = album.applied;
  let status;
  if (applied) {
    const shown = applied.tags.slice(0, CARD_TAGS);
    const more = applied.tags.length - shown.length;
    status = el(
      "div",
      { class: "applied" },
      applied.tags.length
        ? el(
            "ul",
            { class: "tapes", "aria-label": t("list.applied_tags") },
            ...shown.map(tape),
            more > 0 ? el("li", { class: "more", title: applied.tags.slice(CARD_TAGS).join(", "), text: `+${more}` }) : null,
          )
        : null,
      applied.keywords?.length
        ? el("p", { class: "keywords-line", text: t("list.keywords", { keywords: applied.keywords.join(" ") }) })
        : null,
    );
  } else {
    status = el("p", { class: "untagged", text: t("list.not_tagged") });
  }

  const selectable = state.selecting && mine;
  const selected = state.selected.has(album.id);
  const name = album.name || t("list.untitled");
  const cover = album.thumbnail_asset_id
    ? el("img", { class: "thumb", src: thumbUrl(album.thumbnail_asset_id), alt: "", loading: "lazy" })
    : el("span", { class: "thumb none" });

  // Cover: select (selection mode), else open the album in Immich in a new tab, or the
  // tagging panel when Immich's address is unknown.
  const inImmich = immichUrl(`albums/${encodeURIComponent(album.id)}`);
  let coverControl;
  if (!state.selecting && inImmich) {
    coverControl = el(
      "a",
      {
        class: "cover-btn",
        href: inImmich,
        target: "_blank",
        rel: "noopener",
        title: t("list.open_in_immich"),
        "aria-label": `${t("list.open_in_immich")}: ${name}`,
      },
      cover,
    );
  } else {
    coverControl = el(
      "button",
      {
        type: "button",
        class: "cover-btn",
        "aria-label": state.selecting ? t("bulk.select_one", { album: name }) : `${t("list.suggest")}: ${name}`,
        ...(state.selecting ? { "aria-pressed": String(selected) } : {}),
        ...(state.selecting && !mine ? { disabled: "", title: t("bulk.only_own") } : {}),
        onclick: () => (state.selecting ? selectable && toggleSelected(album) : openPanel(album)),
      },
      cover,
    );
  }

  const classes = ["album"];
  if (state.selecting) classes.push("selecting");
  if (selected) classes.push("selected");
  if (state.selecting && !mine) classes.push("locked");

  return el(
    "li",
    { class: classes.join(" ") },
    el(
      "div",
      { class: "card-cover" },
      coverControl,
      state.selecting && mine ? el("span", { class: "pick", "aria-hidden": "true" }, icon("check")) : null,
      album.prepared && !applied
        ? el("span", { class: "pill ready", title: t("list.ready_title"), text: t("list.ready") })
        : null,
    ),
    el(
      "div",
      { class: "card-body" },
      el("h2", { title: name, text: name }),
      el("p", { class: "meta", text: meta.join(" · ") }),
      status,
    ),
    state.selecting
      ? null
      : el(
          "div",
          { class: "row-actions" },
          el("button", { type: "button", class: "btn tonal", onclick: () => openPanel(album) }, icon("tag"), el("span", { text: t("list.suggest") })),
          mine
            ? el(
                "button",
                {
                  type: "button",
                  class: "icon-btn share-btn",
                  title: t("list.share"),
                  "aria-label": `${t("list.share")}: ${name}`,
                  onclick: () => openShare(album),
                },
                icon("share"),
              )
            : null,
        ),
  );
}

// ------------------------------------------------------------------ panels

function showPanel(id) {
  $("#scrim").hidden = false;
  $(id).hidden = false;
  $(`${id} .close`).focus();
}

function closePanels(force = false) {
  if (state.busy && !force) return;
  $("#panel").hidden = true;
  $("#share").hidden = true;
  $("#scrim").hidden = true;
  state.album = null;
  state.shareAlbum = null;
}

// ------------------------------------------------------------------ tagging panel

function openPanel(album) {
  state.album = album;
  state.variants = [];
  state.usedVariant = -1;
  state.tags = album.applied ? [...album.applied.tags] : [];
  state.keywords = [];
  state.related = [];
  const previous = album.applied?.keywords || [];
  state.kwTouched = previous.length > 0;
  $("#p-kw-line").value = previous.join(" ");
  $("#p-title").textContent = album.name || t("list.untitled");
  $("#p-meta").textContent = [plural(album.asset_count, "count.photos"), album.date_range]
    .filter(Boolean)
    .join(", ");
  $("#p-thumb").src = thumbUrl(album.thumbnail_asset_id);
  $("#t-album").disabled = !isMine(album);
  $("#p-prepared").hidden = true;
  setResult("");
  showPanel("#panel");
  renderChips();
  loadSuggestions();
}

async function loadSuggestions(refresh = false) {
  const album = state.album;
  const box = $("#p-variants");
  const warn = $("#p-warn");
  warn.hidden = true;
  box.replaceChildren(el("p", { class: "loading", text: t("panel.reading") }));
  $("#p-regen").disabled = true;
  try {
    const query = refresh ? "?refresh=true" : "";
    const data = await api(`api/albums/${encodeURIComponent(album.id)}/suggestions${query}`, {
      method: "POST",
    });
    if (state.album !== album) return;
    $("#p-prepared").hidden = !data.prepared;
    state.variants = data.variants;
    if (data.warnings.length) {
      warn.textContent = data.warnings.join(" ");
      warn.hidden = false;
    }
    if (!state.tags.length && state.variants.length) preselect();
    setKeywordSuggestions(data.keywords, data.related);
    renderVariants();
  } catch (error) {
    box.replaceChildren(el("p", { class: "warn", text: t("panel.no_suggestions_error", { error: error.message }) }));
  } finally {
    $("#p-regen").disabled = false;
  }
}

function variantTitle(variant) {
  // Generated labels (LLM) are already in the album's language; ours are translated.
  return variant.source === "llm" ? variant.label : t(`source.${variant.source}`);
}

function renderVariants() {
  const box = $("#p-variants");
  if (!state.variants.length) {
    box.replaceChildren(el("p", { class: "loading", text: t("panel.no_suggestions") }));
    return;
  }
  box.replaceChildren(
    ...state.variants.map((variant, index) =>
      el(
        "div",
        { class: "variant", "data-used": String(index === state.usedVariant) },
        el(
          "div",
          { class: "variant-head" },
          el("strong", { text: variantTitle(variant) }),
          el("span", { class: "source", text: t(`source_hint.${variant.source}`) }),
          el(
            "span",
            { class: "actions" },
            el("button", {
              type: "button",
              text: t("panel.use"),
              title: t("panel.use_title"),
              onclick: () => useVariant(index, "replace"),
            }),
            el("button", {
              type: "button",
              text: t("action.add"),
              title: t("panel.add_title"),
              onclick: () => useVariant(index, "merge"),
            }),
          ),
        ),
        el("ul", { class: "tapes" }, ...variant.tags.map(tape)),
      ),
    ),
  );
}

/** Fill "Tags to apply" of an untagged album from the variants named in tagging.yaml
 * `preselect` (default: the first variant). */
function preselect() {
  const wanted = state.status?.preselect ?? ["first"];
  const picked = [];
  const pick = (index) => {
    if (index >= 0 && !picked.includes(index)) picked.push(index);
  };
  for (const name of wanted) {
    if (name === "first") pick(0);
    else if (name === "llm-all") state.variants.forEach((v, i) => v.source === "llm" && pick(i));
    else pick(state.variants.findIndex((v) => v.source === name)); // llm = the first LLM variant
  }
  state.tags = [];
  for (const index of picked) addUnique(state.tags, state.variants[index].tags);
  state.usedVariant = picked.length === 1 ? picked[0] : -1;
  renderVariants();
  renderChips();
}

function useVariant(index, mode) {
  if (mode === "replace") state.tags = [];
  addUnique(state.tags, state.variants[index].tags);
  state.usedVariant = index;
  renderVariants();
  renderChips();
}

function renderChips() {
  const list = $("#p-chips");
  list.replaceChildren(
    ...state.tags.map((tag, index) =>
      el(
        "li",
        { class: "tape" },
        el("button", { type: "button", class: "text", title: t("panel.edit"), onclick: () => editChip(index) }, tapeLabel(tag)),
        el("button", {
          type: "button",
          class: "remove",
          "aria-label": t("panel.remove_tag", { tag }),
          text: "×",
          onclick: () => {
            state.tags.splice(index, 1);
            renderChips();
          },
        }),
      ),
    ),
  );
  if (!state.tags.length) list.append(el("li", { class: "loading", text: t("panel.no_tags") }));
  syncKeywords();
}

function editChip(index) {
  const input = $("#p-input");
  input.value = state.tags[index];
  state.tags.splice(index, 1);
  renderChips();
  input.focus();
  input.select();
}

// ------------------------------------------------------------------ album keywords
//
// The keyword line is a plain text field (words separated by spaces). Until the user edits
// it, it follows the tags: the last part of each tag ("czas/miesiac/grudzień" -> "grudzień",
// "nowy-rok" -> "nowy rok") plus the base forms of the title words.

function tokens(text) {
  return text.toLowerCase().split(/\s+/).filter(Boolean);
}

function tagLeaves(tags) {
  const leaves = [];
  for (const tag of tags) {
    const leaf = tag.split("/").pop().replace(/[-_]+/g, " ").trim();
    // Long leaves are whole album titles ("album/2015-12-31-...-zielonej"); skip them.
    if (leaf && tokens(leaf).length <= 3) leaves.push(leaf);
  }
  return leaves;
}

function autoKeywordLine() {
  const words = [];
  const add = (phrase) => {
    for (const word of tokens(phrase)) if (!words.includes(word)) words.push(word);
  };
  tagLeaves(state.tags).forEach(add);
  state.keywords.forEach((k) => add(k.base));
  return words.join(" ");
}

function syncKeywords() {
  if (!state.kwTouched) $("#p-kw-line").value = autoKeywordLine();
  renderKeywordSuggestions();
  renderPreviews();
}

function phraseAt(words, phrase) {
  const parts = tokens(phrase);
  for (let i = 0; i + parts.length <= words.length; i++) {
    if (parts.every((w, j) => words[i + j] === w)) return i;
  }
  return -1;
}

function togglePhrase(phrase) {
  const words = tokens($("#p-kw-line").value);
  const at = phraseAt(words, phrase);
  if (at >= 0) words.splice(at, tokens(phrase).length);
  else words.push(...tokens(phrase));
  $("#p-kw-line").value = words.join(" ");
  state.kwTouched = true;
  renderKeywordSuggestions();
  renderPreviews();
}

function toggle(label, on, onChange) {
  return el(
    "li",
    {},
    el("button", { type: "button", class: "toggle", "aria-pressed": String(on), text: label, onclick: onChange }),
  );
}

function renderKeywordSuggestions() {
  const words = tokens($("#p-kw-line").value);
  const options = [];
  for (const k of state.keywords) {
    options.push(k.word.toLowerCase());
    if (k.base !== k.word.toLowerCase()) options.push(k.base);
  }
  options.push(...state.related);
  const unique = [...new Set(options)];
  const list = $("#p-kw-suggestions");
  list.replaceChildren(
    ...unique.map((option) => toggle(option, phraseAt(words, option) >= 0, () => togglePhrase(option))),
  );
  if (!unique.length) list.append(el("li", { class: "loading", text: t("panel.keywords_none") }));
}

function setKeywordSuggestions(keywords, related) {
  state.keywords = keywords;
  state.related = related;
  syncKeywords();
}

function selectedKeywords() {
  return tokens($("#p-kw-line").value);
}

// ------------------------------------------------------------------ previews and apply

function renderPreviews() {
  if (!state.album) return;
  const targets = currentTargets();
  const descriptions = state.status?.descriptions;
  const prefix = descriptions?.photo?.tag_prefix ?? "#";
  const label = descriptions?.album?.label ?? "";
  const keywords = selectedKeywords();

  const photo = $("#p-photo-preview");
  photo.hidden = !targets.photo_description || !state.tags.length;
  photo.textContent = t("panel.photo_preview", { line: state.tags.map((tag) => prefix + tag).join(" ") });

  const album = $("#p-album-preview");
  album.hidden = !targets.album_description || !keywords.length;
  album.textContent = t("panel.album_preview", { line: label + keywords.join(" ") });

  $("#p-keywords-section").classList.toggle("dim", !targets.album_description);

  const apply = $("#p-apply");
  const needsTags = targets.tags || targets.photo_description;
  const ready =
    (targets.tags || targets.photo_description || targets.album_description) &&
    (!needsTags || state.tags.length > 0) &&
    (!targets.album_description || keywords.length > 0);
  apply.disabled = state.busy || !ready;
  const parts = [];
  if (targets.tags) parts.push(plural(state.tags.length, "count.tags"));
  if (targets.photo_description) parts.push(t("target.photo_short"));
  if (targets.album_description) parts.push(t("target.album_short"));
  // Safety catch, only when something will be taken off the photos.
  const removing = tagsToRemove();
  if (removing.length) parts.push(t("apply.and_remove", { n: removing.length }));
  apply.textContent = parts.length ? t("apply.button", { what: parts.join(", ") }) : t("apply.choose");
  const removal = $("#p-removal");
  removal.hidden = !removing.length;
  removal.textContent = removing.length ? t("panel.will_remove", { tags: removing.join(", ") }) : "";
  const remove = $("#p-remove");
  remove.hidden = !state.album.applied;
  remove.disabled = state.busy;
}

function setResult(text, kind = "", target = "#p-result") {
  const result = $(target);
  result.textContent = text;
  result.dataset.state = kind;
}

/** One sentence describing an apply result, in the configured language. */
function applySummary(result, targets) {
  const done = [];
  if (targets.tags || targets.photo_description) {
    done.push(t("apply.photos_updated", { photos: plural(result.own_count, "count.photos") }));
  }
  if (result.album_description_updated) done.push(t("apply.album_updated"));
  if (result.removed_tags?.length) {
    done.push(t("apply.removed", { tags: result.removed_tags.join(", "), photos: plural(result.untagged, "count.photos") }));
  }
  let text = `${result.album_name}: ${done.join(", ") || t("apply.nothing")}.`;
  if (result.complete) text += ` ${t("apply.verified")}`;
  if (result.warnings.length) text += ` ${result.warnings.join(" ")}`;
  return text;
}

/** Tags of `list` not in `other`, compared like the server does (case-insensitive). */
function without(list, other) {
  const skip = new Set(other.map((tag) => tag.toLowerCase()));
  return list.filter((tag) => !skip.has(tag.toLowerCase()));
}

/** Tags the app put on the open album earlier and Apply would take off (sync). */
function tagsToRemove() {
  const applied = state.album?.applied?.tags || [];
  return currentTargets().tags ? without(applied, state.tags) : [];
}

function updateRowAfterApply(albumId, result) {
  const entry = state.albums.find((a) => a.id === albumId);
  if (entry && result.history) {
    // Same as the server's history: earlier tags + new ones - removed ones.
    const previous = entry.applied || { tags: [], keywords: [] };
    const tags = [...previous.tags, ...without(result.history.tags, previous.tags)];
    entry.applied = {
      ...result.history,
      tags: without(tags, result.history.removed || []),
      keywords: result.history.keywords.length ? result.history.keywords : previous.keywords,
    };
  }
  renderAlbums();
  loadLists();
  setTimeout(pollPrefetch, 1500); // the next album starts being prepared
}

async function applyTags() {
  const album = state.album;
  if (!album) return;
  const targets = currentTargets();
  const body = JSON.stringify({ tags: state.tags, keywords: selectedKeywords(), targets });
  const url = `api/albums/${encodeURIComponent(album.id)}/apply`;

  if (asyncMode()) {
    closePanels(true);
    const note = toast(t("toast.applying", { album: album.name }), "pending");
    try {
      const result = await api(url, { method: "POST", body });
      note.replace(applySummary(result, targets), result.complete === false ? "error" : "ok", result.log_id);
      updateRowAfterApply(album.id, result);
    } catch (error) {
      note.replace(t("toast.apply_failed", { album: album.name, error: error.message }), "error");
    }
    return;
  }

  state.busy = true;
  renderPreviews();
  setResult(t("apply.working", { photos: plural(album.asset_count, "count.photos") }));
  try {
    const result = await api(url, { method: "POST", body });
    if (result.tags.length) state.tags = [...result.tags];
    setResult(applySummary(result, targets), result.complete === false ? "warn" : "ok");
    updateRowAfterApply(album.id, result);
  } catch (error) {
    setResult(t("apply.failed", { error: error.message }), "error");
  } finally {
    state.busy = false;
    renderPreviews();
  }
}

async function removeTagging() {
  const album = state.album;
  if (!album?.applied) return;
  const tags = album.applied.tags.join(", ") || "-";
  if (!confirm(t("remove.confirm", { album: album.name, tags }))) return;
  const url = `api/albums/${encodeURIComponent(album.id)}/remove`;
  const done = (result) => {
    const entry = state.albums.find((a) => a.id === album.id);
    if (entry) entry.applied = null;
    renderAlbums();
    setTimeout(pollPrefetch, 1500);
    let text = t("remove.done", {
      album: result.album_name,
      tags: result.removed_tags.join(", ") || "-",
      photos: plural(result.untagged, "count.photos"),
    });
    if (result.warnings.length) text += ` ${result.warnings.join(" ")}`;
    return text;
  };

  if (asyncMode()) {
    closePanels(true);
    const note = toast(t("remove.working", { album: album.name }), "pending");
    try {
      const result = await api(url, { method: "POST", body: "{}" });
      note.replace(done(result), result.complete === false ? "error" : "ok", result.log_id);
    } catch (error) {
      note.replace(t("remove.failed", { album: album.name, error: error.message }), "error");
    }
    return;
  }
  state.busy = true;
  renderPreviews();
  setResult(t("remove.working", { album: album.name }));
  try {
    const result = await api(url, { method: "POST", body: "{}" });
    state.tags = [];
    album.applied = null;
    setResult(done(result), result.complete === false ? "warn" : "ok");
    renderChips();
  } catch (error) {
    setResult(t("remove.failed", { album: album.name, error: error.message }), "error");
  } finally {
    state.busy = false;
    renderPreviews();
  }
}

// ------------------------------------------------------------------ sharing

function shareText(r) {
  const base = t(`share.result.${r.status}`, { album: r.album_name || r.album_id, user: r.user });
  return r.detail ? `${base} (${r.detail})` : base;
}

function toastShareResults(out) {
  for (const r of out.results) {
    const ok = ["shared", "removed", "unchanged"].includes(r.status);
    toast(shareText(r), ok ? "ok" : "error", r.log_id);
  }
}

function countsText(out) {
  return Object.entries(out.counts)
    .map(([status, n]) => t(`share.count.${status}`, { n }))
    .join(", ");
}

async function openShare(album) {
  state.shareAlbum = album;
  $("#s-title").textContent = t("share.title", { album: album.name || t("list.untitled") });
  $("#s-email").value = defaultUsers();
  $("#s-role").value = settings.sharing?.default_role || "viewer";
  setResult("", "", "#s-result");
  renderPeople(album.users);
  showPanel("#share");
  if (defaultUsers()) $("#s-add .primary").focus();
  try {
    renderPeople(await api(`api/albums/${encodeURIComponent(album.id)}/users`));
  } catch (error) {
    setResult(t("share.load_failed", { error: error.message }), "error", "#s-result");
  }
}

function renderPeople(users) {
  const album = state.shareAlbum;
  if (album) {
    album.users = users;
    renderAlbums();
  }
  $("#s-users").replaceChildren(
    ...users.map((u) =>
      el(
        "li",
        {},
        el("div", {}, el("strong", { text: u.name || u.email }), el("span", { class: "meta", text: u.email })),
        el("span", { class: "role", text: t(`role.${u.role}`) }),
        u.role === "owner"
          ? null
          : el("button", { type: "button", class: "quiet", text: t("share.remove"), onclick: () => unshare(u) }),
      ),
    ),
  );
}

async function share(event) {
  event.preventDefault();
  const album = state.shareAlbum;
  const users = splitUsers($("#s-email").value);
  if (!album || !users.length) return;
  const body = JSON.stringify({ users, role: $("#s-role").value });
  const url = `api/albums/${encodeURIComponent(album.id)}/users`;

  if (asyncMode()) {
    closePanels(true);
    try {
      const out = await api(url, { method: "POST", body });
      toastShareResults(out);
      const entry = state.albums.find((a) => a.id === album.id);
      if (entry) entry.users = out.album_users;
      renderAlbums();
    } catch (error) {
      toast(t("toast.share_failed", { album: album.name, error: error.message }), "error");
    }
    return;
  }

  setResult(t("share.working"), "", "#s-result");
  try {
    const out = await api(url, { method: "POST", body });
    renderPeople(out.album_users);
    const failed = out.results.some((r) => !["shared", "unchanged"].includes(r.status));
    setResult(out.results.map(shareText).join(" "), failed ? "error" : "ok", "#s-result");
  } catch (error) {
    setResult(t("share.failed", { error: error.message }), "error", "#s-result");
  }
}

async function unshare(user) {
  const album = state.shareAlbum;
  if (!album || !confirm(t("share.confirm_remove", { user: user.email }))) return;
  try {
    renderPeople(
      await api(`api/albums/${encodeURIComponent(album.id)}/users/${encodeURIComponent(user.id)}`, {
        method: "DELETE",
      }),
    );
    const text = t("share.result.removed", { album: album.name, user: user.email });
    if (asyncMode()) toast(text, "ok");
    else setResult(text, "ok", "#s-result");
  } catch (error) {
    const text = t("share.remove_failed", { error: error.message });
    if (asyncMode()) toast(text, "error");
    else setResult(text, "error", "#s-result");
  }
}

// ------------------------------------------------------------------ bulk sharing

function renderBulk() {
  $("#bulk").hidden = !state.selecting;
  $("#select-mode").setAttribute("aria-pressed", String(state.selecting));
  $("#select-mode").textContent = t(state.selecting ? "bulk.done" : "bulk.start");
  const n = state.selected.size;
  $("#bulk-count").textContent = t("bulk.count", { albums: plural(n, "count.albums") });
  $("#bulk-share").disabled = n === 0;
  $("#bulk-remove").disabled = n === 0;
  document.body.classList.toggle("with-bulk", state.selecting);
}

async function bulkShare(remove) {
  const users = splitUsers($("#bulk-user").value);
  const ids = [...state.selected];
  if (!users.length || !ids.length) {
    setResult(t("share.need_user"), "error", "#bulk-result");
    return;
  }
  if (remove && !confirm(t("bulk.confirm_remove", { users: users.join(", "), albums: plural(ids.length, "count.albums") }))) {
    return;
  }
  const body = JSON.stringify({ album_ids: ids, users, role: $("#bulk-role").value, remove });
  if (asyncMode()) {
    setResult("", "", "#bulk-result");
    toast(t(remove ? "bulk.removing" : "bulk.sharing", { albums: plural(ids.length, "count.albums") }), "ok");
  } else {
    setResult(t(remove ? "bulk.removing" : "bulk.sharing", { albums: plural(ids.length, "count.albums") }), "", "#bulk-result");
  }
  try {
    const out = await api("api/sharing/bulk", { method: "POST", body });
    if (asyncMode()) toastShareResults(out);
    else {
      const problems = out.results
        .filter((r) => ["error", "skipped"].includes(r.status))
        .map(shareText);
      setResult(
        `${countsText(out)}.${problems.length ? " " + problems.join(" ") : ""}`,
        out.counts.error || out.counts.skipped ? "warn" : "ok",
        "#bulk-result",
      );
    }
    await loadAlbums();
  } catch (error) {
    const text = t("share.failed", { error: error.message });
    if (asyncMode()) toast(text, "error");
    else setResult(text, "error", "#bulk-result");
  }
}

// ------------------------------------------------------------------ wiring

$("#q").addEventListener("input", (event) => {
  state.query = event.target.value;
  renderAlbums();
  sendFocus();
});

for (const button of document.querySelectorAll(".seg button")) {
  button.addEventListener("click", () => {
    state.filter = button.dataset.filter;
    for (const b of document.querySelectorAll(".seg button")) {
      b.setAttribute("aria-pressed", String(b === button));
    }
    renderAlbums();
    sendFocus();
  });
}

for (const box of document.querySelectorAll(".targets input")) {
  box.addEventListener("change", () => {
    saveTargets();
    renderPreviews();
  });
}

$("#reload").addEventListener("click", loadAlbums);
for (const button of document.querySelectorAll("[data-close]")) button.addEventListener("click", () => closePanels());
$("#scrim").addEventListener("click", () => closePanels());
$("#p-regen").addEventListener("click", () => loadSuggestions(true));
$("#p-apply").addEventListener("click", applyTags);
$("#p-remove").addEventListener("click", removeTagging);
$("#p-clear").addEventListener("click", () => {
  state.tags = [];
  state.usedVariant = -1;
  renderVariants();
  renderChips();
});

$("#p-add").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = $("#p-input");
  addUnique(state.tags, input.value.split(","));
  input.value = "";
  renderChips();
});

$("#p-kw-line").addEventListener("input", () => {
  state.kwTouched = true;
  renderKeywordSuggestions();
  renderPreviews();
});

$("#p-kw-reset").addEventListener("click", () => {
  state.kwTouched = false;
  syncKeywords();
});

$("#s-add").addEventListener("submit", share);

$("#select-mode").addEventListener("click", () => {
  state.selecting = !state.selecting;
  if (!state.selecting) state.selected.clear();
  setResult("", "", "#bulk-result");
  renderBulk();
  renderAlbums();
});
$("#bulk-all").addEventListener("click", () => {
  for (const album of visibleAlbums()) if (isMine(album)) state.selected.add(album.id);
  renderBulk();
  renderAlbums();
});
$("#bulk-none").addEventListener("click", () => {
  state.selected.clear();
  renderBulk();
  renderAlbums();
});
$("#bulk").addEventListener("submit", (event) => {
  event.preventDefault();
  bulkShare(false);
});
$("#bulk-remove").addEventListener("click", () => bulkShare(true));

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closePanels();
});

async function start() {
  await initI18n();
  initChrome();
  $("#bulk-user").value = defaultUsers();
  $("#bulk-role").value = settings.sharing?.default_role || "viewer";
  renderBulk();
  await loadStatus();
  await loadAlbums();
  sendFocus();
  pollPrefetch();
  loadLists();
  setInterval(pollPrefetch, 8000);
}

start();
