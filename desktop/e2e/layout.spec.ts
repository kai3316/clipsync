import { expect, test } from "@playwright/test";

for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }]) {
  test(`browser isolation and layout ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const failures: string[] = [];
    page.on("pageerror", error => failures.push(error.message));
    await page.goto("/");
    // The window opens on the overview — the sidebar's first row and the page
    // the legacy window opened on — and with no host behind it that page has to
    // say so rather than spin on a runtime that is never going to answer.
    await expect(page.getByRole("heading", { name: "概览", exact: true })).toBeVisible();
    await expect(page.getByRole("alert")).toContainText("NATIVE_HOST_REQUIRED");
    await expect(page.getByText("概览暂不可用")).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(await page.locator(".brand img").evaluate((img: HTMLImageElement) => img.naturalWidth > 0)).toBe(true);
    await page.screenshot({ path: `test-results/layout-${viewport.width}.png`, fullPage: true });
    expect(failures).toEqual([]);
  });
}
