import json
import sqlite3
import zipfile

import pytest

from lineops import aeroscale_import as ai, fleet, flights

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE airports(icao TEXT PRIMARY KEY, iata TEXT, name TEXT, city TEXT, uf TEXT, tz TEXT, updated_at TEXT);
CREATE TABLE intl_flights(id INTEGER PRIMARY KEY, adep TEXT, ades TEXT);
CREATE TABLE airlines(code TEXT, iata TEXT, name TEXT);
CREATE TABLE schedules(id INTEGER PRIMARY KEY, name TEXT, airline TEXT, kind TEXT, start_date TEXT, end_date TEXT, aircraft_id INTEGER, params TEXT);
CREATE TABLE rpl_imports(id INTEGER PRIMARY KEY, airline TEXT, valid_from TEXT, valid_to TEXT, record_count INTEGER, active INTEGER);
CREATE TABLE rpl_flights(id INTEGER PRIMARY KEY, import_id INTEGER, airline TEXT, ident TEXT, flight_number TEXT, valid_from TEXT,
  valid_to TEXT, days TEXT, type_icao TEXT, wake TEXT, adep TEXT, eobt TEXT, speed TEXT, fl TEXT, route TEXT, ades TEXT, eet TEXT,
  eet_min INTEGER, remarks TEXT, remarks_json TEXT, raw_text TEXT, line_no INTEGER, status TEXT, issues TEXT);
CREATE TABLE aircraft_models(id INTEGER PRIMARY KEY, name TEXT, icao_type TEXT);
CREATE TABLE aircraft(id INTEGER PRIMARY KEY, airline TEXT, registration TEXT, model_id INTEGER, msn TEXT, notes TEXT, active INTEGER, is_demo INTEGER);
CREATE TABLE config_fields(id INTEGER PRIMARY KEY, key TEXT);
CREATE TABLE aircraft_config(aircraft_id INTEGER, field_id INTEGER, value TEXT, source TEXT);
CREATE TABLE schedule_reserves(id INTEGER PRIMARY KEY, uid TEXT, schedule_id INTEGER, start_date TEXT, end_date TEXT, days INTEGER, location TEXT,
  location_kind TEXT, status TEXT, act_start TEXT, act_days INTEGER, note TEXT);
CREATE TABLE schedule_days(id INTEGER PRIMARY KEY, schedule_id INTEGER, date TEXT, kind TEXT, location TEXT, status TEXT, note TEXT);
CREATE TABLE schedule_legs(id INTEGER PRIMARY KEY, uid TEXT, schedule_id INTEGER, seq INTEGER, aircraft_id INTEGER, registration TEXT, airline TEXT,
  flight_id INTEGER, flight_number TEXT, type_icao TEXT, planned_type TEXT, adep TEXT, ades TEXT, dep_utc TEXT, arr_utc TEXT, eet_min INTEGER,
  data_origin TEXT, activity TEXT, reserve_id INTEGER);
