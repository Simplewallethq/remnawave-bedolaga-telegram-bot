from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class AppleIapVerifyRequest(BaseModel):
    user_id: int
    transaction_jws: str
    product_id: Optional[str] = None


class AppleIapVerifyResponse(BaseModel):
    status: str
    subscription_end_date: datetime
