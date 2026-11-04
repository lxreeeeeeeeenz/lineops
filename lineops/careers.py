"""Carreiras: um modo por companhia (GOL, LATAM e Azul; companhias do mundo depois).

Onde fica cada coisa:
  dados/lineops.db          núcleo: o piloto (login), tokens, celular, Voos Externos, ajustes comuns
  dados/carreiras/GLO.db    carreira GOL: escala, frota, voos, diário, salário, equipe, tripulação, carreira
Cada carreira é um banco completo com o mesmo esquema, então todos os módulos funcionam igual em qualquer companhia: o
servidor só entrega a eles o banco da carreira escolhida no login. Nada se mistura entre companhias.

Ajustes comuns (VID da IVAO, Pilot ID do SimBrief, som do ACARS) ficam no núcleo e são copiados para a carreira quando ela
é aberta, porque os módulos leem dali.

Perfis de companhia: cor, frota típica, bases e a fonte da escala. Cores são usadas como tema; logotipo é um arquivo que
você mesmo coloca (o LineOps não traz logo de companhia nenhuma). A fonte da escala segue a regra do projeto: nada
inventado. GOL, LATAM e Azul vêm do AeroScale (planos de voo repetitivos do CGNA); companhias do mundo virão de fontes reais
(AeroScale e ADS-B observado) quando existirem.
"""
from __future__ import annotations

import re

import shutil
import sqlite3
from pathlib import Path

from .db import connect, migrate, now_iso, one, rows, tx
from .errors import Conflict, FGVError, NotFound

CORE = "lineops.db"
LEGACY = ("gol" "ops.db", "flygolv.db")          # bancos de antes das carreiras (viram a carreira GOL)
CAREERS_DIR = "carreiras"
DEFAULT = "GLO"
GLOBAL_SYNC = ("ivao_vid", "simbrief_userid", "simbrief_username", "acars_chime")
LOGO_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "svg": "image/svg+xml"}
LOGO_MAX = 600 * 1024

PROFILES = {
    "GLO": {"code": "GLO", "name": "GOL", "full": "GOL Linhas Aéreas", "iata": "G3", "callsign": "GOL", "country": "BR",
            "accent": "#FF6A13", "accent2": "#FFA15C", "fleet": ["B737", "B738", "B38M"], "hubs": ["SBGR", "SBGL", "SBBR", "SBCF", "SBSP"],
            "source": "aeroscale", "status": "ativo", "blurb": "Escala do AeroScale, 737 NG e MAX, base no Brasil."},
    "TAM": {"code": "TAM", "name": "LATAM", "full": "LATAM Airlines Brasil", "iata": "JJ", "callsign": "TAM", "country": "BR",
            "accent": "#E8114B", "accent2": "#FF6B93", "fleet": ["A319", "A320", "A20N", "A321", "A21N", "B789", "B77W"],
            "hubs": ["SBGR", "SBBR", "SBSP"], "source": "aeroscale", "status": "ativo",
            "blurb": "Escala do AeroScale, família A320 no Brasil; 787 e 777 nos internacionais vistos no ADS-B."},
    "AZU": {"code": "AZU", "name": "Azul", "full": "Azul Linhas Aéreas Brasileiras", "iata": "AD", "callsign": "AZUL", "country": "BR",
            "accent": "#1E90FF", "accent2": "#6CB8FF", "fleet": ["AT76", "E195", "E295", "A20N", "A21N", "A332", "A339"],
            "hubs": ["SBKP", "SBCF", "SBRF"], "source": "aeroscale", "status": "ativo",
            "blurb": "Escala do AeroScale: ATR 72, E-Jets E1 e E2 e A320neo no Brasil; hubs em Viracopos, Confins e Recife."},
    "WORLD": {"code": "WORLD", "name": "Mundo", "full": "Companhias do mundo", "iata": None, "callsign": None, "country": None,
              "accent": "#6FA8DC", "accent2": "#A9CBEB", "fleet": [], "hubs": [], "source": "adsb", "status": "em_breve",
              "blurb": "Escalas de companhias do mundo, de fontes reais (AeroScale e ADS-B)."},
}


PILOT_PREFIX = {"GLO": "GOL", "TAM": "TAM", "AZU": "AZU", "WORLD": "MUN"}


def pilot_code(stored: str | None, career: str | None) -> str:
    """Código do piloto na companhia da sessão. O banco guarda um só (padrão GOL001, de quando só existia a GOL); o número
    fica e o prefixo segue a companhia escolhida: GOL001 na GOL, TAM001 na LATAM, AZU001 na Azul."""
    m = re.match(r"^\s*([A-Za-z]*)\s*-?\s*(\d+)\s*$", stored or "")
    num = m.group(2) if m else "001"
    return PILOT_PREFIX.get((career or "").upper(), (career or "GOL").upper()[:3]) + num


def profile(code: str | None) -> dict:
    p = PROFILES.get((code or "").upper())
    if not p:
        raise NotFound("Companhia desconhecida.")
    return p


