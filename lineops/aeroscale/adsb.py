"""Dados abertos de ADS-B (adsb.lol) – matrículas reais e rodízio observado de GOL, LATAM e Azul.

Fonte: histórico diário publicado pelo adsb.lol no GitHub (github.com/adsblol/globe_history_AAAA),
licença ODbL 1.0. Cada dia é um .tar (≈ 3,5–4 GB, dividido em partes .tar.aa, .tar.ab…) com o rastro
de todas as aeronaves vistas pela rede no dia (arquivo traces/xx/trace_full_<hex>.json, gzip).

Como a leitura é feita, sem guardar o arquivo inteiro:
* o .tar é lido direto da rede por faixas de bytes (HTTP Range); trechos grandes que não interessam
  (mapas de calor) são pulados sem baixar;
* só os rastros com endereço ICAO do Brasil (E40000–E7FFFF) são abertos; dentro deles, só as
  aeronaves com indicativo das companhias escolhidas (GLO, TAM, AZU…) são guardadas;
* de cada rastro saem: matrícula, tipo e as "corridas" de indicativo (GLO1908 visto das 08:08 às 09:30…).

Depois, cada corrida é ligada ao voo do RPL com o mesmo número e horário compatível. A sequência de voos
de cada matrícula vira rodízio observado, usado para medir e recalibrar o modelo de previsão.
Indicativo que não existe no RPL (internacional, fretamento) vira perna "por posição": origem/destino
pelo aeroporto mais próximo do primeiro/último ponto em baixa altitude, quando houver.
"""
from __future__ import annotations

import gzip
import io
import json
import math
import os
import re
import tarfile
import threading
import time as _time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from . import __version__, catalog, fleet, predict
from .airport_geo_world import WORLD_AIRPORTS
from .db import connect, get_settings, now_iso, row, rows, set_settings
from .rpl_service import get_index

REPO_URL = "https://github.com/adsblol/globe_history_{year}/releases/download/{tag}/{tag}.tar.{part}"
USER_AGENT = f"AeroScale/{__version__} (simulador; leitura do historico aberto adsb.lol)"
BR_HEX = (0xE40000, 0xE7FFFF)
DEFAULT_PREFIXES = ("GLO", "TAM", "AZU")
PARTS = [a + b for a in "a" for b in "abcdefghij"]
LOW_ALT_FT = 8000      # ponto abaixo disto (ou no solo) serve para identificar aeroporto
NEAR_KM = 40
MATCH_SLACK_MIN = 90   # tolerância de atraso/antecipação entre o visto no ADS-B e o horário do RPL
MAX_TURN_H = 22


def _url(day: date, tag: str, part: str) -> str:
    tpl = os.environ.get("AEROSCALE_ADSB_URL") or REPO_URL
    return tpl.format(year=day.year, tag=tag, part=part)


def tag_for(day: date, pod: str = "prod") -> str:
    return f"v{day:%Y.%m.%d}-planes-readsb-{pod}-0"


def _probe(url: str) -> int | None:
    """Tamanho do arquivo remoto (pedindo só 1 byte); None se não existir."""
    req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            cr = r.headers.get("Content-Range") or ""
            m = re.search(r"/(\d+)$", cr)
            if m:
                return int(m.group(1))
            ln = r.headers.get("Content-Length")
            return int(ln) if ln and r.status == 200 else None
    except urllib.error.HTTPError as e:
        if e.code in (404, 416):
            return None
        raise


def locate(day: date) -> tuple[str, list[tuple[str, int]]]:
    """Encontra o pacote publicado do dia (prod; se faltar, staging) e o tamanho de cada parte."""
    for pod in ("prod", "staging"):
        tag = tag_for(day, pod)
        parts = []
        for p in PARTS:
            size = _probe(_url(day, tag, p))
            if size is None:
                break
            parts.append((_url(day, tag, p), size))
        if parts:
            return tag, parts
    raise LookupError(f"O adsb.lol ainda não publicou {day:%d/%m/%Y} (o dia sai no GitHub cerca de 1 dia depois).")


class Cancelled(Exception):
    pass


class RemoteParts(io.RawIOBase):
    """Arquivo somente leitura e 'pulável' sobre as partes remotas (.tar.aa + .tar.ab …), via HTTP Range."""
    CHUNK = 8 << 20

    def __init__(self, parts: list[tuple[str, int]], progress=None, cancel: threading.Event | None = None):
        super().__init__()
        self.parts = parts
        self.total = sum(s for _u, s in parts)
        self.pos = 0
        self.buf = b""
        self.bstart = 0
        self.downloaded = 0
        self.requests = 0
        self.progress = progress
        self.cancel = cancel

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.total + off
        return self.pos

    def _fetch(self, start: int):
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()
        acc = 0
        for url, size in self.parts:
            if start < acc + size:
                lo = start - acc
                hi = min(size - 1, lo + self.CHUNK - 1)
                for attempt in range(5):
                    try:
                        req = urllib.request.Request(url, headers={"Range": f"bytes={lo}-{hi}", "User-Agent": USER_AGENT})
                        with urllib.request.urlopen(req, timeout=90) as r:
                            data = r.read()
                        break
                    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                        if attempt == 4:
                            raise
                        _time.sleep((2, 5, 10, 20)[attempt])
                self.buf, self.bstart = data, start
                self.downloaded += len(data)
                self.requests += 1
                if self.progress:
                    self.progress(self)
                return
            acc += size
        self.buf, self.bstart = b"", start

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.total - self.pos
        out = bytearray()
        while n > 0 and self.pos < self.total:
            if not (self.bstart <= self.pos < self.bstart + len(self.buf)):
                self._fetch(self.pos)
                if not self.buf:
                    break
            i = self.pos - self.bstart
            chunk = self.buf[i:i + n]
            out += chunk
            self.pos += len(chunk)
            n -= len(chunk)
        return bytes(out)

    def readinto(self, b):
        d = self.read(len(b))
        b[:len(d)] = d
        return len(d)


