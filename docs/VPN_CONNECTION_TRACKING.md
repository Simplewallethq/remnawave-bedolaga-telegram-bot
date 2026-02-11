# Отслеживание подключений пользователей к VPN

## Описание

Реализована функциональность для отслеживания факта подключения пользователей к VPN. Система автоматически определяет, подключался ли пользователь к VPN хотя бы один раз, и сохраняет эту информацию в базе данных.

## Изменения в базе данных

### Таблица `users`

Добавлено новое поле:
- **`has_connected_to_vpn`** (Boolean, NOT NULL, default: False) - флаг, указывающий, подключался ли пользователь к VPN хотя бы раз

## Как это работает

### 1. Источник данных

Информация о подключениях берется из RemnaWave API. Когда пользователь впервые подключается к VPN, RemnaWave фиксирует это в поле `first_connected_at` объекта `UserTraffic`.

### 2. Обновление флага

Флаг `has_connected_to_vpn` обновляется в двух местах:

#### a) При синхронизации использования подписки
В функции `SubscriptionService.sync_subscription_usage()`:
- При каждой синхронизации трафика проверяется наличие `first_connected_at` в данных RemnaWave
- Если пользователь подключился, но флаг еще не установлен, он обновляется
- Логируется событие первого подключения

#### b) При периодической синхронизации с RemnaWave
В функции `MonitoringService._sync_with_remnawave()`:
- Каждый час (когда `minute == 0`) выполняется проверка всех пользователей
- Запрашиваются только пользователи с `has_connected_to_vpn = False` и существующим `remnawave_uuid`
- Для каждого пользователя проверяется наличие `first_connected_at` в RemnaWave
- Обновляются флаги для всех подключившихся пользователей
- Логируется количество обновленных записей

## Использование

### Получение статистики

```python
from app.database.crud.vpn_connection_stats import get_vpn_connection_stats

async with get_db() as db:
    stats = await get_vpn_connection_stats(db)
    print(f"Всего пользователей: {stats['total_users']}")
    print(f"Подключились к VPN: {stats['users_connected_to_vpn']}")
    print(f"Процент подключений: {stats['connection_rate']}%")
```

Возвращаемые данные:
- `total_users` - общее количество пользователей
- `users_with_subscription` - пользователей с подпиской
- `users_connected_to_vpn` - пользователей, подключившихся к VPN
- `users_not_connected` - пользователей с подпиской, но без подключения
- `connection_rate` - процент подключившихся пользователей
- `active_users_connected` - активных пользователей, подключившихся к VPN
- `active_users_not_connected` - активных пользователей без подключения

### Получение списка пользователей без подключения

```python
from app.database.crud.vpn_connection_stats import get_users_not_connected_to_vpn

async with get_db() as db:
    # Только активные пользователи
    users = await get_users_not_connected_to_vpn(db, limit=50, active_only=True)
    
    # Все пользователи с подпиской
    all_users = await get_users_not_connected_to_vpn(db, limit=100, active_only=False)
```

### Проверка подключения конкретного пользователя

```python
from app.database.crud.user import get_user_by_id

async with get_db() as db:
    user = await get_user_by_id(db, user_id=123)
    if user.has_connected_to_vpn:
        print("Пользователь подключался к VPN")
    else:
        print("Пользователь еще не подключался к VPN")
```

## SQL запросы для аналитики

### Статистика по подключениям

```sql
-- Общая статистика
SELECT 
    COUNT(*) as total_users,
    SUM(CASE WHEN has_connected_to_vpn THEN 1 ELSE 0 END) as connected_users,
    ROUND(100.0 * SUM(CASE WHEN has_connected_to_vpn THEN 1 ELSE 0 END) / COUNT(*), 2) as connection_rate
FROM users
WHERE id IN (SELECT user_id FROM subscriptions);

-- Пользователи с активной подпиской без подключения
SELECT 
    u.id,
    u.telegram_id,
    u.username,
    u.first_name,
    s.created_at as subscription_created,
    s.end_date as subscription_ends
FROM users u
JOIN subscriptions s ON u.id = s.user_id
WHERE 
    u.has_connected_to_vpn = FALSE
    AND s.status = 'active'
    AND s.end_date > NOW();

-- Статистика по дням регистрации
SELECT 
    DATE(u.created_at) as registration_date,
    COUNT(*) as total_registered,
    SUM(CASE WHEN u.has_connected_to_vpn THEN 1 ELSE 0 END) as connected,
    ROUND(100.0 * SUM(CASE WHEN u.has_connected_to_vpn THEN 1 ELSE 0 END) / COUNT(*), 2) as connection_rate
FROM users u
WHERE u.id IN (SELECT user_id FROM subscriptions)
GROUP BY DATE(u.created_at)
ORDER BY registration_date DESC
LIMIT 30;
```

## Миграция

Миграция базы данных выполняется автоматически при запуске приложения через Alembic:

```bash
# Применить миграцию
alembic upgrade head

# Откатить миграцию (если нужно)
alembic downgrade -1
```

Файл миграции: `migrations/alembic/versions/a1b2c3d4e5f6_add_has_connected_to_vpn_to_users.py`

## Логирование

Система логирует следующие события:

1. **Первое подключение пользователя** (INFO):
   ```
   ✅ Пользователь 123456789 впервые подключился к VPN в 2026-02-11 19:00:00
   ```

2. **Массовое обновление при синхронизации** (INFO):
   ```
   🔄 Обновлено флагов подключения к VPN: 5
   ```

3. **Получение статистики** (INFO):
   ```
   📊 Статистика подключений к VPN: {'total_users': 1000, 'users_connected_to_vpn': 750, ...}
   ```

## Примечания

- Флаг устанавливается только один раз при первом подключении
- Информация синхронизируется с RemnaWave API
- Проверка выполняется автоматически каждый час
- Данные используются для аналитики и улучшения конверсии пользователей
