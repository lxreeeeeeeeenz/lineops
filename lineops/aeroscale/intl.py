"""Voos internacionais observados no ADS-B.

Os RPL do CGNA só trazem voos domésticos: nenhum dos 10.333 registros de AZU, GLO e TAM sai ou chega fora
do Brasil. Os voos internacionais das três companhias (GRU–SCL, GRU–MIA, VCP–MCO, GRU–LIS…) aparecem
nos dados ADS-B abertos do adsb.lol. Este módulo monta, a partir dessas observações, um catálogo de voos
internacionais reais: número do voo, origem, destino, tipo da aeronave, horário e dias em que foi visto.

* Origem/destino: aeroporto mais próximo do primeiro/último ponto em baixa altitude. Quando um lado não foi
  visto (cobertura sobre o oceano ou no exterior), usa o voo par de número vizinho (ex.: TAM8146 ?→LIS e
  TAM8147 LIS→GRU: o TAM8146 sai de GRU). Sem os dois lados, o voo fica de fora – nada é inventado.
* Horários: decolagem/pouso vistos no ADS-B (± táxi). Lado não visto: estimado pela distância
  (velocidade média de cruzeiro), e o voo fica marcado como "horário estimado".
* Dias: os dias da semana em que foi visto. Visto em pelo menos 75% dos dias lidos (mínimo 3 dias):
  considerado diário (frequência estimada).

Sem dados ADS-B no computador do usuário, vale o catálogo embutido (aeroscale/data/intl_observed.json),
gerado com o histórico do adsb.lol (ODbL).
"""
from __future__ import annotations

import json
import os
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from .airport_geo_world import WORLD_AIRPORTS
from .db import now_iso, rows
from .world_tz import tz_for

BUNDLED = os.path.join(os.path.dirname(__file__), "data", "intl_observed.json")
CRUISE_KMH = 820          # velocidade média para estimar duração quando um lado não foi visto
OVERHEAD_MIN = 40         # táxi, subida e descida
TAXI_OUT, TAXI_IN = 12, 8


def is_foreign(icao: str | None) -> bool:
    a = WORLD_AIRPORTS.get(icao or "")
    return bool(a) and a[4] != "BR"


def airport_info(icao: str) -> dict | None:
    a = WORLD_AIRPORTS.get(icao)
    if not a:
        return None
    iata, lat, lon, city, cc = a
    return {"icao": icao, "iata": iata or None, "name": city, "city": city, "uf": cc, "tz": tz_for(cc, lon),
            "lat": lat, "lon": lon, "source": "mundo"}


def _gc_km(a: str, b: str) -> float | None:
    from .adsb import haversine_km
    pa, pb = WORLD_AIRPORTS.get(a), WORLD_AIRPORTS.get(b)
    if not pa or not pb:
        return None
    return haversine_km(pa[1], pa[2], pb[1], pb[2])


def _num(ident: str) -> int | None:
    d = "".join(ch for ch in ident[3:] if ch.isdigit())
    return int(d) if d else None


def _parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _median_minute(values: list[int]) -> int:
    """Mediana de minutos do dia tratando a virada da meia-noite."""
    if not values:
        return 0
    ref = values[0]
    adj = [v if abs(v - ref) <= 720 else (v - 1440 if v > ref else v + 1440) for v in values]
    return int(statistics.median(adj)) % 1440


ENDPOINT_ALT_FT = 4000    # ponto abaixo disto (ou no solo) identifica o aeroporto
ENDPOINT_KM = 25


def _endpoint(lat, lon, alt):
    """Aeroporto do ponto em baixa altitude, só entre os da malha (Brasil: aeroportos do RPL; exterior:
    aeroportos grandes/médios). Evita atribuir pontos de subida a bases militares vizinhas."""
    from .adsb import haversine_km
    from .airport_geo import AIRPORT_GEO
    if lat is None or alt is None or alt > ENDPOINT_ALT_FT:
        return None
    best, bd = None, ENDPOINT_KM
    for icao, (_iata, la, lo, _c, cc) in WORLD_AIRPORTS.items():
        if abs(la - lat) > 0.4 or abs(lo - lon) > 0.4:
            continue
        if cc == "BR" and icao not in AIRPORT_GEO:
            continue
        dd = haversine_km(lat, lon, la, lo)
        if dd < bd:
            best, bd = icao, dd
    return best


