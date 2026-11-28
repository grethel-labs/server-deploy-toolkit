import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, type Health, type Job } from "../api";
import { go, href, useT } from "../lib";

export function Card({ title, sub, action, children, flush }: { title?: ReactNode; sub?: ReactNode; action?: ReactNode; children: ReactNode; flush?: boolean }) {
  return (
    <section className="card">
      {title && (
        <header>
          <div>
            <h2>{title}</h2>
            {sub && <div className="sub">{sub}</div>}
          </div>
          {action}
        </header>
      )}
      <div className={flush ? "body flush" : "body"}>{children}</div>
    </section>
  );
}

export function Kpi({ label, value, hint, onClick }: { label: string; value: ReactNode; hint?: ReactNode; onClick?: () => void }) {
  return (
    <div className="card kpi" onClick={onClick} role={onClick ? "link" : undefined} style={onClick ? { cursor: "pointer" } : undefined}>
      <span className="label">{label}</span>
      <span className="value">{value}</span>
      {hint && <span className="hint">{hint}</span>}
    </div>
  );
}

const TONE: Record<string, string> = {
  ok: "ok", running: "ok", succeeded: "ok", healthy: "ok", done: "ok", handling: "info",
  warning: "warn", medium: "warn", waiting: "warn", awaiting_approval: "warn", scheduled: "info", verifying: "info", paused: "warn", suspect: "warn",
  critical: "bad", high: "bad", failed: "bad", rolled_back: "bad", needs_operator: "bad", down: "bad", rejected: "plain",
  low: "info", stopped: "plain", unknown: "plain", maintenance: "info", previewed: "plain", cancelled: "plain",
};

export function Pill({ state, children }: { state: string; children?: ReactNode }) {
  const t = useT();
  const labels: Record<string, string> = {
    ok: t("Healthy"), warning: t("Warning"), critical: t("Failing"),
    unknown: t("Unknown"), maintenance: t("Maintenance"),
    running: t("Running"), stopped: t("Stopped"), paused: t("Paused"),
    awaiting_approval: t("Awaiting approval"), scheduled: t("Scheduled"),
    verifying: t("Verifying"), succeeded: t("Done"), failed: t("Failed"),
    rolled_back: t("Rolled back"), rejected: t("Rejected"), previewed: t("Preview"),
    handling: t("Being fixed"), waiting: t("Waiting"), needs_operator: t("Needs you"),
    critical_sev: "", high: t("High"), medium: t("Medium"), low: t("Low"),
    healthy: t("Healthy"), suspect: t("Suspect"), down: t("Down"),
    joining: t("Joining"), draining: t("Draining"),
  };
  const tone = TONE[state] ?? "plain";
  return <span className={`pill ${tone}`}>{children ?? labels[state] ?? state}</span>;
}

export function HealthPill({ health }: { health: Health }) {
  return <Pill state={health} />;
}

export function Meter({ value, max, label }: { value: number | null | undefined; max: number; label?: string }) {
  if (value == null || !max) return <div className="meter" aria-label={label} />;
  const ratio = Math.min(1, value / max);
  const tone = ratio > 0.9 ? "bad" : ratio > 0.75 ? "warn" : "";
  return (
    <div className={`meter ${tone}`} role="meter" aria-valuemin={0} aria-valuemax={max} aria-valuenow={value} aria-label={label}>
      <span style={{ width: `${ratio * 100}%` }} />
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Loading() {
  const t = useT();
  return <div className="empty">{t("Loading …")}</div>;
}

export function Dialog({ open, onClose, title, children, footer }: { open: boolean; onClose: () => void; title: ReactNode; children: ReactNode; footer?: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog ref={ref} className="dlg" onClose={onClose} onCancel={onClose}>
      <header><h2>{title}</h2></header>
      <div className="body">{children}</div>
      {footer && <footer>{footer}</footer>}
    </dialog>
  );
}

/** Create a job preview, show its effect, then approve it in the same dialog. */
export function useJobFlow() {
  const t = useT();
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function start(create: () => Promise<Job>) {
    setError(null);
    setBusy(true);
    try {
      setJob(await create());
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    if (!job) return;
    setBusy(true);
    try {
      await api.post(`/api/jobs/${job.id}/approve`, { fingerprint: job.fingerprint });
      const id = job.id;
      setJob(null);
      go(`/jobs/${id}`);
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  }

  const dialog = (
    <Dialog
      open={job !== null || error !== null}
      onClose={() => { setJob(null); setError(null); }}
      title={job ? job.title : t("That did not work")}
      footer={
        <>
          <button className="btn" onClick={() => { setJob(null); setError(null); }}>{t("Cancel")}</button>
          {job && <a className="btn" href={href(`/jobs/${job.id}`)} onClick={() => setJob(null)}>{t("Decide later")}</a>}
          {job && <button className="btn primary" disabled={busy} onClick={approve}>{t("Approve and run")}</button>}
        </>
      }
    >
      {error && <div className="banner warn">{error}</div>}
      {job && (
        <>
          <p className="muted small">{t("This happens when you approve. OpsTend checks the result afterwards.")}</p>
          <ol className="steps">
            {job.steps.map((s, i) => (
              <li key={i}><span className="dot">{i + 1}</span><div>{s.title || s.effect}</div></li>
            ))}
          </ol>
          {job.reason && <p className="small muted">{t("Reason")}: {job.reason}</p>}
        </>
      )}
    </Dialog>
  );
  return { start, dialog, busy };
}

export function Tabs({ items, current }: { items: { path: string; label: string; count?: number }[]; current: string }) {
  return (
    <nav className="tabs">
      {items.map((i) => (
        <a key={i.path} href={href(i.path)} aria-current={current === i.path ? "page" : undefined}>
          {i.label}{i.count ? <span className="muted"> · {i.count}</span> : null}
        </a>
      ))}
    </nav>
  );
}
