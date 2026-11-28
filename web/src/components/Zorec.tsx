import { Fragment, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { api, onLive, type Job } from "../api";
import { go, href, useCan, useData, useRoute, useT } from "../lib";
import { Diagram } from "./Diagram";
import { Icon } from "./Icon";
import { Pill } from "./ui";

type Ui = { kind: "navigate" | "highlight" | "fill" | "diagram"; target: string; value: string; label: string };
type Answer = {
  text: string; source: "rules" | "model"; model: string | null; provider: string | null; seconds: number | null;
  links: { label: string; href: string }[];
  actions: { title: string; action: string; params: Record<string, unknown> }[];
  ui: Ui[];
};
type Msg = { id: string; role: "user" | "assistant"; text: string; answer?: Answer; done?: string[]; jobs?: string[] };

let seq = 0;
const newId = () => `${Date.now().toString(36)}-${(seq++).toString(36)}`;
type Status = { mode: "rules" | "model"; reason: string; enabled: boolean; capabilities: string[] };
type Mood = "idle" | "thinking" | "acting" | "happy";
type Sketch = { label: string; source: string };

const STORE = "opstend.zorec";

function load(): Msg[] {
  try {
    return (JSON.parse(sessionStorage.getItem(STORE) || "[]") as Msg[]).map((m) => ({ ...m, id: m.id ?? newId() }));
  } catch {
    return [];
  }
}

// --- what Zorec can see and touch on the current page -------------------------------

function labelOf(el: Element): string {
  const own = el.getAttribute("aria-label") || el.getAttribute("data-zorec-label");
  if (own) return own;
  if (el instanceof HTMLInputElement || el instanceof HTMLSelectElement || el instanceof HTMLTextAreaElement) {
    const lbl = el.closest("label")?.textContent || (el.id && document.querySelector(`label[for="${el.id}"]`)?.textContent);
    return (lbl || el.getAttribute("placeholder") || el.name || "").trim().slice(0, 60);
  }
  return (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 60);
}

function pageContext(path: string, params: URLSearchParams) {
  const main = document.getElementById("main");
  const elements = Array.from(main?.querySelectorAll("[data-zorec]") ?? [])
    .slice(0, 40)
    .map((el) => ({ key: el.getAttribute("data-zorec"), label: labelOf(el) }));
  const fields = Array.from(main?.querySelectorAll<HTMLInputElement>("input[name], select[name], textarea[name]") ?? [])
    .filter((el) => el.type !== "password" && el.type !== "hidden")
    .slice(0, 30)
    .map((el) => ({ name: el.name, label: labelOf(el), value: el.type === "checkbox" ? String(el.checked) : String(el.value).slice(0, 60) }));
  return {
    route: `#${path}`,
    params: Object.fromEntries(params),
    title: main?.querySelector("h1")?.textContent ?? "",
    elements,
    fields,
  };
}

function find(key: string): HTMLElement | null {
  const esc = CSS.escape(key);
  return document.querySelector<HTMLElement>(`#main [data-zorec="${esc}"]`)
    ?? document.querySelector<HTMLElement>(`#main [name="${esc}"]`)
    ?? document.getElementById(key);
}

const wait = (ms: number) => new Promise((r) => window.setTimeout(r, ms));

async function until(fn: () => HTMLElement | null, ms = 2500) {
  const end = Date.now() + ms;
  for (;;) {
    const el = fn();
    if (el || Date.now() > end) return el;
    await wait(100);
  }
}

function spotlight(el: HTMLElement, label: string) {
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  const ring = document.createElement("div");
  ring.className = "zorec-spot";
  const tip = document.createElement("div");
  tip.className = "zorec-tip";
  tip.textContent = label;
  if (label) ring.appendChild(tip);
  document.body.appendChild(ring);
  const place = () => {
    const r = el.getBoundingClientRect();
    Object.assign(ring.style, { top: `${r.top - 6}px`, left: `${r.left - 6}px`, width: `${r.width + 12}px`, height: `${r.height + 12}px` });
  };
  place();
  const timer = window.setInterval(place, 50);
  window.setTimeout(() => {
    window.clearInterval(timer);
    ring.classList.add("out");
    window.setTimeout(() => ring.remove(), 400);
  }, 5000);
}

/** Type into a React-controlled field so its state updates like a real input. */
async function typeInto(el: HTMLElement, value: string) {
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  el.focus();
  if (el instanceof HTMLInputElement && el.type === "checkbox") {
    const want = value === "true";
    if (el.checked !== want) el.click();
  } else if (el instanceof HTMLSelectElement) {
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
    setter?.call(el, value);
    el.dispatchEvent(new Event("change", { bubbles: true }));
  } else if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) {
    const proto = el instanceof HTMLInputElement ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    const step = Math.max(1, Math.ceil(value.length / 24));
    for (let i = step; i < value.length + step; i += step) {
      setter?.call(el, value.slice(0, i));
      el.dispatchEvent(new Event("input", { bubbles: true }));
      await wait(25);
    }
  }
  el.classList.add("zorec-filled");
  window.setTimeout(() => el.classList.remove("zorec-filled"), 4000);
}

