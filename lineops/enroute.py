"""Mensagens em rota: o que chega no avião enquanto você voa.

Numa companhia de verdade, a tripulação recebe no ACARS (impressora da cabine de comando) o tempo atualizado do destino,
a programação seguinte e os dados do pernoite. O LineOps faz o mesmo, pelas fases que a IVAO ou o cliente ACARS mostram:

  subida / ~15% do voo   Cabine (interfone): serviço de bordo; refeição da tripulação se já passou de 3h30 de jornada
  ~25% do voo            Controle de Tripulantes: o que vem depois do pouso: próxima perna OU pernoite (fim da jornada,
                         repouso mínimo da Lei 13.475, próxima apresentação, hotel e transporte, refeições, sol)
  ~55% ou 40 min do pouso  DOV: METAR e TAF do destino (e do alternado do OFP), lidos na hora
  ~70% do voo            MCC: o que a aeronave faz depois do pouso
  depois do pouso        Controle de Tripulantes: boas-vindas e quando o repouso começa (só em pernoite)

O progresso sai da posição (distância que falta até o destino) ou, sem posição, do tempo desde a decolagem contra o tempo
de voo do OFP. Nada é inventado: hotel e transporte vêm do cadastro de pernoites (seu, ou dos briefings que você importar);
sem cadastro, a mensagem diz que falta. As regras citadas são as da Lei 13.475/2017:
  art. 35 §4  a jornada termina 30 min após o corte dos motores (voo doméstico)
  art. 47     fora da base: quarto individual com banheiro privativo e transporte aeroporto–hotel; §4 sem transporte,
              o repouso conta de quando o transporte fica disponível
  art. 48     repouso mínimo: 12 h após jornada de até 12 h; 16 h após mais de 12 até 15 h; 24 h após mais de 15 h
  art. 61 §3  alimentação em voo em intervalos de no máximo 4 h
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from . import equipe, fleet, geo, journey, nighttime, ofp as ofp_mod, rules
from .db import audit, jloads, now_iso, one, rows, tx
from .errors import FGVError

log = logging.getLogger("lineops.enroute")
FMT = "%Y-%m-%dT%H:%M:%SZ"
AIR = {"AIRBORNE", "CLIMB", "CRUISE", "DESCENT", "APPROACH"}
LANDED = {"LANDED", "TAXI_IN", "IN"}
WX_FRESH_MIN = 20
OFF_SCHEDULE_H = 3            # voando a mais de 3 h do horário da escala: os horários do pernoite são os programados
RELEASE_AFTER_MIN = 30        # Lei 13.475, art. 35 §4 (doméstico)
SOURCE = "Lei 13.475/2017 (arts. 35 §4, 47, 48 e 61 §3)"


# ------------------------------------------------------------------ utilidades
def _p(s):
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc) if s else None


def _iso(d):
    return d.strftime(FMT) if d else None


def _hm(m) -> str:
    m = int(round(m))
    return f"{m // 60}h{m % 60:02d}"


def _lt(conn, iso: str | None, icao: str | None, with_date=False) -> str:
    """'13:00 local' (ou '16:00Z' sem fuso conhecido)."""
    if not iso:
        return "—"
    lab = rules.local_label(conn, _p(iso), icao)
    if not lab:
        return (f"{iso[8:10]}/{iso[5:7]} " if with_date else "") + f"{iso[11:16]}Z"
    d = lab.get("date") or iso[:10]
    return (f"{d[8:10]}/{d[5:7]} " if with_date else "") + f"{lab['hhmm']} local"


def min_rest(duty_min: int | None) -> int | None:
    """Repouso mínimo em minutos (Lei 13.475, art. 48)."""
    if duty_min is None:
        return None
    return 12 * 60 if duty_min <= 12 * 60 else 16 * 60 if duty_min <= 15 * 60 else 24 * 60


def _rest_rule(duty_min):
    if duty_min is None:
        return ""
    return "jornada de até 12 h" if duty_min <= 720 else "jornada de mais de 12 até 15 h" if duty_min <= 900 else "jornada de mais de 15 h"


def sun_events(lat: float, lon: float, start: datetime, hours: int = 30) -> list[tuple[str, datetime]]:
    """Pôr e nascer do sol (centro do disco a -0,833°), varrendo de 2 em 2 min com a fórmula do nighttime.py."""
    out, step = [], timedelta(minutes=2)
    t, prev = start, nighttime.sun_elevation_deg(lat, lon, start) + 0.833
    end = start + timedelta(hours=hours)
    while t < end:
        t2 = t + step
        e = nighttime.sun_elevation_deg(lat, lon, t2) + 0.833
        if prev > 0 >= e:
            out.append(("poente", t2))
        elif prev <= 0 < e:
            out.append(("nascente", t2))
        t, prev = t2, e
    return out


# ------------------------------------------------------------------ cadastro de pernoites (hotel, transporte)
def airport_info(conn, icao: str | None) -> dict | None:
    return one(conn.execute("SELECT * FROM airport_info WHERE icao=?", ((icao or "").upper(),))) if icao else None


def list_airport_info(conn) -> dict:
    items = rows(conn.execute("SELECT * FROM airport_info ORDER BY icao"))
    have = {i["icao"] for i in items}
    for i in items:
        i["place"] = journey.place(conn, i["icao"])
    p, _s = journey.schedule_params(conn, now_iso()[:10])
    base = p.get("base_resolved") or p.get("base")
    dests = [r[0] for r in conn.execute("""SELECT ades, COUNT(*) n FROM roster_leg WHERE removed_batch IS NULL AND activity<>'SOBREAVISO'
                                           AND ades IS NOT NULL GROUP BY ades ORDER BY n DESC""")]
    suggest = [{"icao": i, "place": journey.place(conn, i)} for i in dests if i not in have and i != base][:30]
    return {"items": items, "suggest": suggest, "base": base,
            "note": "O LineOps não inventa hotel: o que estiver aqui aparece nas mensagens de pernoite. Sem cadastro, a mensagem diz que falta."}


def save_airport_info(conn, icao: str, *, hotel=None, transport=None, notes=None, source=None, user_id=None) -> dict:
    icao = (icao or "").strip().upper()
    if len(icao) != 4 or not icao.isalnum():
        raise FGVError("ICAO inválido (4 letras).", code="campo_invalido")
    clean = lambda v, n: (str(v).strip()[:n] or None) if v not in (None, "") else None
    hotel, transport, notes, source = clean(hotel, 160), clean(transport, 200), clean(notes, 1000), clean(source, 80) or "cadastro manual"
    with tx(conn):
        old = airport_info(conn, icao)
        if old and old.get("hotel") != hotel and old.get("hotel_lat") is not None:
            conn.execute("""UPDATE airport_info SET hotel_address=NULL, hotel_lat=NULL, hotel_lon=NULL, hotel_ref=NULL, route_min=NULL,
                            route_km=NULL, route_src=NULL, route_at=NULL WHERE icao=?""", (icao,))
        if old and old.get("hotel") == hotel and old.get("hotel_ref") and source == "cadastro manual":
            source = old.get("source") or source              # editou só o transporte ou as notas: o hotel continua o do OpenStreetMap
        if not (hotel or transport or notes):
            conn.execute("DELETE FROM airport_info WHERE icao=?", (icao,))
            audit(conn, user_id, "airport_info.delete", "airport_info", icao)
            return {"icao": icao, "deleted": True}
        conn.execute("""INSERT INTO airport_info(icao, hotel, transport, notes, source, updated_at) VALUES (?,?,?,?,?,?)
                        ON CONFLICT(icao) DO UPDATE SET hotel=excluded.hotel, transport=excluded.transport, notes=excluded.notes,
                        source=excluded.source, updated_at=excluded.updated_at""", (icao, hotel, transport, notes, source, now_iso()))
        audit(conn, user_id, "airport_info.save", "airport_info", icao)
    return airport_info(conn, icao)


# ------------------------------------------------------------------ estado do voo e progresso
def flight_state(conn, s: dict) -> dict:
    """Fases a partir do snapshot da sessão e das mensagens do ACARS/IVAO (as duas fontes falam do mesmo jeito aqui)."""
    snap = jloads(s["snapshot"], {}) if isinstance(s.get("snapshot"), str) else (s.get("snapshot") or {})
    msgs = rows(conn.execute("SELECT kind, text, t FROM acars_message WHERE session_id=? ORDER BY id", (s["id"],)))
    first = lambda pred: next((m["t"] for m in msgs if pred(m)), None)
    off = snap.get("off_utc") or first(lambda m: m["kind"] == "OFF" or (m["text"] or "").startswith("Decolagem"))
    on = snap.get("on_utc") or first(lambda m: m["kind"] == "ON" or (m["text"] or "").startswith("Pouso"))
    phase = str(s.get("phase") or snap.get("phase") or "").upper()
    if not off and phase in AIR:
        r = conn.execute("SELECT MIN(t) FROM track_point WHERE session_id=? AND phase IN ('CLIMB','CRUISE','DESCENT','APPROACH','AIRBORNE')",
                         (s["id"],)).fetchone()
        off = r[0] if r else None
    landed = bool(on) or phase in LANDED
    return {"off_utc": off, "on_utc": on, "in_utc": snap.get("in_utc"), "phase": phase, "airborne": bool(off or phase in AIR) and not landed,
            "landed": landed, "lat": snap.get("lat"), "lon": snap.get("lon"), "gs": snap.get("gs_kt"), "alt": snap.get("alt_ft")}


def progress(conn, f: dict, st: dict, now: str) -> dict:
    """Fração do voo já feita e chegada estimada. Base: posição > OFP > escala."""
    a, b = journey.place(conn, f["adep"]), journey.place(conn, f["ades"])
    nowd = _p(now)
    total = geo.nm(a["lat"], a["lon"], b["lat"], b["lon"]) if None not in (a["lat"], a["lon"], b["lat"], b["lon"]) else None
    o = ofp_mod.latest(conn, f["id"])
    eet = (o or {}).get("data", {}).get("eet_min") if o else None
    basis, frac, eta, rem = None, None, None, None
    if total and total > 20 and isinstance(st.get("lat"), (int, float)) and isinstance(st.get("lon"), (int, float)):
        rem = geo.nm(st["lat"], st["lon"], b["lat"], b["lon"])
        frac, basis = max(0.0, min(1.0, 1 - rem / total)), "posição"
        if isinstance(st.get("gs"), (int, float)) and st["gs"] > 120:
            eta = nowd + timedelta(minutes=rem / st["gs"] * 60 + 8)       # + aproximação e táxi curto
    off = _p(st.get("off_utc"))
    if eet is None and f.get("dep_utc") and f.get("arr_utc"):
        blk = (_p(f["arr_utc"]) - _p(f["dep_utc"])).total_seconds() / 60
        eet, eet_src = (max(15, blk - 20), "escala") if blk > 25 else (None, None)
    else:
        eet_src = "OFP" if eet is not None else None
    if off and eet:
        el = (nowd - off).total_seconds() / 60
        if frac is None:
            frac, basis = max(0.0, min(1.0, el / eet)), f"tempo de voo do {eet_src}"
        if eta is None:
            eta = off + timedelta(minutes=eet + 5)
    elapsed = (nowd - off).total_seconds() / 60 if off else None
    return {"frac": frac, "eta_utc": _iso(eta), "remaining_nm": round(rem) if rem is not None else None, "total_nm": round(total) if total else None,
            "basis": basis, "elapsed_min": elapsed, "to_go_min": (eta - nowd).total_seconds() / 60 if eta else None}


# ------------------------------------------------------------------ o que vem depois do pouso
def _profile_report(f) -> int | None:
    try:
        return rules.crew_profile(f.get("airline") or "GLO").get("report_min")
    except Exception:
        return None


def plan_after(conn, f: dict, eta_utc: str | None = None, real_off: str | None = None) -> dict:
    """Próxima perna, pernoite ou fim de jornada, pela escala do AeroScale (mesma regra de pernoite da tela Dia)."""
    leg = one(conn.execute("SELECT * FROM roster_leg WHERE id=?", (f["leg_id"],))) if f.get("leg_id") else None
    out = {"kind": "sem_escala", "leg": leg, "off_schedule": False}
    if not leg:
        return out
    if real_off and leg["dep_utc"] and abs((_p(real_off) - _p(leg["dep_utc"])).total_seconds()) > OFF_SCHEDULE_H * 3600:
        out["off_schedule"] = True
    date = leg["local_date"] or leg["dep_utc"][:10]
    d = journey.day(conn, date)
    fl = [l for l in d["legs"] if l["is_flight"]]
    idx = next((i for i, l in enumerate(fl) if l["uid"] == leg["uid"]), None)
    p, sched = journey.schedule_params(conn, date)
    base = p.get("base_resolved") or p.get("base")
    rb = p.get("report_before") if isinstance(p.get("report_before"), (int, float)) else _profile_report(f)
    out.update(day=d, base=base, at=journey.place(conn, f["ades"]))
    if idx is not None and idx < len(fl) - 1:
        nxt = fl[idx + 1]
        turn = d["turns"][idx] if idx < len(d["turns"]) else None
        nf = one(conn.execute("""SELECT f.id, f.state, a.registration FROM flight f LEFT JOIN aircraft a ON a.id=f.aircraft_id
                                 WHERE f.leg_id=? AND f.state<>'CANCELADO'""", (nxt["id"],)))
        out.update(kind="perna", next=nxt, turn=turn, next_flight=nf)
        return out
    # último voo do dia: fim da jornada
    first = fl[0] if fl else dict(leg)
    duty = d.get("duty") or {}
    report = duty.get("report_utc") or (_iso(_p(first["dep_utc"]) - timedelta(minutes=int(rb))) if rb is not None else None)
    release = duty.get("release_utc") or _iso(_p(leg["arr_utc"]) + timedelta(minutes=RELEASE_AFTER_MIN))
    duty_min = int((_p(release) - _p(report)).total_seconds() // 60) if report and release else None
    nd = d.get("next_duty")
    nrep = None
    if nd:
        nrep = nd.get("report_utc") or (_iso(_p(nd["dep_utc"]) - timedelta(minutes=int(rb))) if rb is not None else nd["dep_utc"])
    out.update(report_utc=report, release_utc=release, release_src="escala" if duty.get("release_utc") else "art. 35 §4",
               duty_min=duty_min, min_rest=min_rest(duty_min), next_duty=nd, next_report_utc=nrep,
               rest_min=int((_p(nrep) - _p(release)).total_seconds() // 60) if nrep and release else None)
    # com a chegada estimada de verdade (só quando você voa no horário da escala)
    if eta_utc and not out["off_schedule"] and abs((_p(eta_utc) - _p(leg["arr_utc"])).total_seconds()) >= 15 * 60:
        rr = _p(eta_utc) + timedelta(minutes=RELEASE_AFTER_MIN)
        out["real"] = {"eta_utc": eta_utc, "release_utc": _iso(rr),
                       "duty_min": int((rr - _p(report)).total_seconds() // 60) if report else None,
                       "rest_min": int((_p(nrep) - rr).total_seconds() // 60) if nrep else None}
    if d.get("overnight"):
        out["kind"] = "base" if (base and f["ades"] == base) or d["overnight"].get("at_base") else "pernoite"
    elif nd and nd.get("at", {}).get("icao") != f["ades"]:
        out["kind"] = "fim_jornada"
    elif nd:
        out["kind"] = "pernoite"
    else:
        out["kind"] = "fim_escala"
    return out


def _colleagues(conn, f) -> list[str]:
    """Outros voos da companhia indo para o mesmo destino na IVAO, pelo último whazzup lido (não busca de novo)."""
    from . import ivao
    data = ivao._cache.get("data")
    if not data:
        return []
    vid = ivao.vid_of(conn)
    out = []
    for p in ivao.pilots(data):
        cs = str(p.get("callsign") or "").upper()
        if not cs.startswith((f.get("airline") or "GLO").upper()) or str(p.get("userId")) == str(vid or ""):
            continue
        if str((p.get("flightPlan") or {}).get("arrivalId") or "").upper() == f["ades"]:
            out.append(cs)
    return out[:6]


def _meals(conn, icao, a_iso, b_iso) -> list[str]:
    from .aeroscale.airports import get_tz
    from .payroll import MEALS
    tzn = rules.airport_tz(conn, icao)
    tz = get_tz(tzn) if tzn else None
    if not tz or not a_iso or not b_iso:
        return []
    a, b = _p(a_iso), _p(b_iso)
    out, day = [], a.astimezone(tz).date()
    while day <= b.astimezone(tz).date():
        for _k, label, s, e in MEALS:
            ws = datetime.combine(day, s, tzinfo=tz).astimezone(timezone.utc)
            we = datetime.combine(day, e, tzinfo=tz).astimezone(timezone.utc)
            if a <= we and b >= ws:
                out.append(f"{label} {s:%H:%M}–{e:%H:%M}")
        day += timedelta(days=1)
    return out


def _sun(conn, icao, a_iso, b_iso) -> str | None:
    pl = journey.place(conn, icao)
    if pl["lat"] is None or not a_iso:
        return None
    a = _p(a_iso)
    hours = max(6, min(40, int(((_p(b_iso) - a).total_seconds() / 3600) if b_iso else 24) + 1))
    ev = sun_events(pl["lat"], pl["lon"], a, hours)
    bits = []
    for kind, t in ev[:2]:
        bits.append(("pôr do sol " if kind == "poente" else "nascer do sol ") + _lt(conn, _iso(t), icao))
    return " · ".join(bits) or None


def plan_text(conn, f: dict, plan: dict, *, during=True, now=None) -> tuple[str, str, str] | None:
    """(assunto, corpo, nível) da mensagem do Controle de Tripulantes."""
    fn, ades = f["flight_number"] or "Voo", f["ades"]
    pl = plan.get("at") or journey.place(conn, ades)
    city = pl.get("city") or ades
    w = equipe.who(conn, "tripulantes", now)
    hi = f"Comandante, {w['name'].split()[0]} do Controle de Tripulantes."
    sched_note = ("\nVocê está voando fora do horário da escala: os horários abaixo são os programados." if plan.get("off_schedule") else "")
    k = plan["kind"]
    if k == "sem_escala":
        return None
    if k == "perna":
        n, t, nf = plan["next"], plan.get("turn") or {}, plan.get("next_flight")
        lines = [hi + (" Depois do pouso:" if during else f" Depois do {fn}:") + sched_note, "",
                 f"PRÓXIMA PERNA: {n['flight_number'] or '—'} {n['adep']}→{n['ades']}, partida {n['dep_utc'][11:16]}Z ({_lt(conn, n['dep_utc'], n['adep'])})"]
        if t.get("same_airport") is False:
            lines.append(f"ATENÇÃO: a próxima perna sai de {n['adep']}, não de {ades}. A escala não mostra como você chega lá.")
        elif t.get("minutes") is not None:
            lines.append(f"SOLO: {_hm(t['minutes'])} programados em {ades}" + (f" (mínimo de solo da escala: {t['min_turn']} min)" if t.get("min_turn") else "")
                         + (" · ABAIXO do mínimo da escala" if t.get("below_min_turn") else ""))
        same_ac = nf and nf["registration"] and f.get("registration") and nf["registration"] == f["registration"]
        lines.append("AERONAVE: " + (f"segue a {f['registration']}" if same_ac else f"troca para a {nf['registration']}" if nf and nf["registration"]
                                     else "ainda sem reserva no LineOps" if not nf else "reservada sem aeronave da frota"))
        if nf and nf["state"] == "RESERVADO" and not conn.execute("SELECT 1 FROM ofp WHERE flight_id=?", (nf["id"],)).fetchone():
            lines.append("DESPACHO: o DOV começa o despacho da próxima 3 h antes e vai pedir o OFP.")
        return (f"{fn}: próxima perna {n['flight_number'] or n['ades']}", "\n".join(lines), "info")
    rel, rep = plan.get("release_utc"), plan.get("next_report_utc")
    nd = plan.get("next_duty")
    if k == "base":
        subj = f"{fn}: fim da jornada na base"
        lines = [hi + " Fim da jornada em casa." + sched_note, ""]
    elif k == "fim_jornada":
        subj = f"{fn}: fim da jornada em {ades}"
        lines = [hi + f" Fim da jornada em {city}." + sched_note, ""]
    elif k == "fim_escala":
        subj = f"{fn}: fim da escala publicada"
        lines = [hi + f" Este é o último voo da escala publicada. Depois do pouso em {city}, nada programado ainda." + sched_note, ""]
    else:
        subj = f"{fn}: pernoite em {city}"
        lines = [hi + f" Seguem as informações do seu pernoite em {city} ({ades})." + sched_note, ""]
    lv = "info"
    leg = plan["leg"]
    lines.append(f"CHEGADA: {leg['arr_utc'][11:16]}Z ({_lt(conn, leg['arr_utc'], ades)}) programada"
                 + (f" · estimada {plan['real']['eta_utc'][11:16]}Z" if plan.get("real") else ""))
    if rel:
        lines.append(f"FIM DA JORNADA: {_lt(conn, rel, ades)}" + (" (30 min após o corte, art. 35 §4)" if plan.get("release_src") != "escala" else " (parâmetro da sua escala)")
                     + (f" · jornada de {_hm(plan['duty_min'])}" if plan.get("duty_min") else ""))
    if plan.get("min_rest") and k != "fim_escala":
        free = _iso(_p(rel) + timedelta(minutes=plan["min_rest"])) if rel else None
        lines.append(f"REPOUSO MÍNIMO: {plan['min_rest'] // 60} h (art. 48, {_rest_rule(plan['duty_min'])})" + (f" → disponível a partir de {_lt(conn, free, ades, True)}" if free else ""))
    if nd:
        lines.append(f"PRÓXIMA APRESENTAÇÃO: {_lt(conn, rep, nd['at']['icao'], True)} em {nd['at']['icao']} · {nd.get('flight_number') or '—'} "
                     f"{nd['at']['icao']}→{(nd.get('to') or {}).get('icao') or '?'} às {_lt(conn, nd['dep_utc'], nd['at']['icao'])}")
        if plan.get("rest_min") is not None and plan.get("min_rest"):
            ok = plan["rest_min"] >= plan["min_rest"]
            lines.append(f"REPOUSO PROGRAMADO: {_hm(plan['rest_min'])} · " + ("acima do mínimo legal" if ok else
                         f"ABAIXO do mínimo de {plan['min_rest'] // 60} h do art. 48. Confira com o Planejamento de Escala."))
            lv = "info" if ok else "alerta"
        if plan.get("real") and plan["real"].get("rest_min") is not None and plan.get("min_rest"):
            r = plan["real"]
            lines.append(f"COM A CHEGADA ESTIMADA: jornada termina {_lt(conn, r['release_utc'], ades)}, repouso de {_hm(r['rest_min'])}"
                         + ("" if r["rest_min"] >= plan["min_rest"] else " · abaixo do mínimo: aviso o Planejamento"))
            if r["rest_min"] < plan["min_rest"]:
                lv = "alerta"
    if k == "pernoite":
        from . import stay
        info = airport_info(conn, ades)
        tl = stay.timeline(conn, f, plan)
        if tl:
            lines += stay.lines_for_plan(conn, f, plan, tl)
            if tl["short"]:
                lv = "alerta"
        if info and info.get("notes"):
            lines.append(f"OBSERVAÇÕES: {info['notes']}")
        meals = _meals(conn, ades, rel, rep)
        if meals:
            lines.append("REFEIÇÕES NO PERNOITE: " + " · ".join(meals) + " (janelas da CCT, cl. 2.3)")
    if k in ("pernoite", "base", "fim_jornada"):
        sun = _sun(conn, ades, rel or leg["arr_utc"], rep)
        if sun:
            lines.append(f"SOL EM {ades}: {sun}")
    if k == "pernoite":
        col = _colleagues(conn, f)
        if col:
            lines.append(f"NA REDE: também a caminho de {ades} na IVAO agora: {', '.join(col)}")
    lines += ["", "Bom pernoite!" if k == "pernoite" else "Bom descanso!" if k in ("base", "fim_jornada") else "Bom voo!"]
    return subj, "\n".join(lines), lv


# ------------------------------------------------------------------ tempo no destino
def _metar_row(conn, icao):
    return one(conn.execute("SELECT * FROM weather_cache WHERE kind='metar' AND icao=?", (icao,)))


def _fresh(conn, icao, now) -> bool:
    r = _metar_row(conn, icao)
    if not r or not r["fetched_at"]:
        return False
    try:
        at = datetime.fromisoformat(r["fetched_at"].replace("Z", "+00:00"))
        at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (_p(now) - at).total_seconds() < WX_FRESH_MIN * 60


def _metar_line(conn, icao) -> list[str]:
    from .aeroscale import weather as wx
    r = _metar_row(conn, icao)
    if not r or not r["raw"]:
        return [f"METAR {icao}: sem leitura (o servidor do LineOps está sem internet?). Ouça o ATIS na chegada."]
    d = wx.decode_metar(r["raw"])
    bits = []
    wd = d.get("wind") or {}
    if wd.get("calm"):
        bits.append("vento calmo")
    elif wd:
        bits.append(("vento variável" if wd.get("variable") or wd.get("dir") is None else f"vento {int(wd['dir']):03d}°") + f" {wd.get('speed_kt')} kt"
                    + (f" rajada {wd['gust_kt']}" if wd.get("gust_kt") else ""))
    if d.get("cavok"):
        bits.append("CAVOK")
    elif d.get("visibility"):
        bits.append(f"visibilidade {d['visibility'].get('text')}")
    bits += [x["text"] for x in d.get("weather") or []]
    ceil = [c for c in d.get("clouds") or [] if c.get("cover") in ("BKN", "OVC") and c.get("base_ft") is not None]
    if ceil:
        bits.append(f"teto {min(c['base_ft'] for c in ceil)} ft")
    elif d.get("vertical_visibility_ft"):
        bits.append(f"visibilidade vertical {d['vertical_visibility_ft']} ft")
    if d.get("temp_c") is not None:
        bits.append(f"{d['temp_c']}/{d.get('dewpoint_c')} °C")
    if d.get("qnh_hpa"):
        bits.append(f"QNH {d['qnh_hpa']}")
    cat = wx.flight_category(d)
    out = [f"METAR {r['raw']}", "  " + " · ".join(str(b) for b in bits if b) + (f" · {cat}" if cat else "")]
    al = [a["text"] for a in wx.metar_alerts(d)]
    if al:
        out.append("  ATENÇÃO: " + "; ".join(al))
    return out


def wx_text(conn, f, now) -> tuple[str, str, str]:
    o = ofp_mod.latest(conn, f["id"])
    altn = (o or {}).get("data", {}).get("altn") if o else None
    w = equipe.who(conn, "despacho", now)
    lines = [f"Comandante, {w['name'].split()[0]} do despacho. Tempo atualizado para a sua chegada em {f['ades']}:", ""]
    lines += _metar_line(conn, f["ades"])
    t = one(conn.execute("SELECT raw FROM weather_cache WHERE kind='taf' AND icao=?", (f["ades"],)))
    if t and t["raw"]:
        lines.append(f"TAF {t['raw']}")
    if altn and altn != f["ades"]:
        lines.append("")
        lines.append(f"ALTERNADO {altn}:")
        lines += _metar_line(conn, altn)
    lines += ["", "Fonte: aviationweather.gov (NOAA), lido pelo servidor do LineOps. Confirme no ATIS."]
    from .aeroscale import weather as wx
    r = _metar_row(conn, f["ades"])
    lv = "info"
    if r and r["raw"]:
        d = wx.decode_metar(r["raw"])
        if wx.flight_category(d) in ("IFR", "LIFR") or any(a["level"] == "warning" for a in wx.metar_alerts(d)):
            lv = "alerta"
    return f"{f['flight_number'] or 'Voo'}: tempo em {f['ades']}", "\n".join(lines), lv


# ------------------------------------------------------------------ MCC e cabine
def mcc_text(conn, f, now) -> tuple[str, str, str] | None:
    if not f.get("aircraft_id"):
        return None
    reg = f.get("registration") or "aeronave"
    nxt = one(conn.execute("""SELECT id, flight_number, adep, ades, dep_utc FROM flight WHERE aircraft_id=? AND id<>? AND state IN ('RESERVADO','EM_VOO')
                              AND COALESCE(dep_utc,'') >= COALESCE(?, '') ORDER BY dep_utc LIMIT 1""", (f["aircraft_id"], f["id"], f.get("dep_utc"))))
    w = equipe.who(conn, "mcc", now)
    lines = [f"Comandante, {w['name'].split()[0]} do MCC. Sobre a {reg} depois do pouso em {f['ades']}:", ""]
    if nxt and nxt["adep"] == f["ades"]:
        lines.append(f"PRÓXIMO VOO DA AERONAVE: {nxt['flight_number'] or '—'} {nxt['adep']}→{nxt['ades']} às {(nxt['dep_utc'] or '')[11:16]}Z")
    elif nxt:
        lines.append(f"PRÓXIMO VOO DA AERONAVE: {nxt['flight_number'] or '—'} saindo de {nxt['adep']} (não de {f['ades']}): alguém vai ter que levá-la.")
    else:
        lines.append(f"PRÓXIMO VOO DA AERONAVE: nenhum no LineOps. A {reg} fica em {f['ades']}.")
    av = fleet.availability(conn, f["aircraft_id"], now)
    if av["restrictions"]:
        lines.append("ITENS ABERTOS: " + "; ".join(f"{r['title']} ({r['restriction'].lower()})" for r in av["restrictions"]))
    else:
        lines.append("ITENS ABERTOS: nenhum. Se algo falhar no voo, anote no PIREP que eu abro o item.")
    lines.append("(MEL simulado do LineOps)")
    return f"{reg}: depois do pouso em {f['ades']}", "\n".join(lines), "info"


def cabin_text(conn, f, now, plan, chief_name=None) -> tuple[str, str, str]:
    w = {"name": chief_name} if chief_name else equipe.who(conn, "cabine", now)
    o = ofp_mod.latest(conn, f["id"])
    pax = (o or {}).get("data", {}).get("pax_count") if o else None
    lines = [f"Comandante, {w['name'].split()[0]} na cabine. Começando o serviço de bordo"
             + (f", {int(pax)} passageiros a bordo pelo OFP." if pax else ".")]
    rep = plan.get("report_utc") if plan else None
    day = (plan or {}).get("day") or {}
    rep = rep or (day.get("duty") or {}).get("report_utc")
    if rep and (_p(now) - _p(rep)).total_seconds() >= 3.5 * 3600:
        h_ = (_p(now) - _p(rep)).total_seconds() / 3600
        lines.append(f"Já são {_hm(h_ * 60)} desde a apresentação: mando a refeição da tripulação agora "
                     "(em voo, a alimentação tem intervalo de no máximo 4 h, Lei 13.475, art. 61 §3).")
    else:
        lines.append("Quer um café aí na frente? É só chamar no interfone.")
    return "Cabine: serviço de bordo", "\n".join(lines), "info"


# ------------------------------------------------------------------ motor
def _thresholds(prog):
    fr, el = prog["frac"], prog["elapsed_min"] or 0
    tg = prog["to_go_min"]
    return {
        "cab": (fr is not None and fr >= 0.12) or el >= 12,
        "plan": (fr is not None and fr >= 0.25) or (fr is None and el >= 20),
        "wx": (fr is not None and fr >= 0.55) or (tg is not None and tg <= 40) or (fr is None and el >= 35),
        "mcc": (fr is not None and fr >= 0.7) or (fr is None and el >= 45),
    }


def _sessions(conn, now):
    since = _iso(_p(now) - timedelta(hours=3))
    return rows(conn.execute("""SELECT s.id, s.flight_id, s.status, s.phase, s.snapshot, s.last_seen_at FROM acars_session s
                                WHERE s.status='LIVE' OR (s.status='COMPLETED' AND s.last_seen_at >= ?) ORDER BY s.id""", (since,)))


def _flight(conn, fid):
    return one(conn.execute("""SELECT f.*, a.registration FROM flight f LEFT JOIN aircraft a ON a.id=f.aircraft_id WHERE f.id=?""", (fid,)))


def _has(conn, key) -> bool:
    return bool(conn.execute("SELECT 1 FROM ops_message WHERE key=?", (key,)).fetchone())


def process(conn, now: str | None = None, *, wx_tried=False) -> list[str]:
    """Uma passada (dentro de transação). Devolve os aeroportos cujo METAR precisa ser lido antes da mensagem do DOV."""
    now = now or now_iso()
    need = []
    for s in _sessions(conn, now):
        f = _flight(conn, s["flight_id"])
        if not f:
            continue
        st = flight_state(conn, s)
        fid = f["id"]
        link = [{"label": "Flight Center", "href": f"#/voo/{fid}"}]
        if s["status"] == "LIVE" and st["airborne"]:
            prog = progress(conn, f, st, now)
            th = _thresholds(prog)
            plan = None
            if th["cab"] or th["plan"]:
                plan = plan_after(conn, f, prog["eta_utc"], st["off_utc"])
            if th["cab"] and not _has(conn, f"er_cab:{fid}"):
                from . import tripulacao
                crew = tripulacao.for_flight(conn, f)
                chief = (crew or {}).get("chief")
                subj, body, lv = cabin_text(conn, f, now, plan, chief["name"] if chief else None)
                equipe.post(conn, f"er_cab:{fid}", "cabine", subj, body, flight_id=fid, level=lv, now=now, channel="acars",
                            sender=chief["name"] if chief else None, sender_title="Chefe de cabine" if chief else None)
            if th["plan"] and not _has(conn, f"er_plan:{fid}"):
                t = plan_text(conn, f, plan, now=now)
                if t:
                    equipe.post(conn, f"er_plan:{fid}", "tripulantes", t[0], t[1], flight_id=fid, level=t[2], now=now, channel="acars",
                                links=[{"label": "Ver o dia", "href": f"#/escala/{plan['leg']['local_date']}"}] if plan.get("leg") else link)
                    from . import stay
                    stay.schedule(conn, f, plan, now)            # carro, check-in, despertar e saída ficam agendados
            if th["wx"] and not _has(conn, f"er_wx:{fid}"):
                o = ofp_mod.latest(conn, fid)
                icaos = [x for x in (f["ades"], (o or {}).get("data", {}).get("altn") if o else None) if x]
                if wx_tried or all(_fresh(conn, i, now) for i in icaos):
                    subj, body, lv = wx_text(conn, f, now)
                    equipe.post(conn, f"er_wx:{fid}", "despacho", subj, body, flight_id=fid, level=lv, now=now, channel="acars", links=link)
                else:
                    need += [i for i in icaos if i not in need]
            if th["mcc"] and not _has(conn, f"er_mcc:{fid}"):
                t = mcc_text(conn, f, now)
                if t:
                    equipe.post(conn, f"er_mcc:{fid}", "mcc", t[0], t[1], flight_id=fid, level=t[2], now=now, channel="acars",
                                links=[{"label": "Ficha da aeronave", "href": f"#/frota/{f['aircraft_id']}"}])
        elif st["landed"] and (st["in_utc"] or s["status"] != "LIVE") and _has(conn, f"er_plan:{fid}") and not _has(conn, f"er_arr:{fid}"):
            # espera o calço (IN): é dele que conta o fim da jornada
            plan = plan_after(conn, f, None, st["off_utc"])
            if plan["kind"] == "pernoite" and not plan.get("off_schedule"):
                from . import stay
                stay.schedule(conn, f, plan, now, in_utc=st.get("in_utc") or st.get("on_utc"))   # reagenda pelo calço real
            if plan["kind"] in ("pernoite", "base", "fim_jornada"):
                arr_text(conn, f, plan, st, now)
    return need


def arr_text(conn, f, plan, st, now):
    """Depois do pouso: quando o repouso começa de verdade (corte + 30 min) e o transporte."""
    ades = f["ades"]
    city = (plan.get("at") or {}).get("city") or ades
    cut = st.get("in_utc") or st.get("on_utc")
    w = equipe.who(conn, "tripulantes", now)
    lines = [f"Bem-vindo a {city}, comandante. Aqui é {w['name'].split()[0]}, do Controle de Tripulantes.", ""]
    if cut and not plan.get("off_schedule"):
        rest_from = _iso(_p(cut) + timedelta(minutes=RELEASE_AFTER_MIN))
        lines.append(f"SEU REPOUSO CONTA A PARTIR DE: {_lt(conn, rest_from, ades)} (30 min após o corte, art. 35 §4)"
                     + (" ou de quando o transporte chegar, se atrasar (art. 47 §4)." if plan["kind"] == "pernoite" else "."))
        if plan.get("next_report_utc") and plan.get("min_rest"):
            rest = int((_p(plan["next_report_utc"]) - _p(rest_from)).total_seconds() // 60)
            lines.append(f"ATÉ A APRESENTAÇÃO: {_hm(rest)}" + ("" if rest >= plan["min_rest"] else
                         f" · ABAIXO das {plan['min_rest'] // 60} h do art. 48: o Planejamento precisa reprogramar a apresentação."))
    if plan["kind"] == "pernoite":
        from . import stay
        info = airport_info(conn, ades) or {}
        tl = stay.timeline(conn, f, plan, None if plan.get("off_schedule") else cut)
        if tl:
            r = stay.ride(f["id"], tl["transport"])
            lines.append(("SUA VAN: " if tl["transport"] == "van" else "SEU CARRO: ") + f"{stay.ride_text(r)}, no desembarque às {_lt(conn, tl['car_utc'], ades)}")
            lines.append(f"HOTEL: {info.get('hotel') or 'não escolhido'} · {tl['drive']['label']}")
            if tl.get("wake_utc"):
                lines.append(f"DESPERTAR: {_lt(conn, tl['wake_utc'], ades, True)} · eu aviso por aqui")
    nd = plan.get("next_duty")
    if nd:
        lines.append(f"APRESENTAÇÃO: {_lt(conn, plan.get('next_report_utc'), nd['at']['icao'], True)} em {nd['at']['icao']}")
    lines += ["", "Bom descanso!"]
    equipe.post(conn, f"er_arr:{f['id']}", "tripulantes", f"{f['flight_number'] or 'Voo'}: bem-vindo a {city}", "\n".join(lines),
                flight_id=f["id"], now=now)


def after_declared(conn, f, e, now):
    """Voo declarado (“Já fiz esse voo”): o pacote do que vem depois chega logo após o lançamento, fora do canal ACARS."""
    try:
        fr = _flight(conn, f["id"])
        if not fr or _has(conn, f"er_plan:{fr['id']}"):
            return
        basis = (e.get("time_basis") if hasattr(e, "get") else None) or "REAL"
        real_off = (e["off_utc"] or e["out_utc"]) if basis == "REAL" else None      # registrado no horário da escala: está no horário
        plan = plan_after(conn, fr, None, real_off)
        t = plan_text(conn, fr, plan, during=False, now=now)
        if t:
            equipe.post(conn, f"er_plan:{fr['id']}", "tripulantes", t[0], t[1], flight_id=fr["id"], level=t[2], now=now,
                        links=[{"label": "Ver o dia", "href": f"#/escala/{plan['leg']['local_date']}"}] if plan.get("leg") else [])
            from . import stay
            stay.schedule(conn, fr, plan, now, in_utc=e["in_utc"] if basis == "REAL" else None)
    except Exception:
        log.exception("pernoite do voo declarado")


def refresh_weather(conn, icaos: list[str], fetch=None) -> None:
    """Fora de transação. Sem internet, a mensagem do DOV sai dizendo que não conseguiu ler."""
    if fetch is None and os.environ.get("LINEOPS_OFFLINE") == "1":
        return
    try:
        (fetch or (lambda c, i: rules.weather(c, i)))(conn, icaos)
    except Exception:
        log.warning("METAR/TAF do destino indisponível agora", exc_info=True)
