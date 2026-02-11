"""CRUD операции для статистики подключений пользователей к VPN"""
import logging
from typing import Dict, Any
from datetime import datetime, timedelta
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User, Subscription, SubscriptionStatus

logger = logging.getLogger(__name__)


async def get_vpn_connection_stats(
    db: AsyncSession,
    days: int = 30
) -> Dict[str, Any]:
    """
    Получает статистику по подключениям пользователей к VPN
    
    Args:
        db: Сессия базы данных
        days: Количество дней для анализа (для фильтрации новых пользователей)
    
    Returns:
        Словарь со статистикой:
        - total_users: Общее количество пользователей
        - users_with_subscription: Пользователей с подпиской
        - users_connected_to_vpn: Пользователей, подключившихся к VPN
        - users_not_connected: Пользователей, не подключившихся к VPN
        - connection_rate: Процент подключившихся пользователей
        - active_users_connected: Активных пользователей, подключившихся к VPN
        - active_users_not_connected: Активных пользователей, не подключившихся к VPN
    """
    try:
        # Общее количество пользователей
        total_users_result = await db.execute(
            select(func.count(User.id))
        )
        total_users = total_users_result.scalar() or 0
        
        # Пользователи с подпиской
        users_with_sub_result = await db.execute(
            select(func.count(User.id))
            .join(Subscription, User.id == Subscription.user_id)
        )
        users_with_subscription = users_with_sub_result.scalar() or 0
        
        # Пользователи, подключившиеся к VPN
        connected_result = await db.execute(
            select(func.count(User.id))
            .where(User.has_connected_to_vpn == True)
        )
        users_connected_to_vpn = connected_result.scalar() or 0
        
        # Пользователи с подпиской, но не подключившиеся к VPN
        not_connected_result = await db.execute(
            select(func.count(User.id))
            .join(Subscription, User.id == Subscription.user_id)
            .where(User.has_connected_to_vpn == False)
        )
        users_not_connected = not_connected_result.scalar() or 0
        
        # Активные пользователи (с активной подпиской), подключившиеся к VPN
        now = datetime.utcnow()
        active_connected_result = await db.execute(
            select(func.count(User.id))
            .join(Subscription, User.id == Subscription.user_id)
            .where(
                and_(
                    User.has_connected_to_vpn == True,
                    Subscription.status == SubscriptionStatus.ACTIVE.value,
                    Subscription.end_date > now
                )
            )
        )
        active_users_connected = active_connected_result.scalar() or 0
        
        # Активные пользователи, не подключившиеся к VPN
        active_not_connected_result = await db.execute(
            select(func.count(User.id))
            .join(Subscription, User.id == Subscription.user_id)
            .where(
                and_(
                    User.has_connected_to_vpn == False,
                    Subscription.status == SubscriptionStatus.ACTIVE.value,
                    Subscription.end_date > now
                )
            )
        )
        active_users_not_connected = active_not_connected_result.scalar() or 0
        
        # Процент подключившихся
        connection_rate = 0.0
        if users_with_subscription > 0:
            connection_rate = (users_connected_to_vpn / users_with_subscription) * 100
        
        stats = {
            "total_users": total_users,
            "users_with_subscription": users_with_subscription,
            "users_connected_to_vpn": users_connected_to_vpn,
            "users_not_connected": users_not_connected,
            "connection_rate": round(connection_rate, 2),
            "active_users_connected": active_users_connected,
            "active_users_not_connected": active_users_not_connected,
        }
        
        logger.info(f"📊 Статистика подключений к VPN: {stats}")
        return stats
        
    except Exception as e:
        logger.error(f"Ошибка получения статистики подключений: {e}")
        return {
            "total_users": 0,
            "users_with_subscription": 0,
            "users_connected_to_vpn": 0,
            "users_not_connected": 0,
            "connection_rate": 0.0,
            "active_users_connected": 0,
            "active_users_not_connected": 0,
        }


async def get_users_not_connected_to_vpn(
    db: AsyncSession,
    limit: int = 100,
    offset: int = 0,
    active_only: bool = True
) -> list[User]:
    """
    Получает список пользователей, которые не подключились к VPN
    
    Args:
        db: Сессия базы данных
        limit: Максимальное количество результатов
        offset: Смещение для пагинации
        active_only: Только пользователи с активной подпиской
    
    Returns:
        Список пользователей
    """
    try:
        query = (
            select(User)
            .join(Subscription, User.id == Subscription.user_id)
            .where(User.has_connected_to_vpn == False)
        )
        
        if active_only:
            now = datetime.utcnow()
            query = query.where(
                and_(
                    Subscription.status == SubscriptionStatus.ACTIVE.value,
                    Subscription.end_date > now
                )
            )
        
        query = query.limit(limit).offset(offset)
        
        result = await db.execute(query)
        users = result.scalars().all()
        
        logger.info(f"Найдено {len(users)} пользователей без подключения к VPN")
        return list(users)
        
    except Exception as e:
        logger.error(f"Ошибка получения пользователей без подключения: {e}")
        return []
