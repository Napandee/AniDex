// Opens every page of the built static demo through the Pages-like server
// (demo/serve.mjs) and fails if any page, while loading, has
//   - a page error (uncaught exception),
//   - a failed request, or a request answered 400 or above,
//   - a request to any host other than the demo server, or
//   - an /api/* call the shim answered 404 ("Not available in the demo").
// "While loading" means up to the load event plus network idle: an /api/* 404
// from a later click is the page's own "not in the demo" path, not a failure.
//
//   node demo/check.mjs <siteDir>            (local: demo/run.sh check _site)

import path from "node:path";
import { pathToFileURL } from "node:url";
import { chromium } from "playwright";
import { startServer, siteBase, sitePages, pageUrl } from "./serve.mjs";

// Fixed time zone and locale, so dates render the same on every machine.
const SAME = { timezoneId: "UTC", locale: "en-US" };
export const VIEWPORTS = {
  desktop: { ...SAME, viewport: { width: 1440, height: 900 } },
  phone: { ...SAME, viewport: { width: 393, height: 852 }, isMobile: true, hasTouch: true },
};

export function launch() {
  return chromium.launch();
}

// Runs before any page script. The demo shim assigns window.fetch once (after
// setting __anidexDemoShim); that assignment is wrapped so every call through the
// shim is logged with its status. Later wrappers (the app's own) pass through it.
function recordShimFetches() {
  const log = (window.__demoFetchLog = []);
  let current = window.fetch;
  let wrapped = false;
  Object.defineProperty(window, "fetch", {
    configurable: true,
    get() { return current; },
    set(fn) {
      if (wrapped || !window.__anidexDemoShim || typeof fn !== "function") {
        current = fn;
        return;
      }
      wrapped = true;
      const inner = fn;
      current = function (input, init) {
        const url = typeof input === "string" ? input : input && input.url !== undefined ? input.url : String(input);
        const entry = { url: new URL(url, location.href).href, status: null, local: false };
        log.push(entry);
        const p = inner.apply(this, arguments);
        // A Response built in JS (the shim's answer) has an empty url; one from
        // the network has its URL, and the network listeners already see it.
        Promise.resolve(p).then(
          (r) => { entry.status = r && r.status; entry.local = !!r && r.url === ""; },
          () => { entry.status = "rejected"; },
        );
        return p;
      };
    },
  });
}

/**
 * Opens <url> at desktop size and returns a list of problems (strings); empty
 * means the page is clean. <browser> is a Playwright browser.
 */
export async function checkPage(browser, url, { viewport = "desktop" } = {}) {
  const origin = new URL(url).origin;
  const context = await browser.newContext(VIEWPORTS[viewport]);
  const page = await context.newPage();
  await page.addInitScript(recordShimFetches);
  const problems = [];
  let loading = true;
  page.on("pageerror", (e) => loading && problems.push(`page error: ${e.message.split("\n")[0]}`));
  page.on("requestfailed", (r) => loading && problems.push(`failed request: ${r.url()} (${r.failure()?.errorText})`));
  page.on("response", (r) => {
    if (loading && r.status() >= 400 && r.url() !== url) problems.push(`HTTP ${r.status()}: ${r.url()}`);
  });
  page.on("request", (r) => {
    const u = r.url();
    if (loading && /^https?:/.test(u) && new URL(u).origin !== origin) problems.push(`outside request: ${u}`);
  });
  try {
    const resp = await page.goto(url, { waitUntil: "load" });
    if (!resp || resp.status() !== 200) problems.push(`page answered ${resp ? resp.status() : "nothing"}`);
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(100); // let shim answers settle
    loading = false;
    const log = await page.evaluate(() => window.__demoFetchLog || []);
    for (const f of log) {
      const where = new URL(f.url);
      if (f.local && f.status === 404) problems.push(`api ${where.pathname}${where.search} answered 404 during load`);
      else if (f.status === "rejected") problems.push(`fetch ${where.pathname}${where.search} rejected during load`);
    }
  } catch (e) {
    problems.push(`load failed: ${e.message.split("\n")[0]}`);
  } finally {
    loading = false;
    await context.close();
  }
  return problems;
}

async function main(siteDir) {
  const base = siteBase(siteDir);
  const pages = [...sitePages(siteDir), "/not-in-demo.html"];
  const server = await startServer(siteDir, base);
  const browser = await launch();
  let bad = 0;
  try {
    for (const p of pages) {
      const problems = await checkPage(browser, server.url + pageUrl(p));
      if (problems.length) {
        bad++;
        for (const m of problems) console.log(`FAIL ${p}: ${m}`);
      } else {
        console.log(`ok   ${p}`);
      }
    }
  } finally {
    await browser.close();
    await server.close();
  }
  console.log(`CHECKED ${pages.length} pages under ${base}: ${bad} with problems`);
  return bad ? 1 : 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const siteDir = process.argv[2];
  if (!siteDir) {
    console.error("usage: node demo/check.mjs <siteDir>");
    process.exit(2);
  }
  main(siteDir).then((code) => process.exit(code), (e) => {
    console.error(`ERROR ${e.stack || e.message}`);
    process.exit(1);
  });
}
