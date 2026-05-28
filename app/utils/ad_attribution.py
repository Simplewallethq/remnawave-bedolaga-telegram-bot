from __future__ import annotations

import re
from typing import Optional

_AD_RE = re.compile(r"^([a-zA-Z0-9_]+)-([a-zA-Z0-9]{8})$")


def parse_ad_payload(payload: str) -> Optional[tuple[str, str]]:
    """Parse ad deep-link payload of form '{source}-{8-char campaign_id}'.

    Returns (source, campaign_id) or None if the payload doesn't match.
    """
    m = _AD_RE.match(payload)
    return (m.group(1), m.group(2)) if m else None
