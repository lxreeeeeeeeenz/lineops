"""Voos Externos (fora das OPS): long haul de verdade para voar com os amigos.

De onde vem o voo: do ADS-B. O LineOps pergunta ao adsb.lol (API pública, dados ODbL) quais long-haulers estão no ar agora
(777, 787, A330, A350, A340, 747, A380, MD-11, 767) e, para cada callsign, qual é a rota (endpoint routeset do adsb.lol,
que vem da base comunitária de rotas por callsign e marca se a posição do avião é plausível para aquela rota). Só entram
rotas plausíveis, de um trecho só, com distância de long haul. Cada busca alimenta um catálogo local de voos reais vistos;
o gerador sorteia desse catálogo. Nada é inventado: o horário de partida do real é ESTIMADO pela distância já voada, e a
duração do voo de vocês é ESTIMADA pela velocidade de cruzeiro típica do tipo (as duas coisas aparecem como estimativa).

Como vocês voam: cada um no seu avião, na IVAO ou na VATSIM, com saídas escalonadas (padrão 3 min, separação típica
de esteira entre pesados). O LineOps monta o convite para mandar no WhatsApp/Discord, o link do SimBrief de cada um, o
tempo nas duas pontas e, no dia, o radar do grupo (acha cada callsign no whazzup da IVAO ou no data feed da VATSIM).
Depois, cada um lança o seu toque e o LineOps monta o pódio. Nada disso mexe na escala, no diário da GOL ou nos salários.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import __version__, geo, rules
from .db import audit, now_iso, one, rows, tx
from .errors import Conflict, FGVError, NotFound

log = logging.getLogger("lineops.externos")
FMT = "%Y-%m-%dT%H:%M:%SZ"
ADSB_TYPE_URL = "https://api.adsb.lol/v2/type/{t}"
ADSB_ROUTESET_URL = "https://api.adsb.lol/api/0/routeset"
VATSIM_URL = "https://data.vatsim.net/v3/vatsim-data.json"
UA = f"LineOps/{__version__} (virtual airline pessoal, self-hosted)"
ATTRIBUTION = "Voos e rotas: adsb.lol (dados ODbL; rotas da base comunitária por callsign)."
MIN_KM = 5500                     # ~7 h de voo: daqui para cima é long haul
CACHE_S = 600
TYPES = {   # tipo ICAO: (nome, família, cruzeiro típico em kt TAS)
    "B772": ("Boeing 777-200ER", "777", 485), "B77L": ("Boeing 777-200LR/F", "777", 485), "B77W": ("Boeing 777-300ER", "777", 490),
    "B778": ("Boeing 777-8", "777", 490), "B779": ("Boeing 777-9", "777", 490),
    "B788": ("Boeing 787-8", "787", 485), "B789": ("Boeing 787-9", "787", 488), "B78X": ("Boeing 787-10", "787", 488),
    "A332": ("Airbus A330-200", "A330", 470), "A333": ("Airbus A330-300", "A330", 470), "A338": ("Airbus A330-800", "A330", 470),
    "A339": ("Airbus A330-900", "A330", 470), "A359": ("Airbus A350-900", "A350", 488), "A35K": ("Airbus A350-1000", "A350", 488),
    "A343": ("Airbus A340-300", "A340", 470), "A346": ("Airbus A340-600", "A340", 480),
    "B744": ("Boeing 747-400", "747", 490), "B748": ("Boeing 747-8", "747", 495), "A388": ("Airbus A380-800", "A380", 490),
    "MD11": ("McDonnell Douglas MD-11", "MD-11", 480), "B763": ("Boeing 767-300ER", "767", 460), "B764": ("Boeing 767-400ER", "767", 460),
}
OVERHEAD_MIN = 35                 # táxi, subida e descida além do cruzeiro
ROUTE_FACTOR = 1.05               # a rota voada é ~5% mais longa que o arco de círculo máximo (aerovias, SID/STAR)
AIRLINES = {  # ICAO → nome (companhias long haul mais comuns; o resto aparece pelo código)
    "QTR": "Qatar Airways", "UAE": "Emirates", "ETD": "Etihad", "AFR": "Air France", "KLM": "KLM", "DLH": "Lufthansa", "BAW": "British Airways",
    "VIR": "Virgin Atlantic", "TAP": "TAP Air Portugal", "IBE": "Iberia", "AAL": "American Airlines", "DAL": "Delta", "UAL": "United",
    "SIA": "Singapore Airlines", "CPA": "Cathay Pacific", "JAL": "Japan Airlines", "ANA": "All Nippon Airways", "KAL": "Korean Air",
    "AAR": "Asiana", "THY": "Turkish Airlines", "ETH": "Ethiopian", "SAA": "South African", "QFA": "Qantas", "ANZ": "Air New Zealand",
    "ACA": "Air Canada", "TAM": "LATAM Brasil", "LAN": "LATAM", "AZU": "Azul", "GLO": "GOL", "AVA": "Avianca", "CMP": "Copa", "AMX": "Aeroméxico",
    "EVA": "EVA Air", "CAL": "China Airlines", "CCA": "Air China", "CES": "China Eastern", "CSN": "China Southern", "CHH": "Hainan",
    "SVA": "Saudia", "ELY": "El Al", "KQA": "Kenya Airways", "RAM": "Royal Air Maroc", "MSR": "EgyptAir", "AIC": "Air India", "PAL": "Philippine",
    "THA": "Thai Airways", "MAS": "Malaysia", "GIA": "Garuda", "VIV": "Viva", "SWR": "Swiss", "AUA": "Austrian", "FIN": "Finnair",
    "SAS": "SAS", "ITY": "ITA Airways", "AEA": "Air Europa", "EDW": "Edelweiss", "CFG": "Condor", "WJA": "WestJet", "OMA": "Oman Air",
    "GFA": "Gulf Air", "KAC": "Kuwait Airways", "RJA": "Royal Jordanian", "FDX": "FedEx", "UPS": "UPS", "GTI": "Atlas Air", "CLX": "Cargolux",
    "CKS": "Kalitta", "BOX": "AeroLogic", "ABW": "AirBridgeCargo", "LCO": "LATAM Cargo", "HAL": "Hawaiian", "FJI": "Fiji Airways",
}
NETWORKS = ("IVAO", "VATSIM", "OFFLINE", "OUTRA")
CALLSIGN_RE = re.compile(r"^([A-Z]{3})(\d{1,4}[A-Z]{0,2})$")

_lock = threading.Lock()
_cache = {"at": 0.0, "result": None}
_vatsim = {"at": 0.0, "data": None}


def _p(s):
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc) if s else None


def _iso(d):
    return d.strftime(FMT) if d else None


def _http(url, data=None, opener=None, timeout=20) -> bytes:
    if opener:
        return opener(url, data)
    if os.environ.get("LINEOPS_OFFLINE") == "1":
        raise FGVError("Sem internet neste modo.", code="offline", status=503)
    h = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def est_block(km: float | None, type_icao: str | None) -> int | None:
    """Duração estimada: distância (+5% de rota) ÷ cruzeiro típico do tipo + táxi, subida e descida. Vento não entra: o SimBrief calcula."""
    if not km:
        return None
    kt = TYPES.get((type_icao or "").upper(), ("", "", 480))[2]
    return int(round(km * ROUTE_FACTOR / 1.852 / kt * 60 + OVERHEAD_MIN))


def _airport(conn, icao, extra: dict | None = None) -> dict:
    a = rules.airport(conn, icao) or {}
    e = extra or {}
    return {"icao": icao, "name": a.get("city") or e.get("location") or e.get("name") or icao,
            "lat": a.get("lat") if a.get("lat") is not None else e.get("lat"),
            "lon": a.get("lon") if a.get("lon") is not None else e.get("lon"),
            "tz": a.get("tz") or rules.airport_tz(conn, icao), "country": _country(icao, a, e)}


def _country(icao, a, e) -> str | None:
    """País (ISO de 2 letras): o catálogo mundial do AeroScale guarda o país; o mapa brasileiro guarda a UF."""
    from .aeroscale import intl
    w = intl.airport_info(icao or "")
    if w and w.get("uf"):
        return w["uf"]
    if (icao or "")[:2] in ("SB", "SD", "SI", "SJ", "SN", "SS", "SW"):
        return "BR"
    return a.get("country") or e.get("countryiso2")


# ------------------------------------------------------------------ ADS-B: o que está no ar agora
def fetch_live(conn, *, opener=None, types=None, min_km=MIN_KM, now=None) -> dict:
    """Long-haulers no ar agora + rota de cada um. Guarda no catálogo. Cache de 10 min (a API é pública, sem abuso)."""
    now = now or now_iso()
    with _lock:
        if not opener and _cache["result"] and time.time() - _cache["at"] < CACHE_S:
            return {**_cache["result"], "cached": True}
    tlist = [t for t in (types or TYPES) if t in TYPES]

    def one_type(t):
        try:
            d = json.loads(_http(ADSB_TYPE_URL.format(t=t), None, opener, timeout=15).decode("utf-8"))
            return [dict(a, _type=t) for a in d.get("ac") or []]
        except FGVError:
            raise
        except Exception as e:
            log.warning("adsb.lol %s: %s", t, e)
            return []
    with ThreadPoolExecutor(max_workers=4) as ex:
        planes = [a for lst in ex.map(one_type, tlist) for a in lst]
    cand = {}
    for a in planes:
        cs = str(a.get("flight") or "").strip().upper()
        if not CALLSIGN_RE.match(cs) or a.get("lat") is None or a.get("lon") is None:
            continue
        alt = a.get("alt_baro")
        if not isinstance(alt, (int, float)) or alt < 10000:
            continue                                         # no solo ou subindo/descendo perto do aeroporto
        cand[cs] = a
    routes = {}
    items = list(cand.values())
    for i in range(0, len(items), 50):
        body = json.dumps({"planes": [{"callsign": str(a["flight"]).strip().upper(), "lat": a["lat"], "lng": a["lon"]} for a in items[i:i + 50]]}).encode()
        try:
            res = json.loads(_http(ADSB_ROUTESET_URL, body, opener, timeout=25).decode("utf-8"))
        except FGVError:
            raise
        except Exception as e:
            log.warning("adsb.lol routeset: %s", e)
            continue
        for r in res if isinstance(res, list) else []:
            if isinstance(r, dict) and r.get("callsign"):
                routes[str(r["callsign"]).upper()] = r
    kept = 0
    seen_types = {}
    with tx(conn):
        for cs, a in cand.items():
            r = routes.get(cs)
            if not r or not r.get("plausible") or str(r.get("plausible")).lower() in ("0", "false"):
                continue
            codes = str(r.get("airport_codes") or "").split("-")
            if len(codes) != 2 or not all(len(c) == 4 for c in codes) or "unknown" in str(r.get("airport_codes")).lower():
                continue
            aps = {x.get("icao"): x for x in r.get("_airports") or [] if isinstance(x, dict)}
            o, d = _airport(conn, codes[0], aps.get(codes[0])), _airport(conn, codes[1], aps.get(codes[1]))
            if None in (o["lat"], o["lon"], d["lat"], d["lon"]):
                continue
            km = geo.nm(o["lat"], o["lon"], d["lat"], d["lon"]) * 1.852
            if km < min_km:
                continue
            flown = geo.nm(o["lat"], o["lon"], a["lat"], a["lon"]) * 1.852
            gs = a.get("gs") if isinstance(a.get("gs"), (int, float)) and a.get("gs") > 200 else 470
            est_dep = _iso(_p(now) - timedelta(minutes=flown / 1.852 / gs * 60 + 20))
            m = CALLSIGN_RE.match(cs)
            conn.execute("""INSERT INTO ext_seen(callsign, airline, number, type_icao, registration, adep, ades, adep_name, ades_name,
                            adep_lat, adep_lon, ades_lat, ades_lon, km, est_dep_utc, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(callsign) DO UPDATE SET type_icao=excluded.type_icao, registration=excluded.registration, adep=excluded.adep,
                            ades=excluded.ades, adep_name=excluded.adep_name, ades_name=excluded.ades_name, adep_lat=excluded.adep_lat,
                            adep_lon=excluded.adep_lon, ades_lat=excluded.ades_lat, ades_lon=excluded.ades_lon, km=excluded.km,
                            est_dep_utc=excluded.est_dep_utc, last_seen=excluded.last_seen, times_seen=ext_seen.times_seen+1""",
                         (cs, m.group(1), m.group(2), a["_type"], (a.get("r") or None), codes[0], codes[1], o["name"], d["name"],
                          o["lat"], o["lon"], d["lat"], d["lon"], round(km), est_dep, now, now))
            kept += 1
            seen_types[a["_type"]] = seen_types.get(a["_type"], 0) + 1
    out = {"planes": len(planes), "candidates": len(cand), "routes": len(routes), "kept": kept, "types": seen_types,
           "at": now, "attribution": ATTRIBUTION}
    if not opener:
        with _lock:
            _cache.update(at=time.time(), result=out)
    return out


def catalog(conn, *, family=None, type_icao=None, origin=None, dest=None, country=None, min_h=None, max_h=None, airline=None,
            days=7, limit=60, now=None) -> list[dict]:
    """Voos reais do catálogo, com filtros. origin/dest aceitam ICAO (SBGR) ou país (BR) ou começo do ICAO (SB, K)."""
    now = now or now_iso()
    since = _iso(_p(now) - timedelta(days=days))
    out = []
    for r in rows(conn.execute("SELECT * FROM ext_seen WHERE last_seen>=? ORDER BY last_seen DESC LIMIT 2000", (since,))):
        t = TYPES.get(r["type_icao"] or "", ("", "", 480))
        r["type_name"], r["family"] = t[0] or r["type_icao"], t[1]
        r["airline_name"] = AIRLINES.get(r["airline"] or "", r["airline"])
        r["est_block_min"] = est_block(r["km"], r["type_icao"])
        r["adep_country"] = _airport(conn, r["adep"])["country"]
        r["ades_country"] = _airport(conn, r["ades"])["country"]
        if family and r["family"] != family:
            continue
        if type_icao and r["type_icao"] != type_icao.upper():
            continue
        if airline and r["airline"] != airline.upper():
            continue
        if not _match(r, "adep", origin) or not _match(r, "ades", dest):
            continue
        if country and country.upper() not in (r["adep_country"], r["ades_country"]):
            continue
        if min_h and (r["est_block_min"] or 0) < float(min_h) * 60:
            continue
        if max_h and (r["est_block_min"] or 0) > float(max_h) * 60:
            continue
        out.append(r)
    return out[:limit]


def _match(r, side, q):
    if not q:
        return True
    q = q.strip().upper()
    icao, cc = r[side] or "", r[f"{side}_country"] or ""
    return icao == q or (len(q) == 2 and (cc == q or icao.startswith(q))) or (len(q) == 1 and icao.startswith(q))


def draw(conn, seed=None, **filters) -> dict | None:
    lst = catalog(conn, limit=2000, **filters)
    return random.Random(seed).choice(lst) if lst else None


# ------------------------------------------------------------------ voo em grupo
def _member_callsigns(base: str | None, n: int) -> list[str | None]:
    if not base:
        return [None] * n
    return [base] + [f"{base}{chr(ord('B') + i)}"[:10] for i in range(n - 1)]


def create(conn, *, callsign=None, adep=None, ades=None, type_icao=None, dep_utc=None, stagger_min=3, members=None, notes=None,
           user_id=None) -> dict:
    """Monta o voo em grupo. Com callsign do catálogo, rota e tipo vêm do voo real; sem, rota manual."""
    seen = one(conn.execute("SELECT * FROM ext_seen WHERE callsign=?", ((callsign or "").upper(),))) if callsign else None
    if seen:
        adep, ades, real_type, src = seen["adep"], seen["ades"], seen["type_icao"], "ADSB"
        type_icao = (type_icao or real_type).upper()
    else:
        adep, ades = (adep or "").strip().upper(), (ades or "").strip().upper()
        real_type, src = None, "MANUAL"
        if len(adep) != 4 or len(ades) != 4 or adep == ades:
            raise FGVError("Informe origem e destino (ICAO, 4 letras).", code="campo_invalido")
        type_icao = (type_icao or "").strip().upper() or None
    try:
        dep = _p(str(dep_utc).strip().replace(" ", "T")[:16] + ":00Z") if dep_utc else None
    except ValueError:
        dep = None
    if not dep:
        raise FGVError("Informe o horário de saída do grupo (UTC).", code="campo_invalido")
    try:
        stagger = max(0, min(30, int(stagger_min)))
    except (TypeError, ValueError):
        raise FGVError("Intervalo entre saídas inválido.", code="campo_invalido")
    mem = [m for m in (members or []) if str(m.get("name") or "").strip()]
    if not mem:
        raise FGVError("Coloque pelo menos uma pessoa no voo.", code="campo_invalido")
    if len(mem) > 12:
        raise FGVError("No máximo 12 pessoas por voo.", code="campo_invalido")
    o = _airport(conn, adep, {"name": seen["adep_name"], "lat": seen["adep_lat"], "lon": seen["adep_lon"]} if seen else None)
    d = _airport(conn, ades, {"name": seen["ades_name"], "lat": seen["ades_lat"], "lon": seen["ades_lon"]} if seen else None)
    km = round(geo.nm(o["lat"], o["lon"], d["lat"], d["lon"]) * 1.852) if None not in (o["lat"], o["lon"], d["lat"], d["lon"]) else None
    m = CALLSIGN_RE.match((seen or {}).get("callsign") or (callsign or "").upper())
    sugg = _member_callsigns(m.group(0) if m else None, len(mem))
    with tx(conn):
        fid = conn.execute("""INSERT INTO ext_flight(callsign, airline, number, adep, ades, adep_name, ades_name, adep_lat, adep_lon, ades_lat, ades_lon,
                              type_icao, real_type, km, est_block_min, dep_utc, stagger_min, source, seen_at, notes, created_at)
                              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (m.group(0) if m else None, m.group(1) if m else None, m.group(2) if m else None, adep, ades, o["name"], d["name"],
                            o["lat"], o["lon"], d["lat"], d["lon"], type_icao, real_type, km, est_block(km, type_icao), _iso(dep), stagger, src,
                            seen["last_seen"] if seen else None, (notes or "").strip()[:500] or None, now_iso())).lastrowid
        for i, x in enumerate(mem):
            net = str(x.get("network") or "IVAO").upper()
            if net not in NETWORKS:
                raise FGVError("Rede inválida.", code="campo_invalido")
            cs = str(x.get("callsign") or "").strip().upper()[:10] or sugg[i]
            conn.execute("INSERT INTO ext_member(flight_id, seq, name, network, callsign, type_icao) VALUES (?,?,?,?,?,?)",
                         (fid, i, str(x["name"]).strip()[:40], net, cs, (str(x.get("type_icao") or "").strip().upper()[:4] or type_icao)))
        audit(conn, user_id, "ext.create", "ext_flight", fid, {"route": f"{adep}-{ades}", "members": len(mem)})
    return get(conn, fid)


