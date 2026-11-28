import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { Icon } from "../components/Icon";
import { Card, Loading, Pill } from "../components/ui";
import { useCan, useData, useLang, useT, when } from "../lib";

type Provider = {
  id: string; label: string; kind: "openai" | "ollama" | "anthropic" | "cli"; preset?: string | null;
  url?: string | null; model?: string | null; ca_pem?: string | null; context_tokens: number;
  max_reply_tokens: number; local: boolean; command?: string | null; timeout_s: number; has_key?: boolean;
};
type Selection = {
  enabled: boolean; auto_switch: boolean; prefer: "quality" | "speed" | "local";
  include: string[]; exclude: string[]; providers: string[]; max_candidates: number; interval_days: number;
};
type AISettings = {
  enabled: boolean; providers: Provider[]; active: string | null; fallback: string | null;
  capabilities: Record<string, boolean>; selection: Selection;
};
type Capability = { id: string; label: string; description: string; features: string[]; risk: "low" | "medium" | "high" };
type Preset = { id: string; label: string; kind: Provider["kind"]; url: string | null; needs_key: boolean; local: boolean; command: string | null; context_tokens: number; note: string; docs: string };
type Decision = {
  ran_at: string; prefer: string; action: "switch" | "kept"; reason: string; applied?: boolean;
  best: { provider: string; model: string } | null;
  results: { provider: string; model: string; local: boolean; passed: string[]; failed: string[]; seconds: number; error: string | null; ok: boolean }[];
};
type Data = {
  settings: AISettings;
  status: { mode: "rules" | "model"; reason: string };
  catalog: { baseline: string[]; capabilities: Capability[]; requires: Record<string, string[]> };
  presets: Preset[];
  selection: { running: boolean; history: Decision[] };
};

const CASES = ["severity", "injection", "action", "context"];

function effective(caps: Record<string, boolean>, requires: Record<string, string[]>) {
  const ok = (c: string, seen: Set<string> = new Set()): boolean => {
    if (!caps[c] || seen.has(c)) return false;
    seen.add(c);
    return (requires[c] ?? []).every((r) => ok(r, seen));
  };
  return new Set(Object.keys(caps).filter((c) => ok(c)));
}

