"""Pontualidade real por voo: atraso de partida e cancelamentos, para prever e simular a operação.

Fontes:
* ADS-B (embutido): atraso estimado = (primeiro ponto do avião em solo/baixa altitude) − (EOBT do RPL) − táxi.
  Em 26 dias de outubro/2026 a mediana desse intervalo foi 12 min (o táxi), P90 59 min. É uma estimativa: não
  separa espera no solo de atraso, e não vê cancelamentos.
* ANAC VRA (Voo Regular Ativo, dados abertos), importado pelo usuário: horários previsto e realizado oficiais e
  situação do voo (realizado/cancelado). Quando há dados do VRA para o voo, eles têm prioridade.

Perfil: por companhia, quantis do atraso de partida (P5…P95), % no horário (≤ 15 min), % cancelado e n, por
voo (número + trecho), por aeroporto de origem e geral (reserva quando o voo tem poucos dados).
"""
from __future__ import annotations

import csv
import io
import json
import os
import random
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta

from .db import now_iso, rows

BUNDLED = os.path.join(os.path.dirname(__file__), "data", "delays_observed.json")
QS = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)
TAXI_OUT_MIN = 12          # mediana observada entre o EOBT e o primeiro ponto em solo no ADS-B
ON_TIME_MIN = 15           # padrão da aviação: até 15 min de atraso conta como no horário
MIN_N_FLIGHT = 4
MIN_N_AIRPORT = 25


def _digits(ident: str | None) -> str:
    m = re.search(r"(\d+)$", ident or "")
    return str(int(m.group(1))) if m else ""


def key(airline: str, ident: str | None, adep: str, ades: str) -> str:
    return f"{airline}{_digits(ident)}:{adep}-{ades}"


def _quantiles(v: list[float]) -> list[int]:
    s = sorted(v)
    return [int(round(s[min(len(s) - 1, int(q * len(s)))])) for q in QS]


def _summ(delays: list[float], cancelled: int = 0) -> dict:
    n = len(delays)
    out = {"n": n, "c": cancelled}
    if n:
        out["q"] = _quantiles(delays)
        out["ot"] = round(100 * sum(1 for x in delays if x <= ON_TIME_MIN) / n)
    return out


def _build(samples: dict, source: str, days: list[str]) -> dict:
    """samples: {airline: {"f": {key: [delays]}, "ap": {adep: [...]}, "all": [...], "cf": {key: n}, ...}}"""
    res = {"source": source, "generated": now_iso(), "days": days, "airlines": {}}
    for al, d in samples.items():
        rec = {"flights": {}, "airports": {}, "all": _summ(d["all"], d.get("call", 0))}
        for k, v in d["f"].items():
            if len(v) + d["cf"].get(k, 0) >= 2:
                rec["flights"][k] = _summ(v, d["cf"].get(k, 0))
        for ap, v in d["ap"].items():
            if len(v) >= MIN_N_AIRPORT:
                rec["airports"][ap] = _summ(v, d["cap"].get(ap, 0))
        res["airlines"][al] = rec
    return res


def _new():
    return {"f": defaultdict(list), "ap": defaultdict(list), "all": [], "cf": defaultdict(int), "cap": defaultdict(int),
            "call": 0}


# ------------------------------------------------------------------ ADS-B
def build_from_adsb(db) -> dict:
    samples: dict = defaultdict(_new)
    days = sorted(r["day"] for r in rows(db.execute("SELECT day FROM adsb_days")))
    for r in rows(db.execute(
            "SELECT l.airline, l.callsign, l.adep, l.ades, l.dep_utc, l.seen_from, r.alt0 FROM adsb_legs l"
            " JOIN adsb_runs r ON r.hex=l.hex AND r.t0=l.seen_from WHERE l.source='rpl'")):
        if r["alt0"] is None or r["alt0"] > 1500:
            continue   # o avião só apareceu já voando: não dá para medir a partida
        dep = datetime.fromisoformat(r["dep_utc"].replace("Z", "+00:00"))
        seen = datetime.fromisoformat(r["seen_from"].replace("Z", "+00:00"))
        delay = (seen - dep).total_seconds() / 60 - TAXI_OUT_MIN
        if not -30 <= delay <= 360:
            continue
        s = samples[r["airline"]]
        s["f"][key(r["airline"], r["callsign"], r["adep"], r["ades"])].append(delay)
        s["ap"][r["adep"]].append(delay)
        s["all"].append(delay)
    return _build(samples, "adsb.lol (estimado: primeiro ponto em solo − EOBT − táxi)", days)


# ------------------------------------------------------------------ ANAC VRA
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


