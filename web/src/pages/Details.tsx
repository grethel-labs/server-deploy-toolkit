import { useEffect, useState } from "react";
import { api, type Device, type Finding, type Guest, type Host, type Insight, type Job, type Service } from "../api";
import { Icon } from "../components/Icon";
import { Card, Dialog, Empty, HealthPill, Loading, Meter, Pill, useJobFlow } from "../components/ui";
import { gb, go, href, pct, speed, useCan, useData, useLang, useT, when } from "../lib";
import { GuestTable } from "./Infrastructure";

function InsightList({ items }: { items: Insight[] }) {
  const t = useT();
  const flow = useJobFlow();
  const canAct = useCan("operator");
  if (items.length === 0) return null;
  return (
    <Card title={t("Insights")} flush>
      {items.map((i) => (
        <div key={i.id} className={`item sev-stripe ${i.severity}`}>
          <span className="title">{i.title}</span>
          <span className="muted small">{i.detail}</span>
          {i.fix && canAct && <div><button className="btn small" onClick={() => flow.start(() => api.post<Job>(`/api/network/insights/${i.id}/fix`))}>{i.fix.summary}</button></div>}
        </div>
      ))}
      {flow.dialog}
    </Card>
  );
}

function Facts({ rows }: { rows: [string, React.ReactNode][] }) {
  return (
    <dl style={{ display: "grid", gridTemplateColumns: "minmax(120px, auto) 1fr", gap: "8px 16px", margin: 0 }}>
      {rows.map(([k, v]) => (
        <div key={k} style={{ display: "contents" }}><dt className="muted">{k}</dt><dd style={{ margin: 0 }}>{v}</dd></div>
      ))}
    </dl>
  );
}

export function HostDetail({ id }: { id: string }) {
  const t = useT();
  const lang = useLang();
  const { data } = useData<{ host: Host; guests: Guest[]; updates: { id: string; notes: string; current: string; available: string; security: boolean }[]; findings: Finding[]; insights: Insight[]; bridges: { name: string; vlan_aware: boolean; ports: string[]; address: string | null }[] }>(`/api/hosts/${encodeURIComponent(id)}`);
  if (!data) return <Loading />;
  const h = data.host;
  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow"><a href={href("/infra")}>{t("Hosts")}</a> · {h.site}</div>
          <h1>{h.name}</h1>
          <p className="muted">{h.os} · {h.address}</p>
        </div>
        <div className="row">
          <HealthPill health={h.health} />
          <a className="btn" href={href(`/network/topology?focus=${encodeURIComponent(h.id)}`)}><Icon name="map" />{t("On the map")}</a>
        </div>
      </div>
      <div className="grid cols-3">
        <Card title="CPU"><div className="stack"><b className="num">{pct(h.usage.cpu_pct)}</b><Meter value={h.usage.cpu_pct} max={100} /><span className="muted small">{h.capacity.cpu_cores} {t("cores")}</span></div></Card>
        <Card title={t("Memory")}><div className="stack"><b className="num">{gb(h.usage.memory_mb)} / {gb(h.capacity.memory_mb)}</b><Meter value={h.usage.memory_mb} max={h.capacity.memory_mb} /></div></Card>
        <Card title={t("Storage")}><div className="stack"><b className="num">{h.usage.disk_gb ?? "–"} / {h.capacity.disk_gb} GB</b><Meter value={h.usage.disk_gb} max={h.capacity.disk_gb} /></div></Card>
      </div>
      <InsightList items={data.insights} />
      <div className="grid split">
        <div className="grid">
          <h2>{t("Guests and containers")}</h2>
          <GuestTable guests={data.guests} />
        </div>
        <div className="grid">
          <Card title={t("Facts")}>
            <Facts rows={[
              [t("Platform"), h.platform === "proxmox" ? "Proxmox VE" : "Linux"],
              [t("Version"), h.version || "–"],
              [t("Traits"), h.tags.join(" · ") || "–"],
              [t("Source"), h.observed ? `${h.observed.source}, ${when(h.observed.observed_at, lang)}` : "–"],
            ]} />
          </Card>
          <Card title={t("Network bridges")} flush>
            {data.bridges.map((b) => (
              <div key={b.name} className="item small">
                <div className="row between"><b>{b.name}</b>{b.vlan_aware ? <span className="pill ok plain">VLAN-aware</span> : <span className="pill plain">{t("no VLAN")}</span>}</div>
                <span className="muted">{b.address ?? "–"} · {b.ports.join(", ")}</span>
              </div>
            ))}
            {data.bridges.length === 0 && <Empty>–</Empty>}
          </Card>
          <Card title="Updates" flush>
            {data.updates.length === 0 && <Empty>{t("Up to date.")}</Empty>}
            {data.updates.map((u) => (
              <div key={u.id} className="item small"><div className="row between"><b>{u.notes || u.id}</b>{u.security && <span className="pill bad plain">{t("Security")}</span>}</div><span className="muted mono">{u.current} → {u.available}</span></div>
            ))}
          </Card>
        </div>
      </div>
    </>
  );
}

