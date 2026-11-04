"""Mensagens do CCO: o que aconteceu na sua operação, na voz de um centro de controle.

Tudo vem de eventos reais já gravados (audit_log e mensagens do ACARS). Nenhuma mensagem é inventada: se nada aconteceu,
o feed diz isso.
"""
from __future__ import annotations

from .db import jloads, one, rows


def _flight(conn, fid) -> dict | None:
    if fid in (None, ""):
        return None
    return one(conn.execute("""SELECT f.id, f.flight_number, f.adep, f.ades, a.registration FROM flight f
                               LEFT JOIN aircraft a ON a.id=f.aircraft_id WHERE f.id=?""", (int(fid),)))


def _name(f) -> str:
    if not f:
        return "voo"
    return f"{f['flight_number'] or 'Voo ' + str(f['id'])} {f['adep']}→{f['ades']}"


def _flight_of(conn, entity, eid, det) -> dict | None:
    if entity == "flight":
        return _flight(conn, eid)
    if entity == "pirep":
        r = one(conn.execute("SELECT flight_id FROM pirep WHERE id=?", (int(eid),))) if eid else None
        return _flight(conn, r and r["flight_id"])
    if entity == "acars_session":
        r = one(conn.execute("SELECT flight_id FROM acars_session WHERE id=?", (int(eid),))) if eid else None
        return _flight(conn, r and r["flight_id"])
    if entity == "logbook_entry":
        r = one(conn.execute("SELECT flight_id FROM logbook_entry WHERE id=?", (int(eid),))) if eid else None
        return _flight(conn, r and r["flight_id"])
    if det.get("flight"):
        return _flight(conn, det["flight"])
    return None


def _reg(conn, aid) -> str:
    r = one(conn.execute("SELECT registration FROM aircraft WHERE id=?", (int(aid),))) if aid else None
    return r["registration"] if r else "aeronave"


def _text(conn, a: dict) -> tuple[str, str] | None:
    act, det = a["action"], jloads(a["detail"], {}) or {}
    f = _flight_of(conn, a["entity"], a["entity_id"], det)
    n = _name(f)
    if act == "flight.book":
        return "voo", f"{n} reservado" + (f" com {f['registration']}" if f and f.get("registration") else ", aeronave a definir") + "."
    if act == "flight.unplanned":
        return "voo", f"Voo fora da escala aberto: {n}."
    if act == "flight.assign_aircraft":
        return "frota", f"{_reg(conn, det.get('aircraft'))} designada para o {n}."
    if act == "flight.manual":
        b = det.get("block_min")
        return "ok", f"{n} lançado no diário como voo declarado" + (f" ({b // 60}h{b % 60:02d} de bloco)" if b else "") + "."
    if act == "flight.manual_reverse":
        return "alerta", f"Lançamento do {n} estornado: {det.get('reason') or 'sem motivo'}."
    if act == "ofp.attach":
        return "despacho", f"OFP recebido para o {n}" + (" (arquivo enviado)" if det.get("source") == "arquivo" else " (SimBrief)") + "."
    if act == "loadsheet.issue":
        return "despacho", f"LOADSHEET do {n} emitido, edição {det.get('edition', 1)}."
    if act == "acars.start":
        s = one(conn.execute("SELECT sim FROM acars_session WHERE id=?", (int(a["entity_id"]),))) if a["entity_id"] else None
        via = {"ivao": "pela IVAO", "xplane": "no X-Plane", "msfs": "no MSFS", "p3d": "no Prepar3D"}.get((s or {}).get("sim"), "")
        return "live", f"{n}: acompanhamento iniciado {via}. Bom voo.".replace("  ", " ")
    if act == "acars.abort":
        return "alerta", f"{n}: acompanhamento cancelado."
    if act in ("acars.complete", "ivao.pirep"):
        return "pireps", f"PIREP do {n} recebido. Aguardando a sua revisão."
    if act == "pirep.approve":
        return "ok", f"PIREP do {n} aprovado: diário, horas e ciclos atualizados."
    if act == "pirep.reject":
        return "alerta", f"PIREP do {n} rejeitado: {det.get('notes') or 'sem motivo'}."
    if act == "pirep.reverse":
        return "alerta", f"PIREP do {n} estornado: {det.get('reason') or 'sem motivo'}."
    if act == "import.aeroscale":
        nv, ch = det.get("legs_new") or 0, det.get("legs_changed") or 0
        return "importar", f"Escala atualizada pelo AeroScale: {nv} perna(s) nova(s), {ch} alterada(s)."
    if act == "aircraft.add":
        return "frota", f"{det.get('registration') or 'Aeronave'} entrou na frota."
    if act == "discrepancy.add":
        return "alerta", f"Discrepância aberta na {_reg(conn, det.get('aircraft'))}."
    if act == "discrepancy.close":
        return "ok", "Discrepância corrigida e fechada pela manutenção."
    if act == "aircraft.unavailability":
        return "frota", f"{_reg(conn, a['entity_id'])} indisponível de {det.get('from')} a {det.get('to')}."
    if act == "payroll.close":
        m = det.get("month") or ""
        return "salarios", f"Folha de {m[5:7]}/{m[:4]} fechada."
    if act == "network.friend":
        return "rede", f"VID {a['entity_id']}" + (f" ({det['nickname']})" if det.get("nickname") else "") + " salvo nos seus colegas da Rede."
    if act == "token.create":
        return "live", f"Acesso do ACARS liberado para “{det.get('name') or 'novo computador'}”."
    return None


def ops_feed(conn, limit: int = 14) -> list[dict]:
    out = []
    for a in rows(conn.execute("SELECT * FROM audit_log WHERE action NOT IN ('user.login','user.setup') ORDER BY id DESC LIMIT 200")):
        try:
            r = _text(conn, a)
        except Exception:
            r = None
        if r:
            out.append({"t": a["ts"], "icon": r[0], "text": r[1], "action": a["action"]})
        if len(out) >= limit:
            break
    return out


def flight_feed(conn, flight_id: int) -> list[dict]:
    """Linha do tempo de um voo: mudanças de estado e mensagens do ACARS, em ordem."""
    out = [{"t": h["ts"], "kind": "ESTADO", "text": h["reason"] or h["to_state"]}
           for h in rows(conn.execute("SELECT ts, to_state, reason FROM flight_state_history WHERE flight_id=? ORDER BY id", (flight_id,)))]
    for m in rows(conn.execute("""SELECT m.t, m.kind, m.text FROM acars_message m JOIN acars_session s ON s.id=m.session_id
                                  WHERE s.flight_id=? ORDER BY m.t, m.client_seq""", (flight_id,))):
        out.append({"t": m["t"], "kind": m["kind"], "text": m["text"]})
    return sorted(out, key=lambda x: x["t"] or "")[-40:]
