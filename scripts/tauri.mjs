#!/usr/bin/env node
// Standard `npm run tauri` entry.
//
// `desktop/src-tauri/tauri.conf.json` deliberately keeps a placeholder version
// (0.1.0): the real version lives in `internal/version.py`, and CI plus
// `scripts/build-tauri.ps1` write `build/tauri-package.json` before a release
// build.  A bare `npm exec tauri build` did not, so it produced
// `ClipSync_0.1.0_*` bundles and no staged sidecar.  This wrapper runs the same
// generator CI runs for a `build`, forwards the generated `--config`, and
// leaves every other subcommand (`dev`, `icon`, ...) untouched.
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const desktopDir = resolve(dirname(fileURLToPath(import.meta.url)), "..", "desktop");
const rootDir = resolve(desktopDir, "..");
const args = process.argv.slice(2);

function findPython() {
  const candidates = [];
  if (process.env.PYTHON) candidates.push(process.env.PYTHON);
  if (process.platform === "win32") {
    candidates.push("python.exe", "python");
  } else {
    candidates.push("python3", "python");
  }
  for (const candidate of candidates) {
    const probe = spawnSync(candidate, ["--version"], { stdio: "ignore" });
    if (!probe.error && probe.status === 0) return candidate;
  }
  return null;
}

const hasConfig = args.some(
  (arg) => arg === "--config" || arg === "-c" || arg.startsWith("--config="),
);
if (args[0] === "build" && !hasConfig) {
  const python = findPython();
  if (!python) {
    console.error("tauri: no Python interpreter found; set PYTHON or put python on PATH");
    process.exit(1);
  }
  const generated = spawnSync(
    python,
    [join(rootDir, "scripts", "make-package-config.py")],
    { cwd: desktopDir, stdio: "inherit" },
  );
  if (generated.error || generated.status !== 0) {
    console.error("tauri: could not generate build/tauri-package.json");
    process.exit(generated.status ?? 1);
  }
  args.push("--config", join(rootDir, "build", "tauri-package.json"));
}

const cli = join(desktopDir, "node_modules", "@tauri-apps", "cli", "tauri.js");
if (!existsSync(cli)) {
  console.error("tauri: desktop/node_modules is missing; run `npm ci` in desktop/ first");
  process.exit(1);
}
const child = spawnSync(process.execPath, [cli, ...args], {
  cwd: desktopDir,
  stdio: "inherit",
});
process.exit(child.status ?? 1);