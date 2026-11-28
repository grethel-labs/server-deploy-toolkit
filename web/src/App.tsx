import { useEffect, useMemo, useState } from "react";
import { api, offline, onLive, setCsrf, type Device, type Guest, type Host, type Session } from "./api";
import { Icon } from "./components/Icon";
import { Zorec } from "./components/Zorec";
import { applyTheme, go, href, LangContext, loadCatalog, type Catalog, SessionContext, storedTheme, useData, useRoute, useT, type Lang, type Theme } from "./lib";
import { Cluster } from "./pages/Cluster";
import { DeviceDetail, GuestDetail, HostDetail, ObjectRedirect } from "./pages/Details";
import { Infrastructure } from "./pages/Infrastructure";
import { JobDetail, Jobs } from "./pages/Jobs";
import { Login } from "./pages/Login";
import { Network } from "./pages/Network";
import { Operations } from "./pages/Operations";
import { Security } from "./pages/Security";
import { NewService, Services } from "./pages/Services";
import { Settings } from "./pages/Settings";
import { Updates } from "./pages/Updates";

export function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const refresh = () =>
    api.get<Session>("/api/session").then((s) => {
      setCsrf(s.csrf);
      setSession(s);
    });
  useEffect(() => {
    refresh();
    applyTheme(storedTheme());
  }, []);
  const lang: Lang = session?.lang ?? navigator.language.slice(0, 2);
  useEffect(() => {
    if (session) loadCatalog(lang).then(setCatalog);
  }, [session, lang]);
  if (!session || !catalog) return null;
  document.documentElement.lang = lang;
  return (
    <LangContext.Provider value={{ lang, catalog }}>
      <SessionContext.Provider value={{ session, refresh }}>
        {session.user ? <Shell /> : <Login setup={!!session.setup} onDone={refresh} />}
      </SessionContext.Provider>
    </LangContext.Provider>
  );
}

type Overview = { decisions: unknown[]; incidents: { state: string }[]; working: unknown[] };

