"""Equipe da operação: quem fala com você dentro da companhia, como numa empresa aérea de verdade.

Os setores e o que cada um faz seguem a estrutura real de uma companhia brasileira sob o RBAC 121:
* Planejamento de Escala — publica a escala, no mínimo mensal, com 5 dias de antecedência (Lei 13.475, art. 26).
* Controle de Tripulantes (CCO) — executa a escala no dia: alterações, lembrete de apresentação, reserva e sobreaviso
  (sobreaviso: apresentação em até 90 min se acionado, art. 43; reserva: 3 a 6 h no aeroporto, art. 44).
* Despacho Operacional (DOV) — prepara e acompanha o voo; o despacho é assinado pelo DOV e pelo comandante
  (RBAC 121, seção 121.663, que segue o 14 CFR 121.663).
* Controle de Carregamento — LOADSHEET e mudanças de última hora.
* Controle de Manutenção (MCC) — liberação da aeronave, itens diferidos (MEL simulado).
* Chefia de Equipamento — acompanha os seus voos aprovados.
* Folha de Pagamento — fechamento do mês (se os salários estiverem ligados).

As PESSOAS são personagens fictícios (nomes editáveis em Ajustes), nunca funcionários reais. O CONTEÚDO de cada mensagem
vem de fatos do LineOps: a escala que o AeroScale mandou, o OFP que o SimBrief gerou, o MEL que você registrou, o que a IVAO
mostrou. Nenhum dado é inventado; quando falta, a mensagem diz que falta.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from . import fleet, ofp as ofp_mod, rules
from .db import audit, jloads, now_iso, one, rows, tx
from .errors import Conflict, NotFound

FMT = "%Y-%m-%dT%H:%M:%SZ"
_log = logging.getLogger("lineops.equipe")
DISCLAIMER = "Personagens fictícios da LineOps. Não representam funcionários reais de nenhuma companhia aérea ou empresa."

SECTORS = {
    "escala": "Planejamento de Escala",
    "tripulantes": "Controle de Tripulantes · CCO",
    "despacho": "Despacho Operacional · CCO",
    "carregamento": "Controle de Carregamento",
    "mcc": "Controle de Manutenção · MCC",
    "manutencao": "Chefia de Manutenção",
    "chefia": "Chefia de Equipamento",
    "folha": "Folha de Pagamento",
    "cabine": "Cabine · interfone",
    "transporte": "Transporte de Tripulantes",
    "hotelaria": "Hotelaria",
    "tripulacao": "Sua tripulação",
    "cco": "Coordenação do CCO",
}
SHIFTS = {"manha": (6, 14), "tarde": (14, 22), "noite": (22, 30)}
DEFAULT_STAFF = [
    ("escala", "todos", "Marina Duarte", "Analista de Planejamento de Escala"),
    ("tripulantes", "manha", "Rafael Nogueira", "Controlador de Tripulantes"),
    ("tripulantes", "tarde", "Juliana Prado", "Controladora de Tripulantes"),
    ("tripulantes", "noite", "Eduardo Lins", "Controlador de Tripulantes"),
    ("despacho", "manha", "Carla Menezes", "DOV · Despachante Operacional de Voo"),
    ("despacho", "tarde", "Bruno Tavares", "DOV · Despachante Operacional de Voo"),
    ("despacho", "noite", "Fernanda Costa", "DOV · Despachante Operacional de Voo"),
    ("carregamento", "todos", "Thiago Ramos", "Agente de Controle de Carregamento"),
    ("mcc", "todos", "Paulo Siqueira", "Engenheiro de Controle de Manutenção"),
    ("manutencao", "todos", "Eng. Walter Bastos", "Chefe de Manutenção de Linha"),
    ("chefia", "todos", "Cmte. Ricardo Alves", "Chefe de Equipamento"),
    ("folha", "todos", "Equipe de Folha", "Gente e Gestão"),
    ("cabine", "todos", "Patrícia Lopes", "Chefe de Cabine"),
    ("transporte", "todos", "Central de Transporte", "Transporte de Tripulantes"),
    ("hotelaria", "todos", "Amanda Ribeiro", "Analista de Hotelaria"),
    ("cco", "todos", "Sandro Vieira", "Coordenador do CCO"),
]
DOV_WINDOW_H = 3            # o DOV começa a trabalhar no seu voo 3 h antes da partida
SIMBRIEF_EVERY_S = 300


# ------------------------------------------------------------------ equipe
# cada companhia tem a sua gente (personagens fictícios, como os da GOL); setor e cargo são os mesmos
STAFF_BY_CAREER = {
    "AZU": [
        ("escala", "todos", "Bianca Lourenço", "Analista de Planejamento de Escala"),
        ("tripulantes", "manha", "Caio Montenegro", "Controlador de Tripulantes"),
        ("tripulantes", "tarde", "Tânia Bezerra", "Controladora de Tripulantes"),
        ("tripulantes", "noite", "Gilberto Rios", "Controlador de Tripulantes"),
        ("despacho", "manha", "Mirela Cordeiro", "DOV · Despachante Operacional de Voo"),
        ("despacho", "tarde", "Leandro Quintela", "DOV · Despachante Operacional de Voo"),
        ("despacho", "noite", "Raquel Pontes", "DOV · Despachante Operacional de Voo"),
        ("carregamento", "todos", "Hugo Sarmento", "Agente de Controle de Carregamento"),
        ("mcc", "todos", "Joana Albuquerque", "Engenheira de Controle de Manutenção"),
        ("manutencao", "todos", "Eng. Murilo Tenório", "Chefe de Manutenção de Linha"),
        ("chefia", "todos", "Cmte. Augusto Vilela", "Chefe de Equipamento"),
        ("folha", "todos", "Equipe de Folha", "Gente e Gestão"),
        ("cabine", "todos", "Lorena Castilho", "Chefe de Cabine"),
        ("transporte", "todos", "Central de Transporte", "Transporte de Tripulantes"),
        ("hotelaria", "todos", "Nádia Correia", "Analista de Hotelaria"),
        ("cco", "todos", "Felipe Sampaio", "Coordenador do CCO"),
    ],
    "TAM": [
        ("escala", "todos", "Helena Siqueira", "Analista de Planejamento de Escala"),
        ("tripulantes", "manha", "Marcos Tavora", "Controlador de Tripulantes"),
        ("tripulantes", "tarde", "Paula Rezende", "Controladora de Tripulantes"),
        ("tripulantes", "noite", "Otávio Brandão", "Controlador de Tripulantes"),
        ("despacho", "manha", "Simone Arruda", "DOV · Despachante Operacional de Voo"),
        ("despacho", "tarde", "Ricardo Fontana", "DOV · Despachante Operacional de Voo"),
        ("despacho", "noite", "Lívia Camargo", "DOV · Despachante Operacional de Voo"),
        ("carregamento", "todos", "Igor Medeiros", "Agente de Controle de Carregamento"),
        ("mcc", "todos", "Sérgio Valadares", "Engenheiro de Controle de Manutenção"),
        ("manutencao", "todos", "Eng. Cláudio Peixoto", "Chefe de Manutenção de Linha"),
        ("chefia", "todos", "Cmte. Fernanda Ribeiro", "Chefe de Equipamento"),
        ("folha", "todos", "Equipe de Folha", "Gente e Gestão"),
        ("cabine", "todos", "Roberta Galvão", "Chefe de Cabine"),
        ("transporte", "todos", "Central de Transporte", "Transporte de Tripulantes"),
        ("hotelaria", "todos", "Vitor Assunção", "Analista de Hotelaria"),
        ("cco", "todos", "Denise Moraes", "Coordenadora do CCO"),
    ],
}


def _seed(conn):
    """Postos padrão; setores novos de versões futuras entram sem mexer nos nomes que você já trocou."""
    from .careers import code_of
    staff = STAFF_BY_CAREER.get(code_of(conn), DEFAULT_STAFF)
    if conn.execute("SELECT COUNT(*) FROM ops_staff").fetchone()[0] < len(staff):
        conn.executemany("INSERT OR IGNORE INTO ops_staff(sector, shift, name, title) VALUES (?,?,?,?)", staff)


def _base_tz(conn, now: str):
    from .journey import schedule_params
    p, _s = schedule_params(conn, now[:10])
    base = p.get("base_resolved") or p.get("base")
    return base, rules.airport_tz(conn, base) or "America/Sao_Paulo"


def _local(conn, utc_iso: str | None, icao: str | None) -> str:
    """'14:00Z (11:00 local)' quando o fuso é conhecido; senão só UTC."""
    if not utc_iso:
        return "—"
    lab = rules.local_label(conn, datetime.strptime(utc_iso, FMT).replace(tzinfo=timezone.utc), icao)
    return f"{utc_iso[11:16]}Z" + (f" ({lab['hhmm']} local)" if lab else "")


def shift_of(conn, now: str) -> str:
    _b, tzn = _base_tz(conn, now)
    from .aeroscale.airports import get_tz
    h = datetime.strptime(now, FMT).replace(tzinfo=timezone.utc).astimezone(get_tz(tzn) or timezone.utc).hour
    return "manha" if 6 <= h < 14 else "tarde" if 14 <= h < 22 else "noite"


def who(conn, sector: str, now: str | None = None) -> dict:
    """Quem está de plantão no setor agora (o turno segue a hora local da sua base)."""
    _seed(conn)
    now = now or now_iso()
    r = one(conn.execute("SELECT * FROM ops_staff WHERE sector=? AND shift=?", (sector, shift_of(conn, now)))) or \
        one(conn.execute("SELECT * FROM ops_staff WHERE sector=? ORDER BY shift='todos' DESC LIMIT 1", (sector,)))
    return r or {"name": SECTORS.get(sector, sector), "title": SECTORS.get(sector, "")}


def staff(conn) -> list[dict]:
    _seed(conn)
    out = rows(conn.execute("SELECT * FROM ops_staff"))
    order = list(SECTORS)
    sh = ["todos", "manha", "tarde", "noite"]
    out.sort(key=lambda r: (order.index(r["sector"]) if r["sector"] in order else 99, sh.index(r["shift"]) if r["shift"] in sh else 9))
    for r in out:
        r["sector_label"] = SECTORS.get(r["sector"], r["sector"])
    return out


def save_staff(conn, items: list[dict], user_id=None) -> list[dict]:
    with tx(conn):
        _seed(conn)
        for it in items or []:
            name = str(it.get("name") or "").strip()[:60]
            if not name:
                continue
            n = conn.execute("UPDATE ops_staff SET name=?, title=COALESCE(?, title) WHERE sector=? AND shift=?",
                             (name, (str(it.get("title")).strip()[:80] or None) if it.get("title") else None, it.get("sector"), it.get("shift"))).rowcount
            if not n:
                raise NotFound(f"Posto não encontrado: {it.get('sector')}/{it.get('shift')}.")
        audit(conn, user_id, "team.update", "ops_staff", None)
    return staff(conn)


# ------------------------------------------------------------------ mensagens
def post(conn, key: str, sector: str, subject: str, body: str, *, flight_id=None, level="info", ack=False,
         links=None, now=None, channel=None, sender=None, sender_title=None) -> bool:
    """channel='acars': a mensagem chegou no avião durante o voo (a tela mostra como impressão do ACARS).
    sender: quem assina, quando não é o plantonista do setor (ex.: o copiloto do dia, o checador)."""
    now = now or now_iso()
    w = {"name": sender, "title": sender_title or ""} if sender else who(conn, sector, now)
    cur = conn.execute("""INSERT OR IGNORE INTO ops_message(key, ts, sector, sender, sender_title, subject, body, flight_id, level, ack_required, links, channel)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (key, now, sector, w["name"], w["title"], subject[:160], body[:4000], flight_id, level, 1 if ack else 0,
                        json.dumps(links or [], ensure_ascii=False), channel))
    return cur.rowcount > 0