# ------------------------------------------------------------------ leitura de um rastro
def callsign_runs(trace: dict, prefixes) -> list[dict]:
    """Trechos contínuos com o mesmo indicativo. Cada um: callsign, t0/t1 (epoch), posições inicial/final,
    altitude mínima no começo/fim e número de pontos."""
    ts = trace.get("timestamp") or 0
    out: list[dict] = []
    cur = None
    cs = None
    for t in trace.get("trace") or []:
        a = t[8] if len(t) > 8 and isinstance(t[8], dict) else None
        f = (a.get("flight") or "").strip() if a else ""
        if f and not f.startswith("@"):
            cs = f
        if not cs:
            continue
        tm = ts + t[0]
        alt = 0 if t[3] == "ground" else (t[3] if isinstance(t[3], (int, float)) else None)
        if cur is None or cs != cur["callsign"]:
            cur = {"callsign": cs, "t0": tm, "t1": tm, "lat0": t[1], "lon0": t[2], "alt0": alt,
                   "lat1": t[1], "lon1": t[2], "alt1": alt, "points": 0}
            out.append(cur)
        cur["t1"], cur["lat1"], cur["lon1"], cur["alt1"] = tm, t[1], t[2], alt
        cur["points"] += 1
    pref = tuple(prefixes)
    return [r for r in out if r["callsign"].startswith(pref) and r["points"] >= 2]


def scan_day(day: date, prefixes=DEFAULT_PREFIXES, brazil_only=True, progress=None,
             cancel: threading.Event | None = None) -> dict:
    """Lê o pacote do dia e devolve {"aircraft": {hex: {...}}, "runs": {hex: [...]}, "stats": {...}}."""
    tag, parts = locate(day)
    t_start = _time.time()
    rf = RemoteParts(parts, progress=progress, cancel=cancel)
    needles = [f'"{p}'.encode() for p in prefixes]  # indicativo começa com o prefixo ("GLO1234 ")
    aircraft, runs = {}, {}
    seen_traces = 0
    tf = tarfile.open(fileobj=rf, mode="r:")
    for m in tf:
        mt = re.search(r"trace_full_([0-9a-f]{6})\.json$", m.name)
        if not mt or not m.isfile():
            continue
        seen_traces += 1
        hx = mt.group(1)
        if brazil_only and not BR_HEX[0] <= int(hx, 16) <= BR_HEX[1]:
            continue
        raw = tf.extractfile(m).read()
        try:
            data = gzip.decompress(raw)
        except OSError:
            data = raw
        if not any(n in data for n in needles):
            continue
        try:
            tr = json.loads(data)
        except ValueError:
            continue
        rs = callsign_runs(tr, prefixes)
        if not rs:
            continue
        al = Counter(r["callsign"][:3] for r in rs).most_common(1)[0][0]
        aircraft[hx] = {"hex": hx, "registration": tr.get("r"), "icao_type": tr.get("t"), "descr": tr.get("desc"),
                        "airline": al, "last_callsign": rs[-1]["callsign"]}
        runs[hx] = rs
    return {"day": day.isoformat(), "tag": tag, "aircraft": aircraft, "runs": runs,
            "stats": {"traces": seen_traces, "aircraft": len(aircraft), "runs": sum(len(v) for v in runs.values()),
                      "mb": round(rf.downloaded / 1e6, 1), "total_mb": round(rf.total / 1e6, 1),
                      "requests": rf.requests, "seconds": int(_time.time() - t_start)}}


