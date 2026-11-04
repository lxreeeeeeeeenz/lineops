"""Reserva automática na escala.

Na geração de uma escala semanal ou mensal, o AeroScale sorteia um (ou mais) bloco(s) de reserva:

* duração: inteiro aleatório entre reserve_min_days e reserve_max_days (sempre dentro de 3–6 dias: nunca 7 dias
  de reserva seguidos);
* posição: dias consecutivos que não sejam indisponibilidade da aeronave nem folga fixa do dia da semana;
  as folgas regulamentares são planejadas depois, em volta da reserva;
* local: base (reserve_base_pct %) ou outro aeroporto escolhido ao acaso entre os que têm partidas
  compatíveis com o tipo da aeronave no RPL;
* acionamento: sorteio independente (reserve_activation_pct %). Se acionado, sorteia o dia de início e
  quantos dias (1 a reserve_activation_max_days) e o planejador monta voos reais do RPL saindo do local
  da reserva e voltando a ele no último dia. Sem sequência compatível: tentativa registrada como inviável,
  nenhum voo é inventado.

Os sorteios usam uma semente própria (params["reserve_seed"]) e o resultado fica salvo na tabela
schedule_reserves: abrir a escala de novo nunca muda nada. "Regerar tudo" ou "Novo sorteio da reserva"
fazem um sorteio novo.
"""
from __future__ import annotations

import random
import uuid
from datetime import date, timedelta

from .db import now_iso, rows

HARD_MIN, HARD_MAX = 3, 6   # nunca 7 dias de reserva seguidos (v2.2.2)

STATUS_LABEL = {
    "pendente": "a definir",
    "sem_acionamento": "NÃO ACIONADO",
    "acionado": "SIMULADO",
    "inviavel": "INVIÁVEL",
}


def clamp_days(lo, hi) -> tuple[int, int]:
    lo = max(HARD_MIN, min(HARD_MAX, int(lo)))
    hi = max(lo, min(HARD_MAX, int(hi)))
    return lo, hi


def default_blocks(kind: str, settings: dict) -> int:
    if not settings.get("reserve_enabled", True):
        return 0
    if kind in ("mensal", "semestral", "anual"):   # nas longas, por mês
        return int(settings.get("reserve_per_month", 1) or 0)
    if kind == "semanal":
        return 1
    return 0


def _runs(days: list[date], blocked: set[date]) -> list[list[date]]:
    out, cur = [], []
    for d in days:
        if d in blocked:
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(d)
    if cur:
        out.append(cur)
    return out


def draw(p: dict, start: date, end: date, blocked: set[date], base: str, candidates: list[str]) -> tuple[list[dict], list[str]]:
    """Sorteia os blocos de reserva. Devolve (blocos, avisos)."""
    n = int(p.get("reserve_blocks") or 0)
    if n <= 0:
        return [], []
    rng = random.Random(int(p["reserve_seed"]))
    lo, hi = clamp_days(p.get("reserve_min_days", HARD_MIN), p.get("reserve_max_days", HARD_MAX))
    days = []
    d = start
    while d <= end:
        days.append(d)
        d += timedelta(days=1)
    taken = set(blocked)
    blocks, warns = [], []
    for k in range(n):
        runs = [r for r in _runs(days, taken) if len(r) >= lo]
        if not runs:
            warns.append(f"Reserva {k + 1}: não há {lo} dias consecutivos livres no período (indisponibilidade ou "
                         "folgas fixas do dia da semana); bloco não incluído.")
            break
        longest = max(len(r) for r in runs)
        dur = rng.randint(lo, min(hi, longest))
        starts = [r[i] for r in runs if len(r) >= dur for i in range(len(r) - dur + 1)]
        s = rng.choice(starts)
        e = s + timedelta(days=dur - 1)
        loc_roll = rng.random() * 100
        others = [c for c in candidates if c != base]
        if loc_roll < float(p.get("reserve_base_pct", 80)) or not others:
            loc, kind = base, "base"
            if loc_roll >= float(p.get("reserve_base_pct", 80)):
                warns.append("Reserva: o sorteio indicou outro aeroporto, mas nenhum outro tem partidas compatíveis; "
                             "ficou na base.")
        else:
            loc, kind = rng.choice(sorted(others)), "outro"
        act_roll = rng.random() * 100
        activated = act_roll < float(p.get("reserve_activation_pct", 50))
        act_start = act_days = None
        if activated:
            bdays = [s + timedelta(days=i) for i in range(dur)]
            act_start = rng.choice(bdays)
            left = (e - act_start).days + 1
            act_days = rng.randint(1, max(1, min(int(p.get("reserve_activation_max_days", 3) or 1), left)))
        blocks.append({"uid": uuid.uuid4().hex, "start_date": s, "end_date": e, "days": dur, "location": loc,
                       "location_kind": kind, "location_roll": round(loc_roll, 2),
                       "base_pct": float(p.get("reserve_base_pct", 80)),
                       "activation_pct": float(p.get("reserve_activation_pct", 50)),
                       "activation_roll": round(act_roll, 2), "activated": activated,
                       "act_start": act_start, "act_days": act_days, "status": "pendente", "note": "",
                       "seed": int(p["reserve_seed"])})
        # um dia de folga entre blocos, para não emendar duas reservas
        taken |= {s + timedelta(days=i) for i in range(-1, dur + 1)}
    blocks.sort(key=lambda b: b["start_date"])
    return blocks, warns


