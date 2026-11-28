import { useEffect, useMemo, useState } from "react";
import { api, type Guest, type Job, type Service } from "../api";
import { Diagram } from "../components/Diagram";
import { Icon } from "../components/Icon";
import { Card, Dialog, Empty, HealthPill, Loading, Pill, Tabs, useJobFlow } from "../components/ui";
import { go, href, useCan, useData, useRoute, useT, when, useLang } from "../lib";

// --- types --------------------------------------------------------------------------------

type Port = { container: number; protocol: string; exposure: "internal" | "lan" | "public"; hostname: string | null };
type Svc = { name: string; image: string | null; ports: Port[]; volumes: { name: string; path: string; backup: boolean }[]; cpus: number; memory_mb: number };
type Setting = { name: string; label: string; type: "string" | "number" | "boolean" | "secret" | "select"; default: unknown; required: boolean; help: string; options: string[] };
type DeviceReq = { kind: "gpu" | "usb" | "display" | "serial" | "audio"; selector: string; optional: boolean; reason: string };
type Network = { egress: "none" | "allowlist" | "internet"; egress_hosts: string[] };
type Connection = { to_workload: string; to_service: string; port: number; reason: string };
type Adapter = {
  id: string; name: string; version: string; description: string; category: string; status: string; homepage: string | null;
  services: Svc[]; settings: Setting[]; resources: { cpus: number; memory_mb: number; storage_gb: number };
  devices: DeviceReq[]; network: Network; architectures: string[]; notes: string[]; installed?: boolean; origin: string | null;
};
type Test = { name: string; ok?: boolean; detail?: string };
type Workload = {
  id: string; name: string; adapter: string | null; environment: "production" | "sandbox"; state: string; host_id: string | null;
  services: Svc[]; settings: Record<string, unknown>; secrets: Record<string, string>; resources: { cpus: number; memory_mb: number; storage_gb: number };
  devices: DeviceReq[]; network: Network; connections: Connection[]; expires_at: string | null; sandbox_of: string | null;
  tests: Test[]; created_by: string; message: string; host_name?: string; isolation?: Line[];
};
type Line = { ok: boolean; text: string };
type Candidate = { host_id: string; name: string; fits: boolean; reasons: string[]; free_cpu: number; free_memory_mb: number; devices: string[] };
type Preview = { workload: Workload; candidates: Candidate[]; host: string | null; isolation: Line[]; steps: string[]; problems: string[] };

const STATE: Record<string, string> = { running: "ok", testing: "scheduled", deploying: "verifying", failed: "failed", stopped: "stopped", draft: "previewed", expired: "unknown" };

function Glyph({ name }: { name: string }) {
  return <span className="glyph" aria-hidden="true">{name.slice(0, 2)}</span>;
}

function Checklist({ lines }: { lines: Line[] }) {
  return (
    <ul className="checklist">
      {lines.map((l, i) => (
        <li key={i}><span className={l.ok ? "yes" : "attn"}>{l.ok ? "✓" : "!"}</span><span>{l.text}</span></li>
      ))}
    </ul>
  );
}

/** PlantUML of a workload's isolation: entry, services, outbound path and connections. */
function isolationSource(w: Workload, labels: { internet: string; lan: string; you: string; proxy: string }) {
  const safe = (s: string) => s.replace(/[^A-Za-z0-9_]/g, "_");
  const lines = ["@startuml", "left to right direction"];
  const exposed = w.services.some((s) => s.ports.some((p) => p.exposure !== "internal"));
  if (exposed) lines.push(`actor "${labels.lan}" as lan`);
  lines.push(`package "${w.name}" as wl {`);
  for (const s of w.services) lines.push(`  component "${s.name}" as svc_${safe(s.name)}`);
  if (w.network.egress === "allowlist") lines.push(`  rectangle "${labels.proxy}" as proxy`);
  lines.push("}");
  if (w.network.egress !== "none") lines.push(`cloud "${labels.internet}" as inet`);
  for (const s of w.services) {
    for (const p of s.ports) {
      if (p.exposure !== "internal") lines.push(`lan --> svc_${safe(s.name)} : "${p.container}/${p.protocol}"`);
    }
  }
  if (w.network.egress === "internet") lines.push(`wl --> inet`);
  if (w.network.egress === "allowlist") {
    for (const s of w.services) lines.push(`svc_${safe(s.name)} --> proxy`);
    lines.push(`proxy --> inet : "${w.network.egress_hosts.slice(0, 3).join(", ")}${w.network.egress_hosts.length > 3 ? " …" : ""}"`);
  }
  for (const c of w.connections) {
    lines.push(`node "${c.to_workload}/${c.to_service}" as c_${safe(c.to_workload + c.to_service)}`);
    lines.push(`wl --> c_${safe(c.to_workload + c.to_service)} : "${c.port}"`);
  }
  lines.push("@enduml");
  return lines.join("\n");
}

