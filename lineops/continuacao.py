"""Continuações (beta): depois do voo X, para onde o mesmo avião (e a tripulação) segue.

O RPL do CGNA lista voos, não diz qual avião faz qual. Para descobrir a continuação, cruzamos três coisas:

1. RPL: os voos que saem do destino de X entre 25 min e 4 h depois do calço, em cada dia da semana em que X voa.
   Ex.: o TAM3376 chega em REC às ~23:15Z; o TAM3262 sai de REC às 00:15Z para Petrolina, mas só às quintas e aos
   sábados. Nesses dias ele é candidato; nos outros, não.
2. ADS-B (rodízios reais vistos pelo adsb.lol, o mesmo perfil que o gerador usa): quantas vezes o avião de X fez Y logo
   depois. A contagem vale "por oportunidade": um voo que só existe 2 dias por semana e foi visto 2 vezes pesa mais do
   que um diário visto 5 vezes.
3. Numeração: o par de ida e volta costuma ser X e X±1 (3376 ida, 3375 volta). Em algumas companhias e voos o par é
   "invertido" (a volta é X+1 em vez de X−1, ou o contrário). O sentido mais comum é medido nos pares que o ADS-B
   confirma, e o par do outro sentido é marcado como invertido.

E uma quarta, que manda em tudo: as sequências que você cadastra (o que o piloto disse), com os dias da semana.

Confiança: alta (ADS-B forte: 3+ vezes e mais de metade das oportunidades), média (ADS-B 2+ vezes com 25%+, ou par de
numeração confirmado ao menos uma vez), baixa (só uma pista). É beta: com 26 dias de ADS-B, muito voo tem pouca
observação, e a tela mostra os números para você julgar.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from datetime import date, timedelta

from .db import now_iso, rows, tx
from .errors import FGVError, NotFound

log = logging.getLogger("lineops.continuacao")

GAP_MIN, GAP_MAX = 25, 240
WD = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
_CACHE: dict = {}
_LOCK = threading.Lock()


def _num(ident: str | None) -> int | None:
    import re
    m = re.search(r"(\d+)$", ident or "")
    return int(m.group(1)) if m else None


def _key(ident, a, b) -> str:
    return f"{ident}:{a}-{b}"


def _load(data_dir, airline: str):
    """Índice do RPL e perfil de rodízios observados (ADS-B) do motor."""
    from . import motor, troca
    from .aeroscale import ops as ops_mod
    idx, err = troca._get_index(data_dir, airline)
    if err:
        raise FGVError(err, code="sem_rpl")
    c = motor.connect(data_dir)
    try:
        prof = ops_mod.get_profile(c)
    finally:
        c.close()
    rec = (prof.get("airlines") or {}).get(airline) or {"days": 0, "flights": {}}
    return idx, rec, len(prof.get("days") or []) or int(rec.get("days") or 0)


def real_successions(data_dir, airline: str) -> dict:
    """{ident X: {ident Y: {weekday: n}}} tirado das escalas reais: as de referência (aeroscale/data/escalas_reais.json)
    e as que você importou em "Importar escala real". Um voo seguido de outro no mesmo dia, saindo de onde o primeiro
    chegou, é uma continuação vista de verdade."""
    import json
    import os
    from .aeroscale import roster_import
    out: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    texts = []
    try:
        ref = json.loads(open(os.path.join(os.path.dirname(__file__), "aeroscale", "data", "escalas_reais.json"), encoding="utf-8").read())
        texts += [(it["text"], it["month"]) for it in ref.get("items", []) if it.get("airline") == airline]
    except (OSError, ValueError):
        pass

    def feed(text, month):
        y, m = int(month[:4]), int(month[5:7])
        days, _w = roster_import.parse(text, y, m)
        for d in days:
            if d.get("kind") != "voo":
                continue
            legs = []
            for sg in d.get("segments") or []:
                r, nums = sg["route"], sg.get("numbers") or []
                for i in range(len(r) - 1):
                    n = nums[i] if i < len(nums) else ""
                    if n and not (sg.get("dhd") or [False] * len(r))[i]:
                        legs.append((airline + n.lstrip("0"), r[i], r[i + 1]))
                    else:
                        legs.append((None, r[i], r[i + 1]))
            wd = date.fromisoformat(str(d["date"])[:10]).weekday()
            for (xa, xf, xt), (ya, yf, yt) in zip(legs, legs[1:]):
                if xa and ya and xt == yf:
                    out[xa][ya][wd] += 1
    for text, month in texts:
        try:
            feed(text, month)
        except Exception:                                  # noqa: BLE001 — texto de referência fora do formato
            log.exception("escala real de referência")
    try:                                                   # as que você importou (motor: schedules com imported=True)
        from . import motor
        c = motor.connect(data_dir)
        try:
            for r in c.execute("SELECT id, params FROM schedules WHERE airline=?", (airline,)):
                try:
                    pr = json.loads(r[1] or "{}")
                except ValueError:
                    continue
                if not pr.get("imported"):
                    continue
                legs = [dict(x) for x in c.execute("SELECT flight_number, adep, ades, dep_utc, data_origin FROM schedule_legs WHERE schedule_id=? ORDER BY dep_utc", (r[0],))]
                for a, b in zip(legs, legs[1:]):
                    if a["flight_number"] and b["flight_number"] and a["ades"] == b["adep"] and a["dep_utc"][:10] == b["dep_utc"][:10]:
                        wd = date.fromisoformat(a["dep_utc"][:10]).weekday()
                        out[a["flight_number"]][b["flight_number"]][wd] += 1
        finally:
            c.close()
    except Exception:                                      # noqa: BLE001
        log.exception("escalas importadas")
    return {x: {y: dict(w) for y, w in ys.items()} for x, ys in out.items()}


def analyze(data_dir, airline: str) -> dict:
    """Todas as continuações da companhia numa semana de referência do RPL (a semana publicada). Em cache."""
    idx, rec, days_obs = _load(data_dir, airline)
    cf, ct = idx.coverage()
    ck = (str(data_dir), airline, cf, ct, days_obs, len(idx.flights))
    with _LOCK:
        if ck in _CACHE:
            return _CACHE[ck]
    start = date.fromisoformat(cf) if cf else date.today()
    occ = []
    for k in range(8):                                    # 7 dias + o seguinte (continuação depois da meia-noite)
        occ += idx.occurrences(start + timedelta(days=k), projection=True)
    by_adep = defaultdict(list)
    for o in occ:
        by_adep[o["adep"]].append(o)
    for v in by_adep.values():
        v.sort(key=lambda o: o["dep"])
    flights = rec.get("flights") or {}
    weeks = max(days_obs / 7, 1e-9)
    per = {}                                              # chave de X → dados
    for o in occ:
        d = date.fromisoformat(o["date"])
        if d >= start + timedelta(days=7):
            continue
        kx = _key(o["ident"], o["adep"], o["ades"])
        x = per.setdefault(kx, {"ident": o["ident"], "adep": o["adep"], "ades": o["ades"], "weekdays": set(), "cands": {},
                                "obs": flights.get(kx, {})})
        wd = d.weekday()
        x["weekdays"].add(wd)
        lo, hi = o["arr"] + timedelta(minutes=GAP_MIN), o["arr"] + timedelta(minutes=GAP_MAX)
        for y in by_adep.get(o["ades"], []):
            if y["dep"] < lo:
                continue
            if y["dep"] > hi:
                break
            if y["ident"] == o["ident"] and y["ades"] == o["adep"]:
                continue
            ky = _key(y["ident"], y["adep"], y["ades"])
            cand = x["cands"].setdefault(ky, {"ident": y["ident"], "adep": y["adep"], "ades": y["ades"], "weekdays": set(),
                                              "gaps": [], "delta": (_num(y["ident"]) - _num(o["ident"]))
                                              if _num(y["ident"]) is not None and _num(o["ident"]) is not None else None})
            cand["weekdays"].add(wd)
            cand["gaps"].append(int((y["dep"] - o["arr"]).total_seconds() // 60))
    # sentido mais comum do par de numeração, medido nos pares confirmados pelo ADS-B
    sign = defaultdict(int)
    for kx, x in per.items():
        for ky, c in x["cands"].items():
            if c["delta"] in (1, -1) and (x["obs"].get("next") or {}).get(ky, 0) >= 2:
                sign[c["delta"]] += 1
    usual = 1 if sign[1] > sign[-1] else -1
    real = real_successions(data_dir, airline)
    out = {}
    for kx, x in per.items():
        nxt = x["obs"].get("next") or {}
        rx = real.get(x["ident"], {})
        ranked = []
        for ky, c in x["cands"].items():
            seen = int(nxt.get(ky, 0))
            both = len(c["weekdays"])
            opp = max(1.0, weeks * both)                  # vezes em que X e Y voaram juntos no período do ADS-B
            rate = seen / opp
            pair = c["delta"] in (1, -1)
            rw = rx.get(c["ident"], {})                   # visto em escala real de piloto: {dia da semana: vezes}
            nreal = sum(rw.values())
            score = rate * 10 + (1.5 if pair else 0) + min(seen, 6) * 0.2 + min(nreal, 3) * 2.5
            if not seen and not pair and not nreal:
                continue
            conf = ("alta" if (seen >= 3 and rate >= 0.5) or nreal >= 2 or (nreal and seen) else
                    "média" if (seen >= 2 and rate >= 0.25) or (pair and seen >= 1) or nreal else "baixa")
            wds = set(c["weekdays"]) | set(rw)
            ranked.append({"ident": c["ident"], "adep": c["adep"], "ades": c["ades"], "weekdays": sorted(wds),
                           "gap_min": sorted(c["gaps"])[len(c["gaps"]) // 2], "adsb": seen, "rate": round(rate, 2), "real": nreal,
                           "real_weekdays": sorted(rw), "pair": pair, "inverted": pair and c["delta"] != usual, "delta": c["delta"],
                           "kind": "volta" if c["ades"] == x["adep"] else "segue", "confidence": conf, "score": round(score, 3)})
        for yid, rw in rx.items():                        # continuação vista em escala real que o RPL da semana não tem na janela
            if any(r["ident"] == yid for r in ranked):
                continue
            yo = next((o for o in occ if o["ident"] == yid and o["adep"] == x["ades"]), None)
            if not yo:
                continue
            nreal = sum(rw.values())
            ranked.append({"ident": yid, "adep": yo["adep"], "ades": yo["ades"], "weekdays": sorted(rw), "gap_min": None, "adsb": 0,
                           "rate": 0.0, "real": nreal, "real_weekdays": sorted(rw), "pair": False, "inverted": False, "delta": None,
                           "kind": "volta" if yo["ades"] == x["adep"] else "segue", "confidence": "alta" if nreal >= 2 else "média",
                           "score": round(min(nreal, 3) * 2.5, 3), "note": "visto em escala real; o RPL desta semana não tem o voo nesse horário"})
        ranked.sort(key=lambda r: -r["score"])
        best_by_wd = {}
        for wd in sorted(set(x["weekdays"]) | set(w for r in ranked for w in r.get("real_weekdays", []))):
            b = next((r for r in ranked if wd in r.get("real_weekdays", [])), None) or next((r for r in ranked if wd in r["weekdays"]), None)
            if b:
                best_by_wd[wd] = b["ident"]
        keep = ranked[:6] + [r for r in ranked[6:] if r["ident"] in best_by_wd.values()]   # o melhor de cada dia sempre aparece
        out[kx] = {"ident": x["ident"], "adep": x["adep"], "ades": x["ades"], "weekdays": sorted(x["weekdays"]),
                   "observed": int(x["obs"].get("n", 0)), "nights": int(x["obs"].get("night", 0)), "candidates": keep,
                   "best_by_weekday": best_by_wd}
    res = {"airline": airline, "reference_week": [start.isoformat(), (start + timedelta(days=6)).isoformat()],
           "adsb_days": days_obs, "pair_sign": usual, "flights": out,
           "real_flights": len(real),
           "source": "RPL do CGNA (semana publicada) + rodízios vistos no ADS-B (adsb.lol, ODbL) + escalas reais de pilotos"}
    with _LOCK:
        _CACHE.clear()
        _CACHE[ck] = res
    return res


def wd_label(wds) -> str:
    wds = sorted(set(wds))
    return "todos os dias" if len(wds) == 7 else ", ".join(WD[w] for w in wds)


# ------------------------------------------------------------------ regras informadas (o que o piloto disse)
def rules_list(conn, airline: str | None = None) -> list[dict]:
    q, a = "SELECT * FROM continuation_rule", []
    if airline:
        q += " WHERE airline=?"
        a.append(airline)
    out = rows(conn.execute(q + " ORDER BY from_ident, id", a))
    for r in out:
        r["weekdays"] = [int(x) for x in (r["weekdays"] or "")]
    return out


def _norm_ident(airline: str, v: str) -> str:
    v = (v or "").strip().upper().replace(" ", "")
    if v.isdigit():
        v = airline + v
    if not (4 <= len(v) <= 8) or not v[:3].isalpha():
        raise FGVError(f"Número de voo inválido: {v or '(vazio)'} (ex.: TAM3376 ou 3376).", code="campo_invalido")
    return v


def _norm_target(conn, airline: str, v: str) -> str:
    """A continuação é um voo (TAM3262, 3262) ou um destino (PNZ, SBPL → "@SBPL": qualquer voo para lá)."""
    from .troca import resolve_airport
    raw = (v or "").strip().upper()
    if raw and not any(ch.isdigit() for ch in raw):
        icao = resolve_airport(conn, raw.lstrip("@"))
        if not icao:
            raise FGVError(f"Aeroporto {raw} não encontrado (use ICAO, ex. SBPL, ou IATA, ex. PNZ).", code="campo_invalido")
        return "@" + icao
    return _norm_ident(airline, raw)


def rule_add(conn, airline: str, from_ident: str, to_ident: str, weekdays=None, note: str | None = None) -> dict:
    a, b = _norm_ident(airline, from_ident), _norm_target(conn, airline, to_ident)
    if a == b:
        raise FGVError("O voo e a continuação são o mesmo.", code="campo_invalido")
    wds = sorted({int(x) for x in (weekdays or [])})
    if any(not 0 <= x <= 6 for x in wds):
        raise FGVError("Dias da semana: 0 (seg) a 6 (dom).", code="campo_invalido")
    with tx(conn):
        rid = conn.execute("INSERT INTO continuation_rule(airline, from_ident, to_ident, weekdays, note, created_at) VALUES (?,?,?,?,?,?)",
                           (airline, a, b, "".join(str(x) for x in wds), (note or "").strip()[:200] or None, now_iso())).lastrowid
    return next(r for r in rules_list(conn) if r["id"] == rid)


def rule_delete(conn, rid: int) -> dict:
    with tx(conn):
        n = conn.execute("DELETE FROM continuation_rule WHERE id=?", (rid,)).rowcount
    if not n:
        raise NotFound("Regra não encontrada.")
    return {"ok": True}


# ------------------------------------------------------------------ consultas
def lookup(conn, data_dir, airline: str, q: str) -> dict:
    """Busca por número (3376, TAM3376) ou trecho (REC, SBRF-SBPL)."""
    from .troca import resolve_airport
    a = analyze(data_dir, airline)
    q = (q or "").strip().upper()
    if not q:
        raise FGVError("Digite um número de voo ou um aeroporto.", code="campo_invalido")
    hits = []
    num = q[len(airline):] if q.startswith(airline) else q
    if num.isdigit():
        hits = [f for f in a["flights"].values() if _num(f["ident"]) == int(num)]
    else:
        parts = [resolve_airport(conn, p) or p for p in q.replace("–", "-").split("-") if p]
        hits = [f for f in a["flights"].values() if all(p in (f["adep"], f["ades"]) for p in parts)]
    rules = rules_list(conn, airline)
    out = []
    for f in sorted(hits, key=lambda f: (f["ident"], f["adep"]))[:40]:
        out.append({**f, "rules": [r for r in rules if r["from_ident"] == f["ident"]]})
    return {"query": q, "items": out, "adsb_days": a["adsb_days"], "reference_week": a["reference_week"], "source": a["source"],
            "pair_sign": a["pair_sign"]}


def for_leg(conn, data_dir, leg: dict) -> dict | None:
    """A continuação provável de uma perna da escala, no dia da semana dela (UTC da partida, como o RPL)."""
    from . import journey
    airline = leg.get("airline")
    if not leg.get("flight_number") or not airline:
        return None
    try:
        a = analyze(data_dir, airline)
    except FGVError:
        return None
    f = a["flights"].get(_key(leg["flight_number"], leg["adep"], leg["ades"]))
    wd = journey.parse(leg["dep_utc"]).weekday()
    rule = next((r for r in sorted(rules_list(conn, airline), key=lambda r: not r["weekdays"]) if r["from_ident"] == leg["flight_number"]
                 and (not r["weekdays"] or wd in r["weekdays"])), None)
    if rule:
        t = rule["to_ident"]
        return {"ident": None if t.startswith("@") else t, "to_airport": t[1:] if t.startswith("@") else None, "source": "regra",
                "confidence": "regra", "note": rule["note"], "weekdays": rule["weekdays"]}
    if not f:
        return None
    best = next((c for c in f["candidates"] if wd in c["weekdays"]), None)
    if not best:
        return {"ident": None, "source": "adsb", "nights": f["nights"], "observed": f["observed"]}
    return {**best, "source": "identificada", "observed": f["observed"], "nights": f["nights"]}


# força das continuações identificadas: a partir de quanto cada confiança entra, e quanto ela pesa
CONF_MIN = {"alta": 1, "média": 30, "baixa": 80}
CONF_W = {"alta": 1.0, "média": 0.85, "baixa": 0.6}
DEFAULT_STRENGTH = 70


def follow_map(conn, data_dir, airline: str, strength: int = DEFAULT_STRENGTH, auto: bool = True) -> dict:
    """{voo: {dia_da_semana: {"to", "p", "src", "conf"}}} para o gerador.

    strength (0 a 100) é o slider "Força das continuações" do gerador:
    * 0: desligado (nem as suas regras).
    * As SUAS regras valem sempre (p = 1) com a força acima de 0: foi você (ou o piloto) quem disse.
    * As IDENTIFICADAS (RPL + ADS-B) entram sozinhas na geração: confiança alta a partir de 1, média a partir de 30, baixa
      a partir de 80. Cada vez que o voo X aparece, a continuação é obrigatória com chance p = força × peso da confiança
      (alta 1, média 0,85, baixa 0,6), sorteada pela semente da escala (mesma semente, mesma escala). Na força 100, a
      continuação de confiança alta vem sempre.
    """
    strength = max(0, min(100, int(strength or 0)))
    if strength == 0:
        return {}
    out: dict = defaultdict(dict)
    if auto:
        try:
            a = analyze(data_dir, airline)
        except FGVError:
            a = {"flights": {}}
        for f in a["flights"].values():
            for wd, y in f["best_by_weekday"].items():
                c = next((c for c in f["candidates"] if c["ident"] == y), None)
                if not c or strength < CONF_MIN.get(c["confidence"], 101):
                    continue
                out[f["ident"]][str(wd)] = {"to": y, "p": round(strength / 100 * CONF_W[c["confidence"]], 3),
                                            "src": "auto", "conf": c["confidence"]}
    # o que você cadastrou vale mais; a regra com dias da semana vale mais que a de todos os dias
    for r in sorted(rules_list(conn, airline), key=lambda r: bool(r["weekdays"])):
        for wd in (r["weekdays"] or range(7)):
            out[r["from_ident"]][str(wd)] = {"to": r["to_ident"], "p": 1.0, "src": "regra", "conf": None}
    return {k: dict(v) for k, v in out.items()}


def entry(v) -> dict | None:
    """Uma entrada do mapa (aceita o formato antigo, só o destino em texto, como regra)."""
    if not v:
        return None
    return {"to": v, "p": 1.0, "src": "regra", "conf": None} if isinstance(v, str) else v


def roll(seed, ident: str, day) -> float:
    """Sorteio estável de 0 a 1 para (semente, voo, dia): mesma semente, mesma escala."""
    import hashlib
    h = hashlib.sha1(f"{seed}|{ident}|{day}".encode()).hexdigest()[:8]
    return int(h, 16) / 0x100000000


def summarize(legs: list[dict], follow: dict) -> dict:
    """Quantas vezes, na escala pronta, o voo seguinte foi a continuação (das suas regras e das identificadas)."""
    from datetime import datetime
    n = {"regra": 0, "auto": 0, "known": 0}
    for a, b in zip(legs, legs[1:] + [None]):
        e = entry((follow.get(a.get("flight_number") or "") or {}).get(str(datetime.fromisoformat(a["dep_utc"].replace("Z", "+00:00")).weekday())))
        if not e:
            continue
        n["known"] += 1
        if not b or a["ades"] != b["adep"]:
            continue
        y = e["to"]
        if (y.startswith("@") and b["ades"] == y[1:]) or b.get("flight_number") == y:
            n[e["src"]] += 1
    return n
