from pathlib import Path

from app.config import settings
from app.utils import bot_registry


PAGE_IMAGE = Path("images") / "pay.webp"


def test_mirror_shows_its_own_picture(tmp_path):
    mirror_logo = tmp_path / "mirror.jpg"
    mirror_logo.write_bytes(b"picture")
    bot_registry.clear()
    bot_registry.register_bot(1001, mirror_logo)
    try:
        assert bot_registry.resolve_photo_for_bot(1001, PAGE_IMAGE) == mirror_logo
    finally:
        bot_registry.clear()


def test_mirror_without_picture_keeps_shared_artwork(tmp_path):
    bot_registry.clear()
    bot_registry.register_bot(1002, Path(settings.LOGO_FILE))
    bot_registry.register_bot(1003, tmp_path / "missing.jpg")
    try:
        assert bot_registry.resolve_photo_for_bot(1002, PAGE_IMAGE) == PAGE_IMAGE
        assert bot_registry.resolve_photo_for_bot(1003, PAGE_IMAGE) == PAGE_IMAGE
    finally:
        bot_registry.clear()


def test_unknown_bot_keeps_shared_artwork():
    bot_registry.clear()
    assert bot_registry.resolve_photo_for_bot(2001, PAGE_IMAGE) == PAGE_IMAGE
    assert bot_registry.resolve_photo_for_bot(None, PAGE_IMAGE) == PAGE_IMAGE


def test_personal_pictures_are_not_replaced(tmp_path):
    mirror_logo = tmp_path / "mirror.jpg"
    mirror_logo.write_bytes(b"picture")
    qr_path = Path("data") / "referral_qr" / "7.png"
    bot_registry.clear()
    bot_registry.register_bot(1004, mirror_logo)
    try:
        assert bot_registry.resolve_photo_for_bot(1004, qr_path) == qr_path
    finally:
        bot_registry.clear()
