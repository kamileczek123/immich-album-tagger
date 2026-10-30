// Login page.
import { $, initI18n, settings, t } from "./common.js";

await initI18n();

if (settings.auth_mode === "immich" || settings.auth_mode === "immich_keys") {
  // Immich accounts log in with their e-mail.
  $('label[for="username"]').textContent = t("login.email");
  $("#username").type = "email";
  $("#username").autocomplete = "email";
  $("#hint").textContent = t(settings.auth_mode === "immich_keys" ? "login.hint_immich_keys" : "login.hint_immich");
  $("#hint").hidden = false;
}
if (new URLSearchParams(location.search).get("lost")) {
  // The cookie was still valid but the app no longer had the session (restart, expiry).
  $("#error").textContent = t("error.session_lost");
}

$("#login").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = $("#error");
  error.textContent = "";
  error.dataset.state = "";
  const response = await fetch("api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: $("#username").value, password: $("#password").value }),
  });
  if (response.ok) {
    // Back to the page that asked for the login; only pages of this app.
    const next = new URLSearchParams(location.search).get("next");
    location.href = next === "logs" ? "logs" : "./";
    return;
  }
  const body = await response.json().catch(() => ({}));
  error.textContent = body.detail || t("error.login");
  error.dataset.state = "error";
  $("#password").select();
});
