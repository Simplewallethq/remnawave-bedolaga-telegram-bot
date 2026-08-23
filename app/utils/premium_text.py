"""Animated custom emoji replacement for primary-bot message text."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


RESTRICTED_EMOJI_SET_NAME = "RestrictedEmoji"
_VARIATION_SELECTOR = "\ufe0f"
_HTML_PART_RE = re.compile(r"(<[^>]+>)")
_HTML_TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z0-9-]+)")
_BLOCKED_TAGS = {"a", "code", "pre", "tg-emoji"}


def build_text_emoji_map(stickers: Iterable[Any]) -> dict[str, str]:
    """Build alternative-text -> custom-id mapping from a Telegram sticker set."""
    result: dict[str, str] = {}

    for sticker in stickers:
        emoji = getattr(sticker, "emoji", None)
        custom_emoji_id = getattr(sticker, "custom_emoji_id", None)
        if not emoji or not custom_emoji_id:
            continue

        # Keep the first item when a set contains several animated variants for
        # the same standard emoji (RestrictedEmoji currently does this for 🙂).
        result.setdefault(emoji, custom_emoji_id)

        without_variation = emoji.replace(_VARIATION_SELECTOR, "")
        if without_variation != emoji:
            result.setdefault(without_variation, custom_emoji_id)
        elif len(emoji) == 1:
            result.setdefault(f"{emoji}{_VARIATION_SELECTOR}", custom_emoji_id)

    return result


def compile_text_emoji_pattern(emoji_map: Mapping[str, str]) -> re.Pattern[str] | None:
    if not emoji_map:
        return None

    # Composite emoji must win over their shorter components.
    alternatives = sorted(emoji_map, key=len, reverse=True)
    return re.compile("|".join(re.escape(emoji) for emoji in alternatives))


def apply_premium_text_emojis(
    text: str,
    emoji_map: Mapping[str, str],
    pattern: re.Pattern[str] | None = None,
) -> str:
    """Replace standard emoji in safe HTML text nodes with custom emoji tags."""
    if not text or not emoji_map:
        return text

    pattern = pattern or compile_text_emoji_pattern(emoji_map)
    if pattern is None:
        return text

    def replace_emoji(match: re.Match[str]) -> str:
        emoji = match.group(0)
        custom_emoji_id = emoji_map[emoji]
        return f'<tg-emoji emoji-id="{custom_emoji_id}">{emoji}</tg-emoji>'

    parts = _HTML_PART_RE.split(text)
    blocked_depth = 0
    rendered: list[str] = []

    for part in parts:
        tag_match = _HTML_TAG_RE.match(part) if part.startswith("<") else None
        if tag_match:
            is_closing = bool(tag_match.group(1))
            tag_name = tag_match.group(2).lower()
            is_self_closing = part.rstrip().endswith("/>")

            if is_closing and tag_name in _BLOCKED_TAGS:
                blocked_depth = max(0, blocked_depth - 1)

            rendered.append(part)

            if not is_closing and not is_self_closing and tag_name in _BLOCKED_TAGS:
                blocked_depth += 1
            continue

        if blocked_depth == 0:
            part = pattern.sub(replace_emoji, part)
        rendered.append(part)

    return "".join(rendered)
