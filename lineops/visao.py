"""Visões da escala e da carreira: o mês em calendário, o mapa das rotas, os limites do mês, o passaporte e o diário em CSV.

Tudo é leitura do que já existe (escala do motor, diário, regras do AeroScale). Nada é estimado: onde falta dado, o campo
volta vazio.
* MÊS: cada dia com o tipo (voo, folga, reserva, sobreaviso, livre), as pernas, o trecho, o bloco e onde você dorme.
* MAPA: aeroportos (lat/lon do catálogo do AeroScale) e os trechos da escala do período, ou de tudo o que você já voou.
* LIMITES: horas de voo e de jornada do mês contra os limites da Lei 13.475 (art. 33 e 41), folgas contra o mínimo e
  madrugadas, pelo crew.analyze do AeroScale.
* PASSAPORTE: os aeroportos onde você já pousou (diário), com a primeira e a última visita; horas por tipo de avião.
"""
from __future__ import annotations

import calendar
import csv
import io
from datetime import date, datetime, timedelta

from . import journey, rules
from .db import jloads, now_iso, one, rows

DUTY_MONTH_H = 176.0          # Lei 13.475, art. 41: jornada mensal


def _place(conn, icao):
    if not icao:
        return None
    a = rules.airport(conn, icao) or {}
    from .conquistas import country_of            # aeroporto estrangeiro traz o país no campo da UF ("AR" para Ezeiza)
    return {"icao": icao, "iata": a.get("iata"), "city": a.get("city") or a.get("name"), "lat": a.get("lat"), "lon": a.get("lon"),
            "country": country_of(a)}


def _month_bounds(ym: str) -> tuple[date, date]:
    y, m = int(ym[:4]), int(ym[5:7])
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


