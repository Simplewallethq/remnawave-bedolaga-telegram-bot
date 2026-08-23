from types import SimpleNamespace

from app.utils.premium_text import (
    apply_premium_text_emojis,
    build_text_emoji_map,
    compile_text_emoji_pattern,
)


def test_build_map_keeps_first_duplicate_and_supports_variation_aliases():
    stickers = [
        SimpleNamespace(emoji="🙂", custom_emoji_id="first"),
        SimpleNamespace(emoji="🙂", custom_emoji_id="second"),
        SimpleNamespace(emoji="✌️", custom_emoji_id="victory"),
    ]

    result = build_text_emoji_map(stickers)

    assert result["🙂"] == "first"
    assert result["✌️"] == "victory"
    assert result["✌"] == "victory"


def test_replaces_emoji_only_in_safe_html_text_nodes():
    emoji_map = {"✅": "ok", "👨‍💻": "developer", "🖥": "screen"}
    pattern = compile_text_emoji_pattern(emoji_map)
    source = (
        "✅ <b>Готово 👨‍💻</b> "
        '<a href="https://example.com">✅ Ссылка</a> '
        "<code>✅ код</code> "
        '<tg-emoji emoji-id="existing">✅</tg-emoji>'
    )

    result = apply_premium_text_emojis(source, emoji_map, pattern)

    assert '<tg-emoji emoji-id="ok">✅</tg-emoji>' in result
    assert '<b>Готово <tg-emoji emoji-id="developer">👨‍💻</tg-emoji></b>' in result
    assert '<a href="https://example.com">✅ Ссылка</a>' in result
    assert "<code>✅ код</code>" in result
    assert result.endswith('<tg-emoji emoji-id="existing">✅</tg-emoji>')


def test_composite_emoji_is_replaced_before_shorter_component():
    emoji_map = {"👨": "person", "👨‍💻": "developer"}

    result = apply_premium_text_emojis("Разработчик 👨‍💻", emoji_map)

    assert result == (
        'Разработчик <tg-emoji emoji-id="developer">👨‍💻</tg-emoji>'
    )