// --- small markdown: paragraphs, bullets, **bold**, `code` ------------------------------

function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") ? <b key={i}>{part.slice(2, -2)}</b>
      : part.startsWith("`") && part.endsWith("`") ? <code key={i}>{part.slice(1, -1)}</code>
        : <Fragment key={i}>{part}</Fragment>);
}

function Text({ text }: { text: string }) {
  const blocks = text.trim().split(/\n{2,}/);
  return (
    <>
      {blocks.map((block, i) => {
        const lines = block.split("\n");
        if (lines.every((l) => /^\s*([*•-]|\d+\.)\s+/.test(l))) {
          return <ul key={i}>{lines.map((l, j) => <li key={j}>{inline(l.replace(/^\s*([*•-]|\d+\.)\s+/, ""))}</li>)}</ul>;
        }
        return <p key={i}>{lines.map((l, j) => <Fragment key={j}>{j > 0 && <br />}{inline(l)}</Fragment>)}</p>;
      })}
    </>
  );
}

// --- the mascot ------------------------------------------------------------------------

export function Mascot({ mood, size = 36 }: { mood: Mood; size?: number }) {
  return (
    <span className={`mascot ${mood}`} style={{ width: size, height: size }} aria-hidden="true">
      <img src="/brand/zorec.svg" alt="" />
      {mood === "thinking" && <span className="dots"><i /><i /><i /></span>}
    </span>
  );
}

// --- executions started from the chat --------------------------------------------------

function JobCard({ id, onDone }: { id: string; onDone: (job: Job) => void }) {
  const t = useT();
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => api.get<{ job: Job }>(`/api/jobs/${id}`).then((r) => setJob(r.job), () => undefined), [id]);
  useEffect(() => {
    reload();
    return onLive((e) => e.type === "job" && e.id === id && reload());
  }, [id, reload]);
  const finished = job && ["succeeded", "failed", "rolled_back", "rejected", "cancelled"].includes(job.state);
  useEffect(() => {
    if (job && finished) onDone(job);
  }, [finished]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!job) return <div className="jobcard"><span className="spinner" /></div>;

  async function decide(approve: boolean) {
    setBusy(true);
    setError(null);
    try {
      if (approve) await api.post(`/api/jobs/${id}/approve`, { fingerprint: job!.fingerprint });
      else await api.post(`/api/jobs/${id}/reject`, { reason: "Zorec chat" });
      reload();
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={`jobcard ${job.state}`} data-testid="zorec-job">
      <div className="row">
        <b style={{ flex: 1 }}>{job.title}</b>
        <Pill state={job.state} />
      </div>
      <ol className="steps compact">
        {job.steps.map((s, i) => (
          <li key={i} className={s.state}>
            <span className="dot">{s.state === "succeeded" ? "✓" : s.state === "running" ? <span className="spinner" /> : s.state === "failed" ? "!" : i + 1}</span>
            <div>{s.title || s.effect}{s.error && <div className="small warn-text">{s.error}</div>}</div>
          </li>
        ))}
      </ol>
      {error && <div className="small warn-text">{error}</div>}
      {job.state === "awaiting_approval" && (
        <div className="row">
          <button className="btn small primary" disabled={busy} onClick={() => decide(true)}><Icon name="play" size={14} />{t("Approve and run")}</button>
          <button className="btn small ghost" disabled={busy} onClick={() => decide(false)}>{t("Reject")}</button>
          <a className="btn small ghost" href={href(`/jobs/${id}`)}>{t("Details")}</a>
        </div>
      )}
    </div>
  );
}

// --- the panel ---------------------------------------------------------------------------

