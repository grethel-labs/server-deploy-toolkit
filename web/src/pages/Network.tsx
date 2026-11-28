import { useState } from "react";
import { api, type Device, type Guest, type Host, type Insight, type Job } from "../api";
import { Icon } from "../components/Icon";
import { Topology } from "../components/Topology";
import { Card, Empty, HealthPill, Kpi, Loading, Pill, Tabs, useJobFlow } from "../components/ui";
import { go, href, useCan, useData, useRoute, useT } from "../lib";

type Client = { mac: string; name: string | null; ip: string | null; vlan: number | null; device_mac: string | null; port: number | null; wired: boolean; signal_dbm: number | null };
type Net = { id: string; name: string; vlan: number | null; subnet: string | null; purpose: string; isolated: boolean };
type Rule = { id: string; name: string; action: string; src: string; dst: string; protocol: string; ports: string | null; enabled: boolean; order: number };
type NetworkData = {
  devices: Device[]; clients: Client[]; networks: Net[]; firewall: Rule[]; insights: Insight[];
  tunnels: { id: string; name: string; a_host: string; b_host: string; up: boolean }[];
  publications: { id: string; hostname: string; proxy_host: string; target: string; public: boolean }[];
};

export function Network() {
  const t = useT();
  const route = useRoute();
  const sub = route.path.split("/")[2] ?? "";
  const { data } = useData<NetworkData>("/api/network");
  if (!data) return <Loading />;
  const tabs = [
    { path: "/network", label: t("Overview") },
    { path: "/network/topology", label: t("Topology") },
    { path: "/network/devices", label: t("Devices and ports"), count: data.devices.length },
    { path: "/network/clients", label: "Clients", count: data.clients.length },
    { path: "/network/vlans", label: t("Networks and firewall"), count: data.networks.length },
    { path: "/network/path", label: t("Check a path") },
  ];
  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">UniFi · Proxmox · Linux · VPN</div>
          <h1>{t("Network")}</h1>
        </div>
      </div>
      <Tabs items={tabs} current={sub ? `/network/${sub}` : "/network"} />
      {sub === "" && <NetOverview data={data} />}
      {sub === "topology" && <TopologyView />}
      {sub === "devices" && <Devices data={data} />}
      {sub === "clients" && <Clients data={data} />}
      {sub === "vlans" && <Vlans data={data} />}
      {sub === "path" && <PathCheck data={data} />}
    </>
  );
}

function NetOverview({ data }: { data: NetworkData }) {
  const t = useT();
  const canAct = useCan("operator");
  const flow = useJobFlow();
  const upgrades = data.devices.filter((d) => d.upgrade_to).length;
  const wifi = data.clients.filter((c) => !c.wired).length;
  return (
    <>
      <div className="grid cols-4">
        <Kpi label={t("Devices")} value={data.devices.length} hint={upgrades ? t("{upgrades} with new firmware", { upgrades }) : t("Firmware current")} onClick={() => go("/network/devices")} />
        <Kpi label="Clients" value={data.clients.length} hint={t("{wifi} on Wi-Fi", { wifi })} onClick={() => go("/network/clients")} />
        <Kpi label={t("Networks")} value={data.networks.length} hint={`${data.firewall.length} ${t("firewall rules")}`} onClick={() => go("/network/vlans")} />
        <Kpi label={t("Insights")} value={data.insights.length} hint={t("across all systems")} />
      </div>
      <Card title={t("What OpsTend finds in the network")}
        sub={t("Joins switch ports, VLANs, Proxmox bridges, guests and firewall – more than any single system sees.")} flush>
        {data.insights.length === 0 && <Empty>{t("Nothing unusual.")}</Empty>}
        {data.insights.map((i) => (
          <div key={i.id} className={`item sev-stripe ${i.severity}`}>
            <div className="row between"><span className="title">{i.title}</span><Pill state={i.severity} /></div>
            <span className="muted small">{i.detail}</span>
            <div className="row">
              {i.fix && canAct && <button className="btn small primary" onClick={() => flow.start(() => api.post<Job>(`/api/network/insights/${i.id}/fix`))}>{i.fix.summary}</button>}
              {i.objects[0] && <a className="btn small" href={href(`/network/topology?focus=${encodeURIComponent(i.objects[0])}&layers=physical,network`)}><Icon name="map" size={14} />{t("Show on map")}</a>}
            </div>
          </div>
        ))}
      </Card>
      <div className="grid cols-2">
        <Card title={t("VPN tunnels")} flush>
          {data.tunnels.length === 0 && <Empty>–</Empty>}
          {data.tunnels.map((tn) => <div key={tn.id} className="item small"><div className="row between"><b>{tn.name}</b><Pill state={tn.up ? "ok" : "critical"}>{tn.up ? t("up") : t("down")}</Pill></div><span className="muted mono">{tn.a_host} ⇄ {tn.b_host}</span></div>)}
        </Card>
        <Card title={t("Published services")} flush>
          {data.publications.length === 0 && <Empty>{t("Nothing is reachable from outside.")}</Empty>}
          {data.publications.map((p) => <div key={p.id} className="item small"><div className="row between"><b className="mono">{p.hostname}</b><span className={`pill ${p.public ? "warn" : "ok"} plain`}>{p.public ? t("public") : "VPN"}</span></div><span className="muted">{p.proxy_host} → {p.target}</span></div>)}
        </Card>
      </div>
      {flow.dialog}
    </>
  );
}


