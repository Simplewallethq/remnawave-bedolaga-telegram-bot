#!/usr/bin/env python3
"""E2E-проверка cabinet API (/cabinet/*) для личного кабинета LetoVPNSite.

Запуск (бот должен быть поднят, обычно docker compose):
    python3 scripts/test_cabinet_api.py [--base http://localhost:8080]

Параметры берутся из .env в корне репозитория (BOT_TOKEN, WEB_API_DEFAULT_TOKEN).
Скрипт создаёт реального пользователя test+<ts>@letovpn.test (и аккаунт в панели
RemnaWave, если она настроена) и пополняет ему баланс через админ-API.
Создание платежей (topup) шлёт запросы в реальные платёжки — суммы минимальные,
инвойсы оплачивать не нужно.
"""

import argparse
import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PASSED = []
FAILED = []


def check(name, cond, info=""):
    if cond:
        PASSED.append(name)
        print(f"  ✅ {name}")
    else:
        FAILED.append((name, info))
        print(f"  ❌ {name}  {info}")


def load_env():
    env = {}
    path = Path(__file__).resolve().parent.parent / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def request(method, url, body=None, token=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return e.code, raw
    except Exception as e:
        return 0, str(e)


def sign_init_data(bot_token: str, telegram_id: int) -> str:
    """Собирает валидный Telegram WebApp initData (подпись HMAC по BOT_TOKEN)."""
    user = json.dumps(
        {"id": telegram_id, "first_name": "E2E", "username": "e2e_test"},
        separators=(",", ":"),
    )
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAE2E2E2",
        "user": user,
    }
    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    sig = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    pairs["hash"] = sig
    return urllib.parse.urlencode(pairs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8080")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    cab = base + "/cabinet"

    env = load_env()
    admin_key = env.get("WEB_API_DEFAULT_TOKEN", "")
    bot_token = env.get("BOT_TOKEN", "")

    ts = int(time.time())
    email = f"test+{ts}@letovpn.test"
    password = "E2e-test-password-1"

    print(f"\n— Auth: register/login ({email})")
    st, data = request("POST", f"{cab}/auth/register", {"email": email, "password": password})
    check("register 200", st == 200, f"got {st}: {data}")
    token = (data or {}).get("token")
    user0 = (data or {}).get("user") or {}
    check("register: token присутствует", bool(token))
    check("register: профиль {id,glyphSeed,balanceRub}", all(k in user0 for k in ("id", "glyphSeed", "balanceRub")), str(user0))

    st, data = request("POST", f"{cab}/auth/register", {"email": email, "password": password})
    check("повторный register → 409", st == 409, f"got {st}")

    st, data = request("POST", f"{cab}/auth/login", {"email": email, "password": "wrong"})
    check("login с неверным паролем → 401", st == 401, f"got {st}")

    st, data = request("POST", f"{cab}/auth/login", {"email": email, "password": password})
    check("login 200", st == 200, f"got {st}: {data}")
    token = (data or {}).get("token") or token

    print("\n— Профиль / подписка / тарифы")
    st, data = request("GET", f"{cab}/me")
    check("GET /me без токена → 401", st == 401, f"got {st}")

    st, data = request("GET", f"{cab}/me", token="invalid.jwt.token")
    check("GET /me с мусорным JWT → 401", st == 401, f"got {st}")

    st, me = request("GET", f"{cab}/me", token=token)
    check("GET /me 200", st == 200, f"got {st}: {me}")
    check("me: balanceRub == 0", (me or {}).get("balanceRub") == 0, str(me))

    st, sub = request("GET", f"{cab}/subscription", token=token)
    check("GET /subscription 200", st == 200, f"got {st}: {sub}")
    s = (sub or {}).get("subscription") or {}
    need = ("planName", "devices", "expiresAt", "daysLeft", "autoRenew", "status")
    check("subscription: все поля фронта", all(k in s for k in need), str(s))
    check("subscription: триал активен", s.get("isTrial") is True and s.get("status") in ("active", "trial"), str(s))
    sub_url_ok = bool(s.get("subscriptionUrl"))
    check("subscription: subscriptionUrl (RemnaWave-провижининг)", sub_url_ok, "панель не создала аккаунт — см. логи")

    st, plans_resp = request("GET", f"{cab}/plans", token=token)
    check("GET /plans 200", st == 200, f"got {st}")
    plans = (plans_resp or {}).get("plans") or []
    check("plans: непустой список", len(plans) > 0, str(plans_resp))
    plan_fields = ("id", "name", "price", "devices", "unlimited", "features")
    check("plans: поля фронта", plans and all(all(k in p for k in plan_fields) for p in plans), str(plans[:1]))
    paid_plan = next((p for p in plans if p.get("price", 0) > 0), None)
    check("plans: есть платный тариф", paid_plan is not None)

    print("\n— Автопродление")
    st, data = request("POST", f"{cab}/subscription/autorenew", {"enabled": True}, token=token)
    check("autorenew on 200", st == 200 and (data or {}).get("subscription", {}).get("autoRenew") is True, f"got {st}: {data}")
    st, data = request("POST", f"{cab}/subscription/autorenew", {"enabled": False}, token=token)
    check("autorenew off 200", st == 200 and (data or {}).get("subscription", {}).get("autoRenew") is False, f"got {st}: {data}")

    print("\n— Покупка тарифа")
    plan_id = paid_plan["id"] if paid_plan else "solo"
    price_rub = paid_plan["price"] if paid_plan else 0
    st, data = request("POST", f"{cab}/subscription/purchase", {"plan_id": plan_id, "months": 1}, token=token)
    check("purchase при пустом балансе → 402", st == 402, f"got {st}: {data}")

    st, data = request("POST", f"{cab}/subscription/purchase", {"plan_id": "nope", "months": 1}, token=token)
    check("purchase несуществующего тарифа → 404", st == 404, f"got {st}")

    # Пополняем баланс через админ-API
    internal_id = None
    st, users = request(
        "GET", f"{base}/users?search={urllib.parse.quote(email)}",
        headers={"X-API-Key": admin_key},
    )
    if st == 200:
        items = users.get("items") or users.get("users") or (users if isinstance(users, list) else [])
        for u in items:
            if u.get("email") == email:
                internal_id = u.get("id")
                break
    check("админ-API: пользователь найден", internal_id is not None, f"status {st}: {str(users)[:200]}")

    topup_kopeks = int(price_rub * 100 + 50000)
    st, data = request(
        "POST", f"{base}/users/{internal_id}/balance",
        {"amount_kopeks": topup_kopeks, "description": "E2E test topup"},
        headers={"X-API-Key": admin_key},
    )
    check("админ-API: баланс пополнен", st == 200, f"got {st}: {data}")

    st, me = request("GET", f"{cab}/me", token=token)
    check("me: баланс обновился", (me or {}).get("balanceRub", 0) >= price_rub, str(me))

    st, data = request("POST", f"{cab}/subscription/purchase", {"plan_id": plan_id, "months": 1}, token=token)
    check("purchase 200 (с баланса)", st == 200, f"got {st}: {data}")
    s = (data or {}).get("subscription") or {}
    check("purchase: тариф применён", s.get("planId") == plan_id and s.get("isTrial") is False, str(s))

    st, me = request("GET", f"{cab}/me", token=token)
    expected = round(topup_kopeks / 100 - price_rub, 2)
    check("purchase: баланс списан", abs((me or {}).get("balanceRub", -1) - expected) < 0.01,
          f"balance={ (me or {}).get('balanceRub') }, ожидали {expected}")

    print("\n— Транзакции")
    st, data = request("GET", f"{cab}/transactions?limit=100", token=token)
    check("GET /transactions 200", st == 200, f"got {st}")
    items = (data or {}).get("items") or []
    check("transactions: items есть", len(items) >= 2, str(data)[:300])
    dep = next((i for i in items if i.get("amount", 0) > 0), None)
    pay = next((i for i in items if i.get("amount", 0) < 0), None)
    check("transactions: есть пополнение (+) и покупка (−)", dep is not None and pay is not None, str(items[:3]))
    tx_fields = ("id", "amount", "status", "method", "date", "labelKey")
    check("transactions: поля фронта", items and all(k in items[0] for k in tx_fields), str(items[:1]))

    print("\n— Пополнение (создание платежей)")
    for kind in ("card", "sbp", "crypto"):
        st, data = request("POST", f"{cab}/topup", {"amount": 100, "method": kind}, token=token)
        ok = st == 200 and bool((data or {}).get("paymentUrl"))
        check(f"topup {kind}: paymentUrl", ok, f"got {st}: {str(data)[:200]}")

    st, data = request("POST", f"{cab}/topup", {"amount": -5, "method": "card"}, token=token)
    check("topup с отрицательной суммой → 400/422", st in (400, 422), f"got {st}")
    st, data = request("POST", f"{cab}/topup", {"amount": 100, "method": "paypal"}, token=token)
    check("topup с неизвестным методом → 400", st == 400, f"got {st}")

    print("\n— Устройства")
    st, data = request("POST", f"{cab}/device-binding-code", token=token)
    check("device-binding-code 200", st == 200, f"got {st}: {data}")
    code1 = (data or {}).get("code")
    check("binding-code: code+expiresAt+ttlHours", bool(code1) and "expiresAt" in (data or {}) and "ttlHours" in (data or {}), str(data))
    st, data = request("POST", f"{cab}/device-binding-code", token=token)
    check("binding-code: повторный вызов → тот же код", st == 200 and (data or {}).get("code") == code1, str(data))

    st, data = request("GET", f"{cab}/devices", token=token)
    check("GET /devices 200", st == 200 and isinstance((data or {}).get("devices"), list), f"got {st}: {data}")

    st, data = request("POST", f"{cab}/devices/reset", token=token)
    check("devices/reset 200", st == 200, f"got {st}: {data}")

    st, data = request("DELETE", f"{cab}/devices/nonexistent-hwid", token=token)
    check("DELETE несуществующего устройства → 4xx/502 без 500", st in (400, 404, 502), f"got {st}")

    print("\n— Рефералы")
    st, ref = request("GET", f"{cab}/referral", token=token)
    check("GET /referral 200", st == 200, f"got {st}: {ref}")
    ref_fields = ("code", "link", "invited", "earnedRub", "rewardPerFriendRub")
    check("referral: поля фронта", all(k in (ref or {}) for k in ref_fields), str(ref))
    ref_code = (ref or {}).get("code")

    st, data = request("GET", f"{cab}/referral/payouts", token=token)
    check("GET /referral/payouts 200", st == 200 and isinstance((data or {}).get("payouts"), list), f"got {st}: {data}")

    print("\n— Вход по коду")
    st, data = request("POST", f"{cab}/auth/login-code", {"code": ref_code})
    check("login-code по реферальному коду 200", st == 200 and bool((data or {}).get("token")), f"got {st}: {str(data)[:200]}")

    sub_code = (me or {}).get("subscriptionCode")
    if sub_code:
        st, data = request("POST", f"{cab}/auth/login-code", {"code": sub_code})
        check("login-code по коду подписки 200", st == 200, f"got {st}")
    else:
        print("  ⚠️ subscriptionCode пуст (нет remnawave short uuid) — пропуск")

    st, data = request("POST", f"{cab}/auth/login-code", {"code": "definitely-not-a-code"})
    check("login-code с мусором → 401", st == 401, f"got {st}")

    print("\n— Вход через Telegram initData")
    st, users = request("GET", f"{base}/users?limit=50", headers={"X-API-Key": admin_key})
    tg_id = None
    if st == 200:
        items = users.get("items") or users.get("users") or []
        for u in items:
            if u.get("telegram_id"):
                tg_id = u["telegram_id"]
                break
    if tg_id and bot_token:
        init_data = sign_init_data(bot_token, tg_id)
        st, data = request("POST", f"{cab}/auth/telegram", {"init_data": init_data})
        check("auth/telegram с валидной подписью 200", st == 200 and bool((data or {}).get("token")), f"got {st}: {str(data)[:200]}")
        st, data = request("POST", f"{cab}/auth/telegram", {"init_data": init_data.replace("hash=", "hash=dead")})
        check("auth/telegram с битой подписью → 401", st == 401, f"got {st}")
    else:
        print("  ⚠️ Нет telegram-пользователя в БД или BOT_TOKEN — пропуск")

    print("\n— Уведомления")
    st, data = request("GET", f"{cab}/notifications", token=token)
    check("GET /notifications 200", st == 200 and isinstance((data or {}).get("notifications"), list), f"got {st}: {data}")

    print(f"\n{'='*60}\nИтого: {len(PASSED)} ✅ / {len(FAILED)} ❌")
    for name, info in FAILED:
        print(f"  ❌ {name}: {info}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
