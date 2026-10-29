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
  test: {
    environment: "jsdom",
    // jsdom's default opaque origin has no Storage; the window's relay
    // consent tests use localStorage, and a real browser always has it.
    environmentOptions: { jsdom: { url: "http://localhost/" } },
    include: ["tests/**/*.test.ts"],
  },
});
