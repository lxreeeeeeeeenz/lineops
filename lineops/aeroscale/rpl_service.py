"""Importação de RPL e expansão dos voos em ocorrências datadas."""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from . import catalog
from .db import add_flag, now_iso, row, rows
from .rpl_parser import parse_rpl


class ImportError_(Exception):
    def __init__(self, message, data=None):
        super().__init__(message)
        self.data = data or {}


def import_rpl(db, data: bytes, filename: str, storage_dir: str, replace_overlapping=True,
               force=False) -> dict:
    res = parse_rpl(data, filename)
    if not res.airline:
        raise ImportError_("Não foi possível identificar a companhia do arquivo.",
                           {"issues": [i.__dict__ for i in res.file_issues]})
    if not res.records:
        raise ImportError_("Nenhum registro de voo reconhecido. Confira se é um arquivo CGNA/RPL.",
                           {"issues": [i.__dict__ for i in res.file_issues]})
    dup = row(db.execute("SELECT id, filename, imported_at FROM rpl_imports WHERE sha256=?", (res.sha256,)))
    if dup and not force:
        raise ImportError_(
            f"Este arquivo já foi importado (importação #{dup['id']}, {dup['filename']}).",
            {"duplicate_of": dup["id"]})

    created_airline = catalog.ensure_airline(db, res.airline)
    apmap = catalog.airport_map(db)

    unknown_aps: dict[str, int] = defaultdict(int)
    for r in res.records:
        for ap in (r.adep, r.ades):
            if ap and ap not in apmap:
                unknown_aps[ap] += 1
                msg = f"Aeroporto {ap} não cadastrado: horário local indisponível (exibido em UTC)."
                if not any(i.message == msg for i in r.issues):
                    r.add("warning", "aeroporto_desconhecido", msg)

    file_issues = [i.__dict__ for i in res.file_issues]
    if created_airline:
        file_issues.append({"severity": "warning", "code": "cia_nova",
                            "message": f"Companhia {res.airline} criada automaticamente; revise o nome em Configurações."})
    counts = res.counts()
    os.makedirs(storage_dir, exist_ok=True)
    cur = db.execute(
        "INSERT INTO rpl_imports(airline, filename, sha256, encoding, header_validity, valid_from, valid_to,"
        " record_count, warning_count, error_count, file_issues, layout, active, imported_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
        (res.airline, filename, res.sha256, res.encoding, res.header_validity, res.valid_from, res.valid_to,
         counts["records"], counts["warnings"], counts["errors"], json.dumps(file_issues, ensure_ascii=False),
         json.dumps(res.layout), now_iso()),
    )
    imp_id = cur.lastrowid
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "rpl.txt")
    path = os.path.join(storage_dir, f"rpl_{imp_id:04d}_{safe}")
    with open(path, "wb") as fh:
        fh.write(data)
    db.execute("UPDATE rpl_imports SET stored_path=? WHERE id=?", (path, imp_id))

    db.executemany(
        "INSERT INTO rpl_flights(import_id, airline, ident, flight_number, valid_from, valid_to, days, type_icao,"
        " wake, adep, eobt, speed, fl, route, ades, eet, eet_min, remarks, remarks_json, raw_text, line_no,"
        " status, issues) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (imp_id, res.airline, r.ident, r.flight_number, r.valid_from, r.valid_to, r.days, r.type_icao, r.wake,
             r.adep, r.eobt, r.speed, r.fl, r.route, r.ades, r.eet, r.eet_min, r.remarks,
             json.dumps(r.remarks_dict, ensure_ascii=False), "\n".join(r.raw_lines), r.line_no, r.status,
             json.dumps([i.__dict__ for i in r.issues], ensure_ascii=False))
            for r in res.records
        ],
    )
    for t in sorted({r.type_icao for r in res.records if r.type_icao}):
        catalog.ensure_model(db, res.airline, t, source="rpl")
    for ap, n in unknown_aps.items():
        add_flag(db, "airport", None, "aeroporto_desconhecido",
                 f"Aeroporto {ap} aparece em {n} registro(s) do RPL {res.airline} e não está cadastrado.",
                 data={"icao": ap})

    replaced = []
    if replace_overlapping and res.valid_from and res.valid_to:
        for old in rows(db.execute(
                "SELECT id FROM rpl_imports WHERE airline=? AND id<>? AND active=1 AND valid_from<=? AND valid_to>=?",
                (res.airline, imp_id, res.valid_to, res.valid_from))):
            db.execute("UPDATE rpl_imports SET active=0 WHERE id=?", (old["id"],))
            replaced.append(old["id"])
    # recalcula contagens (avisos de aeroporto entraram depois do parse)
    w = sum(1 for r in res.records if r.status == "warning")
    e = sum(1 for r in res.records if r.status == "error")
    db.execute("UPDATE rpl_imports SET warning_count=?, error_count=? WHERE id=?", (w, e, imp_id))
    return {
        "id": imp_id, "airline": res.airline, "records": counts["records"], "warnings": w, "errors": e,
        "valid_from": res.valid_from, "valid_to": res.valid_to, "header_validity": res.header_validity,
        "unknown_airports": sorted(unknown_aps), "replaced_imports": replaced, "file_issues": file_issues,
        "airline_created": created_airline,
    }


