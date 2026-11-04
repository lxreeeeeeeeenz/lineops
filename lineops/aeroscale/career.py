"""Modo carreira: perfil do piloto, progressão pelas horas do diário de bordo e pedido de escala.

Pedido do mês (como nos sistemas de "bid" das companhias): folgas em datas escolhidas, evitar madrugada,
preferência por manhã ou tarde, destinos preferidos e evitados. A companhia concede parte dos pedidos conforme a
antiguidade: a chance de cada folga pedida ser atendida sobe com o cargo e os anos de casa (sorteio com semente,
reproduzível). O relatório compara o pedido com a escala gerada.
"""
from __future__ import annotations

import json
import random
from datetime import date

from . import catalog
from .db import now_iso, row, rows

DEFAULT = {"name": "", "rank": "Copiloto", "airline": "GLO", "base": "SBGR", "icao_type": "B738",
           "hire_year": date.today().year, "captain_hours": 3000, "mode": "tripulante"}
RANKS = ("Copiloto", "Comandante")


def get_profile(db) -> dict:
    r = db.execute("SELECT value FROM meta WHERE key='career'").fetchone()
    p = dict(DEFAULT)
    if r:
        try:
            p.update(json.loads(r[0]))
        except ValueError:
            pass
    return p


def save_profile(db, data: dict) -> dict:
    p = get_profile(db)
    for k in DEFAULT:
        if k in data and data[k] not in (None, ""):
            p[k] = data[k]
    p["airline"] = str(p["airline"]).upper()
    p["base"] = str(p["base"]).upper()
    p["icao_type"] = str(p["icao_type"]).upper()
    if p["rank"] not in RANKS:
        raise ValueError("Cargo inválido.")
    p["hire_year"] = int(p["hire_year"])
    p["captain_hours"] = max(100, int(p["captain_hours"]))
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('career', ?)", (json.dumps(p, ensure_ascii=False),))
    return p


def progress(db) -> dict:
    """Horas do diário (total e no tipo da habilitação) e marcos da carreira."""
    p = get_profile(db)
    fam = catalog.type_family(p["icao_type"])
    ent = rows(db.execute("SELECT date, aircraft_type, block_min, landings FROM logbook ORDER BY date, off_block_utc"))
    tot = on_type = 0
    milestones = []
    marks = [10, 50, 100, 250, 500, 1000, 1500, 2000, 3000, 5000, 10000]
    for e in ent:
        tot += int(e["block_min"] or 0)
        if catalog.type_family(e["aircraft_type"] or "") == fam:
            on_type += int(e["block_min"] or 0)
        while marks and tot >= marks[0] * 60:
            milestones.append({"date": e["date"], "text": f"{marks[0]} h de voo"})
            marks.pop(0)
    need = p["captain_hours"] * 60
    out = {"profile": p, "total_min": tot, "on_type_min": on_type, "flights": len(ent), "family": fam,
           "captain_min": need, "captain_pct": min(100, round(100 * tot / need)) if need else 100,
           "eligible_captain": p["rank"] == "Copiloto" and tot >= need, "milestones": milestones[-8:],
           "seniority_years": max(0, date.today().year - int(p["hire_year"]))}
    return out


def grant_pct(p: dict) -> int:
    """Chance de atender cada folga pedida: 55% para copiloto recém-contratado, até 95% para comandante antigo."""
    years = max(0, date.today().year - int(p.get("hire_year") or date.today().year))
    base = 70 if p.get("rank") == "Comandante" else 55
    return min(95, base + 4 * years)


