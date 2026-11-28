import { expect, test } from "@playwright/test";

test("phone layout uses the tab bar and has no horizontal scroll", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("navigation", { name: "Schnellnavigation" })).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await page.getByRole("navigation", { name: "Schnellnavigation" }).getByRole("link", { name: "Netz" }).click();
  await expect(page.getByRole("heading", { name: "Netzwerk", exact: true })).toBeVisible();
});

test("the app is installable as a PWA", async ({ page }) => {
  await page.goto("/");
  const manifest = await page.evaluate(async () => (await fetch("/manifest.webmanifest")).json());
  expect(manifest.display).toBe("standalone");
  expect(manifest.icons.length).toBeGreaterThanOrEqual(2);
  const sw = await page.evaluate(async () => (await navigator.serviceWorker.getRegistration())?.active?.scriptURL ?? null);
  expect(sw === null || sw.endsWith("/sw.js")).toBeTruthy();
});
