import { useEffect, useRef, useState } from "react";
import { render } from "@grethel-labs/excaliplant";
import { bindHooks } from "@grethel-labs/excaliplant/hooks";
import { isDark, useT } from "../lib";

/** Any PlantUML source drawn with excaliplant at a fixed aspect ratio. */
export function Diagram({ source, ratio = 16 / 9, label }: { source: string; ratio?: number; label?: string }) {
  const t = useT();
  const host = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const dark = isDark();

  useEffect(() => {
    let cancelled = false;
    let cleanup: (() => void) | undefined;
    render(source, { aspectRatio: ratio, theme: dark ? "grethel-labs-dark" : "grethel-labs-light" })
      .then((result) => result.toSvg({ interactive: true, padding: 12 }))
      .then((markup) => {
        if (cancelled || !host.current) return;
        host.current.innerHTML = markup;
        const svg = host.current.querySelector("svg");
        if (svg) {
          svg.removeAttribute("width");
          svg.removeAttribute("height");
          svg.setAttribute("role", "img");
          svg.setAttribute("aria-label", label ?? t("Diagram"));
          cleanup = bindHooks(svg);
        }
        setError(null);
      })
      .catch((e) => !cancelled && setError(String(e?.message ?? e)));
    return () => {
      cancelled = true;
      cleanup?.();
    };
  }, [source, ratio, dark]);

  return (
    <div className="diagram">
      {error && <div className="banner warn">{error}</div>}
      <div ref={host} className="diagram-canvas" style={{ aspectRatio: String(ratio) }} />
    </div>
  );
}