def get(conn, fid: int) -> dict:
    f = one(conn.execute("SELECT * FROM ext_flight WHERE id=?", (fid,)))
    if not f:
        raise NotFound("Voo externo não encontrado.")
    f["members"] = rows(conn.execute("SELECT * FROM ext_member WHERE flight_id=? ORDER BY seq", (fid,)))
    dep = _p(f["dep_utc"])
    for mbr in f["members"]:
        mbr["dep_utc"] = _iso(dep + timedelta(minutes=f["stagger_min"] * mbr["seq"]))
        mbr["simbrief"] = simbrief_url(f, mbr)
    f["arr_utc"] = _iso(dep + timedelta(minutes=f["est_block_min"])) if f["est_block_min"] else None
    t = TYPES.get(f["type_icao"] or "", ("", "", 480))
    f["type_name"], f["family"] = t[0] or f["type_icao"], t[1]
    f["real_type_name"] = TYPES.get(f["real_type"] or "", (f["real_type"],))[0] if f["real_type"] else None
    f["airline_name"] = AIRLINES.get(f["airline"] or "", f["airline"])
    o, d = _airport(conn, f["adep"]), _airport(conn, f["ades"])
    f["adep_tz"], f["ades_tz"] = o["tz"], d["tz"]
    f["dep_local"] = rules.local_label(conn, dep, f["adep"])
    f["arr_local"] = rules.local_label(conn, _p(f["arr_utc"]), f["ades"]) if f["arr_utc"] else None
    f["podium"] = podium(f["members"])
    f["invite"] = invite_text(conn, f)
    return f