# ------------------------------------------------------------------ banco
def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def ingest(db, scan: dict, prefixes) -> dict:
    day = scan["day"]
    ts = now_iso()
    db.execute("DELETE FROM adsb_runs WHERE day=?", (day,))
    for hx, a in scan["aircraft"].items():
        ex = row(db.execute("SELECT * FROM adsb_aircraft WHERE hex=?", (hx,)))
        if ex:
            days = {d for d in (ex["days_list"] or "").split(",") if d} | {day}
            db.execute("UPDATE adsb_aircraft SET registration=COALESCE(?, registration), icao_type=COALESCE(?, icao_type),"
                       " descr=COALESCE(?, descr), airline=?, first_day=?, last_day=?, days=?, days_list=?,"
                       " last_callsign=CASE WHEN ?>=last_day THEN ? ELSE last_callsign END, updated_at=? WHERE hex=?",
                       (a["registration"], a["icao_type"], a["descr"], a["airline"], min(days), max(days), len(days),
                        ",".join(sorted(days)), day, a["last_callsign"], ts, hx))
        else:
            db.execute("INSERT INTO adsb_aircraft(hex, registration, icao_type, descr, airline, first_day, last_day, days,"
                       " days_list, last_callsign, updated_at) VALUES (?,?,?,?,?,?,?,1,?,?,?)",
                       (hx, a["registration"], a["icao_type"], a["descr"], a["airline"], day, day, day,
                        a["last_callsign"], ts))
        for r in scan["runs"][hx]:
            db.execute("INSERT INTO adsb_runs(day, hex, callsign, airline, t0, t1, lat0, lon0, alt0, lat1, lon1, alt1, points)"
                       " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (day, hx, r["callsign"], r["callsign"][:3], _iso(r["t0"]), _iso(r["t1"]), r["lat0"], r["lon0"],
                        r["alt0"], r["lat1"], r["lon1"], r["alt1"], r["points"]))
    st = scan["stats"]
    db.execute("INSERT OR REPLACE INTO adsb_days(day, tag, status, aircraft, runs, mb, seconds, prefixes, scanned_at)"
               " VALUES (?,?,?,?,?,?,?,?,?)",
               (day, scan["tag"], "ok", st["aircraft"], st["runs"], st["mb"], st["seconds"], ",".join(prefixes), ts))
    match_runs(db, [day])
    return st


def _nearest_airport(lat, lon, alt) -> str | None:
    if lat is None or alt is None or alt > LOW_ALT_FT:
        return None
    best, bd = None, NEAR_KM
    for icao, (_iata, la, lo, _c, _cc) in WORLD_AIRPORTS.items():
        if abs(la - lat) > 0.5 or abs(lo - lon) > 0.6:
            continue
        d = haversine_km(lat, lon, la, lo)
        if d < bd:
            best, bd = icao, d
    return best


