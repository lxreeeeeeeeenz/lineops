"""OFP do SimBrief: busca, leitura e guarda.

A API pública devolve o último plano do piloto em JSON:
  https://www.simbrief.com/api/xml.fetcher.php?userid=SEU_ID&json=1   (ou &username=...)
Não pede chave. Quem busca é o servidor do LineOps, no seu computador.

Os nomes dos campos do JSON do SimBrief não têm documentação oficial campo a campo, então aqui **nada é
obrigatório**: cada valor é procurado num caminho conhecido e, se não existir, fica nulo e entra na lista de
campos não encontrados, que aparece na tela. O JSON original fica guardado para conferência.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .db import audit, jloads, now_iso, one, rows, tx
from .errors import Conflict, FGVError, NotFound

API = "https://www.simbrief.com/api/xml.fetcher.php"
TIMEOUT = 25
MAX_RAW = 2_000_000
LB_TO_KG = 0.45359237

# nome interno -> caminho no JSON do SimBrief
PATHS = {
    "airline": "general.icao_airline", "flight_number": "general.flight_number", "callsign": "atc.callsign",
    "route": "general.route", "route_navlog": "general.route_navlog", "cost_index": "general.costindex",
    "initial_altitude": "general.initial_altitude", "cruise_profile": "general.cruise_profile",
    "air_distance": "general.air_distance", "route_distance": "general.route_distance",
    "avg_wind_comp": "general.avg_wind_comp", "avg_temp_dev": "general.avg_temp_dev", "sid": "general.sid_ident",
    "star": "general.star_ident", "airac": "general.airac",
    "adep": "origin.icao_code", "adep_name": "origin.name", "adep_rwy": "origin.plan_rwy", "adep_elev": "origin.elevation",
    "ades": "destination.icao_code", "ades_name": "destination.name", "ades_rwy": "destination.plan_rwy", "ades_elev": "destination.elevation",
    "altn": "alternate.icao_code", "altn_name": "alternate.name", "altn_burn": "alternate.burn", "altn_distance": "alternate.distance",
    "fuel_taxi": "fuel.taxi", "fuel_trip": "fuel.enroute_burn", "fuel_contingency": "fuel.contingency",
    "fuel_alternate": "fuel.alternate_burn", "fuel_reserve": "fuel.reserve", "fuel_extra": "fuel.extra",
    "fuel_min_takeoff": "fuel.min_takeoff", "fuel_takeoff": "fuel.plan_takeoff", "fuel_ramp": "fuel.plan_ramp",
    "fuel_landing": "fuel.plan_landing", "fuel_arrival": "fuel.reserve_arrival",
    "oew": "weights.oew", "pax_count": "weights.pax_count", "pax_weight": "weights.pax_weight",
    "bag_weight": "weights.bag_weight", "cargo": "weights.cargo", "payload": "weights.payload",
    "est_zfw": "weights.est_zfw", "max_zfw": "weights.max_zfw", "est_tow": "weights.est_tow", "max_tow": "weights.max_tow",
    "est_ldw": "weights.est_ldw", "max_ldw": "weights.max_ldw", "max_rmw": "weights.max_rmw",
    "eet_min": "times.est_time_enroute", "sched_out": "times.sched_out", "sched_in": "times.sched_in",
    "est_out": "times.est_out", "est_off": "times.est_off", "est_on": "times.est_on", "est_in": "times.est_in",
    "taxi_out_min": "times.taxi_out", "taxi_in_min": "times.taxi_in", "endurance_min": "times.endurance",
    "type_icao": "aircraft.icaocode", "type_name": "aircraft.name", "registration": "aircraft.reg",
    "equipment": "aircraft.equip", "selcal": "aircraft.selcal",
    "units": "params.units", "generated_epoch": "params.time_generated", "layout": "params.ofp_layout",
    "ofp_id": "params.request_id", "static_id": "params.static_id", "status": "fetch.status",
}
SECONDS = {"eet_min", "taxi_out_min", "taxi_in_min", "endurance_min"}           # o SimBrief manda segundos
EPOCH = {"sched_out", "sched_in", "est_out", "est_off", "est_on", "est_in", "generated_epoch"}
WEIGHTS = {"oew", "pax_weight", "bag_weight", "cargo", "payload", "est_zfw", "max_zfw", "est_tow", "max_tow",
           "est_ldw", "max_ldw", "max_rmw"}
FUEL = {"fuel_taxi", "fuel_trip", "fuel_contingency", "fuel_alternate", "fuel_reserve", "fuel_extra",
        "fuel_min_takeoff", "fuel_takeoff", "fuel_ramp", "fuel_landing", "fuel_arrival", "altn_burn"}
NUMBERS = WEIGHTS | FUEL | SECONDS | {"cost_index", "initial_altitude", "air_distance", "route_distance",
                                      "avg_wind_comp", "avg_temp_dev", "pax_count", "adep_elev", "ades_elev", "altn_distance"}


def _dig(raw: dict, path: str):
    cur = raw
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[0] if cur else None
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    if isinstance(cur, list):
        cur = cur[0] if cur else None
    return cur


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _epoch(v):
    n = _num(v)
    if n is None or n <= 0:
        return None
    return datetime.fromtimestamp(n, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(raw: dict) -> tuple[dict, list[dict]]:
    """Lê o JSON do SimBrief. Campo ausente vira nulo e entra em 'missing'; nada é estimado."""
    if not isinstance(raw, dict) or not raw:
        raise FGVError("O arquivo não é um OFP do SimBrief (JSON vazio ou inválido).", code="ofp_invalido")
    out: dict = {}
    missing: list[str] = []
    for name, path in PATHS.items():
        v = _dig(raw, path)
        if v is None or v == "":
            missing.append(name)
            out[name] = None
            continue
        if name in EPOCH:
            out[name] = _epoch(v)
        elif name in SECONDS:
            n = _num(v)
            out[name] = int(round(n / 60)) if n is not None else None
        elif name in NUMBERS:
            out[name] = _num(v)
        else:
            out[name] = str(v).strip()
    if not out.get("adep") or not out.get("ades"):
        raise FGVError("O JSON não parece um OFP do SimBrief: faltam origem e destino.", code="ofp_invalido")

    lbs = (out.get("units") or "").upper().startswith("LB")
    if lbs:                                           # guarda tudo em kg, dizendo que converteu
        for k in WEIGHTS | FUEL:
            if out.get(k) is not None:
                out[k] = round(out[k] * LB_TO_KG, 1)
        out["converted_from"] = "LBS"
    out["units_stored"] = "KG"
    nav = raw.get("navlog") or {}
    fixes = nav.get("fix") if isinstance(nav, dict) else nav
    out["navlog_points"] = len(fixes) if isinstance(fixes, list) else None
    alts = raw.get("alternate")
    out["alternates"] = [a.get("icao_code") for a in alts if isinstance(a, dict)] if isinstance(alts, list) else (
        [out["altn"]] if out.get("altn") else [])
    out["generated_at"] = out.pop("generated_epoch", None)
    out["missing"] = missing
    warnings = []
    if missing:
        warnings.append({"level": "info", "code": "campos_ausentes",
                         "message": f"{len(missing)} campo(s) não vieram neste OFP: {', '.join(missing[:8])}"
                                    + ("…" if len(missing) > 8 else "")})
    if lbs:
        warnings.append({"level": "info", "code": "conversao", "message": "Pesos e combustível vinham em libras e foram convertidos para quilos."})
    return out, warnings


def fetch(userid: str | None = None, username: str | None = None, timeout: int = TIMEOUT) -> dict:
    """Busca o último OFP do piloto na API pública do SimBrief (sem chave)."""
    if not userid and not username:
        raise FGVError("Informe seu Pilot ID ou o nome de usuário do SimBrief.", code="simbrief_sem_id")
    q = {"json": "1"}
    q["userid" if userid else "username"] = (userid or username).strip()
    url = API + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": "LineOps (simulação pessoal)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 400:
            raise FGVError("O SimBrief não reconheceu esse Pilot ID/usuário, ou você ainda não gerou nenhum plano.",
                           code="simbrief_nao_encontrado")
        raise FGVError(f"O SimBrief respondeu {e.code}.", code="simbrief_erro")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FGVError(f"Não foi possível falar com o SimBrief: {e}. O computador do servidor tem internet?",
                       code="simbrief_sem_rede")
    try:
        raw = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        raise FGVError("O SimBrief devolveu algo que não é JSON (o plano pode não existir ainda).", code="simbrief_resposta")
    if isinstance(raw, dict) and str((raw.get("fetch") or {}).get("status", "")).lower().startswith("error"):
        raise FGVError(f"SimBrief: {(raw.get('fetch') or {}).get('status')}", code="simbrief_erro")
    return raw


def _slim(raw: dict) -> str:
    """Guarda o JSON inteiro menos o OFP em HTML (grande e nunca exibido)."""
    cut = {k: v for k, v in raw.items() if k not in ("text",)}
    s = json.dumps(cut, ensure_ascii=False)
    if len(s) > MAX_RAW:
        cut = {k: v for k, v in cut.items() if k not in ("navlog", "tlr", "weather", "notams", "impacts")}
        s = json.dumps(cut, ensure_ascii=False)
    return s


def attach(conn, flight_id: int, raw: dict, *, source: str = "simbrief-api", user_id: int | None = None) -> dict:
    data, warnings = parse(raw)
    with tx(conn):
        f = one(conn.execute("SELECT f.*, a.registration FROM flight f LEFT JOIN aircraft a ON a.id=f.aircraft_id WHERE f.id=?", (flight_id,)))
        if not f:
            raise NotFound("Voo não encontrado.")
        if f["state"] in ("APROVADO", "CANCELADO"):
            raise Conflict("O voo já foi encerrado; não dá para anexar um OFP.", code="estado_invalido")
        if data.get("adep") != f["adep"] or data.get("ades") != f["ades"]:
            warnings.append({"level": "warn", "code": "trecho_diferente",
                             "message": f"O OFP é de {data.get('adep')}→{data.get('ades')} e o voo é "
                                        f"{f['adep']}→{f['ades']}. Confira se gerou o plano certo."})
        fn = (data.get("flight_number") or "").lstrip("0")
        esperado = "".join(ch for ch in (f["flight_number"] or "") if ch.isdigit()).lstrip("0")
        if fn and esperado and fn != esperado:
            warnings.append({"level": "info", "code": "numero_diferente",
                             "message": f"Número do voo no OFP ({data.get('flight_number')}) diferente do da escala ({f['flight_number']})."})
        if f["registration"] and data.get("registration") and data["registration"].upper() != f["registration"].upper():
            warnings.append({"level": "info", "code": "matricula_diferente",
                             "message": f"Matrícula no OFP ({data['registration']}) diferente da atribuída ({f['registration']})."})
        cur = conn.execute("""INSERT INTO ofp(flight_id, source, ofp_id, fetched_at, generated_at, units, airline, flight_number,
                              adep, ades, altn, data, raw, warnings) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (flight_id, source, data.get("ofp_id"), now_iso(), data.get("generated_at"), data.get("units"),
                            data.get("airline"), data.get("flight_number"), data.get("adep"), data.get("ades"), data.get("altn"),
                            json.dumps(data, ensure_ascii=False), _slim(raw), json.dumps(warnings, ensure_ascii=False)))
        audit(conn, user_id, "ofp.attach", "ofp", cur.lastrowid, {"flight": flight_id, "source": source})
    return get(conn, cur.lastrowid)


