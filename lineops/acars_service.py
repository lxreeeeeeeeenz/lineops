"""Servidor do ACARS: sessões, ingestão idempotente de trilha/mensagens e criação do PIREP.
O cliente roda a máquina de fases (funciona offline e reenvia depois); o servidor valida, guarda e nunca confia cegamente."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import fleet, flights, geo, rules
from .db import audit, jloads, now_iso, one, rows, tx
from .errors import Conflict, FGVError, NotFound
from .nighttime import night_minutes

MAX_POINTS_PER_REPORT = 600
MAX_MESSAGES_PER_REPORT = 200
SNAPSHOT_KEYS = {"phase", "lat", "lon", "alt_ft", "gs_kt", "vs_fpm", "hdg", "fuel_kg", "gw_kg", "max_alt_ft", "distance_nm",
                 "out_utc", "off_utc", "on_utc", "in_utc", "landing_vs_fpm", "landing_g"}
SUMMARY_KEYS = {"out_utc", "off_utc", "on_utc", "in_utc", "fuel_out_kg", "fuel_off_kg", "fuel_on_kg", "fuel_in_kg", "gw_out_kg",
                "gw_off_kg", "gw_on_kg", "landing_vs_fpm", "landing_g", "bounces", "max_alt_ft", "distance_nm"}
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _ts(s):
    if s is None:
        return None
    try:
        return datetime.strptime(s, TS_FMT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise FGVError(f"Data/hora inválida: {s!r} (use AAAA-MM-DDThh:mm:ssZ).", code="data_invalida")


def _num(v, name, lo=None, hi=None):
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise FGVError(f"{name} inválido.", code="valor_invalido")
    if v != v or (lo is not None and v < lo) or (hi is not None and v > hi):
        raise FGVError(f"{name} fora do intervalo.", code="valor_invalido")
    return v


def eligible_flights(conn) -> list[dict]:
    """Voos que o cliente ACARS pode iniciar agora (RESERVADO com aeronave) + os já em andamento (para retomar)."""
    return rows(conn.execute("""SELECT f.id, f.flight_number, f.adep, f.ades, f.dep_utc, f.arr_utc, f.type_icao, f.state, f.kind,
                                a.registration, a.type_icao AS aircraft_type FROM flight f JOIN aircraft a ON a.id=f.aircraft_id
                                WHERE f.state IN ('RESERVADO','EM_VOO') ORDER BY COALESCE(f.dep_utc,'9999') LIMIT 100"""))


def start_session(conn, flight_id, *, sim=None, client_version=None, token_id=None, confirm_restrictions=False, user_id=None) -> dict:
    with tx(conn):
        f = one(conn.execute("SELECT * FROM flight WHERE id=?", (flight_id,)))
        if not f:
            raise NotFound("Voo não encontrado.")
        live = one(conn.execute("SELECT * FROM acars_session WHERE flight_id=? AND status='LIVE'", (flight_id,)))
        if live:                                          # retomada (ex.: cliente reiniciou)
            conn.execute("UPDATE acars_session SET last_seen_at=?, sim=COALESCE(?,sim), client_version=COALESCE(?,client_version) WHERE id=?",
                         (now_iso(), sim, client_version, live["id"]))
            return _session_out(conn, live["id"], resumed=True)
        if f["state"] != "RESERVADO":
            raise Conflict(f"O voo está {f['state']}; só voos RESERVADOS iniciam o ACARS.", code="estado_invalido")
        if not f["aircraft_id"]:
            raise Conflict("Atribua uma aeronave da frota ao voo antes de iniciar o ACARS.", code="sem_aeronave")
        av = fleet.availability(conn, f["aircraft_id"])
        if av["status"] == "INDISPONIVEL":
            raise Conflict("Aeronave indisponível: " + "; ".join(av["blocking"]), code="aeronave_indisponivel", details=av)
        needs = [r for r in av["restrictions"] if r["restriction"] == "CONFIRMA"]
        if needs and not confirm_restrictions:
            raise Conflict("Há restrições que exigem confirmação.", code="confirmacao_necessaria", details={"restrictions": needs})
        sid = conn.execute("INSERT INTO acars_session(flight_id, token_id, sim, client_version, started_at, last_seen_at, status, phase) "
                           "VALUES (?,?,?,?,?,?, 'LIVE', 'PRE')", (flight_id, token_id, sim, client_version, now_iso(), now_iso())).lastrowid
        flights.transition(conn, flight_id, "EM_VOO", reason=f"ACARS iniciado ({sim or 'sim n/d'})", user_id=user_id)
        audit(conn, user_id, "acars.start", "acars_session", sid, {"flight": flight_id})
        out = _session_out(conn, sid)
        out["restrictions"] = av["restrictions"]
        return out


def _session_out(conn, sid, resumed=False) -> dict:
    s = one(conn.execute("SELECT * FROM acars_session WHERE id=?", (sid,)))
    s["snapshot"] = jloads(s["snapshot"], {})
    s["resumed"] = resumed
    s["flight"] = flights.get_flight(conn, s["flight_id"])
    s["last_client_seq"] = (conn.execute("SELECT COALESCE(MAX(client_seq),0) m FROM acars_message WHERE session_id=?", (sid,)).fetchone()["m"])
    return s


def _live_session(conn, sid, token_user=None):
    s = one(conn.execute("SELECT * FROM acars_session WHERE id=?", (sid,)))
    if not s:
        raise NotFound("Sessão ACARS não encontrada.")
    return s


def report(conn, sid, *, points=(), messages=(), snapshot=None) -> dict:
    points, messages = list(points or []), list(messages or [])
    if len(points) > MAX_POINTS_PER_REPORT or len(messages) > MAX_MESSAGES_PER_REPORT:
        raise FGVError("Relatório grande demais; envie em lotes menores.", code="lote_grande")
    with tx(conn):
        s = _live_session(conn, sid)
        if s["status"] != "LIVE":
            raise Conflict(f"Sessão {s['status']}; não aceita novos dados.", code="sessao_encerrada")
        new_pts = 0
        for p in points:
            t = p.get("t"); _ts(t)
            lat, lon = _num(p.get("lat"), "lat", -90, 90), _num(p.get("lon"), "lon", -180, 180)
            if lat is None or lon is None:
                continue
            cur = conn.execute("INSERT OR IGNORE INTO track_point(session_id, t, lat, lon, alt_ft, gs_kt, vs_fpm, hdg, phase) VALUES (?,?,?,?,?,?,?,?,?)",
                               (sid, t, lat, lon, _int(p.get("alt_ft")), _int(p.get("gs_kt")), _int(p.get("vs_fpm")), _int(p.get("hdg")), p.get("phase")))
            new_pts += cur.rowcount
        new_msgs = 0
        for m in messages:
            seq = int(m.get("seq", -1))
            if seq < 0 or not m.get("text"):
                continue
            _ts(m.get("t"))
            cur = conn.execute("INSERT OR IGNORE INTO acars_message(session_id, client_seq, t, kind, text) VALUES (?,?,?,?,?)",
                               (sid, seq, m["t"], str(m.get("kind", "INFO"))[:16], str(m["text"])[:300]))
            new_msgs += cur.rowcount
        sets, vals = ["last_seen_at=?"], [now_iso()]
        if snapshot:
            snap = {k: v for k, v in snapshot.items() if k in SNAPSHOT_KEYS}
            old = jloads(s["snapshot"], {})
            old.update(snap)
            sets += ["snapshot=?", "phase=?"]
            vals += [json.dumps(old), snap.get("phase") or s["phase"]]
        conn.execute(f"UPDATE acars_session SET {', '.join(sets)} WHERE id=?", (*vals, sid))
    return {"ok": True, "new_points": new_pts, "new_messages": new_msgs}


def _int(v):
    return None if v is None else int(round(float(v)))


def abort(conn, sid, *, reason=None, user_id=None) -> dict:
    with tx(conn):
        s = _live_session(conn, sid)
        if s["status"] == "ABORTED":
            return _session_out(conn, sid)
        if s["status"] != "LIVE":
            raise Conflict("Sessão já concluída.", code="sessao_encerrada")
        conn.execute("UPDATE acars_session SET status='ABORTED', completed_at=? WHERE id=?", (now_iso(), sid))
        flights.transition(conn, s["flight_id"], "RESERVADO", reason=f"ACARS abortado: {reason or 'sem motivo'}", user_id=user_id)
        audit(conn, user_id, "acars.abort", "acars_session", sid, {"reason": reason})
    return _session_out(conn, sid)


def complete(conn, sid, summary: dict, *, user_id=None) -> dict:
    """Encerra a sessão e cria o PIREP (idempotente: repetir devolve o mesmo PIREP)."""
    with tx(conn):
        s = _live_session(conn, sid)
        existing = one(conn.execute("SELECT id FROM pirep WHERE session_id=?", (sid,)))
        if existing:
            return {"pirep_id": existing["id"], "duplicate": True}
        if s["status"] != "LIVE":
            raise Conflict("Sessão não está ativa.", code="sessao_encerrada")
        clean = {k: summary.get(k) for k in SUMMARY_KEYS if summary.get(k) is not None}
        for k in ("out_utc", "off_utc", "on_utc", "in_utc"):
            _ts(clean.get(k))
        for k in SUMMARY_KEYS - {"out_utc", "off_utc", "on_utc", "in_utc", "bounces"}:
            if k in clean:
                clean[k] = _num(clean[k], k)
        f = one(conn.execute("SELECT * FROM flight WHERE id=?", (s["flight_id"],)))
        pts = rows(conn.execute("SELECT t, lat, lon FROM track_point WHERE session_id=? ORDER BY t", (sid,)))
        data, warns = build_pirep_data(clean, pts, f, rules.airport_map(conn))
        pid = conn.execute("INSERT INTO pirep(session_id, flight_id, state, submitted_at, data, warnings) VALUES (?,?, 'PENDING', ?,?,?)",
                           (sid, s["flight_id"], now_iso(), json.dumps(data, ensure_ascii=False), json.dumps(warns, ensure_ascii=False))).lastrowid
        conn.execute("UPDATE acars_session SET status='COMPLETED', completed_at=? WHERE id=?", (now_iso(), sid))
        flights.transition(conn, s["flight_id"], "PIREP_RECEBIDO", reason="PIREP enviado pelo ACARS", user_id=user_id)
        audit(conn, user_id, "acars.complete", "pirep", pid, {"session": sid, "warnings": len(warns)})
    return {"pirep_id": pid, "duplicate": False, "warnings": warns}


def _min(a, b):
    if not a or not b:
        return None
    return int((_ts(b) - _ts(a)).total_seconds() // 60)


def build_pirep_data(summary: dict, track: list[dict], flight: dict, apmap: dict | None = None) -> tuple[dict, list[dict]]:
    """Calcula minutos/noite/aeroportos detectados e gera avisos. Medições vêm do simulador (rotuladas como tal)."""
    apmap = apmap if apmap is not None else {}
    w = []
    block, air = _min(summary.get("out_utc"), summary.get("in_utc")), _min(summary.get("off_utc"), summary.get("on_utc"))
    for k, label in (("out_utc", "OUT"), ("off_utc", "OFF"), ("on_utc", "ON"), ("in_utc", "IN")):
        if not summary.get(k):
            w.append({"level": "warn", "code": f"sem_{label.lower()}", "message": f"{label} não registrado (voo INCOMPLETO)."})
    seq = [summary.get(k) for k in ("out_utc", "off_utc", "on_utc", "in_utc") if summary.get(k)]
    if seq != sorted(seq):
        w.append({"level": "error", "code": "ooi_fora_de_ordem", "message": "Horários OOOI fora de ordem cronológica."})
    if block is not None and (block < 5 or block > 20 * 60):
        w.append({"level": "warn", "code": "bloco_improvavel", "message": f"Tempo de bloco improvável: {block} min."})
    if air is not None and block is not None and air > block:
        w.append({"level": "error", "code": "ar_maior_que_bloco", "message": "Tempo de voo maior que o de bloco."})
    night = None
    if summary.get("off_utc") and summary.get("on_utc"):
        night = night_minutes(track, summary["off_utc"], summary["on_utc"])
        if night is None:
            w.append({"level": "info", "code": "noite_indeterminada", "message": "Trilha insuficiente para calcular voo noturno."})
    det_dep = det_arr = None
    if track:
        det_dep = geo.nearest_airport(apmap, track[0]["lat"], track[0]["lon"])
        det_arr = geo.nearest_airport(apmap, track[-1]["lat"], track[-1]["lon"])
        if det_dep and flight.get("adep") and det_dep != flight["adep"]:
            w.append({"level": "warn", "code": "origem_diferente", "message": f"Posição inicial perto de {det_dep}, voo previa {flight['adep']}."})
        if det_arr and flight.get("ades") and det_arr != flight["ades"]:
            w.append({"level": "warn", "code": "destino_diferente", "message": f"Posição final perto de {det_arr}, voo previa {flight['ades']}."})
        if not det_dep or not det_arr:
            w.append({"level": "info", "code": "aeroporto_nao_detectado", "message": "Aeroporto de origem/destino não identificado pela trilha."})
    else:
        w.append({"level": "warn", "code": "sem_trilha", "message": "Nenhum ponto de trilha recebido."})
    vs = summary.get("landing_vs_fpm")
    if vs is not None and vs <= -600:
        w.append({"level": "info", "code": "pouso_forte", "message": f"Razão de descida no toque: {int(vs)} fpm."})
    if summary.get("bounces"):
        w.append({"level": "info", "code": "quiques", "message": f"{summary['bounces']} quique(s)/arremetida(s) após o toque."})
    fo, fi = summary.get("fuel_out_kg"), summary.get("fuel_in_kg")
    fuel_used = round(fo - fi, 1) if fo is not None and fi is not None else None
    if fuel_used is not None and fuel_used < 0:
        w.append({"level": "warn", "code": "combustivel_negativo", "message": "Combustível final maior que o inicial (reabastecimento no voo?)."})
    data = dict(summary)
    data.update({"block_min": block, "air_min": air, "night_min": night, "fuel_used_kg": fuel_used, "track_points": len(track),
                 "detected_adep": det_dep, "detected_ades": det_arr,
                 "measured_by": "simulador via LineOps ACARS (medição simulada, não oficial)"})
    return data, w


STALE_S = 120


def live_status(conn) -> list[dict]:
    from datetime import datetime as _dt
    agora = _ts(now_iso())
    out = []
    for r in conn.execute("""SELECT s.id, s.flight_id, s.sim, s.phase, s.last_seen_at, s.snapshot, f.flight_number, f.adep, f.ades, a.registration
                             FROM acars_session s JOIN flight f ON f.id=s.flight_id LEFT JOIN aircraft a ON a.id=f.aircraft_id WHERE s.status='LIVE'"""):
        d = dict(r)
        d["snapshot"] = jloads(d["snapshot"], {})
        visto = _ts(d.get("last_seen_at"))
        d["silent_s"] = int((agora - visto).total_seconds()) if visto else None
        d["stale"] = bool(d["silent_s"] is not None and d["silent_s"] > STALE_S)
        out.append(d)
    return out


def track_for(conn, sid) -> list[dict]:
    return rows(conn.execute("SELECT t, lat, lon, alt_ft, gs_kt, vs_fpm, hdg, phase FROM track_point WHERE session_id=? ORDER BY t", (sid,)))


def messages_for(conn, sid) -> list[dict]:
    return rows(conn.execute("SELECT client_seq AS seq, t, kind, text FROM acars_message WHERE session_id=? ORDER BY t, client_seq", (sid,)))
