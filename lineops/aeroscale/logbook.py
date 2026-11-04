"""Diário de bordo (logbook): pernas voadas no simulador, lançamentos manuais e importados.

Origem de cada lançamento: 'escala' (perna marcada como voada), 'manual', 'ivao'/'vatsim' (detectado no
monitor de rede), 'importado' (arquivo de outro app: ELEVATEX, Volanta, CSV/JSON genérico).

Tempo noturno: aproximação pelo horário local do aeroporto de partida (18:00–06:00), sem cálculo de pôr do sol.
"""
from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone

from . import catalog
from .airports import get_tz
from .db import now_iso, row, rows

SCHEMA = """
CREATE TABLE IF NOT EXISTS logbook (
    id INTEGER PRIMARY KEY AUTOINCREMENT, leg_id INTEGER, schedule_id INTEGER,
    date TEXT NOT NULL, airline TEXT, flight_number TEXT, adep TEXT, ades TEXT,
    aircraft_type TEXT, registration TEXT,
    off_block_utc TEXT, takeoff_utc TEXT, landing_utc TEXT, on_block_utc TEXT,
    block_min INTEGER, flight_min INTEGER, night_min INTEGER DEFAULT 0, landings INTEGER DEFAULT 1,
    role TEXT DEFAULT 'PIC', network TEXT, source TEXT NOT NULL DEFAULT 'manual', remarks TEXT,
    external_id TEXT, created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_logbook_date ON logbook(date);
CREATE UNIQUE INDEX IF NOT EXISTS ux_logbook_ext ON logbook(source, external_id) WHERE external_id IS NOT NULL;
"""

NIGHT_START, NIGHT_END = time(18, 0), time(6, 0)


def _p(s):
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    s = str(s).strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _f(d):
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


def night_minutes(t0: datetime, t1: datetime, tzname: str | None) -> int:
    """Minutos entre t0 e t1 que caem entre 18:00 e 06:00 locais (aproximação de voo noturno)."""
    if not t0 or not t1 or t1 <= t0:
        return 0
    tz = get_tz(tzname) or timezone.utc
    n, t = 0, t0
    while t < t1:
        lt = t.astimezone(tz).time()
        if lt >= NIGHT_START or lt < NIGHT_END:
            n += 1
        t += timedelta(minutes=1)
    return n


