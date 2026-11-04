"""Cadastros de apoio: aeroportos, companhias, tipos/famílias de aeronave."""
from __future__ import annotations

from .airport_geo import AIRPORT_GEO
from .airports import BUILTIN_AIRPORTS, valid_tz
from .db import now_iso, rows

# Designador ICAO -> (nome comercial, família). Usado só para rotular e agrupar;
# a compatibilidade "exata" sempre compara o designador.
TYPE_INFO = {
    "A318": ("Airbus A318", "A320"), "A319": ("Airbus A319", "A320"),
    "A320": ("Airbus A320", "A320"), "A321": ("Airbus A321", "A320"),
    "A19N": ("Airbus A319neo", "A320"), "A20N": ("Airbus A320neo", "A320"),
    "A21N": ("Airbus A321neo", "A320"),
    "A332": ("Airbus A330-200", "A330"), "A333": ("Airbus A330-300", "A330"),
    "A339": ("Airbus A330-900neo", "A330"), "A359": ("Airbus A350-900", "A350"),
    "B737": ("Boeing 737-700", "B737"), "B738": ("Boeing 737-800", "B737"),
    "B739": ("Boeing 737-900", "B737"), "B37M": ("Boeing 737 MAX 7", "B737"),
    "B38M": ("Boeing 737 MAX 8", "B737"), "B39M": ("Boeing 737 MAX 9", "B737"),
    "B763": ("Boeing 767-300", "B767"), "B77W": ("Boeing 777-300ER", "B777"),
    "B788": ("Boeing 787-8", "B787"), "B789": ("Boeing 787-9", "B787"),
    "B78X": ("Boeing 787-10", "B787"),
    "E190": ("Embraer E190", "E-JET"), "E195": ("Embraer E195", "E-JET"),
    "E290": ("Embraer E190-E2", "E-JET"), "E295": ("Embraer E195-E2", "E-JET"),
    "AT72": ("ATR 72", "ATR"), "AT75": ("ATR 72-500", "ATR"), "AT76": ("ATR 72-600", "ATR"),
    "C208": ("Cessna 208 Caravan", "C208"),
}


def type_name(icao: str) -> str:
    return TYPE_INFO.get(icao, (icao, icao))[0]


def type_family(icao: str | None) -> str | None:
    if not icao:
        return None
    return TYPE_INFO.get(icao, (icao, icao))[1]


def types_compatible(aircraft_type: str | None, flight_type: str | None, mode: str = "exact") -> bool:
    if not aircraft_type or not flight_type:
        return False
    if aircraft_type == flight_type:
        return True
    if mode == "family":
        return type_family(aircraft_type) == type_family(flight_type)
    return False


# ---------------------------------------------------------------- aeroportos
def airport_map(db) -> dict[str, dict]:
    out = {}
    for icao, (iata, name, city, uf, tz) in BUILTIN_AIRPORTS.items():
        out[icao] = {"icao": icao, "iata": iata, "name": name, "city": city, "uf": uf, "tz": tz,
                     "source": "embutido"}
    for r in db.execute("SELECT * FROM airports"):
        d = dict(r)
        d["source"] = "manual"
        out[d["icao"]] = d
    try:  # aeroportos estrangeiros dos voos internacionais observados
        from .intl import foreign_airports
        for icao, info in foreign_airports(db).items():
            out.setdefault(icao, info)
    except Exception:
        pass
    for icao, d in out.items():
        g = AIRPORT_GEO.get(icao)
        if g:
            d.update(lat=g[0], lon=g[1], elev_ft=g[2], rwy_m=g[3])
    return out


def airport_tz(apmap: dict, icao: str) -> str | None:
    a = apmap.get(icao)
    if a and a.get("tz") and valid_tz(a["tz"]):
        return a["tz"]
    return None


def upsert_airport(db, data: dict) -> dict:
    icao = (data.get("icao") or "").strip().upper()
    if len(icao) != 4 or not icao.isalpha():
        raise ValueError("Código ICAO deve ter 4 letras.")
    tz = (data.get("tz") or "").strip()
    if tz and not valid_tz(tz):
        raise ValueError(f"Fuso horário IANA desconhecido: {tz} (ex.: America/Sao_Paulo).")
    db.execute(
        "INSERT OR REPLACE INTO airports(icao, iata, name, city, uf, tz, updated_at) VALUES (?,?,?,?,?,?,?)",
        (icao, (data.get("iata") or "").upper()[:3], data.get("name") or "", data.get("city") or "",
         (data.get("uf") or "").upper()[:2], tz, now_iso()),
    )
    return {"icao": icao}


# ---------------------------------------------------------------- companhias
def ensure_airline(db, code: str) -> bool:
    """Garante a companhia; cria marcada para revisão se for nova. Retorna True se criou."""
    if db.execute("SELECT 1 FROM airlines WHERE code=?", (code,)).fetchone():
        return False
    db.execute(
        "INSERT INTO airlines(code, iata, name, color, needs_review, created_at) VALUES (?,?,?,?,1,?)",
        (code, "", f"{code} (nova companhia – revisar nome)", "#9AA7B5", now_iso()),
    )
    return True


def airlines(db) -> list[dict]:
    return rows(db.execute("SELECT * FROM airlines ORDER BY code"))


def ensure_model(db, airline: str, icao_type: str, source="rpl") -> int:
    r = db.execute("SELECT id FROM aircraft_models WHERE airline=? AND icao_type=? ORDER BY id LIMIT 1",
                   (airline, icao_type)).fetchone()
    if r:
        return r["id"]
    cur = db.execute(
        "INSERT INTO aircraft_models(airline, name, icao_type, family, source, created_at) VALUES (?,?,?,?,?,?)",
        (airline, type_name(icao_type), icao_type, type_family(icao_type), source, now_iso()),
    )
    return cur.lastrowid


# Aeroportos da mesma cidade: tripulante baseado na cidade cobre os dois (deslocamento por terra)
SAME_CITY = [{"SBGR", "SBSP"}, {"SBGL", "SBRJ"}, {"SBCF", "SBBH"}]


def same_city(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    return any(a in g and b in g for g in SAME_CITY)
