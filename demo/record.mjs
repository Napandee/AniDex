// Records the AniDex static demo from the real app (record, then replay).
//
// Runs inside the sealed record stack (demo/build.sh, podman or docker):
// the app, its Postgres and this recorder share a network with no
// egress. It registers the demo user (first account: admin), applies demo/seed.sql
// bound to that user, logs in, visits every page in demo/demo.config.json, and
// writes a static site:
//   <out>/<page>/index.html  server HTML, links rewritten, shim injected
//   <out>/fixtures.json      every /api/* GET a page made while loading
//   <out>/pages.json         the pages recorded (and skipped)
//   <out>/static/...         app/static plus the generated covers
//   <out>/404.html, .nojekyll, not-in-demo.html, demo-shim.js
//
// Any failure prints a line starting with ERROR and the run exits non-zero.
// A page that ends on a different URL than requested is not saved: it is logged
// as "SKIP <page> redirected to <url>".
//
//   node demo/record.mjs            (env: APP_URL, DATABASE_URL, OUT)

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");

// ── Pure helpers (unit-tested in tests/demo/record.test.mjs) ───────────────

/** "/" -> "", "/queue" -> "../", "/anime/1/notes" -> "../../../" */
export function relPrefix(page) {
  const depth = page.split("/").filter(Boolean).length;
  return "../".repeat(depth);
}

/** "/" -> "index.html", "/stats/wrapped" -> "stats/wrapped/index.html" */
export function pageFile(page) {
  const p = page.replace(/^\/+|\/+$/g, "");
  return p ? `${p}/index.html` : "index.html";
}

