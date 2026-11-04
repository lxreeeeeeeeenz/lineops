"""Voos do LineOps: reserva de pernas do AeroScale (ou voo não planejado) e máquina de estados com histórico."""
from __future__ import annotations

from .db import audit, jloads, now_iso, one, rows, tx
from .errors import Conflict, FGVError, NotFound
from .fleet import availability, get_aircraft
from .rules import types_compatible

TRANSITIONS = {
    "RESERVADO": {"EM_VOO", "CANCELADO", "APROVADO"},        # APROVADO direto só pelo "já fiz esse voo" (manual.py)
    "EM_VOO": {"PIREP_RECEBIDO", "RESERVADO", "CANCELADO"},
    "PIREP_RECEBIDO": {"APROVADO", "REJEITADO"},
    "REJEITADO": {"RESERVADO", "CANCELADO", "APROVADO"},
    "APROVADO": {"PIREP_RECEBIDO", "RESERVADO"},  # estorno explícito: do PIREP (pireps.reverse) ou do lançamento manual
    "CANCELADO": set(),
}


def transition(conn, flight_id, to_state, *, reason=None, user_id=None, allow_from=None):
    with tx(conn):
        f = one(conn.execute("SELECT id, state FROM flight WHERE id=?", (flight_id,)))
        if not f:
            raise NotFound("Voo não encontrado.")
        if to_state not in TRANSITIONS.get(f["state"], set()):
            raise Conflict(f"Transição inválida: {f['state']} → {to_state}.", code="transicao_invalida")
        ts = now_iso()
        conn.execute("UPDATE flight SET state=?, updated_at=? WHERE id=?", (to_state, ts, flight_id))
        conn.execute("INSERT INTO flight_state_history(flight_id, ts, from_state, to_state, reason, user_id) VALUES (?,?,?,?,?,?)",
                     (flight_id, ts, f["state"], to_state, reason, user_id))


def _leg(conn, leg_id):
    leg = one(conn.execute("SELECT * FROM roster_leg WHERE id=?", (leg_id,)))
    if not leg:
        raise NotFound("Perna da escala não encontrada.")
    return leg


def _check_aircraft(conn, aircraft_id, *, type_icao, dep_utc, arr_utc, ignore_flight_id=None, force_type=False):
    """Devolve (avisos). Levanta Conflict em bloqueios: indisponível, tipo incompatível, sobreposição."""
    ac = get_aircraft(conn, aircraft_id, totals=False)
    warns = []
    av = availability(conn, aircraft_id, dep_utc)
    if av["status"] == "INDISPONIVEL":
        raise Conflict("Aeronave indisponível: " + "; ".join(av["blocking"]), code="aeronave_indisponivel", details=av)
    for r in av["restrictions"]:
        warns.append(f"Restrição ({r['restriction']}): {r['title']}" + (f" — {r['text']}" if r.get("text") else ""))
    if type_icao and not types_compatible(ac["type_icao"], type_icao):
        if not force_type:
            raise Conflict(f"Tipo da aeronave ({ac['type_icao']}) incompatível com o voo ({type_icao}).", code="tipo_incompativel")
        warns.append(f"Tipo diferente do planejado ({ac['type_icao']} × {type_icao}) — confirmado por você.")
    elif type_icao and ac["type_icao"] != type_icao.upper():
        warns.append(f"Mesma família, tipo diferente do planejado ({ac['type_icao']} × {type_icao}).")
    if dep_utc and arr_utc:
        q = ("SELECT id, flight_number, dep_utc, arr_utc FROM flight WHERE aircraft_id=? AND state IN ('RESERVADO','EM_VOO','PIREP_RECEBIDO') "
             "AND dep_utc < ? AND arr_utc > ?")
        for o in conn.execute(q, (aircraft_id, arr_utc, dep_utc)):
            if o["id"] != ignore_flight_id:
                raise Conflict(f"{ac['registration']} já está reservada em {o['flight_number'] or 'outro voo'} "
                               f"({o['dep_utc']}→{o['arr_utc']}).", code="aeronave_ocupada")
    return warns