def build_catalog(db) -> list[dict]:
    """Monta o catálogo a partir das tabelas do ADS-B (adsb_runs/adsb_aircraft) do próprio banco."""
    _nearest_airport = None  # noqa: F841 (substituído por _endpoint)
    days_total = db.execute("SELECT COUNT(*) c FROM adsb_days WHERE status='ok'").fetchone()["c"]
    if not days_total:
        return []
    rpl_idents = {r["ident"] for r in db.execute(
        "SELECT DISTINCT f.ident FROM rpl_flights f JOIN rpl_imports i ON i.id=f.import_id WHERE i.active=1")}
    types = {r["hex"]: r["icao_type"] for r in db.execute("SELECT hex, icao_type FROM adsb_aircraft")}
    obs: dict[tuple, list] = defaultdict(list)
    for r in rows(db.execute("SELECT * FROM adsb_runs ORDER BY t0")):
        cs = r["callsign"] or ""
        if len(cs) < 4 or cs in rpl_idents:
            continue
        t0, t1 = _parse(r["t0"]), _parse(r["t1"])
        if (t1 - t0).total_seconds() > 18 * 3600:
            continue
        dep = _endpoint(r["lat0"], r["lon0"], r["alt0"])
        arr = _endpoint(r["lat1"], r["lon1"], r["alt1"])
        if dep and dep == arr:
            continue
        obs[(cs[:3], cs)].append({"dep": dep, "arr": arr, "t0": t0, "t1": t1, "type": types.get(r["hex"]),
                                  "dep_ground": r["alt0"] == 0, "arr_ground": r["alt1"] == 0})
    cat: dict[tuple, dict] = {}
    for (al, cs), lst in obs.items():
        deps = Counter(o["dep"] for o in lst if o["dep"])
        arrs = Counter(o["arr"] for o in lst if o["arr"])
        cat[(al, cs)] = {"airline": al, "ident": cs, "adep": deps.most_common(1)[0][0] if deps else None,
                         "ades": arrs.most_common(1)[0][0] if arrs else None, "obs": lst}
    # completa um lado pelo voo par: ida com número par, volta com o ímpar seguinte (8082/8083, 8146/8147).
    # Usa só lados OBSERVADOS do par (nunca um lado que também foi deduzido).
    observed = {k: (f["adep"], f["ades"]) for k, f in cat.items()}
    for (al, cs), f in cat.items():
        n = _num(cs)
        if n is None:
            continue
        m = n + 1 if n % 2 == 0 else n - 1
        key = next((k for k in cat if k[0] == al and _num(k[1]) == m and k[1][:3] == cs[:3]), None)
        if not key:
            continue
        p_dep, p_arr = observed[key]
        if not f["adep"] and f["ades"] and p_dep == f["ades"] and p_arr:
            f["adep"], f["endpoint_note"] = p_arr, f"origem pelo voo par {key[1]}"
        if not f["ades"] and f["adep"] and p_arr == f["adep"] and p_dep:
            f["ades"], f["endpoint_note"] = p_dep, f"destino pelo voo par {key[1]}"
    out = []
    for f in cat.values():
        if not f["adep"] or not f["ades"] or f["adep"] == f["ades"]:
            continue
        if not (is_foreign(f["adep"]) or is_foreign(f["ades"])):
            continue  # só internacionais (voo doméstico fora do RPL é extra/fretamento)
        lst = f["obs"]
        tc0 = Counter(o["type"] for o in lst if o["type"])
        fam_types = {"A318", "A319", "A320", "A321", "A19N", "A20N", "A21N", "B737", "B738", "B739", "B37M", "B38M",
                     "B39M", "E190", "E195", "E290", "E295", "AT72", "AT75", "AT76"}
        if tc0 and tc0.most_common(1)[0][0] in fam_types and (_gc_km(f["adep"], f["ades"]) or 0) > 7000:
            continue  # além do alcance de um narrowbody: origem/destino mal identificados
        # duração pela distância (o transponder fica ligado no pátio: o tempo visto inclui solo)
        km = _gc_km(f["adep"], f["ades"]) or 0
        block = int(km / CRUISE_KMH * 60 + OVERHEAD_MIN)
        dep_mins, dates = [], set()
        anchors = Counter()
        for o in lst:
            if o["dep"] == f["adep"]:   # partida vista: no ar logo após decolar, ou ainda no solo
                t = o["t0"] + timedelta(minutes=10) if o["dep_ground"] else o["t0"] - timedelta(minutes=TAXI_OUT + 3)
                anchors["partida"] += 1
            elif o["arr"] == f["ades"]:  # chegada vista
                t_in = o["t1"] if o["arr_ground"] else o["t1"] + timedelta(minutes=TAXI_IN)
                t = t_in - timedelta(minutes=block)
                anchors["chegada"] += 1
            else:
                continue
            dep_mins.append(t.hour * 60 + t.minute)
            dates.add(t.date())
        times = ("partida vista no ADS-B" if anchors["partida"] >= anchors["chegada"] else "chegada vista no ADS-B") \
            + "; duração estimada pela distância"
        if not dep_mins:
            continue
        seen = len(dates)
        daily = days_total >= 3 and seen >= 0.75 * days_total
        wd = "0123456" if daily else "".join(sorted({str(x.weekday()) for x in dates}))
        tc = Counter(o["type"] for o in lst if o["type"])
        out.append({"airline": f["airline"], "ident": f["ident"], "adep": f["adep"], "ades": f["ades"],
                    "type_icao": tc.most_common(1)[0][0] if tc else None, "dep_min": _median_minute(dep_mins),
                    "block_min": block, "weekdays": wd, "days_seen": seen, "days_total": days_total,
                    "first_seen": min(dates).isoformat(), "last_seen": max(dates).isoformat(),
                    "times_source": times, "endpoints_source": f.get("endpoint_note") or "observado",
                    "freq_source": f"diário (visto em {seen} de {days_total} dias)" if daily else
                    f"visto em {seen} de {days_total} dia(s)"})
    out.sort(key=lambda x: (x["airline"], x["ident"]))
    return out


