// Operation log page.
import { $, api, el, initChrome, initI18n, t } from "./common.js";

let entries = [];
let filter = "all";
let person = "";

function when(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString(document.documentElement.lang);
}

function detailLines(entry) {
  const d = entry.details || {};
  const lines = [];
  if (d.tags?.length) lines.push([t("logs.field.tags"), d.tags.join(", ")]);
  if (d.keywords?.length) lines.push([t("logs.field.keywords"), d.keywords.join(" ")]);
  if (d.removed?.length) lines.push([t("logs.field.removed"), d.removed.join(", ")]);
  if (d.targets) {
    const on = Object.entries(d.targets).filter(([, v]) => v).map(([k]) => t(`logs.target.${k}`));
    lines.push([t("logs.field.targets"), on.join(", ") || "-"]);
  }
  if (d.photos !== undefined) lines.push([t("logs.field.photos"), `${d.photos} / ${d.album_photos}`]);
  if (d.user) lines.push([t("logs.field.user"), d.user]);
  if (d.users?.length) lines.push([t("logs.field.user"), d.users.join(", ")]);
  if (d.role) lines.push([t("logs.field.role"), t(`role.${d.role}`)]);
  if (d.result) lines.push([t("logs.field.result"), t(`logs.result.${d.result}`)]);
  if (entry.by) lines.push([t("logs.field.by"), entry.by]);
  return lines;
}

function render() {
  const shown = entries.filter(
    (e) => (filter === "all" || e.status === filter) && (!person || (e.by || "") === person),
  );
  const errors = entries.filter((e) => e.status === "error").length;
  $("#count").textContent = t("logs.count", { shown: shown.length, total: entries.length, errors });
  $("#empty").hidden = shown.length > 0;
  $("#entries").replaceChildren(
    ...shown.map((entry) =>
      el(
        "li",
        { id: entry.id, class: `entry ${entry.status}` },
        el(
          "div",
          { class: "entry-head" },
          el("span", { class: `badge ${entry.status}`, text: t(`logs.status.${entry.status}`) }),
          el("strong", { text: t(`logs.action.${entry.action}`) }),
          entry.album_name ? el("span", { class: "entry-album", text: entry.album_name }) : null,
          el("time", { datetime: entry.time, text: when(entry.time) }),
        ),
        el(
          "dl",
          {},
          ...detailLines(entry).flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]),
        ),
        entry.message ? el("p", { class: "entry-message", text: entry.message }) : null,
      ),
    ),
  );
  if (location.hash) {
    const target = document.getElementById(location.hash.slice(1));
    if (target) {
      target.classList.add("focus");
      target.scrollIntoView({ block: "center" });
    }
  }
}

function fillPeople() {
  // Filter by who did it; shown when the log has entries of more than one person.
  const people = [...new Set(entries.map((e) => e.by || ""))].filter(Boolean).sort();
  const select = $("#person");
  if (person && !people.includes(person)) people.push(person);
  select.replaceChildren(
    el("option", { value: "", text: t("logs.everyone") }),
    ...people.map((p) => el("option", { value: p, text: p })),
  );
  select.value = person;
  $("#person-box").hidden = people.length < 2 && !person;
}

async function load() {
  entries = await api("api/log");
  fillPeople();
  render();
}

for (const button of document.querySelectorAll(".seg button")) {
  button.addEventListener("click", () => {
    filter = button.dataset.filter;
    for (const b of document.querySelectorAll(".seg button")) b.setAttribute("aria-pressed", String(b === button));
    render();
  });
}
$("#person").addEventListener("change", () => {
  person = $("#person").value;
  render();
});
$("#reload").addEventListener("click", load);
$("#clear").addEventListener("click", async () => {
  if (!confirm(t("logs.confirm_clear"))) return;
  await api("api/log", { method: "DELETE" });
  await load();
});

await initI18n();
initChrome();
await load();