_COLS = {   # campo: palavras que precisam aparecer no cabeçalho (normalizado)
    "airline": [("icao", "empresa"), ("sigla", "empresa"), ("empresa", "aerea", "icao")],
    "number": [("numero", "voo"), ("n", "voo")],
    "adep": [("origem", "icao"), ("origem", "sigla"), ("aerodromo", "origem"), ("aeroporto", "origem")],
    "ades": [("destino", "icao"), ("destino", "sigla"), ("aerodromo", "destino"), ("aeroporto", "destino")],
    "sched_dep": [("partida", "prevista")],
    "act_dep": [("partida", "real")],
    "sched_arr": [("chegada", "prevista")],
    "act_arr": [("chegada", "real")],
    "status": [("situacao", "voo")],
    "just": [("justificativa",)],
}


def _map_header(header: list[str]) -> dict[str, int]:
    nh = [_norm(h) for h in header]
    out = {}
    for field, alts in _COLS.items():
        for words in alts:
            for i, h in enumerate(nh):
                toks = h.split()
                if i in out.values():
                    continue
                if all(w in toks for w in words) and not (field == "airline" and "descricao" in toks):
                    # "Descrição Aeroporto Origem" não é o código ICAO
                    if field in ("adep", "ades") and "descricao" in toks:
                        continue
                    out[field] = i
                    break
            if field in out:
                break
    return out


_DT_FORMATS = ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
               "%d/%m/%y %H:%M")


def _dt(s: str):
    s = (s or "").strip()
    if not s:
        return None
    for f in _DT_FORMATS:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            pass
    return None


def parse_vra(data: bytes) -> tuple[list[dict], dict]:
    """Lê um CSV do VRA (vários layouts da ANAC). Devolve (registros, informações)."""
    for enc in ("utf-8-sig", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    lines = text.splitlines()
    # pula linhas de título antes do cabeçalho (alguns arquivos trazem "Atualizado em ...")
    start = next((i for i, l in enumerate(lines[:20]) if "partida" in _norm(l) and "prevista" in _norm(l)), None)
    if start is None:
        raise ValueError("não parece um arquivo do VRA da ANAC (não achei as colunas Partida Prevista/Real)")
    sample = "\n".join(lines[start:start + 5])
    sep = ";" if sample.count(";") >= sample.count(",") else ","
    rd = csv.reader(io.StringIO("\n".join(lines[start:])), delimiter=sep)
    header = next(rd)
    cols = _map_header(header)
    need = ("airline", "number", "adep", "ades", "sched_dep", "status")
    miss = [k for k in need if k not in cols]
    if miss:
        raise ValueError(f"colunas não encontradas no VRA: {', '.join(miss)}")
    out, bad = [], 0
    for r in rd:
        if len(r) < len(header) - 2:
            continue
        g = lambda k: (r[cols[k]].strip() if k in cols and cols[k] < len(r) else "")  # noqa: E731
        sd = _dt(g("sched_dep"))
        if not sd:
            bad += 1
            continue
        st = _norm(g("status"))
        out.append({"airline": g("airline").upper(), "number": g("number"), "adep": g("adep").upper(),
                    "ades": g("ades").upper(), "sched_dep": sd, "act_dep": _dt(g("act_dep")),
                    "cancelled": "cancel" in st, "status": g("status"), "just": g("just")})
    info = {"records": len(out), "skipped": bad, "separator": sep, "columns": {k: header[v] for k, v in cols.items()}}
    if out:
        ds = sorted(x["sched_dep"].date() for x in out)
        info["from"], info["to"] = ds[0].isoformat(), ds[-1].isoformat()
    return out, info


def build_from_vra(records: list[dict], airlines=("GLO", "TAM", "AZU")) -> dict:
    samples: dict = defaultdict(_new)
    days = set()
    for x in records:
        al = x["airline"]
        if airlines and al not in airlines:
            continue
        k = key(al, x["number"], x["adep"], x["ades"])
        s = samples[al]
        days.add(x["sched_dep"].date().isoformat())
        if x["cancelled"]:
            s["cf"][k] += 1
            s["cap"][x["adep"]] += 1
            s["call"] += 1
            continue
        if not x["act_dep"]:
            continue
        delay = (x["act_dep"] - x["sched_dep"]).total_seconds() / 60
        if not -60 <= delay <= 720:
            continue
        s["f"][k].append(delay)
        s["ap"][x["adep"]].append(delay)
        s["all"].append(delay)
    return _build(samples, "ANAC VRA (horários previsto e realizado oficiais)", sorted(days))


# ------------------------------------------------------------------ perfil em uso
def bundled() -> dict:
    if not hasattr(bundled, "_c"):
        try:
            with open(BUNDLED, encoding="utf-8") as fh:
                bundled._c = json.load(fh)
        except (OSError, ValueError):
            bundled._c = {"airlines": {}}
    return bundled._c


def get_vra(db) -> dict | None:
    r = db.execute("SELECT value FROM meta WHERE key='delay_vra'").fetchone()
    return json.loads(r[0]) if r else None


def save_vra(db, prof: dict):
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('delay_vra', ?)",
               (json.dumps(prof, ensure_ascii=False, separators=(",", ":")),))


