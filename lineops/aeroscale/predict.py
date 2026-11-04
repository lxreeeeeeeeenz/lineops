"""Previsão de continuidade operacional (rodízio provável de uma aeronave).

O RPL diz QUAIS voos existem, mas não QUAL aeronave faz cada um. Este módulo estima, para um voo
que termina num aeroporto, quais voos do RPL têm maior probabilidade de serem a próxima perna da
mesma aeronave — e classifica os intervalos (solo, solo longo, pernoite, parada noturna curta,
lacuna de dados).

Modelo: log-linear (multinomial logit) sobre os candidatos possíveis, com pesos calibrados nas
amostras observadas (aeroscale/data/observed_samples.json: 2 aeronaves B738 da GOL, 1 A320neo da
Azul, 1 A320/A320neo da LATAM, 23 a 31/10/2026). Padrões encontrados nessas amostras:

* bate-voltas quase sempre usam números consecutivos (ex.: G31854 GIG→NVT e G31855 NVT→GIG);
* voos com mais de uma etapa mantêm o número na mesma aeronave;
* solo programado mínimo de 30–40 min; a maioria entre 40 e 60 min;
* pernoites com chegada entre 20:00 e 03:00 e saída entre 04:00 e 09:00, muitas vezes fora dos hubs;
* o tipo registrado no RPL nem sempre é o da aeronave que voa (B738 voando voos registrados como
  B38M; A320 voando voos registrados como A321) – por isso a compatibilidade é por família, com
  bônus quando o tipo coincide.

Toda previsão sai com probabilidade, nível de confiança e a lista de motivos (pontuação de cada
característica). Previsões nunca viram dados confirmados e nunca recebem número de voo inventado:
os candidatos são sempre voos existentes no RPL (ou projetados do padrão semanal, sinalizados).
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import date, datetime, time, timedelta, timezone

from . import catalog
from .airports import BUILTIN_AIRPORTS, get_tz, to_local

# Pesos calibrados por busca coordenada maximizando a log-verossimilhança das 76 transições
# observadas nas amostras (resultado na calibração: próxima perna real em 1º lugar em ~43% dos casos e
# entre as 3 primeiras em ~68%, com 50–100 alternativas por aeroporto nos hubs; a tela "Previsões" mostra
# o resultado atualizado com os RPL ativos).
WEIGHTS = {
    "same_ident": 4.0,      # mesmo número de voo continua (voo com escalas)
    "pair_return": 1.5,     # volta com número consecutivo (bate-volta)
    "reverse": 0.5,         # volta para a origem anterior
    "turn_slope": 1.2,      # penalidade por hora de solo acima do solo típico
    "long_ground": 1.5,     # penalidade extra para solo diurno longo (> 4 h)
    "overnight": 3.5,       # custo de encerrar o dia e pernoitar
    "first_bank": 0.0,      # bônus ao 1º voo da manhã após pernoite (sem ganho nas amostras)
    "type_exact": 0.6,      # tipo do RPL igual ao da aeronave
    "freq": 0.5,            # regularidade do voo na semana (dias que opera / 7)
}
# Rodízio observado no ADS-B (ops.py). Fora de WEIGHTS de propósito: a calibração usa esse mesmo ADS-B e
# não pode "aprender" com a resposta. Valores escolhidos medindo as escalas geradas contra o ADS-B.
OBS_W = {
    "obs_link": 4.5,    # × fração das vezes em que a mesma aeronave fez este voo logo depois
    "obs_seen": 0.8,    # sucessão vista pelo menos uma vez
    "obs_never": 4.0,   # × concentração do sucessor mais visto, quando este nunca apareceu (n ≥ 3)
    "obs_night": 1.5,   # pernoite depois de um voo que costuma (ou nunca costuma) encerrar o dia
    "obs_first": 1.0,   # 1º voo da manhã que costuma abrir o dia
}
OBS_MIN_N = 3
TURN_TYPICAL = 90          # minutos de solo sem penalidade (mediana observada 60–75 min)
MIN_TURN = {"GLO": 30, "AZU": 40, "TAM": 40}       # menor solo programado observado (STA→STD)
OBSERVED_TURN_P10 = {"GLO": 40, "AZU": 40, "TAM": 40}  # 10º percentil observado (STA→STD)
DEFAULT_MIN_TURN = 35
HORIZON_H = 22             # até quantas horas após a chegada procurar a próxima perna

def weights_by_airline(settings: dict) -> dict:
    """Pesos calibrados com ADS-B guardados nas configurações ({"GLO": {...}}); vazio = pesos padrão."""
    out = {}
    for al, w in (settings.get("model_weights") or {}).items():
        if isinstance(w, dict):
            out[al] = {k: float(v) for k, v in w.items() if k in WEIGHTS}
    return out


def weights_for(settings: dict, airline: str) -> dict | None:
    return weights_by_airline(settings).get(airline)


_SAMPLES_PATH = os.path.join(os.path.dirname(__file__), "data", "observed_samples.json")


def _num(ident: str | None) -> int | None:
    m = re.search(r"(\d+)$", ident or "")
    return int(m.group(1)) if m else None


def _local(dt: datetime, tz_of, icao: str) -> datetime:
    return to_local(dt, tz_of(icao))


def gap_kind(arr: datetime, dep: datetime, ap: str, tz_of, min_turn: int) -> str:
    """Classifica o intervalo em solo entre duas pernas da mesma aeronave."""
    g = (dep - arr).total_seconds() / 60
    if g < 0:
        return "sobreposicao"
    if g < min_turn:
        return "solo_curto"
    la, ld = _local(arr, tz_of, ap), _local(dep, tz_of, ap)
    night_arr = la.hour >= 20 or la.hour < 4
    morning_dep = 3 <= ld.hour < 11
    if g >= 30 * 60:
        return "parada_longa"
    if g >= 4 * 60 and (night_arr or (la.date() != ld.date())) and morning_dep:
        return "pernoite"
    if g >= 6 * 60 and la.date() != ld.date():
        return "pernoite"
    if g >= 60 and la.hour < 4 and ld.hour < 7 and la.date() == ld.date():
        return "parada_noturna_curta"
    if g > 4 * 60:
        return "solo_longo"
    return "solo"


GAP_LABEL = {
    "sobreposicao": "Sobreposição (horários incompatíveis)",
    "solo_curto": "Solo abaixo do mínimo observado",
    "solo": "Solo normal",
    "solo_longo": "Solo longo (aeronave parada de dia)",
    "pernoite": "Pernoite",
    "parada_noturna_curta": "Parada noturna curta (madrugada)",
    "parada_longa": "Aeronave parada > 30 h (manutenção ou falta de dados)",
    "lacuna_dados": "Lacuna de dados: a perna seguinte parte de outro aeroporto",
}


class RotationModel:
    """Distribuição de probabilidade da próxima perna de uma aeronave."""

    def __init__(self, idx, airline: str, ac_type: str | None, tz_of, projection=True, type_mode="family",
                 weights: dict | None = None, ops=None):
        self.w = {**WEIGHTS, **(weights or {})}
        self.ops = ops if ops is not None and ops.has_data else None
        self.idx = idx
        self.airline = airline
        self.ac_type = ac_type
        self.tz_of = tz_of
        self.projection = projection
        self.type_mode = type_mode
        self.min_turn = MIN_TURN.get(airline, DEFAULT_MIN_TURN)
        self._cache: dict[str, list] = {}
        self._day_ap: dict = {}
        days: dict[str, set] = {}
        for f in idx.flights:  # união dos dias de operação de todos os registros do voo
            days.setdefault(f["ident"], set()).update(i for i, c in enumerate(f["days"]) if c != "0")
        self.freq = {k: len(v) for k, v in days.items()}

    def _by_ap(self, d: date) -> dict:
        if d not in self._day_ap:
            m: dict[str, list] = {}
            for o in self.idx.occurrences(d, self.projection):
                m.setdefault(o["adep"], []).append(o)
            self._day_ap[d] = m
        return self._day_ap[d]

    def compatible(self, t: str) -> tuple[bool, bool]:
        if not self.ac_type:
            return True, False
        exact = t == self.ac_type
        if exact:
            return True, True
        fam_ok = catalog.type_family(t) == catalog.type_family(self.ac_type)
        if self.type_mode == "exact":
            return False, False
        return fam_ok, False

    def candidates(self, cur: dict, exclude: set | None = None) -> list[dict]:
        key = cur["ref"]
        if key in self._cache and not exclude:
            return self._cache[key]
        ap = cur["ades"]
        lo = cur["arr"] + timedelta(minutes=self.min_turn)
        hi = cur["arr"] + timedelta(hours=HORIZON_H)
        occs = []
        d = lo.date() - timedelta(days=1)
        while d <= hi.date():
            occs += [o for o in self._by_ap(d).get(ap, []) if lo <= o["dep"] <= hi]
            d += timedelta(days=1)
        scored = []
        cur_num = _num(cur["ident"])
        overnight_first = None
        for o in sorted(occs, key=lambda o: o["dep"]):
            if exclude and o["ref"] in exclude:
                continue
            ok, exact = self.compatible(o["type_icao"])
            if not ok or o["ref"] == cur["ref"]:
                continue
            W = self.w
            feats = []
            x: dict[str, float] = {}   # característica bruta (com sinal); pontuação = soma de W[k] * x[k]

            def add(k, val, label, text):
                x[k] = x.get(k, 0.0) + val
                feats.append((label, W[k] * val, text))
            kind = gap_kind(cur["arr"], o["dep"], ap, self.tz_of, self.min_turn)
            g = (o["dep"] - cur["arr"]).total_seconds() / 60
            if o["ident"] == cur["ident"]:
                add("same_ident", 1.0, "same_ident", "mesmo número de voo continua (voo com escalas)")
            on = _num(o["ident"])
            if o["ades"] == cur["adep"]:
                if cur_num is not None and on is not None and abs(on - cur_num) == 1:
                    add("pair_return", 1.0, "pair_return", f"bate-volta com número consecutivo ({cur['ident']}/{o['ident']})")
                else:
                    add("reverse", 1.0, "reverse", f"volta para {cur['adep']}")
            if kind == "pernoite":
                add("overnight", -1.0, "overnight", f"pernoite em {ap}")
                if overnight_first is None:
                    overnight_first = o["ref"]
                    if W.get("first_bank"):
                        add("first_bank", 1.0, "first_bank", "primeira saída da manhã")
                    else:
                        x["first_bank"] = 1.0
            else:
                extra = max(0.0, g - TURN_TYPICAL) / 60
                if extra:
                    add("turn_slope", -extra, "turn", f"{int(g)} min de solo")
                else:
                    feats.append(("turn", 0.0, f"{int(g)} min de solo (típico)"))
                if kind == "solo_longo":
                    add("long_ground", -1.0, "long_ground", "aeronave parada por mais de 4 h de dia")
            if self.ops is not None:
                for lab, val, text in self.observed_features(cur, o, kind):
                    feats.append((lab, OBS_W[lab] * val, text))
            if exact:
                add("type_exact", 1.0, "type_exact", f"tipo do RPL igual ({o['type_icao']})")
            elif self.ac_type:
                feats.append(("type_family", 0.0, f"RPL prevê {o['type_icao']} (mesma família)"))
            fq = self.freq.get(o["ident"], 0) / 7
            add("freq", fq, "freq", f"opera {self.freq.get(o['ident'], 0)} dia(s)/semana")
            if o["origin"] == "PROJETADO":
                feats.append(("projected", 0.0, "projetado do padrão semanal (não garantido)"))
            score = sum(w for _k, w, _t in feats)
            scored.append({"occ": o, "score": score, "kind": kind, "ground_min": int(g), "features": feats, "x": x})
        if scored:
            mx = max(c["score"] for c in scored)
            z = sum(math.exp(c["score"] - mx) for c in scored)
            for c in scored:
                c["p"] = math.exp(c["score"] - mx) / z
            scored.sort(key=lambda c: -c["p"])
        if not exclude:
            self._cache[key] = scored
        return scored

    def observed_features(self, cur: dict, o: dict, kind: str) -> list[tuple[str, float, str]]:
        """O que o ADS-B diz sobre a transição cur→o (mesma matrícula)."""
        out = []
        f = self.ops.flight(cur["ident"], cur["adep"], cur["ades"])
        if not f or not f["n"]:
            return out
        n = f["n"]
        if kind in ("pernoite", "parada_noturna_curta"):
            share = f["night"] / n
            if n >= OBS_MIN_N and share >= 0.5:
                out.append(("obs_night", share, f"{cur['ident']} encerrou o dia em {f['night']} de {n} vezes (ADS-B)"))
            elif n >= OBS_MIN_N + 1 and f["night"] == 0:
                out.append(("obs_night", -1.0, f"{cur['ident']} nunca encerrou o dia em {n} observações (ADS-B)"))
            g = self.ops.flight(o["ident"], o["adep"], o["ades"])
            if g and g["first"] >= 2 and g["first"] >= 0.5 * max(g["n"], g["first"]):
                out.append(("obs_first", 1.0, f"{o['ident']} abriu o dia em {g['first']} observações (ADS-B)"))
            return out
        c = int(f["next"].get(f"{o['ident'] or '?'}:{o['adep']}-{o['ades']}", 0))
        if c:
            out.append(("obs_link", c / n, f"mesma aeronave fez {cur['ident']} → {o['ident']} em {c} de {n} vezes (ADS-B)"))
            out.append(("obs_seen", 1.0, "sequência vista no rodízio real"))
        elif n >= OBS_MIN_N:
            top = max([f["night"], *f["next"].values()]) / n
            out.append(("obs_never", -top, f"nunca visto depois de {cur['ident']} em {n} observações (ADS-B)"))
        return out

    def logp(self, cur: dict, nxt: dict) -> float | None:
        for c in self.candidates(cur):
            if c["occ"]["ref"] == nxt["ref"]:
                return math.log(max(c["p"], 1e-9))
        return None

    def prob(self, cur: dict, nxt: dict) -> tuple[float | None, int | None]:
        for i, c in enumerate(self.candidates(cur)):
            if c["occ"]["ref"] == nxt["ref"]:
                return c["p"], i + 1
        return None, None

    def chain(self, cur: dict, steps: int = 4, until: datetime | None = None) -> list[dict]:
        """Sequência mais provável (gulosa) a partir de uma perna."""
        out = []
        acc = 1.0
        used = {cur["ref"]}
        node = cur
        for _ in range(steps):
            cands = [c for c in self.candidates(node) if c["occ"]["ref"] not in used]
            if not cands:
                break
            top = cands[0]
            acc *= top["p"]
            used.add(top["occ"]["ref"])
            out.append({**describe(top, len(cands)), "chain_p": acc})
            node = top["occ"]
            if until and node["dep"] > until:
                break
        return out


def confidence(p: float, n: int, feats: list) -> tuple[str, str]:
    structural = any(k in ("same_ident", "pair_return") for k, _w, _t in feats)
    if p >= 0.6 or (p >= 0.45 and structural):
        lvl = "alta"
    elif p >= 0.3:
        lvl = "média"
    else:
        lvl = "baixa"
    why = []
    if not structural:
        why.append("sem evidência estrutural (número consecutivo ou mesmo voo)")
    if n >= 8:
        why.append(f"{n} alternativas possíveis")
    return lvl, "; ".join(why)


def describe(c: dict, n: int) -> dict:
    o = c["occ"]
    lvl, why = confidence(c["p"], n, c["features"])
    return {
        "ref": o["ref"], "ident": o["ident"], "adep": o["adep"], "ades": o["ades"], "type_icao": o["type_icao"],
        "dep_utc": o["dep"].strftime("%Y-%m-%dT%H:%M:%SZ"), "arr_utc": o["arr"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "origin": o["origin"], "p": round(c["p"], 4), "confidence": lvl, "confidence_note": why,
        "gap_kind": c["kind"], "gap_label": GAP_LABEL.get(c["kind"], c["kind"]), "ground_min": c["ground_min"],
        "reasons": [{"feature": k, "weight": round(w, 2), "text": t} for k, w, t in c["features"]],
        "candidates": n,
    }


# ------------------------------------------------------------------ análise de sequência
def analyze_sequence(legs: list[dict], tz_of, min_turn: int = DEFAULT_MIN_TURN) -> dict:
    """legs: dicts com adep, ades, dep (datetime), arr (datetime), ident. Retorna intervalos classificados,
    bate-voltas, pernoites e base estimada."""
    gaps, overnights, turnbacks = [], [], []
    for i, (a, b) in enumerate(zip(legs, legs[1:])):
        if b["adep"] != a["ades"]:
            kind = "lacuna_dados"
        else:
            kind = gap_kind(a["arr"], b["dep"], a["ades"], tz_of, min_turn)
        gaps.append({"after": i, "airport": a["ades"], "kind": kind, "label": GAP_LABEL.get(kind, kind),
                     "minutes": int((b["dep"] - a["arr"]).total_seconds() // 60)})
        if kind in ("pernoite", "parada_longa"):
            overnights.append(a["ades"])
    # base: aeroporto com mais pernoites; empate -> mais partidas
    counts: dict[str, int] = {}
    for ap in overnights:
        counts[ap] = counts.get(ap, 0) + 1
    deps: dict[str, int] = {}
    for l in legs:
        deps[l["adep"]] = deps.get(l["adep"], 0) + 1
    base, base_conf = None, "baixa"
    if counts:
        base = max(counts, key=lambda k: (counts[k], deps.get(k, 0)))
        share = counts[base] / len(overnights)
        base_conf = "alta" if share >= 0.6 and len(overnights) >= 3 else "média" if share >= 0.4 else "baixa"
    elif deps:
        base = max(deps, key=deps.get)
    # bate-voltas: A -> X -> A sem pernoite em X
    for i in range(len(legs) - 1):
        a, b = legs[i], legs[i + 1]
        if b["adep"] == a["ades"] and b["ades"] == a["adep"] and gaps[i]["kind"] in ("solo", "solo_longo"):
            na, nb = _num(a["ident"]), _num(b["ident"])
            turnbacks.append({"start": a["adep"], "via": a["ades"], "legs": [i, i + 1],
                              "paired_numbers": na is not None and nb is not None and abs(na - nb) == 1})
    return {"gaps": gaps, "overnights": overnights, "turnbacks": turnbacks, "base": base,
            "base_confidence": base_conf}


# ------------------------------------------------------------------ calibração / qualidade
def load_samples() -> list[dict]:
    with open(_SAMPLES_PATH, encoding="utf-8") as fh:
        return json.load(fh)["samples"]


def sample_to_legs(sample: dict, idx, tz_of) -> list[dict]:
    iata = {v[0]: k for k, v in BUILTIN_AIRPORTS.items()}
    out = []
    for d, o, de, fn, std, sta, atd in sample["legs"]:
        oi, di = iata.get(o), iata.get(de)
        ident = sample["airline"] + re.sub(r"^[A-Z0-9]{2}", "", fn)
        tzo = get_tz(tz_of(oi)) if oi else timezone.utc
        tzd = get_tz(tz_of(di)) if di else timezone.utc
        dd = date.fromisoformat(d)
        dep = datetime.combine(dd, datetime.strptime(std, "%H:%M").time(), tzo or timezone.utc).astimezone(timezone.utc)
        arr = datetime.combine(dd, datetime.strptime(sta, "%H:%M").time(), tzd or timezone.utc).astimezone(timezone.utc)
        if arr < dep:
            arr += timedelta(days=1)
        delay = None
        if atd:
            ad = datetime.combine(dd, datetime.strptime(atd, "%H:%M").time(), tzo or timezone.utc).astimezone(timezone.utc)
            if ad < dep - timedelta(hours=12):
                ad += timedelta(days=1)
            delay = int((ad - dep).total_seconds() // 60)
        occ = None
        if oi and idx is not None:
            for x in idx.occurrences(dep.date(), True):
                if x["ident"] == ident and x["adep"] == oi and abs((x["dep"] - dep).total_seconds()) <= 300:
                    occ = x
                    break
        out.append({"ident": ident, "flight": fn, "adep": oi or o, "ades": di or de, "dep": dep, "arr": arr,
                    "delay": delay, "occ": occ, "iata": (o, de)})
    out.sort(key=lambda l: l["dep"] + timedelta(minutes=max(0, l["delay"] or 0)))
    return out


def backtest(db, get_index_fn, tz_of, weights: dict | None = None) -> dict:
    """Mede o modelo nas transições observadas: em quantas a próxima perna real é a 1ª/3 primeiras.
    weights: pesos únicos ({"overnight": 3.5, …}) ou por companhia ({"GLO": {…}})."""
    res = {"samples": [], "transitions": 0, "top1": 0, "top3": 0, "loglik": 0.0, "skipped": 0}
    for s in load_samples():
        idx = get_index_fn(db, s["airline"], 10)
        w = weights.get(s["airline"]) if weights and all(isinstance(v, dict) for v in weights.values()) else weights
        model = RotationModel(idx, s["airline"], s["icao_type"], tz_of, weights=w)
        legs = sample_to_legs(s, idx, tz_of)
        st = {"id": s["id"], "label": s["label"], "airline": s["airline"], "icao_type": s["icao_type"],
              "legs": len(legs), "matched": sum(1 for l in legs if l["occ"]), "transitions": 0, "top1": 0,
              "top3": 0, "details": []}
        for a, b in zip(legs, legs[1:]):
            disrupted = (a["delay"] or 0) > 60 or (b["delay"] or 0) > 60
            if not a["occ"] or not b["occ"] or b["adep"] != a["ades"] or disrupted:
                res["skipped"] += 1
                continue
            p, rank = model.prob(a["occ"], b["occ"])
            st["transitions"] += 1
            st["top1"] += 1 if rank == 1 else 0
            st["top3"] += 1 if rank and rank <= 3 else 0
            res["loglik"] += math.log(max(p or 1e-6, 1e-6))
            st["details"].append({"from": a["ident"], "to": b["ident"], "rank": rank,
                                  "p": round(p, 3) if p else None,
                                  "n": len(model.candidates(a["occ"]))})
        seq = analyze_sequence([{**l} for l in legs], tz_of, model.min_turn)
        st["analysis"] = {"base": seq["base"], "base_confidence": seq["base_confidence"],
                          "overnights": seq["overnights"],
                          "turnbacks": len(seq["turnbacks"]),
                          "paired_turnbacks": sum(1 for t in seq["turnbacks"] if t["paired_numbers"])}
        res["transitions"] += st["transitions"]
        res["top1"] += st["top1"]
        res["top3"] += st["top3"]
        res["samples"].append(st)
    n = max(1, res["transitions"])
    res["top1_rate"] = round(res["top1"] / n, 3)
    res["top3_rate"] = round(res["top3"] / n, 3)
    res["mean_loglik"] = round(res["loglik"] / n, 3)
    return res
