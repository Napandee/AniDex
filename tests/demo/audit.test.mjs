// Pages-like server, site check and layout audit. Needs a browser: run in the
// Playwright image (mcr.microsoft.com/playwright:v1.64.1-noble) with demo/node_modules
// installed, e.g. demo/run.sh test.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { startServer } from "../../demo/serve.mjs";
import { auditPage, compareBaseline, sortFindings, launch } from "../../demo/audit.mjs";
import { checkPage } from "../../demo/check.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(here, "fixtures");
const SHIM = path.join(here, "../../demo/shim/demo-shim.js");

let browser;
let fixtures; // the fixture pages, served at /
before(async () => {
  browser = await launch();
  fixtures = await startServer(FIXTURES, "/");
});
after(async () => {
  await browser?.close();
  await fixtures?.close();
});

const rules = (findings) => [...new Set(findings.map((f) => f.rule))].sort();

// ── Layout audit ───────────────────────────────────────────────────────────

test("overflow.html: horizontal-scroll and small-text at phone width", async () => {
  const found = await auditPage(browser, fixtures.url + "overflow.html", "phone");
  const r = rules(found);
  assert.ok(r.includes("horizontal-scroll"), `rules: ${r}`);
  assert.ok(r.includes("small-text"), `rules: ${r}`);
  const tiny = found.find((f) => f.rule === "small-text");
  assert.equal(tiny.selector, "main > p.tiny");
});

test("clean.html: no findings at either viewport", async () => {
  for (const vp of ["desktop", "phone"]) {
    const found = await auditPage(browser, fixtures.url + "clean.html", vp);
    assert.deepEqual(found, [], `${vp}: ${JSON.stringify(found)}`);
  }
});

test("covered.html: a control drawn under another element is reported", async () => {
  const found = await auditPage(browser, fixtures.url + "covered.html", "desktop");
  const covered = found.filter((f) => f.rule === "covered-control");
  assert.equal(covered.length, 1, JSON.stringify(found));
  assert.equal(covered[0].selector, "#bar > button.icon");
  assert.match(covered[0].detail, /#notice/);
});

test("overlay.html: a full-screen view drawn over the page is not a covered control", async () => {
  for (const vp of ["desktop", "phone"]) {
    const found = await auditPage(browser, fixtures.url + "overlay.html", vp);
    assert.deepEqual(found, [], `${vp}: ${JSON.stringify(found)}`);
  }
});

test("a missing page is a page-load finding, not a crash", async () => {
  const found = await auditPage(browser, fixtures.url + "nope.html", "desktop");
  assert.deepEqual(rules(found), ["page-load"]);
});

// ── Baseline ───────────────────────────────────────────────────────────────

test("a finding in the baseline does not fail; a new one does", () => {
  const known = { page: "/", viewport: "phone", rule: "small-text", selector: "main > p.tiny" };
  const fresh = { page: "/queue", viewport: "desktop", rule: "overflow", selector: "div.wide" };
  const gone = { page: "/stats", viewport: "phone", rule: "overflow", selector: "canvas" };
  const r = compareBaseline([{ ...known, detail: "8px" }, fresh], [known, gone]);
  assert.deepEqual(r.fresh.map((f) => f.page), ["/queue"]);
  assert.deepEqual(r.fixed, [gone]);
  assert.deepEqual(compareBaseline([known], [known]).fresh, []);
});

test("sortFindings is stable and keeps only the baseline keys", () => {
  const a = { page: "/b", viewport: "phone", rule: "overflow", selector: "x", detail: "d" };
  const b = { page: "/a", viewport: "phone", rule: "overflow", selector: "y" };
  const c = { page: "/a", viewport: "desktop", rule: "overflow", selector: "y" };
  const out = sortFindings([a, b, c, { ...b }]);
  assert.deepEqual(out, [
    { page: "/a", viewport: "desktop", rule: "overflow", selector: "y" },
    { page: "/a", viewport: "phone", rule: "overflow", selector: "y" },
    { page: "/b", viewport: "phone", rule: "overflow", selector: "x" },
  ]);
});

// ── Pages-like server under /AniDex/ (Review Focus 4) ────────────────────

function makeSite() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "demo-site-"));
  const put = (rel, body) => {
    fs.mkdirSync(path.dirname(path.join(dir, rel)), { recursive: true });
    fs.writeFileSync(path.join(dir, rel), body);
  };
  const head = `<script>window.ANIDEX_DEMO = {"base":"/AniDex/","fixtures":{"api/ok":{"status":200,"body":{"ok":true}}}};</script>` +
    `<script src="/AniDex/demo-shim.js"></script><link rel="stylesheet" href="/AniDex/static/style.css">`;
  put("index.html", `<!DOCTYPE html><html><head><meta charset="utf-8">${head}</head><body>Home ✓</body></html>`);
  put("404.html", `<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>Fallback page</body></html>`);
  put("queue/index.html", `<!DOCTYPE html><html><head><meta charset="utf-8">${head}</head><body>Queue</body></html>`);
  put("static/style.css", "body { color: #111; }");
  put("demo-shim.js", fs.readFileSync(SHIM, "utf8"));
  put("load404/index.html",
    `<!DOCTYPE html><html><head><meta charset="utf-8">${head}</head><body>` +
    `<script>fetch('/api/ok'); fetch('/api/nope');</script></body></html>`);
  put("later/index.html",
    `<!DOCTYPE html><html><head><meta charset="utf-8">${head}</head><body>` +
    `<button onclick="fetch('/api/nope')">Open</button><script>fetch('/api/ok');</script></body></html>`);
  put("broken/index.html",
    `<!DOCTYPE html><html><head><meta charset="utf-8">${head}<script src="/AniDex/static/missing.js"></script></head>` +
    `<body><script>undefinedFunction();</script></body></html>`);
  return dir;
}

