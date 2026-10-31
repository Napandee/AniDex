// Layout audit of the built static demo: every page in <siteDir>/pages.json at
// desktop (1440x900) and phone (393x852, mobile) through the Pages-like server.
// Rules (one finding per {page, viewport, rule, selector}):
//   page-load          the page did not open, or answered other than 200
//   page-error         an uncaught exception while loading
//   failed-request     a request failed or was answered 400 or above
//   horizontal-scroll  the page scrolls sideways
//   overflow           an element runs past the screen edge (outermost one only;
//                      inside a box that scrolls or clips sideways is fine)
//   small-text         visible text under 10px
//   covered-control    a link, button or field in the first screen is drawn under
//                      another element, so a tap there hits the other element
//                      (under a fixed full-screen view, such as a dialog, is fine)
// Findings already in demo/audit-baseline.json are reported but do not fail; any
// other finding fails the run. page-load, page-error and failed-request are never
// written to the baseline and always fail. Full-page screenshots go to audit-shots/.
//
//   node demo/audit.mjs <siteDir> [--update-baseline]   (local: demo/run.sh audit _site)

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { startServer, siteBase, sitePages, pageUrl } from "./serve.mjs";
import { VIEWPORTS, launch } from "./check.mjs";

export { VIEWPORTS, launch };

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const BASELINE = path.join(HERE, "audit-baseline.json");
const KEYS = ["page", "viewport", "rule", "selector"];
const key = (f) => KEYS.map((k) => f[k]).join("\u0000");

// Runs in the page. Returns [{rule, selector, detail}].
function measure() {
  const vw = document.documentElement.clientWidth;
  const vh = window.innerHeight;
  const out = [];
  const step = (e) => {
    if (e.id) return "#" + CSS.escape(e.id);
    const cls = [...e.classList].slice(0, 2).map((c) => "." + CSS.escape(c)).join("");
    return e.tagName.toLowerCase() + cls;
  };
  // Up to three steps, stopping at an id or at <body>.
  const sel = (e) => {
    const parts = [];
    for (let n = e; n && n !== document.body && n !== document.documentElement && parts.length < 3; n = n.parentElement) {
      parts.unshift(step(n));
      if (n.id) break;
    }
    return parts.join(" > ") || e.tagName.toLowerCase();
  };
  const shown = (e) => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && e.checkVisibility({ opacityProperty: true, visibilityProperty: true });
  };
  const all = [...document.body.querySelectorAll("*")].filter(shown);

  const scrollW = document.documentElement.scrollWidth;
  if (scrollW > vw + 1) out.push({ rule: "horizontal-scroll", selector: "html", detail: `scrollWidth ${scrollW}px > ${vw}px` });

  // Inside a box that scrolls or clips sideways, running wide is fine.
  const contained = (e) => {
    for (let p = e.parentElement; p && p !== document.body; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX;
      if (o === "auto" || o === "scroll" || o === "hidden" || o === "clip") return true;
    }
    return false;
  };
  const sx = window.scrollX;
  const off = (e) => {
    const r = e.getBoundingClientRect();
    return r.right + sx > vw + 1 || r.left + sx < -1;
  };
  for (const e of all) {
    if (!off(e) || contained(e)) continue;
    if (e.parentElement && e.parentElement !== document.body && off(e.parentElement) && !contained(e.parentElement)) continue;
    const r = e.getBoundingClientRect();
    out.push({ rule: "overflow", selector: sel(e), detail: `${Math.round(r.left + sx)}..${Math.round(r.right + sx)}px of ${vw}px` });
  }

  for (const e of all) {
    const own = [...e.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
    if (!own) continue;
    const size = parseFloat(getComputedStyle(e).fontSize);
    if (size < 10) out.push({ rule: "small-text", selector: sel(e), detail: `${size}px` });
  }

  // A fixed element filling the whole screen (a dialog, a full-screen view) covers
  // the page on purpose; what is under it is not a finding.
  const overlay = (hit) => {
    for (let n = hit; n && n !== document.body; n = n.parentElement) {
      if (getComputedStyle(n).position !== "fixed") continue;
      const r = n.getBoundingClientRect();
      if (r.left <= 1 && r.top <= 1 && r.right >= vw - 1 && r.bottom >= vh - 1) return true;
    }
    return false;
  };
  const controls = document.body.querySelectorAll("a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button]");
  for (const c of controls) {
    if (!shown(c) || getComputedStyle(c).pointerEvents === "none") continue;
    const r = c.getBoundingClientRect();
    if (r.top < 0 || r.left < 0 || r.bottom > vh || r.right > vw) continue;
    // The centre, and a point a quarter in from each edge.
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const points = [[cx, cy], [cx, r.top + r.height / 4], [cx, r.bottom - r.height / 4],
      [r.left + r.width / 4, cy], [r.right - r.width / 4, cy]];
    for (const [x, y] of points) {
      const hit = document.elementFromPoint(x, y);
      if (!hit || c.contains(hit) || hit.contains(c) || overlay(hit)) continue;
      out.push({ rule: "covered-control", selector: sel(c), detail: `under ${sel(hit)}` });
      break;
    }
  }

  // One finding per rule + selector.
  const seen = new Set();
  return out.filter((f) => {
    const k = f.rule + " " + f.selector;
    return seen.has(k) ? false : (seen.add(k), true);
  });
}

/**
 * Opens <url> at <viewport> ("desktop" | "phone") and returns its findings
 * [{rule, selector, detail}]. With <shot>, saves a full-page screenshot there.
 */