def list_flights(conn) -> dict:
    fl = [get(conn, r["id"]) for r in rows(conn.execute("SELECT id FROM ext_flight WHERE status<>'CANCELADO' ORDER BY dep_utc DESC LIMIT 60"))]
    now = now_iso()
    return {"upcoming": sorted([f for f in fl if f["status"] == "PLANEJADO" and (f["arr_utc"] or f["dep_utc"]) >= _iso(_p(now) - timedelta(hours=6))],
                               key=lambda x: x["dep_utc"]),
            "past": [f for f in fl if f["status"] == "VOADO" or (f["arr_utc"] or f["dep_utc"]) < _iso(_p(now) - timedelta(hours=6))],
            "leaderboard": leaderboard(conn)}


def cancel(conn, fid: int, user_id=None) -> dict:
    with tx(conn):
        if not conn.execute("UPDATE ext_flight SET status='CANCELADO' WHERE id=?", (fid,)).rowcount:
            raise NotFound("Voo externo não encontrado.")
        audit(conn, user_id, "ext.cancel", "ext_flight", fid)
    return {"ok": True}


def save_result(conn, member_id: int, *, block=None, landing_fpm=None, landing_g=None, remarks=None, user_id=None) -> dict:
    m = one(conn.execute("SELECT * FROM ext_member WHERE id=?", (member_id,)))
    if not m:
        raise NotFound("Participante não encontrado.")
    bm = None
    if block not in (None, ""):
        mt = re.match(r"^(\d{1,2})[:h](\d{2})$", str(block).strip())
        if not mt:
            raise FGVError("Bloco no formato 13:45.", code="campo_invalido")
        bm = int(mt.group(1)) * 60 + int(mt.group(2))
    try:
        vs = None if landing_fpm in (None, "") else abs(int(float(landing_fpm)))
        g = None if landing_g in (None, "") else round(float(landing_g), 2)
    except (TypeError, ValueError):
        raise FGVError("Razão ou G inválidos.", code="campo_invalido")
    if vs is not None and vs > 3000 or g is not None and not 0.5 <= g <= 5:
        raise FGVError("Razão ou G fora da faixa.", code="campo_invalido")
    with tx(conn):
        conn.execute("UPDATE ext_member SET block_min=?, landing_fpm=?, landing_g=?, remarks=?, done_at=? WHERE id=?",
                     (bm, vs, g, (remarks or "").strip()[:200] or None, now_iso(), member_id))
        conn.execute("UPDATE ext_flight SET status='VOADO' WHERE id=? AND status='PLANEJADO'", (m["flight_id"],))
        audit(conn, user_id, "ext.result", "ext_member", member_id, {"fpm": vs, "g": g})
    return get(conn, m["flight_id"])


