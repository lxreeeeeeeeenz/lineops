"""Escala em texto, como circula entre tripulantes: o exportador do AeroScale (scheduler.export_txt), lendo a escala que
está na sua carreira agora (com trocas, cortes e escalas refeitas), e não a escala original do motor.

    ESCALA DE NOVEMBRO:
    (01) GRU-REC-GRU (1554,1555)
    (02) FR
    (03) RESERVA GRU
    (04) SOBREAVISO

O algoritmo é o do AeroScale 2.2.4, linha por linha (dia da jornada com +1, extra/DHD, quebra de trecho com " / ",
reserva acionada, bloco de reservas no fim). As siglas de folga, reserva, sobreaviso, inativo e extra são as do perfil da
companhia (crew_profiles.codes). A única diferença: dia que nenhuma escala cobre (depois de apagar ou de cortar a escala)
sai como "SEM ESCALA", em vez de virar folga.
"""
from __future__ import annotations

from datetime import date, timedelta

from . import journey, rules
from .aeroscale import crew_profiles, reserve as as_reserve
from .aeroscale.airports import to_local
from .aeroscale.scheduler import MESES, _flight_digits
from .db import jloads, rows
from .errors import FGVError

NO_SCHEDULE = "SEM ESCALA"


def _schedules(conn) -> list[dict]:
    """Escalas vivas (não apagadas), da mais nova para a mais antiga."""
    out = []
    for s in rows(conn.execute("SELECT id, airline, start_date, end_date, params FROM roster_schedule ORDER BY id DESC")):
        p = jloads(s["params"], {})
        if p.get("apagada") or not s["start_date"] or not s["end_date"]:
            continue
        out.append({**s, "params": p})
    return out


def _owner(scheds: list[dict], ds: str) -> dict | None:
    return next((s for s in scheds if s["start_date"] <= ds <= s["end_date"]), None)


def _gather(conn, start: str, end: str) -> dict:
    """A escala da carreira no formato que o export_txt do AeroScale lê (serialize_schedule)."""
    from . import escala
    legs = escala.legs(conn, start, end)
    scheds = _schedules(conn)
    res = rows(conn.execute("SELECT * FROM roster_reserve WHERE start_date<=? AND end_date>=? ORDER BY start_date", (end, start)))
    by_uid = {}
    for r in res:
        a = rules.airport(conn, r["location"]) or {}
        r.update({"location_iata": a.get("iata") or r["location"], "status_label": as_reserve.STATUS_LABEL.get(r["status"], r["status"]),
                  "days": r["days"] or (date.fromisoformat(r["end_date"]) - date.fromisoformat(r["start_date"])).days + 1})
        by_uid[r["uid"]] = r["id"]
    for l in legs:
        l["reserve_id"] = by_uid.get(l.get("reserve_uid"))
    # sobreaviso, inativo e reserva importada: só o da escala que hoje é dona do dia (a mais nova que o cobre)
    days = []
    for d in rows(conn.execute("SELECT * FROM roster_day WHERE date BETWEEN ? AND ? ORDER BY date", (start, end))):
        own = _owner(scheds, d["date"])
        if own and own["id"] == d["schedule_id"]:
            a = rules.airport(conn, d["location"]) or {}
            days.append({**d, "location_iata": a.get("iata") or d["location"] or ""})
    airline = (legs[0]["airline"] if legs else None) or (scheds[0]["airline"] if scheds else None)
    covered = {l["local_date"] for l in legs}
    x, y = date.fromisoformat(start), date.fromisoformat(end)
    while x <= y:
        if _owner(scheds, x.isoformat()):
            covered.add(x.isoformat())
        x += timedelta(days=1)
    return {"airline": airline, "legs": legs, "reserves": res, "days": days, "covered": covered}


