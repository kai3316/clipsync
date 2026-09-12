import { chromium, expect } from "@playwright/test";
import { spawn, spawnSync } from "node:child_process";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { createServer } from "node:net";
import { once } from "node:events";

if (process.platform !== "win32") throw new Error("This smoke runner targets Windows WebView2");
const root = resolve(import.meta.dirname, "../..");
const output = resolve(root, "desktop/test-results");
const useLauncher = process.env.CLIPSYNC_SMOKE_LAUNCHER === "1";
const nativeExecutable = process.env.CLIPSYNC_NATIVE_EXE
  ? resolve(process.env.CLIPSYNC_NATIVE_EXE)
  : resolve(root, "desktop/src-tauri/target/debug/clipsync-desktop.exe");
await mkdir(output, { recursive: true });
let devServer;
let directPort = 1420;
let directConfig;
if (!useLauncher) {
  const portProbe = createServer();
  portProbe.listen(0, "127.0.0.1");
  await once(portProbe, "listening");
  directPort = portProbe.address().port;
  await new Promise(done => portProbe.close(done));
  devServer = spawn(process.execPath, [
    resolve(root, "desktop/node_modules/vite/bin/vite.js"), "--host", "127.0.0.1",
    "--port", String(directPort), "--strictPort",
  ], { cwd: resolve(root, "desktop"), windowsHide: true, stdio: "ignore" });
  let ready = false;
  for (let attempt = 0; attempt < 75; attempt++) {
    try {
      ready = (await fetch(`http://127.0.0.1:${directPort}`)).ok;
      if (ready) break;
    } catch {}
    await new Promise(done => setTimeout(done, 200));
  }
  if (!ready) {
    devServer.kill();
    throw new Error("Could not start the native test's development server");
  }
  const baseConfig = JSON.parse(await (await import("node:fs/promises")).readFile(
    resolve(root, "desktop/src-tauri/tauri.conf.json"), "utf8",
  ));
  baseConfig.build.beforeDevCommand = "node -e \"process.exit(0)\"";
  baseConfig.build.devUrl = `http://127.0.0.1:${directPort}`;
  baseConfig.app.security.devCsp = baseConfig.app.security.devCsp
    .replace("ws://127.0.0.1:1420", `ws://127.0.0.1:${directPort}`);
  directConfig = resolve(root, "build", `tauri-smoke-${process.pid}.json`);
  await mkdir(resolve(root, "build"), { recursive: true });
  await writeFile(directConfig, JSON.stringify(baseConfig), "utf8");
}
process.on("exit", () => devServer?.kill());
const data = await mkdtemp(resolve(tmpdir(), "clipsync-native-smoke-"));
const python = process.env.CLIPSYNC_PYTHON || "python";
const cargoBin = resolve(process.env.USERPROFILE || "C:/Users/Default", ".cargo/bin");
const env = {
  ...process.env,
  PATH: `${cargoBin}${process.platform === "win32" ? ";" : ":"}${process.env.PATH || ""}`,
  CLIPSYNC_CONFIG_DIR: data,
  CLIPSYNC_PYTHON: python,
};
const lanPortServer = createServer();
lanPortServer.listen(0, "127.0.0.1");
await once(lanPortServer, "listening");
const lanPort = lanPortServer.address().port;
await new Promise(done => lanPortServer.close(done));
const seed = spawnSync(python, ["-c", [
  "from internal.config.config import Config, save",
  "from internal.clipboard.history_db import ClipboardHistoryDB",
  "from internal.clipboard.format import ClipboardContent, ContentType",
  `save(Config(encryption_enabled=False, sync_enabled=False, source_tracking_enabled=False, port=${lanPort}, device_name='Native smoke device'))`,
  "h=ClipboardHistoryDB()",
  "h.add(ClipboardContent(types={ContentType.TEXT: b'Tauri native IPC smoke record'}))",
  "h.close()",
].join("; ")], { cwd: root, env, encoding: "utf8", windowsHide: true });
if (seed.status !== 0) throw new Error(`Fixture creation failed: ${seed.stderr}`);

