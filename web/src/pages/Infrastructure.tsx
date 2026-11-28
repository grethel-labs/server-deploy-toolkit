import type { Guest, Host } from "../api";
import { Card, Empty, HealthPill, Loading, Meter, Pill, Tabs } from "../components/ui";
import { gb, go, pct, useData, useRoute, useT } from "../lib";

export function Infrastructure() {
  const t = useT();
  const { params } = useRoute();
  const tab = params.get("tab") ?? "hosts";
  const site = params.get("site");
  const { data: hosts } = useData<Host[]>("/api/hosts");
  const { data: guests } = useData<Guest[]>("/api/guests");
  if (!hosts || !guests) return <Loading />;
  const shownHosts = hosts.filter((h) => !site || h.site === site);
  const hostIds = new Set(shownHosts.map((h) => h.id));
  const shownGuests = guests.filter((g) => hostIds.has(g.host_id));

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">{site ?? t("All sites")}</div>
          <h1>{t("Hosts and guests")}</h1>
          <p className="muted">{t("Proxmox, Linux servers and their VMs, LXC and containers – with the same actions on every platform.")}</p>
        </div>
        {site && <a className="btn" href="#/infra">{t("Show all sites")}</a>}
      </div>
      <Tabs current={tab === "guests" ? "/infra?tab=guests" : "/infra"} items={[
        { path: "/infra", label: "Hosts", count: shownHosts.length },
        { path: "/infra?tab=guests", label: t("Guests and containers"), count: shownGuests.filter((g) => !g.template).length },
      ]} />
      {tab === "guests" ? <GuestTable guests={shownGuests} /> : (
        <Card flush>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Host</th><th>{t("Platform")}</th><th className="hide-sm">CPU</th><th className="hide-sm">{t("Memory")}</th><th>{t("Guests")}</th><th>Updates</th><th>{t("State")}</th></tr></thead>
              <tbody>
                {shownHosts.map((h) => (
                  <tr key={h.id} className="link" onClick={() => go(`/hosts/${encodeURIComponent(h.id)}`)}>
                    <td><b>{h.name}</b><span className="sub">{h.address} · {h.site}</span></td>
                    <td>{h.platform === "proxmox" ? "Proxmox VE" : "Linux"}<span className="sub">{h.os}</span></td>
                    <td className="hide-sm" style={{ minWidth: 120 }}><span className="num">{pct(h.usage.cpu_pct)}</span><Meter value={h.usage.cpu_pct} max={100} /></td>
                    <td className="hide-sm" style={{ minWidth: 140 }}><span className="num">{gb(h.usage.memory_mb)} / {gb(h.capacity.memory_mb)}</span><Meter value={h.usage.memory_mb} max={h.capacity.memory_mb} /></td>
                    <td className="num">{h.running} / {h.guests}</td>
                    <td className="num">{h.updates ? <span className="pill warn plain">{h.updates}</span> : "–"}</td>
                    <td><HealthPill health={h.health} />{h.stale && <span className="sub">{t("Data stale")}</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  );
}

export function GuestTable({ guests }: { guests: Guest[] }) {
  const t = useT();
  if (guests.length === 0) return <Card><Empty>{t("No guests.")}</Empty></Card>;
  const kind = { vm: "VM", lxc: "LXC", container: "Container" };
  return (
    <Card flush>
      <div className="table-wrap">
        <table>
          <thead><tr><th>{t("Name")}</th><th>{t("Kind")}</th><th>Host</th><th className="hide-sm">{t("Address")}</th><th className="hide-sm">VLAN</th><th>{t("State")}</th></tr></thead>
          <tbody>
            {guests.map((g) => (
              <tr key={g.id} className="link" onClick={() => go(`/guests/${encodeURIComponent(g.id)}`)}>
                <td><b>{g.name}</b><span className="sub">{g.template ? t("Template") : g.image ?? `#${g.native_id}`}</span></td>
                <td>{kind[g.kind]}<span className="sub">{g.platform === "proxmox" ? "Proxmox" : g.kind === "container" ? "Podman" : "libvirt"}</span></td>
                <td>{g.host_name}</td>
                <td className="hide-sm mono">{g.nics.flatMap((n) => n.ips)[0] ?? "–"}</td>
                <td className="hide-sm num">{g.nics.map((n) => n.vlan).filter(Boolean).join(", ") || "–"}</td>
                <td><Pill state={g.template ? "unknown" : g.state}>{g.template ? t("Template") : undefined}</Pill></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