def _complete(db, e: dict) -> dict:
    """Preenche durações e tempo noturno a partir dos horários."""
    ob, tk, ld, ib = (_p(e.get(k)) for k in ("off_block_utc", "takeoff_utc", "landing_utc", "on_block_utc"))
    if ob and ib and not e.get("block_min"):
        e["block_min"] = int((ib - ob).total_seconds() // 60)
    if tk and ld and not e.get("flight_min"):
        e["flight_min"] = int((ld - tk).total_seconds() // 60)
    if not e.get("flight_min") and e.get("block_min"):
        e["flight_min"] = max(0, int(e["block_min"]) - 20)   # sem horário de decolagem/pouso: bloco − táxi
    t0, t1 = tk or ob, ld or ib
    if t0 and t1 and e.get("night_min") in (None, ""):
        apmap = catalog.airport_map(db)
        e["night_min"] = night_minutes(t0, t1, catalog.airport_tz(apmap, e.get("adep") or ""))
    if not e.get("date"):
        e["date"] = (ob or tk or datetime.now(timezone.utc)).date().isoformat()
    for k in ("off_block_utc", "takeoff_utc", "landing_utc", "on_block_utc"):
        e[k] = _f(_p(e.get(k)))
    return e


FIELDS = ("leg_id", "schedule_id", "date", "airline", "flight_number", "adep", "ades", "aircraft_type", "registration",
          "off_block_utc", "takeoff_utc", "landing_utc", "on_block_utc", "block_min", "flight_min", "night_min",
          "landings", "role", "network", "source", "remarks", "external_id")


def add(db, e: dict) -> int:
    e = _complete(db, dict(e))
    for k in ("adep", "ades", "aircraft_type", "airline"):
        if e.get(k):
            e[k] = str(e[k]).strip().upper()
    if not e.get("adep") or not e.get("ades"):
        raise ValueError("Informe origem e destino (ICAO).")
    ts = now_iso()
    vals = [e.get(k) for k in FIELDS]
    cur = db.execute(f"INSERT INTO logbook({', '.join(FIELDS)}, created_at, updated_at) VALUES "
                     f"({', '.join('?' * len(FIELDS))}, ?, ?)", (*vals, ts, ts))
    return cur.lastrowid


def update(db, lid: int, e: dict):
    cur = row(db.execute("SELECT * FROM logbook WHERE id=?", (lid,)))
    if not cur:
        raise LookupError("Lançamento não encontrado.")
    new = {**cur, **{k: v for k, v in e.items() if k in FIELDS}}
    for k in ("block_min", "flight_min", "night_min"):
        if k not in e and any(x in e for x in ("off_block_utc", "takeoff_utc", "landing_utc", "on_block_utc")):
            new[k] = None
    new = _complete(db, new)
    db.execute(f"UPDATE logbook SET {', '.join(f'{k}=?' for k in FIELDS)}, updated_at=? WHERE id=?",
               (*[new.get(k) for k in FIELDS], now_iso(), lid))


def delete(db, lid: int):
    db.execute("UPDATE schedule_legs SET flown_status='', act_dep_utc=NULL, act_arr_utc=NULL, logbook_id=NULL"
               " WHERE logbook_id=?", (lid,))
    db.execute("DELETE FROM logbook WHERE id=?", (lid,))


# ------------------------------------------------------------------ perna da escala
def mark_flown(db, leg_id: int, data: dict | None = None) -> dict:
    data = data or {}
    l = row(db.execute("SELECT * FROM schedule_legs WHERE id=?", (leg_id,)))
    if not l:
        raise LookupError("Perna não encontrada.")
    if l.get("activity") == "EXTRA":
        raise ValueError("Perna como extra (passageiro) não entra no diário de bordo como voo.")
    ob = data.get("off_block_utc") or l.get("sim_dep_utc") or l["dep_utc"]
    ib = data.get("on_block_utc") or l.get("sim_arr_utc") or l["arr_utc"]
    e = {"leg_id": leg_id, "schedule_id": l["schedule_id"], "airline": l["airline"], "flight_number": l["flight_number"],
         "adep": l["adep"], "ades": l["ades"], "aircraft_type": data.get("aircraft_type") or l["type_icao"],
         "registration": data.get("registration") or l.get("registration"), "off_block_utc": ob, "on_block_utc": ib,
         "takeoff_utc": data.get("takeoff_utc"), "landing_utc": data.get("landing_utc"),
         "landings": int(data.get("landings") or 1), "role": data.get("role") or "PIC",
         "network": data.get("network"), "source": data.get("source") or "escala", "remarks": data.get("remarks")}
    if l.get("logbook_id") and row(db.execute("SELECT id FROM logbook WHERE id=?", (l["logbook_id"],))):
        update(db, l["logbook_id"], {k: v for k, v in e.items() if v is not None})
        lid = l["logbook_id"]
    else:
        lid = add(db, e)
    db.execute("UPDATE schedule_legs SET flown_status='voado', act_dep_utc=?, act_arr_utc=?, logbook_id=? WHERE id=?",
               (_f(_p(ob)), _f(_p(ib)), lid, leg_id))
    return row(db.execute("SELECT * FROM logbook WHERE id=?", (lid,)))


def unmark_flown(db, leg_id: int):
    l = row(db.execute("SELECT logbook_id FROM schedule_legs WHERE id=?", (leg_id,)))
    if not l:
        raise LookupError("Perna não encontrada.")
    if l["logbook_id"]:
        delete(db, l["logbook_id"])
    db.execute("UPDATE schedule_legs SET flown_status='', act_dep_utc=NULL, act_arr_utc=NULL, logbook_id=NULL WHERE id=?",
               (leg_id,))


def match_leg(db, e: dict, window_h: float = 6) -> int | None:
    """Perna de escala que corresponde a um voo (mesmo trecho, horário próximo, número igual se houver)."""
    t = _p(e.get("off_block_utc") or e.get("takeoff_utc"))
    if not t or not e.get("adep") or not e.get("ades"):
        return None
    lo, hi = _f(t - timedelta(hours=window_h)), _f(t + timedelta(hours=window_h))
    cands = rows(db.execute("SELECT * FROM schedule_legs WHERE adep=? AND ades=? AND dep_utc BETWEEN ? AND ?"
                            " AND flown_status='' AND activity<>'EXTRA' ORDER BY dep_utc",
                            (e["adep"].upper(), e["ades"].upper(), lo, hi)))
    if not cands:
        return None
    dig = re.sub(r"\D", "", str(e.get("flight_number") or ""))
    if dig:
        same = [c for c in cands if re.sub(r"\D", "", c.get("flight_number") or "").lstrip("0") == dig.lstrip("0")]
        if same:
            cands = same
    return min(cands, key=lambda c: abs((_p(c["dep_utc"]) - t).total_seconds()))["id"]


# ------------------------------------------------------------------ consultas
def entries(db, q: str = "", year: str = "", limit: int = 2000) -> list[dict]:
    sql, args = "SELECT * FROM logbook WHERE 1=1", []
    if year:
        sql += " AND substr(date,1,4)=?"
        args.append(year)
    if q:
        sql += " AND (flight_number LIKE ? OR adep LIKE ? OR ades LIKE ? OR aircraft_type LIKE ? OR registration LIKE ?)"
        args += [f"%{q.upper()}%"] * 5
    return rows(db.execute(sql + " ORDER BY date DESC, off_block_utc DESC LIMIT ?", (*args, limit)))


def totals(db) -> dict:
    ls = rows(db.execute("SELECT * FROM logbook"))
    by_type, by_al, by_month = Counter(), Counter(), Counter()
    aps = set()
    out = {"flights": len(ls), "block_min": 0, "flight_min": 0, "night_min": 0, "landings": 0}
    for e in ls:
        b = int(e["block_min"] or 0)
        out["block_min"] += b
        out["flight_min"] += int(e["flight_min"] or 0)
        out["night_min"] += int(e["night_min"] or 0)
        out["landings"] += int(e["landings"] or 0)
        by_type[catalog.type_family(e["aircraft_type"] or "") or e["aircraft_type"] or "?"] += b
        by_al[e["airline"] or "?"] += b
        by_month[(e["date"] or "")[:7]] += b
        aps.update(x for x in (e["adep"], e["ades"]) if x)
    out["airports"] = len(aps)
    out["by_family"] = [{"family": k, "block_min": v} for k, v in by_type.most_common()]
    out["by_airline"] = [{"airline": k, "block_min": v} for k, v in by_al.most_common()]
    out["by_month"] = [{"month": k, "block_min": v} for k, v in sorted(by_month.items())][-12:]
    return out


def export_csv(db) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Data", "Cia", "Voo", "Origem", "Destino", "Tipo", "Matrícula", "Calço (UTC)", "Decolagem (UTC)",
                "Pouso (UTC)", "Calço chegada (UTC)", "Bloco (min)", "Voo (min)", "Noturno (min)", "Pousos", "Função",
                "Rede", "Origem do lançamento", "Observações"])
    for e in reversed(entries(db, limit=100000)):
        w.writerow([e["date"], e["airline"] or "", e["flight_number"] or "", e["adep"], e["ades"], e["aircraft_type"] or "",
                    e["registration"] or "", e["off_block_utc"] or "", e["takeoff_utc"] or "", e["landing_utc"] or "",
                    e["on_block_utc"] or "", e["block_min"] or "", e["flight_min"] or "", e["night_min"] or "",
                    e["landings"] or "", e["role"] or "", e["network"] or "", e["source"], e["remarks"] or ""])
    return "﻿" + buf.getvalue()