export async function auditPage(browser, url, viewport, { shot } = {}) {
  const context = await browser.newContext(VIEWPORTS[viewport]);
  const page = await context.newPage();
  const found = [];
  // The server's port changes every run: keep it out of the baseline keys.
  const origin = new URL(url).origin;
  const local = (s) => s.split(origin).join("");
  page.on("pageerror", (e) => found.push({ rule: "page-error", selector: local(e.message.split("\n")[0]).slice(0, 120), detail: e.message }));
  page.on("requestfailed", (r) => found.push({ rule: "failed-request", selector: local(r.url()), detail: r.failure()?.errorText || "" }));
  page.on("response", (r) => {
    if (r.status() >= 400 && r.url() !== url) found.push({ rule: "failed-request", selector: local(r.url()), detail: `HTTP ${r.status()}` });
  });
  try {
    const resp = await page.goto(url, { waitUntil: "load" });
    if (!resp || resp.status() !== 200) {
      found.push({ rule: "page-load", selector: "html", detail: `answered ${resp ? resp.status() : "nothing"}` });
      return found;
    }
    await page.waitForLoadState("networkidle");
    await page.evaluate(() => document.fonts.ready);
    await page.evaluate(() => window.scrollTo(0, 0));
    found.push(...(await page.evaluate(measure)));
    if (shot) {
      fs.mkdirSync(path.dirname(shot), { recursive: true });
      await page.screenshot({ path: shot, fullPage: true });
    }
  } catch (e) {
    found.push({ rule: "page-load", selector: "html", detail: e.message.split("\n")[0] });
  } finally {
    await context.close();
  }
  return found;
}

/** Baseline form: only the key fields, de-duplicated, in a stable order. */
export function sortFindings(findings) {
  const byKey = new Map();
  for (const f of findings) byKey.set(key(f), Object.fromEntries(KEYS.map((k) => [k, f[k]])));
  return [...byKey.values()].sort((a, b) => {
    for (const k of KEYS) if (a[k] !== b[k]) return a[k] < b[k] ? -1 : 1;
    return 0;
  });
}

// A page that does not load cleanly is never accepted, baseline or not.
const HARD = new Set(["page-load", "page-error", "failed-request"]);

/** The baseline to write for <findings>: sorted, keys only, load failures left out. */
export function toBaseline(findings) {
  return sortFindings(findings.filter((f) => !HARD.has(f.rule)));
}

/**
 * { fresh: findings that fail the run (not in the baseline, or a load failure),
 *   fixed: baseline entries no longer found }.
 */
export function compareBaseline(findings, baseline) {
  const known = new Set(baseline.filter((b) => !HARD.has(b.rule)).map(key));
  const now = new Set(findings.map(key));
  return {
    fresh: findings.filter((f) => !known.has(key(f))),
    fixed: baseline.filter((b) => !now.has(key(b))),
  };
}

const slug = (page) => (page.replace(/^\/+|\/+$/g, "").replace(/[^A-Za-z0-9.-]+/g, "_") || "index");

async function main(siteDir, update) {
  const base = siteBase(siteDir);
  const pages = sitePages(siteDir);
  const shots = path.resolve(HERE, "..", "audit-shots");
  const server = await startServer(siteDir, base);
  const browser = await launch();
  const findings = [];
  try {
    for (const p of pages) {
      for (const vp of Object.keys(VIEWPORTS)) {
        const found = await auditPage(browser, server.url + pageUrl(p), vp, { shot: path.join(shots, `${slug(p)}-${vp}.png`) });
        findings.push(...found.map((f) => ({ page: p, viewport: vp, ...f })));
      }
    }
  } finally {
    await browser.close();
    await server.close();
  }

  const baseline = fs.existsSync(BASELINE) ? JSON.parse(fs.readFileSync(BASELINE, "utf8")) : [];
  const { fresh, fixed } = compareBaseline(findings, baseline);
  const isFresh = new Set(fresh.map(key));
  for (const f of findings) {
    console.log(`${isFresh.has(key(f)) ? "NEW " : "base"} ${f.page} [${f.viewport}] ${f.rule} ${f.selector} (${f.detail})`);
  }
  for (const b of fixed) console.log(`gone ${b.page} [${b.viewport}] ${b.rule} ${b.selector} (remove from baseline)`);
  const counts = {};
  for (const f of sortFindings(findings)) counts[f.rule] = (counts[f.rule] || 0) + 1;
  console.log(`AUDITED ${pages.length} pages x ${Object.keys(VIEWPORTS).length} viewports: ` +
    `${sortFindings(findings).length} findings (${Object.entries(counts).map(([r, n]) => `${r} ${n}`).join(", ") || "none"}), ` +
    `${sortFindings(fresh).length} new, ${fixed.length} gone; screenshots in ${path.relative(process.cwd(), shots) || "."}`);

  if (update) {
    fs.writeFileSync(BASELINE, JSON.stringify(toBaseline(findings), null, 1) + "\n");
    console.log(`baseline written: ${path.relative(process.cwd(), BASELINE)}`);
    const hard = findings.filter((f) => HARD.has(f.rule));
    if (hard.length) console.log(`FAIL ${hard.length} load failures (page-load, page-error, failed-request) are never baselined`);
    return hard.length ? 1 : 0;
  }
  return fresh.length ? 1 : 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const args = process.argv.slice(2);
  const update = args.includes("--update-baseline");
  const siteDir = args.find((a) => !a.startsWith("--"));
  if (!siteDir) {
    console.error("usage: node demo/audit.mjs <siteDir> [--update-baseline]");
    process.exit(2);
  }
  main(siteDir, update).then((code) => process.exit(code), (e) => {
    console.error(`ERROR ${e.stack || e.message}`);
    process.exit(1);
  });
}