def podium(members) -> list[dict]:
    from . import landing
    done = [m for m in members if m["landing_fpm"] is not None]
    done.sort(key=lambda m: (m["landing_fpm"], m["landing_g"] or 9))
    out = []
    for i, m in enumerate(done):
        ev = landing.evaluate(m["landing_fpm"], m["landing_g"] or 1.2, type_icao=m.get("type_icao"))
        out.append({"pos": i + 1, "name": m["name"], "fpm": m["landing_fpm"], "g": m["landing_g"], "label": ev["label"], "tier": ev["tier"]})
    return out


def leaderboard(conn) -> list[dict]:
    """Quem pousa melhor no grupo (média e melhor toque nos voos externos)."""
    out = []
    for r in rows(conn.execute("""SELECT name, COUNT(*) n, AVG(landing_fpm) avg, MIN(landing_fpm) best,
                                  SUM(CASE WHEN landing_fpm >= 600 OR landing_g >= 2.2 THEN 1 ELSE 0 END) hard
                                  FROM ext_member m JOIN ext_flight f ON f.id=m.flight_id
                                  WHERE m.landing_fpm IS NOT NULL AND f.status<>'CANCELADO' GROUP BY lower(name) ORDER BY avg""")):
        out.append({"name": r["name"], "flights": r["n"], "avg_fpm": round(r["avg"]), "best_fpm": r["best"], "hard": r["hard"]})
    return out


