"""Senhas (scrypt da biblioteca padrão) e tokens (guardados só como hash SHA-256)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

from .db import now_iso

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
SESSION_DAYS = 30


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    b = lambda x: base64.b64encode(x).decode()
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b(salt)}${b(h)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _s, n, r, p, salt, h = stored.split("$")
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(h))
    except Exception:
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(conn, user_id: int, kind: str, name: str | None = None, days: int | None = None) -> str:
    token = ("gol_" if kind == "api" else "gos_") + secrets.token_urlsafe(32)
    exp = None
    if kind == "session":
        exp = (datetime.now(timezone.utc) + timedelta(days=days or SESSION_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("INSERT INTO auth_token(user_id, kind, name, token_hash, created_at, expires_at) VALUES (?,?,?,?,?,?)",
                 (user_id, kind, name, token_hash(token), now_iso(), exp))
    return token


def lookup_token(conn, token: str):
    r = conn.execute("SELECT t.id AS token_id, t.kind, t.expires_at, t.revoked_at, t.career AS career, u.* FROM auth_token t "
                     "JOIN user u ON u.id=t.user_id WHERE t.token_hash=?", (token_hash(token),)).fetchone()
    if not r or r["revoked_at"]:
        return None
    if r["expires_at"] and r["expires_at"] < now_iso():
        return None
    conn.execute("UPDATE auth_token SET last_used_at=? WHERE id=?", (now_iso(), r["token_id"]))
    return dict(r)


class LoginThrottle:
    """Bloqueio simples em memória: 5 falhas seguidas travam o usuário por 30 s."""

    def __init__(self, max_fail=5, lock_s=30):
        self.max_fail, self.lock_s = max_fail, lock_s
        self.fails: dict[str, tuple[int, float]] = {}

    def check(self, key: str) -> int:
        n, until = self.fails.get(key, (0, 0.0))
        if n >= self.max_fail and time.time() < until:
            return int(until - time.time()) + 1
        return 0

    def fail(self, key: str):
        n, _u = self.fails.get(key, (0, 0.0))
        self.fails[key] = (n + 1, time.time() + self.lock_s)

    def ok(self, key: str):
        self.fails.pop(key, None)
