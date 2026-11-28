import { api, type Job } from "../api";
import { Icon } from "../components/Icon";
import { Card, Empty, Loading, useJobFlow } from "../components/ui";
import { useCan, useData, useLang, useT } from "../lib";

type Update = { id: string; target: string; kind: string; current: string; available: string; security: boolean; reboot: boolean; notes: string };
type Wave = { index: number; targets: string[]; reason: string };
type Data = { updates: Update[]; waves: Wave[]; names: Record<string, string>; plan_error: string | null; next_window: string; window: { days: string[]; start: string; duration_minutes: number; timezone: string } };

export function Updates() {
  const t = useT();
  const lang = useLang();
  const canAct = useCan("operator");
  const flow = useJobFlow();
  const { data } = useData<Data>("/api/updates");
  if (!data) return <Loading />;
  const groups: Record<string, Update[]> = {};
  data.updates.forEach((u) => (groups[u.target] ??= []).push(u));
  const kind: Record<string, string> = { package: t("Packages"), firmware: "Firmware", opstend: "OpsTend", app: t("Application") };
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Updates</h1>
          <p className="muted">{t("OpsTend installs security updates in the maintenance window. Everything else runs as one coordinated rollout across all systems.")}</p>
        </div>
        <div className="small muted">{t("Next maintenance window")}: <b>{new Date(data.next_window).toLocaleString(lang, { weekday: "long", hour: "2-digit", minute: "2-digit" })}</b></div>
      </div>
      <div className="grid split">
        <Card title={t("Available")} flush>
          {data.updates.length === 0 && <Empty>{t("Everything is up to date.")}</Empty>}
          {Object.entries(groups).map(([target, ups]) => (
            <div key={target} className="item">
              <div className="row between"><b>{data.names[target] ?? target}</b><span className="muted small">{kind[ups[0].kind] ?? ups[0].kind}</span></div>
              {ups.map((u) => (
                <div key={u.id} className="row small">
                  <span style={{ minWidth: 160 }}>{u.notes || u.id}</span>
                  <span className="mono muted">{u.current} → {u.available}</span>
                  {u.security && <span className="pill bad plain">{t("Security")}</span>}
                  {u.reboot && <span className="pill warn plain">{t("Reboot")}</span>}
                </div>
              ))}
            </div>
          ))}
        </Card>
        <Card title={t("Coordinated rollout")} sub={t("Order that keeps quorum and updates edge devices before the core")}>
          {data.plan_error && <div className="banner warn">{data.plan_error}</div>}
          <ol className="steps">
            {data.waves.map((w) => (
              <li key={w.index}>
                <span className="dot">{w.index + 1}</span>
                <div><b>{w.targets.map((x) => data.names[x] ?? x).join(", ")}</b><div className="muted small">{w.reason}</div></div>
              </li>
            ))}
          </ol>
          {canAct && data.waves.length > 0 && (
            <button className="btn primary" style={{ marginTop: 12 }} onClick={() => flow.start(() => api.post<Job>("/api/rollouts", {}))}>
              <Icon name="refresh" />{t("Prepare rollout")}
            </button>
          )}
          <p className="muted small" style={{ marginTop: 12 }}>{t("After each wave OpsTend checks health. If a wave fails, the rollout stops and the affected system returns to its previous state.")}</p>
        </Card>
      </div>
      {flow.dialog}
    </>
  );
}