def messages(conn, *, sector=None, flight_id=None, unread=False, limit=80) -> list[dict]:
    q, a = ["SELECT * FROM ops_message WHERE 1=1"], []
    if sector:
        q.append("AND sector=?"); a.append(sector)
    if flight_id:
        q.append("AND flight_id=?"); a.append(flight_id)
    if unread:
        q.append("AND read_at IS NULL")
    q.append("ORDER BY ts DESC, id DESC LIMIT ?"); a.append(limit)
    out = rows(conn.execute(" ".join(q), a))
    for m in out:
        m["links"] = jloads(m["links"], [])
        m["sector_label"] = SECTORS.get(m["sector"], m["sector"])
    return out


def counts(conn) -> dict:
    r = conn.execute("SELECT SUM(read_at IS NULL) u, SUM(ack_required=1 AND acked_at IS NULL) a FROM ops_message").fetchone()
    return {"unread": r["u"] or 0, "pending_ack": r["a"] or 0}


def mark(conn, mid: int, *, ack=False) -> dict:
    with tx(conn):
        m = one(conn.execute("SELECT * FROM ops_message WHERE id=?", (mid,)))
        if not m:
            raise NotFound("Mensagem não encontrada.")
        ts = now_iso()
        conn.execute("UPDATE ops_message SET read_at=COALESCE(read_at, ?) WHERE id=?", (ts, mid))
        if ack:
            if not m["ack_required"]:
                raise Conflict("Esta mensagem não pede ciente.", code="sem_ciente")
            conn.execute("UPDATE ops_message SET acked_at=COALESCE(acked_at, ?) WHERE id=?", (ts, mid))
    return messages_by_id(conn, mid)


def messages_by_id(conn, mid):
    m = one(conn.execute("SELECT * FROM ops_message WHERE id=?", (mid,)))
    m["links"] = jloads(m["links"], [])
    m["sector_label"] = SECTORS.get(m["sector"], m["sector"])
    return m


