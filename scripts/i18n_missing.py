"""Print texts that a catalog lacks: python scripts/i18n_missing.py de"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
from test_i18n import LOCALES, server_templates, web_templates  # noqa: E402

lang = sys.argv[1] if len(sys.argv) > 1 else "de"
catalog = json.loads((LOCALES / f"{lang}.json").read_text())
for text in sorted((server_templates() | web_templates()) - catalog.keys()):
    print(json.dumps(text, ensure_ascii=False))