def roster_text(conn, start: str, end: str, style: str | None = None, times: bool = False) -> dict:
    try:
        d_a, d_b = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError):
        raise FGVError("Período inválido: use AAAA-MM-DD.", code="campo_invalido")
    if d_b < d_a or (d_b - d_a).days > 400:
        raise FGVError("Período inválido (no máximo 400 dias).", code="campo_invalido")
    s = _gather(conn, start, end)
    if not s["covered"]:
        return {"text": "", "start": start, "end": end, "style": style or "companhia", "empty": True,
                "message": "Não há escala nesse período."}
    style = style if style in ("companhia", "classico") else "companhia"      # na LineOps toda escala é de tripulante
    code = lambda icao: (rules.airport(conn, icao) or {}).get("iata") or icao  # noqa: E731
    tz = lambda icao: rules.airport_tz(conn, icao)                              # noqa: E731
    parse = journey.parse
    local_day = lambda iso_utc, icao: to_local(parse(iso_utc), tz(icao)).date().isoformat()  # noqa: E731
    cod = crew_profiles.codes(s["airline"]) if style == "companhia" else {
        "off_code": "FOLGA", "reserve_code": "RESERVA", "sobreaviso_code": "SOBREAVISO", "inactive_code": "INATIVO EM",
        "dhd_style": "prefix"}
    by_day: dict[str, list] = {}
    if style == "companhia":
        # como na escala real: o dia é o da JORNADA (voo depois da meia-noite fica no dia em que começou, com +1)
        cur_day, prev = None, None
        for l in sorted(s["legs"], key=lambda x: x["dep_utc"]):
            dep = parse(l["dep_utc"])
            loc_dep = to_local(dep, tz(l["adep"]))
            gap_h = (dep - parse(prev["arr_utc"])).total_seconds() / 3600 if prev else 99
            if cur_day is None or gap_h >= 10 or (l["local_date"] != cur_day and loc_dep.hour >= 5):
                cur_day = l["local_date"]
            by_day.setdefault(cur_day, []).append(l)
            prev = l
    else:
        for l in s["legs"]:
            by_day.setdefault(l["local_date"], []).append(l)
    days_kind = {d["date"]: d for d in s["days"]}
    cov = sorted(s["covered"] | set(by_day))
    d1 = max(d_a, date.fromisoformat(cov[0]))
    d2 = min(d_b, date.fromisoformat(cov[-1]))
    reserves = s["reserves"]

    def res_of(day: date):
        return next((r for r in reserves if r["start_date"] <= day.isoformat() <= r["end_date"]), None)

    def num(l):
        n = _flight_digits(l["flight_number"])
        if l.get("activity") == "EXTRA":
            return f"{n} DHD" if cod["dhd_style"] == "suffix" else f"DH{n}"
        return n

    def seg_txt(legs):
        route = [code(legs[0]["adep"])]
        for a, b in zip([None] + legs, legs):
            if a is not None and b["adep"] != a["ades"]:
                route.append("/ " + code(b["adep"]))  # quebra de continuidade no mesmo dia
            route.append(code(b["ades"]))
        return "-".join(route).replace("-/ ", " / ")

    def legs_txt(legs, ds=None):
        plus = ""
        if style == "companhia" and ds and any(l["arr_utc"] and local_day(l["arr_utc"], l["ades"]) > ds for l in legs):
            plus = " +1"
        if style == "companhia" and all(l.get("activity") == "EXTRA" for l in legs) and cod["dhd_style"] == "suffix":
            txt = "DHD " + ",".join(_flight_digits(l["flight_number"]) for l in legs)
        elif style == "companhia" and any(l.get("activity") == "EXTRA" for l in legs) and \
                not all(l.get("activity") == "EXTRA" for l in legs):
            # extra e voo no mesmo dia: um trecho por atividade, separados por " / "
            parts, cur = [], [legs[0]]
            for l in legs[1:]:
                if (l.get("activity") == "EXTRA") != (cur[-1].get("activity") == "EXTRA"):
                    parts.append(cur)
                    cur = []
                cur.append(l)
            parts.append(cur)
            txt = " / ".join(f"{seg_txt(x)} ({','.join(num(l) for l in x)})" for x in parts)
            txt += plus
        else:
            txt = f"{seg_txt(legs)}{plus} ({','.join(num(l) for l in legs)})"
        if times:
            a = to_local(parse(legs[0]["dep_utc"]), tz(legs[0]["adep"]))
            b = to_local(parse(legs[-1]["arr_utc"]), tz(legs[-1]["ades"]))
            txt += f" · {a:%H:%M}–{b:%H:%M}"
        return txt

    lines: list[str] = []
    month = None
    d = d1
    while d <= d2:
        if (d.year, d.month) != month:
            month = (d.year, d.month)
            lines.append(f"ESCALA DE {MESES[d.month - 1]}{f' {d.year}' if d.year != d1.year else ''}:")
        ds = d.isoformat()
        legs = sorted(by_day.get(ds, []), key=lambda l: l["dep_utc"])
        r = res_of(d)
        sp = days_kind.get(ds)
        if r:
            act = [l for l in legs if l.get("activity") == "RESERVA"]
            lines.append(f"({d.day:02d}) {cod['reserve_code']} {r['location_iata']}" + (f" · ACIONADO: {legs_txt(act)}" if act else ""))
        elif sp:
            label = {"sobreaviso": cod["sobreaviso_code"], "inativo": f"{cod['inactive_code']} {sp['location_iata']}",
                     "reserva": f"{cod['reserve_code']} {sp['location_iata']}"}.get(sp["kind"], sp["kind"].upper())
            lines.append(f"({d.day:02d}) {label}" + (f" · ACIONADO: {legs_txt(legs, ds)}" if legs else ""))
        elif not legs:
            lines.append(f"({d.day:02d}) {cod['off_code'] if ds in s['covered'] else NO_SCHEDULE}")
        else:
            lines.append(f"({d.day:02d}) {legs_txt(legs, ds)}")
        d += timedelta(days=1)
    for r in reserves:
        a, b = date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"])
        loc = f"Base {r['location_iata']}" if r["location_kind"] == "base" else f"{r['location_iata']} (fora da base)"
        status = {"acionado": "SIMULADO", "sem_acionamento": "NÃO ACIONADO",
                  "inviavel": "INVIÁVEL (sem sequência de voos compatível no RPL)"}.get(r["status"], r["status_label"])
        lines += ["", f"RESERVA: {a:%d/%m} a {b:%d/%m} ({r['days']} dias)", f"Local: {loc}", f"Acionamento: {status}"]
        act_days = sorted({l["local_date"] for l in s["legs"] if l.get("reserve_id") == r["id"]})
        if act_days:
            lines += ["", "ACIONAMENTO RESERVAS"]
            for ds in act_days:
                dl = sorted([l for l in s["legs"] if l.get("reserve_id") == r["id"] and l["local_date"] == ds],
                            key=lambda l: l["dep_utc"])
                lines.append(f"({ds[8:10]}) {legs_txt(dl)}")
    return {"text": "\n".join(lines) + "\n", "start": d1.isoformat(), "end": d2.isoformat(), "style": style, "empty": False,
            "legs": len(s["legs"]), "airline": s["airline"]}