def save_catalog(db, items: list[dict], source: str) -> int:
    """Grava o catálogo. Voos do ADS-B do usuário substituem os embutidos com o mesmo número."""
    ts = now_iso()
    if source == "adsb":
        db.execute("DELETE FROM intl_flights WHERE source='adsb'")
    for f in items:
        db.execute("DELETE FROM intl_flights WHERE airline=? AND ident=?" + (" AND source='embutido'" if source == "adsb" else ""),
                   (f["airline"], f["ident"]))
        db.execute("INSERT INTO intl_flights(airline, ident, adep, ades, type_icao, dep_min, block_min, weekdays,"
                   " days_seen, days_total, first_seen, last_seen, times_source, endpoints_source, freq_source, source,"
                   " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (f["airline"], f["ident"], f["adep"], f["ades"], f.get("type_icao"), f["dep_min"], f["block_min"],
                    f["weekdays"], f.get("days_seen"), f.get("days_total"), f.get("first_seen"), f.get("last_seen"),
                    f.get("times_source"), f.get("endpoints_source"), f.get("freq_source"), source, ts))
    return len(items)


def seed_bundled(db):
    """Na primeira execução, carrega o catálogo embutido (sem sobrescrever o que veio do ADS-B do usuário)."""
    if db.execute("SELECT COUNT(*) FROM intl_flights").fetchone()[0]:
        return
    if not os.path.exists(BUNDLED):
        return
    with open(BUNDLED, encoding="utf-8") as fh:
        data = json.load(fh)
    save_catalog(db, data.get("flights", []), "embutido")


def rebuild_from_adsb(db) -> dict:
    items = build_catalog(db)
    n = save_catalog(db, items, "adsb") if items else 0
    return {"flights": n, "total": db.execute("SELECT COUNT(*) c FROM intl_flights").fetchone()["c"]}


def list_flights(db, airline: str | None = None) -> list[dict]:
    q = "SELECT * FROM intl_flights WHERE active=1"
    args = []
    if airline:
        q += " AND airline=?"
        args.append(airline)
    return rows(db.execute(q + " ORDER BY airline, ident", args))


def foreign_airports(db) -> dict[str, dict]:
    out = {}
    for r in db.execute("SELECT DISTINCT adep a FROM intl_flights UNION SELECT DISTINCT ades FROM intl_flights"):
        if r[0] and is_foreign(r[0]):
            info = airport_info(r[0])
            if info:
                out[r[0]] = info
    return out


def occurrences(db, airline: str, d1: date, d2: date) -> list[dict]:
    """Ocorrências datadas no mesmo formato das do RPL (origin='OBSERVADO')."""
    out = []
    for f in list_flights(db, airline):
        d = d1
        while d <= d2:
            if str(d.weekday()) in (f["weekdays"] or ""):
                dep = datetime.combine(d, datetime.min.time(), timezone.utc) + timedelta(minutes=f["dep_min"])
                arr = dep + timedelta(minutes=f["block_min"])
                out.append({"ref": f"I:{f['ident']}:{d.isoformat()}", "flight_id": None, "intl_id": f["id"],
                            "airline": airline, "ident": f["ident"], "type_icao": f["type_icao"] or "",
                            "adep": f["adep"], "ades": f["ades"], "dep": dep, "arr": arr,
                            "eet_min": max(1, f["block_min"] - TAXI_OUT - TAXI_IN), "origin": "OBSERVADO",
                            "ref_date": None, "date": d.isoformat(), "times_source": f["times_source"]})
            d += timedelta(days=1)
    return out
