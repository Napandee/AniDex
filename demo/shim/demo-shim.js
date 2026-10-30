/* AniDex demo shim: replays recorded /api/* responses; never calls the real
 * fetch for /api/*. Classic script. Expects window.ANIDEX_DEMO =
 * { base: "/AniDex/", fixtures: { "api/x?q=1": { status, body } }, now: "<ISO>" }.
 * With `now`, the page's clock starts at the date the demo was recorded at, so
 * dates the page computes (and the /api queries built from them) match the data. */
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

  // Date, but starting at <now> and ticking from there.
  function pinClock(now) {
    var RealDate = win.Date;
    var offset = RealDate.parse(now) - RealDate.now();
    if (isNaN(offset)) return;
    function DemoDate() {
      var a = arguments;
      if (!(this instanceof DemoDate)) return new RealDate(RealDate.now() + offset).toString();
      if (!a.length) return new RealDate(RealDate.now() + offset);
      return new (Function.prototype.bind.apply(RealDate, [null].concat(Array.prototype.slice.call(a))))();
    }
    DemoDate.prototype = RealDate.prototype;
    DemoDate.now = function () { return RealDate.now() + offset; };
    DemoDate.parse = RealDate.parse;
    DemoDate.UTC = RealDate.UTC;
    win.Date = DemoDate;
  }

  function install() {
    if (win.__anidexDemoShim) return;
    win.__anidexDemoShim = true;
    if (cfg.now) pinClock(cfg.now);
    var realFetch = win.fetch;

    // A live response always arrives after the deferred app scripts have run; an
    // instant answer to a fetch made while the page is still parsing would beat
    // them (e.g. stats.html calling script.js's t() before it exists).
    function answer(status, body) {
      var d = win.document;
      if (!d || d.readyState !== "loading") return Promise.resolve(json(status, body));
      return new Promise(function (resolve) {
        d.addEventListener("DOMContentLoaded", function () { resolve(json(status, body)); });
      });
    }

    win.fetch = function (input, init) {
      var rawUrl = typeof input === "string" ? input : input && input.url !== undefined ? input.url : String(input);
      var method = ((init && init.method) || (input && input.method) || "GET").toUpperCase();
      var key = keyFor(rawUrl);
      if (key.indexOf("api/") !== 0) return realFetch.apply(win, arguments);
      if (method !== "GET") return answer(200, { ok: true, detail: "Demo: nothing is saved" });
      var hit = Object.prototype.hasOwnProperty.call(fixtures, key) ? fixtures[key] : null;
      if (hit) return answer(hit.status, hit.body);
      return answer(404, { detail: "Not available in the demo" });
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
      // In normal flow at the top of the page: it pushes the page down and covers
      // nothing. Elements the page fixes at the top are moved below it (makeRoom).
      b.style.cssText = "background:#fde68a;color:#1f2937;text-align:center;" +
        "font:600 14px/1.4 system-ui,sans-serif;padding:6px 12px";
      if (doc.body.insertBefore && doc.body.firstChild !== undefined) doc.body.insertBefore(b, doc.body.firstChild);
      else doc.body.insertBefore(b);
      makeRoom(b);
    }

    // Fixed elements anchored at the top of the screen (a header's icon buttons)
    // would sit over the banner: shift them down by the part of the banner still
    // on screen. Full-height fixed views (dialogs, full-screen pages) are left as
    // they are; they cover the banner instead.
    function makeRoom(b) {
      if (!win.getComputedStyle || !doc.body.getElementsByTagName || !win.addEventListener) return;
      var moved = [];
      function update() {
        var off = Math.max(0, (b.offsetHeight || 0) - (win.scrollY || 0));
        for (var i = 0; i < moved.length; i++) moved[i].style.translate = off ? "0 " + off + "px" : "";
      }
      function measure() {
        for (var i = 0; i < moved.length; i++) moved[i].style.translate = "";
        moved = [];
        var h = b.offsetHeight || 0, vh = win.innerHeight || 0;
        var all = doc.body.getElementsByTagName("*");
        for (var j = 0; j < all.length; j++) {
          var e = all[j];
          if (e === b || win.getComputedStyle(e).position !== "fixed") continue;
          var r = e.getBoundingClientRect();
          if (r.height > 0 && r.top < h && r.height < vh / 2) moved.push(e);
        }
        update();
      }
      if (doc.readyState === "complete") measure();
      else win.addEventListener("load", measure);
      win.addEventListener("resize", measure);
      win.addEventListener("scroll", update, { passive: true });
    }

    if (doc.body) banner();
    else doc.addEventListener("DOMContentLoaded", banner);
  }

  install();
})();
