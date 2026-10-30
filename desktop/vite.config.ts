import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    watch: { ignored: ["**/src-tauri/**", "**/test-results/**", "**/playwright-report/**"] },
  },
  // Two pages, because the shell is two windows: the main window (`index.html`)
  // and the quick-paste overlay (`quick-paste.html`, window label `quick-paste`,
  // URL `/quick-paste.html`) — the host builds that window once and reuses it
  // for every press.  The entries are given as paths rather than through
  // `node:path`/`import.meta.url`, because this config is type-checked with no
  // Node typings and Vite resolves each input against the project root, which is
  // this directory for both `vite dev` and `vite build`.
  build: {
    rollupOptions: {
      input: {
        index: "index.html",
        quickPaste: "quick-paste.html",
      },
    },
  },
  test: {
    environment: "jsdom",
    // jsdom's default opaque origin has no Storage; the window's relay
    // consent tests use localStorage, and a real browser always has it.
    environmentOptions: { jsdom: { url: "http://localhost/" } },
    include: ["tests/**/*.test.ts"],
    // The relay-transport cases drive a fake clock through promise-dependent
    // WebSocket handshakes; on a busy build host a scheduling hiccup can land
    // one frame between two ticks and fail a case that passes on a retry.  One
    // retry keeps genuine failures red (they fail both times) without making a
    // loaded machine look like a broken release.
    retry: 1,
  },
});