# ------------------------------------------------------------------ briefing, convite e SimBrief
def simbrief_url(f, m) -> str:
    cs = m.get("callsign") or f.get("callsign") or ""
    mt = CALLSIGN_RE.match(cs)
    q = {"orig": f["adep"], "dest": f["ades"], "type": m.get("type_icao") or f.get("type_icao") or "", "callsign": cs}
    if mt:
        q.update(airline=mt.group(1), fltnum=mt.group(2))
    dep = _p(m.get("dep_utc") or f["dep_utc"])
    if dep:
        q.update(date=dep.strftime("%d%b%y").upper(), deph=dep.strftime("%H"), depm=dep.strftime("%M"))
    return "https://www.simbrief.com/system/dispatch.php?" + urllib.parse.urlencode({k: v for k, v in q.items() if v})


def _hm(m):
    return f"{m // 60}h{m % 60:02d}" if m is not None else "—"


def invite_text(conn, f) -> str:
    wd = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")
    dep = _p(f["dep_utc"])
    loc = f.get("dep_local")
    lines = [f"✈️ Voo em grupo · {f['callsign'] or 'voo'} {f['adep_name'] or f['adep']} → {f['ades_name'] or f['ades']}",
             f"📅 {wd[dep.weekday()]} {dep:%d/%m} · saída {dep:%H:%M}Z" + (f" ({loc['hhmm']} em {f['adep_name'] or f['adep']})" if loc else ""),
             f"🛫 {f['adep']} → 🛬 {f['ades']} · " + (f"{int(f['km']):,} km".replace(",", ".") if f["km"] else "distância ?")
             + (f" · ~{_hm(f['est_block_min'])} de voo (estimado)" if f["est_block_min"] else "")]
    if f.get("type_icao"):
        lines.append(f"🛩️ {f.get('type_name') or f['type_icao']}" + (f" (o real voou de {f['real_type']}, visto no ADS-B em {f['seen_at'][8:10]}/{f['seen_at'][5:7]})"
                                                                   if f.get("real_type") and f.get("seen_at") else ""))
    if f.get("airline_name") and f["source"] == "ADSB":
        lines.append(f"🏷️ Voo real da {f['airline_name']}: {f['callsign']}")
    lines.append(f"👥 Saídas a cada {f['stagger_min']} min:" if f["stagger_min"] else "👥 Todos juntos:")
    for m in f["members"]:
        lines.append(f"   {m['dep_utc'][11:16]}Z · {m['name']} · {m['network']}" + (f" · {m['callsign']}" if m["callsign"] else "")
                     + (f" · {m['type_icao']}" if m.get("type_icao") and m["type_icao"] != f.get("type_icao") else ""))
    if f.get("notes"):
        lines.append(f"📝 {f['notes']}")
    lines.append(f"📄 SimBrief (gere o seu com o seu callsign): {f['members'][0]['simbrief']}" if f["members"] else "")
    lines.append("Bons voos! 🌍")
    return "\n".join(x for x in lines if x)