def bid_to_params(db, bid: dict) -> tuple[dict, dict]:
    """Converte o pedido em parâmetros do gerador. Devolve (parâmetros, concessão)."""
    p = get_profile(db)
    month = bid.get("month")
    if not month:
        raise ValueError("Escolha o mês do pedido.")
    seed = int(bid.get("seed") or random.randint(1, 10 ** 9))
    rng = random.Random(seed * 31 + 3)
    pct = grant_pct(p)
    asked = sorted({str(d)[:10] for d in (bid.get("days_off") or []) if str(d)[:7] == month})
    granted = [d for d in asked if rng.random() * 100 < pct]
    params = {"airline": p["airline"], "kind": "mensal", "month": month, "mode": p.get("mode") or "tripulante",
              "aircraft_mode": "type_only", "icao_type": p["icao_type"], "base": p["base"], "seed": seed,
              "requested_off": granted, "avoid_madrugada": bool(bid.get("avoid_madrugada")),
              "time_pref": bid.get("time_pref") or "",
              "preferred_airports": [a.strip().upper() for a in (bid.get("prefer") or []) if a.strip()],
              "avoid_airports": [a.strip().upper() for a in (bid.get("avoid") or []) if a.strip()],
              "name": f"{p['name'] or 'Minha escala'} — pedido {month[5:]}/{month[:4]}"}
    return params, {"grant_pct": pct, "asked": asked, "granted": granted,
                    "denied": [d for d in asked if d not in granted], "seed": seed}


def report(sched: dict, bid: dict, concession: dict) -> dict:
    """Pedido × escala gerada."""
    legs = sched["legs"]
    days_with_legs = {l["local_date"] for l in legs}
    planned_off = set((sched.get("params") or {}).get("planned_days_off") or [])
    off_ok = [d for d in concession["granted"] if d not in days_with_legs]
    duties = (sched.get("crew") or {}).get("duties") or []
    madr = sum(1 for d in duties if d.get("madrugada"))
    pref = set(a.upper() for a in (bid.get("prefer") or []))
    visited = sorted({l["ades"] for l in legs} & pref)
    avoid = set(a.upper() for a in (bid.get("avoid") or []))
    hit_avoid = sorted({l["ades"] for l in legs} & avoid)
    tp = bid.get("time_pref") or ""
    in_period = None
    if tp and duties:
        ok = sum(1 for d in duties if (int(d["report_local"][:2]) < 12) == (tp == "manha"))
        in_period = round(100 * ok / len(duties))
    items = [
        {"label": "Folgas pedidas", "value": f"{len(off_ok)} de {len(concession['asked'])} atendidas",
         "detail": (f"concessão por antiguidade: {concession['grant_pct']}%"
                    + (f"; não atendidas: {', '.join(d[8:] + '/' + d[5:7] for d in concession['denied'])}"
                       if concession["denied"] else "")), "ok": len(off_ok) == len(concession["asked"])},
    ]
    if bid.get("avoid_madrugada"):
        items.append({"label": "Evitar madrugada", "value": f"{madr} jornada(s) com madrugada", "ok": madr == 0,
                      "detail": "jornadas que tocam 00:00–06:00 locais"})
    if tp:
        items.append({"label": "Período preferido", "value": f"{in_period}% das jornadas {'de manhã' if tp == 'manha' else 'à tarde'}",
                      "ok": (in_period or 0) >= 70, "detail": "pela hora de apresentação"})
    if pref:
        items.append({"label": "Destinos preferidos", "value": f"{len(visited)} de {len(pref)} visitados",
                      "detail": ", ".join(visited) or "nenhum", "ok": len(visited) > 0})
    if avoid:
        items.append({"label": "Destinos evitados", "value": "nenhum na escala" if not hit_avoid else ", ".join(hit_avoid),
                      "ok": not hit_avoid, "detail": ""})
    return {"items": items, "days_off_planned": len(planned_off), "legs": len(legs)}


def save_bid(db, sid: int, bid: dict, concession: dict, rep: dict):
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
               (f"bid:{sid}", json.dumps({"bid": bid, "concession": concession, "report": rep, "at": now_iso()},
                                         ensure_ascii=False)))


def bids(db) -> list[dict]:
    out = []
    for r in rows(db.execute("SELECT key, value FROM meta WHERE key LIKE 'bid:%' ORDER BY key DESC")):
        sid = int(r["key"].split(":")[1])
        s = row(db.execute("SELECT id, name, start_date FROM schedules WHERE id=?", (sid,)))
        if s:
            out.append({**json.loads(r["value"]), "schedule": s})
    return sorted(out, key=lambda x: x["at"], reverse=True)[:12]
