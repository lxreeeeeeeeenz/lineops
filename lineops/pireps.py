"""Revisão de PIREP: aprovar gera diário de bordo + lançamento de horas/ciclos; rejeitar não altera a aeronave; estorno é explícito."""
from __future__ import annotations

import json

from . import flights, ofp as ofp_mod
from .db import audit, jloads, now_iso, one, rows, tx
from .errors import Conflict, FGVError, NotFound

OVERRIDE_KEYS = {"block_min", "air_min", "night_min", "landings", "remarks", "landing_vs_fpm", "landing_g", "hard_landing_reported"}


def get_pirep(conn, pid) -> dict:
    p = one(conn.execute("""SELECT p.*, f.flight_number, f.adep, f.ades, f.kind AS flight_kind, f.type_icao, f.dep_utc AS sched_dep,
                            a.registration, a.id AS aircraft_id FROM pirep p JOIN flight f ON f.id=p.flight_id
                            LEFT JOIN aircraft a ON a.id=f.aircraft_id WHERE p.id=?""", (pid,)))
    if not p:
        raise NotFound("PIREP não encontrado.")
    for k, d in (("data", {}), ("warnings", []), ("overrides", {})):
        p[k] = jloads(p[k], d)
    plano = ofp_mod.latest(conn, p["flight_id"])
    p["ofp"] = plano
    p["comparison"] = ofp_mod.compare(plano["data"], p["data"]) if plano else []
    return p


def list_pireps(conn, state=None) -> list[dict]:
    q = ("SELECT p.id, p.state, p.submitted_at, p.reviewed_at, f.flight_number, f.adep, f.ades, a.registration, p.warnings "
         "FROM pirep p JOIN flight f ON f.id=p.flight_id LEFT JOIN aircraft a ON a.id=f.aircraft_id")
    args = []
    if state:
        q += " WHERE p.state=?"; args.append(state)
    out = rows(conn.execute(q + " ORDER BY p.submitted_at DESC", args))
    for r in out:
        r["warnings"] = jloads(r["warnings"], [])
    return out


def approve(conn, pid, *, overrides=None, notes=None, user_id=None) -> dict:
    overrides = overrides or {}
    bad = set(overrides) - OVERRIDE_KEYS
    if bad:
        raise FGVError(f"Campos não ajustáveis: {', '.join(sorted(bad))}", code="campo_invalido")
    with tx(conn):
        p = get_pirep(conn, pid)
        if p["state"] != "PENDING":
            raise Conflict(f"PIREP já está {p['state']}.", code="estado_invalido")
        if any(w["level"] == "error" for w in p["warnings"]) and not overrides and not notes:
            raise Conflict("O PIREP tem erros: corrija com ajustes ou registre uma justificativa para aprovar.", code="erros_pendentes")
        d = dict(p["data"])
        d.update({k: v for k, v in overrides.items() if k != "remarks"})
        for k in ("block_min", "air_min", "night_min"):
            if d.get(k) is not None and (not isinstance(d[k], (int, float)) or d[k] < 0):
                raise FGVError(f"{k} inválido.", code="valor_invalido")
        if d.get("block_min") is None or d.get("air_min") is None:
            raise Conflict("Faltam horas de bloco/voo (OOOI incompleto). Informe-as nos ajustes ou rejeite o PIREP.", code="horas_ausentes")
        if d["air_min"] > d["block_min"]:
            raise Conflict("Tempo de voo maior que o de bloco.", code="horas_incoerentes")
        d["landing_vs_fpm"], d["landing_g"] = check_touchdown(d.get("landing_vs_fpm"), d.get("landing_g"))
        reported = 1 if overrides.get("hard_landing_reported") else 0
        if not p["aircraft_id"]:
            raise Conflict("Voo sem aeronave.", code="sem_aeronave")
        ts = now_iso()
        date = (d.get("out_utc") or d.get("off_utc") or p["sched_dep"] or ts)[:10]
        lid = conn.execute(
            """INSERT INTO logbook_entry(pirep_id, flight_id, date, airline, flight_number, adep, ades, aircraft_id, registration, type_icao,
               out_utc, off_utc, on_utc, in_utc, block_min, air_min, night_min, landings, fuel_out_kg, fuel_in_kg, fuel_used_kg,
               landing_vs_fpm, landing_g, source, remarks, created_at, hard_landing_reported)
               SELECT ?, f.id, ?, f.airline, f.flight_number, f.adep, f.ades, a.id, a.registration, a.type_icao, ?,?,?,?, ?,?,?,?, ?,?,?, ?,?, 'ACARS', ?, ?, ?
               FROM flight f JOIN aircraft a ON a.id=f.aircraft_id WHERE f.id=?""",
            (pid, date, d.get("out_utc"), d.get("off_utc"), d.get("on_utc"), d.get("in_utc"), d["block_min"], d["air_min"], d.get("night_min"),
             int(overrides.get("landings", 1)), d.get("fuel_out_kg"), d.get("fuel_in_kg"), d.get("fuel_used_kg"), d.get("landing_vs_fpm"),
             d.get("landing_g"), overrides.get("remarks") or notes, ts, reported, p["flight_id"])).lastrowid
        conn.execute("INSERT INTO usage_ledger(aircraft_id, pirep_id, kind, block_min, air_min, cycles, note, created_at) VALUES (?,?, 'FLIGHT', ?,?,?,?,?)",
                     (p["aircraft_id"], pid, d["block_min"], d["air_min"], int(overrides.get("landings", 1)), f"PIREP {pid}", ts))
        conn.execute("UPDATE pirep SET state='APPROVED', reviewed_at=?, reviewed_by=?, review_notes=?, overrides=? WHERE id=?",
                     (ts, user_id, notes, json.dumps(overrides, ensure_ascii=False), pid))
        flights.transition(conn, p["flight_id"], "APROVADO", reason="PIREP aprovado", user_id=user_id)
        audit(conn, user_id, "pirep.approve", "pirep", pid, {"logbook": lid, "overrides": overrides})
    return get_pirep(conn, pid)


