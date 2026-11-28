"""One-off migration: t(en, de) / _t(lang, de, en) calls -> _("template", **params) + de catalog.

Run from the repository root: python scripts/i18n_convert.py src/opstend/... (files)
Unconvertible calls are listed and left unchanged.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

CATALOG = Path("src/opstend/locales/de.json")


def name_for(src: str, used: dict[str, str]) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "_", src).strip("_").lower() or "value"
    base = re.sub(r"^(self|p|params)_", "", base) if len(base) > 6 else base
    if re.match(r"^\d", base):
        base = "v_" + base
    base = base[:24].rstrip("_")
    name, n = base, 2
    while name in used.values() and used.get(src) != name:
        name, n = f"{base}{n}", n + 1
    return name


def template(node: ast.expr, source: str, names: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.replace("{", "{{").replace("}", "}}")
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(str(part.value).replace("{", "{{").replace("}", "}}"))
            elif isinstance(part, ast.FormattedValue):
                expr = ast.get_source_segment(source, part.value) or ""
                if expr not in names:
                    names[expr] = name_for(expr, names)
                conv = {115: "!s", 114: "!r", 97: "!a"}.get(part.conversion, "")
                spec = ""
                if part.format_spec is not None:
                    spec_t = template(part.format_spec, source, names)
                    if spec_t is None or "{" in spec_t:
                        return None
                    spec = ":" + spec_t
                out.append("{" + names[expr] + conv + spec + "}")
            else:
                return None
        return "".join(out)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = template(node.left, source, names)
        right = template(node.right, source, names)
        return None if left is None or right is None else left + right
    return None


def convert(path: Path, catalog: dict[str, str]) -> list[str]:
    source = path.read_text()
    tree = ast.parse(source)
    edits: list[tuple[int, int, str]] = []
    problems: list[str] = []
    lines = source.encode().splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def pos(lineno: int, col: int) -> int:
        # ast column offsets are UTF-8 byte offsets.
        return offsets[lineno - 1] + col

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id == "t" and len(node.args) == 2 and not node.keywords:
            en_node, de_node = node.args
        elif node.func.id == "_t" and len(node.args) == 3:
            _, de_node, en_node = node.args
        else:
            continue
        names: dict[str, str] = {}
        en = template(en_node, source, names)
        de_names = dict(names)
        de = template(de_node, source, de_names)
        if en is None or de is None or set(de_names) - set(names):
            problems.append(f"{path}:{node.lineno}")
            continue
        catalog[en.replace("{{", "{").replace("}}", "}") if not names else en] = (
            de.replace("{{", "{").replace("}}", "}") if not names else de
        )
        args = [json.dumps(en if names else en.replace("{{", "{").replace("}}", "}"),
                           ensure_ascii=False)]
        args += [f"{n}={expr}" for expr, n in names.items()]
        start = pos(node.lineno, node.col_offset)
        end = pos(node.end_lineno or node.lineno, node.end_col_offset or 0)
        edits.append((start, end, f"_({', '.join(args)})"))
    data = source.encode()
    for start, end, text in sorted(edits, reverse=True):
        data = data[:start] + text.encode() + data[end:]
    out = data.decode()
    out = out.replace("from opstend.i18n import t\n", "from opstend.i18n import _\n")
    path.write_text(out)
    return problems


def main() -> int:
    catalog: dict[str, str] = json.loads(CATALOG.read_text()) if CATALOG.exists() else {}
    problems: list[str] = []
    for f in sys.argv[1:]:
        problems += convert(Path(f), catalog)
    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    CATALOG.write_text(json.dumps(dict(sorted(catalog.items())), ensure_ascii=False, indent=1) + "\n")
    print(f"{len(catalog)} catalog entries")
    for p in problems:
        print("MANUAL", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
