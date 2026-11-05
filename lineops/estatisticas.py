"""Estatísticas da carreira e o envio das escalas geradas para a webhook do projeto.

* compute(): números da escala (o que foi programado) e do diário (o que você voou), no período: aeroportos mais usados,
  rotas, voos que mais se repetem, cidades de pernoite, horário das partidas, horas, dias de voo. Tudo contado das
  pernas e do diário; nada estimado.
* send_generated(): cada escala gerada pelo motor vai para a webhook do projeto (pedido do dono da instalação, para
  estatística). Vai só o resumo da escala, sem nome de usuário, senha, e-mail ou IP: companhia, tipo, base, período,
  pernas, horas, aeroportos e rotas mais usados, a versão da LineOps e um código aleatório desta instalação (para contar
  instalações sem saber de quem é). Dá para desligar em Ajustes › Estatísticas; desligado, nada sai.
"""
from __future__ import annotations

import logging
import threading
import uuid
from collections import Counter
from datetime import date

from . import __version__, journey, rules
from .db import now_iso, one, rows, tx

log = logging.getLogger("lineops.estatisticas")
SHARE_KEY = "estatisticas_envio"           # "1" (padrão) ou "0"
INSTALL_KEY = "instalacao_id"
WEBHOOK = None                             # a mesma webhook padrão do aviso de pernoite (pernoite.DEFAULT_WEBHOOK, só no instalador)


def _ap(conn, icao: str) -> dict:
    a = rules.airport(conn, icao) or {}
    return {"icao": icao, "iata": a.get("iata"), "city": a.get("city") or a.get("name") or icao}


def _top(counter: Counter, n: int, conn=None, kind: str = "ap") -> list[dict]:
    out = []
    for k, v in counter.most_common(n):
        if kind == "ap":
            out.append({**_ap(conn, k), "n": v})
        elif kind == "route":
            a, b = k
            out.append({"a": _ap(conn, a), "b": _ap(conn, b), "n": v})
        else:
            out.append({"key": k, "n": v})
    return out