// --- list -----------------------------------------------------------------------------------

export function Apps() {
  const t = useT();
  const lang = useLang();
  const { params } = useRoute();
  const tab = params.get("tab") ?? "installed";
  const canAct = useCan("operator");
  const { data: workloads } = useData<Workload[]>("/api/workloads");
  const { data: catalog } = useData<Adapter[]>(tab === "catalog" ? "/api/catalog" : null, []);
  const { data: services } = useData<Service[]>(tab === "installed" ? "/api/services" : null);
  if (!workloads) return <Loading />;
  const prod = workloads.filter((w) => w.environment === "production");
  const sandboxes = workloads.filter((w) => w.environment === "sandbox");
  const core = (services ?? []).filter((s) => s.core);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{t("Applications")}</h1>
          <p className="muted">{t("Every application runs in its own isolated area and talks to others only through paths you set.")}</p>
        </div>
        {canAct && (
          <div className="row">
            <a className="btn" href={href("/apps/new")}><Icon name="link" />{t("From a repository")}</a>
            <a className="btn primary" href={href("/apps?tab=catalog")}><Icon name="plus" />{t("Install an application")}</a>
          </div>
        )}
      </div>
      <Tabs current={`/apps${tab === "installed" ? "" : `?tab=${tab}`}`} items={[
        { path: "/apps", label: t("Installed"), count: prod.length },
        { path: "/apps?tab=catalog", label: t("Catalog") },
        { path: "/apps?tab=sandboxes", label: t("Sandboxes"), count: sandboxes.length },
      ]} />
      {tab === "catalog" && (
        !catalog ? <Loading /> : (
          <div className="app-grid">
            {catalog.map((a) => (
              <div key={a.id} className="card app-card">
                <div className="head"><Glyph name={a.name} /><div><b>{a.name}</b><div className="small muted">{t(a.category)} · v{a.version}</div></div></div>
                <p>{a.description}</p>
                <div className="row small">
                  {a.status === "draft" && <span className="pill warn plain">{t("Draft: sandbox only")}</span>}
                  {a.status === "tested" && <span className="pill info plain">{t("Tested")}</span>}
                  {a.network.egress === "none" ? <span className="pill ok plain">{t("No internet")}</span> : <span className="pill plain">{a.network.egress === "internet" ? t("Internet") : t("Selected hosts")}</span>}
                  {a.devices.length > 0 && <span className="pill plain">{a.devices.map((d) => d.kind.toUpperCase()).join(", ")}</span>}
                  {a.installed && <span className="pill ok">{t("Installed")}</span>}
                </div>
                {canAct && <a className="btn primary small" href={href(`/apps/install/${a.id}`)}>{t("Install")}</a>}
              </div>
            ))}
          </div>
        )
      )}
      {tab !== "catalog" && (
        <WorkloadTable rows={tab === "sandboxes" ? sandboxes : prod} lang={lang}
          empty={tab === "sandboxes" ? t("No sandboxes. New applications start in a sandbox by default.") : t("No applications installed yet.")} />
      )}
      {tab === "installed" && core.length > 0 && (
        <Card title={t("Core services")} sub={t("Run by OpsTend itself")} flush>
          <div className="table-wrap"><table><tbody>{core.map((s) => (
            <tr key={s.id}><td><b>{s.name}</b><span className="sub">{s.description}</span></td><td>{s.host_name ?? "–"}</td><td><HealthPill health={s.health} /></td></tr>
          ))}</tbody></table></div>
        </Card>
      )}
    </>
  );
}