# ------------------------------------------------------------------ o mês em calendário
def month(conn, ym: str, now: str | None = None) -> dict:
    d0, d1 = _month_bounds(ym)
    now = now or now_iso()
    today = journey.local_today(conn, now)
    from . import escala
    legs = escala.legs(conn, d0.isoformat(), d1.isoformat())
    by = {}
    for l in legs:
        by.setdefault(l["local_date"] or l["dep_utc"][:10], []).append(l)
    res = rows(conn.execute("SELECT * FROM roster_reserve WHERE start_date<=? AND end_date>=?", (d1.isoformat(), d0.isoformat())))
    swaps = {}
    for r in rows(conn.execute("SELECT leg_ids, state FROM swap_request WHERE state='PENDENTE'")):
        for i in jloads(r["leg_ids"], []):
            swaps[i] = True
    marked = {r[0] for r in conn.execute("SELECT leg_uid FROM overnight_post WHERE state='ENVIADO'")}   # "ESSE VOO É PERNOITE"
    p_cache = {}
    days, tot = [], {"flight_days": 0, "legs": 0, "block_min": 0, "off": 0, "reserve": 0, "standby": 0, "nights_out": 0}
    airports = set()
    d = d0
    base = None
    while d <= d1:
        ds = d.isoformat()
        p, s = journey.schedule_params(conn, ds)
        sid = s["id"] if s else None
        if sid not in p_cache:
            p_cache[sid] = (p, set(p.get("planned_days_off") or []))
        p, offs = p_cache[sid]
        base = base or p.get("base_resolved") or p.get("base")
        dl = by.get(ds, [])
        fl = [l for l in dl if l["activity"] != "SOBREAVISO"]
        sb = [l for l in dl if l["activity"] == "SOBREAVISO"]
        rv = next((r for r in res if r["start_date"] <= ds <= r["end_date"]), None)
        kind = "VOO" if fl else ("RESERVA" if rv else ("SOBREAVISO" if sb else ("FOLGA" if ds in offs else "LIVRE")))
        block = 0
        for l in fl:
            try:
                block += int((journey.parse(l["arr_utc"]) - journey.parse(l["dep_utc"])).total_seconds() // 60) if l["activity"] != "EXTRA" else 0
            except Exception:                                    # noqa: BLE001
                pass
        last = fl[-1] if fl else None
        nxt = None
        if last:
            nxt = one(conn.execute("SELECT adep, local_date FROM roster_leg WHERE removed_batch IS NULL AND activity<>'SOBREAVISO' "
                                   "AND dep_utc>? ORDER BY dep_utc LIMIT 1", (last["arr_utc"],)))
        night = last["ades"] if last and base and last["ades"] != base and nxt and nxt["adep"] == last["ades"] else None
        mk = [l for l in fl if l.get("uid") in marked]
        if mk:                                                   # o voo que você marcou como pernoite manda no dia
            night = mk[-1]["ades"]
        route = []
        for l in fl:
            if not route:
                route.append(l["adep"])
            if route[-1] != l["adep"]:
                route.append(l["adep"])
            route.append(l["ades"])
        airports.update(route)
        days.append({"date": ds, "day": d.day, "weekday": d.weekday(), "kind": kind, "legs": len([l for l in fl if l["activity"] != "EXTRA"]),
                     "extra": len([l for l in fl if l["activity"] == "EXTRA"]), "route": route, "block_min": block or None,
                     "first_dep": fl[0]["dep_utc"] if fl else None, "overnight_at": night, "today": ds == today, "past": ds < today,
                     "booked": sum(1 for l in fl if l["flight_id"]), "flown": sum(1 for l in fl if l["flight_state"] == "APROVADO"),
                     "swap": any(swaps.get(l["id"]) for l in fl), "reserve_at": rv["location"] if rv else None,
                     "projected": any(l["data_origin"] == "PROJETADO" for l in fl)})
        if fl:
            tot["flight_days"] += 1
            tot["legs"] += days[-1]["legs"]
            tot["block_min"] += block
        tot["off"] += kind == "FOLGA"
        tot["reserve"] += kind == "RESERVA"
        tot["standby"] += kind == "SOBREAVISO"
        tot["nights_out"] += bool(night)
        d += timedelta(days=1)
    tot["airports"] = len(airports)
    return {"month": ym, "first": d0.isoformat(), "last": d1.isoformat(), "first_weekday": d0.weekday(), "days": days, "totals": tot,
            "base": _place(conn, base)}


# ------------------------------------------------------------------ mapa das rotas
def route_map(conn, start: str | None = None, end: str | None = None, source: str = "escala") -> dict:
    """Aeroportos e trechos. source='escala' (a escala do período) ou 'diario' (tudo o que você já voou)."""
    if source == "diario":
        legs = rows(conn.execute("SELECT adep, ades, date AS d FROM logbook_entry WHERE reversed_at IS NULL"))
    else:
        legs = rows(conn.execute("""SELECT adep, ades, COALESCE(local_date, substr(dep_utc,1,10)) AS d FROM roster_leg
                                    WHERE removed_batch IS NULL AND activity NOT IN ('SOBREAVISO')
                                      AND COALESCE(local_date, substr(dep_utc,1,10)) BETWEEN ? AND ?""",
                                 (start or "0000", end or "9999")))
    seg, visits = {}, {}
    for l in legs:
        a, b = sorted((l["adep"], l["ades"]))
        seg[(a, b)] = seg.get((a, b), 0) + 1
        for x in (l["adep"], l["ades"]):
            visits[x] = visits.get(x, 0) + 1
    p, _s = journey.schedule_params(conn, start or now_iso()[:10])
    base = p.get("base_resolved") or p.get("base")
    aps = []
    for icao, n in visits.items():
        pl = _place(conn, icao)
        if pl and pl["lat"] is not None:
            aps.append({**pl, "visits": n, "base": icao == base})
    known = {a["icao"] for a in aps}
    return {"source": source, "start": start, "end": end, "airports": sorted(aps, key=lambda a: -a["visits"]),
            "segments": [{"a": a, "b": b, "n": n} for (a, b), n in sorted(seg.items(), key=lambda x: -x[1]) if a in known and b in known],
            "missing": sorted(set(visits) - known), "base": base}


# ------------------------------------------------------------------ limites do mês
def limits(conn, ym: str) -> dict:
    d0, d1 = _month_bounds(ym)
    rep = journey.crew_report(conn, d0.isoformat(), d1.isoformat())
    if not rep.get("available"):
        return {"available": False, "reason": rep.get("reason")}
    from .aeroscale import crew
    p, _s = journey.schedule_params(conn, d0.isoformat())
    fam = rules.type_family(p.get("icao_type_resolved") or p.get("icao_type"))
    m = (rep["summary"].get("months") or {}).get(ym) or {}
    lim_block = crew.monthly_block_limit_h(fam)
    errs = [v["message"] for v in rep["violations"] if v["level"] == "error"] + \
           [i["message"] for d in rep["duties"] for i in d["issues"] if i["level"] == "error"]
    return {"available": True, "month": ym,
            "block": {"h": m.get("block_h", rep["summary"].get("block_h")), "limit": lim_block, "rule": "Lei 13.475, art. 33"},
            "duty": {"h": m.get("duty_h", rep["summary"].get("duty_h")), "limit": DUTY_MONTH_H, "rule": "Lei 13.475, art. 41"},
            "off": {"n": m.get("off"), "required": m.get("required_off"), "weekend_pair": m.get("weekend_pair"), "rule": "Lei 13.475, art. 50 e 51"},
            "madrugadas": rep["summary"].get("madrugadas"), "nights_out": rep["summary"].get("nights_out"),
            "max_consecutive": rep["summary"].get("max_consecutive"), "errors": errs[:5],
            "source": "crew.analyze do AeroScale"}


# ------------------------------------------------------------------ passaporte e horas por tipo
def passport(conn) -> dict:
    lg = rows(conn.execute("SELECT date, adep, ades, type_icao, block_min, night_min, landings FROM logbook_entry WHERE reversed_at IS NULL ORDER BY date"))
    aps, types = {}, {}
    for e in lg:
        for icao, kind in ((e["adep"], "dep"), (e["ades"], "arr")):
            a = aps.setdefault(icao, {"icao": icao, "first": e["date"], "last": e["date"], "landings": 0, "departures": 0})
            a["last"] = e["date"]
            a["landings" if kind == "arr" else "departures"] += 1
        t = types.setdefault(e["type_icao"] or "—", {"type": e["type_icao"] or "—", "name": rules.type_name(e["type_icao"]),
                                                     "flights": 0, "block_min": 0, "night_min": 0})
        t["flights"] += 1
        t["block_min"] += e["block_min"] or 0
        t["night_min"] += e["night_min"] or 0
    out = []
    for a in aps.values():
        pl = _place(conn, a["icao"]) or {}
        out.append({**a, "city": pl.get("city"), "iata": pl.get("iata"), "country": pl.get("country")})
    out.sort(key=lambda x: (x["first"], x["icao"]))
    return {"airports": out, "countries": sorted({a["country"] for a in out if a.get("country")}),
            "types": sorted(types.values(), key=lambda t: -t["block_min"]), "flights": len(lg)}


# ------------------------------------------------------------------ diário em CSV
CSV_COLS = ("date", "airline", "flight_number", "adep", "ades", "registration", "type_icao", "out_utc", "off_utc", "on_utc", "in_utc",
            "block_min", "air_min", "night_min", "landings", "fuel_used_kg", "landing_vs_fpm", "landing_g", "network", "source", "remarks")


def logbook_csv(conn) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(CSV_COLS)
    for e in rows(conn.execute(f"SELECT {', '.join(CSV_COLS)} FROM logbook_entry WHERE reversed_at IS NULL ORDER BY date, out_utc")):
        w.writerow(["" if e[c] is None else e[c] for c in CSV_COLS])
    return "﻿" + buf.getvalue()                            # BOM: o Excel abre com acento certo
