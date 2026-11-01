import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  outputDir: "./test-results",
  // No `channel`: the CI job installs Playwright's own chromium
  // (`npx playwright install chromium`) and a developer's local chromium is
  // whichever revision Playwright bundles.  Pinning Edge made the suite depend
  // on a browser the ubuntu runner does not carry, which is one reason it was
  // never run there.
  use: { baseURL: "http://127.0.0.1:1420", headless: true },
  webServer: {
    command: "npm run dev",
    url: "http://127.0.0.1:1420",
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