export function Zorec({ status }: { status: { busy: boolean; label: string } }) {
  const t = useT();
  const route = useRoute();
  const canEdit = useCan("operator");
  const { data: ai } = useData<Status>("/api/zorec/status", ["zorec.settings"]);
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<"read" | "edit">("read");
  const [log, setLog] = useState<Msg[]>(load);
  const [text, setText] = useState("");
  const [mood, setMood] = useState<Mood>("idle");
  const [sketches, setSketches] = useState<Sketch[]>([]);
  const [sketch, setSketch] = useState<number | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const caps = new Set(ai?.capabilities ?? []);

  useEffect(() => {
    try {
      sessionStorage.setItem(STORE, JSON.stringify(log.slice(-30)));
    } catch {
      /* ignore */
    }
    end.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [log, open]);

  useEffect(() => {
    document.body.classList.toggle("zorec-open", open);
    if (open) window.setTimeout(() => input.current?.focus(), 50);
  }, [open]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k" && e.shiftKey) {
        e.preventDefault();
        setOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const note = (id: string, line: string) =>
    setLog((l) => l.map((m) => (m.id === id ? { ...m, done: [...(m.done ?? []), line] } : m)));

  async function perform(index: string, ui: Ui[]) {
    if (!ui.length) return;
    setMood("acting");
    for (const u of ui) {
      if (u.kind === "navigate") {
        go(u.target.replace(/^#/, ""));
        note(index, t("Opened {page}", { page: u.label || u.target }));
        await wait(500);
      } else if (u.kind === "highlight") {
        const el = await until(() => find(u.target));
        if (el) {
          spotlight(el, u.label);
          note(index, t("Showed {thing}", { thing: u.label || labelOf(el) }));
        }
      } else if (u.kind === "fill") {
        const el = await until(() => find(u.target));
        if (el) {
          await typeInto(el, u.value);
          note(index, t("Filled in {field} – check before you send", { field: labelOf(el) || u.target }));
        }
      } else if (u.kind === "diagram") {
        setSketches((s) => {
          setSketch(s.length);
          return [...s, { label: u.label || t("Diagram"), source: u.value }];
        });
        note(index, t("Drew {name}", { name: u.label || t("a diagram") }));
      }
      await wait(250);
    }
    setMood("happy");
    window.setTimeout(() => setMood("idle"), 1500);
  }

  async function ask(question: string) {
    const q = question.trim();
    if (!q) return;
    setText("");
    const history = log.slice(-8).map((m) => ({ role: m.role, content: m.text }));
    setLog((l) => [...l, { id: newId(), role: "user", text: q }]);
    setMood("thinking");
    try {
      const answer = await api.post<Answer>("/api/zorec", {
        question: q, mode, history, page: pageContext(route.path, route.params),
      });
      const id = newId();
      setLog((l) => [...l, { id, role: "assistant", text: answer.text, answer }]);
      await wait(0);
      if (answer.ui?.length) await perform(id, answer.ui);
      else setMood("idle");
    } catch (e) {
      setLog((l) => [...l, { id: newId(), role: "assistant", text: String((e as Error).message) }]);
      setMood("idle");
    }
  }

  async function act(index: string, a: Answer["actions"][number]) {
    if (a.action === "rollout.plan") return go("/updates");
    if (a.action === "deploy.analyze") return go(`/apps/new?repo=${encodeURIComponent(String(a.params.repo ?? ""))}`);
    const target = String(a.params.guest ?? a.params.device ?? a.params.host ?? a.params.service ?? a.params.workload ?? "fleet");
    try {
      const job = await api.post<Job>("/api/jobs", { title: a.title, target, reason: "Zorec", steps: [{ action: a.action, params: a.params }] });
      setLog((l) => l.map((m) => (m.id === index ? { ...m, jobs: [...(m.jobs ?? []), job.id] } : m)));
    } catch (e) {
      note(index, String((e as Error).message));
    }
  }

  const suggestions: Record<string, string[]> = {
    "/": [t("What is going on?"), t("Which updates are available?"), t("What should I do first?")],
    "/network": [t("Anything odd in the network?"), t("Show me the topology")],
    "/security": [t("How is security?"), t("Explain the most urgent finding")],
    "/updates": [t("Which updates are available?")],
    "/apps": [t("Which application fits for monitoring?"), t("How are my applications isolated?")],
    "/settings": [t("Which permissions does Zorec need for night-time fixes?")],
  };
  const section = "/" + (route.path.split("/")[1] ?? "");
  const chips = suggestions[section] ?? suggestions["/"];
  const modelLabel = ai?.mode === "model" ? ai.reason : t("rule based");

  if (!open) {
    return (
      <button className={`zorec-fab${status.busy ? " busy" : ""}`} onClick={() => setOpen(true)} aria-label={t("Ask Zorec")} title="Ctrl+Shift+K">
        <Mascot mood={status.busy ? "thinking" : "idle"} size={40} />
        <span>{t("Ask Zorec")}<small>{status.label}</small></span>
      </button>
    );
  }

  return (
    <>
      {sketch !== null && sketches[sketch] && (
        <aside className="zorec-canvas" aria-label={t("Diagram")}>
          <header>
            <Icon name="diagram" />
            <b style={{ flex: 1 }}>{sketches[sketch].label}</b>
            {sketches.length > 1 && (
              <div className="seg">{sketches.map((s, i) => <button key={i} aria-pressed={i === sketch} onClick={() => setSketch(i)} title={s.label}>{i + 1}</button>)}</div>
            )}
            <button className="icon-btn" onClick={() => setSketch(null)} aria-label={t("Close")}><Icon name="close" /></button>
          </header>
          <Diagram source={sketches[sketch].source} ratio={4 / 3} label={sketches[sketch].label} />
        </aside>
      )}
      <aside className={`zorec${mode === "edit" ? " edit" : ""}`} aria-label="Zorec">
        <header>
          <Mascot mood={mood} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <b>Zorec</b>
            <div className="small muted sub" title={ai?.reason}>{mood === "thinking" ? t("thinking …") : mood === "acting" ? t("working in the app …") : modelLabel}</div>
          </div>
          {canEdit && caps.has("propose") && (
            <div className="seg" role="group" aria-label={t("Mode")}>
              <button aria-pressed={mode === "read"} onClick={() => setMode("read")}>{t("Read")}</button>
              <button aria-pressed={mode === "edit"} onClick={() => setMode("edit")}>{t("Change")}</button>
            </div>
          )}
          {log.length > 0 && <button className="icon-btn" onClick={() => { setLog([]); setSketches([]); setSketch(null); }} aria-label={t("New conversation")} title={t("New conversation")}><Icon name="refresh" /></button>}
          <button className="icon-btn" onClick={() => setOpen(false)} aria-label={t("Close")}><Icon name="close" /></button>
        </header>
        <div className="log" aria-live="polite">
          {log.length === 0 && (
            <div className="hello">
              <Mascot mood="happy" size={64} />
              <p>{t("I know the current state of your systems. Ask me anything – I can open pages, point at things, fill in forms and draw diagrams. In “Change” mode I prepare jobs for you to approve.")}</p>
              {ai && !ai.enabled && <p className="small muted">{t("AI is off: I answer with rules. You can turn it on in Settings → Zorec & AI.")}</p>}
            </div>
          )}
          {log.map((m) => (
            <div key={m.id} className={`msg${m.role === "user" ? " me" : ""}`}>
              {m.role === "assistant" && m.answer && (
                <div className="meta">
                  {m.answer.source === "model" ? <><Icon name="spark" size={12} />{m.answer.model}{m.answer.seconds != null && ` · ${m.answer.seconds} s`}</> : t("rule based")}
                </div>
              )}
              <Text text={m.text} />
              {m.done && m.done.length > 0 && (
                <ul className="did">{m.done.map((d, j) => <li key={j}><Icon name="check" size={12} />{d}</li>)}</ul>
              )}
              {m.answer && (m.answer.links.length > 0 || m.answer.actions.length > 0 || m.answer.ui?.some((u) => u.kind === "diagram")) && (
                <div className="chips">
                  {m.answer.links.map((l) => <a key={l.href} className="btn small" href={l.href.startsWith("#") ? l.href : href(l.href)}>{l.label}</a>)}
                  {m.answer.ui?.filter((u) => u.kind === "diagram").map((u, j) => (
                    <button key={`d${j}`} className="btn small" onClick={() => {
                      const at = sketches.findIndex((s) => s.source === u.value);
                      if (at >= 0) setSketch(at);
                      else { setSketches((s) => [...s, { label: u.label, source: u.value }]); setSketch(sketches.length); }
                    }}><Icon name="diagram" size={14} />{u.label || t("Diagram")}</button>
                  ))}
                  {m.answer.actions.map((a, j) => (
                    <button key={j} className="btn small primary" onClick={() => act(m.id, a)}><Icon name="arrow" size={14} />{a.title}</button>
                  ))}
                </div>
              )}
              {m.jobs?.map((id) => (
                <JobCard key={id} id={id} onDone={(job) => {
                  setMood(job.state === "succeeded" ? "happy" : "idle");
                  window.setTimeout(() => setMood("idle"), 1500);
                }} />
              ))}
            </div>
          ))}
          {mood === "thinking" && <div className="msg typing"><span /><span /><span /></div>}
          <div ref={end} />
        </div>
        <footer>
          <div className="chips">
            {chips.map((s) => <button key={s} onClick={() => ask(s)}>{s}</button>)}
          </div>
          <form className="ask" onSubmit={(e) => { e.preventDefault(); ask(text); }}>
            <label className="sr-only" htmlFor="zorec-q">{t("Question for Zorec")}</label>
            <textarea id="zorec-q" ref={input} rows={1} value={text} onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(text); } }}
              placeholder={t("Ask about a service, device or job")} />
            <button className="btn primary icon" disabled={mood === "thinking" || !text.trim()} aria-label={t("Send")}><Icon name="send" size={16} /></button>
          </form>
        </footer>
      </aside>
    </>
  );
}
