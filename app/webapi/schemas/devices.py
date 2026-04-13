from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class DeviceLinkRequest(BaseModel):
    """Request body for POST /api/devices/{device_id}/link."""
    subscription_id: int


class DeviceLinkResponse(BaseModel):
    """Response for successful device link operations."""
    device_id: str
    subscription_id: int
    linked_at: datetime


class DeviceErrorResponse(BaseModel):
    """Error response with detail message."""
    detail: str
