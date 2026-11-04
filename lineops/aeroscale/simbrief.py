"""Link automático para o SimBrief (dispatch.simbrief.com/options/custom).

Os campos vêm do que já existe: a perna da escala, a matrícula/tipo da aeronave e o registro do RPL
(rota, nível, equipamento, PBN). Nada é inventado. O alternado é uma SUGESTÃO, escolhida assim:
1. alternado fixo cadastrado para o destino em Configurações (simbrief_alternates);
2. senão, o aeroporto mais próximo do destino (10–450 km) com pista pavimentada suficiente para a
   categoria da aeronave, dando preferência aos atendidos pela própria companhia no RPL;
3. senão, o campo vai vazio e o SimBrief escolhe.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from urllib.parse import urlencode

from . import catalog
from .db import get_settings, row

BASE_URL = "https://dispatch.simbrief.com/options/custom"
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
# pista pavimentada mínima (m) para considerar um aeroporto como alternado, por categoria
MIN_RWY = {"ATR": 1200, "C208": 700, "E-JET": 1500, "A330": 2500, "A350": 2600, "B767": 2500, "B777": 2800,
           "B787": 2600}
DEFAULT_MIN_RWY = 1600
ALT_MIN_KM, ALT_MAX_KM = 10, 450


def haversine_km(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def clean_alternates(v) -> dict:
    """{"SBFZ": "SBJP"} — aceita dict ou texto "SBFZ=SBJP, SBGR=SBKP"."""
    if isinstance(v, str):
        pairs = [p.split("=", 1) for p in re.split(r"[,;\n]+", v) if "=" in p]
        v = {a: b for a, b in pairs}
    out = {}
    for k, x in (v or {}).items():
        k, x = str(k).strip().upper(), str(x).strip().upper()
        if not k and not x:
            continue
        if not (re.fullmatch(r"[A-Z]{4}", k) and re.fullmatch(r"[A-Z]{4}", x)):
            raise ValueError(f"Alternado inválido: {k}={x} (use ICAO de 4 letras, ex.: SBFZ=SBJP).")
        if k == x:
            raise ValueError(f"O alternado de {k} não pode ser ele mesmo.")
        out[k] = x
    return out


def served_airports(db, airline: str) -> set[str]:
    out = set()
    for r in db.execute("SELECT DISTINCT f.adep, f.ades FROM rpl_flights f JOIN rpl_imports i ON i.id=f.import_id"
                        " WHERE f.airline=? AND i.active=1", (airline,)):
        out.add(r["adep"])
        out.add(r["ades"])
    return out


def suggest_alternate(db, dest: str, orig: str, airline: str, type_icao: str | None,
                      apmap: dict | None = None, served: set | None = None, settings: dict | None = None) -> dict:
    s = settings or get_settings(db)
    fixed = (s.get("simbrief_alternates") or {}).get(dest)
    if fixed:
        return {"icao": fixed, "source": "fixo", "note": f"alternado fixo cadastrado para {dest}"}
    apmap = apmap or catalog.airport_map(db)
    d = apmap.get(dest) or {}
    if d.get("lat") is None:
        return {"icao": None, "source": "simbrief", "note": f"{dest} sem coordenadas: o SimBrief escolhe o alternado"}
    fam = catalog.type_family(type_icao) if type_icao else None
    need = MIN_RWY.get(fam or "", DEFAULT_MIN_RWY)
    served = served if served is not None else served_airports(db, airline)
    best = None
    for icao, a in apmap.items():
        if icao in (dest, orig) or a.get("lat") is None or (a.get("rwy_m") or 0) < need:
            continue
        km = haversine_km((d["lat"], d["lon"]), (a["lat"], a["lon"]))
        if not ALT_MIN_KM <= km <= ALT_MAX_KM:
            continue
        sc = km + (0 if icao in served else 150)
        if best is None or sc < best[0]:
            best = (sc, icao, km, icao in served, a.get("rwy_m"))
    if not best:
        return {"icao": None, "source": "simbrief",
                "note": f"nenhum aeroporto adequado a {ALT_MIN_KM}–{ALT_MAX_KM} km de {dest}: o SimBrief escolhe"}
    _sc, icao, km, srv, rwy = best
    return {"icao": icao, "source": "sugerido", "distance_km": round(km), "served": srv, "rwy_m": rwy,
            "note": f"mais próximo de {dest} ({round(km)} km, pista {rwy} m"
                    + (f", atendido pela {airline})" if srv else ", não atendido pela companhia no RPL)")}


def _fl_feet(fl: str | None) -> int | None:
    m = re.fullmatch(r"[FAS]?(\d{2,3})", (fl or "").strip().upper())
    return int(m.group(1)) * 100 if m else None


def _rpl_row(db, leg: dict) -> tuple[dict | None, str]:
    """Registro do RPL da perna; para pernas manuais/sugeridas, o voo do mesmo trecho mais frequente."""
    if leg.get("flight_id"):
        f = row(db.execute("SELECT * FROM rpl_flights WHERE id=?", (leg["flight_id"],)))
        if f:
            return f, "rpl"
    q = ("SELECT f.* FROM rpl_flights f JOIN rpl_imports i ON i.id=f.import_id WHERE i.active=1 AND f.airline=?"
         " AND f.adep=? AND f.ades=?")
    args = [leg["airline"], leg["adep"], leg["ades"]]
    if leg.get("flight_number"):
        f = row(db.execute(q + " AND f.ident=? ORDER BY f.valid_to DESC LIMIT 1", args + [leg["flight_number"]]))
        if f:
            return f, "rpl"
    f = row(db.execute(q + " GROUP BY f.route ORDER BY COUNT(*) DESC LIMIT 1", args))
    if f:
        return f, "trecho"
    return None, "nenhum"


def build(db, leg: dict, aircraft: dict | None = None, ctx: dict | None = None, overrides: dict | None = None) -> dict:
    """Monta a URL do SimBrief e devolve também os campos usados (para conferência na tela)."""
    ctx = ctx if ctx is not None else {}
    s = ctx.get("settings") or get_settings(db)
    apmap = ctx.get("apmap") or catalog.airport_map(db)
    ctx.setdefault("settings", s)
    ctx.setdefault("apmap", apmap)
    if leg["airline"] not in ctx.setdefault("served", {}):
        ctx["served"][leg["airline"]] = served_airports(db, leg["airline"])
    ov = overrides or {}
    f, rsrc = _rpl_row(db, leg)
    rem = json.loads(f["remarks_json"] or "{}") if f else {}
    airline = leg["airline"]
    ident = leg.get("flight_number") or ""
    m = re.match(r"^[A-Z]{3}(\d+[A-Z]?)$", ident)
    fltnum = m.group(1) if m else re.sub(r"^\D+", "", ident)
    ac_type = (aircraft or {}).get("icao_type") or leg.get("type_icao") or leg.get("planned_type") or (f or {}).get("type_icao")
    reg = (aircraft or {}).get("registration") or leg.get("registration")
    dep = datetime.fromisoformat(leg["dep_utc"].replace("Z", "+00:00"))
    eet = leg.get("eet_min") or (f or {}).get("eet_min")
    alt = suggest_alternate(db, leg["ades"], leg["adep"], airline, ac_type, apmap,
                            ctx["served"][airline], s)
    route = ov.get("route") if ov.get("route") is not None else ((f or {}).get("route") or "")
    fl = _fl_feet(ov.get("fl")) if ov.get("fl") else _fl_feet((f or {}).get("fl"))
    altn = (ov.get("altn") or alt.get("icao") or "").upper()
    rmk_parts = []
    if rem.get("EQPT"):
        rmk_parts.append(f"EQPT/{rem['EQPT']}")
    if rem.get("PBN"):
        rmk_parts.append(f"PBN/{rem['PBN']}")
    rmk_parts.append(f"OPR/{airline}")
    rmk_words: list[str] = []
    for w in f"{s.get('simbrief_rmk') or ''} {rem.get('RMK') or ''}".split():
        if w not in rmk_words:  # RPL já com "RMK/TCAS" não vira "TCAS TCAS"
            rmk_words.append(w)
    rmk_txt = " ".join(rmk_words)
    if rmk_txt:
        rmk_parts.append(f"RMK/{rmk_txt}")
    params = {"airline": airline}
    if fltnum:
        params["fltnum"] = fltnum
    if ac_type:
        params["type"] = ac_type
    params.update(orig=leg["adep"], dest=leg["ades"])
    if route:
        params["route"] = route
    if fl:
        params["fl"] = fl
    if altn:
        params["altn"] = altn
    params["extrarmk"] = " ".join(rmk_parts)
    if reg and not str(reg).upper().startswith("FIC-"):
        params["reg"] = str(reg).upper()
    params.update(date=f"{dep.day:02d}{MONTHS[dep.month - 1]}{dep.year % 100:02d}", deph=f"{dep.hour:02d}",
                  depm=f"{dep.minute:02d}")
    if eet:
        params.update(steh=str(eet // 60), stem=f"{eet % 60:02d}")
    params["units"] = s.get("simbrief_units") or "KGS"
    url = BASE_URL + "?" + urlencode(params)
    notes = []
    if rsrc == "trecho":
        notes.append(f"Rota, nível e equipamento copiados do voo {f['ident']} do RPL (mesmo trecho), porque esta "
                     "perna não tem registro próprio no RPL.")
    elif rsrc == "nenhum":
        notes.append("Sem registro no RPL para este trecho: rota e nível ficam para o SimBrief.")
    if reg and str(reg).upper().startswith("FIC-"):
        notes.append("Matrícula fictícia não enviada.")
    return {"url": url, "params": params, "alternate": alt, "rpl_source": rsrc, "notes": notes}


def annotate(db, sched: dict) -> None:
    """Coloca leg['simbrief'] (só a URL e o alternado) em cada perna da escala."""
    ctx: dict = {}
    ac = None
    if sched.get("aircraft") and sched["aircraft"].get("id"):
        ac = row(db.execute("SELECT a.registration, m.icao_type FROM aircraft a LEFT JOIN aircraft_models m"
                            " ON m.id=a.model_id WHERE a.id=?", (sched["aircraft"]["id"],)))
    for leg in sched.get("legs", []):
        try:
            b = build(db, leg, ac if ac and leg.get("aircraft_id") == sched["aircraft"]["id"] else None, ctx)
            leg["simbrief"] = {"url": b["url"], "altn": b["params"].get("altn"), "alt_source": b["alternate"]["source"]}
        except Exception as e:  # nunca quebra a escala por causa do link
            leg["simbrief"] = {"url": None, "error": str(e)}
