"""Partner VIP-link token validation.

The kazApi service issues short HMAC-signed tokens via LinkToVipUser. The bot
decodes and verifies them locally in this module — no callback to kazApi is
needed at redemption time. One-time-use enforcement happens at the DB layer
(UNIQUE constraint on partner_link_redemptions.jti).

Token layout (binary, 34 bytes, encoded as base64url without padding ≈ 46 chars):
    [0..15]   jti                — 16 random bytes
    [16..20]  sub_until_unix     — 5 bytes, big-endian
    [21..25]  link_expires_unix  — 5 bytes, big-endian
    [26..33]  hmac_sha256(payload, SECRET)[:8]

Must match service/vip_link.go in kazApi.
"""

from __future__ import annotations

import base64
import hmac
import logging
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Optional

from app.config import settings


logger = logging.getLogger(__name__)


TOKEN_BYTES = 34
PAYLOAD_BYTES = 26
HMAC_BYTES = 8
JTI_BYTES = 16


@dataclass(frozen=True)
class PartnerTokenPayload:
    jti: str  # base64url(jti_raw), matches kazApi audit jti
    sub_until: datetime
    link_expires_at: datetime


def _read_uint40_be(buf: bytes) -> int:
    """Reads a 5-byte big-endian unsigned integer from buf[:5]."""
    if len(buf) < 5:
        raise ValueError("buffer too short")
    high, low = struct.unpack(">BI", buf[:5])
    return (high << 32) | low


def decode_and_verify_partner_token(token_str: str) -> Optional[PartnerTokenPayload]:
    """Decode and verify a VIP-link token from start_parameter (without the ``p_`` prefix).

    Returns the payload if HMAC matches one of the configured secrets and the
    link has not expired. Returns ``None`` on any failure (invalid format,
    bad HMAC, expired). Failures are intentionally not distinguished to the
    caller — an invalid token is just invalid.
    """
    if not token_str:
        return None

    secrets = settings.get_partner_link_secrets()
    if not secrets:
        logger.warning("PARTNER_LINK_SECRET is not configured; rejecting all partner tokens")
        return None

    try:
        raw = base64.urlsafe_b64decode(token_str + "=" * (-len(token_str) % 4))
    except (ValueError, TypeError):
        return None

    if len(raw) != TOKEN_BYTES:
        return None

    payload = raw[:PAYLOAD_BYTES]
    tag = raw[PAYLOAD_BYTES:]

    # Constant-time check against all configured secrets (current + previous for rotation).
    valid = False
    for secret in secrets:
        expected = hmac.new(secret, payload, sha256).digest()[:HMAC_BYTES]
        if hmac.compare_digest(tag, expected):
            valid = True
            break
    if not valid:
        return None

    try:
        sub_until_unix = _read_uint40_be(payload[JTI_BYTES : JTI_BYTES + 5])
        link_expires_unix = _read_uint40_be(payload[JTI_BYTES + 5 : JTI_BYTES + 10])
    except ValueError:
        return None

    link_expires_at = datetime.fromtimestamp(link_expires_unix, tz=timezone.utc).replace(tzinfo=None)
    if link_expires_at <= datetime.utcnow():
        return None

    sub_until = datetime.fromtimestamp(sub_until_unix, tz=timezone.utc).replace(tzinfo=None)
    jti_raw = payload[:JTI_BYTES]
    jti = base64.urlsafe_b64encode(jti_raw).rstrip(b"=").decode("ascii")

    return PartnerTokenPayload(jti=jti, sub_until=sub_until, link_expires_at=link_expires_at)