function WorkloadTable({ rows, empty, lang }: { rows: Workload[]; empty: string; lang: string }) {
  const t = useT();
  if (rows.length === 0) return <Card><Empty>{empty}</Empty></Card>;
  return (
    <Card flush>
      <div className="table-wrap"><table>
        <thead><tr><th>{t("Application")}</th><th>{t("Runs on")}</th><th className="hide-sm">{t("Network")}</th><th className="hide-sm">{t("Checks")}</th><th>{t("State")}</th></tr></thead>
        <tbody>{rows.map((w) => (
          <tr key={w.id} className="link" onClick={() => go(`/apps/${w.id}`)}>
            <td><b>{w.name}</b><span className="sub">{w.adapter ?? w.id}{w.expires_at ? ` · ${t("until {time}", { time: new Date(w.expires_at).toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit" }) })}` : ""}</span></td>
            <td>{w.host_name ?? "–"}</td>
            <td className="hide-sm small">{w.network.egress === "none" ? t("No internet") : w.network.egress === "internet" ? t("Internet") : t("{count} allowed hosts", { count: w.network.egress_hosts.length })}{w.connections.length ? ` · ${t("{count} paths", { count: w.connections.length })}` : ""}</td>
            <td className="hide-sm">{w.tests.length ? `${w.tests.filter((x) => x.ok).length}/${w.tests.length}` : "–"}</td>
            <td><Pill state={STATE[w.state] ?? w.state}>{t(w.state)}</Pill></td>
          </tr>
        ))}</tbody>
      </table></div>
    </Card>
  );
}

// --- install wizard ---------------------------------------------------------------------------------