def book_leg(conn, leg_id, *, aircraft_id=None, user_id=None, force_type=False, notes=None) -> dict:
    with tx(conn):
        leg = _leg(conn, leg_id)
        if leg["removed_batch"]:
            raise Conflict("Esta perna foi removida do AeroScale na última importação.", code="perna_removida")
        if leg["activity"] in ("SOBREAVISO",):
            raise Conflict("Sobreaviso não é um voo.", code="nao_e_voo")
        if conn.execute("SELECT 1 FROM flight WHERE leg_id=? AND state<>'CANCELADO'", (leg_id,)).fetchone():
            raise Conflict("Esta perna já está reservada.", code="perna_reservada")
        if conn.execute("SELECT 1 FROM flight WHERE leg_id=?", (leg_id,)).fetchone():
            raise Conflict("Esta perna já teve uma reserva cancelada; reative-a em vez de criar outra.", code="perna_cancelada")
        aid, warns = aircraft_id, []
        if aid is None and leg["registration"]:
            r = conn.execute("SELECT id FROM aircraft WHERE registration=?", (leg["registration"],)).fetchone()
            aid = r["id"] if r else None
        if aid is not None:
            warns = _check_aircraft(conn, aid, type_icao=leg["type_icao"], dep_utc=leg["dep_utc"], arr_utc=leg["arr_utc"], force_type=force_type)
        ts = now_iso()
        fid = conn.execute("""INSERT INTO flight(leg_id, kind, airline, flight_number, adep, ades, dep_utc, arr_utc, type_icao, aircraft_id, state, pilot_id, notes, created_at, updated_at)
                              VALUES (?, 'ROSTER', ?,?,?,?,?,?,?,?, 'RESERVADO', ?,?,?,?)""",
                           (leg_id, leg["airline"], leg["flight_number"], leg["adep"], leg["ades"], leg["dep_utc"], leg["arr_utc"],
                            leg["type_icao"], aid, user_id, notes, ts, ts)).lastrowid
        conn.execute("INSERT INTO flight_state_history(flight_id, ts, from_state, to_state, reason, user_id) VALUES (?,?,?,?,?,?)",
                     (fid, ts, None, "RESERVADO", "reserva da escala", user_id))
        audit(conn, user_id, "flight.book", "flight", fid, {"leg": leg_id, "aircraft": aid})
    out = get_flight(conn, fid)
    out["warnings"] = warns
    return out


def create_unplanned(conn, *, airline, flight_number, adep, ades, type_icao, aircraft_id, dep_utc=None, arr_utc=None,
                     user_id=None, notes=None, force_type=False) -> dict:
    """Voo fora da escala do AeroScale: sempre identificado como NÃO PLANEJADO, com aeronave obrigatória."""
    adep, ades = (adep or "").strip().upper(), (ades or "").strip().upper()
    if len(adep) != 4 or len(ades) != 4 or adep == ades:
        raise FGVError("Informe origem e destino ICAO (4 letras) diferentes.", code="aeroporto_invalido")
    if not aircraft_id:
        raise FGVError("Voo não planejado exige uma aeronave da frota.", code="aeronave_obrigatoria")
    with tx(conn):
        ac = get_aircraft(conn, aircraft_id, totals=False)
        t = (type_icao or ac["type_icao"]).upper()
        warns = _check_aircraft(conn, aircraft_id, type_icao=t, dep_utc=dep_utc, arr_utc=arr_utc, force_type=force_type)
        ts = now_iso()
        fid = conn.execute("""INSERT INTO flight(leg_id, kind, airline, flight_number, adep, ades, dep_utc, arr_utc, type_icao, aircraft_id, state, pilot_id, notes, created_at, updated_at)
                              VALUES (NULL, 'UNPLANNED', ?,?,?,?,?,?,?,?, 'RESERVADO', ?,?,?,?)""",
                           ((airline or ac["airline"]).upper(), flight_number or None, adep, ades, dep_utc, arr_utc, t, aircraft_id, user_id, notes, ts, ts)).lastrowid
        conn.execute("INSERT INTO flight_state_history(flight_id, ts, from_state, to_state, reason, user_id) VALUES (?,?,?,?,?,?)",
                     (fid, ts, None, "RESERVADO", "voo não planejado", user_id))
        audit(conn, user_id, "flight.unplanned", "flight", fid, {"adep": adep, "ades": ades})
    out = get_flight(conn, fid)
    out["warnings"] = warns
    return out