function TopologyView() {
  const t = useT();
  const { params } = useRoute();
  const [layers, setLayers] = useState<string[]>((params.get("layers") ?? "physical").split(","));
  const [clients, setClients] = useState(false);
  const [selected, setSelected] = useState<{ id: string; kind: string; name: string } | null>(null);
  const toggle = (l: string) => setLayers((cur) => cur.includes(l) ? cur.filter((x) => x !== l) || ["physical"] : [...cur, l]);
  const link = selected && (selected.kind === "guest" ? `/guests/${encodeURIComponent(selected.id)}`
    : selected.kind === "host" ? `/hosts/${encodeURIComponent(selected.id)}`
    : ["switch", "gateway", "ap"].includes(selected.kind) ? `/devices/${encodeURIComponent(selected.id)}` : null);
  return (
    <div className="grid split">
      <Card title={t("Placement and connections")}
        sub={t("Click an element. The map keeps a fixed aspect ratio and lays itself out to fit.")}
        action={<div className="row">
          {[["physical", t("Cabling")], ["network", t("Networks and guests")], ["access", t("VPN and access")]].map(([k, label]) => (
            <label key={k} className="chk"><input type="checkbox" checked={layers.includes(k)} onChange={() => toggle(k)} />{label}</label>
          ))}
          <label className="chk"><input type="checkbox" checked={clients} onChange={() => setClients((c) => !c)} />Clients</label>
        </div>} flush>
        <Topology layers={layers.length ? layers : ["physical"]} clients={clients} focus={params.get("focus") ?? undefined} onSelect={setSelected} />
      </Card>
      <Card title={selected ? selected.name : t("Selection")}>
        {!selected && <p className="muted">{t("Select a device, host or guest on the map. Red elements have an insight, dashed lines are slow or faulty.")}</p>}
        {selected && (
          <div className="stack">
            <span className="pill plain">{selected.kind}</span>
            <span className="mono small">{selected.id}</span>
            {link && <a className="btn" href={href(link)}>{t("Open details")}<Icon name="arrow" size={14} /></a>}
            <a className="btn" href={href(`/network/path?dst=${encodeURIComponent(selected.id)}`)}>{t("Check path to it")}</a>
          </div>
        )}
        <div className="legend" style={{ marginTop: 16 }}>
          <span><i style={{ background: "#E6F4EF" }} />{t("fine")}</span>
          <span><i style={{ background: "#FFF4DA" }} />{t("notice")}</span>
          <span><i style={{ background: "#FDE7E7" }} />{t("problem")}</span>
        </div>
      </Card>
    </div>
  );
}

function Devices({ data }: { data: NetworkData }) {
  const t = useT();
  const kind = { gateway: "Gateway", switch: "Switch", ap: t("Access point"), other: t("Device") };
  return (
    <Card flush>
      <div className="table-wrap"><table>
        <thead><tr><th>{t("Device")}</th><th>{t("Kind")}</th><th className="hide-sm">{t("Address")}</th><th>{t("Ports up")}</th><th>Firmware</th><th>{t("State")}</th></tr></thead>
        <tbody>{data.devices.map((d) => (
          <tr key={d.id} className="link" onClick={() => go(`/devices/${encodeURIComponent(d.id)}`)}>
            <td><b>{d.name}</b><span className="sub">{d.model}</span></td>
            <td>{kind[d.kind]}</td>
            <td className="hide-sm mono">{d.address}</td>
            <td className="num">{d.ports.filter((p) => p.up).length} / {d.ports.length}</td>
            <td>{d.firmware}{d.upgrade_to && <span className="sub">→ {d.upgrade_to}</span>}</td>
            <td><HealthPill health={d.health} /></td>
          </tr>
        ))}</tbody>
      </table></div>
    </Card>
  );
}

function Clients({ data }: { data: NetworkData }) {
  const t = useT();
  const canAct = useCan("operator");
  const flow = useJobFlow();
  const devices = Object.fromEntries(data.devices.map((d) => [d.mac, d.name]));
  const visible = data.clients.filter((c) => c.name);
  return (
    <Card flush>
      <div className="table-wrap"><table>
        <thead><tr><th>Client</th><th>{t("Address")}</th><th>VLAN</th><th>{t("Connected via")}</th><th>{t("Signal")}</th><th /></tr></thead>
        <tbody>{visible.map((c) => (
          <tr key={c.mac}>
            <td><b>{c.name}</b><span className="sub mono">{c.mac}</span></td>
            <td className="mono">{c.ip ?? "–"}</td>
            <td className="num">{c.vlan ?? 1}</td>
            <td>{devices[c.device_mac ?? ""] ?? "–"}{c.wired && c.port ? ` · Port ${c.port}` : c.wired ? "" : " · WLAN"}</td>
            <td className="num">{c.signal_dbm != null ? <span className={c.signal_dbm <= -75 ? "pill warn plain" : ""}>{c.signal_dbm} dBm</span> : "–"}</td>
            <td>{canAct && <button className="btn small" onClick={() => flow.start(() => api.post<Job>("/api/jobs", { title: t("Block {c_name}", { c_name: c.name }), target: `client:${c.mac}`, steps: [{ action: "unifi.client_block", params: { mac: c.mac, block: true } }] }))}>{t("Block")}</button>}</td>
          </tr>
        ))}</tbody>
      </table></div>
      {flow.dialog}
    </Card>
  );
}