export function InstallWizard({ adapterId }: { adapterId: string }) {
  const t = useT();
  const { data: adapter } = useData<Adapter>(`/api/catalog/${adapterId}`, []);
  const { data: existing } = useData<Workload[]>("/api/workloads", []);
  const [step, setStep] = useState(0);
  const [id, setId] = useState(adapterId);
  const [name, setName] = useState("");
  const [settings, setSettings] = useState<Record<string, unknown>>({});
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [resources, setResources] = useState({ cpus: 1, memory_mb: 512, storage_gb: 5 });
  const [devices, setDevices] = useState<DeviceReq[]>([]);
  const [network, setNetwork] = useState<Network>({ egress: "none", egress_hosts: [] });
  const [connections, setConnections] = useState<Connection[]>([]);
  const [environment, setEnvironment] = useState<"sandbox" | "production">("sandbox");
  const [host, setHost] = useState<string | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const flow = useJobFlow();

  useEffect(() => {
    if (!adapter) return;
    setName(adapter.name);
    setResources(adapter.resources);
    setDevices(adapter.devices.filter((d) => !d.optional));
    setNetwork(adapter.network);
    setSettings(Object.fromEntries(adapter.settings.filter((s) => s.type !== "secret" && s.default != null).map((s) => [s.name, s.default])));
    if (adapter.status === "draft") setEnvironment("sandbox");
  }, [adapter]);

  const request = useMemo(() => ({ adapter: adapterId, id, name, settings, secrets, resources, devices, network, connections, environment, host }),
    [adapterId, id, name, settings, secrets, resources, devices, network, connections, environment, host]);

  useEffect(() => {
    if (step < 3) return;
    const timer = window.setTimeout(() => {
      api.post<Preview>("/api/workloads/preview", { ...request, secrets: {} }).then((p) => { setPreview(p); setError(null); }, (e) => setError(String(e.message)));
    }, 250);
    return () => window.clearTimeout(timer);
  }, [request, step]);

  if (!adapter) return <Loading />;
  const steps = [t("Settings"), t("Resources and devices"), t("Network"), t("Where and how"), t("Check")];
  const missingSecret = adapter.settings.some((s) => s.type === "secret" && s.required && !secrets[s.name]);
  const idOk = /^[a-z0-9][a-z0-9-]{0,30}$/.test(id) && !(existing ?? []).some((w) => w.id === id);

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow"><a href={href("/apps?tab=catalog")}>{t("Catalog")}</a></div>
          <h1>{t("Install {name}", { name: adapter.name })}</h1>
          <p className="muted">{adapter.description}</p>
        </div>
      </div>
      <div className="steps-bar" role="list">
        {steps.map((s, i) => <span key={s} role="listitem" aria-current={i === step ? "step" : undefined} className={i < step ? "done" : ""}>{i + 1}. {s}</span>)}
      </div>
      {adapter.notes.map((n) => <div key={n} className="banner">{n}</div>)}

      {step === 0 && (
        <Card title={t("Settings")}>
          <div className="grid cols-2">
            <label className="lbl">{t("Name")}<input className="field" value={name} onChange={(e) => setName(e.target.value)} /></label>
            <label className="lbl">{t("Short name")}<span className="help">{t("Lowercase letters, digits and dashes; used for containers and networks.")}</span>
              <input className="field" value={id} onChange={(e) => setId(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-"))} aria-invalid={!idOk} /></label>
            {adapter.settings.map((s) => (
              <label key={s.name} className="lbl">{s.label}{s.required && " *"}
                {s.help && <span className="help">{s.help}</span>}
                {s.type === "secret" ? (
                  <><input className="field" type="password" autoComplete="new-password" value={secrets[s.name] ?? ""} onChange={(e) => setSecrets({ ...secrets, [s.name]: e.target.value })} />
                    <span className="help">{t("Stored encrypted; never shown again or written to jobs.")}</span></>
                ) : s.type === "boolean" ? (
                  <input type="checkbox" checked={Boolean(settings[s.name])} onChange={(e) => setSettings({ ...settings, [s.name]: e.target.checked })} />
                ) : s.type === "select" ? (
                  <select className="field" value={String(settings[s.name] ?? "")} onChange={(e) => setSettings({ ...settings, [s.name]: e.target.value })}>{s.options.map((o) => <option key={o}>{o}</option>)}</select>
                ) : (
                  <input className="field" value={String(settings[s.name] ?? "")} onChange={(e) => setSettings({ ...settings, [s.name]: e.target.value })} />
                )}
              </label>
            ))}
          </div>
        </Card>
      )}

      {step === 1 && (
        <Card title={t("Resources and devices")} sub={t("Limits protect the other applications on the same host.")}>
          <div className="grid cols-3">
            <label className="lbl">CPU<input className="field" type="number" min={0.1} step={0.25} value={resources.cpus} onChange={(e) => setResources({ ...resources, cpus: Number(e.target.value) })} /></label>
            <label className="lbl">{t("Memory (MB)")}<input className="field" type="number" min={32} step={64} value={resources.memory_mb} onChange={(e) => setResources({ ...resources, memory_mb: Number(e.target.value) })} /></label>
            <label className="lbl">{t("Storage (GB)")}<input className="field" type="number" min={1} value={resources.storage_gb} onChange={(e) => setResources({ ...resources, storage_gb: Number(e.target.value) })} /></label>
          </div>
          <h3 style={{ marginTop: 16 }}>{t("Devices")}</h3>
          <p className="muted small">{t("A device gives the application direct hardware access. Only add what it needs.")}</p>
          <div className="grid cols-2">
            {(["gpu", "usb", "display", "serial", "audio"] as const).map((kind) => {
              const offered = adapter.devices.find((d) => d.kind === kind);
              const on = devices.some((d) => d.kind === kind);
              return (
                <label key={kind} className="choice">
                  <input type="checkbox" checked={on} onChange={(e) => setDevices(e.target.checked ? [...devices, { kind, selector: offered?.selector ?? "", optional: false, reason: offered?.reason ?? "" }] : devices.filter((d) => d.kind !== kind))} />
                  <span><b>{{ gpu: t("Graphics card"), usb: t("USB device"), display: t("Display output (HDMI/DP)"), serial: t("Serial port"), audio: t("Audio") }[kind]}</b>
                    <span className="small muted">{offered?.reason || { gpu: t("Video transcoding, AI models"), usb: t("Zigbee stick, scanner, dongle"), display: t("Show a dashboard on a screen"), serial: t("Meters and controllers"), audio: t("Speakers and microphones") }[kind]}</span>
                    {on && kind === "usb" && <input className="field" style={{ marginTop: 6 }} placeholder={t("vendor:product, e.g. 10c4:ea60")} value={devices.find((d) => d.kind === "usb")?.selector ?? ""} onChange={(e) => setDevices(devices.map((d) => d.kind === "usb" ? { ...d, selector: e.target.value } : d))} />}
                  </span>
                </label>
              );
            })}
          </div>
        </Card>
      )}

      {step === 2 && (
        <NetworkEditor network={network} setNetwork={setNetwork} connections={connections} setConnections={setConnections} self={id} />
      )}

      {step === 3 && (
        <div className="grid cols-2">
          <Card title={t("How")}>
            <div className="stack">
              <label className="choice"><input type="radio" name="env" checked={environment === "sandbox"} onChange={() => setEnvironment("sandbox")} />
                <span><b>{t("Test in a sandbox first (recommended)")}</b><span className="small muted">{t("No internet, no real data, removed automatically after a few hours. Take it into operation when the checks pass.")}</span></span></label>
              <label className="choice"><input type="radio" name="env" checked={environment === "production"} disabled={adapter.status === "draft"} onChange={() => setEnvironment("production")} />
                <span><b>{t("Install for operation")}</b><span className="small muted">{adapter.status === "draft" ? t("Available after the adapter passed a sandbox.") : t("Uses the network rules from the previous step.")}</span></span></label>
            </div>
          </Card>
          <Card title={t("Where")} sub={t("Hosts are ranked by free resources and requested devices.")} flush>
            {!preview ? <Loading /> : preview.candidates.map((c) => (
              <label key={c.host_id} className="item" style={{ cursor: c.fits ? "pointer" : "default", opacity: c.fits ? 1 : 0.6 }}>
                <div className="row between">
                  <span className="row"><input type="radio" name="host" disabled={!c.fits} checked={(host ?? preview.host) === c.host_id} onChange={() => setHost(c.host_id)} /><b>{c.name}</b></span>
                  <span className="small muted">{c.free_cpu} CPU · {Math.round(c.free_memory_mb / 1024 * 10) / 10} GB {t("free")}</span>
                </div>
                {c.devices.length > 0 && <span className="small">{c.devices.join(", ")}</span>}
                {!c.fits && <span className="small muted">{c.reasons.join("; ")}</span>}
              </label>
            ))}
          </Card>
        </div>
      )}

      {step === 4 && preview && (
        <div className="grid split">
          <div className="grid">
            <Card title={t("Isolation")}><Diagram source={isolationSource(preview.workload, { internet: t("Internet"), lan: t("Local network"), you: t("You"), proxy: t("Outbound proxy") })} ratio={2} label={t("Network of {name}", { name })} /></Card>
            <Card title={t("What OpsTend will do")}><ol className="steps">{preview.steps.map((s, i) => <li key={i}><span className="dot">{i + 1}</span><div>{s}</div></li>)}</ol></Card>
          </div>
          <Card title={t("Check")}>
            <Checklist lines={preview.isolation} />
            {preview.problems.map((p) => <div key={p} className="banner warn" style={{ marginTop: 8 }}>{p}</div>)}
          </Card>
        </div>
      )}

      {error && <div className="banner warn">{error}</div>}
      <div className="row between">
        <button className="btn" disabled={step === 0} onClick={() => setStep(step - 1)}>{t("Back")}</button>
        {step < steps.length - 1 ? (
          <button className="btn primary" disabled={step === 0 && (!idOk || missingSecret || !name)} onClick={() => setStep(step + 1)}>{t("Next")}</button>
        ) : (
          <button className="btn primary" disabled={!preview?.host} onClick={() => flow.start(() => api.post<Job>("/api/workloads", request))}>
            {environment === "sandbox" ? t("Start the sandbox") : t("Install")}
          </button>
        )}
      </div>
      {flow.dialog}
    </>
  );
}

function NetworkEditor({ network, setNetwork, connections, setConnections, self }: {
  network: Network; setNetwork: (n: Network) => void; connections: Connection[]; setConnections: (c: Connection[]) => void; self: string;
}) {
  const t = useT();
  const { data: workloads } = useData<Workload[]>("/api/workloads", []);
  const [hostText, setHostText] = useState(network.egress_hosts.join("\n"));
  useEffect(() => setHostText(network.egress_hosts.join("\n")), [network.egress]);
  const targets = (workloads ?? []).filter((w) => w.id !== self && w.environment === "production");
  return (
    <div className="grid cols-2">
      <Card title={t("Outbound")} sub={t("What the application may reach on the internet.")}>
        <div className="stack">
          {([["none", t("Nothing"), t("Safest. Updates come through OpsTend.")],
             ["allowlist", t("Only selected hosts"), t("Through a proxy that allows exactly these names.")],
             ["internet", t("The whole internet"), t("Needed by monitors and some integrations.")]] as const).map(([value, label, help]) => (
            <label key={value} className="choice"><input type="radio" name="egress" checked={network.egress === value} onChange={() => setNetwork({ ...network, egress: value })} />
              <span><b>{label}</b><span className="small muted">{help}</span></span></label>
          ))}
          {network.egress === "allowlist" && (
            <label className="lbl">{t("Allowed hosts, one per line")}
              <textarea className="field" rows={4} value={hostText} onChange={(e) => { setHostText(e.target.value); setNetwork({ ...network, egress_hosts: e.target.value.split(/\s+/).filter(Boolean) }); }} /></label>
          )}
        </div>
      </Card>
      <Card title={t("Paths to other applications")} sub={t("Without a path, applications cannot see each other.")}>
        <div className="stack">
          {connections.map((c, i) => (
            <div key={i} className="row between small">
              <span><b>{c.to_workload}/{c.to_service}</b> : {c.port} — {c.reason || "–"}</span>
              <button className="btn small ghost" onClick={() => setConnections(connections.filter((_, j) => j !== i))}>{t("Remove")}</button>
            </div>
          ))}
          {targets.length === 0 ? <span className="muted small">{t("No other applications to connect to.")}</span> : <AddConnection targets={targets} onAdd={(c) => setConnections([...connections, c])} />}
        </div>
      </Card>
    </div>
  );
}

function AddConnection({ targets, onAdd }: { targets: Workload[]; onAdd: (c: Connection) => void }) {
  const t = useT();
  const [to, setTo] = useState(targets[0]?.id ?? "");
  const target = targets.find((w) => w.id === to);
  const ports = (target?.services ?? []).flatMap((s) => s.ports.map((p) => ({ service: s.name, port: p.container })));
  const [choice, setChoice] = useState(0);
  const [reason, setReason] = useState("");
  return (
    <div className="grid" style={{ gap: 8 }}>
      <div className="row">
        <select className="field" style={{ flex: 1 }} value={to} onChange={(e) => { setTo(e.target.value); setChoice(0); }} aria-label={t("Application")}>{targets.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}</select>
        <select className="field" style={{ flex: 1 }} value={choice} onChange={(e) => setChoice(Number(e.target.value))} aria-label={t("Port")}>{ports.map((p, i) => <option key={i} value={i}>{p.service}:{p.port}</option>)}</select>
      </div>
      <input className="field" placeholder={t("Why, e.g. sends measurements")} value={reason} onChange={(e) => setReason(e.target.value)} />
      <button className="btn small" disabled={!ports.length} onClick={() => ports[choice] && onAdd({ to_workload: to, to_service: ports[choice].service, port: ports[choice].port, reason })}><Icon name="plus" size={14} />{t("Add path")}</button>
    </div>
  );
}

// --- detail ------------------------------------------------------------------------------------------

type Detail = { workload: Workload; isolation: Line[]; guests: Guest[]; jobs: Job[]; adapter: Adapter | null; host: { name: string; address: string } | null };

export function WorkloadDetail({ id }: { id: string }) {
  const t = useT();
  const lang = useLang();
  const canAct = useCan("operator");
  const flow = useJobFlow();
  const { data } = useData<Detail>(`/api/workloads/${id}`);
  const [editNet, setEditNet] = useState(false);
  const [net, setNet] = useState<Network | null>(null);
  const [conns, setConns] = useState<Connection[]>([]);
  if (!data) return <Loading />;
  const w = data.workload;
  const act = (action: string, extra: Record<string, unknown> = {}) => flow.start(() => api.post<Job>(`/api/workloads/${w.id}/actions`, { action, ...extra }));
  const sandbox = w.environment === "sandbox";
  const allOk = w.tests.length > 0 && w.tests.every((x) => x.ok);
  const lanPorts = w.services.flatMap((s) => s.ports.filter((p) => p.exposure !== "internal").map((p) => p.container));

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow"><a href={href(sandbox ? "/apps?tab=sandboxes" : "/apps")}>{t("Applications")}</a>{data.host ? ` · ${data.host.name}` : ""}</div>
          <h1>{w.name} {sandbox && <span className="env-badge">{t("Sandbox")}</span>}</h1>
          <p className="muted">{data.adapter?.description ?? w.adapter}</p>
        </div>
        <Pill state={STATE[w.state] ?? w.state}>{t(w.state)}</Pill>
      </div>
      {w.message && <div className="banner warn">{w.message}</div>}
      {sandbox && w.expires_at && (
        <div className="banner">{t("This sandbox has no internet access and is removed automatically at {time}.", { time: new Date(w.expires_at).toLocaleString(lang) })}
          {allOk && canAct && <> <button className="btn small primary" onClick={() => act("promote")}>{t("Take into operation")}</button></>}
        </div>
      )}
      {canAct && (
        <div className="row">
          {w.state === "stopped" ? <button className="btn" onClick={() => act("start")}><Icon name="play" />{t("Start")}</button>
            : <button className="btn" onClick={() => act("stop")}><Icon name="stop" />{t("Stop")}</button>}
          <button className="btn" onClick={() => act("restart")}><Icon name="refresh" />{t("Restart")}</button>
          <button className="btn" onClick={() => act("test")}><Icon name="check" />{t("Run checks")}</button>
          {!sandbox && <button className="btn" onClick={() => act("sandbox")}><Icon name="copy" />{t("Test a change in a sandbox")}</button>}
          {w.host_id && <a className="btn" href={href(`/console?target=${encodeURIComponent(`container:${w.host_id}:${w.id}-${w.services[0]?.name}`)}`)}><Icon name="ops" />{t("Console")}</a>}
          <button className="btn danger" onClick={() => act("remove", { purge: sandbox })}>{t("Remove")}</button>
        </div>
      )}
      <div className="grid split">
        <div className="grid">
          <Card title={t("Isolation and paths")} action={canAct && <button className="btn small" onClick={() => { setNet(w.network); setConns(w.connections); setEditNet(true); }}>{t("Change")}</button>}>
            <Diagram source={isolationSource(w, { internet: t("Internet"), lan: t("Local network"), you: t("You"), proxy: t("Outbound proxy") })} ratio={2.2} label={t("Network of {name}", { name: w.name })} />
            <div style={{ marginTop: 12 }}><Checklist lines={data.isolation} /></div>
            {lanPorts.length > 0 && data.host && <p className="small">{t("Open in the local network:")} {lanPorts.map((p) => <a key={p} href={`http://${data.host!.address}:${p}`} target="_blank" rel="noreferrer" style={{ marginRight: 8 }}>{data.host!.address}:{p}</a>)}</p>}
          </Card>
          <Card title={t("Containers")} flush>
            {data.guests.length === 0 ? <Empty>{t("Not deployed yet.")}</Empty> : data.guests.map((g) => (
              <a key={g.id} className="item" href={href(`/guests/${encodeURIComponent(g.id)}`)} style={{ color: "inherit" }}>
                <div className="row between"><b>{g.name}</b><Pill state={g.state} /></div><span className="small muted mono">{g.image}</span>
              </a>
            ))}
          </Card>
        </div>
        <div className="grid">
          <Card title={t("Checks")} flush>
            {w.tests.length === 0 ? <Empty>{t("This application has no checks.")}</Empty> : w.tests.map((x) => (
              <div key={x.name} className="item small"><div className="row between"><span>{x.name}</span>{x.ok === undefined ? <span className="pill plain">–</span> : <Pill state={x.ok ? "ok" : "failed"}>{x.ok ? t("passed") : t("failed")}</Pill>}</div>{x.detail && <span className="muted">{x.detail}</span>}</div>
            ))}
          </Card>
          <Card title={t("Resources")}>
            <div className="stack small">
              <div className="row between"><span>CPU</span><b>{w.resources.cpus}</b></div>
              <div className="row between"><span>{t("Memory")}</span><b>{w.resources.memory_mb} MB</b></div>
              <div className="row between"><span>{t("Storage")}</span><b>{w.resources.storage_gb} GB</b></div>
              {w.devices.map((d, i) => <div key={i} className="row between"><span>{d.kind.toUpperCase()}</span><b>{d.selector || t("any")}</b></div>)}
              {Object.keys(w.secrets).length > 0 && <div className="row between"><span>{t("Secrets")}</span><b>{Object.keys(w.secrets).join(", ")}</b></div>}
            </div>
          </Card>
          <Card title={t("Jobs")} flush>
            {data.jobs.length === 0 ? <Empty>–</Empty> : data.jobs.map((j) => (
              <a key={j.id} className="item small" href={href(`/jobs/${j.id}`)} style={{ color: "inherit" }}><div className="row between"><span>{j.title}</span><Pill state={j.state} /></div><span className="muted">{when(j.updated_at, lang)}</span></a>
            ))}
          </Card>
        </div>
      </div>
      <Dialog open={editNet} onClose={() => setEditNet(false)} title={t("Network rules of {name}", { name: w.name })}
        footer={<><button className="btn" onClick={() => setEditNet(false)}>{t("Cancel")}</button>
          <button className="btn primary" onClick={() => { setEditNet(false); flow.start(() => api.put<Job>(`/api/workloads/${w.id}/network`, { network: net, connections: conns })); }}>{t("Create preview")}</button></>}>
        {net && <NetworkEditor network={net} setNetwork={setNet} connections={conns} setConnections={setConns} self={w.id} />}
      </Dialog>
      {flow.dialog}
    </>
  );
}