def disclaimer(code: str | None) -> str:
    p = PROFILES.get((code or "").upper()) or {}
    who = p.get("full") or "qualquer companhia aérea"
    return (f"LineOps é um projeto independente de simulação aérea, sem afiliação, patrocínio ou endosso da {who}. "
            "Todo conteúdo é simulado e não é documentação operacional.")


# ------------------------------------------------------------------ arquivos
def core_path(data_dir: Path) -> Path:
    return Path(data_dir) / CORE


def career_path(data_dir: Path, code: str) -> Path:
    return Path(data_dir) / CAREERS_DIR / f"{code.upper()}.db"


def career_files(data_dir: Path) -> list[Path]:
    d = Path(data_dir) / CAREERS_DIR
    return sorted(f for f in d.glob("*.db") if f.stem.upper() in PROFILES) if d.exists() else []


def all_db_files(data_dir: Path) -> list[Path]:
    c = core_path(data_dir)
    return ([c] if c.exists() else []) + career_files(data_dir)


def _move_db(src: Path, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dest)
    for ext in ("-wal", "-shm"):
        s = Path(str(src) + ext)
        if s.exists():
            s.replace(Path(str(dest) + ext))


def bootstrap(data_dir: Path) -> str | None:
    """Primeira execução da LineOps sobre dados antigos: o banco único vira a carreira GOL e o núcleo é montado a partir
    dele (piloto, tokens, ajustes comuns, Voos Externos). Devolve o nome do banco antigo adotado, se houve."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    core = core_path(data_dir)
    if core.exists():
        return None
    legacy = next((data_dir / n for n in LEGACY if (data_dir / n).exists()), None)
    if legacy:
        bk = data_dir / "backups"
        bk.mkdir(parents=True, exist_ok=True)
        keep = bk / f"antes-da-lineops-{legacy.name}"
        if not keep.exists():
            src = sqlite3.connect(legacy)                     # cópia consistente do banco antigo, antes de qualquer mudança
            dst = sqlite3.connect(keep)
            try:
                with dst:
                    src.backup(dst)
            finally:
                src.close(); dst.close()
        _move_db(legacy, career_path(data_dir, DEFAULT))
        old_store = data_dir / "aeroscale"                  # cópia do banco do AeroScale usada pelas regras (GolOps)
        new_store = data_dir / CAREERS_DIR / f"{DEFAULT}.aeroscale"
        if old_store.is_dir() and not new_store.exists():
            shutil.copytree(old_store, new_store)
    tmp = core.with_name(core.name + ".tmp")
    for x in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        x.unlink(missing_ok=True)
    c = connect(tmp)                                          # o núcleo só vira lineops.db se tudo der certo
    try:
        migrate(c)
        if legacy:
            g = connect(career_path(data_dir, DEFAULT))
            migrate(g)
            g.close()
            c.execute("ATTACH DATABASE ? AS old", (str(career_path(data_dir, DEFAULT)),))
            with tx(c):
                c.execute("INSERT OR IGNORE INTO user SELECT * FROM old.user")
                cols = [r[1] for r in c.execute("PRAGMA old.table_info(auth_token)")]
                c.execute(f"INSERT OR IGNORE INTO auth_token({','.join(cols)}) SELECT {','.join(cols)} FROM old.auth_token")
                c.execute("UPDATE auth_token SET career=? WHERE career IS NULL", (DEFAULT,))
                c.execute("INSERT OR IGNORE INTO setting SELECT * FROM old.setting WHERE key IN (%s)" % ",".join("?" * len(_core_keys())), _core_keys())
                for t in ("ext_seen", "ext_flight", "ext_member"):
                    c.execute(f"INSERT OR IGNORE INTO {t} SELECT * FROM old.{t}")
                c.execute("INSERT OR IGNORE INTO career(code, created_at, last_used_at) VALUES (?,?,?)", (DEFAULT, now_iso(), now_iso()))
            c.execute("DETACH DATABASE old")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        c.close()
    _move_db(tmp, core)
    return legacy.name if legacy else None


def _core_keys():
    from .notify import KEYS
    return tuple(GLOBAL_SYNC) + tuple(k for k in KEYS if k != "notify_cursor")


def ensure_career(data_dir: Path, core, code: str) -> Path:
    """Cria (se preciso) e atualiza o banco da carreira; espelha o piloto do núcleo (as tabelas da carreira apontam para ele)."""
    code = code.upper()
    profile(code)
    path = career_path(data_dir, code)
    path.parent.mkdir(parents=True, exist_ok=True)
    c = connect(path)
    try:
        migrate(c)
        mirror_users(core, c)
        with tx(c):
            c.execute("INSERT INTO setting(key, value) VALUES ('career_code', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (code,))
        sync_settings(core, c)
    finally:
        c.close()
    with tx(core):
        core.execute("INSERT OR IGNORE INTO career(code, created_at) VALUES (?,?)", (code, now_iso()))
    return path


def mirror_users(core, c) -> None:
    us = rows(core.execute("SELECT * FROM user"))
    if not us:
        return
    with tx(c):
        for u in us:
            cols = list(u)
            c.execute(f"INSERT INTO user({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
                      f"ON CONFLICT(id) DO UPDATE SET {','.join(f'{k}=excluded.{k}' for k in cols if k != 'id')}", [u[k] for k in cols])


def sync_settings(core, c) -> None:
    """Ajustes comuns do núcleo → carreira (os módulos leem da carreira)."""
    vals = {r["key"]: r["value"] for r in core.execute(f"SELECT key, value FROM setting WHERE key IN ({','.join('?' * len(GLOBAL_SYNC))})", GLOBAL_SYNC)}
    cur = {r["key"]: r["value"] for r in c.execute(f"SELECT key, value FROM setting WHERE key IN ({','.join('?' * len(GLOBAL_SYNC))})", GLOBAL_SYNC)}
    if vals == cur:
        return
    with tx(c):
        for k in GLOBAL_SYNC:
            if k in vals:
                c.execute("INSERT INTO setting(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, vals[k]))
            else:
                c.execute("DELETE FROM setting WHERE key=?", (k,))


# ------------------------------------------------------------------ escolha
def list_careers(data_dir: Path, core) -> list[dict]:
    have = {r["code"]: r for r in rows(core.execute("SELECT * FROM career"))}
    out = []
    for code, p in PROFILES.items():
        r = have.get(code)
        item = {**p, "created": bool(r), "last_used_at": r["last_used_at"] if r else None, "has_logo": bool(r and r["logo"]), "stats": None}
        path = career_path(data_dir, code)
        if r and path.exists():
            c = connect(path)
            try:
                s = c.execute("SELECT COUNT(*) n, COALESCE(SUM(block_min),0) b FROM logbook_entry WHERE reversed_at IS NULL").fetchone()
                item["stats"] = {"flights": s["n"], "block_min": s["b"], "legs": c.execute("SELECT COUNT(*) FROM roster_leg WHERE removed_batch IS NULL").fetchone()[0]}
            finally:
                c.close()
        out.append(item)
    return out


def select(data_dir: Path, core, token_id: int, code: str) -> dict:
    code = (code or "").upper()
    p = profile(code)
    if p["status"] != "ativo":
        raise Conflict(f"O modo {p['name']} ainda não está pronto.", code="modo_indisponivel")
    ensure_career(data_dir, core, code)
    with tx(core):
        core.execute("UPDATE auth_token SET career=? WHERE id=?", (code, token_id))
        core.execute("UPDATE career SET last_used_at=? WHERE code=?", (now_iso(), code))
        core.execute("INSERT INTO setting(key, value) VALUES ('active_career', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (code,))
    return p


def active(core) -> str:
    r = core.execute("SELECT value FROM setting WHERE key='active_career'").fetchone()
    return (r[0] if r else None) or DEFAULT


def last_used(core) -> str | None:
    r = one(core.execute("SELECT code FROM career WHERE last_used_at IS NOT NULL ORDER BY last_used_at DESC LIMIT 1"))
    return r["code"] if r else None


# ------------------------------------------------------------------ logo (arquivo seu)
def save_logo(data_dir: Path, core, code: str, filename: str, data: bytes) -> dict:
    code = profile(code)["code"]
    ext = (filename or "").rsplit(".", 1)[-1].lower()
    if ext not in LOGO_TYPES:
        raise FGVError("Logo em PNG, JPG, WEBP ou SVG.", code="formato_invalido")
    if not data or len(data) > LOGO_MAX:
        raise FGVError("Logo vazio ou maior que 600 KB.", code="arquivo_grande")
    if ext == "svg" and (b"<script" in data.lower() or b"javascript:" in data.lower()):
        raise FGVError("SVG com script não é aceito.", code="formato_invalido")
    d = Path(data_dir) / CAREERS_DIR
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob(f"{code}.logo.*"):
        old.unlink()
    (d / f"{code}.logo.{ext}").write_bytes(data)
    with tx(core):
        core.execute("INSERT OR IGNORE INTO career(code, created_at) VALUES (?,?)", (code, now_iso()))
        core.execute("UPDATE career SET logo=? WHERE code=?", (f"{code}.logo.{ext}", code))
    return {"ok": True}


def remove_logo(data_dir: Path, core, code: str) -> dict:
    code = profile(code)["code"]
    for old in (Path(data_dir) / CAREERS_DIR).glob(f"{code}.logo.*"):
        old.unlink()
    with tx(core):
        core.execute("UPDATE career SET logo=NULL WHERE code=?", (code,))
    return {"ok": True}


def logo_file(data_dir: Path, core, code: str) -> tuple[Path, str] | None:
    r = one(core.execute("SELECT logo FROM career WHERE code=?", ((code or "").upper(),)))
    if not r or not r["logo"]:
        return None
    f = Path(data_dir) / CAREERS_DIR / r["logo"]
    return (f, LOGO_TYPES[f.suffix[1:].lower()]) if f.exists() else None


def code_of(conn) -> str:
    """Companhia da carreira deste banco (o banco de carreira guarda o próprio código)."""
    r = conn.execute("SELECT value FROM setting WHERE key='career_code'").fetchone()
    return (r[0] if r else None) or DEFAULT


def sync_settings_path(core_path: Path, c) -> None:
    core = connect(core_path)
    try:
        sync_settings(core, c)
    finally:
        core.close()