# ------------------------------------------------------------------ importação (ELEVATEX, Volanta, CSV/JSON)
def _n(s: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(s))   # camelCase -> snake_case (startedAt, departureIcao)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


ALIASES = {   # campo do diário: nomes aceitos (normalizados), em ordem de preferência
    "adep": ["departure_icao", "dep_icao", "origin_icao", "departure_airport", "departure", "dep", "origin", "from",
             "orig", "origem", "partida_icao", "departureicao", "dep_airport", "start_airport", "departureairport"],
    "ades": ["arrival_icao", "arr_icao", "destination_icao", "arrival_airport", "arrival", "arr", "destination", "dest",
             "to", "destino", "chegada_icao", "arrivalicao", "arr_airport", "end_airport", "arrivalairport"],
    "flight_number": ["flight_number", "flightnumber", "callsign", "flight", "voo", "flight_no", "ident"],
    "aircraft_type": ["aircraft_icao", "aircraft_type", "icao_type", "aircrafticao", "type", "aircraft", "tipo",
                      "equipment", "aircraft_model"],
    "registration": ["registration", "tail", "tail_number", "reg", "matricula", "aircraft_registration"],
    "off_block_utc": ["off_block", "offblock", "block_off", "out", "out_time", "departure_time", "dep_time",
                      "start_time", "started_at", "start", "departure_utc", "off_block_time", "blocks_off", "calco"],
    "takeoff_utc": ["takeoff", "takeoff_time", "off", "wheels_off", "airborne", "takeoff_utc", "departed_at"],
    "landing_utc": ["landing", "landing_time", "on", "wheels_on", "touchdown", "landing_utc", "landed_at"],
    "on_block_utc": ["on_block", "onblock", "block_on", "in", "in_time", "arrival_time", "arr_time", "end_time",
                     "ended_at", "end", "arrival_utc", "on_block_time", "blocks_on"],
    "block_min": ["block_time", "block_minutes", "block_min", "duration", "duration_min", "total_time", "flight_time",
                  "time", "minutes"],
    "landings": ["landings", "landing_count", "pousos"],
    "network": ["network", "rede"],
    "remarks": ["remarks", "notes", "comment", "observacoes"],
    "external_id": ["id", "flight_id", "uuid"],
    "date": ["date", "data", "flight_date", "day"],
}