// --- from a repository ---------------------------------------------------------------------------------

export function FromRepository() {
  const t = useT();
  const { params } = useRoute();
  const [repo, setRepo] = useState(params.get("repo") ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<{ adapter: Adapter; questions: string[] } | null>(null);
  async function analyze(url = repo) {
    setBusy(true);
    setError(null);
    try {
      setDraft(await api.post("/api/catalog/draft", { repo: url }));
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow"><a href={href("/apps")}>{t("Applications")}</a></div>
          <h1>{t("New application from a repository")}</h1>
          <p className="muted">{t("Zorec reads the Compose file or Containerfile and drafts an adapter. Drafts run only in a sandbox until their checks pass.")}</p>
        </div>
      </div>
      <Card>
        <form className="row" onSubmit={(e) => { e.preventDefault(); analyze(); }}>
          <label className="sr-only" htmlFor="repo">Repository</label>
          <input id="repo" className="field" style={{ flex: 1, minWidth: 240 }} value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="https://github.com/owner/repo" />
          <button className="btn primary" disabled={busy || !repo}>{busy ? t("Reading …") : t("Draft adapter")}</button>
          <button type="button" className="btn ghost" onClick={() => { const r = "https://github.com/werkstatt/wetterstation"; setRepo(r); analyze(r); }}>{t("Load example")}</button>
        </form>
        {error && <div className="banner warn" style={{ marginTop: 12 }}>{error}</div>}
      </Card>
      {draft && (
        <Card title={t("Draft: {name}", { name: draft.adapter.name })} sub={draft.adapter.origin ?? ""}>
          <div className="stack">
            {draft.questions.map((q) => <div key={q} className="banner warn">{q}</div>)}
            {draft.adapter.notes.map((q) => <div key={q} className="banner">{q}</div>)}
            <p className="small">{t("Services")}: {draft.adapter.services.map((s) => s.name).join(", ")} · {t("Settings")}: {draft.adapter.settings.length}</p>
            <div><a className="btn primary" href={href(`/apps/install/${draft.adapter.id}`)}>{t("Test in a sandbox")}</a></div>
          </div>
        </Card>
      )}
    </>
  );
}
