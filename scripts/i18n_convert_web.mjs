// One-off migration of the web app: t("Deutsch", "English") -> t("English {x}", { x }) + de catalog.
import { readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url + "/../../web/");
const ts = require("typescript");

const catalogPath = new URL("../src/opstend/locales/de.json", import.meta.url).pathname;
const catalog = JSON.parse(readFileSync(catalogPath, "utf8"));
const manual = [];

function nameFor(expr, used) {
  let base = expr.replace(/\?\.|\./g, "_").replace(/[^A-Za-z0-9_]/g, "_").replace(/_+/g, "_").replace(/^_|_$/g, "").toLowerCase();
  base = base.split("_").slice(-2).join("_") || "value";
  if (/^\d/.test(base)) base = "v_" + base;
  let name = base, n = 2;
  while ([...used.values()].includes(name)) name = `${base}${n++}`;
  return name;
}

function tpl(node, sf, used) {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
  if (ts.isTemplateExpression(node)) {
    let out = node.head.text;
    for (const span of node.templateSpans) {
      const expr = span.expression.getText(sf);
      if (!used.has(expr)) used.set(expr, nameFor(expr, used));
      out += "{" + used.get(expr) + "}" + span.literal.text;
    }
    return out;
  }
  return null;
}

for (const file of process.argv.slice(2)) {
  const text = readFileSync(file, "utf8");
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const edits = [];
  const visit = (node) => {
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === "t" && node.arguments.length === 2) {
      const used = new Map();
      const en = tpl(node.arguments[1], sf, used);
      const before = new Set(used.keys());
      const de = tpl(node.arguments[0], sf, used);
      const extra = [...used.keys()].filter((k) => !before.has(k));
      if (en === null || de === null || extra.length) {
        manual.push(`${file}:${sf.getLineAndCharacterOfPosition(node.getStart()).line + 1}`);
      } else {
        catalog[en] = de;
        const params = [...used.entries()].map(([expr, name]) => (expr === name ? name : `${name}: ${expr}`));
        const repl = `t(${JSON.stringify(en)}${params.length ? `, { ${params.join(", ")} }` : ""})`;
        edits.push([node.getStart(), node.getEnd(), repl]);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  let out = text;
  for (const [s, e, r] of edits.sort((a, b) => b[0] - a[0])) out = out.slice(0, s) + r + out.slice(e);
  writeFileSync(file, out);
}
writeFileSync(catalogPath, JSON.stringify(Object.fromEntries(Object.entries(catalog).sort(([a], [b]) => a.localeCompare(b))), null, 1) + "\n");
console.log(`catalog: ${Object.keys(catalog).length}`);
for (const m of manual) console.log("MANUAL", m);
