// Light / dark theme, shared with Immich. Loaded as a classic script in <head>, so the page
// never flashes in the wrong theme. Behind a proxy on Immich's own domain (e.g.
// https://immich.example.com/IAT/) the app shares Immich's localStorage, so it reads and writes
// the same key Immich uses ("immich-ui-theme": "light" | "dark" | "system", JSON-encoded) and
// both always look alike. Elsewhere it follows the system setting until toggled.
(function () {
  const KEY = "immich-ui-theme";
  const media = window.matchMedia("(prefers-color-scheme: dark)");

  function preference() {
    let value = null;
    try {
      value = JSON.parse(localStorage.getItem(KEY));
    } catch {
      value = localStorage.getItem(KEY);
    }
    if (value && typeof value === "object") value = value.system ? "system" : value.value; // old format
    return value === "light" || value === "dark" ? value : "system";
  }

  function apply() {
    const choice = preference();
    const dark = choice === "dark" || (choice === "system" && media.matches);
    document.documentElement.classList.toggle("dark", dark);
  }

  apply();
  media.addEventListener("change", apply);
  window.addEventListener("storage", (event) => {
    if (event.key === KEY) apply(); // changed in an Immich tab
  });

  window.iatTheme = {
    isDark: () => document.documentElement.classList.contains("dark"),
    toggle() {
      try {
        localStorage.setItem(KEY, JSON.stringify(this.isDark() ? "light" : "dark"));
      } catch {
        /* private mode: toggle for this page only */
        document.documentElement.classList.toggle("dark");
        return;
      }
      apply();
    },
  };
})();