# ------------------------------------------------------------------ ocorrências
class FlightIndex:
    """Voos ativos (importações ativas, sem erro) indexados para expansão por data."""

    def __init__(self, db, airline: str, taxi_min: int = 0):
        self.airline = airline
        self.taxi = int(taxi_min or 0)
        self.dbfile = db.execute("PRAGMA database_list").fetchone()[2]
        # histórico semanal (importações antigas, ativas ou não) para a projeção sazonal: (início, fim, id)
        self.history = [(r["valid_from"], r["valid_to"], r["id"]) for r in rows(db.execute(
            "SELECT id, valid_from, valid_to FROM rpl_imports WHERE airline=? AND valid_from IS NOT NULL", (airline,)))]
        self._season_cache: dict[str, list] = {}
        self.flights = rows(db.execute(
            "SELECT f.* FROM rpl_flights f JOIN rpl_imports i ON i.id=f.import_id"
            " WHERE i.active=1 AND f.airline=? AND f.status<>'error' AND f.eet_min IS NOT NULL",
            (airline,)))
        self.by_id = {f["id"]: f for f in self.flights}
        self.cov_from = min((f["valid_from"] for f in self.flights), default=None)
        self.cov_to = max((f["valid_to"] for f in self.flights), default=None)
        self.by_wd: dict[int, list[dict]] = defaultdict(list)
        for f in self.flights:
            for wd in range(7):
                if f["days"][wd] != "0":
                    self.by_wd[wd].append(f)
        self._cache: dict[str, list[dict]] = {}
        # durações reais por trecho (qualquer voo) para estimar posicionamentos
        self.route_eet: dict[tuple, list[int]] = defaultdict(list)
        for f in self.flights:
            self.route_eet[(f["adep"], f["ades"])].append(f["eet_min"])

    def turn_stats(self) -> dict[str, list[int]]:
        """Intervalos (min) entre a chegada (EOBT+EET) e a próxima partida do MESMO número de voo
        no mesmo dia e aeroporto, por tipo. São conexões em que a aeronave certamente é a mesma."""
        if hasattr(self, "_turns"):
            return self._turns
        out: dict[str, list[int]] = defaultdict(list)
        if self.cov_from:
            d, end = date.fromisoformat(self.cov_from), date.fromisoformat(self.cov_to)
            while d <= end:
                by_ident: dict[str, list] = defaultdict(list)
                for o in self.occurrences(d, projection=False):
                    by_ident[o["ident"]].append(o)
                for os_ in by_ident.values():
                    os_.sort(key=lambda o: o["dep"])
                    for a, b in zip(os_, os_[1:]):
                        if a["ades"] == b["adep"] and a["type_icao"] == b["type_icao"]:
                            gap = (b["dep"] - (a["arr"] - timedelta(minutes=self.taxi))).total_seconds() / 60
                            if 0 < gap < 6 * 60:
                                out[a["type_icao"]].append(int(gap))
                d += timedelta(days=1)
        self._turns = dict(out)
        return self._turns

    def coverage(self):
        return self.cov_from, self.cov_to

    def projection_ref(self, d: date) -> date | None:
        if not self.cov_from:
            return None
        cf, ct = date.fromisoformat(self.cov_from), date.fromisoformat(self.cov_to)
        if cf <= d <= ct:
            return None
        for k in range(7):
            cand = ct - timedelta(days=k)
            if cand < cf:
                break
            if cand.weekday() == d.weekday():
                return cand
        return None

    def occurrences(self, d: date, projection=True) -> list[dict]:
        key = f"{d.isoformat()}:{int(projection)}"
        if key in self._cache:
            return self._cache[key]
        out = []
        ds = d.isoformat()
        for f in self.by_wd[d.weekday()]:
            if f["valid_from"] <= ds <= f["valid_to"]:
                out.append(self._occ(f, d, "REAL", None))
        if not out and projection:
            out = self.seasonal(d)
        if not out and projection:
            ref = self.projection_ref(d)
            if ref:
                rs = ref.isoformat()
                for f in self.by_wd[ref.weekday()]:
                    if f["valid_from"] <= rs <= f["valid_to"]:
                        out.append(self._occ(f, d, "PROJETADO", rs))
        out.sort(key=lambda o: o["dep"])
        self._cache[key] = out
        return out

    SEASON_DAYS = 364   # 52 semanas: mesmo dia da semana do ano anterior

    def seasonal(self, d: date) -> list[dict]:
        """Projeção sazonal: dia sem RPL publicado usa a mesma semana do ano anterior, quando o histórico tem."""
        if not self.cov_to or d <= date.fromisoformat(self.cov_to):
            return []
        ref = d - timedelta(days=self.SEASON_DAYS)
        rs = ref.isoformat()
        imp = max((h for h in self.history if h[0] <= rs <= h[1]), key=lambda h: h[2], default=None)
        if not imp:
            return []
        if rs not in self._season_cache:
            import sqlite3
            con = sqlite3.connect(self.dbfile)
            con.row_factory = sqlite3.Row
            try:
                wd = ref.weekday()
                fl = [dict(r) for r in con.execute(
                    "SELECT id, airline, ident, type_icao, adep, ades, eobt, eet_min, days, valid_from, valid_to"
                    " FROM rpl_flights WHERE import_id=? AND status<>'error' AND eet_min IS NOT NULL"
                    " AND valid_from<=? AND valid_to>=?", (imp[2], rs, rs))]
            finally:
                con.close()
            self._season_cache[rs] = [f for f in fl if f["days"][wd] != "0"]
        out = [self._occ(f, d, "PROJETADO", rs) for f in self._season_cache[rs]]
        for o in out:
            o["seasonal"] = True
        return out

    def occurrences_range(self, d1: date, d2: date, projection=True) -> list[dict]:
        out = []
        d = d1
        while d <= d2:
            out += self.occurrences(d, projection)
            d += timedelta(days=1)
        return out

    def _occ(self, f, d: date, origin: str, ref: str | None) -> dict:
        h, m = int(f["eobt"][:2]), int(f["eobt"][2:])
        dep = datetime(d.year, d.month, d.day, h, m, tzinfo=timezone.utc)
        arr = dep + timedelta(minutes=f["eet_min"] + self.taxi)
        return {
            "ref": f"{f['id']}:{d.isoformat()}", "flight_id": f["id"], "airline": f["airline"],
            "ident": f["ident"], "type_icao": f["type_icao"], "adep": f["adep"], "ades": f["ades"],
            "dep": dep, "arr": arr, "eet_min": f["eet_min"], "origin": origin, "ref_date": ref,
            "date": d.isoformat(),
        }

    def occurrence_by_ref(self, ref: str, projection=True) -> dict | None:
        try:
            fid_s, ds = ref.split(":", 1)
            fid, d = int(fid_s), date.fromisoformat(ds)
        except ValueError:
            return None
        for o in self.occurrences(d, projection):
            if o["flight_id"] == fid:
                return o
        return None

    def median_eet(self, adep: str, ades: str) -> int | None:
        v = sorted(self.route_eet.get((adep, ades), []))
        return v[len(v) // 2] if v else None


def all_route_eet(db) -> dict[tuple, int]:
    """Mediana de EET por trecho em todos os RPL ativos (todas as companhias)."""
    acc: dict[tuple, list[int]] = defaultdict(list)
    for r in db.execute(
            "SELECT f.adep, f.ades, f.eet_min FROM rpl_flights f JOIN rpl_imports i ON i.id=f.import_id"
            " WHERE i.active=1 AND f.status<>'error' AND f.eet_min IS NOT NULL"):
        acc[(r["adep"], r["ades"])].append(r["eet_min"])
    return {k: sorted(v)[len(v) // 2] for k, v in acc.items()}


# ------------------------------------------------------------------ cache
_INDEX_CACHE: dict[tuple, FlightIndex] = {}


def get_index(db, airline: str, taxi_min: int = 0) -> FlightIndex:
    """FlightIndex reaproveitado enquanto as importações ativas não mudarem (evita reler milhares de
    registros a cada requisição)."""
    dbfile = db.execute("PRAGMA database_list").fetchone()[2]
    ver = tuple(db.execute("SELECT COUNT(*), COALESCE(MAX(id),0), COALESCE(SUM(active),0),"
                           " COALESCE(SUM(record_count),0) FROM rpl_imports").fetchone())
    key = (dbfile, airline, int(taxi_min or 0), ver)
    idx = _INDEX_CACHE.get(key)
    if idx is None:
        if len(_INDEX_CACHE) > 12:
            _INDEX_CACHE.clear()
        idx = _INDEX_CACHE[key] = FlightIndex(db, airline, taxi_min)
    return idx


# ------------------------------------------------------------------ pasta rpl/ (arquivos embutidos)
def folder_files(folder: str) -> list[str]:
    if not os.path.isdir(folder):
        return []
    return sorted(f for f in os.listdir(folder) if f.lower().endswith(".txt") and not f.upper().startswith("LEIA"))


def import_folder(db, folder: str, storage_dir: str, skip_hashes: set | None = None) -> list[dict]:
    """Importa os .txt CGNA/RPL da pasta que ainda não foram importados (compara pelo SHA-256).
    Arquivo novo com validade sobreposta substitui a importação anterior da mesma companhia."""
    import hashlib
    skip_hashes = skip_hashes or set()
    out = []
    for fn in folder_files(folder):
        with open(os.path.join(folder, fn), "rb") as fh:
            data = fh.read()
        sha = hashlib.sha256(data).hexdigest()
        if sha in skip_hashes or db.execute("SELECT 1 FROM rpl_imports WHERE sha256=?", (sha,)).fetchone():
            out.append({"filename": fn, "ok": True, "skipped": True})
            continue
        try:
            out.append({"filename": fn, "ok": True, **import_rpl(db, data, fn, storage_dir, True, False)})
            db.commit()
        except ImportError_ as e:
            db.rollback()
            out.append({"filename": fn, "ok": False, "error": str(e)})
    return out