# ------------------------------------------------------------------ agenda (.ics)
def _ics(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545: linhas de até 75 octetos, continuação com espaço."""
    b = line.encode("utf-8")
    if len(b) <= 75:
        return line
    out, cur = [], b""
    for ch in line:
        e = ch.encode("utf-8")
        if len(cur) + len(e) > (75 if not out else 74):
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += e
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def roster_ics(conn, start: str, end: str, career_name: str | None = None) -> dict:
    """A escala da carreira para o Google Agenda, Outlook ou o calendário do celular: cada perna com o horário certo (UTC,
    a agenda mostra no seu fuso) e folga, reserva e sobreaviso como dias inteiros. UID estável por perna: importar de
    novo atualiza em vez de duplicar."""
    from datetime import datetime, timezone

    from . import escala, journey, visao
    try:
        d_a, d_b = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError):
        raise FGVError("Período inválido: use AAAA-MM-DD.", code="campo_invalido")
    if d_b < d_a or (d_b - d_a).days > 400:
        raise FGVError("Período inválido (no máximo 400 dias).", code="campo_invalido")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    z = lambda iso: iso.replace("-", "").replace(":", "")                              # noqa: E731
    name = f"Escala {career_name}" if career_name else "Escala LineOps"
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//LineOps//Escala de simulacao//PT-BR", "CALSCALE:GREGORIAN",
         "METHOD:PUBLISH", f"X-WR-CALNAME:{_ics(name)}"]
    n = 0
    for l in escala.legs(conn, start, end):
        dl, al = journey.local_time(conn, l["dep_utc"], l["adep"]), journey.local_time(conn, l["arr_utc"], l["ades"])
        kind = {"EXTRA": " (extra)", "SOBREAVISO": " (sobreaviso acionado)", "RESERVA": " (reserva acionada)"}.get(l["activity"] or "", "")
        title = f"{l['flight_number'] or 'Posicionamento'} {l['adep']}→{l['ades']}{kind}"
        desc = "\n".join(x for x in [
            f"Partida {dl['hhmm']} local ({l['dep_utc'][11:16]}Z) em {l['adep']}" if dl else f"Partida {l['dep_utc'][11:16]}Z",
            f"Chegada {al['hhmm']} local ({l['arr_utc'][11:16]}Z) em {l['ades']}" if al else f"Chegada {l['arr_utc'][11:16]}Z",
            f"Tipo: {l['type_icao']}" if l.get("type_icao") else None,
            f"Dado: {l['data_origin']}" + (" (padrão semanal do RPL, confira quando o RPL do dia sair)" if l["data_origin"] == "PROJETADO" else ""),
            "LineOps: escala de simulação, não é operação real."] if x)
        L += ["BEGIN:VEVENT", f"UID:{l['uid']}@lineops", f"DTSTAMP:{stamp}", f"DTSTART:{z(l['dep_utc'])}", f"DTEND:{z(l['arr_utc'])}",
              f"SUMMARY:{_ics(title)}", f"LOCATION:{_ics(l['adep'])}", f"DESCRIPTION:{_ics(desc)}", "END:VEVENT"]
        n += 1
    # dias inteiros: folga, reserva e sobreaviso (o mesmo que a tela do mês mostra)
    m, spans = date(d_a.year, d_a.month, 1), []
    while m <= d_b:
        for d in visao.month(conn, m.strftime("%Y-%m"))["days"]:
            if not (start <= d["date"] <= end) or d["kind"] not in ("FOLGA", "RESERVA", "SOBREAVISO"):
                continue
            label = {"FOLGA": "Folga", "RESERVA": f"Reserva {d.get('reserve_at') or ''}".strip(), "SOBREAVISO": "Sobreaviso"}[d["kind"]]
            if spans and spans[-1]["label"] == label and spans[-1]["to"] == (date.fromisoformat(d["date"]) - timedelta(days=1)).isoformat():
                spans[-1]["to"] = d["date"]
            else:
                spans.append({"label": label, "from": d["date"], "to": d["date"]})
        m = date(m.year + (m.month // 12), m.month % 12 + 1, 1)
    for s in spans:
        a, b = date.fromisoformat(s["from"]), date.fromisoformat(s["to"]) + timedelta(days=1)
        L += ["BEGIN:VEVENT", f"UID:dia-{s['from']}-{s['label'].split()[0].lower()}@lineops", f"DTSTAMP:{stamp}",
              f"DTSTART;VALUE=DATE:{a:%Y%m%d}", f"DTEND;VALUE=DATE:{b:%Y%m%d}", f"SUMMARY:{_ics(s['label'])}",
              "TRANSP:TRANSPARENT", "END:VEVENT"]
    L.append("END:VCALENDAR")
    return {"ics": "\r\n".join(_fold(x) for x in L) + "\r\n", "legs": n, "days": len(spans)}
