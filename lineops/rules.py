"""Ponte entre o LineOps e as regras do AeroScale (pasta lineops/aeroscale, cópia literal).

Toda decisão de escala — fuso e dados do aeroporto, família de tipo, classificação de solo e pernoite,
jornada e limites de tripulação, rodízio observado, atrasos — sai daqui, nunca de uma regra reescrita.

Os módulos do AeroScale consultam o banco dele. Por isso o importador guarda uma cópia somente-leitura do
banco do AeroScale em <dados>/aeroscale/source.db a cada importação, e é contra ela que eles rodam.
Sem essa cópia, o que depende só de dados embutidos (aeroportos, tipos, regras de tripulação) continua
funcionando; o que depende do RPL ou do ADS-B fica indisponível e o LineOps diz isso.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path

from .aeroscale import SOURCE_VERSION, catalog, crew, crew_profiles, delays, intl, ops, predict
from .aeroscale import simbrief as simbrief_mod
from .aeroscale import weather as weather_mod
from .aeroscale.airports import offset_label, to_local

_LOCK = threading.Lock()
_CACHE: dict = {}
STORE_DIR = "aeroscale"
SOURCE_NAME = "source.db"


# ------------------------------------------------------------------ cópia do banco do AeroScale
def store_dir(conn) -> Path | None:
    """Pasta de dados do LineOps, deduzida do próprio banco aberto."""
    r = conn.execute("PRAGMA database_list").fetchone()
    p = r[2] if r and r[2] else None
    if not p:
        return None
    p = Path(p)
    # carreira (dados/carreiras/GLO.db): cada companhia tem a sua cópia (dados/carreiras/GLO.aeroscale/)
    return (p.parent / f"{p.stem}.{STORE_DIR}") if p.parent.name == "carreiras" else (p.parent / STORE_DIR)


def source_path(conn) -> Path | None:
    d = store_dir(conn)
    p = (d / SOURCE_NAME) if d else None
    return p if p and p.exists() else None


def save_source(conn, db_file: Path) -> Path | None:
    """Guarda a cópia do banco do AeroScale usada na última importação."""
    d = store_dir(conn)
    if not d:
        return None
    d.mkdir(parents=True, exist_ok=True)
    dest, tmp = d / SOURCE_NAME, d / (SOURCE_NAME + ".tmp")
    src = sqlite3.connect(str(db_file))
    dst = sqlite3.connect(str(tmp))
    try:
        with dst:
            src.backup(dst)                   # cópia consistente, sem WAL solto
    finally:
        src.close()
        dst.close()
    from .fsutil import replace_retry
    replace_retry(tmp, dest)                  # no Windows, espera quem estiver lendo a cópia antiga
    _CACHE.clear()
    return dest


def _open(conn) -> sqlite3.Connection:
    p = source_path(conn)
    if p:
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
    else:
        c = sqlite3.connect(":memory:")       # sem cópia: só os dados embutidos do AeroScale
        c.executescript("CREATE TABLE airports(icao TEXT PRIMARY KEY, iata TEXT, name TEXT, city TEXT, uf TEXT, tz TEXT);"
                        "CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);")
    c.row_factory = sqlite3.Row
    return c


def has_source(conn) -> bool:
    return source_path(conn) is not None


def _key(conn, name: str):
    p = source_path(conn)
    return (name, str(p) if p else "", p.stat().st_mtime_ns if p else 0)


# ------------------------------------------------------------------ aeroportos (catalog + world_tz do AeroScale)
def airport_map(conn) -> dict[str, dict]:
    k = _key(conn, "apmap")
    with _LOCK:
        hit = _CACHE.get(k)
    if hit is not None:
        return hit
    c = _open(conn)
    try:
        m = catalog.airport_map(c)
    finally:
        c.close()
    with _LOCK:
        _CACHE.clear()
        _CACHE[k] = m
    return m


def airport(conn, icao: str | None) -> dict | None:
    """Aeroporto como o AeroScale o conhece; fora do mapa, cai no catálogo mundial dele (intl.airport_info)."""
    if not icao:
        return None
    icao = icao.upper()
    a = airport_map(conn).get(icao)
    return a or intl.airport_info(icao)


def airport_tz(conn, icao: str | None) -> str | None:
    a = airport(conn, icao)
    return a.get("tz") if a and a.get("tz") and catalog.valid_tz(a["tz"]) else None


def tz_of(conn):
    """Callable tz_of(icao) no formato que crew.analyze e predict esperam."""
    return lambda icao: airport_tz(conn, icao)


def local_dt(conn, dt: datetime | None, icao: str | None) -> datetime | None:
    tz = airport_tz(conn, icao)
    if not dt or not tz:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return to_local(dt, tz)


def local_date(conn, dt: datetime, icao: str | None) -> str:
    """Data local da partida: é assim que o AeroScale decide o dia da perna (api.leg_brief)."""
    return (local_dt(conn, dt, icao) or dt).strftime("%Y-%m-%d")


def local_label(conn, dt: datetime | None, icao: str | None) -> dict | None:
    loc = local_dt(conn, dt, icao)
    if not loc:
        return None
    return {"hhmm": loc.strftime("%H:%M"), "date": loc.strftime("%Y-%m-%d"), "tz": str(loc.tzinfo), "offset": offset_label(loc)}


# ------------------------------------------------------------------ tipos (catalog do AeroScale)
def type_name(icao: str | None) -> str | None:
    return catalog.type_name(icao) if icao else None


def type_family(icao: str | None) -> str | None:
    return catalog.type_family(icao)


def types_compatible(a: str | None, b: str | None, mode: str = "family") -> bool:
    return catalog.types_compatible(a, b, mode)


# ------------------------------------------------------------------ solo e pernoite (predict do AeroScale)
GAP_LABEL = predict.GAP_LABEL
OVERNIGHT_KINDS = ("pernoite", "parada_longa", "parada_noturna_curta")


def gap_kind(conn, arr: datetime, dep: datetime, icao: str | None, min_turn: int = 0) -> str:
    return predict.gap_kind(arr, dep, icao, tz_of(conn), min_turn)


# ------------------------------------------------------------------ tripulação (crew do AeroScale)
def crew_report(conn, legs: list[dict], params: dict, base: str | None, family: str | None,
                period: tuple[date, date] | None = None, reserve_days: set | None = None) -> dict:
    """crew.analyze do AeroScale: jornadas, repousos, folgas, madrugadas, limites e violações."""
    return crew.analyze(legs, params, tz_of(conn), base, family, period, reserve_days)


def crew_codes(airline: str) -> dict:
    return crew_profiles.codes(airline)


def crew_profile(airline: str) -> dict:
    """Perfil da companhia no AeroScale: códigos de folga/reserva, apresentação padrão, preferências."""
    return crew_profiles.profile(airline)


# ------------------------------------------------------------------ operação observada (ops/delays do AeroScale)
def night_stock(conn, airline: str) -> dict:
    """Aeroportos onde a companhia deixa aeronave à noite (RPL + ADS-B), como o AeroScale mede."""
    c = _open(conn)
    try:
        return ops.rpl_stock_avg(c, airline)
    except Exception:
        return {}
    finally:
        c.close()


def delays_summary(conn) -> dict:
    """Pontualidade observada (delays.summary do AeroScale)."""
    c = _open(conn)
    try:
        return delays.summary(c)
    except Exception:
        return {}
    finally:
        c.close()


def ops_summary(conn, airline: str | None = None) -> dict:
    """Pernoites e rodízio observados (ops.summary do AeroScale)."""
    c = _open(conn)
    try:
        return ops.summary(c, airline)
    except Exception:
        return {}
    finally:
        c.close()


def simbrief(conn, leg: dict, aircraft: dict | None = None) -> dict:
    """URL e campos do SimBrief montados pelo próprio AeroScale (rota e PBN do RPL, alternado sugerido)."""
    c = _open(conn)
    try:
        return simbrief_mod.build(c, leg, aircraft)
    except Exception as e:
        return {"url": None, "error": str(e), "params": {}, "notes": ["Sem a cópia do banco do AeroScale não dá para montar o link."]}
    finally:
        c.close()


# ------------------------------------------------------------------ meteorologia (weather do AeroScale)
def weather(conn, icaos: list[str], *, force: bool = False, metar_ttl: int = 10, taf_ttl: int = 30) -> dict:
    """METAR e TAF pelo weather.py do AeroScale (aviationweather.gov, sem chave), com o cache no banco do LineOps.

    Quem busca é o servidor, no seu computador. Sem internet, devolve o último cache marcado como velho.
    """
    icaos = [i for i in dict.fromkeys((i or "").strip().upper() for i in icaos) if len(i) == 4]
    if not icaos:
        return {"metars": [], "tafs": []}
    metars = weather_mod.get_metars(conn, icaos, ttl_min=metar_ttl, force=force)
    tafs = weather_mod.get_tafs(conn, icaos, ttl_min=taf_ttl, force=force)
    for m in metars:                       # o módulo já devolve decodificado em m["metar"]
        d = m.get("metar") or {}
        m["raw"] = d.get("raw")
        m["category"] = weather_mod.flight_category(d) if d else None
        m["alerts"] = weather_mod.metar_alerts(d) if d else []
    for t in tafs:
        raw = (t.get("taf") or {}).get("raw") if isinstance(t.get("taf"), dict) else t.get("taf")
        t["raw"] = raw
        t["periods"] = weather_mod.split_taf(raw or "")
    return {"metars": metars, "tafs": tafs, "source": "aviationweather.gov (NOAA/AWC) pelo weather.py do AeroScale"}


def source_info(conn) -> dict:
    p = source_path(conn)
    return {"version": SOURCE_VERSION, "has_source_db": bool(p),
            "source_db": str(p) if p else None,
            "size": p.stat().st_size if p else None}
