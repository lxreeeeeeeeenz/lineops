"""Frota por matrícula. Nada é inventado: limites/configurações só existem se você (ou o AeroScale) informar.
Disponibilidade é SEMPRE calculada (indisponibilidades, discrepâncias, voos), nunca digitada."""
from __future__ import annotations

import csv
import io
import json
import re

from .db import audit, jloads, now_iso, one, rows, tx
from .errors import Conflict, FGVError, NotFound

REG_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,9}$")
LIMIT_KEYS = ("mtow_kg", "mlw_kg", "mzfw_kg", "oew_kg", "max_fuel_kg", "pax_capacity")
RESTRICTIONS = ("NENHUMA", "INFORMA", "CONFIRMA", "BLOQUEIA")


def _norm_limits(limits: dict | None) -> dict:
    out = {}
    for k, v in (limits or {}).items():
        if k not in LIMIT_KEYS:
            raise FGVError(f"Limite desconhecido: {k}", code="limite_invalido")
        if v in (None, ""):
            continue
        val, src = (v.get("value"), v.get("source")) if isinstance(v, dict) else (v, None)
        if val in (None, ""):
            continue
        try:
            val = float(val)
        except (TypeError, ValueError):
            raise FGVError(f"Valor inválido para {k}", code="limite_invalido")
        if val <= 0:
            raise FGVError(f"{k} deve ser positivo (deixe em branco se desconhecido)", code="limite_invalido")
        out[k] = {"value": val, "source": src or "informado pelo usuário"}
    return out


