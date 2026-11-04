"""LOADSHEET: pesos, margens e, quando houver perfil de peso e balanceamento, o centro de gravidade.

Cada número traz de onde veio (OFP do SimBrief, sua frota, ou informado por você no momento da emissão).
Nada é estimado: faltando um dado, a linha fica vazia e o loadsheet avisa o que falta.
Documento de simulação — não é documento operacional.
"""
from __future__ import annotations

import json

from . import ofp as ofp_mod
from .db import audit, jloads, now_iso, one, rows, tx
from .errors import Conflict, FGVError, NotFound

PAX_PADRAO_NOTA = "informado por você"
LIMITES = (("mzfw_kg", "est_zfw", "ZFW"), ("mtow_kg", "est_tow", "TOW"), ("mlw_kg", "est_ldw", "LW"))


def _n(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _line(label, value, source, *, limit=None, limit_source=None):
    d = {"label": label, "value": None if value is None else round(value, 1), "source": source}
    if limit is not None:
        d["limit"] = round(limit, 1)
        d["limit_source"] = limit_source
        d["margin"] = None if value is None else round(limit - value, 1)
        d["over"] = bool(value is not None and value > limit)
    return d


def compute(conn, flight_id: int, overrides: dict | None = None) -> dict:
    """Monta o loadsheet com o que existe. overrides: pax_count, pax_weight, bag_weight, cargo, fuel_ramp, fuel_trip, oew."""
    ov = {k: _n(v) for k, v in (overrides or {}).items() if v not in (None, "")}
    f = one(conn.execute("""SELECT f.*, a.registration, a.type_icao AS ac_type, a.limits, a.model_name
                            FROM flight f LEFT JOIN aircraft a ON a.id=f.aircraft_id WHERE f.id=?""", (flight_id,)))
    if not f:
        raise NotFound("Voo não encontrado.")
    plano = ofp_mod.latest(conn, flight_id)
    d = plano["data"] if plano else {}
    lim = jloads(f["limits"], {}) or {}
    falta: list[str] = []

    def pick(key, ofp_key, rotulo):
        """Prioridade: o que você informou agora > o OFP. Devolve (valor, fonte)."""
        if key in ov:
            return ov[key], PAX_PADRAO_NOTA
        v = _n(d.get(ofp_key))
        if v is None:
            falta.append(rotulo)
            return None, None
        return v, "OFP do SimBrief"

    oew, oew_src = pick("oew", "oew", "peso vazio operacional")
    pax_n, pax_src = pick("pax_count", "pax_count", "número de passageiros")
    pax_w, pax_w_src = pick("pax_weight", "pax_weight", "peso por passageiro")
    bag, bag_src = pick("bag_weight", "bag_weight", "bagagem")
    cargo, cargo_src = pick("cargo", "cargo", "carga")
    fuel_ramp, fr_src = pick("fuel_ramp", "fuel_ramp", "combustível na partida")
    fuel_trip, ft_src = pick("fuel_trip", "fuel_trip", "combustível de viagem")
    taxi = _n(d.get("fuel_taxi"))

    pax_total = (pax_n * pax_w) if pax_n is not None and pax_w is not None else None
    payload = None
    if pax_total is not None or bag is not None or cargo is not None:
        payload = sum(x for x in (pax_total, bag, cargo) if x is not None)
    if "payload" in ov:
        payload = ov["payload"]
    zfw = (oew + payload) if oew is not None and payload is not None else _n(d.get("est_zfw"))
    zfw_src = "calculado" if (oew is not None and payload is not None) else ("OFP do SimBrief" if zfw is not None else None)
    tow = (zfw + fuel_ramp - (taxi or 0)) if zfw is not None and fuel_ramp is not None else _n(d.get("est_tow"))
    tow_src = "calculado" if (zfw is not None and fuel_ramp is not None) else ("OFP do SimBrief" if tow is not None else None)
    lw = (tow - fuel_trip) if tow is not None and fuel_trip is not None else _n(d.get("est_ldw"))
    lw_src = "calculado" if (tow is not None and fuel_trip is not None) else ("OFP do SimBrief" if lw is not None else None)

    def limite(chave, ofp_key):
        if lim.get(chave) and _n(lim[chave].get("value")):
            return _n(lim[chave]["value"]), f"sua frota ({lim[chave].get('source') or 'informado'})"
        v = _n(d.get(ofp_key))
        return (v, "OFP do SimBrief") if v is not None else (None, None)

    mzfw, mzfw_s = limite("mzfw_kg", "max_zfw")
    mtow, mtow_s = limite("mtow_kg", "max_tow")
    mlw, mlw_s = limite("mlw_kg", "max_ldw")

    linhas = [
        _line("Peso vazio operacional (DOW)", oew, oew_src),
        _line("Passageiros", pax_total, f"{int(pax_n)} × {int(pax_w)} kg ({pax_src})" if pax_total is not None else None),
        _line("Bagagem", bag, bag_src),
        _line("Carga", cargo, cargo_src),
        _line("Payload", payload, "calculado" if payload is not None else None),
        _line("Peso zero combustível (ZFW)", zfw, zfw_src, limit=mzfw, limit_source=mzfw_s),
        _line("Combustível na partida", fuel_ramp, fr_src),
        _line("Combustível de táxi", taxi, "OFP do SimBrief" if taxi is not None else None),
        _line("Peso de decolagem (TOW)", tow, tow_src, limit=mtow, limit_source=mtow_s),
        _line("Combustível de viagem", fuel_trip, ft_src),
        _line("Peso de pouso (LW)", lw, lw_src, limit=mlw, limit_source=mlw_s),
    ]
    avisos = []
    for l in linhas:
        if l.get("over"):
            avisos.append({"level": "error", "code": "acima_do_limite",
                           "message": f"{l['label']}: {l['value']:,.0f} kg acima do limite de {l['limit']:,.0f} kg."
                                      .replace(",", ".")})
    if falta:
        avisos.append({"level": "warn", "code": "dados_faltando",
                       "message": "Sem estes dados o loadsheet fica incompleto: " + ", ".join(dict.fromkeys(falta)) + "."})
    if not plano:
        avisos.append({"level": "warn", "code": "sem_ofp", "message": "Nenhum OFP importado: os números teriam de ser todos informados por você."})
    if not any(x for x in (mzfw, mtow, mlw)):
        avisos.append({"level": "info", "code": "sem_limites",
                       "message": "Sem limites de peso cadastrados na frota nem no OFP: não dá para calcular margem."})

    perfil = wb_for(conn, f["ac_type"] or d.get("type_icao"))
    cg = {"available": False, "reason": "Sem perfil de peso e balanceamento para este tipo. Importe o flight_model.cfg da "
                                        "aeronave que você voa para o LineOps calcular o CG."} if not perfil else cg_from(perfil, zfw, tow)

    return {"flight": {"id": f["id"], "flight_number": f["flight_number"], "adep": f["adep"], "ades": f["ades"],
                       "dep_utc": f["dep_utc"], "registration": f["registration"], "type_icao": f["ac_type"] or d.get("type_icao"),
                       "model": f["model_name"]},
            "ofp_id": plano["id"] if plano else None,
            "ofp_generated_at": plano.get("generated_at") if plano else None,
            "lines": linhas, "warnings": avisos, "cg": cg, "missing": list(dict.fromkeys(falta)),
            "complete": not falta,
            "units": "kg", "computed_at": now_iso(),
            "disclaimer": "Documento de simulação gerado pelo LineOps. Não é documento operacional."}


# ------------------------------------------------------------------ perfil de peso e balanceamento
def wb_for(conn, type_icao: str | None, model: str | None = None) -> dict | None:
    if not type_icao:
        return None
    q = "SELECT * FROM wb_profile WHERE type_icao=?"
    args = [type_icao.upper()]
    if model:
        q += " AND model=?"
        args.append(model)
    r = one(conn.execute(q + " ORDER BY id DESC LIMIT 1", args))
    if r:
        r["data"] = jloads(r["data"], {})
    return r


def cg_from(perfil: dict, zfw: float | None, tow: float | None) -> dict:
    """CG só sai com os números do perfil importado da sua aeronave; sem eles, diz o que falta."""
    p = perfil["data"]
    need = [k for k in ("mac_m", "lemac_m", "empty_weight_kg", "empty_arm_m") if p.get(k) is None]
    if need:
        return {"available": False, "reason": f"O perfil de {perfil['model']} não tem {', '.join(need)}."}
    return {"available": False, "reason": "Cálculo de CG entra quando as estações de carga forem conferidas com a sua aeronave.",
            "profile": {"model": perfil["model"], "source": perfil["source"], "stations": len(p.get("stations") or [])}}


# ------------------------------------------------------------------ emissão
def issue(conn, flight_id: int, overrides: dict | None = None, notes: str | None = None, user_id: int | None = None) -> dict:
    data = compute(conn, flight_id, overrides)
    if any(w["level"] == "error" for w in data["warnings"]) and not notes:
        raise Conflict("O loadsheet está acima de um limite. Corrija os pesos ou escreva uma justificativa para emitir.",
                       code="acima_do_limite")
    with tx(conn):
        n = conn.execute("SELECT COALESCE(MAX(edition),0)+1 FROM loadsheet WHERE flight_id=?", (flight_id,)).fetchone()[0]
        data["edition"] = n
        cur = conn.execute("INSERT INTO loadsheet(flight_id, ofp_id, issued_at, edition, data, notes) VALUES (?,?,?,?,?,?)",
                           (flight_id, data["ofp_id"], now_iso(), n, json.dumps(data, ensure_ascii=False), notes))
        audit(conn, user_id, "loadsheet.issue", "loadsheet", cur.lastrowid, {"flight": flight_id, "edition": n})
    return get(conn, cur.lastrowid)


def get(conn, ls_id: int) -> dict:
    r = one(conn.execute("SELECT * FROM loadsheet WHERE id=?", (ls_id,)))
    if not r:
        raise NotFound("LOADSHEET não encontrado.")
    r["data"] = jloads(r["data"], {})
    return r


def latest(conn, flight_id: int) -> dict | None:
    r = one(conn.execute("SELECT id FROM loadsheet WHERE flight_id=? ORDER BY edition DESC LIMIT 1", (flight_id,)))
    return get(conn, r["id"]) if r else None


def history(conn, flight_id: int) -> list[dict]:
    return rows(conn.execute("SELECT id, edition, issued_at, notes FROM loadsheet WHERE flight_id=? ORDER BY edition DESC", (flight_id,)))