function Vlans({ data }: { data: NetworkData }) {
  const t = useT();
  return (
    <div className="grid cols-2">
      <Card title={t("Networks")} flush>
        <div className="table-wrap"><table>
          <thead><tr><th>{t("Network")}</th><th>VLAN</th><th>Subnetz</th><th>{t("Isolated")}</th></tr></thead>
          <tbody>{data.networks.map((n) => (
            <tr key={n.id}><td><b>{n.name}</b><span className="sub">{n.purpose}</span></td><td className="num">{n.vlan ?? 1}</td><td className="mono">{n.subnet}</td><td>{n.isolated ? <span className="pill ok plain">{t("yes")}</span> : "–"}</td></tr>
          ))}</tbody>
        </table></div>
      </Card>
      <Card title={t("Firewall rules")} sub={t("In evaluation order")} flush>
        <div className="table-wrap"><table>
          <thead><tr><th>#</th><th>{t("Rule")}</th><th>{t("From → To")}</th><th>{t("Effect")}</th></tr></thead>
          <tbody>{data.firewall.map((r) => (
            <tr key={r.id}><td className="num">{r.order}</td><td><b>{r.name}</b>{r.ports && <span className="sub">{r.protocol} {r.ports}</span>}</td><td>{r.src.replace("net:", "")} → {r.dst.replace("net:", "")}</td><td><span className={`pill ${r.action === "allow" ? "ok" : "bad"} plain`}>{r.action === "allow" ? t("allow") : t("drop")}</span></td></tr>
          ))}</tbody>
        </table></div>
      </Card>
    </div>
  );
}

function PathCheck({ data }: { data: NetworkData }) {
  const t = useT();
  const { params } = useRoute();
  const { data: guests } = useData<Guest[]>("/api/guests", []);
  const { data: hosts } = useData<Host[]>("/api/hosts", []);
  const [src, setSrc] = useState(params.get("src") ?? "");
  const [dst, setDst] = useState(params.get("dst") ?? "");
  const [port, setPort] = useState("");
  const [result, setResult] = useState<{ reachable: boolean; steps: string[]; hops: string[] } | null>(null);
  const options = [
    ...data.clients.filter((c) => c.name).map((c) => ({ id: `client:${c.mac}`, label: `${c.name} (Client)` })),
    ...(guests ?? []).filter((g) => !g.template).map((g) => ({ id: g.id, label: `${g.name} (${g.kind.toUpperCase()})` })),
    ...(hosts ?? []).map((h) => ({ id: h.id, label: `${h.name} (Host)` })),
  ];
  async function check() {
    setResult(await api.post("/api/network/explain", { src, dst, port: port ? Number(port) : null }));
  }
  return (
    <div className="grid split">
      <Card title={t("Does A reach B?")} sub={t("OpsTend follows the path through Wi-Fi, switch ports, VLANs, the gateway firewall and the host bridge.")}>
        <form className="grid" onSubmit={(e) => { e.preventDefault(); check(); }}>
          <div className="grid cols-3">
            <label className="lbl">{t("From")}<select className="field" value={src} onChange={(e) => setSrc(e.target.value)}><option value="">–</option>{options.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}</select></label>
            <label className="lbl">{t("To")}<select className="field" value={dst} onChange={(e) => setDst(e.target.value)}><option value="">–</option>{options.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}</select></label>
            <label className="lbl">{t("Port (optional)")}<input className="field" inputMode="numeric" value={port} onChange={(e) => setPort(e.target.value.replace(/\D/g, ""))} /></label>
          </div>
          <div><button className="btn primary" disabled={!src || !dst}>{t("Check path")}</button></div>
        </form>
      </Card>
      <Card title={t("Result")}>
        {!result && <p className="muted">{t("Choose source and target.")}</p>}
        {result && (
          <div className="stack">
            <Pill state={result.reachable ? "ok" : "critical"}>{result.reachable ? t("reachable") : t("not reachable")}</Pill>
            <ol className="steps">{result.steps.map((s, i) => <li key={i}><span className={`dot ${result.reachable || i < result.steps.length - 1 ? "done" : "failed"}`}>{i + 1}</span><div>{s}</div></li>)}</ol>
          </div>
        )}
      </Card>
    </div>
  );
}