const portServer = createServer();
portServer.listen(0, "127.0.0.1");
await once(portServer, "listening");
const port = portServer.address().port;
await new Promise(done => portServer.close(done));
// Exercise the launcher's alternate-port path instead of reusing arbitrary pages.
let occupiedPortServer;
if (useLauncher) {
  occupiedPortServer = createServer(socket => socket.destroy());
  await new Promise(done => {
    occupiedPortServer.once("error", done);
    occupiedPortServer.listen(1420, "127.0.0.1", done);
  });
}
const launcherShell = resolve(
  process.env.SystemRoot || "C:/Windows", "System32/WindowsPowerShell/v1.0/powershell.exe",
);
const tauriCli = resolve(root, "desktop/node_modules/@tauri-apps/cli/tauri.js");
const child = spawn(
  useLauncher ? launcherShell : process.execPath,
  useLauncher ? [
    "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
    "-File", resolve(root, "scripts/start-desktop.ps1"),
  ] : [tauriCli, "dev", "--no-watch", "--config", directConfig], {
  // Verify that the launcher resolves the repository independently of cwd.
  cwd: useLauncher ? tmpdir() : root, windowsHide: true, stdio: ["ignore", "pipe", "pipe"],
  env: { ...env, WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-port=${port}` },
});
let diagnostics = "";
child.stderr.on("data", chunk => { diagnostics = (diagnostics + chunk).slice(-4000); });
child.stdout.on("data", chunk => { diagnostics = (diagnostics + chunk).slice(-4000); });
const exited = once(child, "exit");
let browser;
try {
  for (let attempt = 0; attempt < (useLauncher ? 1500 : 100); attempt++) {
    if (child.exitCode !== null) throw new Error(`Native process exited: ${diagnostics}`);
    try {
      browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
      break;
    } catch {
      await new Promise(done => setTimeout(done, 200));
    }
  }
  if (!browser) throw new Error(`WebView debugging endpoint unavailable: ${diagnostics}`);
  let page;
  for (let attempt = 0; attempt < 100; attempt++) {
    page = browser.contexts().flatMap(context => context.pages())
      .find(candidate => {
        const url = candidate.url();
        return url && !url.startsWith("devtools://") && url !== "about:blank";
      });
    if (page) break;
    await new Promise(done => setTimeout(done, 200));
  }
  if (!page) throw new Error("ClipSync WebView page not found");
  if (useLauncher && new URL(page.url()).port === "1420") {
    throw new Error("Launcher did not avoid the occupied development port");
  }
  await expect(page.locator(".connection")).toHaveText("已连接", { timeout: 25_000 });
  await expect(page.locator(".history-content")).toContainText("Tauri native IPC smoke record");
  await page.getByRole("button", { name: "收藏库", exact: true }).click();
  await expect(page.getByRole("heading", { name: "暂无收藏" })).toBeVisible();
  await page.getByRole("button", { name: "新建收藏", exact: true }).click();
  await page.locator("#favorite-title").fill("Native favorite");
  await page.locator("#favorite-group").fill("Smoke");
  await page.locator("#favorite-content").fill("Full native favorite content");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.locator(".favorite-row")).toContainText("Native favorite");
  await page.getByRole("button", { name: "复制收藏 Native favorite", exact: true }).click();
  await expect(page.locator(".favorite-row")).toContainText("Native favorite");
  await page.getByRole("button", { name: "删除收藏 Native favorite", exact: true }).click();
  await page.getByRole("button", { name: "删除收藏", exact: true }).last().click();
  await expect(page.getByRole("heading", { name: "暂无收藏" })).toBeVisible();
  await page.getByRole("button", { name: "剪贴板历史", exact: true }).click();
  await page.getByRole("button", { name: "收藏", exact: true }).click();
  await expect(page.getByRole("button", { name: "取消收藏", exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("button", { name: "取消收藏", exact: true })).toBeVisible();
  await page.screenshot({ path: resolve(output, "native-history.png"), fullPage: true });
  for (const [system, theme, background] of [
    ["dark", "light", "rgb(255, 255, 255)"],
    ["light", "dark", "rgb(32, 36, 35)"],
    ["dark", "system", "rgb(32, 36, 35)"],
    ["light", "system", "rgb(255, 255, 255)"],
  ]) {
    await page.emulateMedia({ colorScheme: system });
    await page.evaluate(mode => { document.documentElement.dataset.theme = mode; }, theme);
    await expect.poll(() => page.evaluate(() =>
      getComputedStyle(document.documentElement).backgroundColor)).toBe(background);
    await page.screenshot({ path: resolve(output, `native-theme-${system}-${theme}.png`), fullPage: true });
  }
  const denied = await page.evaluate(async () => {
    try {
      await window.__TAURI_INTERNALS__.invoke("plugin:shell|execute", { cmd: "whoami" });
      return false;
    } catch { return true; }
  });
  if (!denied) throw new Error("Renderer unexpectedly obtained shell capability");
  await page.getByRole("button", { name: "删除记录", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("button", { name: "删除", exact: true }).click();
  await expect(page.getByRole("heading", { name: "暂无历史记录" })).toBeVisible();
  const status = await page.evaluate(() => window.__TAURI_INTERNALS__.invoke("get_app_status"));
  if (status.sync_state !== "paused") throw new Error("Isolated fixture must keep sync paused");
  if (!status.capabilities.includes("sync.set_enabled")) throw new Error("LAN runtime was not started");
  const portProbe = createServer();
  portProbe.listen(0, "127.0.0.1");
  await once(portProbe, "listening");
  const companionPort = portProbe.address().port;
  await new Promise(done => portProbe.close(done));
  try {
    const companion = await page.evaluate(port =>
      window.__TAURI_INTERNALS__.invoke("configure_companion", {
        enabled: true, port, rotateToken: false,
      }), companionPort);
    if (!companion.running || companion.actual_port !== companionPort) {
      throw new Error("Native Companion did not start");
    }
    const access = new URL(companion.access_url);
    access.hostname = "127.0.0.1";
    const mobile = await fetch(access, { signal: AbortSignal.timeout(5000) });
    if (!mobile.ok || !(await mobile.text()).toLowerCase().includes("<html")) {
      throw new Error("Native Companion mobile page failed");
    }
    const deniedMobile = await fetch(`http://127.0.0.1:${companionPort}/mobile.html`, {
      signal: AbortSignal.timeout(5000),
    });
    if (![401, 403].includes(deniedMobile.status)) throw new Error("Companion allowed unauthenticated access");
  } finally {
    const stopped = await page.evaluate(port =>
      window.__TAURI_INTERNALS__.invoke("configure_companion", {
        enabled: false, port, rotateToken: false,
      }), companionPort);
    if (stopped.running || stopped.access_url) throw new Error("Native Companion did not stop");
  }
  await page.evaluate(() => { void window.__TAURI_INTERNALS__.invoke("quit_app"); });
  await Promise.race([
    exited,
    new Promise((_, reject) => setTimeout(() => reject(new Error("Native exit timed out")), 12_000)),
  ]);
  const reopened = spawnSync(python, ["-m", "src.sidecar_main", "--history-only"], {
    cwd: root, env, windowsHide: true, encoding: "utf8", timeout: 15_000,
    input: '{"type":"request","id":"h","method":"history.list","params":{}}\n',
  });
  if (reopened.status !== 0) throw new Error("Native exit left the sidecar/data lock behind");
  const frames = reopened.stdout.trim().split("\n").map(line => JSON.parse(line));
  if (frames[1].result.total !== 0) throw new Error("Delete did not persist in the real repository");
  console.log(JSON.stringify({
    favoritesCrud: "PASS",
    nativeThemeOverrides: "PASS",
    nativeCompanionHttp: "PASS",
    launcher: useLauncher ? "PASS" : "not exercised",
    nativeIPC: "PASS", pinReload: "PASS", deletePersistence: "PASS",
    shellDenied: "PASS", shutdownReopen: "PASS", screenshot: resolve(output, "native-history.png"),
  }, null, 2));
} finally {
  await browser?.close().catch(() => {});
  if (child.exitCode === null) {
    child.kill();
    await Promise.race([exited, new Promise(done => setTimeout(done, 3000))]);
  }
  if (devServer && devServer.exitCode === null) {
    const serverExit = once(devServer, "exit");
    devServer.kill();
    await serverExit;
  }
  if (occupiedPortServer?.listening) {
    await new Promise(done => occupiedPortServer.close(done));
  }
  if (directConfig) await rm(directConfig, { force: true });
  // Keep the isolated fixture for investigating a failed native test. It contains
  // only the synthetic record above, never the user's database.
}