def legs_summary(conn, legs: list[dict], top: int = 10) -> dict:
    fl = [l for l in legs if (l.get("activity") or "") not in ("SOBREAVISO",)]
    op = [l for l in fl if (l.get("activity") or "") != "EXTRA"]
    aps, deps, arrs, routes, idents, hours = Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
    block = 0
    longest = None
    for l in op:
        aps[l["adep"]] += 1
        aps[l["ades"]] += 1
        deps[l["adep"]] += 1
        arrs[l["ades"]] += 1
        routes[tuple(sorted((l["adep"], l["ades"])))] += 1
        if l.get("flight_number"):
            idents[l["flight_number"]] += 1
        d, a = journey.parse(l["dep_utc"]), journey.parse(l["arr_utc"])
        if d and a:
            m = int((a - d).total_seconds() // 60)
            block += m
            if not longest or m > longest["min"]:
                longest = {"min": m, "flight_number": l.get("flight_number"), "adep": l["adep"], "ades": l["ades"]}
        lt = journey.local_time(conn, l["dep_utc"], l["adep"])
        if lt:
            hours[int(lt["hhmm"][:2])] += 1
    days = Counter(l.get("local_date") or l["dep_utc"][:10] for l in op)
    busiest = days.most_common(1)[0] if days else None
    origins = Counter(l.get("data_origin") for l in op)
    return {"legs": len(op), "extra": len(fl) - len(op), "block_min": block, "flight_days": len(days),
            "legs_per_day": round(len(op) / len(days), 2) if days else None,
            "airports": len(aps), "top_airports": _top(aps, top, conn), "top_departures": _top(deps, 5, conn),
            "top_arrivals": _top(arrs, 5, conn), "top_routes": _top(routes, top, conn, "route"),
            "top_flights": _top(idents, 8, kind="flight"), "by_hour": [hours.get(h, 0) for h in range(24)],
            "longest": longest, "busiest_day": {"date": busiest[0], "legs": busiest[1]} if busiest else None,
            "projected_pct": round(100 * origins.get("PROJETADO", 0) / len(op)) if op else None,
            "intl": sum(1 for l in op if l.get("data_origin") == "OBSERVADO")}


def _overnights(conn, start: str, end: str) -> list[dict]:
    from . import visao
    cnt = Counter()
    d = date.fromisoformat(start).replace(day=1)
    last = date.fromisoformat(end)
    while d <= last:
        for x in visao.month(conn, d.strftime("%Y-%m"))["days"]:
            if start <= x["date"] <= end and x.get("overnight_at"):
                cnt[x["overnight_at"]] += 1
        d = date(d.year + (d.month // 12), d.month % 12 + 1, 1)
    return _top(cnt, 8, conn)


def compute(conn, start: str | None = None, end: str | None = None) -> dict:
    from . import escala
    b = journey.bounds(conn)
    start = start or b.get("first") or now_iso()[:10]
    end = end or b.get("last") or start
    roster = legs_summary(conn, escala.legs(conn, start, end))
    lg = rows(conn.execute("""SELECT date AS local_date, adep, ades, flight_number, out_utc AS dep_utc, in_utc AS arr_utc, 'REAL' AS data_origin,
                              '' AS activity, block_min FROM logbook_entry WHERE reversed_at IS NULL AND date BETWEEN ? AND ?""", (start, end)))
    for x in lg:                                  # no diário vale o bloco registrado (calço a calço)
        if not (x["dep_utc"] and x["arr_utc"]):
            x["dep_utc"] = x["arr_utc"] = f"{x['local_date']}T00:00:00Z"
    flown = legs_summary(conn, lg)
    flown["block_min"] = sum(x["block_min"] or 0 for x in lg)
    return {"period": {"start": start, "end": end}, "roster": roster, "flown": flown,
            "overnights": _overnights(conn, start, end) if roster["legs"] else [],
            "note": "Escala: as pernas programadas no período (sem sobreaviso; extra não conta como voo). Diário: os voos aprovados."}


# ------------------------------------------------------------------ envio das escalas geradas (estatística do projeto)
def share_enabled(core) -> bool:
    r = one(core.execute("SELECT value FROM setting WHERE key=?", (SHARE_KEY,)))
    return (r["value"] if r else "1") != "0"


def set_share(core, on: bool) -> bool:
    with tx(core):
        core.execute("INSERT INTO setting(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (SHARE_KEY, "1" if on else "0"))
    return on


def install_id(core) -> str:
    r = one(core.execute("SELECT value FROM setting WHERE key=?", (INSTALL_KEY,)))
    if r:
        return r["value"]
    v = uuid.uuid4().hex[:12]
    with tx(core):
        core.execute("INSERT OR IGNORE INTO setting(key, value) VALUES (?,?)", (INSTALL_KEY, v))
    return one(core.execute("SELECT value FROM setting WHERE key=?", (INSTALL_KEY,)))["value"]


def generated_payload(conn, airline: str, result: dict, params: dict, inst: str) -> dict:
    from . import escala
    s = legs_summary(conn, escala.legs(conn, result["start_date"], result["end_date"]), top=5)
    ap = lambda x: x["iata"] or x["icao"]                                        # noqa: E731
    fields = [
        {"name": "Companhia · tipo", "value": f"{airline} · {params.get('icao_type') or '—'}", "inline": True},
        {"name": "Base", "value": str(params.get("base") or "automática"), "inline": True},
        {"name": "Período", "value": f"{result['start_date'][8:10]}/{result['start_date'][5:7]} a {result['end_date'][8:10]}/{result['end_date'][5:7]}/{result['end_date'][:4]}"
                                     f" ({params.get('kind') or '—'})", "inline": True},
        {"name": "Pernas · horas", "value": f"{s['legs']} pernas · {s['block_min'] // 60}h{s['block_min'] % 60:02d} de bloco · {s['flight_days']} dias de voo", "inline": False},
        {"name": "Aeroportos mais usados", "value": ", ".join(f"{ap(x)} ({x['n']})" for x in s["top_airports"]) or "—", "inline": False},
        {"name": "Rotas mais usadas", "value": ", ".join(f"{ap(x['a'])}–{ap(x['b'])} ({x['n']})" for x in s["top_routes"][:3]) or "—", "inline": False},
        {"name": "Detalhes", "value": f"{s['airports']} aeroportos · {s['intl']} internacionais · {s['projected_pct'] if s['projected_pct'] is not None else '—'}% projetado"
                                      + (f" · mais longa {s['longest']['adep']}–{s['longest']['ades']} {s['longest']['min'] // 60}h{s['longest']['min'] % 60:02d}" if s.get("longest") else ""),
         "inline": False},
    ]
    return {"username": "LineOps · Estatísticas", "allowed_mentions": {"parse": []},
            "embeds": [{"title": f"Escala gerada · {airline}", "color": 0x6FA8DC, "fields": fields,
                        "footer": {"text": f"LineOps {__version__} · instalação {inst} · estatística anônima (sem nome de usuário)"}}]}


def send_generated(core_path, career_path, airline: str, result: dict, params: dict, opener=None, wait: bool = False):
    """Manda o resumo da escala gerada (em segundo plano: nunca atrasa nem derruba a geração)."""
    from . import pernoite
    from .db import connect

    def run():
        try:
            core = connect(core_path)
            try:
                if not share_enabled(core):
                    return
                inst = install_id(core)
            finally:
                core.close()
            cc = connect(career_path)
            try:
                body = generated_payload(cc, airline, result, params, inst)
            finally:
                cc.close()
            url = WEBHOOK or pernoite.DEFAULT_WEBHOOK
            if not url:                                      # rodando pelo código, sem a webhook do projeto: nada sai
                return
            code, raw = pernoite._http("POST", url, body, opener)
            if not 200 <= code < 300:
                log.warning("estatística da escala não enviada: HTTP %s", code)
        except Exception:                                    # noqa: BLE001 — estatística nunca atrapalha a escala
            log.exception("estatística da escala")
    if wait:
        run()
    else:
        threading.Thread(target=run, name="estatistica-escala", daemon=True).start()
