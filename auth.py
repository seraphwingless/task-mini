"""Проверка подписи Telegram WebApp initData (owner-only).

Telegram передаёт Mini App строку initData, подписанную секретом бота.
Проверяем подпись HMAC-SHA256 и что открыл именно владелец (OWNER_ID).
Так даже если кто-то узнает URL — без валидной подписи для нашего бота
и с чужим id доступа к данным не будет.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class AuthError(Exception):
    pass


def verify_init_data(init_data: str, bot_token: str, owner_id: int,
                     max_age_sec: int = 86400) -> dict:
    """Возвращает данные пользователя, если подпись валидна и это владелец.
    Иначе бросает AuthError."""
    if not init_data:
        raise AuthError("no init data")

    pairs = dict(parse_qsl(init_data, strict_parsing=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise AuthError("no hash")

    # Строка проверки: отсортированные key=value через \n.
    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))

    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calc_hash, received_hash):
        raise AuthError("bad signature")

    # Защита от повторного использования старых данных.
    auth_date = int(pairs.get("auth_date", "0"))
    if max_age_sec and (time.time() - auth_date) > max_age_sec:
        raise AuthError("init data expired")

    user_raw = pairs.get("user")
    if not user_raw:
        raise AuthError("no user")
    user = json.loads(user_raw)

    if int(user.get("id", 0)) != int(owner_id):
        raise AuthError("not the owner")

    return user
