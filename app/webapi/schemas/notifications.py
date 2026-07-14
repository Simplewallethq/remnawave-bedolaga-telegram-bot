from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class NotificationItem(BaseModel):
    id: int
    type: str
    title: Optional[str] = None
    body: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    is_read: bool
    created_at: datetime


class NotificationListResponse(BaseModel):
    items: List[NotificationItem]
    total: int
    unread_count: int


class NotificationUnreadCountResponse(BaseModel):
    unread_count: int


class NotificationMarkReadResponse(BaseModel):
    success: bool = True
    unread_count: int


class NotificationMarkAllReadResponse(BaseModel):
    success: bool = True
    marked: int
