"""Redefinir a senha esquecida, sem apagar nada.

A LineOps não tem senha padrão: cada instalação cria o usuário no primeiro acesso, e o banco guarda só o hash (scrypt).
Por isso a senha não se recupera, se troca. Dois caminhos, os dois só no próprio PC:

* Na tela de entrada, "Esqueci a senha": a LineOps grava um código de 6 dígitos no arquivo REDEFINIR-SENHA.txt da pasta
  de dados (e abre o arquivo no Windows). Quem consegue ler esse arquivo está no computador da instalação. O código vale
  10 minutos e 5 tentativas. Pedido vindo da rede (modo LAN) é recusado.
* Pela linha de comando: `python -m lineops redefinir-senha` (ou redefinir-senha.bat), com a LineOps aberta ou fechada.

Trocar a senha encerra as sessões abertas desse usuário (o token do ACARS continua valendo: ele não usa senha).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import security
from .db import audit, now_iso, one, rows, tx
from .errors import FGVError, NotFound

KEY = "senha_reset"
FILE = "REDEFINIR-SENHA.txt"
VALID_MIN = 10
MAX_TRIES = 5
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def usernames(core) -> list[str]:
    return [r["username"] for r in rows(core.execute("SELECT username FROM user ORDER BY id"))]


def reset_password(core, username: str, new_password: str, *, via: str = "cli") -> dict:
    username = (username or "").strip()
    if not (8 <= len(new_password or "") <= 200):
        raise FGVError("A senha nova precisa de 8 a 200 caracteres.", code="senha_curta")
    u = one(core.execute("SELECT id, username FROM user WHERE username=? COLLATE NOCASE", (username,)))
    if not u:
        raise NotFound(f"Não existe o usuário {username or '(vazio)'} nesta instalação.")
    with tx(core):
        core.execute("UPDATE user SET pw_hash=? WHERE id=?", (security.hash_password(new_password), u["id"]))
        n = core.execute("UPDATE auth_token SET revoked_at=? WHERE user_id=? AND kind='session' AND revoked_at IS NULL",
                         (now_iso(), u["id"])).rowcount
        audit(core, u["id"], "user.senha_redefinida", "user", u["id"], {"via": via, "sessoes_encerradas": n})
    return {"ok": True, "username": u["username"], "sessions_closed": n}


def is_local(host: str | None) -> bool:
    return (host or "") in LOCAL_HOSTS


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def start_local(core, data_dir: Path, *, open_file: bool = True) -> dict:
    """Gera o código e grava no arquivo da pasta de dados."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    exp = _now() + timedelta(minutes=VALID_MIN)
    with tx(core):
        core.execute("INSERT INTO setting(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (KEY, json.dumps({"hash": _hash(code), "exp": exp.strftime("%Y-%m-%dT%H:%M:%SZ"), "tries": 0})))
    f = Path(data_dir) / FILE
    local = exp.astimezone()
    f.write_text("LineOps: código para redefinir a senha\n\n"
                 f"    {code}\n\n"
                 f"Vale até {local:%H:%M} de {local:%d/%m/%Y} (10 minutos) e só neste computador.\n"
                 "Digite o código na tela \"Esqueci a senha\" da LineOps. Se não foi você que pediu, apague este arquivo:\n"
                 "sem o código ninguém troca a senha.\n", encoding="utf-8")
    if open_file and sys.platform == "win32":
        try:
            os.startfile(str(f))                      # noqa: S606 — abre no Bloco de Notas, no PC da própria instalação
        except OSError:
            pass
    return {"ok": True, "file": str(f), "valid_min": VALID_MIN, "users": len(usernames(core))}


def finish_local(core, data_dir: Path, username: str, code: str, new_password: str) -> dict:
    r = one(core.execute("SELECT value FROM setting WHERE key=?", (KEY,)))
    st = json.loads(r["value"]) if r else None
    if not st:
        raise FGVError("Peça um código primeiro (botão “Gerar código”).", code="sem_codigo")
    if st["exp"] < _now().strftime("%Y-%m-%dT%H:%M:%SZ") or st["tries"] >= MAX_TRIES:
        _clear(core, data_dir)
        raise FGVError("O código venceu (10 minutos ou 5 tentativas). Gere outro.", code="codigo_vencido")
    if not hmac.compare_digest(st["hash"], _hash((code or "").strip())):
        st["tries"] += 1
        with tx(core):
            core.execute("UPDATE setting SET value=? WHERE key=?", (json.dumps(st), KEY))
        raise FGVError(f"Código errado ({MAX_TRIES - st['tries']} tentativa(s) restante(s)).", code="codigo_errado")
    if not one(core.execute("SELECT 1 FROM user WHERE username=? COLLATE NOCASE", ((username or "").strip(),))):
        # o código já provou que é o dono deste PC: aqui pode dizer quais usuários existem (o código continua valendo)
        names = usernames(core)
        raise NotFound(f"Não existe o usuário {(username or '').strip() or '(vazio)'} nesta instalação. "
                       + (f"Usuário(s) deste computador: {', '.join(names)}. " if names else "")
                       + "Cada LineOps tem o próprio usuário: o de outro PC não existe aqui.")
    out = reset_password(core, username, new_password, via="codigo_local")
    _clear(core, data_dir)
    return out


def _clear(core, data_dir: Path) -> None:
    with tx(core):
        core.execute("DELETE FROM setting WHERE key=?", (KEY,))
    try:
        (Path(data_dir) / FILE).unlink()
    except OSError:
        pass
