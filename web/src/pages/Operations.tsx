import { api, type AuditEntry, type Insight, type Job, type Service } from "../api";
import { Icon } from "../components/Icon";
import { Card, Empty, Kpi, Loading, Meter, Pill, useJobFlow } from "../components/ui";
import { clock, gb, go, href, useCan, useData, useLang, useSession, useT } from "../lib";

type Incident = { id: string; target: string; title: string; detail: string; severity: string; state: string; job_id: string | null };
type Overview = {
  incidents: Incident[]; unhealthy: Service[]; decisions: Job[]; working: Job[];
  counts: Record<string, number>;
  capacity: { cpu_cores: number; memory_mb: number; memory_used_mb: number; disk_gb: number; disk_used_gb: number; measured: number };
  sites: { name: string; hosts: number; ok: number; workloads: number; stale: number }[];
  insights: Insight[]; activity: AuditEntry[];
  cluster: { role: string; term: number; leader: string | null; voters: string[]; nodes: number };
};

export function Operations() {
  const t = useT();
  const lang = useLang();
  const { session } = useSession();
  const canAct = useCan("operator");
  const { data } = useData<Overview>("/api/overview");
  const flow = useJobFlow();
  if (!data) return <Loading />;
  const c = data.counts;
  const incidents = data.incidents.filter((i) => i.state !== "resolved");

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">{session.site} · {t("whole fleet")}</div>
          <h1>{t("Operations")}</h1>
        </div>
        <div className="row">
          {session.demo && <span className="pill info plain">{t("Demo with example data")}</span>}
          <a className="btn" href={href("/network/topology")}><Icon name="map" />{t("Open topology")}</a>
        </div>
      </div>

      <div className="grid cols-2">
        <Card title={<span className="row"><Icon name="alert" />{t("Incidents")}</span>} action={<span className="pill plain">{incidents.length + data.unhealthy.length}</span>} flush>
          {incidents.length === 0 && data.unhealthy.length === 0 && <Empty>{t("No incidents. Everything runs.")}</Empty>}
          {incidents.map((i) => (
            <div key={i.id} className={`item sev-stripe ${i.severity}`}>
              <div className="row between">
                <span className="title">{i.title}</span>
                <Pill state={i.state} />
              </div>
              <div className="muted small">{i.detail}</div>
              {i.job_id && <div><a className="btn small" href={href(`/jobs/${i.job_id}`)}>{t("Follow the check")}<Icon name="arrow" size={14} /></a></div>}
            </div>
          ))}
          {data.unhealthy.filter((s) => !incidents.some((i) => i.target === `service/${s.id}`)).map((s) => (
            <div key={s.id} className="item sev-stripe high">
              <div className="row between"><span className="title">{s.name}</span><Pill state={s.health} /></div>
              <div className="muted small">{s.description}</div>
            </div>
          ))}
        </Card>

        <Card title={<span className="row"><Icon name="shield" />{t("Pending decisions")}</span>} action={<span className="pill plain">{data.decisions.length}</span>} flush>
          {data.decisions.length === 0 && <Empty>{t("Nothing is waiting for you.")}</Empty>}
          {data.decisions.map((j) => (
            <div key={j.id} className="item">
              <div className="row between">
                <span className="title">{j.title}</span>
                <span className="pill warn">{j.requested_by === "zorec" ? t("Suggested by Zorec") : t("Your approval")}</span>
              </div>
              {j.reason && <div className="muted small">{j.reason}</div>}
              <div className="row">
                <a className="btn small" href={href(`/jobs/${j.id}`)}>{t("Review details")}</a>
                {canAct && (
                  <button className="btn small primary" onClick={() => flow.start(async () => j)}>
                    <Icon name="check" size={14} />{t("Approve …")}
                  </button>
                )}
              </div>
            </div>
          ))}
        </Card>
      </div>

      {data.working.length > 0 && (
        <Card title={<span className="row"><img src="/brand/zorec.svg" alt="" width={22} />{t("Zorec is working on")}</span>} flush>
          {data.working.map((j) => (
            <a key={j.id} className="item" href={href(`/jobs/${j.id}`)} style={{ color: "inherit" }}>
              <div className="row between"><span className="title">{j.title}</span><Pill state={j.state} /></div>
              <div className="muted small">{j.not_before ? t("Starts in the maintenance window {tolocalestring_lang}", { tolocalestring_lang: new Date(j.not_before).toLocaleString(lang) }) : j.reason}</div>
            </a>
          ))}
        </Card>
      )}

      <div className="grid cols-4">
        <Kpi label="Hosts" value={c.hosts} hint={t("{hosts_ok} healthy", { hosts_ok: c.hosts_ok })} onClick={() => go("/infra")} />
        <Kpi label={t("Guests and containers")} value={c.guests} hint={t("{guests_running} running", { guests_running: c.guests_running })} onClick={() => go("/infra?tab=guests")} />
        <Kpi label={t("Security")} value={c.findings_high} hint={t("important findings")} onClick={() => go("/security")} />
        <Kpi label="Updates" value={c.updates} hint={t("{updates_security} security", { updates_security: c.updates_security })} onClick={() => go("/updates")} />
      </div>

      <div className="grid split">
        <div className="grid">
          <Card title={t("Sites")} sub={t("Reachability and workload")} flush>
            <div className="table-wrap">
              <table>
                <thead><tr><th>{t("Site")}</th><th>Hosts</th><th>{t("Workloads")}</th><th>{t("State")}</th></tr></thead>
                <tbody>
                  {data.sites.map((s) => (
                    <tr key={s.name} className="link" onClick={() => go(`/infra?site=${encodeURIComponent(s.name)}`)}>
                      <td><b>{s.name}</b></td>
                      <td className="num">{s.ok} / {s.hosts} {t("reachable")}{s.stale > 0 && <span className="sub">{t("{s_stale} with stale data", { s_stale: s.stale })}</span>}</td>
                      <td className="num">{s.workloads}</td>
                      <td><Pill state={s.ok === s.hosts && !s.stale ? "ok" : "warning"} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <Card title={t("Network insights")} sub={t("Found across UniFi, Proxmox and Linux")}
            action={<a href={href("/network")} className="small">{t("See all")}</a>} flush>
            {data.insights.length === 0 && <Empty>{t("Nothing unusual.")}</Empty>}
            {data.insights.map((i) => (
              <div key={i.id} className={`item sev-stripe ${i.severity}`}>
                <div className="row between"><span className="title">{i.title}</span><Pill state={i.severity} /></div>
                <div className="muted small">{i.detail}</div>
                {i.fix && canAct && (
                  <div><button className="btn small" onClick={() => flow.start(() => api.post<Job>(`/api/network/insights/${i.id}/fix`))}>{i.fix.summary}</button></div>
                )}
              </div>
            ))}
          </Card>
        </div>

        <div className="grid">
          <Card title={t("Capacity")} sub={t("{capacity_measured} hosts with current measurements", { capacity_measured: data.capacity.measured })}>
            <div className="stack">
              <div className="row between"><span>{t("Memory")}</span><b className="num">{gb(data.capacity.memory_used_mb)} / {gb(data.capacity.memory_mb)}</b></div>
              <Meter value={data.capacity.memory_used_mb} max={data.capacity.memory_mb} label={t("Memory")} />
              <div className="row between" style={{ marginTop: 10 }}><span>{t("Local storage")}</span><b className="num">{data.capacity.disk_used_gb} / {data.capacity.disk_gb} GB</b></div>
              <Meter value={data.capacity.disk_used_gb} max={data.capacity.disk_gb} label={t("Storage")} />
              <div className="muted small" style={{ marginTop: 6 }}>{data.capacity.cpu_cores} {t("CPU cores in total")}</div>
            </div>
          </Card>
          <Card title={t("Control plane")}>
            <div className="stack small">
              <div className="row between"><span>{t("This node")}</span><Pill state={data.cluster.role === "leader" ? "ok" : "unknown"}>{data.cluster.role}</Pill></div>
              <div className="row between"><span>Voter</span><b className="num">{data.cluster.voters.length}</b></div>
              <div className="row between"><span>{t("Nodes")}</span><b className="num">{data.cluster.nodes}</b></div>
              <a href={href("/cluster")}>{t("View roles and failover")}</a>
            </div>
          </Card>
          <Card title={t("Recent activity")} action={<a className="small" href={href("/jobs?tab=log")}>{t("Log")}</a>} flush>
            {data.activity.length === 0 && <Empty>{t("No entries yet.")}</Empty>}
            {data.activity.map((e) => (
              <div key={e.seq} className="item small">
                <div className="row between"><b>{String(e.detail.title ?? e.detail.effect ?? e.action)}</b><span className="muted num">{clock(e.at, lang)}</span></div>
                <span className="muted">{e.actor} · {e.target}{e.outcome !== "ok" ? ` · ${e.outcome}` : ""}</span>
              </div>
            ))}
          </Card>
        </div>
      </div>
      {flow.dialog}
    </>
  );
}