def _flatten(d: dict, pre: str = "") -> dict:
    out = {}
    for k, v in d.items():
        kk = f"{pre}{_n(k)}"
        if isinstance(v, dict):
            out.update(_flatten(v, kk + "_"))
            # departure: {"icao": "SBGR"} -> departure_icao; aceita também "departure"
            if "icao" in {_n(x) for x in v}:
                out.setdefault(kk, v.get("icao") or v.get("ICAO"))
        else:
            out[kk] = v
    return out


def _duration_min(v) -> int | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return int(v / 60) if v > 1440 else int(v)   # segundos ou minutos
    s = str(v).strip()
    m = re.match(r"^(\d+):(\d{2})(?::\d{2})?$", s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.match(r"^(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?", s.lower())
    if m and (m.group(1) or m.group(2)):
        return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    try:
        return int(float(s.replace(",", ".")) * (60 if "." in s or "," in s else 1))
    except ValueError:
        return None


def _time_val(v, date_s: str | None):
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) and v > 10 ** 9:
        return datetime.fromtimestamp(v / (1000 if v > 10 ** 12 else 1), tz=timezone.utc)
    s = str(v).strip()
    d = _p(s)
    if d:
        return d
    for f in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(s, f).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    if date_s and re.match(r"^\d{1,2}:\d{2}", s):
        dd = _p(date_s) or _p(str(date_s)[:10])
        if dd:
            hh, mm = map(int, s[:5].split(":"))
            return dd.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return None


