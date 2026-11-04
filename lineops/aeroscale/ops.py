"""Lógica operacional observada: onde cada companhia pernoita e qual voo a mesma aeronave faz em seguida.

Duas fontes, que se confirmam (medido em outubro/2026: os aeroportos com pernoite pelo RPL e pelo ADS-B
coincidem em ~90%; as diferenças são aeroportos do Norte/Nordeste com pouca cobertura de ADS-B):

* RPL (estrutura da malha): quantas aeronaves PRECISAM amanhecer em cada aeroporto. Se um aeroporto tem
  3 saídas antes da primeira chegada da manhã, 3 aeronaves dormiram lá. Conta feita por noite, só com os
  voos de tipo compatível com a aeronave da escala.
* ADS-B (adsb.lol, rodízio real): pernoites vistos por aeroporto e, para cada voo, qual voo a mesma
  matrícula fez em seguida, quantas vezes ele encerrou o dia (pernoite depois dele) e quantas vezes
  abriu o dia. É isso que separa bate-volta de verdade (G31854/G31855 na mesma aeronave) de uma ida e
  volta montada só porque os horários encaixam.

O perfil observado vem embutido (aeroscale/data/ops_observed.json) e é somado ao ADS-B que o usuário ler
em *Dados ADS-B* (dias que não estão no embutido).
"""
from __future__ import annotations

import bisect
import json
import os
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone

from . import catalog, predict
from .airports import get_tz
from .db import now_iso, rows

BUNDLED = os.path.join(os.path.dirname(__file__), "data", "ops_observed.json")
MAX_NEXT = 8            # sucessores guardados por voo (os mais vistos)
NIGHT_HOUR_CUT = 3      # corte da noite (hora local): aeronave no solo às 03:00 = pernoite naquele aeroporto
MIN_FAMILY_NIGHTS = 40  # abaixo disto, usa o total da companhia em vez do da família


def flight_key(ident: str | None, adep: str, ades: str) -> str:
    return f"{ident or '?'}:{adep}-{ades}"


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ------------------------------------------------------------------ perfil a partir do ADS-B
def build_profile(db, exclude_days: set[str] | None = None) -> dict:
    """Perfil observado a partir das pernas ADS-B do banco (adsb_legs)."""
    exclude_days = exclude_days or set()
    apmap = catalog.airport_map(db)
    tz_of = lambda icao: catalog.airport_tz(apmap, icao)  # noqa: E731
    days = sorted(r["day"] for r in rows(db.execute("SELECT day FROM adsb_days")) if r["day"] not in exclude_days)
    dayset = set(days)
    legs_by_hex: dict[str, list] = defaultdict(list)
    for r in rows(db.execute("SELECT l.*, a.icao_type FROM adsb_legs l LEFT JOIN adsb_aircraft a ON a.hex=l.hex"
                             " ORDER BY l.hex, l.dep_utc")):
        if r["day"] in dayset:
            legs_by_hex[r["hex"]].append(r)
    out: dict = {}

    def al_rec(al):
        return out.setdefault(al, {"days": len(days), "nights": {}, "arrivals": Counter(), "flights": {}})

    for _hx, legs in legs_by_hex.items():
        prev_kind = None
        for i, a in enumerate(legs):
            b = legs[i + 1] if i + 1 < len(legs) else None
            kind = None
            if b and a["ades"] and b["adep"] == a["ades"]:
                kind = predict.gap_kind(_parse(a["arr_utc"]), _parse(b["dep_utc"]), a["ades"], tz_of, 0)
            rec = al_rec(a["airline"])
            fam = catalog.type_family(a["icao_type"] or "") or "?"
            if kind in ("solo", "solo_longo", "solo_curto", "pernoite", "parada_noturna_curta"):
                rec["arrivals"][a["ades"]] += 1
            if kind in ("pernoite", "parada_noturna_curta"):
                n = rec["nights"].setdefault(a["ades"], {"n": 0, "fam": Counter()})
                n["n"] += 1
                n["fam"][fam] += 1
            if a["source"] == "rpl" and a["callsign"]:
                k = flight_key(a["callsign"], a["adep"], a["ades"])
                f = rec["flights"].setdefault(k, {"n": 0, "night": 0, "first": 0, "next": Counter()})
                if prev_kind in ("pernoite", "parada_noturna_curta"):
                    f["first"] += 1
                if kind in ("solo", "solo_longo", "solo_curto") and b["source"] == "rpl" and b["callsign"]:
                    f["n"] += 1
                    f["next"][flight_key(b["callsign"], b["adep"], b["ades"])] += 1
                elif kind in ("pernoite", "parada_noturna_curta"):
                    f["n"] += 1
                    f["night"] += 1
            prev_kind = kind if (b and b["adep"] == a["ades"]) else None
    for rec in out.values():
        rec["arrivals"] = dict(rec["arrivals"])
        for n in rec["nights"].values():
            n["fam"] = dict(n["fam"])
        for f in rec["flights"].values():
            f["next"] = dict(Counter(f["next"]).most_common(MAX_NEXT))
    return {"source": "adsb.lol (ODbL 1.0)", "generated": now_iso(), "days": days, "airlines": out}


