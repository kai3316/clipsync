#!/usr/bin/env node
/*
 * Build the phone companion as a static site for Cloudflare Pages.
 *
 * The server normally renders mobile.html per request: it interpolates the
 * access token, the locale JSON and the manifest URL.  A static host has no
 * request to interpolate, so this script produces the same page with
 *
 *   - the locale baked in (Chinese by default, matching the desktop UI),
 *   - the token removed from every URL (remote mode carries it in the fragment
 *     instead; see internal/web/static/remote-transport.js),
 *   - a small manifest and the remote transport injected before the page's own
 *     script so its `fetch` wrapper is installed first.
 *
 * Usage:
 *   node scripts/build-companion-pwa.mjs [--out dist/companion] [--lang zh-CN]
 *   npx wrangler pages deploy dist/companion --project-name=clipsync-companion
 */
import { cp, mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
function option(name, fallback) {
  const index = args.indexOf(name);
  return index === -1 ? fallback : args[index + 1];
}
const outDir = path.resolve(option("--out", path.join(root, "dist", "companion")));
const locale = option("--lang", "zh-CN");

async function exists(file) {
  try {
    await stat(file);
    return true;
  } catch {
    return false;
  }
}

const staticDir = path.join(root, "internal", "web", "static");
let html = await readFile(path.join(staticDir, "mobile.html"), "utf8");
const localeJson = await readFile(path.join(staticDir, "locales", `${locale}.json`), "utf8");

// The server interpolates these two; a static build has no request.  The page's
// `<script>` assigns them, so the values must be JavaScript literals.
html = html
  .replaceAll("__CLIPSYNC_I18N_LOCALE__", JSON.stringify(locale))
  .replaceAll("__CLIPSYNC_I18N_DATA__", localeJson.trim());

// Token-bearing URLs.  Remote mode moves the token into the fragment and the
// transport adopts it into location.search before the page reads it, so static
// asset URLs must not carry one.
html = html
  .replaceAll("?token=__TOKEN__", "")
  .replaceAll("__TOKEN__", "")
  .replace(
    /<link rel="manifest" href="\/manifest\.json[^"]*">/,
    '<link rel="manifest" href="manifest.webmanifest">',
  )
  .replace(
    /<link rel="apple-touch-icon" href="\/icon-192\.png[^"]*">/,
    '<link rel="apple-touch-icon" href="icon.svg">',
  );

// Load before the page's own script (which polls immediately).  The marker also
// survives `?token=` adoption, so a reload stays in remote mode.
const formatTag = '<script src="js/format.js';
if (!html.includes(formatTag)) throw new Error("mobile.html changed: js/format.js tag not found");
html = html.replace(
  formatTag,
  '<script src="remote-transport.js"></script>\n  <script src="js/format.js',
);

await rm(outDir, { recursive: true, force: true });
await mkdir(path.join(outDir, "js"), { recursive: true });
await mkdir(path.join(outDir, "locales"), { recursive: true });

await writeFile(path.join(outDir, "index.html"), html, "utf8");
await writeFile(
  path.join(outDir, "manifest.webmanifest"),
  JSON.stringify(
    {
      name: "ClipSync",
      short_name: "ClipSync",
      start_url: "./",
      scope: "./",
      display: "standalone",
      background_color: "#06080D",
      theme_color: "#05060D",
      icons: [{ src: "icon.svg", sizes: "any", type: "image/svg+xml", purpose: "any" }],
    },
    null,
    2,
  ),
  "utf8",
);

for (const [from, to] of [
  [path.join(staticDir, "remote-transport.js"), "remote-transport.js"],
  [path.join(staticDir, "sw.js"), "sw.js"],
  [path.join(staticDir, "js", "format.js"), path.join("js", "format.js")],
  [path.join(staticDir, "locales", `${locale}.json`), path.join("locales", `${locale}.json`)],
]) {
  await cp(from, path.join(outDir, to));
}

const icon = path.join(root, "assets", "icon.svg");
if (await exists(icon)) await cp(icon, path.join(outDir, "icon.svg"));

// A static page on Pages cannot register a server-rendered manifest, but the
// transport still needs the token before the page script runs; the build has
// produced that.  Verify the assumptions instead of silently shipping a page
// that cannot authenticate.
const produced = await readFile(path.join(outDir, "index.html"), "utf8");
if (produced.includes("__TOKEN__") || produced.includes("__CLIPSYNC_I18N")) {
  throw new Error("build left a server-side placeholder in index.html");
}
if (!produced.includes("remote-transport.js")) {
  throw new Error("build did not inject the remote transport");
}
console.log(`companion PWA built: ${outDir}`);
console.log("deploy: npx wrangler pages deploy " + path.relative(root, outDir) +
  " --project-name=clipsync-companion");