def parse_file(data: bytes, filename: str = "") -> tuple[list[dict], dict]:
    """Lê voos de um arquivo de outro app de logbook. Devolve (lançamentos, informações do mapeamento)."""
    text = None
    for enc in ("utf-8-sig", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            pass
    text = (text or "").strip()
    items: list[dict] = []
    if text.startswith("{") or text.startswith("["):
        obj = json.loads(text)
        if isinstance(obj, dict):
            for k in ("flights", "data", "items", "logbook", "results", "entries"):
                if isinstance(obj.get(k), list):
                    obj = obj[k]
                    break
            else:
                obj = [obj]
        items = [_flatten(x) for x in obj if isinstance(x, dict)]
    else:
        sample = "\n".join(text.splitlines()[:5])
        sep = max((";", ",", "\t"), key=sample.count)
        rd = csv.DictReader(io.StringIO(text), delimiter=sep)
        items = [{_n(k): v for k, v in r.items() if k} for r in rd]
    if not items:
        raise ValueError("arquivo sem voos reconhecíveis")
    keys = set().union(*(x.keys() for x in items[:50]))
    mapping = {}
    for field, names in ALIASES.items():
        for nm in names:
            if nm in keys:
                mapping[field] = nm
                break
    if "adep" not in mapping or "ades" not in mapping:
        raise ValueError(f"não achei origem/destino; campos do arquivo: {', '.join(sorted(keys)[:30])}")
    out = []
    for x in items:
        g = lambda f: x.get(mapping[f]) if f in mapping else None  # noqa: E731
        adep, ades = str(g("adep") or "").strip().upper(), str(g("ades") or "").strip().upper()
        if not re.match(r"^[A-Z0-9]{3,4}$", adep) or not re.match(r"^[A-Z0-9]{3,4}$", ades):
            continue
        date_s = g("date")
        e = {"adep": adep, "ades": ades, "flight_number": (str(g("flight_number")).strip().upper() if g("flight_number") else None),
             "aircraft_type": (str(g("aircraft_type")).strip().upper()[:4] if g("aircraft_type") else None),
             "registration": g("registration"), "network": g("network"), "remarks": g("remarks"),
             "landings": int(_duration_min(g("landings")) or 1) if g("landings") not in (None, "") else 1,
             "external_id": str(g("external_id")) if g("external_id") not in (None, "") else None}
        for f in ("off_block_utc", "takeoff_utc", "landing_utc", "on_block_utc"):
            t = _time_val(g(f), date_s)
            e[f] = _f(t) if t else None
        bm = _duration_min(g("block_min"))
        if bm:
            e["block_min"] = bm
        if not e["on_block_utc"] and e["off_block_utc"] and bm:
            e["on_block_utc"] = _f(_p(e["off_block_utc"]) + timedelta(minutes=bm))
        if date_s and not e["off_block_utc"]:
            d = _time_val(date_s, None)
            e["date"] = d.date().isoformat() if d else str(date_s)[:10]
        out.append(e)
    return out, {"items": len(items), "valid": len(out), "mapping": mapping}


def import_file(db, data: bytes, filename: str, source_label: str = "importado", link_legs: bool = True) -> dict:
    entries_, info = parse_file(data, filename)
    added = dup = linked = 0
    for e in entries_:
        e["source"] = source_label
        if e.get("external_id") and row(db.execute("SELECT id FROM logbook WHERE source=? AND external_id=?",
                                                   (source_label, e["external_id"]))):
            dup += 1
            continue
        if not e.get("external_id") and e.get("off_block_utc") and row(db.execute(
                "SELECT id FROM logbook WHERE adep=? AND ades=? AND off_block_utc=?",
                (e["adep"], e["ades"], e["off_block_utc"]))):
            dup += 1
            continue
        lid = add(db, e)
        added += 1
        if link_legs:
            leg = match_leg(db, e)
            if leg:
                rec = row(db.execute("SELECT * FROM logbook WHERE id=?", (lid,)))
                db.execute("UPDATE logbook SET leg_id=?, schedule_id=(SELECT schedule_id FROM schedule_legs WHERE id=?)"
                           " WHERE id=?", (leg, leg, lid))
                db.execute("UPDATE schedule_legs SET flown_status='voado', act_dep_utc=?, act_arr_utc=?, logbook_id=?"
                           " WHERE id=?", (rec["off_block_utc"], rec["on_block_utc"], lid, leg))
                linked += 1
    return {**info, "added": added, "duplicates": dup, "linked_legs": linked}
