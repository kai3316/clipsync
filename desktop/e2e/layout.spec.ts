import { expect, test } from "@playwright/test";

for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }]) {
  test(`browser isolation and layout ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const failures: string[] = [];
    page.on("pageerror", error => failures.push(error.message));
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "剪贴板历史", exact: true })).toBeVisible();
    await expect(page.getByRole("alert")).toContainText("NATIVE_HOST_REQUIRED");
    await expect(page.getByRole("button", { name: "刷新", exact: true })).toBeDisabled();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(await page.locator(".brand img").evaluate((img: HTMLImageElement) => img.naturalWidth > 0)).toBe(true);
    await page.screenshot({ path: `test-results/layout-${viewport.width}.png`, fullPage: true });
    expect(failures).toEqual([]);
  });
}
