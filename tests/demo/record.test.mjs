import { test } from "node:test";
import assert from "node:assert/strict";
import {
  rewriteHtml,
  rewriteScript,
  redirectSkip,
  fixtureKey,
  pageFile,
} from "../../demo/record.mjs";

const OPTS = {
  page: "/",
  base: "/AniDex/",
  pages: ["/", "/queue", "/settings", "/anime/900001/notes"],
  missing: ["/auth/", "/settings/link/", "/admin/export-all", "/admin/backups/"],
  ignoreQuery: ["back"],
  fixtures: { "api/sync/status": { status: 200, body: { running: false } } },
};

const PAGE = `<!DOCTYPE html><html><head>
<link rel="icon" href="/static/favicon.svg">
<link rel="manifest" href="/static/manifest.json">
<link rel="stylesheet" href="/static/x.css">
<script src="/static/script.js?v=33" defer></script>
</head><body>
<a href="/queue">Queue</a>
<a href="/">Home</a>
<a href="/settings/link/plex">Link Plex</a>
<a href="/admin/backups/3/download">Backup</a>
<a href="/upcoming">Not recorded</a>
<a href="/?status=WATCHING">Filter</a>
<a href="/anime/900001/notes?back=WATCHING">Notes</a>
<a href="https://example.invalid/x">Outside</a>
<a href="#top">Top</a>
<img src="/static/demo-covers/900001.svg">
<form method="post" action="/settings/display"><button>Save</button></form>
<script>
if ('serviceWorker' in navigator) { navigator.serviceWorker.register('/service-worker.js'); }
location.href = '/?' + params.toString();
</script>
</body></html>`;