const POWER = [
  { action: "start", icon: "play", when: ["stopped", "paused"], en: "Start" },
  { action: "shutdown", icon: "power", when: ["running"], en: "Shut down" },
  { action: "reboot", icon: "refresh", when: ["running"], en: "Reboot" },
  { action: "suspend", icon: "stop", when: ["running"], en: "Pause" },
  { action: "resume", icon: "play", when: ["paused"], en: "Resume" },
  { action: "stop", icon: "stop", when: ["running", "paused"], en: "Force off" },
];

export function GuestDetail({ id }: { id: string }) {
  const t = useT();
  const lang = useLang();
  const canAct = useCan("operator");
  const flow = useJobFlow();
  const { data } = useData<{ guest: Guest; host: Host; capabilities: string[]; service: Service | null; jobs: Job[]; insights: Insight[] }>(`/api/guests/${encodeURIComponent(id)}`);
  const { data: hosts } = useData<Host[]>("/api/hosts", []);
  const [dlg, setDlg] = useState<null | "snapshot" | "clone" | "migrate">(null);
  const [value, setValue] = useState("");
  if (!data) return <Loading />;
  const { guest: g, host: h, capabilities: caps } = data;
  const job = (action: string, params: Record<string, unknown> = {}) =>
    flow.start(() => api.post<Job>(`/api/guests/${encodeURIComponent(g.id)}/jobs`, { action, params }));
  const kind = { vm: "VM", lxc: "LXC", container: "Container" }[g.kind];

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow"><a href={href("/infra?tab=guests")}>{t("Guests")}</a> · <a href={href(`/hosts/${encodeURIComponent(h.id)}`)}>{h.name}</a></div>
          <h1>{g.name}</h1>
          <p className="muted">{kind} · {g.image ?? `#${g.native_id}`} {g.uptime_s ? `· ${t("up for")} ${Math.round(g.uptime_s / 86400)} ${t("days")}` : ""}</p>
        </div>
        <Pill state={g.template ? "unknown" : g.state}>{g.template ? t("Template") : undefined}</Pill>
      </div>

      {canAct && (
        <div className="row">
          {!g.template && caps.includes("power") && POWER.filter((p) => p.when.includes(g.state) && !(p.action === "suspend" && g.kind === "lxc")).map((p) => (
            <button key={p.action} className={`btn${p.action === "stop" ? " danger" : ""}`} onClick={() => job("power", { action: p.action })}><Icon name={p.icon} />{t(p.en)}</button>
          ))}
          {caps.includes("snapshot") && <button className="btn" onClick={() => { setValue(`vor-${new Date().toISOString().slice(0, 10)}`); setDlg("snapshot"); }}><Icon name="camera" />Snapshot</button>}
          {caps.includes("backup") && <button className="btn" onClick={() => job("backup", { destination: "local" })}><Icon name="download" />{t("Back up")}</button>}
          {caps.includes("clone") && <button className="btn" onClick={() => { setValue(`${g.name}-kopie`); setDlg("clone"); }}><Icon name="copy" />{g.template ? t("New from template") : t("Clone")}</button>}
          {caps.includes("migrate") && <button className="btn" onClick={() => setDlg("migrate")}><Icon name="move" />{t("Move")}</button>}
          {caps.includes("console") && <span className="muted small">{t("Console in the Proxmox UI")}</span>}
        </div>
      )}

      <InsightList items={data.insights} />
      <div className="grid split">
        <div className="grid">
          <div className="grid cols-2">
            <Card title="CPU"><div className="stack"><b className="num">{pct(g.usage.cpu_pct)}</b><Meter value={g.usage.cpu_pct} max={100} /><span className="muted small">{g.capacity.cpu_cores} vCPU</span></div></Card>
            <Card title={t("Memory")}><div className="stack"><b className="num">{gb(g.usage.memory_mb)} / {gb(g.capacity.memory_mb)}</b><Meter value={g.usage.memory_mb} max={g.capacity.memory_mb} /></div></Card>
          </div>
          <Card title={t("Network")} flush>
            <div className="table-wrap"><table>
              <thead><tr><th>NIC</th><th>MAC</th><th>Bridge</th><th>VLAN</th><th>{t("Address")}</th></tr></thead>
              <tbody>{g.nics.map((n) => (
                <tr key={n.name}><td>{n.name}</td><td className="mono">{n.mac ?? "–"}</td><td>{n.bridge ?? "–"}</td><td className="num">{n.vlan ?? "–"}</td><td className="mono">{n.ips.join(", ") || "DHCP"}</td></tr>
              ))}</tbody>
            </table></div>
          </Card>
          <Card title="Snapshots" flush>
            {g.snapshots.length === 0 && <Empty>{t("No snapshots.")}</Empty>}
            {g.snapshots.map((s) => (
              <div key={s.name} className="item">
                <div className="row between">
                  <b>{s.name}</b>
                  {canAct && <div className="row">
                    <button className="btn small" onClick={() => job("snapshot_rollback", { name: s.name })}>{t("Roll back")}</button>
                    <button className="btn small danger" onClick={() => job("snapshot_delete", { name: s.name })}>{t("Delete")}</button>
                  </div>}
                </div>
                {s.created_at && <span className="muted small">{when(s.created_at, lang)}</span>}
              </div>
            ))}
          </Card>
        </div>
        <div className="grid">
          {data.service && (
            <Card title={t("Service")}>
              <div className="stack"><div className="row between"><b>{data.service.name}</b><HealthPill health={data.service.health} /></div>
                <span className="muted small">{data.service.description}</span>
                {data.service.endpoints.map((e) => <span key={e} className="mono small">{e}</span>)}</div>
            </Card>
          )}
          <Card title={t("Jobs")} flush>
            {data.jobs.length === 0 && <Empty>{t("None yet.")}</Empty>}
            {data.jobs.map((j) => (
              <a key={j.id} className="item" href={href(`/jobs/${j.id}`)} style={{ color: "inherit" }}>
                <div className="row between"><span>{j.title}</span><Pill state={j.state} /></div>
                <span className="muted small">{when(j.updated_at, lang)}</span>
              </a>
            ))}
          </Card>
        </div>
      </div>

      <Dialog open={dlg !== null} onClose={() => setDlg(null)}
        title={dlg === "snapshot" ? t("Create snapshot") : dlg === "clone" ? t("Create new guest") : t("Move to another host")}
        footer={<>
          <button className="btn" onClick={() => setDlg(null)}>{t("Cancel")}</button>
          <button className="btn primary" disabled={!value} onClick={() => {
            const d = dlg; setDlg(null);
            if (d === "snapshot") job("snapshot", { name: value });
            if (d === "clone") job("clone", { name: value });
            if (d === "migrate") job("migrate", { host: value });
          }}>{t("Create preview")}</button>
        </>}>
        {dlg === "migrate" ? (
          <label className="lbl">{t("Target host")}
            <select className="field" value={value} onChange={(e) => setValue(e.target.value)}>
              <option value="">–</option>
              {(hosts ?? []).filter((x) => x.platform === h.platform && x.id !== h.id).map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
            </select>
            <span className="help">{t("Only hosts of the same platform. VMs move live, containers restart briefly.")}</span>
          </label>
        ) : (
          <label className="lbl">{t("Name")}
            <input className="field" value={value} onChange={(e) => setValue(e.target.value.replace(/[^a-zA-Z0-9._-]/g, "-"))} />
          </label>
        )}
      </Dialog>
      {flow.dialog}
    </>
  );
}

