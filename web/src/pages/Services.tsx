import { useEffect, useState } from "react";
import { api, type Job, type Service } from "../api";
import { Icon } from "../components/Icon";
import { Card, Empty, HealthPill, Loading, useJobFlow } from "../components/ui";
import { go, href, useCan, useData, useRoute, useT } from "../lib";

export function Services() {
  const t = useT();
  const canAct = useCan("operator");
  const { data } = useData<Service[]>("/api/services");
  if (!data) return <Loading />;
  const apps = data.filter((s) => !s.core);
  const core = data.filter((s) => s.core);
  const table = (rows: Service[]) => (
    <div className="table-wrap"><table>
      <thead><tr><th>{t("Service")}</th><th>{t("Runs on")}</th><th className="hide-sm">{t("Reachable at")}</th><th>{t("State")}</th></tr></thead>
      <tbody>{rows.map((s) => (
        <tr key={s.id} className={s.guest_id ? "link" : ""} onClick={() => s.guest_id && go(`/guests/${encodeURIComponent(s.guest_id)}`)}>
          <td><b>{s.name}</b><span className="sub">{s.description}{s.version ? ` · ${s.version}` : ""}</span></td>
          <td>{s.guest?.name ?? "–"}<span className="sub">{s.host_name ?? ""}</span></td>
          <td className="hide-sm mono small">{s.endpoints[0] ?? "–"}{s.exposed && <span className="pill warn plain" style={{ marginLeft: 6 }}>{t("public")}</span>}</td>
          <td><HealthPill health={s.health} /></td>
        </tr>
      ))}</tbody>
    </table></div>
  );
  return (
    <>
      <div className="page-head">
        <div><h1>{t("Services")}</h1><p className="muted">{t("Applications and the services OpsTend runs itself.")}</p></div>
        {canAct && <a className="btn primary" href={href("/services/new")}><Icon name="plus" />{t("New service")}</a>}
      </div>
      <Card title={t("Applications")} flush>{apps.length ? table(apps) : <Empty>–</Empty>}</Card>
      <Card title={t("Core services")} sub={t("Control plane, time, DNS, proxy and Zorec")} flush>{table(core)}</Card>
    </>
  );
}

type Env = { name: string; value: string | null; secret: boolean; required: boolean };
type Port = { container: number; protocol: string; exposure: "internal" | "lan" | "public"; hostname: string | null };
type Svc = { name: string; image: string | null; build_context: string | null; ports: Port[]; volumes: { name: string; path: string; backup: boolean }[]; env: Env[]; cpus: number; memory_mb: number; health: { path: string; port: number | null } | null };
type Spec = { name: string; source: string | null; revision: string | null; services: Svc[]; notes: string[]; questions: string[]; confidence: number };
type Analysis = { spec: Spec; violations: { service: string; message: string; blocking: boolean }[]; files: string[]; suggested_host: string | null; hosts: { id: string; name: string; os: string }[] };

