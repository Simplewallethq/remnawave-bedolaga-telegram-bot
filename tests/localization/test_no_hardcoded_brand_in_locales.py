"""В локалях нет захардкоженного бренда: имя, канал и юр-ссылки — только плейсхолдеры."""

import json
import re
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parents[2] / "app" / "localization" / "locales"
BRAND_WORD = re.compile(r"(?<![\w@./-])Leto(?![\w.-])")
FORBIDDEN = ("t.me/vpnleto", "telegra.ph/Politika", "telegra.ph/Polzovatelskoe")


def test_default_locales_use_placeholders_instead_of_brand():
    offenders = []
    for path in sorted(LOCALES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for key, value in data.items():
            if not isinstance(value, str):
                continue
            if BRAND_WORD.search(value) or any(item in value for item in FORBIDDEN):
                offenders.append(f"{path.name}:{key}")
    assert offenders == []
