"""Foto de perfil.

* Qualquer usuário pode enviar a sua (Ajustes › Perfil): JPEG, PNG ou WebP até 2 MB, guardada na pasta de dados
  (dados/avatars/<id>.<ext>), fora do programa: atualizar a LineOps não apaga.
* Sem foto enviada, vale a fixa do programa, se houver (lineops/assets/avatars/): hoje só a do cmdt079, que pediu a dele.
A foto só sai pela API para o próprio usuário (GET /api/me/avatar).
"""
from __future__ import annotations

from pathlib import Path

from .errors import FGVError

AVATARS = {"cmdt079": "cmdt079.jpg"}
MAX_BYTES = 2 * 1024 * 1024
KINDS = {b"\xff\xd8\xff": ("jpg", "image/jpeg"), b"\x89PNG\r\n\x1a\n": ("png", "image/png")}
TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def _kind(data: bytes) -> str | None:
    for magic, (ext, _t) in KINDS.items():
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _uploaded(user: dict, data_dir: Path | None) -> Path | None:
    if not data_dir or user.get("id") is None:
        return None
    for ext in TYPES:
        f = Path(data_dir) / "avatars" / f"{int(user['id'])}.{ext}"
        if f.is_file():
            return f
    return None


def of(user: dict | None, data_dir: Path | None = None) -> Path | None:
    user = user or {}
    f = _uploaded(user, data_dir)
    if f:
        return f
    name = AVATARS.get((user.get("username") or "").strip().lower())
    if not name:
        return None
    f = Path(__file__).parent / "assets" / "avatars" / name        # no .exe, o PyInstaller põe lineops/assets no mesmo lugar
    return f if f.is_file() else None


def media_type(f: Path) -> str:
    return TYPES.get(f.suffix.lstrip(".").lower(), "application/octet-stream")


def save(user: dict, data_dir: Path, data: bytes) -> Path:
    if not data or len(data) > MAX_BYTES:
        raise FGVError("A foto precisa ter até 2 MB.", code="foto_grande")
    ext = _kind(data)
    if not ext:
        raise FGVError("Formato não reconhecido: use JPEG, PNG ou WebP.", code="foto_formato")
    remove(user, data_dir)
    d = Path(data_dir) / "avatars"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{int(user['id'])}.{ext}"
    f.write_bytes(data)
    return f


def remove(user: dict, data_dir: Path) -> bool:
    gone = False
    for ext in TYPES:
        f = Path(data_dir) / "avatars" / f"{int(user['id'])}.{ext}"
        if f.is_file():
            f.unlink()
            gone = True
    return gone
