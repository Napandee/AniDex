import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const SRC = fs.readFileSync(path.join(here, "../../demo/shim/demo-shim.js"), "utf8");

const FIXTURES = {
  "api/library?status=WATCHING": { status: 200, body: { items: ["a"] } },
  "api/library": { status: 200, body: { items: [] } },
};

// Minimal DOM: enough for the banner and the click handler.
function makeDocument() {
  const all = [];
  const listeners = {};
  const body = {
    children: [],
    insertBefore(el) { this.children.unshift(el); all.push(el); },
    prepend(el) { this.insertBefore(el); },
    appendChild(el) { this.children.push(el); all.push(el); },
  };
  return {
    body,
    readyState: "complete",
    createElement(tag) { return { tag, id: "", style: {}, textContent: "" }; },
    getElementById(id) { return all.find((e) => e.id === id) || null; },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    listeners,
  };
}

function load({ fixtures = FIXTURES, base = "/AniDex/", times = 1, now } = {}) {
  const realCalls = [];
  const document = makeDocument();
  const sandbox = {
    Response, URL, Request, JSON, Promise, Object, Date,
    document,
    location: { href: "https://example.github.io/AniDex/library/", assign() {} },
    fetch() { realCalls.push(arguments); throw new Error("demo must never reach a real server"); },
  };
  sandbox.window = sandbox;
  sandbox.ANIDEX_DEMO = now ? { base, fixtures, now } : { base, fixtures };
  vm.createContext(sandbox);
  for (let i = 0; i < times; i++) vm.runInContext(SRC, sandbox);
  return { sandbox, document, realCalls };
}

test("known GET returns fixture status and body", async () => {
  const { sandbox } = load();
  const r = await sandbox.window.fetch("/AniDex/api/library");
  assert.ok(r instanceof Response);
  assert.equal(r.status, 200);
  assert.match(r.headers.get("content-type"), /application\/json/);
  assert.deepEqual(await r.json(), { items: [] });
});

test("GET key includes query string", async () => {
  const { sandbox } = load();
  const a = await sandbox.window.fetch("/AniDex/api/library?status=WATCHING");
  assert.deepEqual(await a.json(), { items: ["a"] });
  const b = await sandbox.window.fetch("api/library?status=WATCHING");
  assert.deepEqual(await b.json(), { items: ["a"] });
  const c = await sandbox.window.fetch(new Request("https://example.github.io/AniDex/api/library?status=WATCHING"));
  assert.deepEqual(await c.json(), { items: ["a"] });
  const d = await sandbox.window.fetch("/AniDex/api/library?status=DROPPED");
  assert.equal(d.status, 404);
});

test("unknown GET resolves to 404 response, not a throw", async () => {
  const { sandbox, realCalls } = load();
  const r = await sandbox.window.fetch("/AniDex/api/nope");
  assert.equal(r.status, 404);
  assert.deepEqual(await r.json(), { detail: "Not available in the demo" });
  assert.equal(realCalls.length, 0);
});

test("POST/PUT/PATCH/DELETE return ok nothing-saved", async () => {
  const { sandbox, realCalls } = load();
  for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
    const r = await sandbox.window.fetch("/AniDex/api/library", { method });
    assert.equal(r.status, 200, method);
    assert.deepEqual(await r.json(), { ok: true, detail: "Demo: nothing is saved" });
  }
  const viaRequest = await sandbox.window.fetch(
    new Request("https://example.github.io/AniDex/api/x", { method: "POST", body: "{}" }));
  assert.deepEqual(await viaRequest.json(), { ok: true, detail: "Demo: nothing is saved" });
  assert.equal(realCalls.length, 0);
});

test("non-/api requests pass to real fetch", () => {
  const { sandbox, realCalls } = load();
  assert.throws(() => sandbox.window.fetch("/AniDex/static/app.css"), /demo must never reach a real server/);
  assert.equal(realCalls.length, 1);
});

test("banner is inserted once", () => {
  const { document } = load({ times: 2 });
  const banners = document.body.children.filter((e) => e.textContent === "Demo — made-up data, nothing is saved");
  assert.equal(banners.length, 1);
});