"""


def make_db(path, *, legs, aircraft=()):
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.execute("INSERT INTO airlines VALUES ('GLO','G3','GOL Linhas Aéreas')")
    c.execute("INSERT INTO schedules VALUES (1,'S','GLO','mensal','2026-11-01','2026-11-30',NULL,'{}')")
    c.execute("INSERT INTO rpl_imports VALUES (1,'GLO','2026-10-29','2026-11-04',1,1)")
    c.execute("""INSERT INTO rpl_flights(id, import_id, airline, ident, flight_number, valid_from, valid_to, days, type_icao, wake,
                 adep, eobt, speed, fl, route, ades, eet, eet_min, remarks, remarks_json, line_no, status, issues)
                 VALUES (7,1,'GLO','GLO1','1','2026-10-29','2026-11-04','1234567','B738','M','SBBR','1000','N0450','380',
                         'KUKOL UZ5 EDREX','SBFL','0154',114,'EQPT/SDFGIKRWY PBN/B1C1','{"EQPT":"SDFGIKRWY"}',1,'ok','[]')""")
    c.execute("INSERT INTO aircraft_models VALUES (1,'Boeing 737-800','B738')")
    for a in aircraft:
        c.execute("INSERT INTO aircraft VALUES (?,?,?,?,?,?,?,?)", a)
    c.execute("INSERT INTO schedule_reserves VALUES (1,'ru1',1,'2026-11-24','2026-11-27',4,'SBBR','base','acionado',NULL,NULL,'')")
    for i, l in enumerate(legs, 1):
        c.execute("INSERT INTO schedule_legs VALUES (?,?,1,?,?,?, 'GLO',?,?,?,?,?,?,?,?,?,?,?,?)",
                  (i, l["uid"], i, l.get("aircraft_id"), l.get("registration"), l.get("flight_id"), l["fn"], "B738", "B738", l["adep"], l["ades"],
                   l["dep"], l["arr"], 60, l.get("origin", "REAL"), l.get("activity", ""), l.get("reserve_id")))
    c.commit(); c.close()


def L(uid, fn, dep="2026-11-02T10:00:00Z", arr="2026-11-02T11:00:00Z", **kw):
    return dict(uid=uid, fn=fn, adep="SBBR", ades="SBFL", dep=dep, arr=arr, **kw)


def test_import_preserves_origin_and_enriches(conn, tmp_path):
    p = tmp_path / "a.db"
    make_db(p, legs=[L("a", "GLO1", flight_id=7, origin="OBSERVADO"), L("b", "GLO2", origin="PROJETADO", activity="RESERVA", reserve_id=1)])
    st = ai.import_backup(conn, p)
    assert st["legs_new"] == 2 and st["origins"] == {"OBSERVADO": 1, "PROJETADO": 1}
    a = conn.execute("SELECT * FROM roster_leg WHERE uid='a'").fetchone()
    assert a["data_origin"] == "OBSERVADO" and a["route"] == "KUKOL UZ5 EDREX" and a["cruise_fl"] == "380" and a["registration"] is None
    assert conn.execute("SELECT reserve_uid FROM roster_leg WHERE uid='b'").fetchone()[0] == "ru1"
    assert any("matrícula" in w for w in st["warnings"])


def test_reimport_idempotent_changes_and_removals(conn, tmp_path):
    p = tmp_path / "a.db"
    make_db(p, legs=[L("a", "GLO1"), L("b", "GLO2")])
    ai.import_backup(conn, p)
    assert ai.import_backup(conn, p)["legs_unchanged"] == 2
    p2 = tmp_path / "b.db"
    make_db(p2, legs=[L("a", "GLO1", dep="2026-11-02T12:00:00Z", arr="2026-11-02T13:00:00Z")])      # a mudou, b sumiu
    st = ai.import_backup(conn, p2)
    assert st["legs_changed"] == 1 and st["legs_removed"] == 1
    a = conn.execute("SELECT * FROM roster_leg WHERE uid='a'").fetchone()
    assert a["dep_utc"].endswith("12:00:00Z")
    old = json.loads(conn.execute("SELECT old_values FROM roster_leg_change WHERE leg_id=? AND kind='CHANGED'", (a["id"],)).fetchone()[0])
    assert old["dep_utc"].endswith("10:00:00Z")
    assert conn.execute("SELECT removed_batch FROM roster_leg WHERE uid='b'").fetchone()[0] is not None
    assert [r["flight_number"] for r in flights.list_roster(conn)] == ["GLO1"]
    ai.import_backup(conn, p)                                                                         # b volta
    assert conn.execute("SELECT removed_batch FROM roster_leg WHERE uid='b'").fetchone()[0] is None


def test_removed_leg_with_booking_is_kept_but_not_bookable(conn, tmp_path):
    p = tmp_path / "a.db"
    make_db(p, legs=[L("a", "GLO1"), L("b", "GLO2", dep="2026-11-03T10:00:00Z", arr="2026-11-03T11:00:00Z")])
    ai.import_backup(conn, p)
    lid = conn.execute("SELECT id FROM roster_leg WHERE uid='a'").fetchone()[0]
    f = flights.book_leg(conn, lid)
    make_db(tmp_path / "c.db", legs=[L("b", "GLO2", dep="2026-11-03T10:00:00Z", arr="2026-11-03T11:00:00Z")])
    ai.import_backup(conn, tmp_path / "c.db")
    assert flights.get_flight(conn, f["id"])["state"] == "RESERVADO"                                  # reserva preservada


def test_dry_run_writes_nothing_and_demo_aircraft_skipped(conn, tmp_path):
    p = tmp_path / "a.db"
    make_db(p, legs=[L("a", "GLO1", registration="FIC-ABC")],
            aircraft=[(1, "GLO", "FIC-ABC", 1, None, None, 1, 1), (2, "GLO", "PR-REL", 1, "123", None, 1, 0)])
    st = ai.import_backup(conn, p, dry_run=True)
    assert st["dry_run"] and st["legs_new"] == 1 and st["aircraft_new"] == 1 and st["aircraft_skipped_demo"] == 1
    assert conn.execute("SELECT COUNT(*) FROM roster_leg").fetchone()[0] == 0 and conn.execute("SELECT COUNT(*) FROM aircraft").fetchone()[0] == 0
    ai.import_backup(conn, p)
    assert [r["registration"] for r in fleet.list_aircraft(conn)] == ["PR-REL"]
    assert conn.execute("SELECT registration FROM roster_leg").fetchone()[0] is None                 # FIC- nunca vira matrícula


def test_zip_and_garbage_inputs(conn, tmp_path):
    p = tmp_path / "aeroscale.db"
    make_db(p, legs=[L("a", "GLO1")])
    z = tmp_path / "backup.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(p, "aeroscale.db")
        zf.writestr("manifest.json", json.dumps({"app": "AeroScale", "version": "2.2.4", "created_at": "2026-11-01T18:02:54Z"}))
        zf.writestr("../../evil.txt", "x")
    assert ai.import_backup(conn, z)["legs_new"] == 1
    assert not (tmp_path.parent / "evil.txt").exists()
    assert conn.execute("SELECT source_version FROM import_batch").fetchone()[0] == "2.2.4"
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("x.txt", "y")
    with pytest.raises(ai.ImportError_):
        ai.import_backup(conn, bad)
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database" * 100)
    with pytest.raises(ai.ImportError_):
        ai.import_backup(conn, junk)


def test_local_date_follows_departure_local_time(conn, tmp_path):
    """O AeroScale data a perna pela hora LOCAL da partida (api.leg_brief: local_date)."""
    p = tmp_path / "ld.db"
    make_db(p, legs=[dict(uid="a", fn="GLO1", adep="SBBR", ades="SBFL", dep="2026-11-01T23:55:00Z", arr="2026-11-02T01:59:00Z"),
                     dict(uid="b", fn="GLO2", adep="SBFL", ades="SAEZ", dep="2026-11-02T02:50:00Z", arr="2026-11-02T05:00:00Z")])
    ai.import_backup(conn, p)
    assert [r["local_date"] for r in conn.execute("SELECT local_date FROM roster_leg ORDER BY dep_utc")] == ["2026-11-01", "2026-11-01"]


BACKUP_REAL = ROOT_BACKUP = __import__("pathlib").Path("/tmp/claude-0/-home-claude/cfba1d29-41db-5627-b841-b89e59ff79a4/scratchpad/backup")


@pytest.mark.skipif(not (BACKUP_REAL / "aeroscale.db").exists(), reason="backup real do AeroScale não está disponível")
def test_overnights_match_aeroscale_own_diagnostic(conn):
    """A lista de pernoites que o LineOps deriva tem de bater com a que o AeroScale escreveu no diagnóstico."""
    import json
    from collections import Counter
    from lineops import journey
    ai.import_backup(conn, BACKUP_REAL)
    legs = [dict(r) for r in conn.execute("SELECT * FROM roster_leg WHERE activity<>'SOBREAVISO' ORDER BY dep_utc")]
    nights = Counter()
    for a, b in zip(legs, legs[1:]):
        g = journey._gap(conn, a, b)
        if g["overnight"] and g["kind"] != "parada_noturna_curta":
            nights[a["ades"]] += 1
    meu = ", ".join(f"{ap} ×{n}" if n > 1 else ap for ap, n in nights.most_common())
    diag = json.loads(conn.execute("SELECT diagnostics FROM roster_schedule").fetchone()[0])
    dele = next(d["message"] for d in diag if d["code"] == "pernoites")
    assert meu and f"Pernoites: {meu}." in dele


def test_import_warns_when_rules_version_differs(conn, tmp_path):
    import json
    import zipfile
    p = tmp_path / "aeroscale.db"
    make_db(p, legs=[L("a", "GLO1")])
    z = tmp_path / "novo.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(p, "aeroscale.db")
        zf.writestr("manifest.json", json.dumps({"app": "AeroScale", "version": "2.3.0", "created_at": "2026-12-01T00:00:00Z"}))
    st = ai.import_backup(conn, z)
    assert any("2.3.0" in w and "2.2.4" in w for w in st["warnings"])
