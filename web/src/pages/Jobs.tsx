import { useState } from "react";
import { api, type AuditEntry, type Job } from "../api";
import { Icon } from "../components/Icon";
import { Card, Empty, Loading, Pill, Tabs } from "../components/ui";
import { go, href, useCan, useData, useLang, useRoute, useT, when } from "../lib";

export function Jobs() {
  const t = useT();
  const lang = useLang();
  const { params } = useRoute();
  const tab = params.get("tab") ?? "jobs";
  const { data: jobs } = useData<Job[]>("/api/jobs");
  const { data: log } = useData<AuditEntry[]>(tab === "log" ? "/api/events?limit=200" : null);
  return (
    <>
      <div className="page-head">
        <div><h1>{t("Jobs and log")}</h1>
          <p className="muted">{t("Every change is a job: preview, approval, execution and a check of the result. The log is tamper-evident.")}</p></div>
      </div>
      <Tabs current={tab === "log" ? "/jobs?tab=log" : "/jobs"} items={[{ path: "/jobs", label: t("Jobs"), count: jobs?.length }, { path: "/jobs?tab=log", label: t("Log") }]} />
      {tab === "log" ? (
        <Card flush>
          {!log ? <Loading /> : (
            <div className="table-wrap"><table>
              <thead><tr><th>{t("Time")}</th><th>{t("Who")}</th><th>{t("What")}</th><th>{t("Target")}</th><th>{t("Outcome")}</th></tr></thead>
              <tbody>{log.map((e) => (
                <tr key={e.seq} className={e.job_id ? "link" : ""} onClick={() => e.job_id && go(`/jobs/${e.job_id}`)}>
                  <td className="num small">{new Date(e.at).toLocaleString(lang)}</td><td>{e.actor}</td>
                  <td>{String(e.detail.title ?? e.detail.effect ?? e.action)}<span className="sub mono">{e.action}</span></td>
                  <td className="mono small">{e.target}</td><td><Pill state={e.outcome === "ok" ? "ok" : "failed"}>{e.outcome}</Pill></td>
                </tr>
              ))}</tbody>
            </table></div>
          )}
        </Card>
      ) : (
        <Card flush>
          {!jobs ? <Loading /> : jobs.length === 0 ? <Empty>{t("No jobs yet.")}</Empty> : (
            <div className="table-wrap"><table>
              <thead><tr><th>{t("Job")}</th><th className="hide-sm">{t("By")}</th><th className="hide-sm">{t("Updated")}</th><th>{t("State")}</th></tr></thead>
              <tbody>{jobs.map((j) => (
                <tr key={j.id} className="link" onClick={() => go(`/jobs/${j.id}`)}>
                  <td><b>{j.title}</b><span className="sub">{j.steps.length} {t("steps")} · {j.target}</span></td>
                  <td className="hide-sm">{j.requested_by}</td><td className="hide-sm small">{when(j.updated_at, lang)}</td><td><Pill state={j.state} /></td>
                </tr>
              ))}</tbody>
            </table></div>
          )}
        </Card>
      )}
    </>
  );
}

export function JobDetail({ id }: { id: string }) {
  const t = useT();
  const lang = useLang();
  const canAct = useCan("operator");
  const { data, reload } = useData<{ job: Job; events: AuditEntry[] }>(`/api/jobs/${id}`);
  const [error, setError] = useState<string | null>(null);
  if (!data) return <Loading />;
  const j = data.job;
  async function act(kind: "approve" | "reject") {
    setError(null);
    try {
      await api.post(`/api/jobs/${j.id}/${kind}`, kind === "approve" ? { fingerprint: j.fingerprint } : { reason: "" });
      reload();
    } catch (e) {
      setError(String((e as Error).message));
    }
  }
  const dot = (s: string) => ({ done: "done", running: "running", failed: "failed", rolled_back: "failed" }[s] ?? "");
  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow"><a href={href("/jobs")}>{t("Jobs")}</a> · {j.target}</div>
          <h1>{j.title}</h1>
          <p className="muted">{t("Requested by")} {j.requested_by} · {when(j.created_at, lang)}{j.approved_by ? ` · ${t("approved by")} ${j.approved_by}` : ""}</p>
        </div>
        <Pill state={j.state} />
      </div>
      {error && <div className="banner warn">{error}</div>}
      {j.message && <div className={`banner${["failed", "rolled_back", "rejected"].includes(j.state) ? " warn" : ""}`}>{j.message}</div>}
      {j.state === "awaiting_approval" && canAct && (
        <Card>
          <div className="row between">
            <span>{t("Review the steps. After approval OpsTend runs them and reads the result back.")}</span>
            <div className="row">
              <button className="btn" onClick={() => act("reject")}>{t("Reject")}</button>
              <button className="btn primary" onClick={() => act("approve")}><Icon name="check" />{t("Approve")}</button>
            </div>
          </div>
        </Card>
      )}
      <div className="grid split">
        <Card title={t("Steps")}>
          <ol className="steps">
            {j.steps.map((s, i) => (
              <li key={i}>
                <span className={`dot ${dot(s.state)}`}>{s.state === "done" ? "✓" : i + 1}</span>
                <div>
                  <b>{s.title || s.effect}</b>
                  {s.title && s.effect && s.title !== s.effect && <div className="muted small">{s.effect}</div>}
                  <div className="small muted"><span className="mono">{s.action}</span> · <Pill state={s.state === "done" ? "succeeded" : s.state}>{undefined}</Pill></div>
                  {s.error && <div className="banner warn small" style={{ marginTop: 6 }}>{s.error}</div>}
                </div>
              </li>
            ))}
          </ol>
          {j.reason && <p className="small muted" style={{ marginTop: 12 }}>{t("Reason")}: {j.reason}</p>}
        </Card>
        <Card title={t("History")} flush>
          {data.events.length === 0 && <Empty>–</Empty>}
          {data.events.map((e) => (
            <div key={e.seq} className="item small">
              <div className="row between"><b>{e.action}</b><span className="muted num">{new Date(e.at).toLocaleTimeString(lang)}</span></div>
              <span className="muted">{e.actor}{e.outcome !== "ok" ? ` · ${e.outcome}` : ""}</span>
            </div>
          ))}
          <div className="item small muted">{t("Preview fingerprint")}: <span className="mono">{j.fingerprint}</span></div>
        </Card>
      </div>
    </>
  );
}
