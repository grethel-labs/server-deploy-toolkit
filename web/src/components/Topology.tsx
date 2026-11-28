import { useEffect, useRef, useState } from "react";
import { render } from "@grethel-labs/excaliplant";
import { bindHooks, onHook } from "@grethel-labs/excaliplant/hooks";
import { api } from "../api";
import { isDark, useT } from "../lib";

type Hook = { id: string; kind: string; name: string };
type Topo = { source: string; hooks: Record<string, Hook> };

/**
 * Interactive network map rendered by excaliplant.
 * The panel has a fixed aspect ratio; excaliplant reflows the graph toward it.
 */
export function Topology({ layers, focus, clients = false, ratio = 16 / 9, onSelect }: {
  layers: string[]; focus?: string; clients?: boolean; ratio?: number; onSelect?: (hook: Hook | null) => void;
}) {
  const t = useT();
  const host = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [zoom, setZoom] = useState<"fit" | 1>("fit");
  const [size, setSize] = useState<[number, number]>([0, 0]);
  const dark = isDark();

  useEffect(() => {
    let cancelled = false;
    let cleanup: (() => void)[] = [];
    setBusy(true);
    const q = new URLSearchParams({ layers: layers.join(","), clients: String(clients), dark: String(dark) });
    if (focus) q.set("focus", focus);
    api.get<Topo>(`/api/network/topology?${q}`)
      .then(async (topo) => {
        const result = await render(topo.source, {
          aspectRatio: ratio,
          theme: dark ? "grethel-labs-dark" : "grethel-labs-light",
        });
        const markup = await result.toSvg({ interactive: true, padding: 16 });
        if (cancelled || !host.current) return;
        host.current.innerHTML = markup;
        const svg = host.current.querySelector("svg");
        if (!svg) return;
        const box = svg.viewBox.baseVal;
        setSize([box.width, box.height]);
        svg.removeAttribute("width");
        svg.removeAttribute("height");
        svg.setAttribute("role", "img");
        svg.setAttribute("aria-label", t("Network map"));
        cleanup.push(bindHooks(svg));
        for (const cls of [".host", ".guest", ".switch", ".gateway", ".ap", ".client", ".service", ".internet"]) {
          cleanup.push(onHook(svg, cls, ({ detail }) => {
            if (detail.type !== "click") return;
            onSelect?.(detail.active && detail.id ? topo.hooks[detail.id] ?? null : null);
          }));
        }
        setError(null);
      })
      .catch((e) => !cancelled && setError(String(e.message ?? e)))
      .finally(() => !cancelled && setBusy(false));
    return () => {
      cancelled = true;
      cleanup.forEach((fn) => fn());
      cleanup = [];
    };
  }, [layers.join(","), focus, clients, dark, ratio]);

  return (
    <div className="topo">
      {error && <div className="banner warn">{error}</div>}
      <div className="zoom seg" role="group" aria-label={t("Zoom")}>
        <button aria-pressed={zoom === "fit"} onClick={() => setZoom("fit")}>{t("Fit")}</button>
        <button aria-pressed={zoom === 1} onClick={() => setZoom(1)}>100 %</button>
      </div>
      <div className={`canvas${zoom === 1 ? " actual" : ""}`} style={{ aspectRatio: String(ratio), opacity: busy ? 0.5 : 1 }} aria-busy={busy}>
        <div ref={host} style={zoom === 1 && size[0] ? { width: size[0], height: size[1] } : { position: "absolute", inset: 8 }} />
      </div>
    </div>
  );
}