export function NewService() {
  const t = useT();
  const { params } = useRoute();
  const [repo, setRepo] = useState(params.get("repo") ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Analysis | null>(null);
  const [spec, setSpec] = useState<Spec | null>(null);
  const [host, setHost] = useState("");
  const flow = useJobFlow();

  async function analyze(url = repo) {
    setBusy(true);
    setError(null);
    try {
      const r = await api.post<Analysis>("/api/deploy/analyze", { repo: url });
      setResult(r);
      setSpec(r.spec);
      setHost(r.suggested_host ?? "");
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  }
  useEffect(() => { if (params.get("repo")) analyze(params.get("repo")!); }, []);

  function update(i: number, fn: (s: Svc) => Svc) {
    if (!spec) return;
    setSpec({ ...spec, services: spec.services.map((s, j) => (j === i ? fn(s) : s)) });
  }

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow"><a href={href("/services")}>{t("Services")}</a></div>
          <h1>{t("New service from a repository")}</h1>
          <p className="muted">{t("Zorec reads the Compose file or Containerfile, proposes a safe configuration and shows what will happen before anything starts.")}</p>
        </div>
      </div>
      <Card>
        <form className="row" onSubmit={(e) => { e.preventDefault(); analyze(); }}>
          <label className="sr-only" htmlFor="repo">Repository</label>
          <input id="repo" className="field" style={{ flex: 1, minWidth: 240 }} value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="https://github.com/owner/repo" />
          <button className="btn primary" disabled={busy || !repo}>{busy ? t("Reading …") : t("Analyze")}</button>
          <button type="button" className="btn ghost" onClick={() => { setRepo("https://github.com/werkstatt/wetterstation"); analyze("https://github.com/werkstatt/wetterstation"); }}>{t("Load example")}</button>
        </form>
        {error && <div className="banner warn" style={{ marginTop: 12 }}>{error}</div>}
      </Card>

      {result && spec && (
        <>
          <Card title={t("Proposal")} sub={`${t("Read")}: ${result.files.join(", ")}${spec.revision ? ` · ${spec.revision.slice(0, 12)}` : ""}`}>
            <div className="stack">
              {spec.notes.map((n) => <div key={n} className="banner">{n}</div>)}
              {spec.questions.map((q) => <div key={q} className="banner warn">{q}</div>)}
              {result.violations.map((v, i) => <div key={i} className={`banner${v.blocking ? " warn" : ""}`}>{v.service}: {v.message}</div>)}
            </div>
          </Card>
          {spec.services.map((s, i) => (
            <Card key={s.name} title={s.name} sub={s.image ?? (s.build_context === "." ? t("Built from the repository") : t("Built from {build_context}", { build_context: s.build_context }))}>
              <div className="grid cols-2">
                <div className="stack">
                  <h3>{t("Ports")}</h3>
                  {s.ports.length === 0 && <span className="muted small">{t("None")}</span>}
                  {s.ports.map((p, j) => (
                    <div key={j} className="row">
                      <span className="mono" style={{ minWidth: 70 }}>{p.container}/{p.protocol}</span>
                      <select className="field" style={{ width: "auto" }} value={p.exposure} onChange={(e) => update(i, (x) => ({ ...x, ports: x.ports.map((q, k) => k === j ? { ...q, exposure: e.target.value as Port["exposure"] } : q) }))}>
                        <option value="internal">{t("internal only")}</option>
                        <option value="lan">{t("on the LAN")}</option>
                        <option value="public">{t("public via HTTPS")}</option>
                      </select>
                      {p.exposure === "public" && <input className="field" style={{ flex: 1 }} placeholder="app.example.org" value={p.hostname ?? ""} onChange={(e) => update(i, (x) => ({ ...x, ports: x.ports.map((q, k) => k === j ? { ...q, hostname: e.target.value } : q) }))} />}
                    </div>
                  ))}
                  <h3 style={{ marginTop: 10 }}>{t("Data")}</h3>
                  {s.volumes.length === 0 && <span className="muted small">{t("No persistent data")}</span>}
                  {s.volumes.map((v) => <span key={v.name} className="small"><b>{v.name}</b> → <span className="mono">{v.path}</span> {v.backup && <span className="pill ok plain">{t("backed up")}</span>}</span>)}
                </div>
                <div className="stack">
                  <h3>{t("Settings")}</h3>
                  {s.env.map((e) => (
                    <label key={e.name} className="lbl">{e.name}{e.secret && <span className="help">{t("Secret – stored encrypted, never in the job")}</span>}
                      <input className="field" type={e.secret ? "password" : "text"} defaultValue={e.secret ? "" : e.value ?? ""} autoComplete="off"
                        onChange={(ev) => !e.secret && update(i, (x) => ({ ...x, env: x.env.map((q) => q.name === e.name ? { ...q, value: ev.target.value } : q) }))} />
                    </label>
                  ))}
                  <div className="row">
                    <label className="lbl" style={{ flex: 1 }}>CPU<input className="field" type="number" step="0.25" min="0.25" value={s.cpus} onChange={(e) => update(i, (x) => ({ ...x, cpus: Number(e.target.value) }))} /></label>
                    <label className="lbl" style={{ flex: 1 }}>{t("Memory (MB)")}<input className="field" type="number" step="64" min="64" value={s.memory_mb} onChange={(e) => update(i, (x) => ({ ...x, memory_mb: Number(e.target.value) }))} /></label>
                  </div>
                </div>
              </div>
            </Card>
          ))}
          <Card title={t("Where should it run?")}>
            <div className="row">
              <select className="field" style={{ width: "auto" }} value={host} onChange={(e) => setHost(e.target.value)}>
                {result.hosts.map((h) => <option key={h.id} value={h.id}>{h.name} · {h.os}</option>)}
              </select>
              <span className="muted small">{t("Suggested by free memory. Every container runs rootless, without capabilities, with limits and its own network.")}</span>
              <span style={{ flex: 1 }} />
              <button className="btn primary" disabled={!host} onClick={() => flow.start(() => api.post<Job>("/api/deploy", { spec, host }))}>{t("Create preview")}</button>
            </div>
          </Card>
        </>
      )}
      {flow.dialog}
    </>
  );
}