function Shell() {
  const t = useT();
  const route = useRoute();
  const [menu, setMenu] = useState(false);
  const [theme, setTheme] = useState<Theme>(storedTheme());
  const { data: ov } = useData<Overview>("/api/overview");
  const [live, setLive] = useState(true);

  useEffect(() => setMenu(false), [route.path]);
  useEffect(() => onLive(() => setLive(true)), []);

  const section = "/" + (route.path.split("/")[1] ?? "");
  const decisions = ov?.decisions.length ?? 0;
  const working = ov?.working.length ?? 0;
  const nav: { group?: string; path: string; icon: string; label: string; badge?: number }[] = [
    { path: "/", icon: "ops", label: t("Operations"), badge: decisions },
    { group: t("Infrastructure"), path: "/infra", icon: "server", label: t("Hosts and guests") },
    { path: "/services", icon: "box", label: t("Services") },
    { path: "/network", icon: "network", label: t("Network") },
    { group: t("Care"), path: "/security", icon: "shield", label: t("Security") },
    { path: "/updates", icon: "refresh", label: "Updates" },
    { path: "/jobs", icon: "list", label: t("Jobs and log") },
    { group: "OpsTend", path: "/cluster", icon: "cluster", label: t("Cluster and roles") },
    { path: "/settings", icon: "settings", label: t("Settings") },
  ];

  function cycleTheme() {
    const next: Theme = theme === "system" ? "dark" : theme === "dark" ? "light" : "system";
    applyTheme(next);
    setTheme(next);
  }

  const page = useMemo(() => {
    const p = route.path;
    if (p === "/") return <Operations />;
    if (p === "/infra") return <Infrastructure />;
    if (p.startsWith("/hosts/")) return <HostDetail id={decodeURIComponent(p.slice(7))} />;
    if (p.startsWith("/guests/")) return <GuestDetail id={decodeURIComponent(p.slice(8))} />;
    if (p.startsWith("/devices/")) return <DeviceDetail id={decodeURIComponent(p.slice(9))} />;
    if (p.startsWith("/object/")) return <ObjectRedirect id={decodeURIComponent(p.slice(8))} />;
    if (p === "/services/new") return <NewService />;
    if (p === "/services") return <Services />;
    if (p.startsWith("/network")) return <Network />;
    if (p === "/security") return <Security />;
    if (p === "/updates") return <Updates />;
    if (p.startsWith("/jobs/")) return <JobDetail id={p.slice(6)} />;
    if (p === "/jobs") return <Jobs />;
    if (p === "/cluster") return <Cluster />;
    if (p === "/settings") return <Settings theme={theme} setTheme={(x) => { applyTheme(x); setTheme(x); }} />;
    return <Operations />;
  }, [route.path, route.params.toString(), theme]);

  const status = working > 0
    ? { busy: true, label: working === 1 ? t("Working on {count} job", { count: working }) : t("Working on {count} jobs", { count: working }) }
    : decisions > 0
      ? { busy: false, label: decisions === 1 ? t("{count} decision open", { count: decisions }) : t("{count} decisions open", { count: decisions }) }
      : { busy: false, label: t("All quiet") };

  return (
    <div className="shell">
      <nav className={`nav${menu ? " open" : ""}`} aria-label={t("Main navigation")}>
        <a className="brand" href={href("/")}>
          <img src="/brand/opstend-dark.svg" alt="" />
          <span><b>OpsTend</b><small>by Grethel Labs</small></span>
        </a>
        {nav.map((n) => (
          <div key={n.path} style={{ display: "contents" }}>
            {n.group && <h4>{n.group}</h4>}
            <a href={href(n.path)} aria-current={section === n.path || (n.path === "/infra" && ["/hosts", "/guests", "/devices"].includes(section)) ? "page" : undefined}>
              <Icon name={n.icon} />{n.label}
              {n.badge ? <span className="badge">{n.badge}</span> : null}
            </a>
          </div>
        ))}
        <div className="foot">OpsTend · {t("Live")}{live ? " ●" : ""}</div>
      </nav>
      <div className="main">
        <header className="top">
          <button className="icon-btn menu-btn" onClick={() => setMenu((m) => !m)} aria-label={t("Menu")}>
            <Icon name="menu" />
          </button>
          <Search />
          <div className="spacer" />
          <button className="icon-btn" onClick={cycleTheme} title={t("Theme: {theme}", { theme })} aria-label={t("Change theme")}>
            <Icon name={theme === "dark" ? "moon" : "sun"} />
          </button>
        </header>
        <main className="content" id="main">
          {offline && <div className="banner warn">{t("Offline: you are seeing the last loaded state.")}</div>}
          {page}
        </main>
      </div>
      <nav className="tabbar" aria-label={t("Quick navigation")}>
        {[
          { path: "/", icon: "ops", label: t("Ops") },
          { path: "/infra", icon: "server", label: t("Systems") },
          { path: "/network", icon: "network", label: t("Network") },
          { path: "/jobs", icon: "list", label: t("Jobs") },
        ].map((n) => (
          <a key={n.path} href={href(n.path)} aria-current={section === n.path ? "page" : undefined}><Icon name={n.icon} size={22} />{n.label}</a>
        ))}
        <a href="#" onClick={(e) => { e.preventDefault(); setMenu(true); }}><Icon name="menu" size={22} />{t("More")}</a>
      </nav>
      <Zorec status={status} />
    </div>
  );
}

function Search() {
  const t = useT();
  const [q, setQ] = useState("");
  const { data: hosts } = useData<Host[]>(q ? "/api/hosts" : null, []);
  const { data: guests } = useData<Guest[]>(q ? "/api/guests" : null, []);
  const { data: net } = useData<{ devices: Device[] }>(q ? "/api/network" : null, []);
  const needle = q.toLowerCase();
  const hits = !q ? [] : [
    ...(hosts ?? []).filter((h) => `${h.name} ${h.address}`.toLowerCase().includes(needle)).map((h) => ({ kind: "Host", name: h.name, to: `/hosts/${encodeURIComponent(h.id)}` })),
    ...(guests ?? []).filter((g) => `${g.name} ${g.native_id} ${g.nics.flatMap((n) => n.ips).join(" ")}`.toLowerCase().includes(needle)).map((g) => ({ kind: g.kind.toUpperCase(), name: g.name, to: `/guests/${encodeURIComponent(g.id)}` })),
    ...(net?.devices ?? []).filter((d) => `${d.name} ${d.model} ${d.address}`.toLowerCase().includes(needle)).map((d) => ({ kind: d.model, name: d.name, to: `/devices/${encodeURIComponent(d.id)}` })),
  ].slice(0, 10);
  return (
    <div className="search">
      <Icon name="search" size={16} />
      <label className="sr-only" htmlFor="global-search">{t("Search")}</label>
      <input id="global-search" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") setQ(""); if (e.key === "Enter" && hits[0]) { go(hits[0].to); setQ(""); } }}
        placeholder={t("Search host, guest, device or address")} autoComplete="off" />
      {hits.length > 0 && (
        <div className="search-results" role="listbox">
          {hits.map((h) => (
            <a key={h.to} href={href(h.to)} onClick={() => setQ("")}><b>{h.name}</b><span className="muted">{h.kind}</span></a>
          ))}
        </div>
      )}
    </div>
  );
}