def add_aircraft(conn, *, registration, airline, type_icao, model_name=None, variant=None, msn=None, limits=None,
                 config=None, baseline_hours=None, baseline_cycles=None, baseline_note=None, notes=None,
                 source="MANUAL", user_id=None) -> dict:
    reg = (registration or "").strip().upper()
    if not REG_RE.match(reg):
        raise FGVError("Matrícula inválida (ex.: PR-XYZ).", code="matricula_invalida")
    t = (type_icao or "").strip().upper()
    if not re.match(r"^[A-Z0-9]{2,4}$", t):
        raise FGVError("Tipo ICAO inválido (ex.: B738).", code="tipo_invalido")
    if baseline_hours is not None and baseline_hours < 0 or baseline_cycles is not None and baseline_cycles < 0:
        raise FGVError("Horas/ciclos iniciais não podem ser negativos.", code="baseline_invalido")
    now = now_iso()
    with tx(conn):
        if conn.execute("SELECT 1 FROM aircraft WHERE registration=?", (reg,)).fetchone():
            raise Conflict(f"Já existe aeronave {reg}.", code="matricula_duplicada")
        cur = conn.execute(
            """INSERT INTO aircraft(registration, airline, type_icao, model_name, variant, msn, config, limits, baseline_hours,
               baseline_cycles, baseline_note, source, notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (reg, (airline or "").strip().upper(), t, model_name, variant, msn, json.dumps(config or {}, ensure_ascii=False),
             json.dumps(_norm_limits(limits), ensure_ascii=False), baseline_hours, baseline_cycles, baseline_note, source, notes, now, now))
        audit(conn, user_id, "aircraft.add", "aircraft", cur.lastrowid, {"registration": reg})
    return get_aircraft(conn, cur.lastrowid)


def update_aircraft(conn, aircraft_id, patch: dict, user_id=None) -> dict:
    allowed = {"airline", "type_icao", "model_name", "variant", "msn", "active", "config", "limits", "baseline_hours",
               "baseline_cycles", "baseline_note", "notes"}
    bad = set(patch) - allowed
    if bad:
        raise FGVError(f"Campos não editáveis: {', '.join(sorted(bad))}", code="campo_invalido")
    with tx(conn):
        cur = get_aircraft(conn, aircraft_id, totals=False)
        sets, vals = [], []
        for k, v in patch.items():
            if k == "limits":
                v = json.dumps(_norm_limits(v), ensure_ascii=False)
            elif k == "config":
                v = json.dumps(v or {}, ensure_ascii=False)
            elif k == "type_icao":
                v = (v or "").strip().upper()
            elif k == "airline":
                v = (v or "").strip().upper()
            elif k == "active":
                v = int(bool(v))
            sets.append(f"{k}=?")
            vals.append(v)
        if sets:
            conn.execute(f"UPDATE aircraft SET {', '.join(sets)}, updated_at=? WHERE id=?", (*vals, now_iso(), aircraft_id))
            audit(conn, user_id, "aircraft.update", "aircraft", aircraft_id, {"fields": sorted(patch)})
    return get_aircraft(conn, aircraft_id)


def get_aircraft(conn, aircraft_id, totals=True) -> dict:
    r = one(conn.execute("SELECT * FROM aircraft WHERE id=?", (aircraft_id,)))
    if not r:
        raise NotFound("Aeronave não encontrada.")
    return _decorate(conn, r, totals)


def _decorate(conn, r: dict, totals=True) -> dict:
    r["config"] = jloads(r["config"], {})
    r["limits"] = jloads(r["limits"], {})
    if totals:
        r["totals"] = totals_for(conn, r["id"], r)
        r["availability"] = availability(conn, r["id"])
        r["location"] = last_location(conn, r["id"])
    return r


def list_aircraft(conn, include_inactive=False) -> list[dict]:
    q = "SELECT * FROM aircraft" + ("" if include_inactive else " WHERE active=1") + " ORDER BY registration"
    return [_decorate(conn, r) for r in rows(conn.execute(q))]


def totals_for(conn, aircraft_id, ac: dict | None = None) -> dict:
    ac = ac or one(conn.execute("SELECT * FROM aircraft WHERE id=?", (aircraft_id,)))
    s = conn.execute("SELECT COALESCE(SUM(block_min),0) b, COALESCE(SUM(air_min),0) a, COALESCE(SUM(cycles),0) c "
                     "FROM usage_ledger WHERE aircraft_id=? AND reversed_at IS NULL", (aircraft_id,)).fetchone()
    bh, bc = ac.get("baseline_hours"), ac.get("baseline_cycles")
    return {"flown_block_min": s["b"], "flown_air_min": s["a"], "flown_cycles": s["c"],
            "baseline_hours": bh, "baseline_cycles": bc,
            "total_hours": None if bh is None else round(bh + s["a"] / 60, 2),
            "total_cycles": None if bc is None else bc + s["c"]}


def last_location(conn, aircraft_id) -> dict | None:
    r = one(conn.execute("SELECT ades, in_utc, on_utc, date, id FROM logbook_entry WHERE aircraft_id=? AND reversed_at IS NULL "
                         "ORDER BY COALESCE(in_utc, on_utc, date) DESC, id DESC LIMIT 1", (aircraft_id,)))
    return {"icao": r["ades"], "since": r["in_utc"] or r["on_utc"] or r["date"], "source": "último voo aprovado"} if r else None


def availability(conn, aircraft_id, at: str | None = None) -> dict:
    """at = ISO UTC (padrão: agora). Resultado: DISPONIVEL | COM_RESTRICAO | INDISPONIVEL + motivos."""
    at = at or now_iso()
    day = at[:10]
    ac = one(conn.execute("SELECT active FROM aircraft WHERE id=?", (aircraft_id,)))
    if not ac:
        raise NotFound("Aeronave não encontrada.")
    block, restr = [], []
    if not ac["active"]:
        block.append("Aeronave desativada na frota.")
    for u in conn.execute("SELECT from_date, to_date, reason FROM unavailability WHERE aircraft_id=? AND from_date<=? AND to_date>=?",
                          (aircraft_id, day, day)):
        block.append(f"Indisponível {u['from_date']}→{u['to_date']}: {u['reason']}")
    for d in conn.execute("SELECT id, title, restriction, restriction_text, due_date, status FROM discrepancy "
                          "WHERE aircraft_id=? AND status<>'CORRIGIDA'", (aircraft_id,)):
        item = {"discrepancy_id": d["id"], "title": d["title"], "restriction": d["restriction"], "text": d["restriction_text"]}
        if d["status"] == "DIFERIDA" and d["due_date"] and d["due_date"] < day:
            block.append(f"Diferimento vencido em {d['due_date']}: {d['title']}")
        elif d["restriction"] == "BLOQUEIA":
            block.append(f"Discrepância bloqueante: {d['title']}")
        elif d["restriction"] in ("INFORMA", "CONFIRMA"):
            restr.append(item)
    st = "INDISPONIVEL" if block else ("COM_RESTRICAO" if restr else "DISPONIVEL")
    return {"status": st, "blocking": block, "restrictions": restr, "at": at}


def add_unavailability(conn, aircraft_id, from_date, to_date, reason, user_id=None):
    get_aircraft(conn, aircraft_id, totals=False)
    if not (from_date and to_date and from_date <= to_date and reason):
        raise FGVError("Informe período válido e motivo.", code="periodo_invalido")
    with tx(conn):
        cur = conn.execute("INSERT INTO unavailability(aircraft_id, from_date, to_date, reason, created_at) VALUES (?,?,?,?,?)",
                           (aircraft_id, from_date, to_date, reason, now_iso()))
        audit(conn, user_id, "aircraft.unavailability", "aircraft", aircraft_id, {"from": from_date, "to": to_date})
    return cur.lastrowid


def add_discrepancy(conn, aircraft_id, title, *, description=None, restriction="NENHUMA", restriction_text=None,
                    mel_ref=None, deferred=False, due_date=None, user_id=None):
    get_aircraft(conn, aircraft_id, totals=False)
    if restriction not in RESTRICTIONS:
        raise FGVError("Restrição inválida.", code="restricao_invalida")
    if not title:
        raise FGVError("Informe o título da discrepância.", code="titulo_vazio")
    with tx(conn):
        cur = conn.execute("""INSERT INTO discrepancy(aircraft_id, opened_at, title, description, status, restriction, restriction_text, mel_ref, due_date)
                              VALUES (?,?,?,?,?,?,?,?,?)""", (aircraft_id, now_iso(), title, description, "DIFERIDA" if deferred else "ABERTA",
                                                              restriction, restriction_text, mel_ref, due_date))
        audit(conn, user_id, "discrepancy.add", "discrepancy", cur.lastrowid, {"aircraft": aircraft_id})
    return cur.lastrowid


def close_discrepancy(conn, discrepancy_id, user_id=None):
    with tx(conn):
        n = conn.execute("UPDATE discrepancy SET status='CORRIGIDA', closed_at=? WHERE id=? AND status<>'CORRIGIDA'",
                         (now_iso(), discrepancy_id)).rowcount
        if not n:
            raise NotFound("Discrepância não encontrada ou já corrigida.")
        audit(conn, user_id, "discrepancy.close", "discrepancy", discrepancy_id)


def adjust_usage(conn, aircraft_id, *, block_min=0, air_min=0, cycles=0, note, user_id=None):
    """Correção manual explícita (lançamento no livro-razão, auditável)."""
    get_aircraft(conn, aircraft_id, totals=False)
    if not note:
        raise FGVError("Ajustes de horas/ciclos exigem uma justificativa.", code="nota_obrigatoria")
    with tx(conn):
        cur = conn.execute("INSERT INTO usage_ledger(aircraft_id, kind, block_min, air_min, cycles, note, created_at) VALUES (?, 'ADJUST', ?,?,?,?,?)",
                           (aircraft_id, block_min, air_min, cycles, note, now_iso()))
        audit(conn, user_id, "usage.adjust", "aircraft", aircraft_id, {"block_min": block_min, "air_min": air_min, "cycles": cycles})
    return cur.lastrowid


def import_csv(conn, text: str, user_id=None) -> dict:
    """CSV com cabeçalho: registration,airline,type_icao[,model_name,msn,mtow_kg,mlw_kg,mzfw_kg,oew_kg,max_fuel_kg,pax_capacity,baseline_hours,baseline_cycles].
    Células vazias = desconhecido. Matrículas existentes são puladas (nunca sobrescritas)."""
    rd = csv.DictReader(io.StringIO(text))
    need = {"registration", "airline", "type_icao"}
    if not rd.fieldnames or not need <= {f.strip().lower() for f in rd.fieldnames}:
        raise FGVError("CSV precisa das colunas: registration, airline, type_icao.", code="csv_invalido")
    added, skipped, errors = [], [], []
    for i, raw in enumerate(rd, start=2):
        r = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        try:
            lim = {k: r[k] for k in LIMIT_KEYS if r.get(k)}
            lim = {k: {"value": v, "source": "CSV"} for k, v in lim.items()}
            bh = float(r["baseline_hours"]) if r.get("baseline_hours") else None
            bc = int(r["baseline_cycles"]) if r.get("baseline_cycles") else None
            add_aircraft(conn, registration=r.get("registration"), airline=r.get("airline"), type_icao=r.get("type_icao"),
                         model_name=r.get("model_name") or None, msn=r.get("msn") or None, limits=lim, baseline_hours=bh,
                         baseline_cycles=bc, baseline_note="CSV" if bh is not None or bc is not None else None, source="CSV", user_id=user_id)
            added.append(r["registration"].upper())
        except Conflict:
            skipped.append(r.get("registration", "").upper())
        except (FGVError, ValueError) as e:
            errors.append({"line": i, "registration": r.get("registration"), "error": getattr(e, "message", str(e))})
    return {"added": added, "skipped_existing": skipped, "errors": errors}
