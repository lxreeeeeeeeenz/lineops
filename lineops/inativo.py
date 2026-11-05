"""Dias INATIVOS (sem voo fora da base, "descanso longo") que o AeroScale coloca na escala: listar e tirar.

Tirar um INATIVO = preencher o dia com voos reais do RPL (ida e volta a partir do aeroporto onde você está, dentro das
regras de jornada e repouso do AeroScale) e apagar a marca. Se nenhum par de voos reais couber, só a marca sai: o dia
fica livre no mesmo aeroporto, nada é inventado. Só os dias com INATIVO mudam; o resto da escala fica como está.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from . import rules
from .db import now_iso, one, rows, tx

MAX_PAIRS = 60                      # pares ida+volta testados por dia (o teste de legalidade roda o crew.analyze inteiro)


def _p(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def list_days(conn, start: str | None = None, end: str | None = None) -> list[dict]:
    """Os dias INATIVOS da escala atual (da escala dona do dia)."""
    from .exportar import _owner, _schedules
    scheds = _schedules(conn)
    q, args = "SELECT * FROM roster_day WHERE kind='inativo'", []
    if start and end:
        q += " AND date BETWEEN ? AND ?"
        args = [start, end]
    out = []
    for d in rows(conn.execute(q + " ORDER BY date", args)):
        own = _owner(scheds, d["date"])
        if own and own["id"] == d["schedule_id"]:
            a = rules.airport(conn, d["location"]) or {}
            out.append({"id": d["id"], "date": d["date"], "location": d["location"], "iata": a.get("iata"), "city": a.get("city"),
                        "note": d["note"], "schedule_id": d["schedule_id"]})
    return out


def _neighbours(conn, ds: str):
    prev = one(conn.execute("SELECT * FROM roster_leg WHERE removed_batch IS NULL AND activity<>'SOBREAVISO' AND dep_utc<? ORDER BY arr_utc DESC LIMIT 1",
                            (f"{ds}T00:00:00Z",)))
    nxt = one(conn.execute("SELECT * FROM roster_leg WHERE removed_batch IS NULL AND activity<>'SOBREAVISO' AND dep_utc>=? ORDER BY dep_utc LIMIT 1",
                           (f"{ds}T23:59:59Z",)))
    return prev, nxt


def _fill(conn, data_dir, d: dict, prev: dict, nxt: dict | None) -> list[dict] | None:
    """Um par de voos reais do RPL saindo e voltando a d['location'] no dia d['date'], que passa nas regras. Ou None."""
    from . import troca
    loc, ds = d["location"], d["date"]
    p = troca._params(conn, prev)
    idx, err = troca._idx_for(data_dir, prev, p)
    if idx is None:
        return None
    want = p.get("icao_type_resolved") or p.get("icao_type") or prev.get("type_icao")
    mode = p.get("type_match") or "family"
    swap_min = int(p.get("swap_min") or 50)
    day = date.fromisoformat(ds)
    tz = rules.local_dt(conn, _p(prev["arr_utc"]), loc)
    off = (tz.utcoffset() if tz else timedelta(0)) or timedelta(0)
    lo = max(_p(prev["arr_utc"]) + timedelta(hours=10), datetime(day.year, day.month, day.day, 5, tzinfo=timezone.utc) - off)
    hi = datetime(day.year, day.month, day.day, 23, 59, tzinfo=timezone.utc) - off
    if nxt:
        hi = min(hi, _p(nxt["dep_utc"]) - timedelta(hours=10))
    if hi <= lo:
        return None
    outs, rets = {}, {}
    for dd in (day - timedelta(days=1), day, day + timedelta(days=1)):
        for o in idx.occurrences(dd, projection=True):
            if o["dep"] < lo or o["arr"] > hi or o.get("origin") == "OBSERVADO":
                continue
            if want and not rules.types_compatible(want, o["type_icao"], mode):
                continue
            if o["adep"] == loc and o["ades"] != loc:
                outs.setdefault(o["ades"], []).append(o)
            elif o["ades"] == loc and o["adep"] != loc:
                rets.setdefault(o["adep"], []).append(o)
    pairs = []
    for D, os_ in outs.items():
        for a in os_:
            for b in rets.get(D, []):
                if b["dep"] >= a["arr"] + timedelta(minutes=swap_min):
                    pairs.append((a, b))
    pairs.sort(key=lambda ab: (ab[1]["arr"] - ab[0]["dep"], ab[0]["dep"]))      # jornada mais curta primeiro
    seen = set()
    for a, b in pairs[:MAX_PAIRS]:
        if (a["ref"], b["ref"]) in seen:
            continue
        seen.add((a["ref"], b["ref"]))
        new = [troca._mk(conn, a, ""), troca._mk(conn, b, "")]
        if troca.legality(conn, prev, set(), new)["ok"]:
            return new
    return None


def remove(conn, data_dir, dates: list[str] | None = None, fill: bool = True, now: str | None = None) -> dict:
    """Tira os dias INATIVOS (todos, ou só as datas pedidas). fill=True tenta preencher cada dia com voos reais."""
    from .aeroscale_import import LEG_HASH_FIELDS, _hash
    now = now or now_iso()
    days = [d for d in list_days(conn) if not dates or d["date"] in dates]
    res = {"days": len(days), "filled": [], "freed": [], "legs": 0}
    if not days:
        return res
    with tx(conn):
        bid = conn.execute("INSERT INTO import_batch(source_kind, source_name, imported_at, stats) VALUES ('limpeza', ?, ?, ?)",
                           ("Dias INATIVOS tirados", now, json.dumps({"dates": [d["date"] for d in days]}))).lastrowid
        for k, d in enumerate(days):
            prev, nxt = _neighbours(conn, d["date"])
            new = _fill(conn, data_dir, d, prev, nxt) if fill and prev else None
            if new:
                for j, c in enumerate(new, 1):
                    leg = {"seq": prev["seq"], "airline": prev["airline"], "flight_number": c["ident"], "type_icao": c["type_icao"],
                           "planned_type": prev["planned_type"], "adep": c["adep"], "ades": c["ades"], "dep_utc": c["dep_utc"], "arr_utc": c["arr_utc"],
                           "eet_min": c["eet_min"], "data_origin": c["origin"] or prev["data_origin"], "activity": "", "reserve_uid": None,
                           "registration": None, "route": None, "cruise_fl": None, "speed": None,
                           "remarks": f"No lugar do dia INATIVO em {d['location']} ({d['date'][8:10]}/{d['date'][5:7]})."}
                    conn.execute(
                        f"""INSERT INTO roster_leg(uid, schedule_id, {', '.join(LEG_HASH_FIELDS)}, local_date, content_hash, first_batch, last_batch)
                            VALUES (?,?,{','.join('?' * len(LEG_HASH_FIELDS))},?,?,?,?)""",
                        (f"inativo-{bid}-{k}-{j}", d["schedule_id"], *[leg[x] for x in LEG_HASH_FIELDS], c["local_date"], _hash(leg), bid, bid))
                    res["legs"] += 1
                res["filled"].append({"date": d["date"], "location": d["location"], "flights": [f"{c['ident']} {c['adep']}→{c['ades']}" for c in new]})
            else:
                res["freed"].append({"date": d["date"], "location": d["location"]})
            conn.execute("DELETE FROM roster_day WHERE id=?", (d["id"],))
    return res
