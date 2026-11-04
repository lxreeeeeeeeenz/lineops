"""Foto de perfil fixa por usuário (pedido do dono da instalação): por enquanto só a do cmdt079, que pediu a dele.
A foto mora em lineops/assets/avatars/ e só sai pela API para o próprio usuário (GET /api/me/avatar)."""
from __future__ import annotations

from pathlib import Path

AVATARS = {"cmdt079": "cmdt079.jpg"}


def of(user: dict | None) -> Path | None:
    name = AVATARS.get(((user or {}).get("username") or "").strip().lower())
    if not name:
        return None
    f = Path(__file__).parent / "assets" / "avatars" / name        # no .exe, o PyInstaller põe lineops/assets no mesmo lugar
    return f if f.is_file() else None
