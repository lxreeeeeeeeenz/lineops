"""Frota: aeronaves, modelos, campos de configuração dinâmicos, histórico e importação de listas."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import unicodedata

from . import catalog
from .db import add_flag, now_iso, row, rows

TRI = ("SIM", "NÃO", "DESCONHECIDO")
TRI_YES = {"SIM", "S", "YES", "Y", "X", "✓", "✔", "1", "TRUE", "VERDADEIRO", "INSTALADO", "INST", "OK",
           "POSSUI", "TEM", "ON", "C/", "COM"}
TRI_NO = {"NAO", "NÃO", "N", "NO", "0", "FALSE", "FALSO", "NAO INSTALADO", "NÃO INSTALADO", "NAO POSSUI",
          "NÃO POSSUI", "OFF", "S/", "SEM"}
TRI_UNKNOWN = {"", "?", "??", "ND", "N/D", "DESCONHECIDO", "UNKNOWN", "S/I", "SI", "INDEFINIDO", "VERIFICAR"}
TRI_AMBIGUOUS = {"-", "--", "NA", "N/A"}

BR_REG = re.compile(r"^(PP|PR|PS|PT|PU)-?([A-Z]{3})$")
GENERIC_REG = re.compile(r"^(?=.*[A-Z])[A-Z0-9]{1,3}-?[A-Z0-9]{2,5}$")


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]+", "", s.upper())


def normalize_registration(raw: str) -> tuple[str | None, str | None]:
    """Retorna (matrícula normalizada, aviso). None se não parecer matrícula."""
    s = (raw or "").strip().upper().replace(" ", "")
    if not s:
        return None, None
    m = BR_REG.match(s)
    if m:
        return f"{m.group(1)}-{m.group(2)}", None
    if GENERIC_REG.match(s):
        return s, "Formato de matrícula fora do padrão brasileiro (PP/PR/PS/PT/PU-XXX)."
    return None, None


def looks_like_reg(s: str) -> bool:
    """Teste estrito (usado para detectar formato): padrão BR ou XX-XXXX com hífen."""
    s = (s or "").strip().upper()
    return bool(BR_REG.match(s) or re.fullmatch(r"(?=.*[A-Z])[A-Z0-9]{1,3}-[A-Z0-9]{2,5}", s))


def normalize_tri(raw) -> tuple[str, str | None]:
    s = str(raw if raw is not None else "").strip().upper()
    s2 = s.replace("Ã", "A") if s not in TRI_NO else s
    if s in TRI_YES or s2 in TRI_YES:
        return "SIM", None
    if s in TRI_NO or s2 in TRI_NO:
        return "NÃO", None
    if s in TRI_UNKNOWN:
        return "DESCONHECIDO", None
    if s in TRI_AMBIGUOUS:
        return "DESCONHECIDO", f"Valor ambíguo '{raw}' tratado como DESCONHECIDO."
    return "DESCONHECIDO", f"Valor não reconhecido '{raw}' tratado como DESCONHECIDO."


MODEL_PATTERNS = [
    (r"A?319.*NEO|A19N", "A19N"), (r"A?320.*NEO|A20N|320N\b", "A20N"), (r"A?321.*NEO|A21N|321N\b", "A21N"),
    (r"A?319", "A319"), (r"A?320", "A320"), (r"A?321", "A321"),
    (r"737.?MAX.?8|B38M|7M8|737-8\b|MAX ?8", "B38M"), (r"737.?MAX.?9|B39M", "B39M"),
    (r"737.?800|B738|73H\b|7378", "B738"), (r"737.?700|B737|73G\b|7377", "B737"),
    (r"E195.?E2|E295|195.?E2", "E295"), (r"E190.?E2|E290", "E290"), (r"E195|EMB.?195|ERJ.?195", "E195"),
    (r"E190|EMB.?190|ERJ.?190", "E190"),
    (r"ATR.?72.?600|AT76|72-600", "AT76"), (r"ATR.?72.?500|AT75|72-500", "AT75"),
    (r"787.?9|B789", "B789"), (r"787.?8|B788", "B788"), (r"767.?300|B763", "B763"),
    (r"777.?300|B77W", "B77W"), (r"A?350.?900|A359", "A359"), (r"A?330.?200|A332", "A332"),
]


def guess_type(model_text: str) -> tuple[str | None, str | None]:
    """Designador ICAO a partir de texto livre. Retorna (tipo, aviso)."""
    s = (model_text or "").upper().strip()
    if not s:
        return None, None
    if s in catalog.TYPE_INFO:
        return s, None
    if "ATR" in s and not re.search(r"500|600|AT7", s):
        return None, f"Modelo '{model_text}' ambíguo (variante de ATR 72 não informada); associe manualmente."
    hits = [t for pat, t in MODEL_PATTERNS if re.search(pat, s)]
    if not hits:
        return None, f"Modelo '{model_text}' não reconhecido; associe manualmente."
    return hits[0], None


# ---------------------------------------------------------------- campos
def fields_for(db, airline: str | None = None, family: str | None = None, include_inactive=False) -> list[dict]:
    q = "SELECT * FROM config_fields WHERE 1=1"
    args: list = []
    if not include_inactive:
        q += " AND active=1"
    if airline is not None:
        q += " AND (airline IS NULL OR airline=?)"
        args.append(airline)
    if family is not None:
        q += " AND (family IS NULL OR family=?)"
        args.append(family)
    out = rows(db.execute(q + " ORDER BY sort, label", args))
    for f in out:
        f["options"] = json.loads(f["options"] or "[]")
    return out


def field_applies(field: dict, airline: str, family: str | None) -> bool:
    return (field["airline"] in (None, airline)) and (field["family"] in (None, family) or family is None)


def create_field(db, data: dict) -> dict:
    label = (data.get("label") or "").strip()
    if not label:
        raise ValueError("Informe o nome do campo.")
    key = norm_text(data.get("key") or label) or None
    if not key:
        raise ValueError("Nome do campo inválido.")
    kind = data.get("kind") or "tri"
    if kind not in ("tri", "text", "number", "select"):
        raise ValueError("Tipo de campo inválido.")
    airline = data.get("airline") or None
    family = data.get("family") or None
    options = data.get("options") or []
    if isinstance(options, str):
        options = [o.strip() for o in options.split(",") if o.strip()]
    if kind == "select" and not options:
        raise ValueError("Campos de lista precisam de opções.")
    dup = db.execute("SELECT id FROM config_fields WHERE key=? AND airline IS ? AND family IS ?",
                     (key, airline, family)).fetchone()
    if dup:
        raise ValueError(f"Já existe o campo {key} com esse escopo.")
    cur = db.execute(
        "INSERT INTO config_fields(key,label,airline,family,kind,options,description,sort,active,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,1,?)",
        (key, label, airline, family, kind, json.dumps(options, ensure_ascii=False), data.get("description") or "",
         int(data.get("sort") or 100), now_iso()))
    return {"id": cur.lastrowid, "key": key}


def update_field(db, fid: int, data: dict):
    f = row(db.execute("SELECT * FROM config_fields WHERE id=?", (fid,)))
    if not f:
        raise LookupError("Campo não encontrado.")
    options = data.get("options", json.loads(f["options"] or "[]"))
    if isinstance(options, str):
        options = [o.strip() for o in options.split(",") if o.strip()]
    db.execute(
        "UPDATE config_fields SET label=?, airline=?, family=?, options=?, description=?, sort=?, active=? WHERE id=?",
        (data.get("label", f["label"]), data.get("airline", f["airline"]) or None,
         data.get("family", f["family"]) or None, json.dumps(options, ensure_ascii=False),
         data.get("description", f["description"]), int(data.get("sort", f["sort"]) or 100),
         1 if data.get("active", f["active"]) else 0, fid))


# ---------------------------------------------------------------- modelos
def list_models(db, airline=None) -> list[dict]:
    q = ("SELECT m.*, (SELECT COUNT(*) FROM aircraft a WHERE a.model_id=m.id) AS aircraft_count"
         " FROM aircraft_models m")
    args = []
    if airline:
        q += " WHERE m.airline=?"
        args.append(airline)
    return rows(db.execute(q + " ORDER BY m.airline, m.icao_type, m.name", args))


def save_model(db, data: dict, mid: int | None = None) -> dict:
    airline = (data.get("airline") or "").upper()
    name = (data.get("name") or "").strip()
    icao = (data.get("icao_type") or "").strip().upper()
    if not airline or not name or not re.fullmatch(r"[A-Z0-9]{2,4}", icao):
        raise ValueError("Informe companhia, nome e designador ICAO (2 a 4 caracteres, ex.: A320).")
    family = (data.get("family") or catalog.type_family(icao) or icao).strip().upper()
    if mid:
        db.execute("UPDATE aircraft_models SET airline=?, name=?, icao_type=?, family=? WHERE id=?",
                   (airline, name, icao, family, mid))
        return {"id": mid}
    dup = db.execute("SELECT id FROM aircraft_models WHERE airline=? AND icao_type=? AND name=?",
                     (airline, icao, name)).fetchone()
    if dup:
        raise ValueError("Modelo já cadastrado para essa companhia.")
    cur = db.execute(
        "INSERT INTO aircraft_models(airline,name,icao_type,family,source,created_at) VALUES (?,?,?,?,'manual',?)",
        (airline, name, icao, family, now_iso()))
    return {"id": cur.lastrowid}


def delete_model(db, mid: int):
    n = db.execute("SELECT COUNT(*) c FROM aircraft WHERE model_id=?", (mid,)).fetchone()["c"]
    if n:
        raise ValueError(f"Modelo em uso por {n} aeronave(s); altere-as antes de excluir.")
    db.execute("DELETE FROM aircraft_models WHERE id=?", (mid,))


# ---------------------------------------------------------------- aeronaves
def _history(db, aid, action, field=None, old=None, new=None, source="manual", note=None):
    db.execute(
        "INSERT INTO aircraft_history(aircraft_id, ts, action, field, old_value, new_value, source, note)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (aid, now_iso(), action, field, None if old is None else str(old), None if new is None else str(new),
         source, note))


AC_SELECT = (
    "SELECT a.*, m.name AS model_name, m.icao_type, m.family,"
    " (SELECT COUNT(*) FROM review_flags f WHERE f.entity='aircraft' AND f.entity_id=a.id AND f.resolved=0)"
    " AS open_flags FROM aircraft a LEFT JOIN aircraft_models m ON m.id=a.model_id"
)


def _attach_config(db, items: list[dict]):
    if not items:
        return items
    ids = [i["id"] for i in items]
    cfg: dict[int, dict] = {i: {} for i in ids}
    q = ("SELECT c.aircraft_id, c.value, c.raw_value, c.source, c.updated_at, f.id AS field_id, f.key, f.label"
         " FROM aircraft_config c JOIN config_fields f ON f.id=c.field_id WHERE c.aircraft_id IN (%s)"
         % ",".join("?" * len(ids)))
    for r in db.execute(q, ids):
        cfg[r["aircraft_id"]][str(r["field_id"])] = {
            "value": r["value"], "raw": r["raw_value"], "source": r["source"], "key": r["key"],
            "label": r["label"], "updated_at": r["updated_at"]}
    for i in items:
        i["config"] = cfg[i["id"]]
        i["unavailable"] = json.loads(i.get("unavailable") or "[]")
    return items


def list_aircraft(db, args: dict) -> list[dict]:
    q = AC_SELECT + " WHERE 1=1"
    p: list = []
    if args.get("airline"):
        q += " AND a.airline=?"
        p.append(args["airline"])
    if args.get("model_id"):
        q += " AND a.model_id=?"
        p.append(int(args["model_id"]))
    if args.get("icao_type"):
        q += " AND m.icao_type=?"
        p.append(args["icao_type"])
    if args.get("family"):
        q += " AND m.family=?"
        p.append(args["family"])
    if args.get("q"):
        q += " AND (a.registration LIKE ? OR a.msn LIKE ? OR a.notes LIKE ? OR m.name LIKE ?)"
        p += [f"%{args['q'].upper()}%", f"%{args['q']}%", f"%{args['q']}%", f"%{args['q']}%"]
    st = args.get("status", "active")
    if st == "active":
        q += " AND a.active=1"
    elif st == "inactive":
        q += " AND a.active=0"
    if args.get("no_model") == "1":
        q += " AND a.model_id IS NULL"
    if args.get("review") == "1":
        q += " AND EXISTS (SELECT 1 FROM review_flags f WHERE f.entity='aircraft' AND f.entity_id=a.id AND f.resolved=0)"
    # filtros de equipamento: "12:SIM,13:NÃO"
    for part in filter(None, (args.get("eq") or "").split(",")):
        try:
            fid_s, val = part.split(":", 1)
            fid = int(fid_s)
        except ValueError:
            continue
        if val == "DESCONHECIDO":
            q += (" AND NOT EXISTS (SELECT 1 FROM aircraft_config c WHERE c.aircraft_id=a.id AND c.field_id=?"
                  " AND c.value IN ('SIM','NÃO'))")
            p.append(fid)
        else:
            q += " AND EXISTS (SELECT 1 FROM aircraft_config c WHERE c.aircraft_id=a.id AND c.field_id=? AND c.value=?)"
            p += [fid, val]
    q += " ORDER BY a.airline, m.icao_type, a.registration"
    return _attach_config(db, rows(db.execute(q, p)))


def get_aircraft(db, aid: int) -> dict | None:
    r = row(db.execute(AC_SELECT + " WHERE a.id=?", (aid,)))
    if not r:
        return None
    _attach_config(db, [r])
    r["fields"] = fields_for(db, r["airline"], r["family"])
    r["raw_records"] = rows(db.execute(
        "SELECT * FROM aircraft_raw WHERE aircraft_id=? ORDER BY id DESC LIMIT 50", (aid,)))
    r["flags"] = rows(db.execute(
        "SELECT * FROM review_flags WHERE entity='aircraft' AND entity_id=? ORDER BY resolved, id DESC", (aid,)))
    return r


def _validate_unavailable(val) -> list:
    if isinstance(val, str):
        val = json.loads(val or "[]")
    out = []
    for p in val or []:
        f, t = (p.get("from") or "").strip(), (p.get("to") or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", f) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", t) or t < f:
            raise ValueError("Período de indisponibilidade inválido (use datas AAAA-MM-DD, fim ≥ início).")
        out.append({"from": f, "to": t, "reason": (p.get("reason") or "").strip()})
    return out


def _clean_blocked(s: str) -> str:
    codes = [c.strip().upper() for c in re.split(r"[,\s;]+", s or "") if c.strip()]
    bad = [c for c in codes if not re.fullmatch(r"[A-Z]{4}", c)]
    if bad:
        raise ValueError(f"Aeroportos restritos inválidos: {', '.join(bad)} (use ICAO de 4 letras).")
    return ",".join(dict.fromkeys(codes))


def create_aircraft(db, data: dict, source="manual", is_demo=False) -> int:
    reg, warn = normalize_registration(data.get("registration", ""))
    if not reg:
        raise ValueError("Matrícula inválida.")
    airline = (data.get("airline") or "").upper()
    if not db.execute("SELECT 1 FROM airlines WHERE code=?", (airline,)).fetchone():
        raise ValueError("Companhia inválida.")
    ex = row(db.execute("SELECT id, airline, active FROM aircraft WHERE registration=?", (reg,)))
    if ex:
        raise ValueError(f"Matrícula {reg} já cadastrada ({ex['airline']}"
                         f"{', desativada' if not ex['active'] else ''}). Edite o registro existente.")
    model_id = data.get("model_id") or None
    if model_id:
        m = row(db.execute("SELECT airline FROM aircraft_models WHERE id=?", (model_id,)))
        if not m or m["airline"] != airline:
            raise ValueError("Modelo não pertence à companhia selecionada.")
    ts = now_iso()
    cur = db.execute(
        "INSERT INTO aircraft(airline, registration, model_id, model_source, msn, notes, blocked_airports,"
        " unavailable, active, source, is_demo, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?)",
        (airline, reg, model_id, source, data.get("msn") or None, data.get("notes") or "",
         _clean_blocked(data.get("blocked_airports", "")),
         json.dumps(_validate_unavailable(data.get("unavailable") or [])), source, 1 if is_demo else 0, ts, ts))
    aid = cur.lastrowid
    _history(db, aid, "criado", source=source, note="Dados fictícios de demonstração" if is_demo else None)
    if warn and not is_demo:
        add_flag(db, "aircraft", aid, "matricula_formato", f"{reg}: {warn}", severity="info")
    if not model_id:
        add_flag(db, "aircraft", aid, "modelo_ausente", f"{reg}: modelo não associado.", severity="warning")
    if data.get("config"):
        set_config(db, aid, data["config"], source=source)
    return aid


def update_aircraft(db, aid: int, data: dict):
    cur = row(db.execute("SELECT * FROM aircraft WHERE id=?", (aid,)))
    if not cur:
        raise LookupError("Aeronave não encontrada.")
    changes = {}
    if "registration" in data:
        reg, _w = normalize_registration(data["registration"])
        if not reg:
            raise ValueError("Matrícula inválida.")
        if reg != cur["registration"]:
            if db.execute("SELECT 1 FROM aircraft WHERE registration=? AND id<>?", (reg, aid)).fetchone():
                raise ValueError(f"Matrícula {reg} já cadastrada.")
            changes["registration"] = reg
    if "model_id" in data:
        mid = data["model_id"] or None
        if mid:
            m = row(db.execute("SELECT airline FROM aircraft_models WHERE id=?", (mid,)))
            if not m or m["airline"] != cur["airline"]:
                raise ValueError("Modelo não pertence à companhia da aeronave.")
        if (mid and int(mid)) != cur["model_id"]:
            changes["model_id"] = int(mid) if mid else None
            changes["model_source"] = "manual"
    for k in ("msn", "notes"):
        if k in data and (data[k] or "") != (cur[k] or ""):
            changes[k] = data[k] or ""
    if "blocked_airports" in data:
        b = _clean_blocked(data["blocked_airports"])
        if b != (cur["blocked_airports"] or ""):
            changes["blocked_airports"] = b
    if "unavailable" in data:
        u = json.dumps(_validate_unavailable(data["unavailable"]))
        if u != (cur["unavailable"] or "[]"):
            changes["unavailable"] = u
    for k, v in changes.items():
        if k != "model_source":
            _history(db, aid, "editado", k, cur[k], v)
    if changes:
        changes["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in changes)
        db.execute(f"UPDATE aircraft SET {sets} WHERE id=?", (*changes.values(), aid))
        if changes.get("model_id"):
            db.execute("UPDATE review_flags SET resolved=1, resolution='modelo associado', resolved_at=?"
                       " WHERE entity='aircraft' AND entity_id=? AND code='modelo_ausente' AND resolved=0",
                       (now_iso(), aid))
    if "config" in data:
        set_config(db, aid, data["config"], source="manual")


def set_config(db, aid: int, values: dict, source="manual", raw_values: dict | None = None):
    """values: {field_id: valor}. Valores tri são validados; texto livre aceito para campos text."""
    ts = now_iso()
    for fid_s, val in values.items():
        fid = int(fid_s)
        f = row(db.execute("SELECT * FROM config_fields WHERE id=?", (fid,)))
        if not f:
            continue
        if f["kind"] == "tri":
            if val not in TRI:
                raise ValueError(f"{f['label']}: valor deve ser SIM, NÃO ou DESCONHECIDO.")
        elif f["kind"] == "number" and val not in (None, ""):
            try:
                float(str(val).replace(",", "."))
            except ValueError:
                raise ValueError(f"{f['label']}: valor numérico inválido.")
        elif f["kind"] == "select" and val not in (None, "") and val not in json.loads(f["options"] or "[]"):
            raise ValueError(f"{f['label']}: opção inválida.")
        old = row(db.execute("SELECT value FROM aircraft_config WHERE aircraft_id=? AND field_id=?", (aid, fid)))
        oldv = old["value"] if old else None
        if oldv == val and old is not None:
            if source == "manual":
                db.execute("UPDATE aircraft_config SET source='manual', updated_at=? WHERE aircraft_id=? AND field_id=?",
                           (ts, aid, fid))
            continue
        db.execute(
            "INSERT OR REPLACE INTO aircraft_config(aircraft_id, field_id, value, raw_value, source, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (aid, fid, val, (raw_values or {}).get(fid_s, val), source, ts))
        _history(db, aid, "config", f["label"], oldv, val, source)
        if source == "manual":
            # Edição manual resolve conflitos de importação pendentes daquele campo
            for fl in rows(db.execute(
                    "SELECT id, data FROM review_flags WHERE entity='aircraft' AND entity_id=? AND resolved=0"
                    " AND code='conflito_config'", (aid,))):
                if json.loads(fl["data"] or "{}").get("field_id") == fid:
                    db.execute("UPDATE review_flags SET resolved=1, resolution='editado manualmente', resolved_at=?"
                               " WHERE id=?", (ts, fl["id"]))
    db.execute("UPDATE aircraft SET updated_at=? WHERE id=?", (ts, aid))


def set_active(db, aid: int, active: bool, note=None):
    cur = row(db.execute("SELECT active FROM aircraft WHERE id=?", (aid,)))
    if not cur:
        raise LookupError("Aeronave não encontrada.")
    if bool(cur["active"]) == active:
        return
    db.execute("UPDATE aircraft SET active=?, updated_at=? WHERE id=?", (1 if active else 0, now_iso(), aid))
    _history(db, aid, "ativada" if active else "desativada", note=note)


def bulk_update(db, ids: list, data: dict) -> dict:
    """Altera várias aeronaves de uma vez: companhia, modelo e/ou um valor de configuração."""
    ids = [int(i) for i in ids or []]
    if not ids:
        raise ValueError("Selecione ao menos uma aeronave.")
    new_airline = (data.get("airline") or "").upper() or None
    if new_airline and not db.execute("SELECT 1 FROM airlines WHERE code=?", (new_airline,)).fetchone():
        raise ValueError("Companhia inválida.")
    model = None
    if data.get("model_id"):
        model = row(db.execute("SELECT * FROM aircraft_models WHERE id=?", (int(data["model_id"]),)))
        if not model:
            raise ValueError("Modelo não encontrado.")
    cfg = {str(k): v for k, v in (data.get("config") or {}).items() if v not in (None, "")}
    ts = now_iso()
    changed = 0
    for aid in ids:
        a = row(db.execute("SELECT * FROM aircraft WHERE id=?", (aid,)))
        if not a:
            continue
        airline = new_airline or a["airline"]
        if new_airline and new_airline != a["airline"]:
            db.execute("UPDATE aircraft SET airline=?, updated_at=? WHERE id=?", (new_airline, ts, aid))
            _history(db, aid, "editado", "airline", a["airline"], new_airline, note="Alteração em lote")
            m_old = row(db.execute("SELECT airline FROM aircraft_models WHERE id=?", (a["model_id"],))) \
                if a["model_id"] else None
            if m_old and m_old["airline"] != new_airline and not model:
                db.execute("UPDATE aircraft SET model_id=NULL WHERE id=?", (aid,))
                _history(db, aid, "editado", "model_id", a["model_id"], None, note="Modelo era de outra companhia")
            changed += 1
        if model:
            if model["airline"] != airline:
                raise ValueError(f"O modelo {model['name']} é de {model['airline']}, mas {a['registration']} "
                                 f"é de {airline}.")
            if model["id"] != a["model_id"]:
                db.execute("UPDATE aircraft SET model_id=?, model_source='manual', updated_at=? WHERE id=?",
                           (model["id"], ts, aid))
                _history(db, aid, "editado", "model_id", a["model_id"], model["id"], note="Alteração em lote")
                db.execute("UPDATE review_flags SET resolved=1, resolution='modelo associado', resolved_at=?"
                           " WHERE entity='aircraft' AND entity_id=? AND code='modelo_ausente' AND resolved=0",
                           (ts, aid))
                changed += 1
        if cfg:
            set_config(db, aid, cfg, source="manual")
            changed += 1
    return {"updated": len(ids), "changes": changed}


def history(db, aid: int) -> list[dict]:
    return rows(db.execute("SELECT * FROM aircraft_history WHERE aircraft_id=? ORDER BY id DESC", (aid,)))


def export_csv(db, args: dict) -> str:
    items = list_aircraft(db, {**args, "status": args.get("status", "all")})
    fields = fields_for(db)
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["companhia", "matricula", "modelo", "tipo_icao", "familia", "msn", "ativa", "ficticia",
                *[f["label"] + (f" [{f['airline'] or 'todas'}/{f['family'] or 'todas'}]") for f in fields],
                "aeroportos_restritos", "indisponibilidades", "observacoes"])
    for a in items:
        w.writerow([a["airline"], a["registration"], a["model_name"] or "", a["icao_type"] or "",
                    a["family"] or "", a["msn"] or "", "SIM" if a["active"] else "NÃO",
                    "SIM" if a["is_demo"] else "NÃO",
                    *[(a["config"].get(str(f["id"])) or {}).get("value", "") if field_applies(
                        f, a["airline"], a["family"]) else "n/a" for f in fields],
                    a["blocked_airports"] or "",
                    "; ".join(f"{u['from']}..{u['to']} {u.get('reason', '')}".strip() for u in a["unavailable"]),
                    a["notes"] or ""])
    return "﻿" + out.getvalue()


# ---------------------------------------------------------------- importação de listas
HEADER_HINTS = {
    "registration": {"MATRICULA", "MATRICULAS", "MAT", "REG", "REGISTRO", "REGISTRATION", "PREFIXO", "TAIL", "ANV"},
    "model": {"MODELO", "MODEL", "AERONAVE", "EQUIPAMENTO", "EQUIP", "ACFT", "AIRCRAFT", "VERSAO"},
    "icao_type": {"TIPO", "TYPE", "ICAO", "TIPOICAO", "DESIGNADOR"},
    "msn": {"MSN", "SERIAL", "NS", "NUMERODESERIE", "SN"},
    "notes": {"OBS", "OBSERVACOES", "OBSERVACAO", "NOTAS", "NOTES", "COMENTARIO"},
    "airline": {"CIA", "COMPANHIA", "AIRLINE", "OPERADOR", "EMPRESA"},
    "equipment_list": {"EQUIPAMENTOS", "OPCIONAIS", "CONFIG", "CONFIGURACAO", "FEATURES"},
}


def _split_line(line: str, delim: str | None) -> list[str]:
    if delim == "ws2":
        return [c.strip() for c in re.split(r"\s{2,}|\t", line.strip())]
    if delim == "ws":
        return line.split()
    return [c.strip() for c in next(csv.reader([line], delimiter=delim))]


def filename_attributes(filename: str) -> list[str]:
    """Atributos comuns sugeridos pelo nome do arquivo.

    Ex.: 'PR-GXA - Collins (27K) SFP 2.0 ISFD.txt' -> ['Collins', '27K', 'SFP 2.0', 'ISFD'].
    Matrículas no nome são descartadas. São só sugestões: o usuário confirma na pré-visualização.
    """
    base = os.path.splitext(os.path.basename(filename or ""))[0]
    chunks = re.split(r"\s*(?:_{2,}|\s-\s|[()\[\],;+|])\s*", base)
    out: list[str] = []
    for ch in chunks:
        words = [w for w in re.split(r"[_\s]+", ch.strip()) if w and w != "-"]
        i = 0
        while i < len(words):
            w = words[i]
            if i + 1 < len(words) and re.fullmatch(r"v?\d+(?:[.,]\d+)+", words[i + 1], re.I):
                w = f"{w} {words[i + 1]}"
                i += 1
            i += 1
            if looks_like_reg(w) or BR_REG.match(w.upper().replace(" ", "")):
                continue
            if w not in out:
                out.append(w)
    return out[:12]


def parse_fleet_table(data: bytes, filename: str = "") -> dict:
    from .rpl_parser import decode_bytes
    text, enc = decode_bytes(data)
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [(i + 1, l) for i, l in enumerate(raw_lines)
             if l.strip() and not l.strip().startswith(("#", "//")) and not re.fullmatch(r"[-=_\s|+]+", l)]
    notes = []
    if not lines:
        raise ValueError("Arquivo vazio.")

    # Lista simples: uma matrícula por linha (a configuração costuma vir no nome do arquivo).
    single = [(n, l.strip()) for n, l in lines if len(l.split()) == 1 and looks_like_reg(l.strip())]
    if len(single) >= max(1, int(len(lines) * 0.8)):
        rows_ = [{"line_no": n, "raw": raw_lines[n - 1], "cells": {"Matrícula": v}} for n, v in single]
        ign = len(lines) - len(single)
        attrs = filename_attributes(filename)
        notes.append("Lista só com matrículas: não há modelo nem equipamentos por linha. Escolha o modelo e "
                     "confirme os atributos comuns a todas as aeronaves da lista"
                     + (" (sugeridos pelo nome do arquivo)." if attrs else "."))
        if ign:
            notes.append(f"{ign} linha(s) que não parecem matrícula foram ignoradas.")
        return {"format": "lista de matrículas", "delimiter": "uma por linha", "encoding": enc,
                "columns": ["Matrícula"], "rows": rows_, "title": "", "notes": notes, "list_attributes": attrs}

    # Formato em blocos: matrícula numa linha, "CHAVE: VALOR" nas seguintes.
    kv_lines = sum(1 for _, l in lines if re.match(r"^\s*[^:=]{1,30}\s*[:=]\s*\S", l))
    reg_only = sum(1 for _, l in lines if looks_like_reg(l.strip().split()[0].rstrip(":")) and len(l.split()) <= 3)
    if kv_lines > len(lines) * 0.5 and reg_only >= 1:
        return _parse_blocks(lines, enc)

    # Delimitador mais consistente
    best, best_score = None, 0.0
    sample = [l for _, l in lines[:80]]
    for d in ("\t", ";", "|", ","):
        counts = [l.count(d) for l in sample]
        nz = [c for c in counts if c > 0]
        if len(nz) < len(sample) * 0.6:
            continue
        mode = max(set(nz), key=nz.count)
        score = nz.count(mode) / len(sample) * (1 + mode / 10)
        if score > best_score:
            best, best_score = d, score
    if best is None:
        ws2 = [len(_split_line(l, "ws2")) for l in sample]
        mode2 = max(set(ws2), key=ws2.count)
        best = "ws2" if mode2 > 1 and ws2.count(mode2) >= len(sample) * 0.6 else "ws"
    table = [(n, _split_line(l, best)) for n, l in lines]
    if best == "|":
        table = [(n, [c for c in cells if c != ""] if cells and cells[0] == "" else cells) for n, cells in table]

    # Cabeçalho: primeira linha sem matrícula e com texto
    header = None
    title_lines = []
    for idx, (n, cells) in enumerate(table[:10]):
        has_reg = any(normalize_registration(c)[0] and BR_REG.match(c.replace(" ", "").upper()) for c in cells)
        if has_reg:
            break
        hints = sum(1 for c in cells for s in HEADER_HINTS.values() if norm_text(c) in s)
        if hints or (len(cells) > 1 and all(re.search(r"[A-Za-z]", c) for c in cells if c)):
            header = (idx, cells)
            if hints:
                break
        else:
            title_lines.append(" ".join(cells))
    start = header[0] + 1 if header else 0
    data_rows = table[start:]
    width = max([len(c) for _, c in data_rows] + [len(header[1]) if header else 0])
    cols = list(header[1]) if header else []
    cols += [f"Coluna {i + 1}" for i in range(len(cols), width)]
    cols = [c or f"Coluna {i + 1}" for i, c in enumerate(cols)]

    # Expande células "CHAVE=VALOR"/"CHAVE: VALOR" em colunas próprias
    out_rows = []
    extra_cols: list[str] = []
    for n, cells in data_rows:
        d = {}
        for i, c in enumerate(cells):
            m = re.match(r"^([A-Za-z][A-Za-z0-9/_ .-]{0,24})\s*[:=]\s*(.*)$", c)
            named_col = header is not None and i < len(header[1]) and header[1][i]
            if m and best != "ws" and not named_col and not normalize_registration(c)[0]:
                k = m.group(1).strip()
                if k not in extra_cols and k not in cols:
                    extra_cols.append(k)
                d[k] = m.group(2).strip()
            else:
                d[cols[i] if i < len(cols) else f"Coluna {i + 1}"] = c
        out_rows.append({"line_no": n, "raw": raw_lines[n - 1], "cells": d})
    all_cols = cols + extra_cols
    if best == "ws":
        notes.append("Colunas separadas por espaço simples: confira se valores com espaços foram divididos.")
    if not header:
        notes.append("Cabeçalho não identificado: as colunas receberam nomes genéricos; mapeie manualmente.")
    return {"format": "tabela", "delimiter": {"\t": "TAB", ";": ";", "|": "|", ",": ",", "ws2": "espaços",
                                              "ws": "espaço"}[best],
            "encoding": enc, "columns": all_cols, "rows": out_rows, "title": " ".join(title_lines)[:200],
            "notes": notes}


def _parse_blocks(lines, enc) -> dict:
    recs, cur, cols = [], None, ["Matrícula"]
    for n, l in lines:
        s = l.strip()
        first = s.split()[0]
        m = re.match(r"^([^:=]{1,30}?)\s*[:=]\s*(.*)$", s)
        if m and norm_text(m.group(1)) in HEADER_HINTS["registration"]:
            reg2, _ = normalize_registration(m.group(2).split()[0] if m.group(2).split() else "")
            if reg2:
                cur = {"line_no": n, "raw": l, "cells": {"Matrícula": reg2}}
                recs.append(cur)
                continue
        cand = first.rstrip(":")
        reg = cand if looks_like_reg(cand) and (not m or m.group(1).strip() == cand) else None
        if reg:
            cur = {"line_no": n, "raw": l, "cells": {"Matrícula": reg}}
            rest = s[len(first):].strip(" -:")
            if rest:
                cur["cells"]["Modelo"] = rest
                if "Modelo" not in cols:
                    cols.append("Modelo")
            recs.append(cur)
        elif m and cur is not None:
            k = m.group(1).strip()
            cur["cells"][k] = m.group(2).strip()
            cur["raw"] += "\n" + l
            if k not in cols:
                cols.append(k)
    return {"format": "blocos", "delimiter": "CHAVE: VALOR", "encoding": enc, "columns": cols, "rows": recs,
            "title": "", "notes": ["Formato em blocos detectado (matrícula seguida de linhas CHAVE: VALOR)."]}


def suggest_mapping(db, table: dict, airline: str | None) -> dict:
    fields = fields_for(db, airline) if airline else fields_for(db)
    by_norm = {}
    for f in fields:
        by_norm.setdefault(norm_text(f["key"]), f)
        by_norm.setdefault(norm_text(f["label"]), f)
    mapping = {}
    used = set()
    for c in table["columns"]:
        nc = norm_text(c)
        values = [r["cells"].get(c, "") for r in table["rows"]]
        target = "ignore"
        for t, hints in HEADER_HINTS.items():
            if nc in hints and t not in used:
                target = t
                break
        if target == "ignore" and nc in by_norm:
            target = f"field:{by_norm[nc]['id']}"
        if target == "ignore" and values:
            nonempty = [v for v in values if str(v).strip()]
            regs = sum(1 for v in nonempty if BR_REG.match(str(v).replace(" ", "").upper()))
            tri = sum(1 for v in values if normalize_tri(v)[1] is None
                      or str(v).strip().upper() in TRI_AMBIGUOUS)
            if nonempty and regs >= len(nonempty) * 0.8 and "registration" not in used:
                target = "registration"
            elif nonempty and sum(1 for v in nonempty if guess_type(v)[0]) >= len(nonempty) * 0.8 \
                    and "model" not in used:
                target = "model"
            elif tri >= len(values) * 0.9 and nc:
                target = "new_field"
        if target in HEADER_HINTS:
            used.add(target)
        mapping[c] = target
    return mapping


def _tok_set(text: str) -> set[str]:
    return {norm_text(t) for t in re.split(r"[,;\s]+", text or "") if t.strip()}


def apply_fleet_import(db, table: dict, mapping: dict, airline: str, filename: str, data: bytes,
                       storage_dir: str, options: dict) -> dict:
    if not db.execute("SELECT 1 FROM airlines WHERE code=?", (airline,)).fetchone():
        raise ValueError("Selecione uma companhia válida.")
    if "registration" not in mapping.values():
        raise ValueError("Mapeie uma coluna como Matrícula.")
    absent_means_no = bool(options.get("absent_means_no"))
    deactivate_missing = bool(options.get("deactivate_missing"))

    # Modelo para as linhas sem modelo (confirmado pelo usuário na pré-visualização)
    default_model = None
    if options.get("default_model_id"):
        default_model = row(db.execute("SELECT * FROM aircraft_models WHERE id=?", (int(options["default_model_id"]),)))
        if not default_model or default_model["airline"] != airline:
            raise ValueError("O modelo escolhido não pertence à companhia da lista.")

    # Atributos comuns a todas as linhas (ex.: configuração indicada no nome do arquivo)
    attrs = [a for a in (options.get("list_attributes") or []) if (a.get("label") or "").strip()
             and a.get("kind") in ("tri", "text")]
    if attrs:
        mapping = dict(mapping)
        table = {**table, "rows": [{**r, "cells": dict(r["cells"])} for r in table["rows"]]}
        for i, a in enumerate(attrs):
            label = a["label"].strip()
            key = norm_text(label)
            ex_f = None
            if a.get("field_id"):
                ex_f = row(db.execute("SELECT * FROM config_fields WHERE id=?", (int(a["field_id"]),)))
            if not ex_f:
                ex_f = next((f for f in fields_for(db, airline, include_inactive=True)
                             if norm_text(f["key"]) == key or norm_text(f["label"]) == key), None)
            if ex_f and ex_f["kind"] == "tri" and a["kind"] == "text":
                ex_f = None  # nome já usado por um campo SIM/NÃO: cria o campo de texto separado
            fid = ex_f["id"] if ex_f else create_field(db, {
                "label": label, "kind": a["kind"], "airline": airline,
                "key": label + ("_TXT" if a["kind"] == "text" and any(
                    norm_text(f["key"]) == key for f in fields_for(db, airline, include_inactive=True)) else ""),
                "description": f"Criado na importação da lista '{filename}'"})["id"]
            col = f"__atributo_{i}"
            mapping[col] = f"field:{fid}"
            val = (a.get("value") or "").strip() if a["kind"] == "text" else (a.get("value") or "SIM")
            for r in table["rows"]:
                r["cells"][col] = val

    # cria campos novos solicitados
    for col, tgt in list(mapping.items()):
        if tgt == "new_field":
            label = col.strip()
            key = norm_text(label)
            ex = next((f for f in fields_for(db, airline, include_inactive=True)
                       if norm_text(f["key"]) == key or norm_text(f["label"]) == key), None)
            fid = ex["id"] if ex else create_field(db, {"label": label, "kind": "tri", "airline": airline})["id"]
            mapping[col] = f"field:{fid}"
    field_cols = {col: int(t.split(":")[1]) for col, t in mapping.items() if t.startswith("field:")}
    fields_by_id = {f["id"]: f for f in fields_for(db, include_inactive=True)}
    eq_col = next((c for c, t in mapping.items() if t == "equipment_list"), None)
    col_of = {t: c for c, t in mapping.items() if t in HEADER_HINTS}

    ts = now_iso()
    sha = hashlib.sha256(data).hexdigest()
    cur = db.execute("INSERT INTO fleet_imports(airline, filename, sha256, mapping, imported_at) VALUES (?,?,?,?,?)",
                     (airline, filename, sha, json.dumps(mapping, ensure_ascii=False), ts))
    imp_id = cur.lastrowid
    os.makedirs(storage_dir, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "frota.txt")
    path = os.path.join(storage_dir, f"frota_{imp_id:04d}_{safe}")
    with open(path, "wb") as fh:
        fh.write(data)

    st = {"created": 0, "updated": 0, "unchanged": 0, "conflicts": 0, "errors": 0, "duplicates_in_file": 0,
          "not_applicable": 0, "flags": 0, "missing_from_list": 0, "rows": len(table["rows"]), "messages": []}
    seen: dict[str, dict] = {}

    def msg(line, text):
        if len(st["messages"]) < 300:
            st["messages"].append({"line": line, "message": text})

    for r in table["rows"]:
        cells, ln = r["cells"], r["line_no"]
        reg, warn = normalize_registration(cells.get(col_of.get("registration"), ""))
        if not reg:
            st["errors"] += 1
            msg(ln, f"Matrícula ausente ou inválida: '{cells.get(col_of.get('registration'), '')}'. Linha ignorada.")
            continue
        if reg in seen:
            st["duplicates_in_file"] += 1
            if seen[reg] != cells:
                msg(ln, f"{reg} repetida no arquivo com dados diferentes; mantida a primeira ocorrência.")
                ex = row(db.execute("SELECT id FROM aircraft WHERE registration=?", (reg,)))
                if ex:
                    add_flag(db, "aircraft", ex["id"], "duplicada_lista",
                             f"{reg} aparece mais de uma vez na lista '{filename}' com dados diferentes.",
                             data={"import_id": imp_id, "line": ln})
                    st["flags"] += 1
            continue
        seen[reg] = cells
        row_airline = airline
        if col_of.get("airline"):
            val = (cells.get(col_of["airline"]) or "").strip().upper()
            if val and val != airline:
                msg(ln, f"{reg}: coluna companhia = '{val}', importando como {airline} e sinalizando.")
        model_txt = (cells.get(col_of.get("model"), "") or "").strip() if col_of.get("model") else ""
        type_txt = (cells.get(col_of.get("icao_type"), "") or "").strip().upper() if col_of.get("icao_type") else ""
        icao, model_warn = (None, None)
        if type_txt:
            icao, model_warn = guess_type(type_txt)
        if not icao and model_txt:
            icao, model_warn = guess_type(model_txt)
        model_id = None
        if icao:
            name = model_txt or catalog.type_name(icao)
            m = row(db.execute("SELECT id FROM aircraft_models WHERE airline=? AND icao_type=? AND name=?",
                               (row_airline, icao, name)))
            if not m:
                m = row(db.execute("SELECT id FROM aircraft_models WHERE airline=? AND icao_type=? AND name=?",
                                   (row_airline, icao, catalog.type_name(icao)))) if not model_txt else None
            model_id = m["id"] if m else db.execute(
                "INSERT INTO aircraft_models(airline,name,icao_type,family,source,created_at) VALUES (?,?,?,?,?,?)",
                (row_airline, name, icao, catalog.type_family(icao), "import", ts)).lastrowid
        family = catalog.type_family(icao) if icao else None
        if not model_id and default_model:
            model_id, family, model_warn = default_model["id"], default_model["family"], None

        ex = row(db.execute(
            "SELECT a.*, m.family AS family FROM aircraft a LEFT JOIN aircraft_models m ON m.id=a.model_id"
            " WHERE a.registration=?", (reg,)))
        changed = False
        if not ex:
            aid = db.execute(
                "INSERT INTO aircraft(airline, registration, model_id, model_source, msn, notes, active, source,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,1,'import',?,?)",
                (row_airline, reg, model_id, "import", cells.get(col_of.get("msn"), "") if col_of.get("msn") else None,
                 cells.get(col_of.get("notes"), "") if col_of.get("notes") else "", ts, ts)).lastrowid
            _history(db, aid, "criado", source="import", note=f"Lista '{filename}' (importação de frota #{imp_id})")
            st["created"] += 1
            if warn:
                add_flag(db, "aircraft", aid, "matricula_formato", f"{reg}: {warn}", severity="info")
                st["flags"] += 1
            if not model_id:
                add_flag(db, "aircraft", aid, "modelo_ausente",
                         f"{reg}: {model_warn or 'modelo não informado na lista'}", data={"model_text": model_txt})
                st["flags"] += 1
            ex_family = family
        else:
            aid = ex["id"]
            ex_family = ex["family"]
            if ex["airline"] != row_airline:
                add_flag(db, "aircraft", aid, "cia_divergente",
                         f"{reg} está cadastrada em {ex['airline']}, mas aparece na lista de {row_airline}.",
                         severity="error", data={"import_id": imp_id, "line": ln})
                st["conflicts"] += 1
                st["flags"] += 1
                msg(ln, f"{reg}: pertence a {ex['airline']}; dados não alterados.")
                db.execute("INSERT INTO aircraft_raw(aircraft_id, import_id, line_no, raw_text, created_at)"
                           " VALUES (?,?,?,?,?)", (aid, imp_id, ln, r["raw"], ts))
                continue
            if model_id and model_id != ex["model_id"]:
                if ex["model_id"] and ex["model_source"] == "manual":
                    add_flag(db, "aircraft", aid, "conflito_modelo",
                             f"{reg}: lista indica modelo '{model_txt or icao}', diferente do cadastrado manualmente.",
                             data={"model_id": model_id, "import_id": imp_id})
                    st["conflicts"] += 1
                    st["flags"] += 1
                else:
                    old = ex["model_id"]
                    db.execute("UPDATE aircraft SET model_id=?, model_source='import', updated_at=? WHERE id=?",
                               (model_id, ts, aid))
                    _history(db, aid, "editado", "model_id", old, model_id, "import", f"Lista '{filename}'")
                    db.execute("UPDATE review_flags SET resolved=1, resolution='modelo associado na importação',"
                               " resolved_at=? WHERE entity='aircraft' AND entity_id=? AND code='modelo_ausente'"
                               " AND resolved=0", (ts, aid))
                    ex_family = family
                    changed = True
            if not ex["active"]:
                msg(ln, f"{reg} está desativada; dados atualizados, mas a aeronave continua desativada.")

        # configurações
        eq_tokens = _tok_set(cells.get(eq_col, "")) if eq_col else None
        targets: dict[int, tuple[str, str]] = {}
        for col, fid in field_cols.items():
            f = fields_by_id.get(fid)
            if not f:
                continue
            rawv = cells.get(col, "")
            if f["kind"] == "tri":
                v, w = normalize_tri(rawv)
                if w:
                    add_flag(db, "aircraft", aid, "valor_ambiguo", f"{reg} / {f['label']}: {w}", severity="info",
                             data={"field_id": fid, "raw": rawv, "import_id": imp_id})
                    st["flags"] += 1
                if v == "DESCONHECIDO" and absent_means_no and not str(rawv).strip():
                    v = "NÃO"
            else:
                v = str(rawv).strip()
            targets[fid] = (v, str(rawv))
        if eq_tokens is not None:
            for f in fields_by_id.values():
                if f["kind"] != "tri" or not f["active"] or f["id"] in targets:
                    continue
                if f["airline"] not in (None, row_airline):
                    continue
                if norm_text(f["key"]) in eq_tokens or norm_text(f["label"]) in eq_tokens:
                    targets[f["id"]] = ("SIM", cells.get(eq_col, ""))
                elif absent_means_no:
                    targets[f["id"]] = ("NÃO", f"(ausente em '{cells.get(eq_col, '')}')")

        for fid, (v, rawv) in targets.items():
            f = fields_by_id[fid]
            if not field_applies(f, row_airline, ex_family):
                st["not_applicable"] += 1
                continue
            curc = row(db.execute("SELECT * FROM aircraft_config WHERE aircraft_id=? AND field_id=?", (aid, fid)))
            if not curc:
                db.execute("INSERT INTO aircraft_config(aircraft_id, field_id, value, raw_value, source, updated_at)"
                           " VALUES (?,?,?,?,'import',?)", (aid, fid, v, rawv, ts))
                if ex:
                    _history(db, aid, "config", f["label"], None, v, "import", f"Lista '{filename}'")
                    changed = True
                continue
            if curc["value"] == v:
                continue
            known_new = v not in ("", "DESCONHECIDO")
            known_cur = curc["value"] not in (None, "", "DESCONHECIDO")
            if curc["source"] == "manual":
                if known_new:
                    add_flag(db, "aircraft", aid, "conflito_config",
                             f"{reg} / {f['label']}: lista diz {v}, cadastro manual diz {curc['value']}.",
                             data={"field_id": fid, "import_value": v, "current": curc["value"], "raw": rawv,
                                   "import_id": imp_id})
                    st["conflicts"] += 1
                    st["flags"] += 1
                continue
            if not known_new and known_cur:
                continue  # lista nova não informa; não apaga valor conhecido
            db.execute("UPDATE aircraft_config SET value=?, raw_value=?, source='import', updated_at=?"
                       " WHERE aircraft_id=? AND field_id=?", (v, rawv, ts, aid, fid))
            _history(db, aid, "config", f["label"], curc["value"], v, "import", f"Lista '{filename}'")
            if known_cur and known_new:
                add_flag(db, "aircraft", aid, "valor_alterado_lista",
                         f"{reg} / {f['label']}: mudou de {curc['value']} para {v} na lista '{filename}'.",
                         severity="info", data={"field_id": fid})
                st["flags"] += 1
            changed = True

        for k in ("msn", "notes"):
            if ex and col_of.get(k):
                v = (cells.get(col_of[k]) or "").strip()
                if v and v != (ex[k] or ""):
                    if ex[k]:
                        add_flag(db, "aircraft", aid, f"conflito_{k}",
                                 f"{reg}: lista traz {k.upper()} '{v}', cadastro tem '{ex[k]}'.", severity="info")
                        st["flags"] += 1
                    else:
                        db.execute(f"UPDATE aircraft SET {k}=? WHERE id=?", (v, aid))
                        _history(db, aid, "editado", k, "", v, "import")
                        changed = True
        db.execute("INSERT INTO aircraft_raw(aircraft_id, import_id, line_no, raw_text, created_at) VALUES (?,?,?,?,?)",
                   (aid, imp_id, ln, r["raw"], ts))
        if ex:
            st["updated" if changed else "unchanged"] += 1

    missing = rows(db.execute("SELECT id, registration FROM aircraft WHERE airline=? AND active=1 AND is_demo=0",
                              (airline,)))
    for a in missing:
        if a["registration"] not in seen:
            st["missing_from_list"] += 1
            if deactivate_missing:
                set_active(db, a["id"], False, note=f"Ausente na lista '{filename}'")
    st["imported_ids"] = imp_id
    db.execute("UPDATE fleet_imports SET stored_path=?, stats=? WHERE id=?",
               (path, json.dumps({k: v for k, v in st.items() if k != "messages"}), imp_id))
    return st


def resolve_flag(db, flag_id: int, action: str) -> dict:
    fl = row(db.execute("SELECT * FROM review_flags WHERE id=?", (flag_id,)))
    if not fl:
        raise LookupError("Pendência não encontrada.")
    data = json.loads(fl["data"] or "{}")
    ts = now_iso()
    if action == "accept_import" and fl["code"] == "conflito_config":
        db.execute("UPDATE aircraft_config SET value=?, raw_value=?, source='import', updated_at=?"
                   " WHERE aircraft_id=? AND field_id=?",
                   (data["import_value"], data.get("raw"), ts, fl["entity_id"], data["field_id"]))
        f = row(db.execute("SELECT label FROM config_fields WHERE id=?", (data["field_id"],)))
        _history(db, fl["entity_id"], "config", f["label"] if f else data["field_id"], data.get("current"),
                 data["import_value"], "revisao", "Valor da lista aceito na revisão")
        res = "valor da lista aceito"
    elif action == "accept_import" and fl["code"] == "conflito_modelo":
        old = row(db.execute("SELECT model_id FROM aircraft WHERE id=?", (fl["entity_id"],)))
        db.execute("UPDATE aircraft SET model_id=?, model_source='import', updated_at=? WHERE id=?",
                   (data["model_id"], ts, fl["entity_id"]))
        _history(db, fl["entity_id"], "editado", "model_id", old["model_id"] if old else None, data["model_id"],
                 "revisao", "Modelo da lista aceito na revisão")
        res = "modelo da lista aceito"
    else:
        res = "mantido valor atual" if action == "keep_current" else "marcado como revisado"
    db.execute("UPDATE review_flags SET resolved=1, resolution=?, resolved_at=? WHERE id=?", (res, ts, flag_id))
    return {"resolution": res}


# ---------------------------------------------------------------- demonstração
DEMO_FLEET = [
    # (cia, matrícula fictícia, tipo, {campo: valor})
    ("TAM", "FIC-T01", "A320", {"ISIS": "SIM", "ANALOGIC": "NÃO", "DDRMI": "SIM", "DCDU": "SIM", "ROW_ROPS": "NÃO",
                                "DRAMS": "DESCONHECIDO"}),
    ("TAM", "FIC-T02", "A320", {"ISIS": "NÃO", "ANALOGIC": "SIM", "DDRMI": "DESCONHECIDO", "DCDU": "NÃO",
                                "ROW_ROPS": "NÃO", "DRAMS": "SIM"}),
    ("TAM", "FIC-T03", "A321", {"ISIS": "SIM", "ANALOGIC": "NÃO", "DDRMI": "SIM", "DCDU": "SIM", "ROW_ROPS": "SIM",
                                "DRAMS": "NÃO"}),
    ("TAM", "FIC-T04", "A319", {"ISIS": "DESCONHECIDO", "ANALOGIC": "SIM", "DDRMI": "NÃO"}),
    ("GLO", "FIC-G01", "B738", {}), ("GLO", "FIC-G02", "B38M", {}), ("GLO", "FIC-G03", "B737", {}),
    ("AZU", "FIC-A01", "A20N", {}), ("AZU", "FIC-A02", "E295", {}), ("AZU", "FIC-A03", "AT76", {}),
]


def seed_demo(db) -> int:
    n = 0
    for airline, reg, icao, cfg in DEMO_FLEET:
        if db.execute("SELECT 1 FROM aircraft WHERE registration=?", (reg,)).fetchone():
            continue
        mid = catalog.ensure_model(db, airline, icao, source="demo")
        fields = {f["key"]: f["id"] for f in fields_for(db, airline)}
        aid = create_aircraft(db, {"airline": airline, "registration": reg, "model_id": mid,
                                   "notes": "DADOS FICTÍCIOS – aeronave de demonstração, não existe."},
                              source="demo", is_demo=True)
        vals = {str(fields[k]): v for k, v in cfg.items() if k in fields}
        if vals:
            set_config(db, aid, vals, source="demo")
        n += 1
    return n


def remove_demo(db) -> int:
    ids = [r["id"] for r in db.execute("SELECT id FROM aircraft WHERE is_demo=1")]
    for i in ids:
        db.execute("UPDATE schedule_legs SET aircraft_id=NULL WHERE aircraft_id=?", (i,))
        db.execute("UPDATE schedules SET aircraft_id=NULL WHERE aircraft_id=?", (i,))
        db.execute("DELETE FROM review_flags WHERE entity='aircraft' AND entity_id=?", (i,))
        db.execute("DELETE FROM aircraft WHERE id=?", (i,))
    return len(ids)
