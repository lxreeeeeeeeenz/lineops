"""Jornada do piloto: dia, semana e Flight Center.

Tudo aqui é leitura e derivação do que o AeroScale entregou — nada é gerado nem completado por conta própria.
Quando um dado não existe (apresentação, pernoite, matrícula), o campo volta nulo e a interface diz que falta.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import fleet, flights, geo, loadsheet as ls_mod, ofp as ofp_mod, rules
from .db import jloads, now_iso, one, rows
from .errors import NotFound

FMT = "%Y-%m-%dT%H:%M:%SZ"
WEEKDAY = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
MONTH = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]
try:                                           # no Windows o zoneinfo depende do pacote tzdata
    from zoneinfo import ZoneInfo
except ImportError:                            # pragma: no cover
    ZoneInfo = None


GAP_LABEL = rules.GAP_LABEL                 # predict.GAP_LABEL do AeroScale
OVERNIGHT_KINDS = rules.OVERNIGHT_KINDS


def parse(s: str | None) -> datetime | None:
    try:
        return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc) if s else None
    except ValueError:
        return None


def iso(d: datetime | None) -> str | None:
    return d.astimezone(timezone.utc).strftime(FMT) if d else None


def local_time(conn, utc_iso: str | None, icao: str | None) -> dict | None:
    """Hora local no aeroporto, pelo fuso que o AeroScale conhece. Sem fuso, devolve None e a tela mostra só UTC."""
    return rules.local_label(conn, parse(utc_iso), icao)


def place(conn, icao: str | None) -> dict:
    ap = rules.airport(conn, icao) or {}
    return {"icao": icao, "city": ap.get("city"), "name": ap.get("name"), "iata": ap.get("iata"),
            "uf": ap.get("uf"), "country": ap.get("country"), "lat": ap.get("lat"), "lon": ap.get("lon"),
            "tz": ap.get("tz")}


def date_label(date: str) -> str:
    d = datetime.strptime(date, "%Y-%m-%d")
    return f"{WEEKDAY[d.weekday()]}, {d.day} de {MONTH[d.month - 1]} de {d.year}"


# ------------------------------------------------------------------ parâmetros da escala (do AeroScale)
def schedule_params(conn, date: str) -> tuple[dict, dict | None]:
    """Parâmetros da escala que cobre o dia (escalas apagadas em "apagar todas" não contam)."""
    alive = lambda q, a: next((r for r in rows(conn.execute(q, a)) if '"apagada"' not in (r["params"] or "")), None)   # noqa: E731
    s = alive("SELECT * FROM roster_schedule WHERE start_date<=? AND end_date>=? ORDER BY id DESC LIMIT 20", (date, date)) \
        or alive("SELECT * FROM roster_schedule ORDER BY id DESC LIMIT 20", ())
    return (jloads(s["params"], {}) if s else {}), s


def _duty_times(p: dict, first_dep: str | None, last_arr: str | None, airline: str | None = None) -> dict:
    """Apresentação e encerramento: parâmetros da sua escala no AeroScale; na falta, o perfil da companhia (crew_profiles)."""
    rb, ra = p.get("report_before"), p.get("release_after")
    if rb is None and airline:
        rb = rules.crew_profile(airline).get("report_min")
    out = {"report_utc": None, "release_utc": None, "source": None}
    if isinstance(rb, (int, float)) and first_dep:
        out["report_utc"] = iso(parse(first_dep) - timedelta(minutes=int(rb)))
    if isinstance(ra, (int, float)) and last_arr:
        out["release_utc"] = iso(parse(last_arr) + timedelta(minutes=int(ra)))
    if out["report_utc"] or out["release_utc"]:
        bits = [f"apresentação {int(rb)} min antes" if out["report_utc"] else "", f"encerramento {int(ra)} min depois" if out["release_utc"] else ""]
        out["source"] = "parâmetros da sua escala no AeroScale (" + ", ".join(b for b in bits if b) + ")"
    return out


# ------------------------------------------------------------------ pernas
def _leg_rows(conn, where: str, args) -> list[dict]:
    return rows(conn.execute(f"""SELECT l.*, f.id AS flight_id, f.state AS flight_state, f.aircraft_id,
                                 a.registration AS ac_registration, a.type_icao AS aircraft_type,
                                 (SELECT COUNT(*) FROM pirep p WHERE p.flight_id=f.id AND p.state='PENDING') AS pirep_pending
                                 FROM roster_leg l LEFT JOIN flight f ON f.leg_id=l.id AND f.state<>'CANCELADO'
                                 LEFT JOIN aircraft a ON a.id=f.aircraft_id
                                 WHERE l.removed_batch IS NULL AND {where} ORDER BY l.dep_utc, l.seq""", args))


def decorate_leg(conn, leg: dict) -> dict:
    leg["registration"] = leg.pop("ac_registration", None) or leg.get("registration")   # l.registration é da escala; a.registration é a frota
    leg["from"] = place(conn, leg["adep"])
    leg["to"] = place(conn, leg["ades"])
    leg["dep_local"] = local_time(conn, leg["dep_utc"], leg["adep"])
    leg["arr_local"] = local_time(conn, leg["arr_utc"], leg["ades"])
    leg["type_name"] = rules.type_name(leg.get("type_icao"))
    leg["is_flight"] = leg.get("activity") != "SOBREAVISO"
    leg["can_book"] = leg["is_flight"] and not leg.get("flight_id")
    d, a = parse(leg["dep_utc"]), parse(leg["arr_utc"])
    leg["block_min"] = int((a - d).total_seconds() // 60) if d and a else leg.get("eet_min")
    f, t = leg["from"], leg["to"]
    leg["distance_nm"] = round(geo.nm(f["lat"], f["lon"], t["lat"], t["lon"])) if f.get("lat") is not None and t.get("lat") is not None else None
    return leg


def legs_of_day(conn, date: str) -> list[dict]:
    return [decorate_leg(conn, l) for l in _leg_rows(conn, "COALESCE(l.local_date, substr(l.dep_utc,1,10))=?", (date,))]


def local_today(conn, now: str | None = None) -> str:
    """Hoje na base da escala (o AeroScale data a perna pela hora local da partida)."""
    now = now or now_iso()
    p, _s = schedule_params(conn, now[:10])
    base = p.get("base_resolved") or p.get("base")
    return rules.local_date(conn, parse(now), base) if base else now[:10]


# ------------------------------------------------------------------ dia
def day(conn, date: str) -> dict:
    p, sched = schedule_params(conn, date)
    legs = legs_of_day(conn, date)
    flights_only = [l for l in legs if l["is_flight"]]
    base = p.get("base_resolved") or p.get("base")
    days_off = set(p.get("planned_days_off") or [])
    reserve = one(conn.execute("SELECT * FROM roster_reserve WHERE start_date<=? AND end_date>=? ORDER BY id DESC LIMIT 1", (date, date)))
    out = {"date": date, "label": date_label(date), "base": place(conn, base) if base else None,
           "schedule": {"id": sched["id"], "name": sched["name"], "airline": sched["airline"],
                        "start_date": sched["start_date"], "end_date": sched["end_date"]} if sched else None,
           "legs": legs, "day_off": date in days_off, "reserve": None, "duty": None, "turns": [],
           "ends_at": None, "overnight": None, "next_duty": None, "kind": "LIVRE"}
    if reserve:
        act_end = None
        if reserve["act_start"] and reserve["act_days"]:
            act_end = (datetime.strptime(reserve["act_start"], "%Y-%m-%d") + timedelta(days=int(reserve["act_days"]) - 1)).strftime("%Y-%m-%d")
        out["reserve"] = {"uid": reserve["uid"], "from": reserve["start_date"], "to": reserve["end_date"], "days": reserve["days"],
                          "at": place(conn, reserve["location"]), "location_kind": reserve["location_kind"], "status": reserve["status"],
                          "act_start": reserve["act_start"], "act_end": act_end, "note": reserve["note"],
                          "activated_today": bool(reserve["act_start"] and act_end and reserve["act_start"] <= date <= act_end)}
    if flights_only:
        out["kind"] = "VOO"
        dt = _duty_times(p, flights_only[0]["dep_utc"], flights_only[-1]["arr_utc"])
        rep, rel = dt["report_utc"], dt["release_utc"]
        out["duty"] = {"report_utc": rep, "report_local": local_time(conn, rep, flights_only[0]["adep"]),
                       "release_utc": rel, "release_local": local_time(conn, rel, flights_only[-1]["ades"]),
                       "source": dt["source"],
                       "duty_min": int((parse(rel) - parse(rep)).total_seconds() // 60) if rep and rel else None,
                       "block_min": sum(l["block_min"] or 0 for l in flights_only) or None,
                       "legs": len(flights_only)}
        min_turn = int(p.get("min_turn_fixed") or p.get("min_turn") or 0)
        for a, b in zip(flights_only, flights_only[1:]):
            out["turns"].append(_gap(conn, a, b, min_turn))
        last = flights_only[-1]
        out["ends_at"] = place(conn, last["ades"])
        nxt = _next_leg(conn, last["arr_utc"])
        if nxt:
            out["next_duty"] = _duty_ref(conn, p, nxt)
            g = _gap(conn, last, nxt, min_turn)
            out["after_day"] = g
            if g["overnight"]:
                out["overnight"] = {"at": place(conn, last["ades"]), "from_utc": last["arr_utc"],
                                    "until_utc": out["next_duty"]["report_utc"] or nxt["dep_utc"], "minutes": g["minutes"],
                                    "kind": g["kind"], "label": g["overnight_label"], "detail": g["label"],
                                    "at_base": bool(base) and last["ades"] == base,
                                    "source": "mesma regra do AeroScale: hora local de chegada e de partida, ou 6 h ou mais no solo"}
    elif out["reserve"]:
        out["kind"] = "RESERVA"
    elif out["day_off"]:
        out["kind"] = "FOLGA"
    if out["kind"] != "VOO":
        nxt = _next_leg(conn, date + "T00:00:00Z")
        if nxt:
            out["next_duty"] = _duty_ref(conn, p, nxt)
    ina = one(conn.execute("SELECT * FROM roster_day WHERE kind='inativo' AND date=? AND schedule_id=? ORDER BY id DESC LIMIT 1",
                           (date, sched["id"]))) if sched and out["kind"] == "LIVRE" else None
    if ina:
        out["kind"] = "INATIVO"
        out["inactive"] = {"at": place(conn, ina["location"]), "note": ina["note"]}
    semtz = sorted({i for l in legs for i in (l["adep"], l["ades"]) if not rules.airport_tz(conn, i)})
    out["tz_unknown"] = semtz
    diags = jloads(sched["diagnostics"], []) if sched else []
    out["diagnostics"] = [d for d in diags if d.get("date") == date]
    out["schedule_notes"] = [d for d in diags if not d.get("date")]
    return out


def _gap(conn, a: dict, b: dict, min_turn: int = 0) -> dict:
    """Intervalo entre duas pernas, classificado pelo predict.gap_kind do AeroScale."""
    same = a["ades"] == b["adep"]
    minutes = int((parse(b["dep_utc"]) - parse(a["arr_utc"])).total_seconds() // 60)
    kind = rules.gap_kind(conn, parse(a["arr_utc"]), parse(b["dep_utc"]), a["ades"], min_turn) if same else "lacuna_dados"
    # o AeroScale conta como pernoite os intervalos classificados assim OU com 6 h ou mais no solo (scheduler._night_check)
    rest = same and (kind in OVERNIGHT_KINDS or minutes >= 6 * 60)
    return {"at": place(conn, a["ades"]), "to": place(conn, b["adep"]) if not same else None, "minutes": minutes, "after_uid": a["uid"],
            "same_airport": same, "kind": kind, "label": GAP_LABEL.get(kind, kind),
            "overnight": rest, "overnight_label": ("Parada de madrugada" if kind == "parada_noturna_curta" else "Pernoite") if rest else None,
            "below_min_turn": bool(min_turn and same and minutes < min_turn), "min_turn": min_turn or None}


def _next_leg(conn, after_utc: str):
    return one(conn.execute("""SELECT l.* FROM roster_leg l WHERE l.removed_batch IS NULL AND l.dep_utc >= ? AND l.activity<>'SOBREAVISO'
                               ORDER BY l.dep_utc LIMIT 1""", (after_utc,)))


def _duty_ref(conn, p: dict, nxt: dict) -> dict:
    ndt = _duty_times(p, nxt["dep_utc"], None)
    d = nxt["local_date"] if nxt.get("local_date") else nxt["dep_utc"][:10]
    return {"date": d, "label": date_label(d), "at": place(conn, nxt["adep"]), "report_utc": ndt["report_utc"],
            "report_local": local_time(conn, ndt["report_utc"], nxt["adep"]), "dep_utc": nxt["dep_utc"],
            "dep_local": local_time(conn, nxt["dep_utc"], nxt["adep"]), "flight_number": nxt["flight_number"],
            "to": place(conn, nxt["ades"]), "leg_id": nxt["id"]}


def today_overview(conn, now: str | None = None) -> dict:
    """O que a tela inicial precisa: hoje, próximo voo (mesmo que em outro dia) e pendências."""
    now = now or now_iso()
    d = day(conn, local_today(conn, now))
    nxt = one(conn.execute("""SELECT l.*, f.id AS flight_id, f.state AS flight_state, a.registration AS ac_registration, a.type_icao AS aircraft_type, f.aircraft_id
                              FROM roster_leg l LEFT JOIN flight f ON f.leg_id=l.id AND f.state<>'CANCELADO' LEFT JOIN aircraft a ON a.id=f.aircraft_id
                              WHERE l.removed_batch IS NULL AND l.arr_utc >= ? AND l.activity<>'SOBREAVISO' ORDER BY l.dep_utc LIMIT 1""", (now,)))
    d["next_flight"] = decorate_leg(conn, dict(nxt)) if nxt else None
    if d["next_flight"]:
        dep = parse(d["next_flight"]["dep_utc"])
        d["next_flight"]["starts_in_min"] = int((dep - parse(now)).total_seconds() // 60) if dep else None
        d["next_flight"]["today"] = d["next_flight"]["dep_utc"][:10] == now[:10]
        if d["next_flight"]["aircraft_id"]:
            d["next_flight"]["availability"] = fleet.availability(conn, d["next_flight"]["aircraft_id"], now)
    live = one(conn.execute("""SELECT s.id, s.phase, s.snapshot, s.last_seen_at, s.sim, f.id AS flight_id, f.flight_number, f.adep, f.ades, a.registration
                               FROM acars_session s JOIN flight f ON f.id=s.flight_id LEFT JOIN aircraft a ON a.id=f.aircraft_id WHERE s.status='LIVE' ORDER BY s.id DESC LIMIT 1"""))
    if live:
        live["snapshot"] = jloads(live["snapshot"], {})
    d["live"] = live
    d["now"] = now
    d["pending_pireps"] = conn.execute("SELECT COUNT(*) FROM pirep WHERE state='PENDING'").fetchone()[0]
    d["fleet_count"] = conn.execute("SELECT COUNT(*) FROM aircraft WHERE active=1").fetchone()[0]
    d["roster_legs"] = conn.execute("SELECT COUNT(*) FROM roster_leg WHERE removed_batch IS NULL").fetchone()[0]
    d["last_import"] = one(conn.execute("SELECT imported_at, source_name, source_version, source_created_at FROM import_batch ORDER BY id DESC LIMIT 1"))
    return d


# ------------------------------------------------------------------ semana
def week(conn, start: str) -> dict:
    p, sched = schedule_params(conn, start)
    days_off = set(p.get("planned_days_off") or [])
    d0 = datetime.strptime(start, "%Y-%m-%d")
    out = []
    for i in range(7):
        date = (d0 + timedelta(days=i)).strftime("%Y-%m-%d")
        legs = legs_of_day(conn, date)
        fl = [l for l in legs if l["is_flight"]]
        res = one(conn.execute("SELECT uid, location, status, act_start, act_days FROM roster_reserve WHERE start_date<=? AND end_date>=?", (date, date)))
        kind = "VOO" if fl else ("RESERVA" if res else ("FOLGA" if date in days_off else "LIVRE"))
        dt = _duty_times(p, fl[0]["dep_utc"], fl[-1]["arr_utc"]) if fl else {"report_utc": None, "release_utc": None}
        out.append({"date": date, "weekday": WEEKDAY[(d0 + timedelta(days=i)).weekday()], "kind": kind,
                    "legs": legs, "flights": len(fl), "block_min": sum(l["block_min"] or 0 for l in fl) or None,
                    "report_utc": dt["report_utc"], "release_utc": dt["release_utc"],
                    "from": place(conn, fl[0]["adep"]) if fl else None, "to": place(conn, fl[-1]["ades"]) if fl else None,
                    "reserve_at": place(conn, res["location"]) if res else None,
                    "booked": sum(1 for l in fl if l.get("flight_id"))})
    return {"start": start, "end": (d0 + timedelta(days=6)).strftime("%Y-%m-%d"), "days": out,
            "schedule": {"name": sched["name"], "start_date": sched["start_date"], "end_date": sched["end_date"]} if sched else None,
            "base": place(conn, p.get("base_resolved") or p.get("base")) if (p.get("base_resolved") or p.get("base")) else None}


def crew_report(conn, start: str | None = None, end: str | None = None) -> dict:
    """Jornadas, repousos, folgas, madrugadas e limites — calculados pelo crew.analyze do AeroScale."""
    p, sched = schedule_params(conn, start or now_iso()[:10])
    if not sched:
        return {"available": False, "reason": "Nenhuma escala importada."}
    start = start or sched["start_date"]
    end = end or sched["end_date"]
    from . import escala
    legs = escala.crew_legs(conn, start, end)
    if not legs:
        return {"available": False, "reason": "Sem pernas no período."}
    base = p.get("base_resolved") or p.get("base")
    fam = rules.type_family(p.get("icao_type_resolved") or p.get("icao_type"))
    rdays = {r["start_date"] for r in conn.execute("SELECT start_date FROM roster_reserve")}
    try:
        rep = rules.crew_report(conn, legs, p, base, fam, (datetime.strptime(start, "%Y-%m-%d").date(),
                                                           datetime.strptime(end, "%Y-%m-%d").date()), rdays)
    except Exception as e:                      # regra do AeroScale indisponível: diz, não inventa
        return {"available": False, "reason": f"Não foi possível aplicar as regras de tripulação: {e}"}
    rep["available"] = True
    rep["period"] = {"start": start, "end": end}
    rep["base"] = place(conn, base) if base else None
    rep["profile"] = rules.crew_profile(sched["airline"])
    rep["source"] = "crew.analyze do AeroScale (mesmo módulo, mesmos limites)"
    return rep


def backfill_local_dates(conn) -> int:
    """Pernas importadas antes da migração 0002 não têm local_date; calcula uma vez."""
    rows_ = rows(conn.execute("SELECT id, dep_utc, adep FROM roster_leg WHERE local_date IS NULL"))
    for r in rows_:
        d = parse(r["dep_utc"])
        conn.execute("UPDATE roster_leg SET local_date=? WHERE id=?",
                     (rules.local_date(conn, d, r["adep"]) if d else (r["dep_utc"] or "")[:10], r["id"]))
    return len(rows_)


def bounds(conn) -> dict:
    r = conn.execute("SELECT MIN(COALESCE(local_date, substr(dep_utc,1,10))) a, MAX(COALESCE(local_date, substr(dep_utc,1,10))) b "
                     "FROM roster_leg WHERE removed_batch IS NULL").fetchone()
    return {"first": r["a"], "last": r["b"]}


# ------------------------------------------------------------------ Flight Center
def simbrief_for(conn, leg: dict, aircraft: dict | None) -> dict:
    """Link do SimBrief montado pelo próprio AeroScale (rota e PBN do RPL, alternado sugerido com justificativa)."""
    ac = {"registration": aircraft["registration"], "icao_type": aircraft["type_icao"]} if aircraft else None
    src = {k: leg.get(k) for k in ("airline", "flight_number", "adep", "ades", "dep_utc", "arr_utc", "eet_min", "type_icao",
                                   "planned_type", "registration", "flight_id")}
    return rules.simbrief(conn, src, ac)


def flight_center(conn, flight_id: int) -> dict:
    f = flights.get_flight(conn, flight_id)
    leg = one(conn.execute("SELECT * FROM roster_leg WHERE id=?", (f["leg_id"],))) if f.get("leg_id") else None
    src = dict(leg) if leg else {k: f.get(k) for k in ("airline", "flight_number", "adep", "ades", "dep_utc", "arr_utc", "type_icao", "route", "eet_min")}
    src.setdefault("activity", "")
    src.setdefault("flight_id", leg["id"] if leg else None)
    decorate_leg(conn, src)
    ac = fleet.get_aircraft(conn, f["aircraft_id"]) if f.get("aircraft_id") else None
    p, _s = schedule_params(conn, (f.get("dep_utc") or now_iso())[:10])
    dt = _duty_times(p, f.get("dep_utc"), f.get("arr_utc"), f.get("airline"))
    pirep = one(conn.execute("SELECT id, state, submitted_at, warnings FROM pirep WHERE flight_id=? ORDER BY id DESC LIMIT 1", (flight_id,)))
    if pirep:
        pirep["warnings"] = jloads(pirep["warnings"], [])
    session = one(conn.execute("SELECT id, status, phase, snapshot, last_seen_at, sim FROM acars_session WHERE flight_id=? ORDER BY id DESC LIMIT 1", (flight_id,)))
    if session:
        session["snapshot"] = jloads(session["snapshot"], {})
    stages = []

    def stage(key, title, state, detail, note=None):
        stages.append({"key": key, "title": title, "state": state, "detail": detail, "note": note})

    stage("voo", "Voo e horários", "PRONTO" if f.get("dep_utc") else "FALTA",
          f"{src['from']['icao']} → {src['to']['icao']}" + (f" · {src['block_min']} min previstos" if src.get("block_min") else ""),
          f"Origem do dado no AeroScale: {leg['data_origin']}" if leg else "Voo não planejado, criado por você no LineOps")
    if ac:
        av = ac["availability"]["status"]
        stage("aeronave", "Aeronave", "PRONTO" if av == "DISPONIVEL" else ("ATENCAO" if av == "COM_RESTRICAO" else "BLOQUEADO"),
              f"{ac['registration']} · {ac['type_icao']}" + (f" · {ac['model_name']}" if ac.get("model_name") else ""),
              ac["location"] and f"Última posição conhecida: {ac['location']['icao']}" or None)
    else:
        stage("aeronave", "Aeronave", "FALTA", "Nenhuma matrícula atribuída", "Atribua uma aeronave da sua frota para liberar o ACARS.")
    if ac:
        op = [d for d in conn.execute("SELECT * FROM discrepancy WHERE aircraft_id=? AND status<>'CORRIGIDA'", (ac["id"],))]
        blocking = [d for d in op if d["restriction"] == "BLOQUEIA"]
        stage("mel", "Situação técnica (MEL simulado)", "BLOQUEADO" if blocking else ("ATENCAO" if op else "PRONTO"),
              "Sem discrepâncias abertas" if not op else "; ".join(f"{d['title']} ({d['restriction'].lower()})" for d in op),
              "Controle seu, simulado — não é MEL oficial.")
    else:
        stage("mel", "Situação técnica (MEL simulado)", "PENDENTE", "Depende da aeronave")
    plano = ofp_mod.latest(conn, flight_id)
    sb_pre = rules.simbrief(conn, {k: src.get(k) for k in ("airline", "flight_number", "adep", "ades", "dep_utc", "arr_utc",
                                                           "eet_min", "type_icao", "planned_type", "registration", "flight_id")},
                            {"registration": ac["registration"], "icao_type": ac["type_icao"]} if ac else None)
    alt = (sb_pre.get("alternate") or {})
    from . import equipe
    rel = equipe.release_of(conn, flight_id)
    if plano:
        d = plano["data"]
        resumo = (f"OFP · {d.get('adep')}→{d.get('ades')}" + (f"/{d.get('altn')}" if d.get("altn") else "")
                  + (f" · {d['fuel_ramp']:,.0f} kg na partida".replace(",", ".") if d.get("fuel_ramp") else ""))
        if rel and rel["status"] == "RETIDO":
            st_, det_ = "BLOQUEADO", f"Retido pelo DOV {rel['dov_name']}: " + "; ".join(rel["reasons"])
        elif rel and rel["pic_signed_at"]:
            st_, det_ = "PRONTO", f"Assinado por DOV {rel['dov_name']} e por você · {resumo}"
        elif rel:
            st_, det_ = "ATENCAO", f"Liberado pelo DOV {rel['dov_name']}: falta a sua assinatura · {resumo}"
        else:
            st_, det_ = "ATENCAO", f"{resumo} · o DOV está conferindo"
        stage("despacho", "Despacho e OFP", st_, det_,
              f"OFP {'anexado pelo DOV' if conn.execute("SELECT 1 FROM audit_log WHERE action='ofp.attach' AND entity_id=? AND user_id IS NULL", (str(plano['id']),)).fetchone() else 'importado'} em {plano['fetched_at'][:16].replace('T', ' ')}Z"
              + (f" · gerado {plano['generated_at'][:16].replace('T', ' ')}Z" if plano.get("generated_at") else "")
              + ". Despacho conjunto: DOV e comandante assinam (RBAC 121.663).")
    else:
        stage("despacho", "Despacho e OFP", "PENDENTE" if sb_pre.get("url") else "SEM_DADO",
              ("Planeje no SimBrief e importe o OFP" + (f" · alternado sugerido {alt['icao']}" if alt.get("icao") else ""))
              if sb_pre.get("url") else "Sem a cópia do banco do AeroScale não dá para montar o link",
              alt.get("note") or "O link é montado pelo módulo do AeroScale.")
    stage("rota", "Rota e nível (RPL)", "PRONTO" if src.get("route") else "SEM_DADO",
          src.get("route") or "O AeroScale não trouxe rota para esta perna",
          (f"FL{src['cruise_fl']} · {src['speed']}" if src.get("cruise_fl") else None) if src.get("route") else None)
    emitido = ls_mod.latest(conn, flight_id)
    try:
        prev = ls_mod.compute(conn, flight_id)
    except Exception:
        prev = {"lines": [], "warnings": [], "complete": False, "missing": ["dados do voo"]}
    estourou = any(w["level"] == "error" for w in prev["warnings"])
    peso = next((l for l in prev["lines"] if l["label"].startswith("Peso de decolagem")), {})
    stage("loadsheet", "LOADSHEET e payload",
          "BLOQUEADO" if estourou else ("PRONTO" if emitido else ("ATENCAO" if prev["complete"] else "PENDENTE")),
          (f"Emitido (edição {emitido['edition']})" if emitido else ("Pronto para emitir" if prev["complete"] else
           "Falta: " + ", ".join(prev["missing"][:3]) if prev["missing"] else "Sem dados"))
          + (f" · TOW {peso['value']:,.0f} kg".replace(",", ".") if peso.get("value") else ""),
          "Centro de gravidade entra quando você importar o peso e balanceamento da sua aeronave.")
    met = {}
    try:
        met = rules.weather(conn, [x for x in (src.get("adep"), src.get("ades"), (plano or {}).get("altn")
                                               or ((sb_pre.get("params") or {}).get("altn"))) if x])
    except Exception as e:                      # sem internet no servidor: a etapa diz isso, não some
        met = {"metars": [], "tafs": [], "error": str(e)}
    ok = [m for m in met.get("metars", []) if m.get("raw")]
    alertas = [a for m in ok for a in (m.get("alerts") or [])]
    stage("meteo", "Meteorologia", "PRONTO" if ok and not alertas else ("ATENCAO" if alertas else "SEM_DADO"),
          ", ".join(f"{m['icao']} {m.get('category') or ''}".strip() for m in ok) if ok else "Sem METAR agora (o servidor tem internet?)",
          "METAR e TAF do aviationweather.gov pelo módulo do AeroScale. NOTAM não entra nesta versão.")
    stage("briefing", "Briefing", "PRONTO", "Resumo desta perna com o que já está preparado")
    via = " pela IVAO" if session and session.get("sim") == "ivao" else ""
    stage("acars", "ACARS", "EM_ANDAMENTO" if session and session["status"] == "LIVE" else ("PRONTO" if f["state"] in ("PIREP_RECEBIDO", "APROVADO") else ("PENDENTE" if ac else "BLOQUEADO")),
          f"Sessão{via} {session['status'].lower()} · {session.get('phase') or '—'}" if session else
          ("Pronto: acompanhe pela IVAO ou pelo ACARS do LineOps" if ac else "Atribua a aeronave antes"))
    manual_e = one(conn.execute("SELECT * FROM logbook_entry WHERE flight_id=? AND source='MANUAL' AND reversed_at IS NULL ORDER BY id DESC LIMIT 1",
                                (flight_id,)))
    if manual_e:
        stage("pirep", "PIREP e debriefing", "PRONTO", f"Voo declarado por você · bloco {manual_e['block_min'] // 60}h{manual_e['block_min'] % 60:02d}",
              "Lançado direto no diário pelo “já fiz esse voo” (sem PIREP do ACARS).")
    else:
        stage("pirep", "PIREP e debriefing", {"PENDING": "ATENCAO", "APPROVED": "PRONTO", "REJECTED": "BLOQUEADO"}.get(pirep["state"], "PENDENTE") if pirep else "PENDENTE",
              f"PIREP #{pirep['id']} · {pirep['state'].lower()}" if pirep else "Será criado quando o ACARS (ou a IVAO) encerrar o voo")
    sb = simbrief_for(conn, src, ac)
    from . import feed, ivao
    return {"flight": f, "leg": src, "aircraft": ac, "manual_entry": manual_e, "timeline": feed.flight_feed(conn, flight_id),
            "release": rel, "messages": equipe.messages(conn, flight_id=flight_id, limit=20),
            "ivao": {"vid": ivao.vid_of(conn)},
            "duty": {"report_utc": dt["report_utc"], "report_local": local_time(conn, dt["report_utc"], f.get("adep")),
                     "release_utc": dt["release_utc"], "source": dt["source"]},
            "stages": stages, "pirep": pirep, "session": session, "ofp": plano, "weather": met,
            "loadsheet": {"preview": prev, "issued": emitido}, "simbrief": sb, "simbrief_url": sb.get("url"), "other_legs_today": [l for l in legs_of_day(conn, (leg["local_date"] if leg and leg.get("local_date") else (f.get("dep_utc") or now_iso())[:10]))
                                 if l.get("flight_id") != flight_id]}