def assign_aircraft(conn, flight_id, aircraft_id, *, user_id=None, force_type=False) -> dict:
    with tx(conn):
        f = one(conn.execute("SELECT * FROM flight WHERE id=?", (flight_id,)))
        if not f:
            raise NotFound("Voo não encontrado.")
        if f["state"] != "RESERVADO":
            raise Conflict("Só é possível trocar a aeronave de um voo RESERVADO.", code="estado_invalido")
        warns = _check_aircraft(conn, aircraft_id, type_icao=f["type_icao"], dep_utc=f["dep_utc"], arr_utc=f["arr_utc"],
                                ignore_flight_id=flight_id, force_type=force_type)
        conn.execute("UPDATE flight SET aircraft_id=?, updated_at=? WHERE id=?", (aircraft_id, now_iso(), flight_id))
        audit(conn, user_id, "flight.assign_aircraft", "flight", flight_id, {"aircraft": aircraft_id})
    out = get_flight(conn, flight_id)
    out["warnings"] = warns
    return out


def cancel(conn, flight_id, *, reason=None, user_id=None):
    transition(conn, flight_id, "CANCELADO", reason=reason or "cancelado pelo usuário", user_id=user_id)
    return get_flight(conn, flight_id)


def reactivate(conn, flight_id, *, user_id=None):
    """REJEITADO → RESERVADO (refazer o voo)."""
    transition(conn, flight_id, "RESERVADO", reason="reaberto para novo voo", user_id=user_id)
    return get_flight(conn, flight_id)


def get_flight(conn, flight_id) -> dict:
    f = one(conn.execute("""SELECT f.*, a.registration, a.type_icao AS aircraft_type, l.data_origin, l.route, l.cruise_fl, l.speed,
                            l.eet_min, l.activity FROM flight f LEFT JOIN aircraft a ON a.id=f.aircraft_id
                            LEFT JOIN roster_leg l ON l.id=f.leg_id WHERE f.id=?""", (flight_id,)))
    if not f:
        raise NotFound("Voo não encontrado.")
    f["history"] = rows(conn.execute("SELECT ts, from_state, to_state, reason FROM flight_state_history WHERE flight_id=? ORDER BY id", (flight_id,)))
    return f


def list_roster(conn, *, date_from=None, date_to=None, schedule_id=None, include_removed=False) -> list[dict]:
    q = ["""SELECT l.*, f.id AS flight_id, f.state AS flight_state, f.aircraft_id AS booked_aircraft_id, a.registration AS booked_registration
            FROM roster_leg l LEFT JOIN flight f ON f.leg_id=l.id LEFT JOIN aircraft a ON a.id=f.aircraft_id WHERE 1=1"""]
    args = []
    if not include_removed:
        q.append("AND l.removed_batch IS NULL")
    if date_from:
        q.append("AND l.dep_utc >= ?"); args.append(date_from)
    if date_to:
        q.append("AND l.dep_utc < ?"); args.append(date_to)
    if schedule_id:
        q.append("AND l.schedule_id = ?"); args.append(schedule_id)
    q.append("ORDER BY l.dep_utc, l.seq")
    out = rows(conn.execute(" ".join(q), args))
    for r in out:
        r["is_flight"] = r["activity"] not in ("SOBREAVISO",)
        r["can_book"] = r["is_flight"] and r["flight_id"] is None
    return out


def list_flights(conn, states=None, limit=200) -> list[dict]:
    q = ["SELECT f.*, a.registration FROM flight f LEFT JOIN aircraft a ON a.id=f.aircraft_id"]
    args = []
    if states:
        q.append(f"WHERE f.state IN ({','.join('?' * len(states))})"); args += list(states)
    q.append("ORDER BY COALESCE(f.dep_utc, f.created_at) DESC LIMIT ?"); args.append(limit)
    return rows(conn.execute(" ".join(q), args))