test("rewrites root-absolute /static/ URLs relative to the page", () => {
  const out = rewriteHtml(PAGE, OPTS);
  assert.match(out, /href="static\/x\.css"/);
  assert.match(out, /src="static\/script\.js\?v=33"/);
  assert.match(out, /src="static\/demo-covers\/900001\.svg"/);
  assert.doesNotMatch(out, /"\/static\//);

  const nested = rewriteHtml(PAGE, { ...OPTS, page: "/anime/900001/notes" });
  assert.match(nested, /href="\.\.\/\.\.\/\.\.\/static\/x\.css"/);
});

test("404 page uses base-absolute URLs (served at any depth)", () => {
  const out = rewriteHtml(PAGE, { ...OPTS, prefix: "/AniDex/" });
  assert.match(out, /href="\/AniDex\/static\/x\.css"/);
  assert.match(out, /href="\/AniDex\/queue\/"/);
});

test("links to recorded pages become relative directory links", () => {
  const out = rewriteHtml(PAGE, OPTS);
  assert.match(out, /<a href="queue\/">Queue/);
  assert.match(out, /<a href="\.\/">Home/);
  assert.match(out, /<a href="anime\/900001\/notes\/\?back=WATCHING">Notes/);
  assert.match(out, /<a href="https:\/\/example\.invalid\/x">Outside/);
  assert.match(out, /<a href="#top">Top/);
});

test("links outside the demo are marked data-demo-missing (Review Focus 3)", () => {
  const out = rewriteHtml(PAGE, OPTS);
  const missing = (label) => new RegExp(`<a [^>]*data-demo-missing[^>]*>${label}<`);
  assert.match(out, missing("Link Plex"));
  assert.match(out, missing("Backup"));
  assert.match(out, missing("Not recorded"));
  assert.match(out, missing("Filter"));
  assert.doesNotMatch(out, /data-demo-missing[^>]*>Queue</);
  // Marked links still point at the not-in-demo page without JS.
  assert.match(out, /<a href="not-in-demo\.html" data-demo-missing[^>]*>Link Plex</);
});

test("removes manifest and service-worker registration", () => {
  const out = rewriteHtml(PAGE, OPTS);
  assert.doesNotMatch(out, /rel="manifest"/);
  assert.doesNotMatch(out, /serviceWorker\.register/);
});

test("injects ANIDEX_DEMO before the shim, both before the app script", () => {
  const out = rewriteHtml(PAGE, OPTS);
  const cfg = out.indexOf("window.ANIDEX_DEMO");
  const shim = out.indexOf('src="demo-shim.js"');
  const app = out.indexOf("static/script.js");
  assert.ok(cfg > 0 && shim > cfg && app > shim, { cfg, shim, app });
  const m = out.match(/window\.ANIDEX_DEMO = (.*?);<\/script>/s);
  const parsed = JSON.parse(m[1]);
  assert.equal(parsed.base, "/AniDex/");
  assert.deepEqual(parsed.fixtures["api/sync/status"].body, { running: false });
  assert.equal(parsed.now, undefined);
  const pinned = JSON.parse(rewriteHtml(PAGE, { ...OPTS, now: "2026-10-15T12:00:00Z" }).match(/window\.ANIDEX_DEMO = (.*?);<\/script>/s)[1]);
  assert.equal(pinned.now, "2026-10-15T12:00:00Z");
});

test("forms go to the not-in-demo page", () => {
  const out = rewriteHtml(PAGE, OPTS);
  assert.match(out, /<form method="get" action="not-in-demo\.html"/);
});

test("script-driven navigation is pinned under the base", () => {
  const out = rewriteHtml(PAGE, OPTS);
  assert.match(out, /location\.href = window\.ANIDEX_DEMO\.base \+ '\?'/);
  const js = rewriteScript(
    "if ('serviceWorker' in navigator) {\n  window.addEventListener('load', () => {\n    navigator.serviceWorker.register('/service-worker.js').catch(err => {});\n  });\n}\n" +
      "location.href = `/anime/${id}/notes`;\n",
  );
  assert.doesNotMatch(js, /serviceWorker\.register/);
  assert.match(js, /location\.href = window\.ANIDEX_DEMO\.base \+ `anime\/\$\{id\}\/notes`/);
});

test("a redirected page is skipped, never saved as the target (Review Focus 2)", () => {
  assert.equal(redirectSkip("/settings", "http://app:8888/settings"), null);
  assert.equal(redirectSkip("/queue", "http://app:8888/queue?x=1"), null);
  assert.equal(
    redirectSkip("/settings/2fa/setup", "http://app:8888/settings"),
    "SKIP /settings/2fa/setup redirected to /settings",
  );
  assert.equal(
    redirectSkip("/admin", "http://app:8888/auth/login"),
    "SKIP /admin redirected to /auth/login",
  );
});

test("fixture keys are path without leading slash plus query verbatim", () => {
  assert.equal(fixtureKey("http://app:8888/api/library?status=WATCHING"), "api/library?status=WATCHING");
  assert.equal(fixtureKey("http://app:8888/api/stats"), "api/stats");
  assert.equal(pageFile("/"), "index.html");
  assert.equal(pageFile("/stats/wrapped"), "stats/wrapped/index.html");
});

test("third-party scripts are served from the demo; listed external links are missing", () => {
  const html =
    '<head></head><script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>' +
    '<a href="https://anilist.co/anime/900001" target="_blank">AniList</a>' +
    '<a href="https://example.invalid/ok">Other</a>';
  const opts = {
    ...OPTS,
    page: "/stats",
    missing: [...OPTS.missing, "https://anilist.co/"],
    vendor: { "https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js": "static/vendor/chart.umd.min.js" },
  };
  const out = rewriteHtml(html, opts);
  assert.match(out, /<script src="\.\.\/static\/vendor\/chart\.umd\.min\.js">/);
  assert.doesNotMatch(out, /cdn\.jsdelivr\.net/);
  assert.match(out, /<a href="\.\.\/not-in-demo\.html" data-demo-missing[^>]*>AniList</);
  assert.match(out, /<a href="https:\/\/example\.invalid\/ok">Other</);
});

test("charset meta stays inside the 1024-byte prescan window", () => {
  const big = { "api/stats": { status: 200, body: { pad: "x".repeat(40000) } } };
  const withCharset = '<!DOCTYPE html>\n<html lang="en">\n<head>\n  <meta charset="UTF-8">\n  <title>AniDex</title>\n</head><body>é</body></html>';
  const out = rewriteHtml(withCharset, { ...OPTS, fixtures: big });
  const at = out.search(/<meta charset=/i);
  assert.ok(at >= 0 && Buffer.byteLength(out.slice(0, at)) < 1024, `charset at ${at}`);
  assert.ok(out.indexOf("window.ANIDEX_DEMO") > at);
  assert.equal(out.match(/<meta charset=/gi).length, 1);

  const without = rewriteHtml("<html><head><title>x</title></head><body></body></html>", { ...OPTS, fixtures: big });
  const at2 = without.search(/<meta charset="utf-8">/);
  assert.ok(at2 >= 0 && at2 < without.indexOf("window.ANIDEX_DEMO") && at2 < 1024);
});
