import { expect, test } from "@playwright/test";

test.describe.configure({ mode: "serial" });

test("operations shows incidents and decisions, and a decision can be approved", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Betrieb" })).toBeVisible();
  await expect(page.getByText("Störungen")).toBeVisible();
  const decisions = page.locator("section", { has: page.getByRole("heading", { name: "Anstehende Entscheidungen" }) });
  const decision = decisions.locator(".item", { hasText: "Firmware 7.2.123 auf sw-werkstatt" });
  await expect(decision).toBeVisible();
  await decision.getByRole("button", { name: /Freigeben/ }).click();
  await expect(page.getByRole("dialog")).toContainText("Firmware auf sw-werkstatt installieren");
  await page.getByRole("button", { name: "Freigeben und ausführen" }).click();
  await expect(page).toHaveURL(/#\/jobs\//);
  await expect(page.locator(".pill", { hasText: "Erledigt" }).first()).toBeVisible({ timeout: 20_000 });
});

test("a guest can be snapshotted through a previewed job", async ({ page }) => {
  await page.goto("/#/infra?tab=guests");
  await page.getByRole("cell", { name: /^fotos/ }).click();
  await expect(page.getByRole("heading", { name: "fotos" })).toBeVisible();
  await page.getByRole("button", { name: "Snapshot" }).click();
  await page.getByRole("button", { name: "Vorschau erstellen" }).click();
  await expect(page.getByRole("dialog")).toContainText("Snapshot");
  await page.getByRole("button", { name: "Freigeben und ausführen" }).click();
  await expect(page.locator(".pill", { hasText: "Erledigt" }).first()).toBeVisible({ timeout: 20_000 });
});

test("the topology renders with excaliplant and elements are selectable", async ({ page }) => {
  await page.goto("/#/network/topology");
  const svg = page.locator(".topo svg");
  await expect(svg).toBeVisible({ timeout: 20_000 });
  await expect(svg).toContainText("sw-werkstatt");
  // excaliplant puts transparent hit areas over each element; click by position like a user.
  const box = await svg.locator("text", { hasText: "sw-werkstatt" }).first().boundingBox();
  if (!box) throw new Error("switch not drawn");
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await expect(page.getByRole("link", { name: "Details öffnen" })).toBeVisible();
});

test("network insights join UniFi and Proxmox facts", async ({ page }) => {
  await page.goto("/#/network");
  await expect(page.getByText(/nutzt VLAN 40, aber sw-werkstatt Port 2 führt es nicht/)).toBeVisible();
  await expect(page.getByText(/läuft mit 100 statt 1000 Mbit\/s/)).toBeVisible();
});

test("path check explains why the guest network is blocked", async ({ page }) => {
  await page.goto("/#/network/path");
  await page.getByLabel("Von").selectOption({ label: "handy-tom (Client)" });
  await page.getByLabel("Nach").selectOption({ label: "medien (VM)" });
  await page.getByRole("button", { name: "Pfad prüfen" }).click();
  await expect(page.getByText("nicht erreichbar")).toBeVisible();
});

test("Zorec answers from live state without a language model", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Zorec fragen" }).click();
  await page.getByRole("button", { name: "Welche Updates gibt es?" }).click();
  await expect(page.locator(".msg").last()).toContainText("Updates verfügbar");
});

test("an application is drafted from a repository", async ({ page }) => {
  await page.goto("/#/apps/new");
  await page.getByRole("button", { name: "Beispiel laden" }).click();
  await expect(page.getByText("Entwurf: Wetterstation")).toBeVisible();
  await expect(page.getByText(/Gib das Geheimnis API_TOKEN/)).toBeVisible();
  await expect(page.getByRole("link", { name: "In einer Sandbox testen" })).toBeVisible();
});

test("an application is installed into a sandbox with isolation preview", async ({ page }) => {
  await page.goto("/#/apps?tab=catalog");
  await page.locator(".app-card", { hasText: "Whoami" }).getByRole("link", { name: "Installieren" }).click();
  await expect(page.getByRole("heading", { name: "Whoami test service installieren" })).toBeVisible();
  for (let i = 0; i < 4; i++) await page.getByRole("button", { name: "Weiter" }).click();
  await expect(page.getByText("Kein Internetzugang.")).toBeVisible();
  await expect(page.locator(".diagram-canvas svg")).toBeVisible({ timeout: 15_000 });
  await page.getByRole("button", { name: "Sandbox starten" }).click();
  await page.getByRole("button", { name: "Freigeben und ausführen" }).click();
  await expect(page.locator(".pill", { hasText: "Erledigt" }).first()).toBeVisible({ timeout: 20_000 });
  await page.goto("/#/apps?tab=sandboxes");
  await expect(page.getByRole("cell", { name: /Whoami test service \(Sandbox\)/ })).toBeVisible();
});

test("theme toggles to dark and the app stays readable", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Darstellung wechseln" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  expect(bg).not.toBe("rgb(243, 246, 245)");
});

test("updates are planned in waves that keep quorum", async ({ page }) => {
  await page.goto("/#/updates");
  await expect(page.getByRole("heading", { name: "Abgestimmter Rollout" })).toBeVisible();
  await expect(page.locator(".steps li").first()).toContainText(/canary/i);
});

test("Zorec opens pages and draws a network path in the side panel", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Zorec fragen" }).click();
  await page.locator("#zorec-q").fill("Öffne die Sicherheit");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/#\/security/);
  await expect(page.getByText("Sicherheit geöffnet")).toBeVisible();
  await page.locator("#zorec-q").fill("Erreicht laptop-anna den drucker?");
  await page.keyboard.press("Enter");
  const canvas = page.getByRole("complementary", { name: "Diagramm" });
  await expect(canvas).toContainText("Weg von laptop-anna nach drucker");
  await expect(canvas.getByRole("img", { name: /Weg von laptop-anna/ })).toBeVisible();
});

test("AI settings show what a permission adds before saving", async ({ page }) => {
  await page.goto("/#/settings?tab=ai");
  await expect(page.getByText("Zorec arbeitet mit Regeln")).toBeVisible();
  await expect(page.getByText("Funktioniert ohne KI")).toBeVisible();
  await page.locator(".cap", { hasText: "Selbst handeln" }).locator("input").check({ force: true });
  const diff = page.locator(".diff");
  await expect(diff).toContainText("+ Behebt Probleme nachts, ohne auf dich zu warten");
  await expect(page.getByText("Ungespeicherte Änderungen")).toBeVisible();
  await page.getByRole("button", { name: "Verwerfen" }).click();
  await expect(diff).toHaveCount(0);
  await expect(page.getByText("gemma4:e2b-it-q4_K_M")).toBeVisible();
});

test("full automation is one click and can be taken back", async ({ page }) => {
  await page.goto("/#/settings?tab=automation");
  await page.getByRole("button", { name: /Volle Automatisierung/ }).click();
  await expect(page.getByRole("button", { name: /Volle Automatisierung/ })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: /Ausgewogen/ }).click();
  await expect(page.getByRole("button", { name: /Ausgewogen/ })).toHaveAttribute("aria-pressed", "true");
});
