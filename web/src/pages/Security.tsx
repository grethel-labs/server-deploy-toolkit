import type { Finding, Insight } from "../api";
import { Card, Empty, Kpi, Loading, Pill } from "../components/ui";
import { href, useData, useT } from "../lib";

export function Security() {
  const t = useT();
  const { data } = useData<{ findings: Finding[]; network: Insight[] }>("/api/security");
  if (!data) return <Loading />;
  const f = data.findings;
  const by = (k: Finding["kind"]) => f.filter((x) => x.kind === k);
  return (
    <>
      <div className="page-head">
        <div>
          <h1>{t("Security")}</h1>
          <p className="muted">{t("Independent scans of packages, images and hardening. Known exploitation (KEV), exploit probability (EPSS) and exposure set the order.")}</p>
        </div>
      </div>
      <div className="grid cols-4">
        <Kpi label={t("Critical or high")} value={f.filter((x) => ["critical", "high"].includes(x.severity)).length} />
        <Kpi label={t("Known exploited")} value={f.filter((x) => x.kev).length} hint="CISA KEV" />
        <Kpi label={t("Reachable from outside")} value={f.filter((x) => x.exposed).length} />
        <Kpi label={t("Hardening")} value={by("hardening").length} hint={t("open items")} />
      </div>
      <Card title={t("Findings by urgency")} flush>
        {f.length === 0 && <Empty>{t("No open findings.")}</Empty>}
        {f.map((x) => (
          <div key={x.id} className={`item sev-stripe ${x.severity}`}>
            <div className="row between">
              <span className="title">{x.title}</span>
              <div className="row">
                {x.kev && <span className="pill bad plain">KEV</span>}
                {x.exposed && <span className="pill warn plain">{t("public")}</span>}
                {x.epss != null && <span className="pill plain">EPSS {(x.epss * 100).toFixed(0)} %</span>}
                <Pill state={x.severity} />
              </div>
            </div>
            {x.detail && <span className="muted small">{x.detail}</span>}
            <span className="small">{x.target}{x.installed ? ` · ${x.installed}` : ""}{x.fixed ? ` → ${x.fixed}` : ""}</span>
            {x.recommendation && <span className="small"><b>{t("Recommendation")}:</b> {x.recommendation}</span>}
          </div>
        ))}
      </Card>
      {data.network.length > 0 && (
        <Card title={t("Network segmentation")} flush>
          {data.network.map((i) => <div key={i.id} className={`item sev-stripe ${i.severity}`}><span className="title">{i.title}</span><span className="muted small">{i.detail}</span><a className="small" href={href("/network/vlans")}>{t("View firewall")}</a></div>)}
        </Card>
      )}
    </>
  );
}