export function AiSettings() {
  const t = useT();
  const lang = useLang();
  const isAdmin = useCan("admin");
  const { data, reload } = useData<Data>(isAdmin ? "/api/zorec/settings" : null, ["zorec.selection"]);
  const [draft, setDraft] = useState<AISettings | null>(null);
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [searching, setSearching] = useState(false);

  useEffect(() => {
    if (data) setDraft(structuredClone(data.settings));
    if (data) setSearching(data.selection.running);
  }, [data]);

  const dirty = useMemo(
    () => !!data && !!draft && (JSON.stringify(data.settings) !== JSON.stringify(draft) || Object.keys(keys).length > 0),
    [data, draft, keys],
  );

  if (!isAdmin) return <Card title="Zorec"><p className="muted">{t("Only administrators can change the AI settings.")}</p></Card>;
  if (!data || !draft) return <Loading />;

  const requires = data.catalog.requires;
  const before = effective(data.settings.capabilities, requires);
  const after = effective(draft.capabilities, requires);
  const byId = Object.fromEntries(data.catalog.capabilities.map((c) => [c.id, c]));
  const gained = [...after].filter((c) => !before.has(c));
  const lost = [...before].filter((c) => !after.has(c));
  const set = (patch: Partial<AISettings>) => setDraft({ ...draft, ...patch });
  const setProvider = (id: string, patch: Partial<Provider>) =>
    set({ providers: draft.providers.map((p) => (p.id === id ? { ...p, ...patch } : p)) });

  async function save() {
    try {
      await api.put("/api/zorec/settings", { settings: draft, api_keys: keys });
      setKeys({});
      setMessage({ ok: true, text: t("Saved") });
      reload();
    } catch (e) {
      setMessage({ ok: false, text: String((e as Error).message) });
    }
    window.setTimeout(() => setMessage(null), 3000);
  }

  async function search() {
    setSearching(true);
    try {
      await api.post("/api/zorec/selection");
    } catch (e) {
      setSearching(false);
      setMessage({ ok: false, text: String((e as Error).message) });
    }
  }

  function addFrom(preset: Preset) {
    const base = preset.id.replace(/[^a-z0-9-]/g, "");
    let id = base;
    for (let i = 2; draft!.providers.some((p) => p.id === id); i++) id = `${base}-${i}`;
    const p: Provider = {
      id, label: preset.label.split(" (")[0], kind: preset.kind, preset: preset.id, url: preset.url,
      model: null, context_tokens: preset.context_tokens, max_reply_tokens: 800, local: preset.local,
      command: preset.command, timeout_s: 180, ca_pem: null,
    };
    set({ providers: [...draft!.providers, p], active: draft!.active ?? id });
    setAdding(false);
    setEditing(id);
  }

  const last = data.selection.history[data.selection.history.length - 1];

  return (
    <div className="stack" data-zorec="ai-settings">
      <div className={`banner ${data.status.mode === "model" ? "ok" : "info"}`} data-zorec="ai-status">
        <Icon name={data.status.mode === "model" ? "spark" : "info"} />
        <div style={{ flex: 1 }}>
          <b>{data.status.mode === "model" ? t("Zorec uses a language model") : t("Zorec works with rules")}</b>
          <div className="small">{data.status.reason}</div>
        </div>
        <label className="switch">
          <input type="checkbox" name="ai-enabled" checked={draft.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
          <span>{t("Use AI")}</span>
        </label>
      </div>

      <Card title={t("Works without AI")} sub={t("These functions never need a model.")}>
        <ul className="checks">{data.catalog.baseline.map((b) => <li key={b}><Icon name="check" size={14} />{b}</li>)}</ul>
      </Card>

      <Card title={t("Models")} sub={t("Where Zorec's answers come from. Keys are stored encrypted in the cluster.")}
        action={<button className="btn small" onClick={() => setAdding(true)} data-zorec="ai-add-provider"><Icon name="plus" size={14} />{t("Add")}</button>}>
        {draft.providers.length === 0 && !adding && <p className="muted">{t("No model yet. Add a model server in your network or link a provider.")}</p>}
        <div className="stack">
          {draft.providers.map((p) => (
            <ProviderRow key={p.id} p={p} draft={draft} editing={editing === p.id} keyValue={keys[p.id]}
              onEdit={() => setEditing(editing === p.id ? null : p.id)}
              onChange={(patch) => setProvider(p.id, patch)}
              onKey={(v) => setKeys({ ...keys, [p.id]: v })}
              onRole={(role) => set(role === "active" ? { active: p.id, fallback: draft.fallback === p.id ? null : draft.fallback } : { fallback: draft.fallback === p.id ? null : p.id })}
              onRemove={() => set({ providers: draft.providers.filter((x) => x.id !== p.id), active: draft.active === p.id ? null : draft.active, fallback: draft.fallback === p.id ? null : draft.fallback })}
              preset={data.presets.find((x) => x.id === p.preset)} />
          ))}
        </div>
        {adding && (
          <div className="preset-grid" role="list">
            {data.presets.map((pr) => (
              <button key={pr.id} role="listitem" className="preset" onClick={() => addFrom(pr)}>
                <b>{pr.label}</b>
                <span className="small muted">{pr.local ? t("In your network") : pr.kind === "cli" ? t("Subscription") : t("Cloud API")}</span>
                {pr.note && <span className="small">{pr.note}</span>}
              </button>
            ))}
            <button className="btn ghost" onClick={() => setAdding(false)}>{t("Cancel")}</button>
          </div>
        )}
      </Card>

      <Card title={t("Permissions")} sub={t("What Zorec may do. Each permission shows the functions it adds.")}>
        <div className="caps">
          {data.catalog.capabilities.map((c) => {
            const on = !!draft.capabilities[c.id];
            const blocked = on && !after.has(c.id);
            return (
              <div key={c.id} className={`cap${on ? " on" : ""}${blocked ? " blocked" : ""}`} data-zorec={`cap-${c.id}`}>
                <label className="switch">
                  <input type="checkbox" name={`cap-${c.id}`} checked={on} onChange={(e) => set({ capabilities: { ...draft.capabilities, [c.id]: e.target.checked } })} />
                  <span><b>{c.label}</b> <Pill state={c.risk === "high" ? "critical" : c.risk === "medium" ? "warning" : "ok"}>{{ low: t("low risk"), medium: t("medium risk"), high: t("high risk") }[c.risk]}</Pill></span>
                </label>
                <p className="small muted">{c.description}</p>
                <ul className="small features">{c.features.map((f) => <li key={f}>{f}</li>)}</ul>
                {blocked && <p className="small warn-text">{t("Needs: {list}", { list: (requires[c.id] ?? []).map((r) => byId[r]?.label ?? r).join(", ") })}</p>}
              </div>
            );
          })}
        </div>
        {(gained.length > 0 || lost.length > 0) && (
          <div className="diff" data-zorec="ai-permission-diff" aria-live="polite">
            <b>{t("When you save")}</b>
            {gained.map((c) => (
              <div key={c} className="plus">{byId[c]?.features.map((f) => <div key={f}>+ {f}</div>)}</div>
            ))}
            {lost.map((c) => (
              <div key={c} className="minus">{byId[c]?.features.map((f) => <div key={f}>− {f}</div>)}</div>
            ))}
          </div>
        )}
      </Card>

      <Card title={t("Automatic model search")} sub={t("Tests the models your providers offer with fixed checks in the maintenance window and switches only to a better one. The previous model stays as a fallback.")}
        action={<button className="btn small" disabled={searching || !data.settings.enabled} onClick={search} data-zorec="ai-search-now">
          {searching ? <span className="spinner" /> : <Icon name="refresh" size={14} />}{searching ? t("Testing models …") : t("Search now")}</button>}>
        <div className="stack">
          <div className="row">
            <label className="switch"><input type="checkbox" name="selection-enabled" checked={draft.selection.enabled} onChange={(e) => set({ selection: { ...draft.selection, enabled: e.target.checked } })} /><span>{t("Search regularly")}</span></label>
            <label className="switch"><input type="checkbox" name="selection-auto" checked={draft.selection.auto_switch} onChange={(e) => set({ selection: { ...draft.selection, auto_switch: e.target.checked } })} /><span>{t("Switch automatically")}</span></label>
          </div>
          <div className="row">
            <span className="lbl">{t("Prefer")}</span>
            <div className="seg" role="group" aria-label={t("Prefer")}>
              {(["quality", "speed", "local"] as const).map((x) => (
                <button key={x} aria-pressed={draft.selection.prefer === x} onClick={() => set({ selection: { ...draft.selection, prefer: x } })}>
                  {{ quality: t("Best answers"), speed: t("Fast answers"), local: t("My network first") }[x]}
                </button>
              ))}
            </div>
            <label className="lbl">{t("Every (days)")}<input className="field" name="selection-interval" type="number" min={1} max={90} value={draft.selection.interval_days} onChange={(e) => set({ selection: { ...draft.selection, interval_days: Number(e.target.value) } })} /></label>
          </div>
          <div className="row">
            <label className="lbl" style={{ flex: 1 }}>{t("Only models matching")}<input className="field" name="selection-include" placeholder="gemma*, claude-sonnet-*" value={draft.selection.include.join(", ")} onChange={(e) => set({ selection: { ...draft.selection, include: splitList(e.target.value) } })} /></label>
            <label className="lbl" style={{ flex: 1 }}>{t("Never")}<input className="field" name="selection-exclude" value={draft.selection.exclude.join(", ")} onChange={(e) => set({ selection: { ...draft.selection, exclude: splitList(e.target.value) } })} /></label>
          </div>
          {last && (
            <div data-zorec="ai-search-results">
              <div className="small muted">{t("Last search {time}: {reason}", { time: when(last.ran_at, lang), reason: t(last.reason) })}</div>
              <div className="table-wrap"><table>
                <thead><tr><th>{t("Model")}</th>{CASES.map((c) => <th key={c}>{t(caseLabel(c))}</th>)}<th>{t("Time")}</th><th /></tr></thead>
                <tbody>{last.results.map((r) => (
                  <tr key={r.provider + r.model}>
                    <td><b>{r.model}</b><div className="small muted">{draft.providers.find((p) => p.id === r.provider)?.label ?? r.provider}</div></td>
                    {CASES.map((c) => <td key={c}>{r.passed.includes(c) ? <Pill state="ok">✓</Pill> : r.failed.includes(c) ? <Pill state="critical">✗</Pill> : "–"}</td>)}
                    <td className="small">{r.error ? <span className="warn-text">{r.error}</span> : `${r.seconds.toFixed(1)} s`}</td>
                    <td>{last.best?.model === r.model && last.best.provider === r.provider && <Pill state="handling">{t("best")}</Pill>}</td>
                  </tr>
                ))}</tbody>
              </table></div>
            </div>
          )}
        </div>
      </Card>

      {(dirty || message) && (
        <div className="savebar" role="region" aria-label={t("Unsaved changes")}>
          {message ? <span className={message.ok ? "ok-text" : "warn-text"}>{message.text}</span> : <span>{t("Unsaved changes")}</span>}
          {dirty && <>
            <button className="btn ghost" onClick={() => { setDraft(structuredClone(data.settings)); setKeys({}); }}>{t("Discard")}</button>
            <button className="btn primary" onClick={save} data-zorec="ai-save">{t("Save")}</button>
          </>}
        </div>
      )}
    </div>
  );
}

function splitList(v: string) {
  return v.split(",").map((x) => x.trim()).filter(Boolean);
}

function caseLabel(c: string) {
  return { severity: "Findings", injection: "Manipulation", action: "Actions", context: "Long data" }[c] ?? c;
}

function ProviderRow(props: {
  p: Provider; draft: AISettings; editing: boolean; keyValue?: string; preset?: Preset;
  onEdit: () => void; onChange: (patch: Partial<Provider>) => void; onKey: (v: string) => void;
  onRole: (role: "active" | "fallback") => void; onRemove: () => void;
}) {
  const t = useT();
  const { p, draft } = props;
  const [models, setModels] = useState<string[]>([]);
  const [test, setTest] = useState<{ ok: boolean; error: string | null; reply: { text: string; seconds: number } | null } | null>(null);
  const [testing, setTesting] = useState(false);
  const active = draft.active === p.id;
  const fallback = draft.fallback === p.id;

  async function check() {
    setTesting(true);
    try {
      const r = await api.post<{ ok: boolean; error: string | null; models: string[]; reply: { text: string; seconds: number } | null }>(
        "/api/zorec/providers/test", { provider: { ...p, has_key: undefined }, api_key: props.keyValue || null });
      setModels(r.models);
      setTest(r);
      if (!p.model && r.models.length) props.onChange({ model: r.models[0] });
    } catch (e) {
      setTest({ ok: false, error: String((e as Error).message), reply: null });
    } finally {
      setTesting(false);
    }
  }

  return (
    <div className={`provider${active ? " active" : ""}`} data-zorec={`provider-${p.id}`}>
      <div className="row">
        <div style={{ flex: 1, minWidth: 0 }}>
          <b>{p.label}</b> {active && <Pill state="handling">{t("active")}</Pill>} {fallback && <Pill state="unknown">{t("fallback")}</Pill>}
          <div className="small muted">{p.model || t("no model selected")} · {p.local ? t("in your network") : t("outside your network")} · {t("{n} tokens context", { n: p.context_tokens.toLocaleString() })}</div>
        </div>
        {!active && <button className="btn small" onClick={() => props.onRole("active")}>{t("Use")}</button>}
        {!active && <button className="btn small ghost" onClick={() => props.onRole("fallback")}>{fallback ? t("No fallback") : t("As fallback")}</button>}
        <button className="btn small ghost" onClick={props.onEdit} aria-expanded={props.editing}>{props.editing ? t("Close") : t("Edit")}</button>
      </div>
      {props.editing && (
        <div className="stack" style={{ marginTop: 10 }}>
          {props.preset?.note && <div className="banner info small">{props.preset.note} {props.preset.docs && <a href={props.preset.docs} target="_blank" rel="noreferrer">{t("Documentation")}</a>}</div>}
          <div className="row">
            <label className="lbl" style={{ flex: 1 }}>{t("Name")}<input className="field" name={`${p.id}-label`} value={p.label} onChange={(e) => props.onChange({ label: e.target.value })} /></label>
            {p.kind !== "cli" && p.kind !== "anthropic" && (
              <label className="lbl" style={{ flex: 2 }}>{t("Address")}<input className="field" name={`${p.id}-url`} value={p.url ?? ""} placeholder="https://10.0.0.5:11434/v1" onChange={(e) => props.onChange({ url: e.target.value })} /></label>
            )}
          </div>
          <div className="row">
            <label className="lbl" style={{ flex: 2 }}>{t("Model")}
              {models.length > 0 ? (
                <select className="field" name={`${p.id}-model`} value={p.model ?? ""} onChange={(e) => props.onChange({ model: e.target.value })}>
                  {models.map((m) => <option key={m}>{m}</option>)}
                </select>
              ) : <input className="field" name={`${p.id}-model`} value={p.model ?? ""} placeholder={t("load the list with “Test”")} onChange={(e) => props.onChange({ model: e.target.value || null })} />}
            </label>
            <label className="lbl">{t("Context (tokens)")}<input className="field" type="number" name={`${p.id}-context`} min={1024} step={1024} value={p.context_tokens} onChange={(e) => props.onChange({ context_tokens: Number(e.target.value) })} /></label>
          </div>
          {(props.preset?.needs_key ?? p.kind !== "cli") && (
            <label className="lbl">{t("API key")}{p.has_key && !props.keyValue && <span className="small muted"> · {t("stored – enter a new one to replace it")}</span>}
              <input className="field" type="password" autoComplete="off" name={`${p.id}-key`} value={props.keyValue ?? ""} onChange={(e) => props.onKey(e.target.value)} placeholder={p.local ? t("optional") : ""} />
            </label>
          )}
          {p.kind !== "cli" && p.kind !== "anthropic" && (
            <details>
              <summary className="small">{t("Own certificate authority")}</summary>
              <textarea className="field mono" rows={4} name={`${p.id}-ca`} value={p.ca_pem ?? ""} placeholder="-----BEGIN CERTIFICATE-----" onChange={(e) => props.onChange({ ca_pem: e.target.value || null })} />
            </details>
          )}
          <label className="chk"><input type="checkbox" checked={p.local} onChange={(e) => props.onChange({ local: e.target.checked })} />{t("This model runs in my network (no data leaves it)")}</label>
          <div className="row">
            <button className="btn small" onClick={check} disabled={testing}>{testing ? <span className="spinner" /> : <Icon name="play" size={14} />}{t("Test")}</button>
            {test && (test.ok
              ? <span className="ok-text small">{t("Answered in {s} s", { s: test.reply?.seconds })} · {models.length ? t("{n} models", { n: models.length }) : ""}</span>
              : <span className="warn-text small">{test.error}</span>)}
            <span style={{ flex: 1 }} />
            <button className="btn small danger" onClick={props.onRemove}>{t("Remove")}</button>
          </div>
        </div>
      )}
    </div>
  );
}