def read_all(conn) -> int:
    return conn.execute("UPDATE ops_message SET read_at=? WHERE read_at IS NULL", (now_iso(),)).rowcount


# ------------------------------------------------------------------ despacho (DOV + comandante)
def release_of(conn, flight_id: int) -> dict | None:
    r = one(conn.execute("SELECT * FROM dispatch_release WHERE flight_id=? AND superseded_at IS NULL ORDER BY id DESC LIMIT 1", (flight_id,)))
    if r:
        r["reasons"] = jloads(r["reasons"], [])
    return r


def sign_release(conn, flight_id: int, user_id=None) -> dict:
    with tx(conn):
        r = release_of(conn, flight_id)
        if not r:
            raise Conflict("Ainda não há despacho do DOV para este voo (falta o OFP).", code="sem_despacho")
        if r["status"] != "LIBERADO":
            raise Conflict("O DOV reteve o despacho: resolva os motivos antes de assinar.", code="despacho_retido", details=r["reasons"])
        if not r["pic_signed_at"]:
            ts = now_iso()
            conn.execute("UPDATE dispatch_release SET pic_signed_at=? WHERE id=?", (ts, r["id"]))
            audit(conn, user_id, "dispatch.sign", "flight", flight_id, {"release": r["id"]})
            f = _flight(conn, flight_id)
            post(conn, f"release_signed:{r['id']}", "despacho", f"{_fname(f)}: despacho assinado",
                 f"Recebi a sua assinatura. O despacho do {_fname(f)} está assinado pelos dois (DOV {r['dov_name']} e comandante). "
                 "Sigo acompanhando o voo daqui. Bom voo!", flight_id=flight_id)
    return release_of(conn, flight_id)


def _flight(conn, fid):
    return one(conn.execute("""SELECT f.*, a.registration, a.id AS ac_id FROM flight f LEFT JOIN aircraft a ON a.id=f.aircraft_id
                               WHERE f.id=?""", (fid,))) if fid else None


def _fname(f) -> str:
    return f"{f['flight_number'] or 'voo ' + str(f['id'])} {f['adep']}→{f['ades']}" if f else "voo"


def _kg(v):
    return "—" if v is None else f"{v:,.0f} kg".replace(",", ".")


def _metar(conn, icao):
    r = one(conn.execute("SELECT raw, fetched_at FROM weather_cache WHERE kind='metar' AND icao=?", (icao,)))
    return r if r and r["raw"] else None


def dov_briefing(conn, flight_id: int, ofp_row: dict, now: str) -> None:
    """O DOV lê o OFP, o MEL e a meteorologia em cache e libera (ou retém) o despacho, explicando por quê."""
    f = _flight(conn, flight_id)
    if not f or f["state"] in ("APROVADO", "CANCELADO"):
        return
    d = ofp_row["data"]
    reasons, notes = [], []
    if d.get("adep") != f["adep"] or d.get("ades") != f["ades"]:
        reasons.append(f"o OFP é de {d.get('adep')}→{d.get('ades')}, e o voo é {f['adep']}→{f['ades']}")
    av = fleet.availability(conn, f["ac_id"], now) if f["ac_id"] else None
    if not f["ac_id"]:
        reasons.append("o voo ainda não tem aeronave da frota")
    elif av["status"] == "INDISPONIVEL":
        reasons.append("aeronave indisponível: " + "; ".join(av["blocking"]))
    for k, lim, lab in (("est_zfw", "max_zfw", "ZFW"), ("est_tow", "max_tow", "TOW"), ("est_ldw", "max_ldw", "LW")):
        if d.get(k) is not None and d.get(lim) is not None and d[k] > d[lim]:
            reasons.append(f"{lab} de {_kg(d[k])} acima do máximo de {_kg(d[lim])}")
    status = "RETIDO" if reasons else "LIBERADO"
    mel = (av or {}).get("restrictions") or []
    w = who(conn, "despacho", now)
    lines = [f"Comandante, segue o despacho do {_fname(f)}" + (f" com a {f['registration']}" if f["registration"] else "") + ".", ""]
    lines.append(f"Rota: {d.get('route') or 'não veio no OFP'}")
    if d.get("initial_altitude"):
        lines.append(f"Nível inicial: FL{round(d['initial_altitude'] / 100):03d}" + (f" · cost index {d['cost_index']}" if d.get("cost_index") is not None else ""))
    if d.get("eet_min"):
        lines.append(f"Tempo de voo estimado: {d['eet_min'] // 60}h{d['eet_min'] % 60:02d}")
    lines.append(f"Combustível na partida {_kg(d.get('fuel_ramp'))} · viagem {_kg(d.get('fuel_trip'))} · reserva {_kg(d.get('fuel_reserve'))}"
                 + (f" · alternado {_kg(d.get('fuel_alternate'))}" if d.get("fuel_alternate") is not None else ""))
    lines.append(f"Alternado: {d.get('altn') or 'nenhum no OFP'}")
    if d.get("est_tow") is not None:
        lines.append(f"Pesos estimados: ZFW {_kg(d.get('est_zfw'))} · TOW {_kg(d.get('est_tow'))} · LW {_kg(d.get('est_ldw'))}")
    for ic in [x for x in (f["adep"], f["ades"], d.get("altn")) if x]:
        m = _metar(conn, ic)
        lines.append(f"METAR {ic}: {m['raw']}" if m else f"METAR {ic}: sem leitura recente no LineOps (atualize na etapa Meteorologia)")
    lines.append("NOTAM: o LineOps não tem fonte de NOTAM; consulte o AISWEB antes de sair.")
    lines.append("MEL: " + ("; ".join(f"{r['title']} ({r['restriction'].lower()})" for r in mel) if mel else "nenhum item diferido aberto na aeronave"))
    for x in ofp_row.get("warnings") or []:
        if x.get("level") == "warn":
            notes.append(x["message"])
    lines.append("")
    if status == "LIBERADO":
        lines.append("Despacho LIBERADO e assinado por mim. Falta a sua assinatura no Flight Center.")
    else:
        lines.append("Despacho RETIDO: " + "; ".join(reasons) + ". Me mande um OFP novo ou ajuste o voo que eu reavalio.")
    if notes:
        lines.append("Observações: " + " ".join(notes))
    with tx(conn):
        conn.execute("UPDATE dispatch_release SET superseded_at=? WHERE flight_id=? AND superseded_at IS NULL", (now, flight_id))
        conn.execute("""INSERT INTO dispatch_release(flight_id, ofp_id, status, reasons, dov_name, dov_signed_at) VALUES (?,?,?,?,?,?)""",
                     (flight_id, ofp_row["id"], status, json.dumps(reasons, ensure_ascii=False), w["name"], now))
        post(conn, f"dov_release:{ofp_row['id']}", "despacho",
             f"{_fname(f)}: despacho {'liberado, aguardando sua assinatura' if status == 'LIBERADO' else 'RETIDO'}",
             "\n".join(lines), flight_id=flight_id, level="acao" if status == "LIBERADO" else "alerta", ack=False,
             links=[{"label": "Abrir no Flight Center", "href": f"#/voo/{flight_id}"}], now=now)


