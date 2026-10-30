// A tiny static server that behaves like GitHub Pages for the built demo:
//   - the site lives under a base path (e.g. /AniDex/); anything outside it is 404
//   - /dir/ serves dir/index.html; /dir (no slash) redirects 301 to /dir/
//   - a missing file answers 404 with the site's 404.html
//   - HTML is sent as "text/html; charset=utf-8"
//
//   node demo/serve.mjs <siteDir> [port]      (base read from <siteDir>/pages.json;
//                                             HOST=0.0.0.0 to listen beyond loopback)
//
// Used by check.mjs, audit.mjs and the screenshot capture.

import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { pathToFileURL } from "node:url";

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".webmanifest": "application/manifest+json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".woff2": "font/woff2",
  ".woff": "font/woff",
  ".txt": "text/plain; charset=utf-8",
};

export function contentType(file) {
  return TYPES[path.extname(file).toLowerCase()] || "application/octet-stream";
}

/** Normalises a base path to "/x/" form ("/" stays "/"). */
export function normaliseBase(base) {
  let b = base || "/";
  if (!b.startsWith("/")) b = "/" + b;
  if (!b.endsWith("/")) b += "/";
  return b;
}

/** The base path recorded in <siteDir>/pages.json, or "/". */
export function siteBase(siteDir) {
  try {
    return normaliseBase(JSON.parse(fs.readFileSync(path.join(siteDir, "pages.json"), "utf8")).base);
  } catch {
    return "/";
  }
}

/** The recorded pages, as paths under the base ("/", "/queue", ...). */
export function sitePages(siteDir) {
  const manifest = JSON.parse(fs.readFileSync(path.join(siteDir, "pages.json"), "utf8"));
  return manifest.pages.map((p) => p.page);
}

/** "/queue" -> "queue/", "/" -> "": the URL of a page relative to the base. */
export function pageUrl(page) {
  const p = page.replace(/^\/+/, "");
  return p === "" || p.endsWith("/") || p.endsWith(".html") ? p : p + "/";
}

function handler(root, base) {
  const notFound = path.join(root, "404.html");
  const send = (res, status, file, headers = {}) => {
    res.writeHead(status, { "content-type": contentType(file), "cache-control": "no-store", ...headers });
    fs.createReadStream(file).pipe(res);
  };
  const miss = (res) => {
    if (fs.existsSync(notFound)) return send(res, 404, notFound);
    res.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
    res.end("404 Not Found\n");
  };
  return (req, res) => {
    if (req.method !== "GET" && req.method !== "HEAD") {
      res.writeHead(405, { allow: "GET, HEAD" });
      return res.end();
    }
    let pathname;
    try {
      pathname = decodeURIComponent(new URL(req.url, "http://x").pathname);
    } catch {
      return miss(res);
    }
    if (pathname + "/" === base) {
      res.writeHead(301, { location: base });
      return res.end();
    }
    if (!pathname.startsWith(base)) return miss(res);
    const rel = pathname.slice(base.length);
    const file = path.resolve(root, rel);
    if (file !== root && !file.startsWith(root + path.sep)) return miss(res);
    let stat;
    try {
      stat = fs.statSync(file);
    } catch {
      return miss(res);
    }
    if (stat.isDirectory()) {
      if (!pathname.endsWith("/")) {
        res.writeHead(301, { location: pathname + "/" });
        return res.end();
      }
      const index = path.join(file, "index.html");
      return fs.existsSync(index) ? send(res, 200, index) : miss(res);
    }
    return send(res, 200, file);
  };
}

/**
 * Serves <siteDir> under <base> on <host> (default 127.0.0.1). Resolves to
 * { origin: "http://127.0.0.1:PORT", url: origin + base, close() }.
 */
export function startServer(siteDir, base = siteBase(siteDir), port = 0, host = "127.0.0.1") {
  const root = path.resolve(siteDir);
  const b = normaliseBase(base);
  const server = http.createServer(handler(root, b));
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, host, () => {
      const origin = `http://${host === "0.0.0.0" ? "127.0.0.1" : host}:${server.address().port}`;
      resolve({
        origin,
        url: origin + b,
        close: () => new Promise((r) => { server.closeAllConnections?.(); server.close(() => r()); }),
      });
    });
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const [siteDir, port] = process.argv.slice(2);
  if (!siteDir) {
    console.error("usage: node demo/serve.mjs <siteDir> [port]");
    process.exit(2);
  }
  const s = await startServer(siteDir, siteBase(siteDir), Number(port || 8000), process.env.HOST || "127.0.0.1");
  console.log(`serving ${path.resolve(siteDir)} at ${s.url}`);
}