def save(db, sid: int, blocks: list[dict]):
    ts = now_iso()
    for b in blocks:
        b["id"] = db.execute(
            "INSERT INTO schedule_reserves(uid, schedule_id, start_date, end_date, days, location, location_kind,"
            " location_roll, base_pct, activation_pct, activation_roll, activated, act_start, act_days, status, note,"
            " seed, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (b["uid"], sid, b["start_date"].isoformat(), b["end_date"].isoformat(), b["days"], b["location"],
             b["location_kind"], b["location_roll"], b["base_pct"], b["activation_pct"], b["activation_roll"],
             1 if b["activated"] else 0, b["act_start"].isoformat() if b["act_start"] else None, b["act_days"],
             b["status"], b["note"], b["seed"], ts, ts)).lastrowid


def update_result(db, b: dict):
    db.execute("UPDATE schedule_reserves SET status=?, note=?, updated_at=? WHERE id=?",
               (b["status"], b["note"], now_iso(), b["id"]))


def load(db, sid: int) -> list[dict]:
    out = []
    for r in rows(db.execute("SELECT * FROM schedule_reserves WHERE schedule_id=? ORDER BY start_date", (sid,))):
        r["start_date"] = date.fromisoformat(r["start_date"])
        r["end_date"] = date.fromisoformat(r["end_date"])
        r["act_start"] = date.fromisoformat(r["act_start"]) if r["act_start"] else None
        r["activated"] = bool(r["activated"])
        out.append(r)
    return out


def serialize(r: dict, apmap: dict, legs: list[dict]) -> dict:
    """Formato para a interface: datas ISO, rótulos e as pernas do acionamento."""
    ap = apmap.get(r["location"]) or {}
    act_legs = [l for l in legs if l.get("reserve_id") == r["id"]]
    act_end = (r["act_start"] + timedelta(days=(r["act_days"] or 1) - 1)) if r["act_start"] else None
    return {
        "id": r["id"], "uid": r["uid"], "start_date": r["start_date"].isoformat(), "end_date": r["end_date"].isoformat(),
        "days": r["days"], "location": r["location"], "location_iata": ap.get("iata") or r["location"],
        "location_city": ap.get("city"), "location_kind": r["location_kind"],
        "location_label": ("Base " if r["location_kind"] == "base" else "Fora da base · ") + (ap.get("iata") or r["location"]),
        "base_pct": r["base_pct"], "location_roll": r["location_roll"],
        "activation_pct": r["activation_pct"], "activation_roll": r["activation_roll"], "activated": r["activated"],
        "act_start": r["act_start"].isoformat() if r["act_start"] else None,
        "act_end": act_end.isoformat() if act_end else None, "act_days": r["act_days"],
        "status": r["status"], "status_label": STATUS_LABEL.get(r["status"], r["status"]), "note": r["note"] or "",
        "legs": [l["id"] for l in act_legs],
    }


def covering(reserves: list[dict], d: date) -> dict | None:
    for r in reserves:
        if r["start_date"] <= d <= r["end_date"]:
            return r
    return None
