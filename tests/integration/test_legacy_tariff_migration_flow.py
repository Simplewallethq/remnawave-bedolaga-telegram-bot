"""Прогон migrate_legacy_subscriptions_to_plans() на реальной SQLite-БД.

Сырой SQL миграции (COALESCE по булевым колонкам, JOIN-ы, самолечащий проход)
проверяется только на живой БД, поэтому здесь поднимается настоящий движок,
а не мок сессии. RemnaWave-пуш подменяется — сеть в тестах не трогаем.
"""

import importlib
import json
import sys
import types
from datetime import datetime, timedelta

import pytest

# conftest подменяет aiosqlite заглушкой, чтобы модули импортировались без драйвера.
# Здесь нужен настоящий драйвер — возвращаем его, а если его нет, тест пропускаем.
if isinstance(sys.modules.get("aiosqlite"), types.ModuleType) and not hasattr(
    sys.modules.get("aiosqlite"), "connect"
):
    sys.modules.pop("aiosqlite", None)
    try:
        importlib.import_module("aiosqlite")
    except ImportError:  # pragma: no cover
        pass

aiosqlite = pytest.importorskip("aiosqlite", reason="нужен настоящий драйвер aiosqlite")
if not hasattr(aiosqlite, "connect"):  # pragma: no cover
    pytest.skip("aiosqlite подменён заглушкой", allow_module_level=True)

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import universal_migration as um  # noqa: E402
from app.database.models import Base, Subscription, User  # noqa: E402


PLAN_SEED = [
    # code, display_name, device_limit, traffic_limit_gb, sort_order, is_active
    ("app", "App", 1, 30, 10, 0),
    ("solo", "Solo", 1, 0, 20, 1),
    ("plus", "Plus", 3, 0, 30, 1),
    ("pro", "Pro", 10, 0, 40, 1),
]