# ------------------------------------------------------------------ motor: eventos do audit_log
def _cursor(conn, key):
    r = conn.execute("SELECT value FROM setting WHERE key=?", (key,)).fetchone()
    return int(r[0]) if r else None


def _set_cursor(conn, key, v):
    conn.execute("INSERT INTO setting(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(int(v))))


def _schedule_summary(conn, sid: int) -> dict:
    legs = rows(conn.execute("""SELECT local_date, adep, ades, activity FROM roster_leg WHERE schedule_id=? AND removed_batch IS NULL""", (sid,)))
    fl = [l for l in legs if l["activity"] not in ("SOBREAVISO",)]
    return {"legs": len([l for l in fl if l["activity"] != "EXTRA"]), "days": len({l["local_date"] for l in fl}),
            "standby": len([l for l in legs if l["activity"] == "SOBREAVISO"]),
            "reserves": conn.execute("SELECT COUNT(*) FROM roster_reserve WHERE schedule_id=?", (sid,)).fetchone()[0],
            "airports": sorted({l["ades"] for l in fl})}


def _on_import(conn, a, now):
    bid = a["entity_id"]
    if not bid:
        return
    bid = int(bid)
    first = conn.execute("SELECT MIN(id) FROM import_batch").fetchone()[0] == bid
    batch = one(conn.execute("SELECT * FROM import_batch WHERE id=?", (bid,)))
    for s in rows(conn.execute("SELECT * FROM roster_schedule WHERE last_batch=?", (bid,))):
        p = jloads(s["params"], {})
        sm = _schedule_summary(conn, s["id"])
        imp = datetime.strptime(batch["imported_at"], FMT).date()
        start = datetime.strptime(s["start_date"], "%Y-%m-%d").date() if s["start_date"] else None
        if start and start > imp:
            ant = (start - imp).days
            prazo = (f"Publicada com {ant} dia(s) de antecedência, dentro dos 5 dias da Lei 13.475 (art. 26)." if ant >= 5 else
                     f"Atenção: publicada com {ant} dia(s) de antecedência; a Lei 13.475 (art. 26) pede no mínimo 5.")
        else:
            prazo = "A escala já está em vigor."
        off = len(p.get("planned_days_off") or [])
        body = (f"Comandante, a sua escala {s['name'] or ''} de {s['start_date']} a {s['end_date']} foi publicada.\n\n"
                f"{sm['legs']} etapa(s) em {sm['days']} dia(s) de voo · {off} folga(s) · {sm['reserves']} bloco(s) de reserva · "
                f"{sm['standby']} sobreaviso(s).\n"
                + (f"Destinos: {', '.join(sm['airports'][:14])}{'…' if len(sm['airports']) > 14 else ''}.\n" if sm["airports"] else "")
                + f"{prazo}\n\nConfira na Escala e dê o seu ciente. Dúvidas sobre trocas, fale com o Controle de Tripulantes.")
        again = conn.execute("SELECT 1 FROM ops_message WHERE key LIKE ?", (f"schedule_pub:{s['id']}:%",)).fetchone()
        if again:
            body = "Versão atualizada da escala (o AeroScale mandou de novo).\n\n" + body
        post(conn, f"schedule_pub:{s['id']}:{bid}", "escala", f"Escala {'atualizada' if again else 'publicada'}: {s['start_date'][8:10]}/{s['start_date'][5:7]} a "
             f"{s['end_date'][8:10]}/{s['end_date'][5:7]}", body, level="acao", ack=True,
             links=[{"label": "Abrir a escala", "href": f"#/escala/{s['start_date']}"}], now=now)
    if first:
        return
    ch = rows(conn.execute("""SELECT c.kind, c.old_values, l.* FROM roster_leg_change c
                              JOIN roster_leg l ON l.id=c.leg_id WHERE c.batch_id=? ORDER BY l.dep_utc""", (bid,)))
    if ch:
        items = []
        for c in ch[:12]:
            fn = c["flight_number"] or f"{c['adep']}→{c['ades']}"
            if c["kind"] == "REMOVED":
                items.append(f"• {fn} de {c['local_date']} saiu da sua escala")
            elif c["kind"] == "RESTORED":
                items.append(f"• {fn} de {c['local_date']} voltou para a sua escala")
            else:
                old = jloads(c["old_values"], {})
                lab = {"dep_utc": "partida", "arr_utc": "chegada", "flight_number": "número", "registration": "matrícula", "adep": "origem", "ades": "destino"}
                fmt = lambda k, v: (str(v)[11:16] + "Z") if k.endswith("_utc") and v else (v if v not in (None, "") else "—")
                det = ", ".join(f"{lab.get(k, k)} {fmt(k, v)} → {fmt(k, c.get(k))}" for k, v in list(old.items()) if k in lab)[:200]
                items.append(f"• {fn} de {c['local_date']} mudou" + (f" ({det})" if det else ""))
        post(conn, f"schedule_change:{bid}", "tripulantes", f"Alteração de escala: {len(ch)} etapa(s)",
             "Comandante, houve alteração na sua escala:\n\n" + "\n".join(items)
             + (f"\n…e mais {len(ch) - 12}." if len(ch) > 12 else "") + "\n\nPreciso do seu ciente.",
             level="alerta", ack=True, links=[{"label": "Abrir a escala", "href": "#/escala"}], now=now)


def _process_audit(conn, now):
    cur = _cursor(conn, "ops_audit_cursor")
    top = conn.execute("SELECT COALESCE(MAX(id),0) FROM audit_log").fetchone()[0]
    if cur is None:                                      # primeira vez: começa do zero se o banco é novo, senão do agora
        cur = 0 if not conn.execute("SELECT 1 FROM flight LIMIT 1").fetchone() else top
        _set_cursor(conn, "ops_audit_cursor", cur)
    for a in rows(conn.execute("SELECT * FROM audit_log WHERE id>? ORDER BY id LIMIT 300", (cur,))):
        try:
            with tx(conn):                               # savepoint: um evento que falha não deixa metade gravada
                _handle(conn, a, min(a["ts"] or now, now))   # a mensagem leva a hora do fato, e quem responde é quem estava de turno
        except Exception:                                # uma mensagem que falha não trava as outras, mas fica no log
            _log.exception("equipe: evento %s (%s) falhou", a["id"], a["action"])
        _set_cursor(conn, "ops_audit_cursor", a["id"])


def _safe(conn, what, fn, *args):
    """Cada reação ao voo roda isolada: se a do toque falhar, a da carreira ainda acontece (e o erro vai para o log)."""
    try:
        with tx(conn):
            fn(*args)
    except Exception:
        _log.exception("equipe: %s falhou", what)


def _flight_of(conn, a, det):
    e, eid = a["entity"], a["entity_id"]
    if e == "flight":
        return _flight(conn, int(eid))
    tbl = {"pirep": "pirep", "acars_session": "acars_session", "logbook_entry": "logbook_entry", "ofp": "ofp", "loadsheet": "loadsheet"}.get(e)
    if tbl and eid:
        r = one(conn.execute(f"SELECT flight_id FROM {tbl} WHERE id=?", (int(eid),)))
        return _flight(conn, r and r["flight_id"])
    return _flight(conn, det.get("flight")) if det.get("flight") else None


def _mcc_status(conn, f, now):
    if not f or not f["ac_id"]:
        return
    av = fleet.availability(conn, f["ac_id"], now)
    if av["status"] == "DISPONIVEL":
        body = f"A {f['registration']} está liberada para o {_fname(f)}, sem itens diferidos abertos."
        lvl = "info"
    elif av["status"] == "COM_RESTRICAO":
        body = (f"A {f['registration']} vai para o {_fname(f)} com restrições:\n"
                + "\n".join(f"• {r['title']} ({r['restriction'].lower()})" + (f": {r['text']}" if r.get("text") else "") for r in av["restrictions"])
                + "\n\nItens simulados do seu MEL. Leia antes do voo.")
        lvl = "alerta"
    else:
        body = f"A {f['registration']} está INDISPONÍVEL: " + "; ".join(av["blocking"]) + ". Peça outra aeronave."
        lvl = "alerta"
    post(conn, f"mcc_ac:{f['id']}:{f['ac_id']}", "mcc", f"{f['registration']}: situação para o {f['flight_number'] or 'voo'}", body,
         flight_id=f["id"], level=lvl, links=[{"label": "Ficha da aeronave", "href": f"#/frota/{f['ac_id']}"}], now=now)


def _handle(conn, a, now):
    act, det = a["action"], jloads(a["detail"], {}) or {}
    if act == "import.aeroscale":
        return _on_import(conn, a, now)
    f = _flight_of(conn, a, det)
    if act == "flight.book" and f:
        post(conn, f"book:{f['id']}", "tripulantes", f"{_fname(f)} confirmado na sua escala",
             f"Comandante, confirmado você no {_fname(f)}, partida {_local(conn, f['dep_utc'], f['adep'])}"
             + (f", aeronave {f['registration']}." if f["registration"] else ". A aeronave ainda está a definir.")
             + " O DOV começa a preparar o despacho 3 h antes.", flight_id=f["id"], now=now,
             links=[{"label": "Flight Center", "href": f"#/voo/{f['id']}"}])
        _mcc_status(conn, f, now)
    elif act == "flight.assign_aircraft" and f:
        _mcc_status(conn, f, now)
    elif act == "ofp.attach" and f:
        o = ofp_mod.get(conn, int(a["entity_id"]))
        dov_briefing(conn, f["id"], o, now)
    elif act == "loadsheet.issue" and f:
        from . import loadsheet as ls_mod
        ls = ls_mod.get(conn, int(a["entity_id"]))
        d = ls["data"]
        val = {l["label"]: l for l in d.get("lines", [])}
        pick = lambda p: next((l for k, l in val.items() if k.startswith(p)), {})
        zfw, tow, lw = pick("Peso zero"), pick("Peso de decolagem"), pick("Peso de pouso")
        ed = ls["edition"]
        body = (f"Comandante, LOADSHEET {'edição ' + str(ed) if ed > 1 else ''} do {_fname(f)} emitido.\n"
                f"ZFW {_kg(zfw.get('value'))} · TOW {_kg(tow.get('value'))} · LW {_kg(lw.get('value'))}\n"
                + ("Margens: " + ", ".join(f"{n} {_kg(l.get('margin'))}" for n, l in (("ZFW", zfw), ("TOW", tow), ("LW", lw)) if l.get("margin") is not None) + "\n"
                   if any(l.get("margin") is not None for l in (zfw, tow, lw)) else "")
                + ("Esta é uma mudança de última hora (LMC) em relação à edição anterior.\n" if ed > 1 else "")
                + "Centro de gravidade não calculado pelo LineOps (falta o peso e balanceamento da aeronave).")
        post(conn, f"ls:{a['entity_id']}", "carregamento", f"{_fname(f)}: LOADSHEET {'LMC ' if ed > 1 else ''}ed. {ed}", body,
             flight_id=f["id"], now=now, links=[{"label": "Flight Center", "href": f"#/voo/{f['id']}"}])
    elif act == "discrepancy.add":
        r = one(conn.execute("SELECT d.*, a.registration FROM discrepancy d JOIN aircraft a ON a.id=d.aircraft_id WHERE d.id=?", (int(a["entity_id"]),)))
        if r:
            post(conn, f"disc:{r['id']}", "mcc", f"{r['registration']}: discrepância registrada",
                 f"Registrada na {r['registration']}: {r['title']}. Restrição: {r['restriction'].lower()}"
                 + (f" ({r['restriction_text']})" if r["restriction_text"] else "") + (f". Diferida até {r['due_date']}" if r["due_date"] else "")
                 + ". Controle simulado, não é MEL oficial.", level="alerta" if r["restriction"] in ("BLOQUEIA", "CONFIRMA") else "info",
                 links=[{"label": "Ficha da aeronave", "href": f"#/frota/{r['aircraft_id']}"}], now=now)
    elif act == "discrepancy.close":
        r = one(conn.execute("SELECT d.*, a.registration FROM discrepancy d JOIN aircraft a ON a.id=d.aircraft_id WHERE d.id=?", (int(a["entity_id"]),)))
        if r:
            post(conn, f"disc_close:{r['id']}", "mcc", f"{r['registration']}: item corrigido",
                 f"{r['title']} corrigido e fechado na {r['registration']}.", now=now)
    elif act == "acars.start" and f:
        rel = release_of(conn, f["id"])
        s = one(conn.execute("SELECT sim FROM acars_session WHERE id=?", (int(a["entity_id"]),)))
        via = "pela IVAO" if s and s["sim"] == "ivao" else "pelo ACARS"
        post(conn, f"follow:{a['entity_id']}", "despacho", f"{_fname(f)}: acompanhando o voo",
             f"Estou acompanhando o {_fname(f)} {via}."
             + ("" if rel and rel["pic_signed_at"] else " Atenção: o voo começou sem o despacho assinado pelos dois.")
             + " Qualquer coisa, me chame.", flight_id=f["id"], level="info" if rel and rel["pic_signed_at"] else "alerta", now=now)
    elif act in ("acars.complete", "ivao.pirep") and f:
        post(conn, f"pirep_rcv:{a['entity_id']}", "despacho", f"{_fname(f)}: voo encerrado",
             f"Calços registrados no {_fname(f)}. O PIREP foi para a sua revisão. Obrigado pelo voo!", flight_id=f["id"], now=now,
             links=[{"label": "Revisar PIREP", "href": f"#/pireps/{a['entity_id']}"}] if a["entity"] == "pirep" else [])
    elif act == "pirep.approve" and f:
        e = one(conn.execute("SELECT * FROM logbook_entry WHERE id=?", (det.get("logbook"),))) if det.get("logbook") else None
        body = f"Recebi o {_fname(f)} aprovado no seu diário."
        if e and e["block_min"]:
            body += f" {e['block_min'] // 60}h{e['block_min'] % 60:02d} de bloco."
        post(conn, f"pirep_ok:{a['entity_id']}", "chefia", f"{_fname(f)}: voo registrado", body, flight_id=f["id"], now=now)
        if e:
            _safe(conn, "toque", touchdown, conn, f, e, now)
            from . import carreira
            _safe(conn, "carreira", carreira.on_logbook, conn, f, e, now)
    elif act == "flight.manual" and f:
        post(conn, f"manual:{det.get('logbook')}", "chefia", f"{_fname(f)}: voo declarado recebido",
             f"Recebido o lançamento do {_fname(f)} no diário como voo declarado ({str(det.get('network') or '').lower() or 'offline'}). "
             "Bom trabalho, comandante.", flight_id=f["id"], now=now)
        e = one(conn.execute("SELECT * FROM logbook_entry WHERE id=?", (det.get("logbook"),))) if det.get("logbook") else None
        if e:
            _safe(conn, "toque", touchdown, conn, f, e, now)
            from . import enroute, carreira
            _safe(conn, "pernoite", enroute.after_declared, conn, f, e, now)
            _safe(conn, "carreira", carreira.on_logbook, conn, f, e, now)
    elif act == "payroll.close":
        m = det.get("month") or ""
        post(conn, f"payroll:{a['entity_id']}", "folha", f"Folha de {m[5:7]}/{m[:4]} fechada",
             f"A folha de {m[5:7]}/{m[:4]} foi fechada" + (f": líquido de R$ {det['net']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                                                          if isinstance(det.get("net"), (int, float)) else "") + ". O contracheque está em Salários.",
             links=[{"label": "Abrir Salários", "href": f"#/salarios/{m}"}], now=now)


def touchdown(conn, f, e, now):
    """Reação ao toque: a Chefia de Manutenção comenta, o MCC bloqueia a aeronave para inspeção e a Chefia convoca."""
    from . import landing
    lim = {}
    if e["aircraft_id"]:
        a = fleet.get_aircraft(conn, e["aircraft_id"], totals=False)
        lim = a.get("limits") or {}
    gw = None
    if e["pirep_id"]:
        p = one(conn.execute("SELECT data FROM pirep WHERE id=?", (e["pirep_id"],)))
        gw = (jloads(p["data"], {}) if p else {}).get("gw_on_kg")
    mlw = (lim.get("mlw_kg") or {}).get("value")
    ev = landing.evaluate(e["landing_vs_fpm"], e["landing_g"], landing_weight_kg=gw, mlw_kg=float(mlw) if mlw else None,
                          reported=bool(e["hard_landing_reported"]), type_icao=e["type_icao"])
    if not ev["tier"]:
        return
    r = conn.execute("SELECT value FROM setting WHERE key='landing_style'").fetchone()
    style = (r[0] if r else "") or "folclorico"
    t = landing.texts(ev, {"reg": e["registration"], "ades": e["ades"], "fn": e["flight_number"] or _fname(f), "seed": e["id"],
                           "lw": _kg(gw), "mlw": _kg(float(mlw)) if mlw else "—"}, style)
    subj = {"manteiga": "pouso manteiga", "firme": "toque firme", "duro": "POUSO DURO", "severo": "POUSO DURO SEVERO"}.get(ev["tier"], ev["label"].lower())
    if ev["reported"] and ev["tier"] not in ("duro", "severo"):
        subj = "pouso duro reportado"
    if ev["inspection"] and e["aircraft_id"] and not conn.execute(
            "SELECT 1 FROM discrepancy WHERE aircraft_id=? AND mel_ref='AMM 05-51' AND description LIKE ?", (e["aircraft_id"], f"%diário #{e['id']}%")).fetchone():
        fleet.add_discrepancy(conn, e["aircraft_id"], "Inspeção de pouso duro" + (" (Fase 1 e provável Fase 2)" if ev["inspection"] == "fase2" else " (Fase 1)"),
                              description=f"{e['flight_number'] or 'Voo'} {e['adep']}→{e['ades']}: {ev['vs']} fpm, {ev['g']:.2f} G. "
                                          + "; ".join(ev["reasons"]) + f". Diário #{e['id']}. Simulado: não é documento de manutenção.",
                              restriction="BLOQUEIA", restriction_text="aeronave fora de serviço até a inspeção", mel_ref="AMM 05-51")
    if t["mnt"]:
        post(conn, f"touchdown:{e['id']}", "manutencao", f"{e['registration'] or _fname(f)}: {subj} em {e['ades']}", t["mnt"],
             flight_id=f["id"], level=t["level"], ack=bool(ev["inspection"]),
             links=[{"label": "Ficha da aeronave", "href": f"#/frota/{e['aircraft_id']}"}] if e["aircraft_id"] else [], now=now)
    if t["chefia"]:
        post(conn, f"touchdown_chefia:{e['id']}", "chefia", f"{e['flight_number'] or 'Voo'}: compareça à chefia", t["chefia"],
             flight_id=f["id"], level="alerta", ack=True, now=now)


# ------------------------------------------------------------------ motor: fases do voo (ACARS/IVAO)
def _process_acars(conn, now):
    cur = _cursor(conn, "ops_acars_cursor")
    top = conn.execute("SELECT COALESCE(MAX(id),0) FROM acars_message").fetchone()[0]
    if cur is None:
        cur = top
        _set_cursor(conn, "ops_acars_cursor", cur)
    for m in rows(conn.execute("""SELECT m.*, s.flight_id FROM acars_message m JOIN acars_session s ON s.id=m.session_id
                                  WHERE m.id>? ORDER BY m.id LIMIT 300""", (cur,))):
        f = _flight(conn, m["flight_id"])
        t = m["text"]
        now = min(m["t"] or now, now)
        if f and t.startswith("Decolagem"):
            post(conn, f"ff_off:{m['id']}", "despacho", f"{_fname(f)}: decolagem", f"Vi a sua decolagem de {f['adep']} às {m['t'][11:16]}Z. "
                 f"Boa viagem, comandante.", flight_id=f["id"], now=now)
        elif f and t.startswith("Pouso"):
            post(conn, f"ff_on:{m['id']}", "despacho", f"{_fname(f)}: pouso", f"Pouso registrado às {m['t'][11:16]}Z. Bem-vindo a {f['ades']}.",
                 flight_id=f["id"], now=now)
        elif f and t.startswith("Sem sinal"):
            post(conn, f"ff_lost:{m['id']}", "despacho", f"{_fname(f)}: perdemos você", "Perdi o seu sinal na IVAO com o avião no ar. "
                 "Reconecte quando puder ou encerre o acompanhamento no Flight Center.", flight_id=f["id"], level="alerta", now=now)
        _set_cursor(conn, "ops_acars_cursor", m["id"])


# ------------------------------------------------------------------ motor: relógio (lembretes, reserva, sobreaviso, DOV)
def _process_clock(conn, now):
    from .journey import _duty_times, schedule_params
    nowd = datetime.strptime(now, FMT).replace(tzinfo=timezone.utc)
    soon = (nowd + timedelta(hours=30)).strftime(FMT)
    legs = rows(conn.execute("""SELECT l.*, f.id AS fid, f.state AS fstate, a.registration AS reg FROM roster_leg l
                                LEFT JOIN flight f ON f.leg_id=l.id AND f.state<>'CANCELADO' LEFT JOIN aircraft a ON a.id=f.aircraft_id
                                WHERE l.removed_batch IS NULL AND l.activity NOT IN ('SOBREAVISO') AND l.dep_utc > ? AND l.dep_utc <= ?
                                ORDER BY l.dep_utc""", (now, soon)))
    days = {}
    for l in legs:
        days.setdefault(l["local_date"] or l["dep_utc"][:10], []).append(l)
    flown = ("APROVADO", "PIREP_RECEBIDO", "EM_VOO")
    days = {k: v for k, v in days.items() if not all(l["fstate"] in flown for l in v)}   # dia já voado antes da hora: sem lembrete
    for day, dl in list(days.items())[:2]:
        p, sched = schedule_params(conn, day)
        dt = _duty_times(p, dl[0]["dep_utc"], dl[-1]["arr_utc"], (sched or {}).get("airline"))
        rep = dt["report_utc"] or dl[0]["dep_utc"]
        hours = (datetime.strptime(rep, FMT).replace(tzinfo=timezone.utc) - nowd).total_seconds() / 3600
        if 0 < hours <= 24:
            body = (f"Comandante, confirmando a sua apresentação {_local(conn, rep, dl[0]['adep'])} em {dl[0]['adep']}"
                    + ("" if dt["report_utc"] else " (horário da 1ª partida; a escala não define apresentação)") + ".\n\n"
                    + "\n".join(f"• {l['flight_number'] or '—'} {l['adep']}→{l['ades']} {l['dep_utc'][11:16]}–{l['arr_utc'][11:16]}Z"
                                + (f" · {l['reg']}" if l["reg"] else "")
                                + (" · já voado (fora do horário)" if l["fstate"] in flown else " · ainda não reservado no LineOps" if not l["fid"] else "")
                                for l in dl)
                    + _crew_suffix(conn, day) + "\n\nBom descanso.")
            post(conn, f"report:{day}", "tripulantes", f"Apresentação {day[8:10]}/{day[5:7]} em {dl[0]['adep']}", body, level="acao",
                 links=[{"label": "Ver o dia", "href": f"#/escala/{day}"}], now=now)
    today = now[:10]
    tomorrow = (nowd + timedelta(days=1)).strftime("%Y-%m-%d")
    for r in rows(conn.execute("SELECT * FROM roster_reserve WHERE end_date>=? AND start_date<=?", (today, tomorrow))):
        if r["start_date"] <= tomorrow:
            post(conn, f"reserve:{r['uid']}", "tripulantes", f"Reserva em {r['location']} a partir de {r['start_date'][8:10]}/{r['start_date'][5:7]}",
                 f"Comandante, você está de reserva em {r['location']} de {r['start_date']} a {r['end_date']}. "
                 "A reserva é cumprida no aeroporto, de 3 a 6 h por período (Lei 13.475, art. 44). Se for acionado, aviso por aqui.", now=now)
        if r["act_start"] and r["act_start"] <= tomorrow:
            al = rows(conn.execute("""SELECT flight_number, adep, ades, dep_utc, arr_utc FROM roster_leg WHERE reserve_uid=? AND removed_batch IS NULL
                                      ORDER BY dep_utc LIMIT 10""", (r["uid"],)))
            post(conn, f"reserve_act:{r['uid']}", "tripulantes", f"Acionamento de reserva: {r['act_start'][8:10]}/{r['act_start'][5:7]}",
                 f"Comandante, você foi acionado da reserva em {r['location']} a partir de {r['act_start']}"
                 + (f" por {r['act_days']} dia(s)" if r["act_days"] else "") + ".\n\n"
                 + ("\n".join(f"• {l['flight_number'] or '—'} {l['adep']}→{l['ades']} {l['dep_utc'][:10]} {l['dep_utc'][11:16]}–{l['arr_utc'][11:16]}Z" for l in al)
                    if al else "As etapas aparecem na Escala.") + "\n\nPreciso do seu ciente.",
                 level="alerta", ack=True, links=[{"label": "Abrir a escala", "href": f"#/escala/{r['act_start']}"}], now=now)
    for s in rows(conn.execute("""SELECT * FROM roster_leg WHERE removed_batch IS NULL AND activity='SOBREAVISO' AND dep_utc > ? AND dep_utc <= ?""",
                               (now, (nowd + timedelta(hours=12)).strftime(FMT)))):
        post(conn, f"standby:{s['uid']}", "tripulantes", f"Sobreaviso {s['dep_utc'][8:10]}/{s['dep_utc'][5:7]}",
             f"Comandante, sobreaviso de {_local(conn, s['dep_utc'], s['adep'])} a {_local(conn, s['arr_utc'], s['adep'])}. "
             "Se acionado, apresente-se em até 90 min (150 min se for outro aeroporto da mesma cidade) — Lei 13.475, art. 43.", now=now)
    return _dov_clock(conn, now, nowd)


def _crew_suffix(conn, day) -> str:
    try:
        from . import tripulacao
        line = tripulacao.crew_line(conn, day)
        return f"\n\n{line}" if line else ""
    except Exception:
        return ""


def _dov_clock(conn, now, nowd):
    pend = rows(conn.execute("""SELECT f.* FROM flight f WHERE f.state='RESERVADO' AND f.dep_utc > ? AND f.dep_utc <= ?
                                AND NOT EXISTS (SELECT 1 FROM ofp o WHERE o.flight_id=f.id) ORDER BY f.dep_utc""",
                             ((nowd - timedelta(hours=1)).strftime(FMT), (nowd + timedelta(hours=DOV_WINDOW_H)).strftime(FMT))))
    if not pend:
        return []
    for f in pend:
        try:
            sb = rules.simbrief(conn, {"airline": f["airline"], "flight_number": f["flight_number"], "adep": f["adep"], "ades": f["ades"],
                                       "dep_utc": f["dep_utc"], "arr_utc": f["arr_utc"], "type_icao": f["type_icao"], "flight_id": f["leg_id"]}, None)
        except Exception:
            sb = {}
        post(conn, f"dov_req:{f['id']}", "despacho", f"{_fname(f)}: preparando o seu despacho",
             f"Comandante, estou montando o despacho do {_fname(f)} (partida {_local(conn, f['dep_utc'], f['adep'])}). "
             "Gere o plano no SimBrief" + (" pelo link abaixo, já com a rota do RPL" if sb.get("url") else "")
             + ". Assim que ele aparecer, eu anexo o OFP e te mando o despacho para assinar.",
             flight_id=f["id"], level="acao",
             links=([{"label": "Gerar no SimBrief", "href": sb["url"], "external": True}] if sb.get("url") else [])
             + [{"label": "Flight Center", "href": f"#/voo/{f['id']}"}], now=now)
    return pend


def _dov_fetch(conn, pend, now, opener=None):
    """Fora de transação: busca o último plano do SimBrief e, se for de um voo pendente, o DOV anexa."""
    nowd = datetime.strptime(now, FMT).replace(tzinfo=timezone.utc)
    cfg = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM setting WHERE key IN ('simbrief_userid','simbrief_username','ops_simbrief_last')")}
    uid, unm = (cfg.get("simbrief_userid") or "").strip(), (cfg.get("simbrief_username") or "").strip()
    last = cfg.get("ops_simbrief_last")
    if not (uid or unm) or (last and (nowd - datetime.strptime(last, FMT).replace(tzinfo=timezone.utc)).total_seconds() < SIMBRIEF_EVERY_S):
        return
    with tx(conn):
        conn.execute("INSERT INTO setting(key, value) VALUES ('ops_simbrief_last', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now,))
    try:
        raw = opener() if opener else ofp_mod.fetch(uid or None, unm or None)
        data, _w = ofp_mod.parse(raw)
    except Exception:
        return
    if data.get("ofp_id") and conn.execute("SELECT 1 FROM ofp WHERE ofp_id=?", (data["ofp_id"],)).fetchone():
        return
    for f in pend:
        if data.get("adep") == f["adep"] and data.get("ades") == f["ades"]:
            ofp_mod.attach(conn, f["id"], raw, source="simbrief-api", user_id=None)   # sem usuário = anexado pelo DOV; o audit dispara o briefing
            break


# ------------------------------------------------------------------ entrada
def run(conn, now: str | None = None, opener=None, wx_fetch=None) -> None:
    """Uma passada do motor. Idempotente: pode rodar a cada requisição e no laço do servidor.
    wx_fetch(conn, icaos) só existe para os testes (o padrão busca METAR/TAF pelo weather do AeroScale)."""
    now = now or now_iso()
    with tx(conn):
        _seed(conn)
        if not conn.execute("SELECT 1 FROM ops_message LIMIT 1").fetchone():
            welcome(conn, now)
        elif not conn.execute("SELECT 1 FROM setting WHERE key='release_note' AND value='1.0'").fetchone():
            release_note(conn, now)
        _process_audit(conn, now)
        _process_acars(conn, now)
    try:
        from . import enroute
        with tx(conn):
            need = enroute.process(conn, now)
        if need:                                         # METAR/TAF do destino: busca fora da transação e manda na mesma passada
            enroute.refresh_weather(conn, need, wx_fetch)
            with tx(conn):
                enroute.process(conn, now, wx_tried=True)
    except Exception:
        _log.exception("mensagens em rota")
    try:
        from . import stay
        with tx(conn):
            stay.dispatch(conn, now)                     # carro, check-in, despertar e saída do pernoite, na hora certa
    except Exception:
        _log.exception("pernoite")
    try:
        from . import carreira, tripulacao
        with tx(conn):
            tripulacao.clock(conn, now)                  # briefing da tripulação na apresentação
            carreira.clock(conn, now)                    # cheque de rota e boletim semanal
    except Exception:
        _log.exception("tripulação, cheque e boletim")
    try:
        from . import troca
        troca.clock(conn, now)                           # respostas dos pedidos de troca de escala
    except Exception:
        _log.exception("pedidos de troca de escala")
    try:
        with tx(conn):
            pend = _process_clock(conn, now)
        if pend:
            _dov_fetch(conn, pend, now, opener)
            with tx(conn):
                _process_audit(conn, now)                # o OFP anexado pelo DOV gera o briefing na mesma passada
    except Exception:
        _log.exception("relógio da equipe (lembretes, reserva, DOV)")


def welcome(conn, now):
    _set_release(conn)
    st = {(r["sector"], r["shift"]): r for r in staff(conn)}
    nm = lambda s, sh="todos": (st.get((s, sh)) or st.get((s, "manha")) or {}).get("name", SECTORS[s])
    post(conn, "welcome", "chefia", "Bem-vindo à equipe",
         "Comandante, seja bem-vindo. A partir de agora a operação fala com você por aqui:\n\n"
         f"• {nm('escala')} (Planejamento de Escala) publica a sua escala, com 5 dias de antecedência.\n"
         f"• {nm('tripulantes', 'manha')}, {nm('tripulantes', 'tarde')} e {nm('tripulantes', 'noite')} (Controle de Tripulantes, nos três turnos do CCO) "
         "avisam alterações, lembram a apresentação e cuidam de reserva e sobreaviso.\n"
         f"• {nm('despacho', 'manha')}, {nm('despacho', 'tarde')} e {nm('despacho', 'noite')} (DOV) preparam o despacho 3 h antes, mandam o plano e acompanham o voo. "
         "O despacho só fica pronto com a assinatura dos dois.\n"
         f"• {nm('carregamento')} (Carregamento) manda o LOADSHEET, e {nm('mcc')} (MCC) libera a aeronave.\n\n"
         f"Bons voos. — {nm('chefia')}\n\n({DISCLAIMER})", level="info", now=now)


def _set_release(conn):
    conn.execute("INSERT INTO setting(key, value) VALUES ('release_note', '1.0') ON CONFLICT(key) DO UPDATE SET value=excluded.value")


def release_note(conn, now):
    """Uma vez, para quem atualizou: o que a 1.0 trouxe e onde ligar."""
    _set_release(conn)
    post(conn, "release:1.0", "chefia", "LineOps 1.0: a equipe cresceu",
         "Comandante, chegou a 1.0. O que muda para você:\n\n"
         "• Celular: a operação te avisa pelo Telegram (com botões de ciente, despacho e código de atraso) ou pelo ntfy. "
         "Ligue em Ajustes › Celular.\n"
         "• Tripulação do dia: agora você voa com copiloto e comissários, a mesma equipe a viagem inteira. Eles falam com você "
         "no briefing, no interfone e depois do pouso.\n"
         "• Carreira: pontualidade (OTP15) com código de atraso, marcos, cheque de rota anual e com quem você mais voou.\n"
         "• Boletim da semana: toda segunda de manhã, um resumo meu.\n"
         "• Backup automático todo dia.\n\n"
         f"Bons voos. — {who(conn, 'chefia', now)['name']}\n\n({DISCLAIMER})",
         level="info", now=now, links=[{"label": "Ligar o celular", "href": "#/ajustes"}, {"label": "Ver a carreira", "href": "#/carreira"}])