test("server: base path, static assets, 404.html fallback, directory redirect", async () => {
  const site = await startServer(makeSite(), "/AniDex/");
  try {
    const css = await fetch(site.origin + "/AniDex/static/style.css");
    assert.equal(css.status, 200);
    assert.match(css.headers.get("content-type"), /^text\/css/);

    const home = await fetch(site.origin + "/AniDex/");
    assert.equal(home.status, 200);
    assert.equal(home.headers.get("content-type"), "text/html; charset=utf-8");
    assert.match(await home.text(), /Home ✓/);

    const queue = await fetch(site.origin + "/AniDex/queue/");
    assert.match(await queue.text(), /Queue/);

    const bare = await fetch(site.origin + "/AniDex/queue", { redirect: "manual" });
    assert.equal(bare.status, 301);
    assert.equal(bare.headers.get("location"), "/AniDex/queue/");

    const missing = await fetch(site.origin + "/AniDex/does-not-exist");
    assert.equal(missing.status, 404);
    assert.equal(missing.headers.get("content-type"), "text/html; charset=utf-8");
    assert.match(await missing.text(), /Fallback page/);

    const outside = await fetch(site.origin + "/static/style.css");
    assert.equal(outside.status, 404);

    const escape = await fetch(site.origin + "/AniDex/%2e%2e/%2e%2e/etc/passwd");
    assert.equal(escape.status, 404);
    assert.doesNotMatch(await escape.text(), /root:/);
  } finally {
    await site.close();
  }
});

// ── Site check ─────────────────────────────────────────────────────────────

test("check: an /api 404 during load fails; one after interaction does not", async () => {
  const site = await startServer(makeSite(), "/AniDex/");
  try {
    const bad = await checkPage(browser, site.url + "load404/");
    assert.equal(bad.length, 1, JSON.stringify(bad));
    assert.match(bad[0], /\/api\/nope.*404/);

    const ok = await checkPage(browser, site.url + "later/");
    assert.deepEqual(ok, []);
    assert.deepEqual(await checkPage(browser, site.url), []);
  } finally {
    await site.close();
  }
});

test("check: page errors and failed assets fail", async () => {
  const site = await startServer(makeSite(), "/AniDex/");
  try {
    const problems = await checkPage(browser, site.url + "broken/");
    assert.ok(problems.some((p) => /page error.*undefinedFunction/.test(p)), problems.join("\n"));
    assert.ok(problems.some((p) => /404.*static\/missing\.js/.test(p)), problems.join("\n"));

    // The audit reports them too, with keys that do not carry the server's port.
    const found = await auditPage(browser, site.url + "broken/", "desktop");
    const failed = found.find((f) => f.rule === "failed-request");
    assert.equal(failed.selector, "/AniDex/static/missing.js");
    assert.ok(found.some((f) => f.rule === "page-error" && /undefinedFunction/.test(f.selector)));
  } finally {
    await site.close();
  }
});