def check_touchdown(vs, g):
    """Todo fim de voo pede o toque: razão (fpm) e G de pico. Sem eles, o voo não entra no diário."""
    if vs in (None, "") or g in (None, ""):
        raise Conflict("Informe a razão de descida (fpm) e o G do toque: todo voo termina com esse registro.", code="toque_ausente")
    try:
        vs, g = int(round(float(vs))), round(float(g), 2)
    except (TypeError, ValueError):
        raise FGVError("Razão ou G do toque inválidos.", code="valor_invalido")
    vs = -abs(vs)
    if not -3000 <= vs <= 0 or not 0.5 <= g <= 6:
        raise FGVError("Toque fora da faixa (razão de 0 a −3000 fpm, G de 0,5 a 6).", code="valor_invalido")
    return vs, g


def reject(conn, pid, *, notes, user_id=None) -> dict:
    if not notes:
        raise FGVError("Informe o motivo da rejeição.", code="motivo_obrigatorio")
    with tx(conn):
        p = get_pirep(conn, pid)
        if p["state"] != "PENDING":
            raise Conflict(f"PIREP já está {p['state']}.", code="estado_invalido")
        conn.execute("UPDATE pirep SET state='REJECTED', reviewed_at=?, reviewed_by=?, review_notes=? WHERE id=?", (now_iso(), user_id, notes, pid))
        flights.transition(conn, p["flight_id"], "REJEITADO", reason=f"PIREP rejeitado: {notes}", user_id=user_id)
        audit(conn, user_id, "pirep.reject", "pirep", pid, {"notes": notes})
    return get_pirep(conn, pid)


def reverse(conn, pid, *, reason, user_id=None) -> dict:
    """Estorna um PIREP aprovado: o lançamento no diário/horas fica marcado como estornado (nada é apagado)."""
    if not reason:
        raise FGVError("Informe o motivo do estorno.", code="motivo_obrigatorio")
    with tx(conn):
        p = get_pirep(conn, pid)
        if p["state"] != "APPROVED":
            raise Conflict("Só PIREPs aprovados podem ser estornados.", code="estado_invalido")
        ts = now_iso()
        conn.execute("UPDATE logbook_entry SET reversed_at=? WHERE pirep_id=?", (ts, pid))
        conn.execute("UPDATE usage_ledger SET reversed_at=? WHERE pirep_id=? AND reversed_at IS NULL", (ts, pid))
        conn.execute("UPDATE pirep SET state='PENDING', review_notes=?, reviewed_at=NULL WHERE id=?", (f"Estornado: {reason}", pid))
        # liberar a restrição UNIQUE(pirep_id) para uma nova aprovação
        conn.execute("UPDATE logbook_entry SET pirep_id=NULL, remarks=COALESCE(remarks||' · ','')||? WHERE reversed_at=? AND pirep_id=?",
                     (f"estornado do PIREP {pid}: {reason}", ts, pid))
        flights.transition(conn, p["flight_id"], "PIREP_RECEBIDO", reason=f"estorno: {reason}", user_id=user_id)
        audit(conn, user_id, "pirep.reverse", "pirep", pid, {"reason": reason})
    return get_pirep(conn, pid)


def logbook(conn, *, year=None, limit=500) -> list[dict]:
    q, args = "SELECT * FROM logbook_entry WHERE reversed_at IS NULL", []
    if year:
        q += " AND substr(date,1,4)=?"; args.append(str(year))
    return rows(conn.execute(q + " ORDER BY date DESC, id DESC LIMIT ?", (*args, limit)))


def logbook_totals(conn) -> dict:
    r = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(block_min),0) b, COALESCE(SUM(air_min),0) a, COALESCE(SUM(night_min),0) nt, "
                     "COALESCE(SUM(landings),0) l, AVG(ABS(landing_vs_fpm)) avs, MIN(ABS(landing_vs_fpm)) best, "
                     "SUM(CASE WHEN ABS(landing_vs_fpm) >= 600 OR landing_g >= 2.2 OR hard_landing_reported=1 THEN 1 ELSE 0 END) hard "
                     "FROM logbook_entry WHERE reversed_at IS NULL").fetchone()
    return {"flights": r["n"], "block_min": r["b"], "air_min": r["a"], "night_min": r["nt"], "landings": r["l"],
            "avg_vs_fpm": round(r["avs"]) if r["avs"] is not None else None, "best_vs_fpm": r["best"], "hard_landings": r["hard"] or 0}
