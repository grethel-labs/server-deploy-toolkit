"""The central catalogs are complete and consistent, and stored texts follow the reader."""

import ast
import json
import re
import string
from pathlib import Path

from opstend import i18n
from opstend.core.jobs import Job, Step
from opstend.i18n import tr

ROOT = Path(__file__).resolve().parent.parent
LOCALES = ROOT / "src/opstend/locales"


def placeholders(text: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}


def server_templates() -> set[str]:
    found = set()
    for path in (ROOT / "src/opstend").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "tr" and node.args
                    and isinstance(node.args[0], ast.Constant)):
                found.add(node.args[0].value)
    return found


def web_templates() -> set[str]:
    found = set()
    pattern = re.compile(r"""\bt\(\s*("(?:[^"\\]|\\.)*")""")
    for path in (ROOT / "web/src").rglob("*.tsx"):
        for m in pattern.finditer(path.read_text()):
            found.add(json.loads(m.group(1)))
    for path in (ROOT / "web/src").rglob("*.ts"):
        for m in pattern.finditer(path.read_text()):
            found.add(json.loads(m.group(1)))
    return found


def test_every_text_is_translated_in_every_catalog():
    wanted = server_templates() | web_templates()
    assert len(wanted) > 100
    for path in LOCALES.glob("*.json"):
        catalog = json.loads(path.read_text())
        missing = sorted(wanted - catalog.keys())
        assert not missing, f"{path.name} lacks {len(missing)} texts, e.g. {missing[:5]}"


def test_placeholders_match():
    for path in LOCALES.glob("*.json"):
        for en, translated in json.loads(path.read_text()).items():
            assert placeholders(en) == placeholders(translated), (path.name, en)


def test_stored_texts_render_in_the_readers_language():
    job = Job(title=tr("Restart {service}", service="Sensorik"), category="restart", target="x",
              steps=[Step(action="a", effect=tr("Shut down {guest}", guest="fotos"))])
    data = job.model_dump(mode="json")
    assert data["title"] == "Restart Sensorik"
    assert i18n.localize(data, "de")["title"] == "Sensorik neu starten"
    assert i18n.localize(data, "de")["steps"][0]["effect"] == "fotos herunterfahren"
    assert i18n.localize(data, "en")["title"] == "Restart Sensorik"


def test_language_choice():
    assert i18n.pick("de-DE,de;q=0.9") == "de"
    assert i18n.pick("fr-FR") == "en"
    assert i18n.pick(None, preferred="de") == "de"
    assert "en" in i18n.languages() and "de" in i18n.languages()