def get(conn, ofp_id: int) -> dict:
    o = one(conn.execute("SELECT * FROM ofp WHERE id=?", (ofp_id,)))
    if not o:
        raise NotFound("OFP não encontrado.")
    o["data"] = jloads(o["data"], {})
    o["warnings"] = jloads(o["warnings"], [])
    o.pop("raw", None)
    return o


def latest(conn, flight_id: int) -> dict | None:
    o = one(conn.execute("SELECT id FROM ofp WHERE flight_id=? ORDER BY id DESC LIMIT 1", (flight_id,)))
    return get(conn, o["id"]) if o else None


def history(conn, flight_id: int) -> list[dict]:
    return rows(conn.execute("SELECT id, source, ofp_id, fetched_at, generated_at FROM ofp WHERE flight_id=? ORDER BY id DESC", (flight_id,)))


def delete(conn, ofp_id: int, user_id: int | None = None):
    with tx(conn):
        if not conn.execute("DELETE FROM ofp WHERE id=?", (ofp_id,)).rowcount:
            raise NotFound("OFP não encontrado.")
        audit(conn, user_id, "ofp.delete", "ofp", ofp_id)


# ------------------------------------------------------------------ planejado × realizado
def compare(ofp_data: dict, pirep_data: dict) -> list[dict]:
    """Linhas de comparação para o PIREP. Só compara o que existe dos dois lados."""
    out = []

    def add(label, planned, flown, unit, fmt=lambda v: f"{v:,.0f}".replace(",", ".")):
        if planned is None or flown is None:
            return
        out.append({"label": label, "planned": planned, "flown": flown, "diff": round(flown - planned, 1),
                    "unit": unit, "planned_text": fmt(planned), "flown_text": fmt(flown),
                    "diff_text": ("+" if flown >= planned else "−") + fmt(abs(flown - planned))})

    add("Combustível na partida", ofp_data.get("fuel_ramp"), pirep_data.get("fuel_out_kg"), "kg")
    add("Combustível na chegada", ofp_data.get("fuel_landing"), pirep_data.get("fuel_in_kg"), "kg")
    add("Consumo", ofp_data.get("fuel_trip"), pirep_data.get("fuel_used_kg"), "kg")
    add("Tempo de voo", ofp_data.get("eet_min"), pirep_data.get("air_min"), "min", lambda v: f"{int(v // 60)}:{int(v % 60):02d}")
    add("Peso na decolagem", ofp_data.get("est_tow"), pirep_data.get("gw_off_kg"), "kg")
    add("Peso no pouso", ofp_data.get("est_ldw"), pirep_data.get("gw_on_kg"), "kg")
    add("Distância", ofp_data.get("air_distance"), pirep_data.get("distance_nm"), "nm")
    return out