def haversine_km(la1, lo1, la2, lo2) -> float:
    p1, p2 = math.radians(la1), math.radians(la2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 12742.0 * math.asin(math.sqrt(h))


def match_runs(db, days: list[str] | None = None) -> dict:
    """Liga cada corrida de indicativo ao voo do RPL (mesmo número, horário compatível) e grava as pernas."""
    taxi = int(get_settings(db).get("taxi_allowance_min") or 0)
    q = "SELECT * FROM adsb_runs"
    args: list = []
    if days:
        q += " WHERE day IN (%s)" % ",".join("?" * len(days))
        args = list(days)
        db.execute("DELETE FROM adsb_legs WHERE day IN (%s)" % ",".join("?" * len(days)), args)
    else:
        db.execute("DELETE FROM adsb_legs")
    runs = rows(db.execute(q + " ORDER BY hex, t0", args))
    occ_cache: dict = {}

    def occs_for(airline, ident, d: date):
        key = (airline, d)
        if key not in occ_cache:
            idx = get_index(db, airline, taxi)
            m: dict = defaultdict(list)
            if idx.flights:
                for o in idx.occurrences(d, True):
                    m[o["ident"]].append(o)
            occ_cache[key] = m
        return occ_cache[key].get(ident, [])

    out = Counter()
    slack = timedelta(minutes=MATCH_SLACK_MIN)
    last_ref: dict[str, str] = {}
    for r in runs:
        t0, t1 = _parse(r["t0"]), _parse(r["t1"])
        d = t0.date()
        cands = []
        for dd in (d - timedelta(days=1), d, d + timedelta(days=1)):
            cands += [o for o in occs_for(r["airline"], r["callsign"], dd) if o["dep"] - slack <= t1 and o["arr"] + slack >= t0]
        if cands:
            for o in sorted(cands, key=lambda o: o["dep"]):  # voo com escalas: o mesmo número cobre 2+ pernas
                if last_ref.get(r["hex"]) == o["ref"]:
                    continue
                last_ref[r["hex"]] = o["ref"]
                db.execute("INSERT OR IGNORE INTO adsb_legs(day, hex, airline, callsign, ref, origin, adep, ades, dep_utc,"
                           " arr_utc, seen_from, seen_to, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'rpl')",
                           (r["day"], r["hex"], r["airline"], r["callsign"], o["ref"], o["origin"], o["adep"], o["ades"],
                            o["dep"].strftime("%Y-%m-%dT%H:%M:%SZ"), o["arr"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                            r["t0"], r["t1"]))
            out["rpl"] += 1
        else:
            adep = _nearest_airport(r["lat0"], r["lon0"], r["alt0"])
            ades = _nearest_airport(r["lat1"], r["lon1"], r["alt1"])
            hours = (t1 - t0).total_seconds() / 3600
            if (adep and adep == ades) or hours > 14:
                out["descartado"] += 1  # parada no solo com transponder ligado / indicativo velho
                continue
            db.execute("INSERT INTO adsb_legs(day, hex, airline, callsign, ref, origin, adep, ades, dep_utc, arr_utc,"
                       " seen_from, seen_to, source) VALUES (?,?,?,?,NULL,'ADSB',?,?,?,?,?,?,'posicao')",
                       (r["day"], r["hex"], r["airline"], r["callsign"], adep, ades, r["t0"], r["t1"], r["t0"], r["t1"]))
            out["posicao"] += 1
    for d in (days or [x["day"] for x in rows(db.execute("SELECT day FROM adsb_days"))]):
        n = db.execute("SELECT COUNT(*) c FROM adsb_legs WHERE day=? AND source='rpl'", (d,)).fetchone()["c"]
        db.execute("UPDATE adsb_days SET matched=? WHERE day=?", (n, d))
    return dict(out)


# ------------------------------------------------------------------ análises
def legs_by_aircraft(db, airline: str | None = None) -> dict[str, list[dict]]:
    q = ("SELECT l.*, a.registration, a.icao_type FROM adsb_legs l JOIN adsb_aircraft a ON a.hex=l.hex")
    args = []
    if airline:
        q += " WHERE l.airline=?"
        args.append(airline)
    out: dict[str, list] = defaultdict(list)
    for r in rows(db.execute(q + " ORDER BY l.hex, l.dep_utc", args)):
        out[r["hex"]].append(r)
    return out


def transitions(db, airline: str | None = None) -> list[dict]:
    """Pares (perna, próxima perna) da mesma matrícula, ambos no RPL e com continuidade de aeroporto."""
    taxi = int(get_settings(db).get("taxi_allowance_min") or 0)
    ref_cache: dict = {}

    def occ(al, ref):
        k = (al, ref)
        if k not in ref_cache:
            fid, ds = ref.split(":", 1)
            ck = (al, ds)
            if ck not in ref_cache:
                ref_cache[ck] = {o["ref"]: o for o in get_index(db, al, taxi).occurrences(date.fromisoformat(ds), True)}
            ref_cache[k] = ref_cache[ck].get(ref)
        return ref_cache[k]

    out = []
    for hx, legs in legs_by_aircraft(db, airline).items():
        rl = [l for l in legs if l["source"] == "rpl"]
        for a, b in zip(rl, rl[1:]):
            if b["adep"] != a["ades"]:
                continue
            g = (_parse(b["dep_utc"]) - _parse(a["arr_utc"])).total_seconds() / 3600
            if not 0 <= g <= MAX_TURN_H:
                continue
            oa, ob = occ(a["airline"], a["ref"]), occ(b["airline"], b["ref"])
            if oa and ob:
                out.append({"hex": hx, "registration": a["registration"], "icao_type": a["icao_type"],
                            "airline": a["airline"], "day": a["day"], "cur": oa, "next": ob})
    return out


def _pct(v: list[float], q: float):
    if not v:
        return None
    v = sorted(v)
    return round(v[min(len(v) - 1, int(q * len(v)))])


def real_stats(db) -> dict:
    """Lógica operacional medida no rodízio real (por companhia)."""
    apmap = catalog.airport_map(db)
    tz_of = lambda icao: catalog.airport_tz(apmap, icao)  # noqa: E731
    res = {}
    for al, legs_map in _group_airline(legs_by_aircraft(db)).items():
        days_legs, turns, rep_days, ac_days, overn = [], [], 0, 0, Counter()
        pairs_rep = Counter()
        for hx, legs in legs_map.items():
            rl = [l for l in legs if l["source"] == "rpl"]
            by_day: dict = defaultdict(list)
            for l in rl:
                dep = _parse(l["dep_utc"])
                by_day[catalog_local_date(dep, tz_of(l["adep"]))].append(l)
            for d, ls in by_day.items():
                if len(ls) < 2:
                    continue
                ac_days += 1
                days_legs.append(len(ls))
                pc = Counter(tuple(sorted((x["adep"], x["ades"]))) for x in ls)
                rep = [k for k, v in pc.items() if v > 2]
                if rep:
                    rep_days += 1
                    for k in rep:
                        pairs_rep["–".join(k)] += 1
            for a, b in zip(rl, rl[1:]):
                if b["adep"] != a["ades"]:
                    continue
                g = (_parse(b["dep_utc"]) - _parse(a["arr_utc"])).total_seconds() / 60
                kind = predict.gap_kind(_parse(a["arr_utc"]), _parse(b["dep_utc"]), a["ades"], tz_of, 0)
                if kind == "pernoite":
                    overn[a["ades"]] += 1
                elif 0 <= g <= 240:
                    turns.append(g)
        res[al] = {
            "aircraft": len(legs_map), "aircraft_days": ac_days,
            "legs_per_day": {"media": round(sum(days_legs) / len(days_legs), 2) if days_legs else None,
                             "p10": _pct(days_legs, .1), "p50": _pct(days_legs, .5), "p90": _pct(days_legs, .9)},
            "turn_min": {"p10": _pct(turns, .1), "p50": _pct(turns, .5), "p90": _pct(turns, .9), "n": len(turns)},
            "repeat_day_pct": round(100 * rep_days / ac_days, 1) if ac_days else None,
            "repeat_pairs": pairs_rep.most_common(8),
            "overnight_top": overn.most_common(8),
        }
    return res


def catalog_local_date(dt: datetime, tzname: str | None) -> str:
    from .airports import to_local
    return to_local(dt, tzname).date().isoformat()


def _group_airline(by_ac: dict) -> dict:
    out: dict = defaultdict(dict)
    for hx, legs in by_ac.items():
        if legs:
            out[legs[0]["airline"]][hx] = legs
    return out


# ------------------------------------------------------------------ avaliação e calibração do modelo
def _model_rows(db, trans: list[dict], weights_by_airline: dict | None = None):
    """Pré-calcula, para cada transição, a matriz de características dos candidatos e o índice do verdadeiro."""
    apmap = catalog.airport_map(db)
    tz_of = lambda icao: catalog.airport_tz(apmap, icao)  # noqa: E731
    taxi = int(get_settings(db).get("taxi_allowance_min") or 0)
    models: dict = {}
    data = []
    for t in trans:
        key = (t["airline"], t["icao_type"])
        if key not in models:
            w = (weights_by_airline or {}).get(t["airline"])
            models[key] = predict.RotationModel(get_index(db, t["airline"], taxi), t["airline"], t["icao_type"], tz_of,
                                                True, "family", weights=w)
        cands = models[key].candidates(t["cur"])
        ti = next((i for i, c in enumerate(cands) if c["occ"]["ref"] == t["next"]["ref"]), None)
        data.append({"airline": t["airline"], "day": t["day"], "hex": t["hex"], "X": [c["x"] for c in cands],
                     "true": ti, "rank": (ti + 1) if ti is not None else None,
                     "p": cands[ti]["p"] if ti is not None else None})
    return data


def evaluate(db, trans: list[dict] | None = None, weights_by_airline: dict | None = None) -> dict:
    trans = trans if trans is not None else transitions(db)
    data = _model_rows(db, trans, weights_by_airline)
    return _metrics(data)


def _metrics(data, w_by_al: dict | None = None) -> dict:
    out: dict = {}
    for al in sorted({d["airline"] for d in data}) + ["_total"]:
        sel = [d for d in data if al == "_total" or d["airline"] == al]
        n = len(sel)
        if not n:
            continue
        if w_by_al is None:
            ranks = [d["rank"] for d in sel]
            ps = [d["p"] for d in sel]
        else:
            ranks, ps = [], []
            for d in sel:
                r, p = _rank_with(d, w_by_al.get(d["airline"]) or predict.WEIGHTS)
                ranks.append(r)
                ps.append(p)
        out[al] = {"n": n, "top1": round(sum(1 for r in ranks if r == 1) / n, 3),
                   "top3": round(sum(1 for r in ranks if r and r <= 3) / n, 3),
                   "fora": sum(1 for r in ranks if r is None),
                   "loglik": round(sum(math.log(max(p or 1e-6, 1e-6)) for p in ps) / n, 3)}
    return out


def _scores(X, w):
    return [sum(w.get(k, 0.0) * v for k, v in x.items()) for x in X]


def _rank_with(d, w):
    if d["true"] is None or not d["X"]:
        return None, None
    s = _scores(d["X"], w)
    mx = max(s)
    z = sum(math.exp(v - mx) for v in s)
    p = math.exp(s[d["true"]] - mx) / z
    rank = 1 + sum(1 for v in s if v > s[d["true"]])
    return rank, p


def fit_weights(data: list[dict], prior: dict, l2: float = 0.5, iters: int = 12) -> dict:
    """Máxima verossimilhança (logit multinomial) por Newton com regularização L2 em direção aos pesos atuais."""
    keys = list(prior.keys())
    K = len(keys)
    w = [float(prior[k]) for k in keys]
    rows_ = [([[x.get(k, 0.0) for k in keys] for x in d["X"]], d["true"]) for d in data if d["true"] is not None and d["X"]]
    if not rows_:
        return dict(prior)

    def objective(wv):
        ll = 0.0
        for X, ti in rows_:
            s = [sum(a * b for a, b in zip(x, wv)) for x in X]
            mx = max(s)
            ll += s[ti] - mx - math.log(sum(math.exp(v - mx) for v in s))
        return ll - l2 * sum((a - float(prior[k])) ** 2 for a, k in zip(wv, keys))

    cur = objective(w)
    for _ in range(iters):
        g = [0.0] * K
        H = [[0.0] * K for _ in range(K)]
        for X, ti in rows_:
            s = [sum(a * b for a, b in zip(x, w)) for x in X]
            mx = max(s)
            e = [math.exp(v - mx) for v in s]
            z = sum(e)
            p = [v / z for v in e]
            mu = [sum(p[i] * X[i][k] for i in range(len(X))) for k in range(K)]
            for k in range(K):
                g[k] += X[ti][k] - mu[k]
            for i, x in enumerate(X):
                if p[i] < 1e-9:
                    continue
                for a in range(K):
                    xa = x[a] - mu[a]
                    if xa == 0:
                        continue
                    pa = p[i] * xa
                    for b in range(a, K):
                        H[a][b] -= pa * (x[b] - mu[b])
        for k in range(K):
            g[k] -= 2 * l2 * (w[k] - float(prior[keys[k]]))
            H[k][k] -= 2 * l2
        for a in range(K):
            for b in range(a):
                H[a][b] = H[b][a]
        step = _solve([[-v for v in r] for r in H], g)
        if step is None:
            break
        t = 1.0
        while t > 1e-3:
            cand = [a + t * s for a, s in zip(w, step)]
            val = objective(cand)
            if val >= cur:
                break
            t /= 2
        if t <= 1e-3:
            break
        gain = val - cur
        w, cur = cand, val
        if gain < 1e-4:
            break
    return {k: round(v, 3) for k, v in zip(keys, w)}


def _solve(A, b):
    n = len(b)
    M = [r[:] + [b[i]] for i, r in enumerate(A)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[piv][c]) < 1e-12:
            return None
        M[c], M[piv] = M[piv], M[c]
        for r in range(n):
            if r != c and M[r][c]:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] for i in range(n)]


