"""“Já fiz esse voo”: o piloto declara um voo feito fora do ACARS (offline, outra rede, outro app).

Entra no diário como lançamento DECLARADO (source='MANUAL'), com os horários que você informar — nada é estimado.
A aeronave recebe horas e ciclos no livro-razão, e o estorno marca os dois lançamentos (nada é apagado).
Os salários (base realizado) passam a contar esse voo como qualquer outro do diário.

Ignorar o horário da escala (0.12), para quem voa uma perna antes (ou depois) da hora programada:
* ignore_schedule="real"   — vale o horário em que você voou de verdade; o diário guarda a partida programada ao lado.
* ignore_schedule="escala" — você pede para registrar nos horários da escala, mesmo que ainda estejam no futuro.
                             O diário marca o lançamento como "no horário da escala" e guarda quando você lançou.
Sem a opção, um voo com OUT no futuro é recusado, como sempre.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import flights
from .db import audit, now_iso, one, tx
from .errors import Conflict, FGVError, NotFound

FMT = "%Y-%m-%dT%H:%M:%SZ"
NETWORKS = ("OFFLINE", "IVAO", "VATSIM", "POSCON", "OUTRA")


def _t(v, name, required=False):
    if not v:
        if required:
            raise FGVError(f"Informe {name}.", code="campo_invalido")
        return None
    s = str(v).strip().replace(" ", "T")
    if len(s) == 16:
        s += ":00"
    if not s.endswith("Z"):
        s += "Z"
    try:
        return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        raise FGVError(f"{name}: data/hora inválida (UTC, AAAA-MM-DDThh:mm).", code="campo_invalido")


def _mins(a, b):
    return int((b - a).total_seconds() // 60) if a and b else None


def log_flight(conn, *, flight_id=None, leg_id=None, aircraft_id=None, out_utc=None, off_utc=None, on_utc=None, in_utc=None,
               landings=1, fuel_used_kg=None, landing_vs_fpm=None, landing_g=None, hard_landing_reported=False, network=None,
               remarks=None, force_type=False, ignore_schedule=None, user_id=None) -> dict:
    o, f_, n_, i = _t(out_utc, "o calço fora (OUT)", True), _t(off_utc, "a decolagem (OFF)"), _t(on_utc, "o pouso (ON)"), _t(in_utc, "o calço (IN)", True)
    seq = [x for x in (o, f_, n_, i) if x]
    if seq != sorted(seq) or o >= i:
        raise FGVError("Horários fora de ordem: OUT ≤ OFF ≤ ON ≤ IN.", code="horarios_invalidos")
    if bool(f_) != bool(n_):
        raise FGVError("Informe decolagem e pouso juntos (ou deixe os dois em branco).", code="horarios_invalidos")
    block, air = _mins(o, i), _mins(f_, n_)
    if not 5 <= block <= 20 * 60:
        raise FGVError(f"Tempo de bloco improvável ({block} min).", code="horarios_invalidos")
    mode = {None: None, False: None, "": None, True: "real", "real": "real", "escala": "escala"}.get(
        ignore_schedule if not isinstance(ignore_schedule, str) else ignore_schedule.strip().lower(), "invalido")
    if mode == "invalido":
        raise FGVError("Opção de horário inválida (use 'real' ou 'escala').", code="campo_invalido")
    if o > datetime.now(timezone.utc) and mode != "escala":
        raise FGVError("O voo ainda não aconteceu (OUT no futuro). Se você voou antes do horário da escala, marque "
                       "“Ignorar horário da escala”.", code="horarios_invalidos")
    net = (network or "OFFLINE").upper()
    if net not in NETWORKS:
        raise FGVError("Rede inválida.", code="campo_invalido")
    from .pireps import check_touchdown
    vs, g = check_touchdown(landing_vs_fpm, landing_g)         # todo voo termina com o toque informado
    try:
        landings = max(1, min(10, int(landings or 1)))
        fuel = None if fuel_used_kg in (None, "") else float(fuel_used_kg)
    except (TypeError, ValueError):
        raise FGVError("Pousos ou combustível inválidos.", code="campo_invalido")
    if fuel is not None and not 0 <= fuel <= 300000:
        raise FGVError("Combustível consumido fora da faixa.", code="campo_invalido")
    with tx(conn):
        if flight_id is None:
            if leg_id is None:
                raise FGVError("Informe o voo ou a perna da escala.", code="campo_invalido")
            r = one(conn.execute("SELECT id FROM flight WHERE leg_id=? AND state<>'CANCELADO'", (leg_id,)))
            flight_id = r["id"] if r else flights.book_leg(conn, leg_id, aircraft_id=aircraft_id, user_id=user_id, force_type=force_type)["id"]
        f = one(conn.execute("SELECT * FROM flight WHERE id=?", (flight_id,)))
        if not f:
            raise NotFound("Voo não encontrado.")
        if f["state"] not in ("RESERVADO", "REJEITADO"):
            hint = {"EM_VOO": "há um ACARS em andamento: encerre ou cancele antes",
                    "PIREP_RECEBIDO": "já existe um PIREP em revisão", "APROVADO": "o voo já está no diário",
                    "CANCELADO": "o voo está cancelado"}.get(f["state"], f["state"])
            raise Conflict(f"Não dá para declarar este voo: {hint}.", code="estado_invalido")
        if aircraft_id and aircraft_id != f["aircraft_id"]:
            if f["state"] != "RESERVADO":
                flights.transition(conn, flight_id, "RESERVADO", reason="reaberto para declarar o voo", user_id=user_id)
            flights.assign_aircraft(conn, flight_id, aircraft_id, user_id=user_id, force_type=force_type)
            f = one(conn.execute("SELECT * FROM flight WHERE id=?", (flight_id,)))
        ac = one(conn.execute("SELECT id, registration, type_icao FROM aircraft WHERE id=?", (f["aircraft_id"],))) if f["aircraft_id"] else None
        ts = now_iso()
        iso = lambda d: d.strftime(FMT) if d else None
        sched = _t(f["dep_utc"], "partida programada") if f["dep_utc"] else None
        if mode == "escala":
            if not sched:
                raise FGVError("Este voo não tem horário de escala para registrar.", code="sem_horario_escala")
            if abs((o - sched).total_seconds()) > 24 * 3600:
                raise FGVError("No horário da escala, o OUT tem que ficar a até 24 h da partida programada "
                               f"({iso(sched)[8:10]}/{iso(sched)[5:7]} {iso(sched)[11:16]}Z).", code="horarios_invalidos")
        note = f"declarado pelo piloto ({net.lower()})" + (f" · {remarks.strip()}" if remarks and remarks.strip() else "")
        if mode == "escala":
            note += f" · registrado no horário da escala, lançado em {ts[8:10]}/{ts[5:7]} {ts[11:16]}Z"
        elif mode == "real" and sched and abs((o - sched).total_seconds()) > 3 * 3600:
            note += f" · voado fora do horário (escala {iso(sched)[8:10]}/{iso(sched)[5:7]} {iso(sched)[11:16]}Z)"
        lid = conn.execute(
            """INSERT INTO logbook_entry(flight_id, date, airline, flight_number, adep, ades, aircraft_id, registration, type_icao,
               out_utc, off_utc, on_utc, in_utc, block_min, air_min, night_min, landings, fuel_used_kg, landing_vs_fpm, landing_g, source, remarks,
               network, created_at, hard_landing_reported, time_basis, sched_out_utc)
               VALUES (?,?,?,?,?,?,?,?,?, ?,?,?,?, ?,?,NULL,?,?,?,?, 'MANUAL', ?, ?, ?, ?, ?, ?)""",
            (flight_id, iso(o)[:10], f["airline"], f["flight_number"], f["adep"], f["ades"], ac and ac["id"], ac and ac["registration"],
             (ac and ac["type_icao"]) or f["type_icao"], iso(o), iso(f_), iso(n_), iso(i), block, air, landings, fuel, vs, g, note[:300], net, ts,
             1 if hard_landing_reported else 0, "ESCALA" if mode == "escala" else "REAL", iso(sched) if mode else None)).lastrowid
        if ac:
            conn.execute("""INSERT INTO usage_ledger(aircraft_id, pirep_id, logbook_id, kind, block_min, air_min, cycles, note, created_at)
                            VALUES (?, NULL, ?, 'FLIGHT', ?, ?, ?, ?, ?)""",
                         (ac["id"], lid, block, air or 0, landings, f"Voo declarado (diário #{lid})" + ("" if air else " · sem tempo de voo informado"), ts))
        flights.transition(conn, flight_id, "APROVADO", reason=f"Já fiz esse voo — {note}", user_id=user_id)
        audit(conn, user_id, "flight.manual", "flight", flight_id, {"logbook": lid, "block_min": block, "network": net, "time_basis": mode})
    out = flights.get_flight(conn, flight_id)
    out["logbook_id"] = lid
    out["warnings"] = ([] if ac else ["Voo sem aeronave: entrou no diário, mas nenhuma matrícula recebeu horas e ciclos."]) + \
                      ([] if air is not None else ["Sem decolagem e pouso: o tempo de voo ficou em branco e a aeronave recebeu só o bloco e o ciclo."])
    return out


def reverse(conn, logbook_id: int, *, reason: str, user_id=None) -> dict:
    if not (reason or "").strip():
        raise FGVError("Informe o motivo do estorno.", code="motivo_obrigatorio")
    with tx(conn):
        e = one(conn.execute("SELECT * FROM logbook_entry WHERE id=?", (logbook_id,)))
        if not e or e["source"] != "MANUAL":
            raise NotFound("Lançamento manual não encontrado (PIREPs são estornados na tela do PIREP).")
        if e["reversed_at"]:
            raise Conflict("Este lançamento já foi estornado.", code="ja_estornado")
        ts = now_iso()
        conn.execute("UPDATE logbook_entry SET reversed_at=?, remarks=COALESCE(remarks||' · ','')||? WHERE id=?",
                     (ts, f"estornado: {reason.strip()}", logbook_id))
        conn.execute("UPDATE usage_ledger SET reversed_at=? WHERE logbook_id=? AND reversed_at IS NULL", (ts, logbook_id))
        f = one(conn.execute("SELECT state FROM flight WHERE id=?", (e["flight_id"],))) if e["flight_id"] else None
        if f and f["state"] == "APROVADO":
            flights.transition(conn, e["flight_id"], "RESERVADO", reason=f"estorno do voo declarado: {reason.strip()}", user_id=user_id)
        audit(conn, user_id, "flight.manual_reverse", "logbook_entry", logbook_id, {"reason": reason})
    return {"ok": True, "flight_id": e["flight_id"]}