async def _prepare(tmp_path, monkeypatch, mode):
    """SQLite-движок с тарифами и легаси-подписками + перехват пушей в панель.

    Обычная async-функция, а не фикстура: conftest даёт каждому тесту свежий event
    loop и не умеет асинхронные фикстуры, а движок нельзя переносить между циклами.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'migration.sqlite'}")

    pushed: list = []

    async def fake_push(payloads):
        pushed.extend(payloads)

    monkeypatch.setattr(um, "engine", engine)
    monkeypatch.setattr(um, "_push_limits_to_panel", fake_push)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for code, name, devices, traffic, order, active in PLAN_SEED:
            await conn.execute(
                text(
                    "INSERT INTO subscription_plans (code, display_name, device_limit,"
                    " traffic_limit_gb, traffic_reset_strategy, custom_app_only,"
                    " priority_support, sort_order, is_active)"
                    " VALUES (:c, :n, :d, :t, 'NO_RESET', 0, 0, :o, :a)"
                ),
                {"c": code, "n": name, "d": devices, "t": traffic, "o": order, "a": active},
            )

    now = datetime.utcnow()
    future = now + timedelta(days=200)
    past = now - timedelta(days=5)

    # telegram_id, device_limit, traffic_gb, status, end_date, is_trial, is_partner
    fixtures = [
        (101, 1, 100, "active", future, False, False),
        (102, 2, 0, "active", future, False, False),
        (103, 5, 250, "active", future, False, False),
        (104, 15, 0, "active", future, False, False),
        (105, 1, 100, "active", future, True, False),   # триал — не трогаем
        (106, 3, 100, "active", future, False, True),   # партнёрская — не трогаем
        (107, 3, 100, "expired", past, False, False),   # истёкшая — не трогаем
        (108, 3, 100, "active", past, False, False),    # дата в прошлом — не трогаем
        (109, None, None, "active", future, False, False),
    ]

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        for telegram_id, devices, traffic, status, end_date, trial, partner in fixtures:
            user = User(
                telegram_id=telegram_id,
                remnawave_uuid=f"uuid-{telegram_id}",
                language="ru",
                status="active",
            )
            session.add(user)
            await session.flush()
            session.add(
                Subscription(
                    user_id=user.id,
                    status=status,
                    is_trial=trial,
                    is_partner=partner,
                    start_date=now,
                    end_date=end_date,
                    traffic_limit_gb=traffic,
                    device_limit=devices,
                    connected_squads=["squad-a"],
                )
            )
        await session.commit()

    monkeypatch.setattr(settings, "LEGACY_TARIFF_MIGRATION_MODE", mode, raising=False)
    monkeypatch.setattr(settings, "LEGACY_TARIFF_MIGRATION_TELEGRAM_IDS", "", raising=False)

    return engine, pushed, now


async def _fetch_state(engine):
    """{telegram_id: (plan_code, plan_period_days, device_limit, traffic_gb, end_date, squads)}"""
    async with engine.begin() as conn:
        rows = (await conn.execute(text(
            "SELECT u.telegram_id, p.code, s.plan_period_days, s.device_limit,"
            " s.traffic_limit_gb, s.end_date, s.connected_squads"
            " FROM subscriptions s JOIN users u ON u.id = s.user_id"
            " LEFT JOIN subscription_plans p ON p.id = s.plan_id"
        ))).fetchall()
    return {row[0]: tuple(row[1:]) for row in rows}


async def test_dry_run_changes_nothing(tmp_path, monkeypatch):
    engine, pushed, _ = await _prepare(tmp_path, monkeypatch, "dry")

    before = await _fetch_state(engine)
    assert await um.migrate_legacy_subscriptions_to_plans() is True

    assert await _fetch_state(engine) == before
    assert pushed == []
    await engine.dispose()


async def test_pilot_limits_scope_to_listed_users(tmp_path, monkeypatch):
    engine, pushed, _ = await _prepare(tmp_path, monkeypatch, "apply")
    monkeypatch.setattr(settings, "LEGACY_TARIFF_MIGRATION_TELEGRAM_IDS", "101", raising=False)

    assert await um.migrate_legacy_subscriptions_to_plans() is True

    state = await _fetch_state(engine)
    assert state[101][0] == "solo"
    assert [tg for tg, row in state.items() if row[0] is not None] == [101]
    assert pushed == [("uuid-101", 1, 0)]
    await engine.dispose()


async def test_full_migration_preserves_dates_and_raises_limits(tmp_path, monkeypatch):
    engine, pushed, now = await _prepare(tmp_path, monkeypatch, "apply")

    before = await _fetch_state(engine)
    assert await um.migrate_legacy_subscriptions_to_plans() is True
    after = await _fetch_state(engine)

    # 1 → Solo, 2 → Plus, 5 → Pro, 15 → Pro с сохранением повышенного лимита, NULL → Solo
    assert [after[tg][:4] for tg in (101, 102, 103, 104, 109)] == [
        ("solo", 30, 1, 0),
        ("plus", 30, 3, 0),
        ("pro", 30, 10, 0),
        ("pro", 30, 15, 0),
        ("solo", 30, 1, 0),
    ]

    # Триалы, партнёрские и истёкшие остаются легаси и не меняются вовсе.
    for telegram_id in (105, 106, 107, 108):
        assert after[telegram_id][0] is None
        assert after[telegram_id] == before[telegram_id]

    # Срок и набор серверов не трогаем ни у кого.
    for telegram_id, row in after.items():
        assert row[4] == before[telegram_id][4], f"end_date изменился у {telegram_id}"
        # Сырой SELECT отдаёт JSON-колонку строкой — сравниваем после разбора.
        assert json.loads(row[5]) == ["squad-a"], f"сквады изменились у {telegram_id}"

    # Ничего платного и активного без тарифа не осталось.
    async with engine.begin() as conn:
        left = (await conn.execute(text(
            "SELECT count(*) FROM subscriptions WHERE plan_id IS NULL AND status = 'active'"
            " AND end_date > :now AND is_trial = 0 AND is_partner = 0"
        ), {"now": now})).scalar()
    assert left == 0

    # Новые лимиты уходят в панель по каждой переведённой подписке — иначе панель
    # продолжит резать по старому HWID-лимиту, а обратный sync откатит их в БД.
    assert sorted(pushed) == [
        ("uuid-101", 1, 0), ("uuid-102", 3, 0), ("uuid-103", 10, 0),
        ("uuid-104", 15, 0), ("uuid-109", 1, 0),
    ]
    await engine.dispose()


async def test_second_run_is_a_noop(tmp_path, monkeypatch):
    engine, pushed, _ = await _prepare(tmp_path, monkeypatch, "apply")

    assert await um.migrate_legacy_subscriptions_to_plans() is True
    state_after_first = await _fetch_state(engine)
    pushed.clear()

    assert await um.migrate_legacy_subscriptions_to_plans() is True

    assert await _fetch_state(engine) == state_after_first
    assert pushed == []
    await engine.dispose()


async def test_panel_rollback_is_healed_on_next_run(tmp_path, monkeypatch):
    """Панель откатила device_limit через sync — следующий старт чинит БД и пушит снова."""
    engine, pushed, _ = await _prepare(tmp_path, monkeypatch, "apply")

    assert await um.migrate_legacy_subscriptions_to_plans() is True

    async with engine.begin() as conn:
        await conn.execute(text(
            "UPDATE subscriptions SET device_limit = 1"
            " WHERE user_id = (SELECT id FROM users WHERE telegram_id = 103)"
        ))
    pushed.clear()

    assert await um.migrate_legacy_subscriptions_to_plans() is True

    state = await _fetch_state(engine)
    assert state[103][2] == 10
    assert pushed == [("uuid-103", 10, 0)]
    # Pro-подписка с сохранённым повышенным лимитом самолечением не понижается.
    assert state[104][2] == 15
    await engine.dispose()