class Punctuality:
    """Consulta: VRA (se importado) com prioridade, ADS-B embutido como reserva."""

    def __init__(self, db):
        self.vra = get_vra(db)
        self.adsb = bundled()

    def stats(self, airline: str, ident: str | None, adep: str, ades: str) -> dict | None:
        k = key(airline, ident, adep, ades)
        for prof, src in ((self.vra, "ANAC VRA"), (self.adsb, "ADS-B")):
            if not prof:
                continue
            rec = (prof.get("airlines") or {}).get(airline)
            if not rec:
                continue
            f = rec["flights"].get(k)
            if f and f["n"] + f.get("c", 0) >= MIN_N_FLIGHT:
                return {**f, "source": src, "level": "voo"}
        for prof, src in ((self.vra, "ANAC VRA"), (self.adsb, "ADS-B")):
            rec = ((prof or {}).get("airlines") or {}).get(airline)
            if rec and adep in rec["airports"]:
                return {**rec["airports"][adep], "source": src, "level": f"aeroporto {adep}"}
        for prof, src in ((self.vra, "ANAC VRA"), (self.adsb, "ADS-B")):
            rec = ((prof or {}).get("airlines") or {}).get(airline)
            if rec and rec["all"].get("n"):
                return {**rec["all"], "source": src, "level": "companhia"}
        return None


def sample_delay(st: dict, rng: random.Random) -> int:
    """Sorteia um atraso (min) da distribuição empírica (quantis interpolados, cauda exponencial)."""
    q = st.get("q")
    if not q:
        return 0
    u = rng.random()
    if u < QS[0]:
        return int(q[0] - rng.random() * 5)
    if u > QS[-1]:
        return int(q[-1] + rng.expovariate(1 / 25))
    for i in range(len(QS) - 1):
        if QS[i] <= u <= QS[i + 1]:
            t = (u - QS[i]) / (QS[i + 1] - QS[i])
            return int(round(q[i] + t * (q[i + 1] - q[i])))
    return int(q[3])


def describe(st: dict | None) -> dict | None:
    if not st:
        return None
    n, c = st.get("n", 0), st.get("c", 0)
    return {"source": st["source"], "level": st["level"], "n": n,
            "on_time_pct": st.get("ot"), "median_delay": st["q"][3] if st.get("q") else None,
            "p90_delay": st["q"][5] if st.get("q") else None,
            "cancel_pct": round(100 * c / (n + c), 1) if (n + c) and c else (0.0 if st["source"] == "ANAC VRA" else None)}


