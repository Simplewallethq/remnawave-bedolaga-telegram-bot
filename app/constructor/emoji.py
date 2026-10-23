"""Премиум-эмодзи конструктора.

У каждого эмодзи интерфейса есть имя: в тексте он рендерится как <tg-emoji>,
в кнопке — как icon_custom_emoji_id. Telegram показывает премиум-эмодзи бота,
только если у владельца бота есть Premium; если он их отклонит, экран
перерисовывается обычными символами (см. ui.UI.show).
"""

from __future__ import annotations

from html import escape
from typing import Optional

CHARS = {
    "shield": "🛡", "rocket": "🚀", "eyes": "👀", "question": "❓", "robot": "🤖",
    "plus": "➕", "gear": "⚙️", "money": "💰", "cash": "💸", "check": "✅",
    "warn": "⚠️", "link": "🔗", "party": "🎉", "sparkles": "✨",
}

IDS = {
    "shield": "5278540791336165644",
    "rocket": "5276314275994954605",
    "eyes": "5276037216244624892",
    "question": "5278753302023004775",
    "robot": "5206626000665868017",
    "plus": "5276314275994954605",
    "gear": "5276442772826515132",
    "money": "5255806447106679302",
    "cash": "5206510891247371052",
    "check": "5278611606756942667",
    "warn": "5276240711795107620",
    "link": "5278305362703835500",
    "party": "5278411813468269386",
    "sparkles": "5206222720416643915",
}


def ch(name: str) -> str:
    return CHARS[name]


def e(name: str) -> str:
    """Эмодзи для текста сообщения."""
    char = CHARS[name]
    custom_id = IDS.get(name)
    if custom_id:
        return f'<tg-emoji emoji-id="{custom_id}">{escape(char)}</tg-emoji>'
    return char


def icon_id(name: Optional[str]) -> Optional[str]:
    return IDS.get(name) if name else None
