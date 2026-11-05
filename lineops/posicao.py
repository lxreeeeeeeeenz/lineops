"""Onde o seu avião está, e recomeçar a escala dali (apagar daqui pra frente, trocar de base).

* POSIÇÃO: o fim do último voo da escala de hoje (hora local da base). Sem voo hoje, o último voo antes de agora; sem nenhum,
  a base da escala. Ao apagar, a posição fica MEMORIZADA: é dali que a escala nova começa.
* APAGAR DAQUI PRA FRENTE: as pernas depois desse voo saem da escala, com histórico (como uma alteração de escala), as
  reservas de voo delas são canceladas, pedidos de troca abertos ficam sem efeito, e folgas e blocos de reserva depois do
  dia saem do calendário. Nada do que já foi voado muda. Dá para desfazer enquanto nenhuma escala nova entrou por cima.
* ESCALA NOVA: o motor do AeroScale gera a partir da posição memorizada (o planejador dele aceita começar fora da base,
  com o histórico de jornada e repouso dos dias anteriores), na base que você escolher. Ver motor.generate(from_position).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import rules
from .db import audit, jloads, now_iso, one, rows, tx
from .errors import Conflict, NotFound

FMT = "%Y-%m-%dT%H:%M:%SZ"
KEY = "posicao_memorizada"
FLOWN = ("EM_VOO", "PIREP_RECEBIDO", "APROVADO", "REJEITADO")


def _p(s):
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc) if s else None


def _local(conn, utc_iso, icao) -> str | None:
    d = rules.local_dt(conn, _p(utc_iso), icao)
    return f"{d:%d/%m %H:%M}" if d else None


def _place(conn, icao) -> dict:
    a = rules.airport(conn, icao) or {}
    return {"icao": icao, "city": a.get("city") or a.get("name"), "name": a.get("name")}


def memorized(conn) -> dict | None:
    """A posição guardada ao apagar a escala, enquanto ainda vale (nenhuma perna entrou depois dela)."""
    r = conn.execute("SELECT value FROM setting WHERE key=?", (KEY,)).fetchone()
    m = jloads(r[0], None) if r else None
    if m and conn.execute("SELECT 1 FROM roster_leg WHERE removed_batch IS NULL AND activity<>'SOBREAVISO' AND dep_utc>? LIMIT 1",
                          (m["at_utc"],)).fetchone():
        return None
    return m


def _legs_alive(conn, with_standby: bool = False):
    """Pernas vivas. Para saber onde o avião está, só voos; para apagar, também as do sobreaviso acionado
    (with_standby): senão ficavam voos soltos depois do corte."""
    from . import escala
    return escala.legs(conn, flights_only=not with_standby)


def current(conn, now: str | None = None) -> dict:
    """Onde o avião estará ao fim da escala de hoje (ou onde parou)."""
    from .journey import local_today, schedule_params
    now = now or now_iso()
    today = local_today(conn, now)
    legs = _legs_alive(conn)
    todays = [l for l in legs if (l["local_date"] or l["dep_utc"][:10]) == today]
    if todays:
        l, src = todays[-1], "hoje"
    else:
        past = [l for l in legs if l["dep_utc"] <= now]
        l, src = (past[-1], "ultimo") if past else (None, None)
    if l:
        return {"icao": l["ades"], "place": _place(conn, l["ades"]), "at_utc": l["arr_utc"], "at_local": _local(conn, l["arr_utc"], l["ades"]),
                "leg_id": l["id"], "flight_number": l["flight_number"], "date": l["local_date"] or l["dep_utc"][:10], "source": src}
    p, _s = schedule_params(conn, today)
    base = p.get("base_resolved") or p.get("base")
    return {"icao": base, "place": _place(conn, base) if base else None, "at_utc": now, "at_local": None, "leg_id": None,
            "flight_number": None, "date": today, "source": "base" if base else None}


def preview(conn, now: str | None = None) -> dict:
    """O que sai se você apagar daqui pra frente."""
    now = now or now_iso()
    pos = current(conn, now)
    mem = memorized(conn)
    after = [l for l in _legs_alive(conn, True) if pos["at_utc"] and l["dep_utc"] > pos["at_utc"]] if pos["icao"] else []
    flying = [l for l in after if l["flight_state"] in FLOWN]
    res = rows(conn.execute("SELECT * FROM roster_reserve WHERE end_date>?", (pos["date"],))) if pos.get("date") else []
    swaps = conn.execute("SELECT COUNT(*) FROM swap_request WHERE state='PENDENTE'").fetchone()[0] if after else 0
    return {"position": pos, "memorized": mem,
            "remove": {"legs": len(after), "from": (after[0]["local_date"] or after[0]["dep_utc"][:10]) if after else None,
                       "to": (after[-1]["local_date"] or after[-1]["dep_utc"][:10]) if after else None,
                       "booked": sum(1 for l in after if l["flight_state"] == "RESERVADO"), "reserves": len(res), "swaps": swaps,
                       "first": {"flight_number": after[0]["flight_number"], "adep": after[0]["adep"], "ades": after[0]["ades"],
                                 "dep_local": _local(conn, after[0]["dep_utc"], after[0]["adep"])} if after else None},
            "blocked": (f"O {flying[0]['flight_number'] or 'voo'} depois desse ponto já está em voo ou voado." if flying else
                        None if pos["icao"] else "Sem escala e sem base: não há de onde recomeçar."),
            "can_undo": can_undo(conn)}


def cut(conn, now: str | None = None, user_id=None) -> dict:
    """Apaga a escala depois do último voo de hoje e memoriza a posição."""
    now = now or now_iso()
    pv = preview(conn, now)
    if pv["blocked"]:
        raise Conflict(pv["blocked"], code="corte_bloqueado")
    pos = pv["position"]
    after = [l for l in _legs_alive(conn, True) if l["dep_utc"] > pos["at_utc"]]
    city = f" ({pos['place']['city']})" if pos.get("place") and pos["place"].get("city") else ""
    return _apply_cut(conn, pos, after, pos["date"], now, user_id,
                      title=f"Escala encerrada depois do {pos['flight_number'] or 'último voo'}",
                      body_tail=f"O seu avião fica em {pos['icao']}{city}"
                      + (f", depois do {pos['flight_number']} ({pos['at_local']} local)" if pos.get("flight_number") else "")
                      + ". A próxima escala começa dali, na base que você escolher em Gerar escala.")


def position_before(conn, day: str) -> dict | None:
    """Onde o avião estava ao fim do último voo antes do dia (para gerar a partir de um dia escolhido)."""
    legs = [l for l in _legs_alive(conn) if (l["local_date"] or l["dep_utc"][:10]) < day]
    if not legs:
        return None
    l = legs[-1]
    return {"icao": l["ades"], "place": _place(conn, l["ades"]), "at_utc": l["arr_utc"], "at_local": _local(conn, l["arr_utc"], l["ades"]),
            "leg_id": l["id"], "flight_number": l["flight_number"], "date": l["local_date"] or l["dep_utc"][:10], "source": "antes_do_dia"}


def legs_from(conn, day: str, until: str | None = None) -> list[dict]:
    return [l for l in _legs_alive(conn, True) if day <= (l["local_date"] or l["dep_utc"][:10]) <= (until or "9999-12-31")]


def cut_from(conn, day: str, now: str | None = None, user_id=None, until: str | None = None) -> dict:
    """Apaga a escala de um dia (inclusive) até `until` (inclusive; sem ele, até o fim): para gerar a escala nova ali."""
    now = now or now_iso()
    after = legs_from(conn, day, until)
    flying = [l for l in after if l["flight_state"] in FLOWN]
    if flying:
        raise Conflict(f"O {flying[0]['flight_number'] or 'voo'} de {flying[0]['local_date']} já está em voo ou voado: "
                       "escolha um dia depois dele.", code="corte_bloqueado")
    keep_until = (datetime.fromisoformat(day) - timedelta(days=1)).date().isoformat()
    pos = position_before(conn, day)
    return _apply_cut(conn, pos, after, keep_until, now, user_id, memorize=False, until=until,
                      title=f"Escala refeita a partir de {day[8:10]}/{day[5:7]}" + (f" até {until[8:10]}/{until[5:7]}" if until else ""),
                      body_tail="A escala nova desse período vem do motor, em seguida.")


def _apply_cut(conn, pos: dict | None, after: list[dict], keep_until: str, now: str, user_id, *, memorize=True,
               title: str, body_tail: str, until: str | None = None) -> dict:
    from . import flights
    with tx(conn):
        bid = conn.execute("INSERT INTO import_batch(source_kind, source_name, imported_at, stats) VALUES ('corte', ?, ?, '{}')",
                           (title, now)).lastrowid
        cancelled = 0
        for l in after:
            conn.execute("UPDATE roster_leg SET removed_batch=? WHERE id=?", (bid, l["id"]))
            conn.execute("INSERT INTO roster_leg_change(leg_id, batch_id, kind, old_values) VALUES (?,?, 'REMOVED', ?)",
                         (l["id"], bid, json.dumps({"corte": True})))
            if l["flight_state"] == "RESERVADO":
                flights.transition(conn, l["flight_id"], "CANCELADO", reason="escala apagada daqui pra frente")
                cancelled += 1
        conn.execute("UPDATE swap_request SET state='EXPIRADO', decided_at=?, answer=? WHERE state='PENDENTE'",
                     (now, "A escala foi apagada daqui pra frente."))
        hi = until or "9999-12-31"
        day1 = (datetime.fromisoformat(keep_until) + timedelta(days=1)).date().isoformat()
        after_hi = (datetime.fromisoformat(hi) + timedelta(days=1)).date().isoformat() if until else None
        saved_res = rows(conn.execute("SELECT * FROM roster_reserve WHERE end_date>? AND start_date<=?", (keep_until, hi)))
        for r in saved_res:
            if r["start_date"] > keep_until and r["end_date"] <= hi:
                conn.execute("DELETE FROM roster_reserve WHERE id=?", (r["id"],))
            elif r["start_date"] > keep_until:
                conn.execute("UPDATE roster_reserve SET start_date=? WHERE id=?", (after_hi, r["id"]))
            else:
                conn.execute("UPDATE roster_reserve SET end_date=? WHERE id=?", (keep_until, r["id"]))
        saved_sched = []
        for s in rows(conn.execute("SELECT * FROM roster_schedule WHERE end_date>? AND start_date<=?", (keep_until, hi))):
            prm = jloads(s["params"], {})
            saved_sched.append({"id": s["id"], "end_date": s["end_date"], "planned_days_off": prm.get("planned_days_off")})
            prm["planned_days_off"] = [d for d in (prm.get("planned_days_off") or []) if not (day1 <= d <= hi)]
            prm["local_edit"] = now                             # reimportar a mesma escala do motor não desfaz o corte
            end = s["end_date"]
            if end <= hi:                                       # a escala acaba dentro do trecho apagado: encerra antes dele
                prm["cut_after"] = keep_until
                end = max(s["start_date"], keep_until)
            conn.execute("UPDATE roster_schedule SET params=?, end_date=? WHERE id=?", (json.dumps(prm, ensure_ascii=False), end, s["id"]))
        mem = None
        if pos and memorize:
            mem = {**{k: pos[k] for k in ("icao", "at_utc", "at_local", "leg_id", "flight_number", "date")}, "batch": bid, "cut_at": now}
            conn.execute("INSERT INTO setting(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (KEY, json.dumps(mem, ensure_ascii=False)))
        conn.execute("UPDATE import_batch SET stats=? WHERE id=?",
                     (json.dumps({"legs_removed": len(after), "reserves": saved_res, "schedules": saved_sched, "position": mem,
                                  "keep_until": keep_until}, ensure_ascii=False, default=str), bid))
        audit(conn, user_id, "escala.corte", "import_batch", bid, {"legs": len(after), "at": (pos or {}).get("icao"), "keep_until": keep_until})
        from .equipe import post
        post(conn, f"corte:{bid}", "escala", title,
             f"Comandante, apaguei a sua escala daqui pra frente: {len(after)} etapa(s) saíram"
             + (f" ({cancelled} reserva(s) de voo cancelada(s))" if cancelled else "") + ". " + body_tail,
             level="acao", links=[{"label": "Gerar escala", "href": "#/gerar"}], now=now)
    return {"removed": len(after), "cancelled": cancelled, "position": mem or pos, "batch": bid}


def can_undo(conn) -> bool:
    mem = memorized(conn)
    if not mem or not mem.get("batch"):
        return False
    b = mem["batch"]
    newer = conn.execute("SELECT 1 FROM roster_leg WHERE first_batch>? AND removed_batch IS NULL AND dep_utc>? LIMIT 1",
                         (b, mem["at_utc"])).fetchone()
    return not newer and bool(conn.execute("SELECT 1 FROM import_batch WHERE id=? AND source_kind='corte'", (b,)).fetchone())


def undo(conn, user_id=None, now: str | None = None) -> dict:
    """Volta a escala apagada (se nenhuma escala nova entrou por cima). Reservas de voo canceladas continuam canceladas."""
    mem = memorized(conn)
    if not mem or not can_undo(conn):
        raise Conflict("Não dá para desfazer: já entrou escala nova depois do corte (ou não houve corte).", code="corte_sem_desfazer")
    b = mem["batch"]
    st = jloads(one(conn.execute("SELECT stats FROM import_batch WHERE id=?", (b,)))["stats"], {})
    with tx(conn):
        ids = [r[0] for r in conn.execute("SELECT id FROM roster_leg WHERE removed_batch=?", (b,))]
        for i in ids:
            conn.execute("UPDATE roster_leg SET removed_batch=NULL WHERE id=?", (i,))
            conn.execute("INSERT INTO roster_leg_change(leg_id, batch_id, kind, old_values) VALUES (?,?, 'RESTORED', '{}')", (i, b))
        for r in st.get("reserves") or []:
            if conn.execute("SELECT 1 FROM roster_reserve WHERE id=?", (r["id"],)).fetchone():
                conn.execute("UPDATE roster_reserve SET end_date=? WHERE id=?", (r["end_date"], r["id"]))
            else:
                cols = list(r)
                conn.execute(f"INSERT INTO roster_reserve({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [r[c] for c in cols])
        for s in st.get("schedules") or []:
            cur = one(conn.execute("SELECT params FROM roster_schedule WHERE id=?", (s["id"],)))
            if not cur:
                continue
            prm = jloads(cur["params"], {})
            prm.pop("cut_after", None)
            prm.pop("local_edit", None)
            if s.get("planned_days_off") is not None:
                prm["planned_days_off"] = s["planned_days_off"]
            conn.execute("UPDATE roster_schedule SET params=?, end_date=? WHERE id=?", (json.dumps(prm, ensure_ascii=False), s["end_date"], s["id"]))
        conn.execute("UPDATE import_batch SET source_kind='corte-desfeito' WHERE id=?", (b,))
        conn.execute("DELETE FROM setting WHERE key=?", (KEY,))
        audit(conn, user_id, "escala.corte_desfeito", "import_batch", b, {"legs": len(ids)})
    return {"restored": len(ids)}


def history(conn, pos: dict, days: int = 10) -> list[dict]:
    """Pernas dos dias antes da posição: o motor usa para respeitar repouso, jornada e folgas na escala nova."""
    since = (_p(pos["at_utc"]) - timedelta(days=days)).strftime(FMT)
    return [dict(r) for r in conn.execute(
        """SELECT flight_number, adep, ades, dep_utc, arr_utc FROM roster_leg WHERE activity<>'SOBREAVISO'
           AND removed_batch IS NULL AND dep_utc>=? AND arr_utc<=? ORDER BY dep_utc""", (since, pos["at_utc"]))]


# ------------------------------------------------------------------ apagar todas as escalas
def wipe_preview(conn) -> dict:
    alive = _legs_alive(conn, True)
    return {"legs": len(alive), "flown": sum(1 for l in alive if l["flight_state"] in FLOWN),
            "booked": sum(1 for l in alive if l["flight_state"] == "RESERVADO"),
            "schedules": conn.execute("SELECT COUNT(*) FROM roster_schedule").fetchone()[0],
            "reserves": conn.execute("SELECT COUNT(*) FROM roster_reserve").fetchone()[0],
            "logbook": conn.execute("SELECT COUNT(*) FROM logbook_entry WHERE reversed_at IS NULL").fetchone()[0]}


def wipe_all(conn, now: str | None = None, user_id=None) -> dict:
    """Apaga todas as escalas da carreira (pernas, folgas, reservas, sobreavisos). O diário de bordo, os voos já feitos,
    os salários fechados e as mensagens ficam: só a escala some. Reservas de voo ainda não voadas são canceladas."""
    from . import flights
    now = now or now_iso()
    alive = _legs_alive(conn, True)
    flying = [l for l in alive if l["flight_state"] == "EM_VOO"]
    if flying:
        raise Conflict(f"O {flying[0]['flight_number'] or 'voo'} está em voo agora: termine (ou encerre) o acompanhamento antes.",
                       code="em_voo")
    with tx(conn):
        bid = conn.execute("INSERT INTO import_batch(source_kind, source_name, imported_at, stats) VALUES ('limpeza', ?, ?, '{}')",
                           ("Todas as escalas apagadas", now)).lastrowid
        cancelled = 0
        for l in alive:
            conn.execute("UPDATE roster_leg SET removed_batch=? WHERE id=?", (bid, l["id"]))
            conn.execute("INSERT INTO roster_leg_change(leg_id, batch_id, kind, old_values) VALUES (?,?, 'REMOVED', ?)",
                         (l["id"], bid, json.dumps({"limpeza": True})))
            if l["flight_state"] == "RESERVADO":
                flights.transition(conn, l["flight_id"], "CANCELADO", reason="todas as escalas apagadas")
                cancelled += 1
        conn.execute("UPDATE swap_request SET state='EXPIRADO', decided_at=?, answer=? WHERE state='PENDENTE'",
                     (now, "Todas as escalas foram apagadas."))
        nres = conn.execute("DELETE FROM roster_reserve").rowcount
        conn.execute("DELETE FROM roster_day")
        for s in rows(conn.execute("SELECT id, params FROM roster_schedule")):
            prm = jloads(s["params"], {})
            prm["planned_days_off"] = []
            prm["apagada"] = now
            conn.execute("UPDATE roster_schedule SET params=? WHERE id=?", (json.dumps(prm, ensure_ascii=False), s["id"]))
        conn.execute("DELETE FROM setting WHERE key=?", (KEY,))
        conn.execute("UPDATE import_batch SET stats=? WHERE id=?",
                     (json.dumps({"legs_removed": len(alive), "cancelled": cancelled, "reserves": nres}), bid))
        audit(conn, user_id, "escala.limpeza", "import_batch", bid, {"legs": len(alive)})
        from .equipe import post
        post(conn, f"limpeza:{bid}", "escala", "Todas as escalas apagadas",
             f"Comandante, apaguei todas as suas escalas: {len(alive)} etapa(s)"
             + (f", {cancelled} reserva(s) de voo cancelada(s)" if cancelled else "") + ". O diário de bordo e os voos já feitos "
             "continuam. Quando quiser, gere a escala nova.", level="acao", links=[{"label": "Gerar escala", "href": "#/gerar"}], now=now)
    return {"removed": len(alive), "cancelled": cancelled, "reserves": nres, "batch": bid}