def calibrate(db) -> dict:
    """Ajusta pesos por companhia com as transições observadas e mede em dados separados (validação)."""
    trans = transitions(db)
    if len(trans) < 50:
        raise ValueError("Poucas transições observadas para calibrar (mínimo 50). Processe mais dias do ADS-B.")
    current = predict.weights_by_airline(get_settings(db))
    data = _model_rows(db, trans, current)
    days = sorted({d["day"] for d in data})
    if len(days) >= 2:
        test_days = set(days[-max(1, len(days) // 4):])
        split = f"treino em {len(days) - len(test_days)} dia(s), validação em {len(test_days)} dia(s) separado(s)"
    else:
        test_days = None
        split = "um dia só: validação com 1 de cada 4 aeronaves (separadas pela matrícula)"
    is_test = (lambda d: d["day"] in test_days) if test_days else (lambda d: int(d["hex"], 16) % 4 == 0)
    train = [d for d in data if not is_test(d)]
    test = [d for d in data if is_test(d)]
    fitted = {}
    for al in sorted({d["airline"] for d in data}):
        tr = [d for d in train if d["airline"] == al]
        if len(tr) >= 50:
            fitted[al] = fit_weights(tr, current.get(al) or predict.WEIGHTS)
    before = _metrics(test, {al: current.get(al) or predict.WEIGHTS for al in fitted})
    after = _metrics(test, {al: fitted.get(al) or current.get(al) or predict.WEIGHTS for al in fitted})
    # pesos finais: refeitos com todos os dados (treino + validação)
    final = {al: fit_weights([d for d in data if d["airline"] == al], current.get(al) or predict.WEIGHTS)
             for al in fitted}
    return {"transitions": len(data), "train": len(train), "test": len(test), "split": split,
            "before": before, "after": after, "weights": final, "current": current, "default": predict.WEIGHTS}


# ------------------------------------------------------------------ frota
def airline_families(db, airline: str) -> set[str]:
    """Famílias de aeronave que a companhia opera segundo o RPL ativo (ex.: GOL → {B737})."""
    out = set()
    for r in db.execute("SELECT DISTINCT f.type_icao FROM rpl_flights f JOIN rpl_imports i ON i.id=f.import_id"
                        " WHERE i.active=1 AND f.airline=?", (airline,)):
        if r["type_icao"]:
            out.add(catalog.type_family(r["type_icao"]) or r["type_icao"])
    return out


def suspect_reason(a: dict, families: set[str]) -> str | None:
    """Matrícula/tipo do banco do ADS-B que não combina com a companhia (banco desatualizado para o hex)."""
    if not a.get("registration"):
        return "sem matrícula no banco do ADS-B"
    if not a.get("icao_type"):
        return "sem tipo no banco do ADS-B"
    fam = catalog.type_family(a["icao_type"]) or a["icao_type"]
    # widebody em voo internacional não aparece no RPL doméstico: só é suspeito se voou voos do RPL
    if families and fam not in families and (a.get("legs_rpl") or 0) > 0:
        return (f"voou voos domésticos da {a['airline']} (RPL: {', '.join(sorted(families))}), mas o banco do ADS-B diz "
                f"{a['icao_type']}; o cadastro desse transponder pode estar desatualizado")
    return None


def to_fleet(db, airline: str | None = None, hexes: list[str] | None = None, fill_models: bool = True) -> dict:
    q = ("SELECT a.*, (SELECT COUNT(*) FROM adsb_legs l WHERE l.hex=a.hex AND l.source='rpl') AS legs_rpl"
         " FROM adsb_aircraft a WHERE registration IS NOT NULL")
    args: list = []
    if airline:
        q += " AND a.airline=?"
        args.append(airline)
    if hexes:
        q += " AND a.hex IN (%s)" % ",".join("?" * len(hexes))
        args += hexes
    created, filled, skipped, errors = 0, 0, 0, []
    model_ids: dict = {}

    def model_for(al, t):
        if not t:
            return None
        k = (al, t)
        if k not in model_ids:
            m = row(db.execute("SELECT id FROM aircraft_models WHERE airline=? AND icao_type=? ORDER BY id LIMIT 1", (al, t)))
            if m:
                model_ids[k] = m["id"]
            else:
                model_ids[k] = db.execute(
                    "INSERT INTO aircraft_models(airline,name,icao_type,family,source,created_at) VALUES (?,?,?,?,'adsb',?)",
                    (al, catalog.type_name(t), t, catalog.type_family(t) or t, now_iso())).lastrowid
        return model_ids[k]

    fam_cache: dict = {}
    suspects = []
    for a in rows(db.execute(q, args)):
        if not db.execute("SELECT 1 FROM airlines WHERE code=?", (a["airline"],)).fetchone():
            skipped += 1
            continue
        if a["airline"] not in fam_cache:
            fam_cache[a["airline"]] = airline_families(db, a["airline"])
        why = suspect_reason(a, fam_cache[a["airline"]])
        if why and not hexes:  # só entra se for escolhida uma a uma
            suspects.append(f"{a['registration']} ({a['icao_type'] or '?'})")
            continue
        ex = row(db.execute("SELECT id, model_id, airline FROM aircraft WHERE registration=?", (a["registration"],)))
        note = f"Vista no ADS-B (adsb.lol) em {a['days']} dia(s), {a['first_day']} a {a['last_day']}; hex {a['hex']}."
        if ex:
            if fill_models and not ex["model_id"] and a["icao_type"] and ex["airline"] == a["airline"]:
                db.execute("UPDATE aircraft SET model_id=?, model_source='adsb', updated_at=? WHERE id=?",
                           (model_for(a["airline"], a["icao_type"]), now_iso(), ex["id"]))
                fleet._history(db, ex["id"], "modelo", "model_id", None, a["icao_type"], source="adsb", note=note)
                db.execute("UPDATE review_flags SET resolved=1, resolution='modelo pelo ADS-B', resolved_at=? WHERE"
                           " entity='aircraft' AND entity_id=? AND code='modelo_ausente' AND resolved=0",
                           (now_iso(), ex["id"]))
                filled += 1
            else:
                skipped += 1
            continue
        try:
            fleet.create_aircraft(db, {"registration": a["registration"], "airline": a["airline"],
                                       "model_id": model_for(a["airline"], a["icao_type"]), "notes": note}, source="adsb")
            created += 1
        except ValueError as e:
            errors.append(f"{a['registration']}: {e}")
    return {"created": created, "models_filled": filled, "skipped": skipped, "suspects": suspects, "errors": errors[:20]}


# ------------------------------------------------------------------ tarefa em segundo plano
JOB: dict = {}
_LOCK = threading.Lock()


def job_status() -> dict:
    with _LOCK:
        j = {k: v for k, v in JOB.items() if k not in ("thread", "cancel")}
    return j or {"state": "idle"}


def start_job(db_path: str, days: list[date], prefixes, brazil_only=True, skip_done=True) -> dict:
    with _LOCK:
        if JOB.get("state") == "running":
            raise ValueError("Já existe uma leitura do ADS-B em andamento.")
        cancel = threading.Event()
        JOB.clear()
        JOB.update(state="running", days=[d.isoformat() for d in days], done=[], skipped=[], failed=[],
                   current=None, stage="iniciando", downloaded_mb=0, total_mb=None, aircraft_found=0,
                   started_at=now_iso(), cancel=cancel, prefixes=list(prefixes), log=[])
    th = threading.Thread(target=_run_job, args=(db_path, days, tuple(prefixes), brazil_only, skip_done, cancel),
                          daemon=True)
    JOB["thread"] = th
    th.start()
    return job_status()


def cancel_job():
    with _LOCK:
        if JOB.get("state") == "running":
            JOB["cancel"].set()
            JOB["stage"] = "cancelando…"


def _set(**kw):
    with _LOCK:
        JOB.update(kw)


def _log(msg):
    with _LOCK:
        JOB.setdefault("log", []).append(f"{datetime.now():%H:%M:%S} {msg}")
        JOB["log"] = JOB["log"][-60:]


def _run_job(db_path, days, prefixes, brazil_only, skip_done, cancel):
    db = connect(db_path)
    try:
        for d in days:
            if cancel.is_set():
                break
            ds = d.isoformat()
            if skip_done and db.execute("SELECT 1 FROM adsb_days WHERE day=? AND status='ok' AND prefixes=?",
                                        (ds, ",".join(prefixes))).fetchone():
                with _LOCK:
                    JOB["skipped"].append(ds)
                _log(f"{ds}: já processado, pulado.")
                continue
            _set(current=ds, stage="localizando o pacote do dia", downloaded_mb=0, total_mb=None)

            def prog(rf):
                _set(stage="lendo rastros", downloaded_mb=round(rf.downloaded / 1e6), total_mb=round(rf.total / 1e6),
                     position_pct=round(100 * rf.pos / rf.total, 1))
            try:
                scan = scan_day(d, prefixes, brazil_only, prog, cancel)
                _set(stage="ligando aos voos do RPL")
                st = ingest(db, scan, prefixes)
                db.commit()
                with _LOCK:
                    JOB["done"].append(ds)
                    JOB["aircraft_found"] = max(JOB.get("aircraft_found", 0), st["aircraft"])
                _log(f"{ds}: {st['aircraft']} aeronaves, {st['runs']} trechos; baixados {st['mb']:.0f} de "
                     f"{st['total_mb']:.0f} MB em {st['seconds']} s.")
            except Cancelled:
                _log(f"{ds}: cancelado.")
                break
            except LookupError as e:
                with _LOCK:
                    JOB["failed"].append({"day": ds, "error": str(e)})
                _log(f"{ds}: {e}")
            except Exception as e:  # rede, arquivo corrompido…
                db.rollback()
                with _LOCK:
                    JOB["failed"].append({"day": ds, "error": f"{type(e).__name__}: {e}"})
                _log(f"{ds}: falhou ({type(e).__name__}: {e}).")
        try:  # atualiza o catálogo de voos internacionais com o que foi lido
            from . import intl
            r = intl.rebuild_from_adsb(db)
            db.commit()
            _log(f"Catálogo internacional: {r['flights']} voo(s) observado(s); {r['total']} no total.")
        except Exception as e:
            _log(f"Catálogo internacional não atualizado: {e}")
        try:  # pernoites e rodízio real (ops.py) com os dias novos
            from . import ops
            r = ops.rebuild_from_adsb(db)
            db.commit()
            _log(f"Pernoites e rodízio observados: {r['days_total']} dia(s) ({r['days_added']} lido(s) aqui).")
        except Exception as e:
            _log(f"Pernoites e rodízio não atualizados: {e}")
        _set(state="cancelled" if cancel.is_set() else "finished", stage="concluído", current=None,
             finished_at=now_iso())
    finally:
        db.close()


def delete_day(db, day: str) -> dict:
    for t in ("adsb_runs", "adsb_legs", "adsb_days"):
        db.execute(f"DELETE FROM {t} WHERE day=?", (day,))
    removed = 0
    for a in rows(db.execute("SELECT hex, days_list FROM adsb_aircraft")):
        days = sorted(d for d in (a["days_list"] or "").split(",") if d and d != day)
        if not days:
            db.execute("DELETE FROM adsb_aircraft WHERE hex=?", (a["hex"],))
            removed += 1
        else:
            db.execute("UPDATE adsb_aircraft SET days_list=?, days=?, first_day=?, last_day=? WHERE hex=?",
                       (",".join(days), len(days), days[0], days[-1], a["hex"]))
    return {"ok": True, "aircraft_removed": removed}


def summary(db) -> dict:
    days = rows(db.execute("SELECT * FROM adsb_days ORDER BY day"))
    by_al = []
    for r in rows(db.execute(
            "SELECT a.airline, COUNT(*) n, SUM(CASE WHEN f.id IS NOT NULL THEN 1 ELSE 0 END) in_fleet,"
            " SUM(CASE WHEN a.registration IS NULL THEN 1 ELSE 0 END) no_reg FROM adsb_aircraft a"
            " LEFT JOIN aircraft f ON f.registration=a.registration GROUP BY a.airline ORDER BY a.airline")):
        types = rows(db.execute("SELECT COALESCE(icao_type,'?') t, COUNT(*) n FROM adsb_aircraft WHERE airline=?"
                                " GROUP BY icao_type ORDER BY n DESC", (r["airline"],)))
        fams = airline_families(db, r["airline"])
        sus = [a for a in rows(db.execute(
            "SELECT a.*, (SELECT COUNT(*) FROM adsb_legs l WHERE l.hex=a.hex AND l.source='rpl') AS legs_rpl"
            " FROM adsb_aircraft a LEFT JOIN aircraft f ON f.registration=a.registration"
            " WHERE a.airline=? AND f.id IS NULL AND a.registration IS NOT NULL", (r["airline"],)))
            if suspect_reason(a, fams)]
        by_al.append({**r, "types": types, "suspects": len(sus)})
    legs = db.execute("SELECT COUNT(*) n, SUM(source='rpl') rpl FROM adsb_legs").fetchone()
    s = get_settings(db)
    return {"days": days, "airlines": by_al, "legs": legs["n"] or 0, "legs_rpl": legs["rpl"] or 0,
            "weights": s.get("model_weights") or {}, "weights_info": s.get("model_weights_info") or None,
            "job": job_status()}


def export_csv(db, airline: str | None = None) -> str:
    import csv
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["companhia", "matricula", "tipo_icao", "descricao", "hex", "dias_visto", "primeiro_dia", "ultimo_dia",
                "ultimo_voo", "pernas_rpl", "conferir"])
    q = ("SELECT a.*, (SELECT COUNT(*) FROM adsb_legs l WHERE l.hex=a.hex AND l.source='rpl') AS legs_rpl"
         " FROM adsb_aircraft a")
    args = []
    if airline:
        q += " WHERE a.airline=?"
        args.append(airline)
    fams: dict = {}
    for a in rows(db.execute(q + " ORDER BY a.airline, a.registration IS NULL, a.registration", args)):
        if a["airline"] not in fams:
            fams[a["airline"]] = airline_families(db, a["airline"])
        w.writerow([a["airline"], a["registration"] or "", a["icao_type"] or "", a["descr"] or "", a["hex"], a["days"],
                    a["first_day"], a["last_day"], a["last_callsign"] or "", a["legs_rpl"],
                    suspect_reason(a, fams[a["airline"]]) or ""])
    return "\ufeff" + buf.getvalue()

