"""Carreira: pontualidade, marcos, cheque de rota e o boletim semanal da chefia.

Tudo sai do seu diário e da escala; o que é personagem (o checador, o coordenador do CCO) é fictício.

* Pontualidade (OTP15, a definição usada pela indústria): partida no horário se o calço fora (OUT) sair até 15 min depois
  do programado; chegada no horário se o calço (IN) for até 15 min depois do programado. Acima disso, o CCO pede o
  código de atraso. Os códigos são os da IATA (AHM 730), numa lista resumida com os mais usados.
  Voo lançado com "ignorar horário" (ou a mais de 6 h do programado) não entra na conta.
* Marcos: primeiro pouso em cada aeroporto, primeira vez em cada matrícula, horas de bloco e pousos acumulados,
  sequência de toques suaves, primeiro voo noturno.
* Cheque de rota: o RBAC 121.440 (que segue o 14 CFR 121.440) pede um cheque em rota do comandante a cada 12 meses.
  Sem cheque nos últimos 12 meses, a Chefia de Equipamento escala um checador (fictício) num dos seus próximos voos.
  A avaliação usa os dados reais do voo: despacho assinado, pontualidade, toque e combustível × OFP. O checador precisa
  VER o voo: voo declarado no "Já fiz esse voo" não vale como cheque (remarca).
  Os critérios de aprovação são do LineOps (não há um padrão público da GOL) e aparecem na mensagem.
* Boletim semanal: segunda-feira de manhã (hora da base), a chefia resume a semana com os seus números.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

from . import equipe, landing, rules
from .db import jloads, now_iso, one, rows

FMT = "%Y-%m-%dT%H:%M:%SZ"
OTP_MIN = 15
OFF_SCHEDULE_H = 6
DELAY_CODES = {
    "11": "Check-in depois do horário limite",
    "15": "Embarque (passageiro faltando, embarque lento)",
    "18": "Bagagem (processamento)",
    "31": "Documentação da aeronave atrasada",
    "32": "Carregamento e descarregamento",
    "35": "Limpeza da aeronave",
    "36": "Abastecimento",
    "37": "Catering",
    "41": "Pane técnica na aeronave",
    "46": "Troca de aeronave por motivo técnico",
    "57": "Plano de voo",
    "61": "Plano de voo / despacho concluído tarde",
    "62": "Exigência operacional (combustível, peso e balanceamento)",
    "63": "Apresentação ou procedimentos de partida da tripulação atrasados",
    "71": "Tempo no aeroporto de partida",
    "72": "Tempo no aeroporto de destino",
    "81": "Controle de fluxo (ATFM) em rota",
    "83": "Restrição de fluxo (ATFM) no destino",
    "87": "Instalações do aeroporto (posição, portão)",
    "89": "Restrição no aeroporto de partida (incluindo ATC, push-back)",
    "93": "Rotação: chegada atrasada da aeronave do voo anterior",
    "95": "Rotação da tripulação",
    "99": "Outros (ex.: problema no simulador)",
}
QUICK_CODES = ("93", "63", "89", "81", "15", "41", "99")
CHECKERS = ("Cmte. Augusto Siqueira", "Cmte. Helena Marques", "Cmte. Otávio Brandão", "Cmte. Silvia Coutinho")
HOURS = (1, 10, 25, 50, 100, 250, 500, 750, 1000, 1500, 2000, 3000, 5000)
LANDINGS = (1, 10, 25, 50, 100, 250, 500, 1000)
STREAKS = (3, 5, 10, 20)


def _p(s):
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc) if s else None


def _mins(a, b):
    return int(round((_p(b) - _p(a)).total_seconds() / 60)) if a and b else None


# ------------------------------------------------------------------ pontualidade
def record_delay(conn, f, e, now) -> dict | None:
    """Calcula o atraso de partida e de chegada do voo do diário. Devolve o registro (ou None se não se aplica)."""
    basis = e.get("time_basis") or "REAL"
    if basis != "REAL" or not f.get("dep_utc") or not e.get("out_utc"):
        return None
    out_d = _mins(f["dep_utc"], e["out_utc"])
    if out_d is None or abs(out_d) > OFF_SCHEDULE_H * 60:
        return None                                       # voado fora do horário: não é atraso, é outro dia
    in_d = _mins(f["arr_utc"], e["in_utc"]) if f.get("arr_utc") and e.get("in_utc") else None
    conn.execute("""INSERT INTO flight_delay(flight_id, logbook_id, out_delay_min, in_delay_min) VALUES (?,?,?,?)
                    ON CONFLICT(flight_id) DO UPDATE SET logbook_id=excluded.logbook_id, out_delay_min=excluded.out_delay_min,
                    in_delay_min=excluded.in_delay_min""", (f["id"], e["id"], out_d, in_d))
    d = one(conn.execute("SELECT * FROM flight_delay WHERE flight_id=?", (f["id"],)))
    if out_d > OTP_MIN and not d["code"] and not d["asked_at"]:
        conn.execute("UPDATE flight_delay SET asked_at=? WHERE flight_id=?", (now, f["id"]))
        equipe.post(conn, f"delay_ask:{f['id']}", "cco", f"{f['flight_number'] or 'Voo'}: atraso de {out_d} min, preciso do código",
                    f"Comandante, o {f['flight_number'] or 'voo'} {f['adep']}→{f['ades']} saiu do calço às {e['out_utc'][11:16]}Z, "
                    f"{out_d} min depois do programado ({f['dep_utc'][11:16]}Z). Acima de 15 min conta como atraso (OTP15).\n\n"
                    "Me passa o código de atraso (IATA) para eu fechar o relatório do dia: no Flight Center, ou pelos botões aqui."
                    + (f"\n\nNa chegada: {in_d:+d} min em relação ao programado." if in_d is not None else ""),
                    flight_id=f["id"], level="acao", now=now, links=[{"label": "Informar o código", "href": f"#/voo/{f['id']}"}])
    return d


def set_delay_code(conn, flight_id: int, code: str, remark: str | None = None, user_id=None) -> dict:
    from .errors import Conflict, FGVError
    from .db import audit, tx
    code = str(code or "").strip()
    if code not in DELAY_CODES:
        raise FGVError("Código de atraso desconhecido.", code="campo_invalido")
    with tx(conn):
        d = one(conn.execute("SELECT * FROM flight_delay WHERE flight_id=?", (flight_id,)))
        if not d:
            raise Conflict("Este voo não tem atraso registrado.", code="sem_atraso")
        ts = now_iso()
        conn.execute("UPDATE flight_delay SET code=?, remark=?, answered_at=? WHERE flight_id=?",
                     (code, (remark or "").strip()[:200] or None, ts, flight_id))
        f = one(conn.execute("SELECT flight_number FROM flight WHERE id=?", (flight_id,)))
        equipe.post(conn, f"delay_ok:{flight_id}:{code}", "cco", f"{(f or {}).get('flight_number') or 'Voo'}: código {code} registrado",
                    f"Anotado: código {code}, {DELAY_CODES[code].lower()}" + (f" ({remark.strip()})" if remark and remark.strip() else "")
                    + ". Obrigado, comandante.", flight_id=flight_id, now=ts)
        conn.execute("UPDATE ops_message SET acked_at=COALESCE(acked_at, ?), read_at=COALESCE(read_at, ?) WHERE key=?",
                     (ts, ts, f"delay_ask:{flight_id}"))
        audit(conn, user_id, "flight.delay_code", "flight", flight_id, {"code": code})
    return one(conn.execute("SELECT * FROM flight_delay WHERE flight_id=?", (flight_id,)))


def delay_of(conn, flight_id) -> dict | None:
    d = one(conn.execute("SELECT * FROM flight_delay WHERE flight_id=?", (flight_id,)))
    if d:
        d["code_label"] = DELAY_CODES.get(d["code"]) if d["code"] else None
        d["late"] = (d["out_delay_min"] or 0) > OTP_MIN
    return d


def otp(conn, since: str | None = None, until: str | None = None) -> dict:
    q = """SELECT d.*, l.date FROM flight_delay d JOIN logbook_entry l ON l.id=d.logbook_id WHERE l.reversed_at IS NULL"""
    a = []
    if since:
        q += " AND l.date>=?"; a.append(since)
    if until:
        q += " AND l.date<=?"; a.append(until)
    rs = rows(conn.execute(q, a))
    dep = [r for r in rs if r["out_delay_min"] is not None]
    arr = [r for r in rs if r["in_delay_min"] is not None]
    codes = {}
    for r in rs:
        if r["code"]:
            codes[r["code"]] = codes.get(r["code"], 0) + 1
    pct = lambda xs, k: round(100 * sum(1 for r in xs if r[k] <= OTP_MIN) / len(xs)) if xs else None
    return {"flights": len(rs), "dep_otp": pct(dep, "out_delay_min"), "arr_otp": pct(arr, "in_delay_min"),
            "late": sum(1 for r in dep if r["out_delay_min"] > OTP_MIN), "pending_codes": sum(1 for r in dep if r["out_delay_min"] > OTP_MIN and not r["code"]),
            "codes": [{"code": k, "label": DELAY_CODES.get(k), "n": v} for k, v in sorted(codes.items(), key=lambda x: -x[1])],
            "rule": "OTP15: no horário até 15 min depois do programado (calço fora na partida, calço na chegada)"}


# ------------------------------------------------------------------ marcos
def _mark(conn, key, kind, title, detail, e, now) -> bool:
    cur = conn.execute("INSERT OR IGNORE INTO milestone(key, kind, title, detail, logbook_id, achieved_at) VALUES (?,?,?,?,?,?)",
                       (key, kind, title, detail, e["id"], now))
    return cur.rowcount > 0


def milestones(conn, f, e, now) -> list[dict]:
    """Marcos novos alcançados com este lançamento do diário."""
    new = []
    prev = rows(conn.execute("SELECT * FROM logbook_entry WHERE reversed_at IS NULL AND id<>? AND id<?", (e["id"], e["id"])))
    tot = conn.execute("SELECT COALESCE(SUM(block_min),0) b, COALESCE(SUM(landings),0) l FROM logbook_entry WHERE reversed_at IS NULL AND id<=?",
                       (e["id"],)).fetchone()
    pb = tot["b"] - (e["block_min"] or 0)
    pl = tot["l"] - (e["landings"] or 0)
    ap = rules.airport(conn, e["ades"]) or {}
    city = ap.get("city") or e["ades"]
    if e["ades"] not in {x["ades"] for x in prev} and e["ades"] not in {x["adep"] for x in prev}:
        if _mark(conn, f"airport:{e['ades']}", "aeroporto", f"Primeiro pouso em {city}", f"{e['ades']} · {e['flight_number'] or ''}".strip(" ·"), e, now):
            new.append({"kind": "aeroporto", "title": f"Primeiro pouso em {city}", "icao": e["ades"]})
    if e["registration"] and e["registration"] not in {x["registration"] for x in prev}:
        if _mark(conn, f"reg:{e['registration']}", "aeronave", f"Primeira vez na {e['registration']}", e["type_icao"], e, now):
            new.append({"kind": "aeronave", "title": f"Primeira vez na {e['registration']}"})
    for h in HOURS:
        if pb < h * 60 <= tot["b"]:
            if _mark(conn, f"hours:{h}", "horas", f"{h} hora{'s' if h > 1 else ''} de bloco no LineOps", None, e, now):
                new.append({"kind": "horas", "title": f"{h} hora{'s' if h > 1 else ''} de bloco", "n": h})
    for n in LANDINGS:
        if pl < n <= tot["l"]:
            if _mark(conn, f"landings:{n}", "pousos", f"{n} pouso{'s' if n > 1 else ''}", None, e, now):
                new.append({"kind": "pousos", "title": f"{n} pouso{'s' if n > 1 else ''}", "n": n})
    streak = 0
    for x in rows(conn.execute("SELECT landing_vs_fpm, landing_g, type_icao FROM logbook_entry WHERE reversed_at IS NULL AND id<=? ORDER BY id DESC LIMIT 25", (e["id"],))):
        ev = landing.evaluate(x["landing_vs_fpm"], x["landing_g"], type_icao=x["type_icao"])
        if ev["tier"] in ("manteiga", "suave"):
            streak += 1
        else:
            break
    if streak in STREAKS:
        if _mark(conn, f"streak:{streak}:{e['id']}", "sequencia", f"{streak} toques suaves seguidos", None, e, now):
            new.append({"kind": "sequencia", "title": f"{streak} toques suaves seguidos", "n": streak})
    if (e["night_min"] or 0) > 0 and not any((x["night_min"] or 0) > 0 for x in prev):
        if _mark(conn, "night:first", "noturno", "Primeiro voo noturno", None, e, now):
            new.append({"kind": "noturno", "title": "Primeiro voo noturno"})
    return new


def celebrate(conn, f, e, new, now):
    from . import tripulacao
    if not new:
        return
    big = [m for m in new if m["kind"] in ("horas", "pousos", "sequencia", "noturno")]
    first = [m for m in new if m["kind"] == "aeroporto"]
    if first:
        c = tripulacao.for_flight(conn, f)
        ap = rules.airport(conn, first[0]["icao"]) or {}
        txt = (f"Primeira vez sua em {ap.get('city') or first[0]['icao']}? Bem-vindo, comandante! Anotei no seu diário."
               if c else f"Primeiro pouso em {ap.get('city') or first[0]['icao']} registrado no seu diário.")
        if c:
            tripulacao.say(conn, f"marco_ap:{e['id']}", c["fo"], f"Primeira vez em {first[0]['icao']}", txt, flight_id=f["id"], now=now)
        else:
            equipe.post(conn, f"marco_ap:{e['id']}", "chefia", f"Primeira vez em {first[0]['icao']}", txt, flight_id=f["id"], now=now)
    if big:
        lines = ["Comandante, parabéns:", ""] + [f"• {m['title']}" for m in big]
        if any(m["kind"] == "horas" and m["n"] >= 100 for m in big):
            lines += ["", "Isso aqui é experiência de linha de verdade. Continue assim."]
        equipe.post(conn, f"marco:{e['id']}", "chefia", "Marco de carreira: " + big[0]["title"], "\n".join(lines),
                    flight_id=f["id"], now=now, links=[{"label": "Ver a carreira", "href": "#/carreira"}])


# ------------------------------------------------------------------ cheque de rota
def check_status(conn, now=None) -> dict:
    now = now or now_iso()
    last = one(conn.execute("""SELECT * FROM line_check WHERE status IN ('APROVADO','APROVADO_OBS') ORDER BY done_at DESC LIMIT 1"""))
    sched = one(conn.execute("SELECT c.*, f.flight_number, f.adep, f.ades, f.dep_utc FROM line_check c JOIN flight f ON f.id=c.flight_id "
                             "WHERE c.status='AGENDADO' ORDER BY c.id DESC LIMIT 1"))
    valid_until = None
    if last and last["done_at"]:
        d = _p(last["done_at"])
        valid_until = (d.replace(day=1) + timedelta(days=32 * 13)).replace(day=1) - timedelta(days=1)   # fim do 12º mês calendário seguinte
        valid_until = valid_until.strftime("%Y-%m-%d")
    hist = rows(conn.execute("SELECT c.*, f.flight_number, f.adep, f.ades FROM line_check c LEFT JOIN flight f ON f.id=c.flight_id ORDER BY c.id DESC LIMIT 10"))
    for h in hist:
        h["items"] = jloads(h["items"], [])
    return {"last": last, "valid_until": valid_until, "scheduled": sched, "history": hist,
            "due": not valid_until or valid_until < now[:10], "rule": "RBAC 121.440 (14 CFR 121.440): cheque em rota a cada 12 meses calendários"}


def check_clock(conn, now) -> None:
    st = check_status(conn, now)
    nowd = _p(now)
    s = st["scheduled"]
    if s:
        f = one(conn.execute("SELECT state, dep_utc FROM flight WHERE id=?", (s["flight_id"],)))
        stale = bool(f and f["dep_utc"] and _p(f["dep_utc"]) < nowd - timedelta(hours=48))
        if not f or f["state"] in ("CANCELADO", "REJEITADO") or (f["state"] != "APROVADO" and stale):
            conn.execute("UPDATE line_check SET status='NAO_REALIZADO', done_at=? WHERE id=?", (now, s["id"]))
            equipe.post(conn, f"lc_miss:{s['id']}", "chefia", "Cheque de rota remarcado",
                        f"O cheque de rota no {s['flight_number'] or 'voo'} não aconteceu. Vou escalar o checador em outro voo seu.", now=now)
        return
    if not st["due"]:
        return
    if not conn.execute("SELECT 1 FROM logbook_entry WHERE reversed_at IS NULL LIMIT 1").fetchone():
        return                                             # primeiro voo ainda não aconteceu: sem cheque logo de cara
    f = one(conn.execute("""SELECT f.* FROM flight f WHERE f.state='RESERVADO' AND f.aircraft_id IS NOT NULL AND f.dep_utc BETWEEN ? AND ?
                            ORDER BY f.dep_utc LIMIT 1""", ((nowd + timedelta(hours=20)).strftime(FMT), (nowd + timedelta(days=15)).strftime(FMT))))
    if not f:
        return
    checker = random.Random(f"lineops-checker-{f['id']}").choice(CHECKERS)
    cid = conn.execute("INSERT INTO line_check(flight_id, checker, announced_at) VALUES (?,?,?)", (f["id"], checker, now)).lastrowid
    equipe.post(conn, f"lc_new:{cid}", "chefia", f"Cheque de rota no {f['flight_number'] or 'voo'} {f['adep']}→{f['ades']}",
                f"Comandante, o seu cheque em rota anual (RBAC 121.440) vai ser no {f['flight_number'] or 'voo'} {f['adep']}→{f['ades']}, "
                f"{f['dep_utc'][8:10]}/{f['dep_utc'][5:7]} às {f['dep_utc'][11:16]}Z. O checador é o {checker}, no jumpseat.\n\n"
                "O que ele vai olhar:\n• despacho assinado por você e pelo DOV antes do calço\n• pontualidade (OTP15)\n"
                "• toque (razão e G)\n• combustível consumido × OFP (quando houver o dado)\n\n"
                "Ele precisa ver o voo: acompanhe pela IVAO ou pelo ACARS. Voo declarado depois não vale como cheque.",
                flight_id=f["id"], level="acao", ack=True, now=now, links=[{"label": "Flight Center", "href": f"#/voo/{f['id']}"}])


def check_evaluate(conn, f, e, now) -> dict | None:
    c = one(conn.execute("SELECT * FROM line_check WHERE flight_id=? AND status='AGENDADO'", (f["id"],)))
    if not c:
        return None
    if e["source"] == "MANUAL":
        conn.execute("UPDATE line_check SET status='NAO_REALIZADO', done_at=?, logbook_id=? WHERE id=?", (now, e["id"], c["id"]))
        equipe.post(conn, f"lc_res:{c['id']}", "chefia", "Cheque de rota não realizado", "O voo do cheque foi declarado depois, sem acompanhamento: "
                    "o checador não tem o que avaliar. Vou remarcar num próximo voo. Acompanhe pela IVAO ou pelo ACARS.",
                    flight_id=f["id"], now=now, sender=c["checker"], sender_title="Checador")
        return {"status": "NAO_REALIZADO"}
    items, fail, obs = [], False, False
    rel = one(conn.execute("SELECT * FROM dispatch_release WHERE flight_id=? AND superseded_at IS NULL ORDER BY id DESC LIMIT 1", (f["id"],)))
    if rel and rel["pic_signed_at"] and e["out_utc"] and rel["pic_signed_at"] <= e["out_utc"]:
        items.append({"item": "Despacho", "ok": True, "note": f"assinado pelos dois antes do calço (DOV {rel['dov_name']})"})
    elif rel and rel["pic_signed_at"]:
        items.append({"item": "Despacho", "ok": False, "note": "assinado só depois do calço"}); obs = True
    else:
        items.append({"item": "Despacho", "ok": False, "note": "voo sem despacho assinado (RBAC 121.663)"}); fail = True
    d = one(conn.execute("SELECT * FROM flight_delay WHERE flight_id=?", (f["id"],)))
    if d and d["out_delay_min"] is not None:
        late = d["out_delay_min"] > OTP_MIN
        items.append({"item": "Pontualidade", "ok": not late, "note": f"calço fora {d['out_delay_min']:+d} min do programado"})
        obs = obs or late
    ev = landing.evaluate(e["landing_vs_fpm"], e["landing_g"], reported=bool(e["hard_landing_reported"]), type_icao=e["type_icao"])
    if ev["tier"]:
        okt = ev["tier"] in ("manteiga", "suave", "normal")
        items.append({"item": "Toque", "ok": okt, "note": f"{ev['vs']} fpm, {ev['g']:.2f} G ({ev['label'].lower()})"})
        if ev["tier"] in ("duro", "severo"):
            fail = True
        elif ev["tier"] == "firme":
            obs = True
    o = one(conn.execute("SELECT data FROM ofp WHERE flight_id=? ORDER BY id DESC LIMIT 1", (f["id"],)))
    od = jloads(o["data"], {}) if o else {}
    plan = (od.get("fuel_trip") or 0) + (od.get("fuel_taxi") or 0)
    if e["fuel_used_kg"] and plan:
        dev = (e["fuel_used_kg"] - plan) / plan * 100
        okf = dev <= 10
        items.append({"item": "Combustível", "ok": okf, "note": f"{int(e['fuel_used_kg']):,} kg × {int(plan):,} kg do OFP ({dev:+.0f}%)".replace(",", ".")})
        obs = obs or not okf
    else:
        items.append({"item": "Combustível", "ok": None, "note": "sem o consumo medido ou sem OFP (a IVAO não publica combustível)"})
    status = "REPROVADO" if fail else "APROVADO_OBS" if obs else "APROVADO"
    conn.execute("UPDATE line_check SET status=?, items=?, done_at=?, logbook_id=? WHERE id=?",
                 (status, json.dumps(items, ensure_ascii=False), now, e["id"], c["id"]))
    head = {"APROVADO": "Cheque de rota APROVADO. Voo limpo, comandante. Parabéns.",
            "APROVADO_OBS": "Cheque de rota APROVADO, com observações. Nada que impeça, mas vale prestar atenção:",
            "REPROVADO": "Cheque de rota NÃO aprovado. Vamos conversar e remarcar depois de um treinamento."}[status]
    body = head + "\n\n" + "\n".join(f"{'✔' if i['ok'] else '✘' if i['ok'] is False else '–'} {i['item']}: {i['note']}" for i in items)
    body += ("\n\nVálido por 12 meses calendários (RBAC 121.440)." if status != "REPROVADO" else "") + \
            "\n\nCritérios do LineOps: reprova pouso duro ou voo sem despacho; atraso, toque firme, despacho tardio e combustível acima de 10% do OFP viram observação."
    equipe.post(conn, f"lc_res:{c['id']}", "chefia", f"Resultado do cheque de rota · {f['flight_number'] or 'voo'}", body, flight_id=f["id"],
                level="alerta" if status == "REPROVADO" else "info", now=now, sender=c["checker"], sender_title="Checador",
                links=[{"label": "Ver a carreira", "href": "#/carreira"}])
    return {"status": status, "items": items}


# ------------------------------------------------------------------ boletim semanal
def bulletin_clock(conn, now) -> None:
    from .aeroscale.airports import get_tz
    from .equipe import _base_tz
    _b, tzn = _base_tz(conn, now)
    loc = _p(now).astimezone(get_tz(tzn) or timezone.utc)
    if loc.weekday() != 0 or loc.hour < 8:
        return
    wk = loc.strftime("%G-W%V")
    if conn.execute("SELECT 1 FROM ops_message WHERE key=?", (f"boletim:{wk}",)).fetchone():
        return
    end = (loc.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    start = (loc.date() - timedelta(days=7)).strftime("%Y-%m-%d")
    ent = rows(conn.execute("SELECT * FROM logbook_entry WHERE reversed_at IS NULL AND date BETWEEN ? AND ?", (start, end)))
    nxt = rows(conn.execute("""SELECT * FROM roster_leg WHERE removed_batch IS NULL AND activity NOT IN ('SOBREAVISO')
                               AND COALESCE(local_date, substr(dep_utc,1,10)) BETWEEN ? AND ? ORDER BY dep_utc""",
                            (loc.strftime("%Y-%m-%d"), (loc.date() + timedelta(days=6)).strftime("%Y-%m-%d"))))
    if not ent and not nxt:
        return
    lines = [f"Comandante, o resumo da semana de {start[8:10]}/{start[5:7]} a {end[8:10]}/{end[5:7]}:", ""]
    if ent:
        blk = sum(x["block_min"] or 0 for x in ent)
        vs = [abs(x["landing_vs_fpm"]) for x in ent if x["landing_vs_fpm"] is not None]
        lines.append(f"VOOS: {len(ent)} · {blk // 60}h{blk % 60:02d} de bloco · {sum(x['landings'] or 0 for x in ent)} pousos")
        if vs:
            best = min(ent, key=lambda x: abs(x["landing_vs_fpm"]) if x["landing_vs_fpm"] is not None else 9e9)
            lines.append(f"TOQUE: média de {round(sum(vs) / len(vs))} fpm · melhor {abs(best['landing_vs_fpm'])} fpm em {best['ades']}")
        o = otp(conn, start, end)
        if o["dep_otp"] is not None:
            lines.append(f"PONTUALIDADE: {o['dep_otp']}% das partidas no horário (OTP15)" + (f" · {o['pending_codes']} atraso(s) sem código" if o["pending_codes"] else ""))
        ms = rows(conn.execute("SELECT title FROM milestone WHERE achieved_at BETWEEN ? AND ?", (start + "T00:00:00Z", end + "T23:59:59Z")))
        if ms:
            lines.append("MARCOS: " + " · ".join(m["title"] for m in ms[:6]))
    else:
        lines.append("Nenhum voo no diário na semana passada.")
    if nxt:
        days = sorted({x["local_date"] or x["dep_utc"][:10] for x in nxt})
        lines.append(f"ESTA SEMANA: {len(nxt)} etapas em {len(days)} dia(s) de voo, começando {days[0][8:10]}/{days[0][5:7]}")
    cs = check_status(conn, now)
    lines.append("CHEQUE DE ROTA: " + (f"válido até {cs['valid_until'][8:10]}/{cs['valid_until'][5:7]}/{cs['valid_until'][:4]}" if not cs["due"] else
                                       f"agendado no {cs['scheduled']['flight_number']}" if cs["scheduled"] else "vencido, vou escalar"))
    lines += ["", "Bons voos nesta semana."]
    equipe.post(conn, f"boletim:{wk}", "chefia", f"Boletim da semana · {wk[-3:]}", "\n".join(lines), now=now,
                links=[{"label": "Ver a carreira", "href": "#/carreira"}])


# ------------------------------------------------------------------ ganchos do motor
def on_logbook(conn, f, e, now) -> None:
    """Depois que um voo entra no diário (PIREP aprovado ou voo declarado)."""
    from . import tripulacao
    record_delay(conn, f, e, now)
    check_evaluate(conn, f, e, now)
    new = milestones(conn, f, e, now)
    celebrate(conn, f, e, new, now)
    ev = landing.evaluate(e["landing_vs_fpm"], e["landing_g"], type_icao=e["type_icao"])
    tripulacao.after_landing(conn, f, e, ev["tier"], now)


MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]


def month_close_clock(conn, now) -> None:
    """Fechamento do mês: no dia 1 (a partir das 9h da base) a Chefia manda o balanço do mês que acabou, do seu diário."""
    from .aeroscale.airports import get_tz
    from .equipe import _base_tz
    _b, tzn = _base_tz(conn, now)
    loc = _p(now).astimezone(get_tz(tzn) or timezone.utc)
    if loc.day > 3 or (loc.day == 1 and loc.hour < 9):
        return
    first = loc.date().replace(day=1)
    prev_end = first - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    ym = prev_start.strftime("%Y-%m")
    if conn.execute("SELECT 1 FROM ops_message WHERE key=?", (f"fechamento:{ym}",)).fetchone():
        return
    ent = rows(conn.execute("SELECT * FROM logbook_entry WHERE reversed_at IS NULL AND date BETWEEN ? AND ? ORDER BY date",
                            (prev_start.isoformat(), prev_end.isoformat())))
    if not ent:
        return
    blk = sum(x["block_min"] or 0 for x in ent)
    night = sum(x["night_min"] or 0 for x in ent)
    before = {r[0] for r in conn.execute("SELECT adep FROM logbook_entry WHERE reversed_at IS NULL AND date<? UNION "
                                         "SELECT ades FROM logbook_entry WHERE reversed_at IS NULL AND date<?", (prev_start.isoformat(),) * 2)}
    aps = []
    for x in ent:
        for a in (x["adep"], x["ades"]):
            if a not in aps:
                aps.append(a)
    new_aps = [a for a in aps if a not in before]
    vs = [abs(x["landing_vs_fpm"]) for x in ent if x["landing_vs_fpm"] is not None]
    best = min((x for x in ent if x["landing_vs_fpm"] is not None), key=lambda x: abs(x["landing_vs_fpm"]), default=None)
    hard = sum(1 for x in ent if landing.evaluate(x["landing_vs_fpm"], x["landing_g"], type_icao=x["type_icao"])["tier"] in ("duro", "severo"))
    o = otp(conn, prev_start.isoformat(), prev_end.isoformat())
    days = len({x["date"] for x in ent})
    lines = [f"Comandante, o fechamento de {MESES[prev_start.month - 1]} de {prev_start.year}, do seu diário:", "",
             f"VOOS: {len(ent)} em {days} dia(s) · {blk // 60}h{blk % 60:02d} de bloco" + (f" ({night // 60}h{night % 60:02d} noturnas)" if night else ""),
             f"AEROPORTOS: {len(aps)}" + (f" · primeira vez em {', '.join(new_aps[:8])}" if new_aps else "")]
    if vs:
        lines.append(f"TOQUE: média de {round(sum(vs) / len(vs))} fpm · melhor {abs(best['landing_vs_fpm'])} fpm em {best['ades']}"
                     + (f" · {hard} pouso(s) duro(s)" if hard else " · nenhum pouso duro"))
    if o["dep_otp"] is not None:
        lines.append(f"PONTUALIDADE: {o['dep_otp']}% das partidas no horário (OTP15)")
    types = {}
    for x in ent:
        types[x["type_icao"] or "—"] = types.get(x["type_icao"] or "—", 0) + (x["block_min"] or 0)
    if len(types) > 1:
        lines.append("POR TIPO: " + " · ".join(f"{t} {m // 60}h{m % 60:02d}" for t, m in sorted(types.items(), key=lambda kv: -kv[1])))
    lines += ["", "Obrigado pelo mês. Vamos para o próximo."]
    equipe.post(conn, f"fechamento:{ym}", "chefia", f"Fechamento de {MESES[prev_start.month - 1]}", "\n".join(lines), now=now,
                links=[{"label": "Ver o mês", "href": "#/carreira"}])


def clock(conn, now) -> None:
    check_clock(conn, now)
    bulletin_clock(conn, now)
    month_close_clock(conn, now)


def summary(conn, now=None) -> dict:
    from . import tripulacao
    now = now or now_iso()
    ms = rows(conn.execute("SELECT * FROM milestone ORDER BY achieved_at DESC, key"))
    month = now[:7]
    return {"milestones": ms, "otp_all": otp(conn), "otp_month": otp(conn, month + "-01", month + "-31"), "check": check_status(conn, now),
            "colleagues": tripulacao.colleagues(conn), "airports": sorted({m["key"].split(":")[1] for m in ms if m["kind"] == "aeroporto"}),
            "delay_codes": DELAY_CODES}
