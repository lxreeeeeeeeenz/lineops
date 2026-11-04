"""Importador do backup do AeroScale (.zip, pasta ou .db). Direção única: AeroScale -> LineOps.

- O backup é aberto numa cópia temporária; o original nunca é alterado.
- Cada perna é identificada pelo `uid` do AeroScale (reimportar é idempotente).
- Valores são guardados exatamente como vieram (inclusive `data_origin`, ex.: OBSERVADO).
- Nada é inventado: sem matrícula no AeroScale => perna sem matrícula.
- Pernas que sumiram de uma escala reimportada são marcadas como removidas (histórico preservado).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from . import rules
from .aeroscale import catalog
from .db import audit, jloads, now_iso, tx

LEG_HASH_FIELDS = ("seq", "airline", "flight_number", "type_icao", "planned_type", "adep", "ades", "dep_utc",
                   "arr_utc", "eet_min", "data_origin", "activity", "reserve_uid", "registration", "route",
                   "cruise_fl", "speed", "remarks")
DEMO_PREFIX = "FIC-"


class ImportError_(Exception):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _open_source(src: Path, tmp: Path):
    """Devolve (conexão sqlite sobre cópia, manifest, sha256 do arquivo de origem)."""
    src = Path(src)
    if not src.exists():
        raise ImportError_(f"Arquivo não encontrado: {src}")
    manifest = {}
    if src.is_dir():
        db_src = src / "aeroscale.db"
        mf = src / "manifest.json"
        if mf.exists():
            manifest = jloads(mf.read_text(encoding="utf-8"), {})
        sha = _sha256(db_src) if db_src.exists() else ""
    elif zipfile.is_zipfile(src):
        sha = _sha256(src)
        with zipfile.ZipFile(src) as z:
            names = {Path(n).name: n for n in z.namelist() if not n.endswith("/")}
            if "aeroscale.db" not in names:
                raise ImportError_("O .zip não contém aeroscale.db — não parece um backup do AeroScale.")
            for want in ("aeroscale.db", "manifest.json"):     # extrai só estes; nomes do zip nunca viram caminho
                if want in names:
                    with z.open(names[want]) as fi, open(tmp / want, "wb") as fo:
                        shutil.copyfileobj(fi, fo)
        db_src = tmp / "aeroscale.db"
        if (tmp / "manifest.json").exists():
            manifest = jloads((tmp / "manifest.json").read_text(encoding="utf-8"), {})
    else:
        db_src, sha = src, _sha256(src)
    if not db_src.exists():
        raise ImportError_("aeroscale.db não encontrado na origem.")
    work = tmp / "work.db"
    shutil.copyfile(db_src, work)
    for ext in ("-wal", "-shm"):                          # WAL junto do original, se houver
        side = Path(str(db_src) + ext)
        if side.exists() and side.stat().st_size:
            shutil.copyfile(side, Path(str(work) + ext))
    c = sqlite3.connect(work)
    c.row_factory = sqlite3.Row
    try:
        c.execute("SELECT 1 FROM schedule_legs LIMIT 1")
    except sqlite3.DatabaseError as e:
        raise ImportError_(f"Banco do AeroScale ilegível ou incompatível: {e}")
    return c, manifest, sha


def _apmap(c) -> dict:
    """Mapa de aeroportos do AeroScale. Num backup sem a tabela de ajustes manuais, usa só o embutido dele."""
    try:
        return catalog.airport_map(c)
    except sqlite3.Error:
        from .aeroscale.airport_geo import AIRPORT_GEO
        from .aeroscale.airports import BUILTIN_AIRPORTS
        out = {i: {"icao": i, "iata": v[0], "name": v[1], "city": v[2], "uf": v[3], "tz": v[4], "source": "embutido"}
               for i, v in BUILTIN_AIRPORTS.items()}
        for i, d in out.items():
            g = AIRPORT_GEO.get(i)
            if g:
                d.update(lat=g[0], lon=g[1], elev_ft=g[2], rwy_m=g[3])
        return out


def _local_date(apmap: dict, dep_utc: str, adep: str) -> str:
    """Data local da partida, com o fuso que o próprio AeroScale usa (catalog.airport_tz + airports.to_local)."""
    from datetime import datetime, timezone

    from .aeroscale.airports import to_local
    try:
        d = datetime.strptime(dep_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return (dep_utc or "")[:10]
    tz = catalog.airport_tz(apmap, (adep or "").upper())
    return (to_local(d, tz) if tz else d).strftime("%Y-%m-%d")


def _skey(s) -> str:
    return f"{s['airline']}|{s['kind']}|{s['start_date']}|{s['end_date']}"


def _hash(d: dict) -> str:
    return hashlib.sha1(json.dumps([d.get(k) for k in LEG_HASH_FIELDS], ensure_ascii=False).encode()).hexdigest()


def _norm_reg(r):
    r = (r or "").strip().upper()
    return r or None


def read_source(src: Path, tmp: Path) -> dict:
    """Lê o backup e devolve estruturas normalizadas (ainda sem gravar nada)."""
    c, manifest, sha = _open_source(src, tmp)
    try:
        meta = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM meta")} if _has(c, "meta") else {}
        airlines = [dict(r) for r in c.execute("SELECT code, iata, name FROM airlines")] if _has(c, "airlines") else []
        schedules = [dict(r) for r in c.execute("SELECT * FROM schedules ORDER BY id")]
        for sc in schedules:
            sc["diagnostics"] = sc.get("diagnostics") or "[]"
        sched_by_id = {s["id"]: s for s in schedules}
        reserves = [dict(r) for r in c.execute("SELECT * FROM schedule_reserves ORDER BY id")] if _has(c, "schedule_reserves") else []
        res_uid = {r["id"]: r["uid"] for r in reserves}
        days = [dict(r) for r in c.execute("SELECT * FROM schedule_days ORDER BY id")] if _has(c, "schedule_days") else []

        # aeronaves (reais, nunca as de demonstração)
        ac_rows, skipped_demo = {}, 0
        if _has(c, "aircraft"):
            models = {r["id"]: dict(r) for r in c.execute("SELECT * FROM aircraft_models")} if _has(c, "aircraft_models") else {}
            fields = {r["id"]: dict(r) for r in c.execute("SELECT * FROM config_fields")} if _has(c, "config_fields") else {}
            for r in c.execute("SELECT * FROM aircraft"):
                reg = _norm_reg(r["registration"])
                if not reg:
                    continue
                if r["is_demo"] or reg.startswith(DEMO_PREFIX):
                    skipped_demo += 1
                    continue
                m = models.get(r["model_id"], {})
                cfg = {}
                if _has(c, "aircraft_config"):
                    for cr in c.execute("SELECT * FROM aircraft_config WHERE aircraft_id=?", (r["id"],)):
                        f = fields.get(cr["field_id"])
                        if f and cr["value"] not in (None, ""):
                            cfg[f["key"]] = {"value": cr["value"], "source": f"AeroScale:{cr['source'] or 'config'}"}
                ac_rows[r["id"]] = {"registration": reg, "airline": r["airline"], "type_icao": (m.get("icao_type") or "").upper() or None,
                                    "model_name": m.get("name"), "msn": r["msn"], "active": int(bool(r["active"])),
                                    "config": cfg, "notes": r["notes"], "source_ref": str(r["id"])}

        apmap = _apmap(c)                                  # aeroportos e fusos como o AeroScale os conhece
        legs = []
        for r in c.execute("""SELECT l.*, f.route AS f_route, f.fl AS f_fl, f.speed AS f_speed, f.remarks AS f_remarks
                              FROM schedule_legs l LEFT JOIN rpl_flights f ON f.id = l.flight_id ORDER BY l.schedule_id, l.seq"""):
            r = dict(r)
            s = sched_by_id.get(r["schedule_id"])
            if not s or not r["uid"] or not (r["adep"] and r["ades"] and r["dep_utc"] and r["arr_utc"]):
                continue
            reg = _norm_reg(r["registration"])
            if not reg and r["aircraft_id"] in ac_rows:
                reg = ac_rows[r["aircraft_id"]]["registration"]
            if reg and reg.startswith(DEMO_PREFIX):
                reg = None
            legs.append({"uid": r["uid"], "skey": _skey(s), "src_sid": r["schedule_id"], "seq": r["seq"], "airline": r["airline"],
                         "flight_number": r["flight_number"], "type_icao": r["type_icao"], "planned_type": r["planned_type"],
                         "adep": r["adep"].upper(), "ades": r["ades"].upper(), "dep_utc": r["dep_utc"], "arr_utc": r["arr_utc"],
                         "eet_min": r["eet_min"], "data_origin": r["data_origin"] or "", "activity": r["activity"] or "",
                         "reserve_uid": res_uid.get(r["reserve_id"]), "registration": reg,
                         "route": r["f_route"], "cruise_fl": r["f_fl"], "speed": r["f_speed"], "remarks": r["f_remarks"],
                         "local_date": _local_date(apmap, r["dep_utc"], r["adep"])})
        return {"manifest": manifest, "sha256": sha, "meta": meta, "airlines": airlines, "schedules": schedules,
                "reserves": reserves, "days": days, "aircraft": list(ac_rows.values()), "skipped_demo": skipped_demo, "legs": legs}
    finally:
        c.close()
        import gc
        gc.collect()                                   # o SQLite só solta o arquivo quando os cursores somem


def _troca_holds(conn, batch_id: int) -> bool:
    """A troca daquele lote ainda vale? (as pernas que ela tirou continuam fora pelo mesmo lote)"""
    r = conn.execute("SELECT COUNT(*) n, SUM(CASE WHEN l.removed_batch=? THEN 1 ELSE 0 END) k FROM roster_leg_change c "
                     "JOIN roster_leg l ON l.id=c.leg_id WHERE c.batch_id=? AND c.kind='REMOVED'", (batch_id, batch_id)).fetchone()
    return bool(r[0]) and r[0] == r[1]


def _has(c, table) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def import_backup(conn, src, *, dry_run: bool = False, user_id: int | None = None, airline: str | None = None,
                  schedule_ids=None) -> dict:
    """Importa (ou simula, com dry_run) um backup. Devolve estatísticas.
    airline: só as escalas desta companhia (a carreira); schedule_ids: só estas escalas (as que o motor acabou de gerar)."""
    from .fsutil import temp_dir
    with temp_dir("lineops_imp_") as tmpdir:                 # limpeza à prova do Windows (arquivo aberto/antivírus)
        return _run_import(conn, src, tmpdir, dry_run=dry_run, user_id=user_id, airline=airline, schedule_ids=schedule_ids)


def _filter(data: dict, airline: str | None, schedule_ids) -> dict:
    if not airline and not schedule_ids:
        return data
    ids = {int(x) for x in schedule_ids} if schedule_ids else None
    keep = [sc for sc in data["schedules"] if (not airline or (sc["airline"] or "").upper() == airline.upper()) and (ids is None or sc["id"] in ids)]
    if not keep:
        raise ImportError_(f"Este arquivo não tem escala da {airline}." if airline else "Escala não encontrada no motor.")
    keys = {_skey(sc) for sc in keep}
    sids = {sc["id"] for sc in keep}
    data = dict(data)
    data["schedules"] = keep
    # com schedule_ids (o motor acabou de gerar), só as pernas DESSA geração: as de gerações anteriores do mesmo período
    # ficam de fora e o importador as marca como removidas (regerar substitui, não soma)
    data["legs"] = [lg for lg in data["legs"] if (lg.get("src_sid") in sids if ids is not None else lg["skey"] in keys)]
    data["reserves"] = [r for r in data["reserves"] if r.get("schedule_id") in sids]
    data["days"] = [d for d in data["days"] if d.get("schedule_id") in sids]
    if airline:
        data["aircraft"] = [a for a in data["aircraft"] if (a["airline"] or airline).upper() == airline.upper()]
        data["airlines"] = [a for a in data["airlines"] if (a.get("code") or "").upper() == airline.upper()]
    return data


def _run_import(conn, src, tmpdir: Path, *, dry_run: bool, user_id: int | None, airline: str | None = None, schedule_ids=None) -> dict:
    data = _filter(read_source(Path(src), tmpdir), airline, schedule_ids)
    stats = {"schedules": len(data["schedules"]), "legs_new": 0, "legs_changed": 0, "legs_unchanged": 0,
             "legs_removed": 0, "legs_restored": 0, "reserves": len(data["reserves"]), "aircraft_new": 0,
             "aircraft_updated": 0, "aircraft_skipped_demo": data["skipped_demo"],
             "legs_total": len(data["legs"]), "origins": {}, "warnings": [], "dry_run": dry_run}
    for lg in data["legs"]:
        stats["origins"][lg["data_origin"] or "(vazio)"] = stats["origins"].get(lg["data_origin"] or "(vazio)", 0) + 1
    ver = (data["manifest"] or {}).get("version")
    from .aeroscale import SOURCE_VERSION
    if ver and ver != SOURCE_VERSION:
        stats["warnings"].append(f"O backup é do AeroScale {ver} e o motor da LineOps é da versão {SOURCE_VERSION}. "
                                 f"Se as regras mudaram, copie os arquivos novos para a pasta lineops/aeroscale.")
    if not data["aircraft"]:
        stats["warnings"].append("O backup não contém aeronaves com matrícula: cadastre a frota no LineOps "
                                 "(matrículas não são inventadas).")

    with tx(conn):
        bid = conn.execute("INSERT INTO import_batch(source_kind, source_name, source_sha256, source_version, source_created_at, imported_at, stats) "
                           "VALUES (?,?,?,?,?,?,?)", ("aeroscale-backup", Path(str(src)).name, data["sha256"],
                                                      data["manifest"].get("version"), data["manifest"].get("created_at"),
                                                      now_iso(), "{}")).lastrowid
        for a in data["airlines"]:
            conn.execute("INSERT INTO airline(code, iata, name) VALUES (?,?,?) ON CONFLICT(code) DO UPDATE SET iata=excluded.iata, name=excluded.name",
                         (a["code"], a["iata"], a["name"]))

        sid_by_key, imported_keys, held = {}, set(), set()
        for s in data["schedules"]:
            key = _skey(s)
            imported_keys.add(key)
            row = conn.execute("SELECT id FROM roster_schedule WHERE skey=?", (key,)).fetchone()
            if row:
                sid = row["id"]
                cur_s = conn.execute("SELECT params FROM roster_schedule WHERE id=?", (sid,)).fetchone()
                cur_p = json.loads(cur_s["params"] or "{}") if cur_s else {}
                uids = [lg["uid"] for lg in data["legs"] if lg["skey"] == key]
                fresh = any(not conn.execute("SELECT 1 FROM roster_leg WHERE uid=?", (u,)).fetchone() for u in uids)
                if not fresh and any(k in cur_p for k in ("cut_after", "apagada", "local_edit")):
                    # a mesma escala do motor chegando de novo depois de um corte/limpeza na LineOps: o que foi apagado
                    # (folgas, reservas, fim da escala) continua apagado, como as pernas
                    held.add(sid)
                    conn.execute("UPDATE roster_schedule SET name=?, diagnostics=?, last_batch=? WHERE id=?",
                                 (s["name"], s.get("diagnostics") or "[]", bid, sid))
                else:
                    # escala nova do motor para o mesmo período (regerar o mês): vale inteira, inclusive início e fim
                    conn.execute("UPDATE roster_schedule SET name=?, params=?, diagnostics=?, start_date=?, end_date=?, last_batch=? WHERE id=?",
                                 (s["name"], s["params"] or "{}", s.get("diagnostics") or "[]", s["start_date"], s["end_date"], bid, sid))
            else:
                sid = conn.execute("INSERT INTO roster_schedule(skey, airline, name, kind, start_date, end_date, params, diagnostics, first_batch, last_batch) "
                                   "VALUES (?,?,?,?,?,?,?,?,?,?)", (key, s["airline"], s["name"], s["kind"], s["start_date"], s["end_date"],
                                                                    s["params"] or "{}", s.get("diagnostics") or "[]", bid, bid)).lastrowid
            sid_by_key[key] = sid

        sid_by_old = {s["id"]: sid_by_key[_skey(s)] for s in data["schedules"]}
        for r in data["reserves"]:
            if sid_by_old[r["schedule_id"]] in held:
                continue
            conn.execute("""INSERT INTO roster_reserve(uid, schedule_id, start_date, end_date, days, location, location_kind, status, act_start, act_days, note, last_batch)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(uid) DO UPDATE SET start_date=excluded.start_date, end_date=excluded.end_date, days=excluded.days,
                              location=excluded.location, location_kind=excluded.location_kind, status=excluded.status,
                              act_start=excluded.act_start, act_days=excluded.act_days, note=excluded.note, last_batch=excluded.last_batch""",
                         (r["uid"], sid_by_old[r["schedule_id"]], r["start_date"], r["end_date"], r["days"], r["location"], r["location_kind"],
                          r["status"], r["act_start"], r["act_days"], r["note"], bid))
        for d in data["days"]:
            if sid_by_old[d["schedule_id"]] in held:
                continue
            conn.execute("""INSERT INTO roster_day(schedule_id, date, kind, location, status, note, last_batch) VALUES (?,?,?,?,?,?,?)
                            ON CONFLICT(schedule_id, date, kind) DO UPDATE SET location=excluded.location, status=excluded.status, note=excluded.note, last_batch=excluded.last_batch""",
                         (sid_by_old[d["schedule_id"]], d["date"], d["kind"], d["location"], d["status"], d["note"], bid))

        troca_batches = {r[0] for r in conn.execute("SELECT id FROM import_batch WHERE source_kind IN ('troca', 'corte', 'limpeza')")}
        seen_uids = set()
        for lg in data["legs"]:
            seen_uids.add(lg["uid"])
            h = _hash(lg)
            cur = conn.execute("SELECT * FROM roster_leg WHERE uid=?", (lg["uid"],)).fetchone()
            sid = sid_by_key[lg["skey"]]
            if not cur:
                conn.execute("""INSERT INTO roster_leg(uid, schedule_id, seq, airline, flight_number, type_icao, planned_type, adep, ades, dep_utc, arr_utc,
                                eet_min, data_origin, activity, reserve_uid, registration, route, cruise_fl, speed, remarks, local_date, content_hash, first_batch, last_batch)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                             (lg["uid"], sid, lg["seq"], lg["airline"], lg["flight_number"], lg["type_icao"], lg["planned_type"], lg["adep"], lg["ades"],
                              lg["dep_utc"], lg["arr_utc"], lg["eet_min"], lg["data_origin"], lg["activity"], lg["reserve_uid"], lg["registration"],
                              lg["route"], lg["cruise_fl"], lg["speed"], lg["remarks"], lg["local_date"], h, bid, bid))
                stats["legs_new"] += 1
                continue
            if cur["removed_batch"] and cur["content_hash"] == h and cur["removed_batch"] in troca_batches:
                # saiu por uma troca de escala aprovada e o AeroScale mandou a mesma perna: a troca continua valendo
                conn.execute("UPDATE roster_leg SET last_batch=?, schedule_id=? WHERE id=?", (bid, sid, cur["id"]))
                stats["legs_unchanged"] += 1
                continue
            if cur["removed_batch"]:
                conn.execute("INSERT INTO roster_leg_change(leg_id, batch_id, kind, old_values) VALUES (?,?,?,?)", (cur["id"], bid, "RESTORED", "{}"))
                stats["legs_restored"] += 1
            if cur["content_hash"] == h:
                conn.execute("UPDATE roster_leg SET last_batch=?, removed_batch=NULL, schedule_id=?, local_date=? WHERE id=?",
                         (bid, sid, lg["local_date"], cur["id"]))
                stats["legs_unchanged"] += 1
                continue
            old = {k: cur[k] for k in LEG_HASH_FIELDS if cur[k] != lg.get(k)}
            conn.execute("INSERT INTO roster_leg_change(leg_id, batch_id, kind, old_values) VALUES (?,?,?,?)",
                         (cur["id"], bid, "CHANGED", json.dumps(old, ensure_ascii=False)))
            conn.execute("""UPDATE roster_leg SET schedule_id=?, seq=?, airline=?, flight_number=?, type_icao=?, planned_type=?, adep=?, ades=?, dep_utc=?, arr_utc=?,
                            eet_min=?, data_origin=?, activity=?, reserve_uid=?, registration=?, route=?, cruise_fl=?, speed=?, remarks=?, local_date=?, content_hash=?,
                            last_batch=?, changed_batch=?, removed_batch=NULL WHERE id=?""",
                         (sid, lg["seq"], lg["airline"], lg["flight_number"], lg["type_icao"], lg["planned_type"], lg["adep"], lg["ades"], lg["dep_utc"],
                          lg["arr_utc"], lg["eet_min"], lg["data_origin"], lg["activity"], lg["reserve_uid"], lg["registration"], lg["route"],
                          lg["cruise_fl"], lg["speed"], lg["remarks"], lg["local_date"], h, bid, bid, cur["id"]))
            stats["legs_changed"] += 1

        # pernas que sumiram de escalas reimportadas
        for key in imported_keys:
            sid = sid_by_key[key]
            for r in conn.execute("SELECT id, uid, first_batch FROM roster_leg WHERE schedule_id=? AND removed_batch IS NULL", (sid,)).fetchall():
                if r["first_batch"] in troca_batches and _troca_holds(conn, r["first_batch"]):
                    continue                     # perna que entrou por troca aprovada: fica enquanto as pernas trocadas seguirem fora
                if r["uid"] not in seen_uids:
                    conn.execute("UPDATE roster_leg SET removed_batch=? WHERE id=?", (bid, r["id"]))
                    conn.execute("INSERT INTO roster_leg_change(leg_id, batch_id, kind, old_values) VALUES (?,?,?,?)", (r["id"], bid, "REMOVED", "{}"))
                    stats["legs_removed"] += 1

        now = now_iso()
        for a in data["aircraft"]:
            if not a["type_icao"]:
                stats["warnings"].append(f"{a['registration']}: tipo ICAO ausente no AeroScale; ignorada (cadastre manualmente).")
                continue
            cur = conn.execute("SELECT id, source FROM aircraft WHERE registration=?", (a["registration"],)).fetchone()
            if cur:
                if cur["source"] == "AEROSCALE":     # edições manuais do LineOps nunca são sobrescritas
                    conn.execute("UPDATE aircraft SET airline=?, type_icao=?, model_name=?, msn=?, updated_at=? WHERE id=?",
                                 (a["airline"], a["type_icao"], a["model_name"], a["msn"], now, cur["id"]))
                    stats["aircraft_updated"] += 1
            else:
                conn.execute("""INSERT INTO aircraft(registration, airline, type_icao, model_name, msn, active, config, source, source_ref, notes, created_at, updated_at)
                                VALUES (?,?,?,?,?,?,?, 'AEROSCALE', ?, ?, ?, ?)""",
                             (a["registration"], a["airline"], a["type_icao"], a["model_name"], a["msn"], a["active"],
                              json.dumps(a["config"], ensure_ascii=False), a["source_ref"], a["notes"], now, now))
                stats["aircraft_new"] += 1

        conn.execute("UPDATE import_batch SET stats=? WHERE id=?", (json.dumps(stats, ensure_ascii=False), bid))
        audit(conn, user_id, "import.aeroscale", "import_batch", bid, {k: v for k, v in stats.items() if k != "origins"})
        stats["batch_id"] = bid
        if dry_run:
            raise _DryRun(stats)
    rules.save_source(conn, tmpdir / "work.db")        # o LineOps guarda o banco do AeroScale para rodar as regras dele
    stats["rules_source_saved"] = True
    return stats


class _DryRun(Exception):
    def __init__(self, stats):
        self.stats = stats


_orig_import = import_backup


def import_backup(conn, src, *, dry_run: bool = False, user_id: int | None = None, airline: str | None = None,   # noqa: F811
                  schedule_ids=None) -> dict:
    try:
        return _orig_import(conn, src, dry_run=dry_run, user_id=user_id, airline=airline, schedule_ids=schedule_ids)
    except _DryRun as d:           # a transação já foi revertida pelo tx(); nada foi gravado
        d.stats.pop("batch_id", None)
        return d.stats