export function DeviceDetail({ id }: { id: string }) {
  const t = useT();
  const canAct = useCan("operator");
  const flow = useJobFlow();
  const { data } = useData<{ devices: Device[]; insights: Insight[] }>("/api/network");
  if (!data) return <Loading />;
  const d = data.devices.find((x) => x.id === id);
  if (!d) return <Empty>{t("Device not found.")}</Empty>;
  const used = d.ports.reduce((s, p) => s + (p.poe_watts ?? 0), 0);
  const step = (action: string, params: Record<string, unknown>, title: string) =>
    flow.start(() => api.post<Job>("/api/jobs", { title, target: d.id, steps: [{ action, params: { device: d.id, ...params } }] }));
  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow"><a href={href("/network/devices")}>{t("Network devices")}</a> · {d.site}</div>
          <h1>{d.name}</h1>
          <p className="muted">{d.model} · {d.address} · Firmware {d.firmware}</p>
        </div>
        <div className="row">
          <HealthPill health={d.health} />
          <a className="btn" href={href(`/network/topology?focus=${encodeURIComponent(d.id)}`)}><Icon name="map" />{t("On the map")}</a>
        </div>
      </div>
      {canAct && (
        <div className="row">
          {d.upgrade_to && <button className="btn primary" onClick={() => step("unifi.device_upgrade", {}, t("Firmware {upgrade_to} on {d_name}", { upgrade_to: d.upgrade_to, d_name: d.name }))}><Icon name="download" />{t("Install firmware {upgrade_to}", { upgrade_to: d.upgrade_to })}</button>}
          <button className="btn" onClick={() => step("unifi.device_restart", {}, t("Restart {d_name}", { d_name: d.name }))}><Icon name="refresh" />{t("Restart")}</button>
        </div>
      )}
      <InsightList items={data.insights.filter((i) => i.objects.includes(d.id))} />
      {d.poe_budget_watts && (
        <Card title="PoE"><div className="stack"><div className="row between"><span>{t("Power")}</span><b className="num">{used.toFixed(1)} / {d.poe_budget_watts} W</b></div><Meter value={used} max={d.poe_budget_watts} /></div></Card>
      )}
      <Card title={t("Ports")} sub={t("Speed, VLANs, PoE and what is connected")} flush>
        <div className="table-wrap"><table>
          <thead><tr><th>Port</th><th>{t("Connected")}</th><th>{t("Speed")}</th><th>VLAN</th><th>PoE</th><th>{t("Errors")}</th><th /></tr></thead>
          <tbody>{d.ports.map((p) => (
            <tr key={p.index}>
              <td><b>{p.index}</b><span className="sub">{p.name}{p.is_uplink ? " · Uplink" : ""}</span></td>
              <td>{p.lldp?.name ?? (p.up ? t("active") : <span className="muted">{t("free")}</span>)}{p.lldp && <span className="sub mono">{p.lldp.port}</span>}</td>
              <td className="num">{p.up ? <>{speed(p.speed_mbps)}{p.max_speed_mbps && p.speed_mbps && p.speed_mbps < p.max_speed_mbps ? <span className="pill warn plain" style={{ marginLeft: 6 }}>{t("max")} {speed(p.max_speed_mbps)}</span> : null}</> : "–"}</td>
              <td className="num">{p.native_vlan ?? 1}{p.tagged_vlans.length ? ` + ${p.tagged_vlans.join(", ")}` : ""}<span className="sub">{p.profile ?? ""}</span></td>
              <td className="num">{p.poe_watts ? `${p.poe_watts} W` : "–"}</td>
              <td className="num">{p.rx_errors + p.tx_errors || "–"}</td>
              <td>{canAct && p.poe_enabled && <button className="btn small" onClick={() => step("unifi.poe_cycle", { port: p.index }, t("Power-cycle port {p_index}", { p_index: p.index }))}>{t("PoE restart")}</button>}</td>
            </tr>
          ))}</tbody>
        </table></div>
      </Card>
      {flow.dialog}
    </>
  );
}

export function ObjectRedirect({ id }: { id: string }) {
  useEffect(() => {
    const enc = encodeURIComponent(id);
    if (id.startsWith("unifi:")) return go(`/devices/${enc}`);
    api.get(`/api/guests/${enc}`).then(() => go(`/guests/${enc}`), () => go(`/hosts/${enc}`));
  }, [id]);
  return <Loading />;
}