def merge(a: dict, b: dict) -> dict:
    """Soma dois perfis (dias diferentes)."""
    res = json.loads(json.dumps(a))
    res["days"] = sorted(set(a.get("days", [])) | set(b.get("days", [])))
    for al, rb in (b.get("airlines") or {}).items():
        ra = res["airlines"].setdefault(al, {"days": 0, "nights": {}, "arrivals": {}, "flights": {}})
        ra["days"] = ra.get("days", 0) + rb.get("days", 0)
        for ap, n in rb["nights"].items():
            t = ra["nights"].setdefault(ap, {"n": 0, "fam": {}})
            t["n"] += n["n"]
            for fm, v in n["fam"].items():
                t["fam"][fm] = t["fam"].get(fm, 0) + v
        for ap, v in rb["arrivals"].items():
            ra["arrivals"][ap] = ra["arrivals"].get(ap, 0) + v
        for k, f in rb["flights"].items():
            t = ra["flights"].setdefault(k, {"n": 0, "night": 0, "first": 0, "next": {}})
            for fld in ("n", "night", "first"):
                t[fld] += f[fld]
            nx = Counter(t["next"])
            nx.update(f["next"])
            t["next"] = dict(nx.most_common(MAX_NEXT))
    return res


def bundled() -> dict:
    if not hasattr(bundled, "_cache"):
        try:
            with open(BUNDLED, encoding="utf-8") as fh:
                bundled._cache = json.load(fh)
        except (OSError, ValueError):
            bundled._cache = {"days": [], "airlines": {}}
    return bundled._cache


def get_profile(db) -> dict:
    """Perfil em uso: embutido + o ADS-B lido pelo usuário (gravado por rebuild_from_adsb)."""
    r = db.execute("SELECT value FROM meta WHERE key='ops_profile'").fetchone()
    if r:
        try:
            return json.loads(r[0])
        except ValueError:
            pass
    return bundled()


def rebuild_from_adsb(db) -> dict:
    """Recalcula o perfil com o ADS-B do banco (só os dias que o embutido não tem) e grava."""
    base = bundled()
    mine = build_profile(db, exclude_days=set(base.get("days", [])))
    prof = merge(base, mine) if mine["days"] else base
    if mine["days"]:
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('ops_profile', ?)",
                   (json.dumps(prof, ensure_ascii=False, separators=(",", ":")),))
    return {"days_bundled": len(base.get("days", [])), "days_added": len(mine["days"]),
            "days_total": len(prof.get("days", []))}


# ------------------------------------------------------------------ consultas
class Ops:
    """Consultas de uma companhia/família ao perfil observado."""

    def __init__(self, profile: dict, airline: str, family: str | None):
        self.rec = (profile.get("airlines") or {}).get(airline) or {"days": 0, "nights": {}, "arrivals": {}, "flights": {}}
        self.days = max(1, int(self.rec.get("days") or 0)) if self.rec.get("days") else 0
        self.family = family
        fam_total = sum((n.get("fam") or {}).get(family, 0) for n in self.rec["nights"].values()) if family else 0
        self.use_family = bool(family) and fam_total >= MIN_FAMILY_NIGHTS

    @property
    def has_data(self) -> bool:
        return self.days > 0

    def nights_per_day(self, ap: str) -> float:
        """Aeronaves (da família, se houver dados suficientes) vistas pernoitando no aeroporto por noite."""
        n = self.rec["nights"].get(ap)
        if not n or not self.days:
            return 0.0
        v = (n.get("fam") or {}).get(self.family, 0) if self.use_family else n["n"]
        return v / self.days

    def arrivals(self, ap: str) -> int:
        return int(self.rec["arrivals"].get(ap, 0))

    def flight(self, ident: str | None, adep: str, ades: str) -> dict | None:
        return self.rec["flights"].get(flight_key(ident, adep, ades))

    def next_count(self, a: dict, b: dict) -> tuple[int, int] | None:
        """(vezes que b veio logo depois de a, observações de a com continuação conhecida)."""
        f = self.flight(a.get("ident"), a["adep"], a["ades"])
        if not f or not f["n"]:
            return None
        return int(f["next"].get(flight_key(b.get("ident"), b["adep"], b["ades"]), 0)), int(f["n"])

    def observed_next(self, a: dict) -> set[str]:
        f = self.flight(a.get("ident"), a["adep"], a["ades"])
        return set(f["next"]) if f else set()