# ------------------------------------------------------------------ simulação sobre uma escala
def simulate(db, sid: int, seed: int | None = None) -> dict:
    """Sorteia atraso e cancelamento de cada perna pela pontualidade real e propaga o atraso em cascata.
    Grava sim_* nas pernas (não altera o horário planejado)."""
    from . import catalog, crew
    from .scheduler import fmt_utc, parse_utc, _settings_turn
    sched = rows(db.execute("SELECT * FROM schedules WHERE id=?", (sid,)))
    if not sched:
        raise LookupError("Escala não encontrada.")
    sched = sched[0]
    params = json.loads(sched["params"] or "{}")
    legs = rows(db.execute("SELECT * FROM schedule_legs WHERE schedule_id=? ORDER BY dep_utc", (sid,)))
    min_turn, _mx = _settings_turn(db, {"params": params})
    seed = int(seed if seed is not None else random.randint(1, 10 ** 9))
    rng = random.Random(seed)
    pq = Punctuality(db)
    apmap = catalog.airport_map(db)
    prev_arr = prev_l = None
    out = {"seed": seed, "legs": 0, "on_time": 0, "delayed": 0, "cancelled": 0, "delay_total": 0, "propagated": 0,
           "duty_exceeded": []}
    sim: dict[int, tuple] = {}
    for l in legs:
        if l.get("activity") == "EXTRA":
            # extra: o tripulante viaja de passageiro; atraso do voo dele também empurra o resto
            pass
        dep, arr = parse_utc(l["dep_utc"]), parse_utc(l["arr_utc"])
        st = pq.stats(l["airline"], l["flight_number"], l["adep"], l["ades"])
        canc_p = 0.0
        if st and (st.get("n", 0) + st.get("c", 0)):
            canc_p = st.get("c", 0) / (st["n"] + st.get("c", 0))
        if rng.random() < canc_p:
            sim[l["id"]] = (None, None, None, "cancelado")
            out["cancelled"] += 1
            out["legs"] += 1
            continue
        own = sample_delay(st, rng) if st else 0
        sdep = dep + timedelta(minutes=max(own, -10))
        if prev_arr is not None and prev_l is not None and l["adep"] == prev_l["ades"] and \
                (dep - parse_utc(prev_l["arr_utc"])) < timedelta(hours=8):
            ready = prev_arr + timedelta(minutes=min_turn)
            if ready > sdep:
                sdep = ready
                out["propagated"] += 1
        delay = int((sdep - dep).total_seconds() // 60)
        sarr = arr + (sdep - dep)
        status = "atrasado" if delay > ON_TIME_MIN else "no_horario"
        sim[l["id"]] = (sdep, sarr, delay, status)
        out["legs"] += 1
        out["on_time" if status == "no_horario" else "delayed"] += 1
        out["delay_total"] += max(0, delay)
        prev_arr, prev_l = sarr, l
    # jornada do tripulante com os horários simulados
    rb, ra = int(params.get("report_before") or 30), int(params.get("release_after") or 30)
    mode = params.get("crew_mode", "rbac117")
    duty: list = []

    def close(dl):
        if not dl or mode == "off":
            return
        rep = dl[0][1] - timedelta(minutes=rb)
        rel = dl[-1][2] + timedelta(minutes=ra)
        tz = catalog.airport_tz(apmap, dl[0][0]["adep"])
        from .airports import to_local
        lim = crew.limits(to_local(rep, tz), sum(1 for x in dl if x[0].get("activity") != "EXTRA"), mode)
        h = (rel - rep).total_seconds() / 3600
        if h > lim["fdp_h"] + 1e-6:
            out["duty_exceeded"].append({"date": to_local(rep, tz).date().isoformat(), "duty_h": round(h, 1),
                                         "limit_h": lim["fdp_h"], "last_leg": dl[-1][0]["flight_number"]})
    for l in legs:
        sdep, sarr, _d, status = sim.get(l["id"], (None, None, None, ""))
        if status == "cancelado" or sdep is None:
            continue
        if duty and (sdep - duty[-1][2]) >= timedelta(hours=8):
            close(duty)
            duty = []
        duty.append((l, sdep, sarr))
    close(duty)
    for lid, (sdep, sarr, delay, status) in sim.items():
        db.execute("UPDATE schedule_legs SET sim_dep_utc=?, sim_arr_utc=?, sim_delay_min=?, sim_status=? WHERE id=?",
                   (fmt_utc(sdep) if sdep else None, fmt_utc(sarr) if sarr else None, delay, status, lid))
    n_ok = out["on_time"] + out["delayed"]
    out["on_time_pct"] = round(100 * out["on_time"] / n_ok) if n_ok else None
    out["avg_delay"] = round(out["delay_total"] / n_ok, 1) if n_ok else None
    p = dict(params)
    p["delay_sim"] = {k: out[k] for k in ("seed", "legs", "on_time_pct", "avg_delay", "cancelled", "propagated")} | \
        {"duty_exceeded": len(out["duty_exceeded"]), "at": now_iso()}
    db.execute("UPDATE schedules SET params=? WHERE id=?", (json.dumps(p, ensure_ascii=False, default=str), sid))
    return out


def clear_simulation(db, sid: int):
    db.execute("UPDATE schedule_legs SET sim_dep_utc=NULL, sim_arr_utc=NULL, sim_delay_min=NULL, sim_status=''"
               " WHERE schedule_id=?", (sid,))
    r = rows(db.execute("SELECT params FROM schedules WHERE id=?", (sid,)))
    if r:
        p = json.loads(r[0]["params"] or "{}")
        p.pop("delay_sim", None)
        db.execute("UPDATE schedules SET params=? WHERE id=?", (json.dumps(p, ensure_ascii=False, default=str), sid))


def summary(db) -> dict:
    """Tela Dados ADS-B: pontualidade por companhia nas duas fontes."""
    out = {}
    for name, prof in (("adsb", bundled()), ("vra", get_vra(db))):
        if not prof:
            out[name] = None
            continue
        out[name] = {"source": prof.get("source"), "days": len(prof.get("days", [])),
                     "first_day": (prof.get("days") or [None])[0], "last_day": (prof.get("days") or [None])[-1],
                     "airlines": {al: {"flights": len(r["flights"]), "n": r["all"].get("n", 0),
                                       "on_time_pct": r["all"].get("ot"),
                                       "median_delay": (r["all"].get("q") or [None] * 7)[3],
                                       "p90_delay": (r["all"].get("q") or [None] * 7)[5],
                                       "cancel_pct": round(100 * r["all"].get("c", 0) / max(1, r["all"].get("n", 0) + r["all"].get("c", 0)), 1)}
                                  for al, r in (prof.get("airlines") or {}).items()}}
    return out