/** Fixture key: request path without the leading "/", plus the query verbatim. */
export function fixtureKey(url) {
  const u = new URL(url);
  return u.pathname.replace(/^\//, "") + u.search;
}

/** null when the browser ended on the requested page (query ignored), else the SKIP line. */
export function redirectSkip(requested, finalUrl) {
  const finalPath = new URL(finalUrl).pathname;
  const norm = (p) => (p.length > 1 ? p.replace(/\/+$/, "") : p);
  if (norm(finalPath) === norm(requested)) return null;
  return `SKIP ${requested} redirected to ${finalPath}`;
}

/** Drop service-worker registration; pin script-driven navigation under the base. */
export function rewriteScript(js) {
  return js
    .replace(/navigator\.serviceWorker\.register\(/g, "Promise.resolve(")
    .replace(/location\.href\s*=\s*(['"`])\//g, "location.href = window.DEMO_SITE.base + $1");
}

function escapeAttr(s) {
  return s.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
}

function decodeAttr(s) {
  return s.replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#39;/g, "'");
}

/**
 * Where a root-absolute <a href> goes in the static site.
 * Returns { href } for a recorded page or asset, or { missing: true }.
 */
function linkTarget(raw, opts, prefix) {
  const url = new URL(decodeAttr(raw), "http://demo.invalid");
  const p = url.pathname;
  if (p.startsWith("/static/")) return { href: prefix + raw.slice(1) };
  if (opts.missing.some((m) => p.startsWith(m))) return { missing: true };
  const norm = p.length > 1 ? p.replace(/\/+$/, "") : p;
  if (!opts.pages.includes(norm)) return { missing: true };
  const params = new URLSearchParams(url.search);
  for (const k of opts.ignoreQuery || []) params.delete(k);
  // v1 is navigation only: a filter/tab/paging query on a recorded page shows
  // "not in the demo" rather than the unfiltered recording.
  if ([...params.keys()].length) return { missing: true };
  const dir = norm === "/" ? "" : norm.slice(1) + "/";
  const target = prefix + dir || "./";
  return { href: escapeAttr(target + url.search + url.hash) };
}

/**
 * Rewrites one recorded page for the static site.
 * opts: { page, base, pages, missing, ignoreQuery, fixtures, prefix? }
 * prefix defaults to the page's relative path back to the site root; 404.html
 * passes the base instead, since Pages serves it at any depth.
 */
export function rewriteHtml(html, opts) {
  const prefix = opts.prefix ?? relPrefix(opts.page);
  const notInDemo = prefix + "not-in-demo.html";
  let out = html;

  // PWA bits: no manifest, no service worker in the demo.
  out = out.replace(/[ \t]*<link\b[^>]*\brel=["']manifest["'][^>]*>\s*/gi, "");
  out = rewriteScript(out);

  // Third-party scripts the app loads (Chart.js from a CDN) come from the demo itself.
  for (const [url, local] of Object.entries(opts.vendor || {})) out = out.split(url).join(prefix + local);

  // Links between pages; listed external links (e.g. AniList pages for made-up ids)
  // count as missing too.
  const externalMissing = opts.missing.filter((m) => /^https?:\/\//.test(m));
  out = out.replace(/<a\b[^>]*>/gi, (tag) =>
    tag.replace(/(\s)href=(["'])([^"']*)\2/i, (m, sp, q, raw) => {
      let t;
      if (/^\/(?!\/)/.test(raw)) t = linkTarget(raw, opts, prefix);
      else if (externalMissing.some((x) => raw.startsWith(x))) t = { missing: true };
      else return m;
      if (t.href !== undefined) return `${sp}href=${q}${t.href}${q}`;
      return `${sp}href=${q}${notInDemo}${q} data-demo-missing data-demo-href=${q}${raw}${q}`;
    }),
  );

  // Forms post nowhere in the demo.
  out = out.replace(/<form\b[^>]*>/gi, (tag) => {
    let t = tag.replace(/\bmethod=(["'])[^"']*\1/i, 'method="get"');
    if (/\baction=/i.test(t)) t = t.replace(/\baction=(["'])[^"']*\1/i, `action="${notInDemo}"`);
    else t = t.replace(/^<form\b/i, `<form action="${notInDemo}"`);
    return t;
  });

  // Every remaining root-absolute asset URL (attributes, inline JSON, CSS url()).
  out = out.replace(/(["'(])\/static\//g, `$1${prefix}static/`);

  // Config + shim go first in <head>, before any app script runs.
  const cfg = JSON.stringify({ base: opts.base, fixtures: opts.fixtures || {}, ...(opts.now ? { now: opts.now } : {}) }).replace(/</g, "\\u003c");
  const inject =
    `<script>window.DEMO_SITE = ${cfg};</script>` +
    `<script src="${prefix}demo-shim.js"></script>`;
  // The config is large; keep <meta charset> ahead of it, inside the browser's
  // 1024-byte charset prescan window.
  const charset = /<meta\s+charset=["']?[^"'\s>]+["']?\s*\/?>/i;
  if (charset.test(out)) out = out.replace(charset, (m) => m + inject);
  else out = out.replace(/<head\b[^>]*>/i, (h) => h + '<meta charset="utf-8">' + inject);
  return out;
}

/** psql-style binding of the seed's :'email' variable. */
export function bindSeed(sql, email) {
  return sql.split(":'email'").join("'" + email.replace(/'/g, "''") + "'");
}

/** Expand "/anime/*\/notes"-style patterns with the given ids. */
export function expandPages(patterns, ids) {
  const out = [];
  for (const p of patterns) {
    if (p.includes("*")) for (const id of ids) out.push(p.replace("*", String(id)));
    else out.push(p);
  }
  return out;
}

// ── Recorder ───────────────────────────────────────────────────────────────

function copyDir(src, dst, filter = () => true) {
  fs.mkdirSync(dst, { recursive: true });
  for (const e of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, e.name);
    const d = path.join(dst, e.name);
    if (!filter(s)) continue;
    if (e.isDirectory()) copyDir(s, d, filter);
    else fs.copyFileSync(s, d);
  }
}

function write(file, data) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, data);
}

async function waitForApp(appUrl, seconds) {
  const until = Date.now() + seconds * 1000;
  for (;;) {
    try {
      const r = await fetch(appUrl + "/auth/register", { redirect: "manual" });
      if (r.status < 500) return;
    } catch {
      /* not up yet */
    }
    if (Date.now() > until) throw new Error(`app not reachable at ${appUrl} after ${seconds}s`);
    await new Promise((r) => setTimeout(r, 1000));
  }
}

async function main() {
  const config = JSON.parse(fs.readFileSync(path.join(HERE, "demo.config.json"), "utf8"));
  const appUrl = (process.env.APP_URL || "http://anidex:8888").replace(/\/+$/, "");
  const dbUrl = process.env.DATABASE_URL;
  const outDir = path.resolve(process.env.OUT || path.join(ROOT, "_site"));
  const appOrigin = new URL(appUrl).origin;
  const errors = [];
  const error = (msg) => {
    errors.push(msg);
    console.log(`ERROR ${msg}`);
  };

  if (!dbUrl) throw new Error("DATABASE_URL is not set");
  fs.rmSync(outDir, { recursive: true, force: true });
  fs.mkdirSync(outDir, { recursive: true });

  await waitForApp(appUrl, 120);
  console.log(`app up at ${appUrl}`);

  const { chromium } = await import("playwright");
  const pg = (await import("pg")).default;

  // The app's hostname is "anidex", not "app": .app is an HSTS-preloaded TLD, so
  // Chromium would force https on http://app:8888 and fail the TLS handshake.
  const browser = await chromium.launch();
  const context = await browser.newContext({ serviceWorkers: "block", timezoneId: "UTC", locale: "en-US" });
  // The demo's "today" is pinned (config.now): build.sh starts the app and its
  // database under libfaketime at that instant, and the browser uses it too, so a
  // rebuild in another month records the same data and the same pages.
  if (config.now) {
    await context.clock.setFixedTime(new Date(config.now));
    const db = new pg.Client({ connectionString: dbUrl });
    await db.connect();
    const { rows } = await db.query("SELECT current_date::text AS d");
    await db.end();
    const appDate = new Date((await fetch(appUrl + "/")).headers.get("date") || 0);
    const want = config.now.slice(0, 10);
    if (rows[0].d !== want) error(`database clock is ${rows[0].d}, not the pinned ${want}`);
    if (appDate.toISOString().slice(0, 10) !== want) error(`app clock is ${appDate.toISOString()}, not the pinned ${want}`);
  }
  // Third-party scripts are answered from pinned local copies (demo/package.json),
  // both while recording (no egress here) and in the published site.
  const vendor = config.vendor || {};
  for (const [url, v] of Object.entries(vendor)) {
    const file = path.join(HERE, v.from);
    if (!fs.existsSync(file)) throw new Error(`vendor file missing: ${file}`);
    await context.route(url, (route) => route.fulfill({ path: file, contentType: "text/javascript" }));
  }
  const page = await context.newPage();
  const { email, password } = config.user;

  // 1. Register the demo user: the first account becomes admin.
  await page.goto(appUrl + "/auth/register");
  await page.fill("#email", email);
  await page.fill("#password", password);
  await Promise.all([page.waitForURL(appUrl + "/"), page.click("button[type=submit]")]);
  console.log(`registered ${email}`);

  // 2. Seed, bound to that user.
  const db = new pg.Client({ connectionString: dbUrl });
  await db.connect();
  const seedSql = fs.readFileSync(path.join(ROOT, config.seed || "demo/seed.sql"), "utf8");
  await db.query(bindSeed(seedSql, email));
  const { rows } = await db.query(
    "SELECT le.anime_id FROM library_entries le JOIN users u ON u.id = le.user_id WHERE u.email = $1 ORDER BY le.anime_id",
    [email],
  );
  await db.end();
  const pages = expandPages(config.pages, rows.map((r) => r.anime_id));
  console.log(`seeded: ${rows.length} library entries, ${pages.length} pages to record`);

  // 3. Log in afresh, as a visitor would.
  await context.clearCookies();
  await page.goto(appUrl + "/auth/login");
  await page.fill("#email", email);
  await page.fill("#password", password);
  await Promise.all([page.waitForURL(appUrl + "/"), page.click("button[type=submit]")]);
  console.log("logged in");

  // 4. Record every page.
  const fixtures = {};
  const recorded = [];
  const skipped = [];
  let pending = [];
  page.on("response", (resp) => {
    const req = resp.request();
    const u = new URL(resp.url());
    if (u.origin !== appOrigin || !u.pathname.startsWith("/api/") || req.method() !== "GET") return;
    pending.push(
      resp.text().then(
        (text) => {
          let body;
          try {
            body = JSON.parse(text);
          } catch {
            body = text;
          }
          fixtures[fixtureKey(resp.url())] = { status: resp.status(), body };
        },
        (e) => error(`could not read ${resp.url()}: ${e.message}`),
      ),
    );
  });
  page.on("requestfailed", (req) => error(`request failed: ${req.method()} ${req.url()} (${req.failure()?.errorText})`));
  page.on("pageerror", (e) => error(`page error on ${page.url()}: ${e.message}`));
  page.on("request", (req) => {
    const u = new URL(req.url());
    if (!["http:", "https:"].includes(u.protocol)) return;
    if (u.origin !== appOrigin && !(req.url() in vendor)) error(`request outside the app: ${req.url()}`);
  });

  const htmlByPage = {};
  for (const p of pages) {
    let resp;
    try {
      resp = await page.goto(appUrl + p, { waitUntil: "networkidle", timeout: 30000 });
    } catch (e) {
      error(`${p}: ${e.message.split("\n")[0]}`);
      continue;
    }
    await Promise.all(pending);
    pending = [];
    const skip = redirectSkip(p, page.url());
    if (skip) {
      console.log(skip);
      skipped.push({ page: p, to: new URL(page.url()).pathname });
      continue;
    }
    if (!resp || resp.status() !== 200) {
      error(`${p}: HTTP ${resp ? resp.status() : "no response"}`);
      continue;
    }
    htmlByPage[p] = await resp.text();
    recorded.push(p);
    console.log(`page ${p}`);
  }
  await browser.close();

  // 5. Write the site. Fixture bodies point at assets under the base.
  const baseFixtures = JSON.parse(
    JSON.stringify(fixtures).replace(/"\/static\//g, `"${config.base}static/`),
  );
  const opts = {
    base: config.base,
    pages: recorded,
    missing: config.missing,
    ignoreQuery: config.ignoreQuery || [],
    fixtures: baseFixtures,
    now: config.now,
    vendor: Object.fromEntries(Object.entries(vendor).map(([url, v]) => [url, v.to])),
  };
  for (const p of recorded) {
    write(path.join(outDir, pageFile(p)), rewriteHtml(htmlByPage[p], { ...opts, page: p }));
  }
  if (htmlByPage["/"]) {
    write(path.join(outDir, "404.html"), rewriteHtml(htmlByPage["/"], { ...opts, page: "/", prefix: config.base }));
  } else {
    error("no index page recorded");
  }

  copyDir(path.join(ROOT, "app", "static"), path.join(outDir, "static"), (s) => !s.endsWith("service-worker.js"));
  const appJs = path.join(outDir, "static", "script.js");
  fs.writeFileSync(appJs, rewriteScript(fs.readFileSync(appJs, "utf8")));
  const covers = path.join(HERE, "covers");
  if (fs.existsSync(covers)) copyDir(covers, path.join(outDir, "static", "demo-covers"));
  else error("demo/covers missing: run demo/make_covers.py first");
  for (const v of Object.values(vendor)) {
    fs.mkdirSync(path.dirname(path.join(outDir, v.to)), { recursive: true });
    fs.copyFileSync(path.join(HERE, v.from), path.join(outDir, v.to));
  }
  // The shim comes from the shared demo kit (Napandee/standard-workflows demo-kit/shim).
  const shimDir = process.env.DEMO_SHIM_DIR || path.join(HERE, "shim");
  fs.copyFileSync(path.join(shimDir, "demo-shim.js"), path.join(outDir, "demo-shim.js"));
  fs.copyFileSync(path.join(shimDir, "not-in-demo.html"), path.join(outDir, "not-in-demo.html"));
  write(path.join(outDir, ".nojekyll"), "");
  write(path.join(outDir, "fixtures.json"), JSON.stringify(baseFixtures, null, 1) + "\n");
  write(
    path.join(outDir, "pages.json"),
    JSON.stringify({ base: config.base, pages: recorded.map((p) => ({ page: p, file: pageFile(p) })), skipped }, null, 1) + "\n",
  );

  console.log(
    `RECORDED ${recorded.length} pages, ${Object.keys(baseFixtures).length} fixtures, ` +
      `${skipped.length} skipped, ${errors.length} errors -> ${outDir}`,
  );
  if (errors.length) process.exit(1);
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch((e) => {
    console.log(`ERROR ${e.stack || e.message}`);
    process.exit(1);
  });
}