# ------------------------------------------------------------------ estoque noturno pelo RPL
def night_stock(occs: list[dict], tz_of, dates: list[date], compatible=None) -> dict[date, dict[str, int]]:
    """Para cada data d: quantas aeronaves compatíveis precisam estar no solo em cada aeroporto às 03:00
    (hora local) de d+1, ou seja, pernoitando na noite d→d+1. É o déficit máximo de saídas sobre chegadas
    a partir do corte, contado com os voos programados (RPL, projeções e internacionais do catálogo)."""
    ev: dict[str, list] = defaultdict(list)
    for o in occs:
        if compatible and not compatible(o["type_icao"]):
            continue
        ev[o["adep"]].append((o["dep"], -1))
        ev[o["ades"]].append((o["arr"], 1))
    out: dict[date, dict[str, int]] = {d: {} for d in dates}
    for ap, es in ev.items():
        es.sort()
        tz = get_tz(tz_of(ap)) or timezone.utc
        times = [e[0] for e in es]
        for d in dates:
            c0 = datetime.combine(d + timedelta(days=1), time(NIGHT_HOUR_CUT, 0), tz).astimezone(timezone.utc)
            c1 = c0 + timedelta(days=1)
            i = bisect.bisect_left(times, c0)
            bal = worst = 0
            while i < len(es) and es[i][0] < c1:
                bal += es[i][1]
                worst = min(worst, bal)
                i += 1
            if worst < 0:
                out[d][ap] = -worst
    return out


def night_date(arr_utc: datetime, tzname: str | None) -> date:
    """Noite a que pertence uma chegada: chegada antes das 03:00 locais conta como noite do dia anterior."""
    loc = arr_utc.astimezone(get_tz(tzname) or timezone.utc)
    return loc.date() - timedelta(days=1) if loc.hour < NIGHT_HOUR_CUT else loc.date()


def rpl_stock_avg(db, airline: str) -> dict[str, float]:
    """Média, na semana de validade do RPL, das aeronaves que amanhecem em cada aeroporto (todos os tipos)."""
    from .db import get_settings
    from .rpl_service import get_index
    idx = get_index(db, airline, int(get_settings(db).get("taxi_allowance_min") or 0))
    if not idx.flights:
        return {}
    cf, _ct = idx.coverage()
    d0 = date.fromisoformat(cf) if cf else date.today()
    dates = [d0 + timedelta(days=k) for k in range(7)]
    occs = []
    for k in range(9):
        occs += idx.occurrences(d0 + timedelta(days=k), True)
    apmap = catalog.airport_map(db)
    st = night_stock(occs, lambda icao: catalog.airport_tz(apmap, icao), dates)
    tot: Counter = Counter()
    for d in dates:
        tot.update(st[d])
    return {ap: v / len(dates) for ap, v in tot.items()}


def summary(db, airline: str | None = None, top: int = 40) -> dict:
    """Tabela para a tela Dados ADS-B: pernoites por aeroporto (RPL × ADS-B, aeronaves por noite)."""
    prof = get_profile(db)
    res = {"days": len(prof.get("days", [])), "first_day": (prof.get("days") or [None])[0],
           "last_day": (prof.get("days") or [None])[-1], "airlines": {}}
    for al, rec in (prof.get("airlines") or {}).items():
        if airline and al != airline:
            continue
        d = max(1, rec.get("days") or 1)
        obs = {ap: n["n"] / d for ap, n in rec["nights"].items()}
        rpl = rpl_stock_avg(db, al)
        aps = sorted(set(obs) | set(rpl), key=lambda ap: -(rpl.get(ap, 0) + obs.get(ap, 0)))
        fl = rec["flights"].values()
        res["airlines"][al] = {
            "stations_adsb": len(obs), "stations_rpl": len(rpl),
            "nights_adsb": round(sum(obs.values()), 1), "nights_rpl": round(sum(rpl.values()), 1),
            "top": [{"airport": ap, "rpl": round(rpl.get(ap, 0), 1), "adsb": round(obs.get(ap, 0), 1)}
                    for ap in aps[:top]],
            "flights": len(rec["flights"]),
            "links": sum(len(f["next"]) for f in fl),
        }
    return res