test("banner takes space in normal flow: not sticky or fixed, no z-index", () => {
  const { document } = load();
  const b = document.body.children.find((e) => e.id === "anidex-demo-banner");
  assert.ok(b);
  assert.doesNotMatch(b.style.cssText, /position|z-index/);
});

test("with ANIDEX_DEMO.now the page's clock starts at the demo date", () => {
  const { sandbox } = load({ now: "2026-10-15T12:00:00Z" });
  const D = sandbox.window.Date;
  const start = Date.parse("2026-10-15T12:00:00Z");
  assert.ok(Math.abs(D.now() - start) < 5000, String(new Date(D.now())));
  assert.ok(Math.abs(new D().getTime() - start) < 5000);
  assert.equal(new D().getUTCFullYear(), 2026);
  assert.ok(new D() instanceof Date);
  assert.equal(new D("2020-01-02T00:00:00Z").toISOString(), "2020-01-02T00:00:00.000Z");
  assert.equal(new D(2020, 0, 2).getFullYear(), 2020);
  assert.equal(D.UTC(2020, 0, 1), Date.UTC(2020, 0, 1));
  assert.equal(D.parse("2020-01-01T00:00:00Z"), Date.parse("2020-01-01T00:00:00Z"));
  assert.match(D(), /2026/);
});

test("without ANIDEX_DEMO.now the clock is left alone", () => {
  const { sandbox } = load();
  assert.equal(sandbox.window.Date, Date);
});

test("click on data-demo-missing link goes to not-in-demo.html", () => {
  const { document, sandbox } = load();
  let prevented = false;
  const link = { getAttribute: (n) => (n === "data-demo-missing" ? "" : null), hasAttribute: (n) => n === "data-demo-missing" };
  const target = { closest: (sel) => (sel.includes("data-demo-missing") ? link : null) };
  let assigned;
  sandbox.location.assign = (u) => { assigned = u; };
  for (const fn of document.listeners.click) fn({ target, preventDefault() { prevented = true; } });
  assert.ok(prevented);
  assert.equal(assigned, "/AniDex/not-in-demo.html");
});

test("root-absolute /api GET (script.js call shape) is answered from fixtures", async () => {
  const { sandbox, realCalls } = load();
  const r = await sandbox.window.fetch("/api/library?status=WATCHING");
  assert.equal(r.status, 200);
  assert.deepEqual(await r.json(), { items: ["a"] });
  assert.equal(realCalls.length, 0);
});

test("root-absolute unknown /api GET is 404 and never reaches real fetch", async () => {
  const { sandbox, realCalls } = load();
  const r = await sandbox.window.fetch("/api/nope");
  assert.equal(r.status, 404);
  assert.deepEqual(await r.json(), { detail: "Not available in the demo" });
  assert.equal(realCalls.length, 0);
});

test("base without trailing slash still answers /api from fixtures", async () => {
  const { sandbox, realCalls } = load({ base: "/AniDex" });
  const r = await sandbox.window.fetch("/api/library");
  assert.equal(r.status, 200);
  assert.deepEqual(await r.json(), { items: [] });
  const r2 = await sandbox.window.fetch("/AniDex/api/library");
  assert.equal(r2.status, 200);
  assert.equal(realCalls.length, 0);
});

test("answers made while the page is still loading wait for DOMContentLoaded", async () => {
  // Deferred app scripts (script.js defines t()) run before DOMContentLoaded; an
  // inline script's fetch callback must not beat them, as it never does live.
  const { sandbox, document } = load();
  document.readyState = "loading";
  let done = false;
  const p = sandbox.window.fetch("/AniDex/api/library").then((r) => { done = true; return r; });
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(done, false);
  document.readyState = "interactive";
  for (const fn of document.listeners.DOMContentLoaded || []) fn();
  const r = await p;
  assert.equal(r.status, 200);
  const nonGet = sandbox.window.fetch("/AniDex/api/library", { method: "POST" });
  assert.equal((await nonGet).status, 200);
});
