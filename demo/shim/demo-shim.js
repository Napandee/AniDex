/* AniDex demo shim: replays recorded /api/* responses; never calls the real
 * fetch for /api/*. Classic script. Expects window.ANIDEX_DEMO =
 * { base: "/AniDex/", fixtures: { "api/x?q=1": { status, body } } }. */
(function () {
  "use strict";
  var win = typeof window !== "undefined" ? window : globalThis;
  var cfg = win.ANIDEX_DEMO || {};
  var base = cfg.base || "/";
  if (base.charAt(base.length - 1) !== "/") base += "/"; // "/AniDex" must not prefix-match "/AniDexfoo/"
  var fixtures = cfg.fixtures || {};
  var BANNER_ID = "anidex-demo-banner";
  var BANNER_TEXT = "Demo — made-up data, nothing is saved";

  function json(status, body) {
    return new Response(JSON.stringify(body), {
      status: status,
      headers: { "content-type": "application/json" },
    });
  }

  // Key = path relative to base, plus query string verbatim.
  function keyFor(rawUrl) {
    // Bare relative URLs ("api/x") resolve against base ON PURPOSE (unlike browsers): keeps /api calls fail-closed.
    var rel = !/^([a-z][a-z0-9+.-]*:|\/)/i.test(rawUrl);
    var u = new URL(rawUrl, rel ? new URL(base, win.location.href).href : win.location.href);
    var p = u.pathname;
    if (p.indexOf(base) === 0) p = p.slice(base.length);
    else p = p.replace(/^\//, "");
    return p + u.search;
  }

  function install() {
    if (win.__anidexDemoShim) return;
    win.__anidexDemoShim = true;
    var realFetch = win.fetch;

    win.fetch = function (input, init) {
      var rawUrl = typeof input === "string" ? input : input && input.url !== undefined ? input.url : String(input);
      var method = ((init && init.method) || (input && input.method) || "GET").toUpperCase();
      var key = keyFor(rawUrl);
      if (key.indexOf("api/") !== 0) return realFetch.apply(win, arguments);
      if (method !== "GET") return Promise.resolve(json(200, { ok: true, detail: "Demo: nothing is saved" }));
      var hit = Object.prototype.hasOwnProperty.call(fixtures, key) ? fixtures[key] : null;
      if (hit) return Promise.resolve(json(hit.status, hit.body));
      return Promise.resolve(json(404, { detail: "Not available in the demo" }));
    };

    var doc = win.document;
    if (!doc) return;

    doc.addEventListener("click", function (ev) {
      var t = ev.target;
      var a = t && t.closest ? t.closest("a[data-demo-missing]") : null;
      if (!a) return;
      ev.preventDefault();
      win.location.assign(base + "not-in-demo.html");
    }, true);

    function banner() {
      if (doc.getElementById(BANNER_ID)) return;
      var b = doc.createElement("div");
      b.id = BANNER_ID;
      b.setAttribute && b.setAttribute("role", "status");
      b.textContent = BANNER_TEXT;
      b.style.cssText = "position:sticky;top:0;z-index:2147483647;background:#fde68a;" +
        "color:#1f2937;text-align:center;font:600 14px/1.4 system-ui,sans-serif;padding:6px 12px";
      if (doc.body.insertBefore && doc.body.firstChild !== undefined) doc.body.insertBefore(b, doc.body.firstChild);
      else doc.body.insertBefore(b);
    }
    if (doc.body) banner();
    else doc.addEventListener("DOMContentLoaded", banner);
  }

  install();
})();
