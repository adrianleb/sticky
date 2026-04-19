from __future__ import annotations

import time

import jwt

ISSUER = "sticky-proxy"


def encode_token(secret: str, telegram_user_id: int) -> str:
    payload = {
        "iss": ISSUER,
        "sub": str(telegram_user_id),
        "iat": int(time.time()),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_token(secret: str, token: str) -> int:
    payload = jwt.decode(
        token, secret, algorithms=["HS256"], options={"require": ["iss", "sub"]}
    )
    if payload.get("iss") != ISSUER:
        raise jwt.InvalidTokenError("bad issuer")
    return int(payload["sub"])
