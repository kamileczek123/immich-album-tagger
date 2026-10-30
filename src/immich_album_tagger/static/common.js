// Shared by all pages: translations, API calls, notifications.

export const $ = (selector) => document.querySelector(selector);

let messages = {};
export let settings = { language: "english", ui: { async_actions: true, toast_ms: 5000 } };

/** Translate a key; {name} placeholders are filled from params. Unknown keys show as-is. */
export function t(key, params = {}) {
  const template = messages[key] ?? key;
  return template.replace(/\{(\w+)\}/g, (match, name) => (name in params ? String(params[name]) : match));
}

let pluralRules = new Intl.PluralRules("en");

/** Count with the right plural form: keys "<base>.one", ".few", ".many", ".other". */
export function plural(n, base) {
  const form = pluralRules.select(n);
  const key = `${base}.${form}` in messages ? `${base}.${form}` : `${base}.other`;
  return t(key, { n: n.toLocaleString(document.documentElement.lang) });
}

/** Load translations and public settings, then translate the static page. */
export async function initI18n() {
  const response = await fetch("api/i18n");
  const data = await response.json();
  messages = data.messages;
  settings = data.settings;
  document.documentElement.lang = data.lang_code || "en";
  pluralRules = new Intl.PluralRules(document.documentElement.lang);
  translatePage();
}

export function translatePage(root = document) {
  for (const node of root.querySelectorAll("[data-i18n]")) node.textContent = t(node.dataset.i18n);
  for (const attr of ["placeholder", "title", "aria-label"]) {
    const key = `i18n${attr === "aria-label" ? "Aria" : attr[0].toUpperCase() + attr.slice(1)}`;
    for (const node of root.querySelectorAll(`[data-i18n-${attr === "aria-label" ? "aria" : attr}]`)) {
      node.setAttribute(attr, t(node.dataset[key]));
    }
  }
  const title = document.querySelector("title[data-i18n]");
  if (title) document.title = t(title.dataset.i18n);
}

/**
 * All URLs are relative ("api/albums", "logs"), so the app works under any path prefix,
 * e.g. https://immich.example.com/IAT/ behind a reverse proxy, as well as at the root.
 * Pages: "" (albums), "logs", "login".
 */
export function currentPage() {
  return location.pathname.split("/").pop();
}

export async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (response.status === 401 && !path.startsWith("api/login")) {
    const lost = body.lost ? "&lost=1" : "";
    location.href = `login?next=${encodeURIComponent(currentPage())}${lost}`;
    throw new Error(t("error.session"));
  }
  if (!response.ok) {
    const detail = Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join("; ") : body.detail;
    throw new Error(detail || `${response.status} ${response.statusText}`);
  }
  return body;
}

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  node.append(...children.filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

// ------------------------------------------------------------------ notifications

function toastBox() {
  let box = document.getElementById("toasts");
  if (!box) {
    box = el("div", { id: "toasts", class: "toasts", "aria-live": "polite" });
    document.body.append(box);
  }
  return box;
}

/**
 * Show a notification in the top left corner (panels open on the right). kind: "ok" | "error" | "pending".
 * Notifications stack top to bottom and leave in the order they came (FIFO).
 * Returns a handle with replace(text, kind, logId) for pending notifications.
 */
export function toast(text, kind = "ok", logId = null) {
  const node = el("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status" });
  let timer = null;

  function fill(message, type, id) {
    node.className = `toast ${type}`;
    node.replaceChildren(el("span", { text: message }));
    if (type === "error") {
      node.append(
        " ",
        el("a", { href: id ? `logs#${id}` : "logs", class: "toast-link", text: t("toast.details") }),
      );
    }
    clearTimeout(timer);
    if (type !== "pending") {
      timer = setTimeout(() => {
        node.classList.add("leaving");
        setTimeout(() => node.remove(), 250);
      }, settings.ui.toast_ms);
    }
  }

  fill(text, kind, logId);
  const box = toastBox();
  box.append(node);
  // A large batch would cover the screen: beyond this many, the oldest leave first (FIFO).
  const MAX_VISIBLE = 8;
  const visible = [...box.children].filter((n) => !n.classList.contains("leaving"));
  for (const old of visible.slice(0, Math.max(0, visible.length - MAX_VISIBLE))) {
    if (old.classList.contains("pending")) continue;
    old.classList.add("leaving");
    setTimeout(() => old.remove(), 250);
  }
  return { replace: (message, type, id = null) => fill(message, type, id) };
}

// ------------------------------------------------------------------ page chrome

/**
 * Address of Immich's web UI for links, e.g. albums/<id>; null if unknown.
 * ui.immich_public_url when set; otherwise, when the app runs under a path of Immich's
 * host name (https://immich.example.com/IAT/), Immich is the domain root ("../").
 */
export function immichUrl(path = "") {
  const configured = (settings.ui?.immich_public_url || "").trim();
  if (configured) return new URL(path, configured.replace(/\/?$/, "/")).href;
  if (new URL("./", location.href).pathname === "/") return null;
  return new URL(path, new URL("/", location.href)).href;
}

const ICONS = {
  share:
    '<circle cx="18" cy="5" r="2.5"/><circle cx="6" cy="12" r="2.5"/><circle cx="18" cy="19" r="2.5"/><path d="m8.2 10.8 7.6-4.4M8.2 13.2l7.6 4.4"/>',
  tag: '<path d="M3 4.5V11l9.5 9.5a1.5 1.5 0 0 0 2.1 0l5.9-5.9a1.5 1.5 0 0 0 0-2.1L11 3H4.5A1.5 1.5 0 0 0 3 4.5z"/><circle cx="7.5" cy="7.5" r="1.3"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
};

/** An inline SVG icon from the fixed set above (never built from data). */
export function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = ICONS[name];
  return svg;
}

/** Header and sidebar shared by the albums and logs pages: theme, account menu, links. */
export async function initChrome() {
  $("#theme").addEventListener("click", () => window.iatTheme.toggle());

  const button = $("#account");
  const menu = $("#account-menu");
  const setOpen = (open) => {
    menu.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
  };
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    setOpen(menu.hidden);
  });
  document.addEventListener("click", (event) => {
    if (!menu.hidden && !menu.contains(event.target)) setOpen(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setOpen(false);
  });
  $("#logout").addEventListener("click", async () => {
    await fetch("api/logout", { method: "POST" });
    location.href = "login";
  });

  const immich = immichUrl();
  $("#to-immich").hidden = !immich;
  if (immich) $("#to-immich").href = immich;

  try {
    const session = await api("api/session");
    $("#version").textContent = session.version;
    const user = session.auth.user;
    $("#logout").hidden = !session.auth.enabled;
    $("#who").textContent = user ? t("nav.user", { user }) : "";
    $("#who").hidden = !user;
    button.textContent = (user || "?").trim().charAt(0).toUpperCase() || "?";
    button.title = user || "";
  } catch {
    /* the pages work without it */
  }
}