def weather(conn, f) -> dict:
    try:
        return rules.weather(conn, [f["adep"], f["ades"]])
    except Exception as e:
        return {"metars": [], "tafs": [], "error": str(e)}


# ------------------------------------------------------------------ radar do grupo (IVAO e VATSIM)
def _vatsim_feed(opener=None) -> dict:
    with _lock:
        if _vatsim["data"] is not None and time.time() - _vatsim["at"] < 30 and not opener:
            return _vatsim["data"]
    d = json.loads(_http(VATSIM_URL, None, opener, timeout=15).decode("utf-8"))
    with _lock:
        _vatsim.update(at=time.time(), data=d)
    return d


def live(conn, fid: int, *, ivao_opener=None, vatsim_opener=None) -> dict:
    from . import ivao
    f = get(conn, fid)
    nets = {m["network"] for m in f["members"]}
    feeds, errs = {}, {}
    if "IVAO" in nets:
        try:
            data = ivao.fetch(opener=ivao_opener)
            feeds["IVAO"] = {str(p.get("callsign") or "").upper(): p for p in ivao.pilots(data)}
        except Exception as e:
            errs["IVAO"] = getattr(e, "message", str(e))
    if "VATSIM" in nets:
        try:
            data = _vatsim_feed(vatsim_opener)
            feeds["VATSIM"] = {str(p.get("callsign") or "").upper(): p for p in data.get("pilots") or []}
        except Exception as e:
            errs["VATSIM"] = getattr(e, "message", str(e))
    out = []
    for m in f["members"]:
        p = feeds.get(m["network"], {}).get((m["callsign"] or "").upper())
        st = {"member_id": m["id"], "name": m["name"], "network": m["network"], "callsign": m["callsign"], "online": bool(p)}
        if p:
            if m["network"] == "IVAO":
                t, fp = p.get("lastTrack") or {}, p.get("flightPlan") or {}
                lat, lon, alt, gs, ground = t.get("latitude"), t.get("longitude"), t.get("altitude"), t.get("groundSpeed"), t.get("onGround")
                dep_, arr_ = fp.get("departureId"), fp.get("arrivalId")
            else:
                fp = p.get("flight_plan") or {}
                lat, lon, alt, gs, ground = p.get("latitude"), p.get("longitude"), p.get("altitude"), p.get("groundspeed"), None
                dep_, arr_ = fp.get("departure"), fp.get("arrival")
                ground = isinstance(gs, (int, float)) and gs < 50
            rem = round(geo.nm(lat, lon, f["ades_lat"], f["ades_lon"])) if None not in (lat, lon, f["ades_lat"], f["ades_lon"]) else None
            tot = (f["km"] or 0) / 1.852
            st.update(lat=lat, lon=lon, alt_ft=alt, gs_kt=gs, on_ground=ground, remaining_nm=rem,
                      progress=round(max(0, min(1, 1 - rem / tot)) * 100) if rem is not None and tot else None,
                      plan=f"{dep_ or '?'}→{arr_ or '?'}", plan_ok=(dep_ == f["adep"] and arr_ == f["ades"]))
        out.append(st)
    return {"flight_id": fid, "members": out, "errors": errs, "at": now_iso(),
            "note": "Posições do whazzup público da IVAO e do data feed da VATSIM, procuradas pelo callsign de cada um."}
