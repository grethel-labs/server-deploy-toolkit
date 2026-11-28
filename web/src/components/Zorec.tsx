import { useEffect, useRef, useState } from "react";
import { api, type Job } from "../api";
import { go, href, useCan, useRoute, useT } from "../lib";
import { Icon } from "./Icon";
import { useJobFlow } from "./ui";

type Answer = {
  text: string; source: "rules" | "model"; model: string | null;
  links: { label: string; href: string }[];
  actions: { title: string; action: string; params: Record<string, unknown> }[];
};
type Msg = { role: "user" | "assistant"; text: string; answer?: Answer };

const STORE = "opstend.zorec";

function load(): Msg[] {
  try {
    return JSON.parse(sessionStorage.getItem(STORE) || "[]");
  } catch {
    return [];
  }
}

export function Zorec({ status }: { status: { busy: boolean; label: string } }) {
  const t = useT();
  const route = useRoute();
  const canEdit = useCan("operator");
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<"read" | "edit">("read");
  const [log, setLog] = useState<Msg[]>(load);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const end = useRef<HTMLDivElement>(null);
  const flow = useJobFlow();

  useEffect(() => {
    try {
      sessionStorage.setItem(STORE, JSON.stringify(log.slice(-30)));
    } catch {
      /* ignore */
    }
    end.current?.scrollIntoView({ block: "end" });
  }, [log, open]);

  async function ask(question: string) {
    if (!question.trim()) return;
    setText("");
    const history = log.slice(-8).map((m) => ({ role: m.role, content: m.text }));
    setLog((l) => [...l, { role: "user", text: question }]);
    setBusy(true);
    try {
      const answer = await api.post<Answer>("/api/zorec", {
        question, mode, history, page: { path: route.path, params: Object.fromEntries(route.params) },
      });
      setLog((l) => [...l, { role: "assistant", text: answer.text, answer }]);
    } catch (e) {
      setLog((l) => [...l, { role: "assistant", text: String((e as Error).message) }]);
    } finally {
      setBusy(false);
    }
  }

  function act(a: Answer["actions"][number]) {
    if (a.action === "rollout.plan") return go("/updates");
    if (a.action === "deploy.analyze") return go(`/services/new?repo=${encodeURIComponent(String(a.params.repo ?? ""))}`);
    const target = String(a.params.guest ?? a.params.device ?? a.params.host ?? a.params.service ?? "fleet");
    flow.start(() => api.post<Job>("/api/jobs", { title: a.title, target, reason: "Zorec", steps: [{ action: a.action, params: a.params }] }));
  }

  const suggestions = [
    t("What is going on?"),
    t("Which updates are available?"),
    t("Anything odd in the network?"),
    t("How is security?"),
  ];

  if (!open) {
    return (
      <button className={`zorec-fab${status.busy ? " busy" : ""}`} onClick={() => setOpen(true)} aria-label={t("Ask Zorec")}>
        <img src="/brand/zorec.svg" alt="" />
        <span>{t("Ask Zorec")}<small>{status.label}</small></span>
      </button>
    );
  }

  return (
    <aside className={`zorec${mode === "edit" ? " edit" : ""}`} aria-label="Zorec">
      <header>
        <img src="/brand/zorec.svg" alt="" />
        <div style={{ flex: 1 }}>
          <b>Zorec</b>
          <div className="small muted">{status.label}</div>
        </div>
        {canEdit && (
          <div className="seg" role="group" aria-label={t("Mode")}>
            <button aria-pressed={mode === "read"} onClick={() => setMode("read")}>{t("Read")}</button>
            <button aria-pressed={mode === "edit"} onClick={() => setMode("edit")}>{t("Change")}</button>
          </div>
        )}
        <button className="icon-btn" onClick={() => setOpen(false)} aria-label={t("Close")}><Icon name="close" /></button>
      </header>
      <div className="log" aria-live="polite">
        {log.length === 0 && (
          <div className="msg">
            {t("I know the current state of your systems. Ask me anything – in “Change” mode I suggest jobs for you to approve.")}
          </div>
        )}
        {log.map((m, i) => (
          <div key={i} className={`msg${m.role === "user" ? " me" : ""}`}>
            {m.role === "assistant" && m.answer && (
              <div className="meta">Zorec · {m.answer.source === "model" ? m.answer.model : t("rule based")}</div>
            )}
            {m.text}
            {m.answer && (m.answer.links.length > 0 || m.answer.actions.length > 0) && (
              <div className="chips" style={{ marginTop: 8 }}>
                {m.answer.links.map((l) => <a key={l.href} className="btn small" href={l.href.startsWith("#") ? l.href : href(l.href)}>{l.label}</a>)}
                {m.answer.actions.map((a, j) => (
                  <button key={j} className="btn small primary" onClick={() => act(a)}><Icon name="arrow" size={14} />{a.title}</button>
                ))}
              </div>
            )}
          </div>
        ))}
        {busy && <div className="msg muted">{t("Zorec is checking …")}</div>}
        <div ref={end} />
      </div>
      <footer>
        <div className="chips">
          {suggestions.map((s) => <button key={s} onClick={() => ask(s)}>{s}</button>)}
        </div>
        <form className="row" onSubmit={(e) => { e.preventDefault(); ask(text); }}>
          <label className="sr-only" htmlFor="zorec-q">{t("Question for Zorec")}</label>
          <input id="zorec-q" className="field" style={{ flex: 1 }} value={text} onChange={(e) => setText(e.target.value)}
            placeholder={t("Ask about a service, device or job")} />
          <button className="btn primary" disabled={busy || !text.trim()}>{t("Send")}</button>
        </form>
      </footer>
      {flow.dialog}
    </aside>
  );
}
