import { createContext, useCallback, useContext, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { api, onLive, type Session } from "./api";

// --- language -------------------------------------------------------------------
// One catalog per language lives on the server (src/opstend/locales/<lang>.json) and is
// shared by the API and the web app. English is the source text and the fallback.

export type Lang = string;
export type Catalog = Record<string, string>;
export const LangContext = createContext<{ lang: Lang; catalog: Catalog }>({ lang: "en", catalog: {} });

export function format(template: string, params?: Record<string, unknown>) {
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (m, key: string) => (key in params ? String(params[key]) : m));
}

/** t("Restart {service}", { service }) – English source text, translated from the catalog. */
export function useT() {
  const { catalog } = useContext(LangContext);
  return useCallback(
    (template: string, params?: Record<string, unknown>) => format(catalog[template] ?? template, params),
    [catalog],
  );
}

export function useLang() {
  return useContext(LangContext).lang;
}

export async function loadCatalog(lang: Lang): Promise<Catalog> {
  if (lang === "en") return {};
  try {
    const res = await fetch(`/api/i18n/${encodeURIComponent(lang)}`);
    return res.ok ? await res.json() : {};
  } catch {
    return {};
  }
}

// --- session --------------------------------------------------------------------

export const SessionContext = createContext<{ session: Session; refresh: () => void } | null>(null);

export function useSession() {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("no session");
  return ctx;
}

export function useCan(role: "viewer" | "operator" | "admin") {
  const { session } = useSession();
  const rank = { viewer: 0, operator: 1, admin: 2 };
  return rank[session.role ?? "viewer"] >= rank[role];
}

// --- routing --------------------------------------------------------------------

function readHash() {
  const raw = window.location.hash.replace(/^#/, "") || "/";
  const [path, query = ""] = raw.split("?");
  return { path: path || "/", params: new URLSearchParams(query) };
}

function subscribe(cb: () => void) {
  window.addEventListener("hashchange", cb);
  return () => window.removeEventListener("hashchange", cb);
}

export function useRoute() {
  const hash = useSyncExternalStore(subscribe, () => window.location.hash, () => "");
  return useMemo(() => readHash(), [hash]);
}

export function go(path: string) {
  window.location.hash = path;
}

export function href(path: string) {
  return `#${path}`;
}

// --- data -----------------------------------------------------------------------

/** Load JSON from the API and refresh it on live events. */
export function useData<T>(path: string | null, live: string[] = ["job", "tick", "incidents", "event"]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const reload = useCallback(() => setVersion((v) => v + 1), []);

  useEffect(() => {
    if (!path) return;
    let alive = true;
    api.get<T>(path).then(
      (d) => alive && (setData(d), setError(null)),
      (e) => alive && setError(String(e.message ?? e)),
    );
    return () => {
      alive = false;
    };
  }, [path, version]);

  useEffect(() => {
    if (!path || live.length === 0) return;
    let timer: number | undefined;
    const off = onLive((e) => {
      if (!live.includes(e.type)) return;
      window.clearTimeout(timer);
      timer = window.setTimeout(reload, 250);
    });
    return () => {
      off();
      window.clearTimeout(timer);
    };
  }, [path, reload, live.join(",")]);

  return { data, error, reload };
}

// --- formatting -----------------------------------------------------------------

export function gb(mb: number | null | undefined) {
  if (mb == null) return "–";
  return mb >= 1024 ? `${(mb / 1024).toFixed(mb >= 10240 ? 0 : 1)} GiB` : `${mb} MiB`;
}

export function pct(v: number | null | undefined) {
  return v == null ? "–" : `${Math.round(v)} %`;
}

export function speed(mbps: number | null | undefined) {
  if (!mbps) return "–";
  return mbps >= 1000 ? `${mbps / 1000} G` : `${mbps} M`;
}

export function when(iso: string | null | undefined, lang: Lang) {
  if (!iso) return "–";
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  const rtf = new Intl.RelativeTimeFormat(lang, { numeric: "auto" });
  if (Math.abs(diff) < 60) return rtf.format(-Math.round(diff), "second");
  if (Math.abs(diff) < 3600) return rtf.format(-Math.round(diff / 60), "minute");
  if (Math.abs(diff) < 86400) return rtf.format(-Math.round(diff / 3600), "hour");
  return d.toLocaleString(lang, { dateStyle: "medium", timeStyle: "short" });
}

export function clock(iso: string, lang: Lang) {
  return new Date(iso).toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// --- theme ------------------------------------------------------------------------

export type Theme = "system" | "light" | "dark";

export function applyTheme(theme: Theme) {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
  try {
    localStorage.setItem("opstend.theme", theme);
  } catch {
    /* private mode */
  }
}

export function storedTheme(): Theme {
  try {
    return (localStorage.getItem("opstend.theme") as Theme) || "system";
  } catch {
    return "system";
  }
}

export function isDark() {
  const attr = document.documentElement.getAttribute("data-theme");
  if (attr) return attr === "dark";
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}
