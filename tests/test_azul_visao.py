"""Modo Azul (ATR, E-Jets, A320neo) e as visões novas: mês, mapa, limites, passaporte, CSV e o fechamento do mês."""
import time

from lineops import carreira, careers, db, equipe, landing, motor, tripulacao, visao


def test_azul_is_unlocked_with_its_own_people_and_rules(tmp_path):
    data = tmp_path / "dados"
    careers.bootstrap(data)
    core = db.connect(careers.core_path(data))
    core.execute("INSERT INTO user(id, username, pw_hash, display_name, created_at) VALUES (1,'t','x','T','x')")
    tid = core.execute("INSERT INTO auth_token(user_id, kind, token_hash, created_at) VALUES (1,'session','h','x')").lastrowid
    p = careers.select(data, core, tid, "AZU")
    assert p["status"] == "ativo" and p["hubs"] == ["SBKP", "SBCF", "SBRF"]
    c = db.connect(careers.career_path(data, "AZU"))
    equipe.run(c, now="2026-11-02T12:00:00Z")
    names = {r[0] for r in c.execute("SELECT name FROM ops_staff")}
    assert "Bianca Lourenço" in names and "Helena Siqueira" not in names
    # comissários pela configuração da Azul (seatmaps.com): ATR 70 → 2, E195 118 → 3, E2 136 → 3, A320neo 174 → 4, A339 298 → 6
    assert [tripulacao.cabin_count(t, "AZU") for t in ("AT76", "E195", "E295", "A20N", "A339")] == [2, 3, 3, 4, 6]
    # ATR e E-Jets: sem o número de G do AMM publicado → só o limite de projeto do trem (600 fpm)
    for t in ("AT76", "E295"):
        assert landing.evaluate(620, 1.3, type_icao=t)["tier"] == "duro"
        assert landing.evaluate(300, 2.4, type_icao=t)["tier"] != "duro"
    assert "ATSB" in landing.evaluate(620, 1.3, type_icao="AT76")["source"]


def test_engine_generates_azul_atr_roster(tmp_path):
    data = tmp_path / "dados"
    motor.init(data)
    op = motor.options(data, "AZU")
    assert {"E295", "A20N", "AT76"} <= {t["type"] for t in op["types"]}
    career = careers.career_path(data, "AZU")
    career.parent.mkdir(parents=True)
    c = db.connect(career)
    db.migrate(c)
    c.execute("INSERT INTO setting(key, value) VALUES ('career_code', 'AZU')")
    c.commit()
    c.close()
    j = motor.generate(data, "AZU", {"kind": "mensal", "month": "2026-11", "base": "SBRF", "icao_type": "AT76", "seed": 4}, career)
    while motor.job(j["id"])["state"] == "running":
        time.sleep(0.2)
    assert motor.job(j["id"])["state"] == "finished", motor.job(j["id"]).get("error")
    c = db.connect(career)
    flown = c.execute("SELECT type_icao FROM roster_leg WHERE removed_batch IS NULL AND activity NOT IN ('SOBREAVISO','EXTRA')").fetchall()
    assert flown and all(r[0] == "AT76" for r in flown)
    # visões do mês
    mo = visao.month(c, "2026-11", "2026-11-01T12:00:00Z")
    assert len(mo["days"]) == 30 and mo["totals"]["flight_days"] > 0 and mo["base"]["icao"] == "SBRF"
    assert all(d["kind"] in ("VOO", "FOLGA", "RESERVA", "SOBREAVISO", "LIVRE") for d in mo["days"])
    mp = visao.route_map(c, "2026-11-01", "2026-11-30")
    assert mp["airports"] and mp["segments"] and all(a["lat"] is not None for a in mp["airports"])
    li = visao.limits(c, "2026-11")
    assert li["available"] and li["block"]["limit"] == 85.0 and li["duty"]["limit"] == 176.0   # turbo-hélice: 85 h (art. 33)
    c.close()


def test_passport_csv_and_month_close(conn):
    conn.execute("INSERT INTO setting(key, value) VALUES ('career_code', 'AZU')")
    for i, (d, a, b, vs) in enumerate([("2026-10-03", "SBKP", "SBRF", 180), ("2026-10-04", "SBRF", "SBFZ", 120), ("2026-10-20", "SBFZ", "SBKP", 650)]):
        conn.execute("""INSERT INTO logbook_entry(date, airline, flight_number, adep, ades, type_icao, block_min, night_min, landings,
                        landing_vs_fpm, landing_g, source, created_at) VALUES (?,?,?,?,?,?,?,?,1,?,1.3,'MANUAL','x')""",
                     (d, "AZU", f"AZU{4000 + i}", a, b, "E295", 120, 30, vs))
    pp = visao.passport(conn)
    assert pp["flights"] == 3 and {a["icao"] for a in pp["airports"]} == {"SBKP", "SBRF", "SBFZ"} and pp["types"][0]["type"] == "E295"
    csv = visao.logbook_csv(conn)
    assert csv.startswith("﻿date;airline") and "AZU4002" in csv
    carreira.month_close_clock(conn, "2026-11-01T15:00:00Z")
    m = conn.execute("SELECT subject, body FROM ops_message WHERE key='fechamento:2026-10'").fetchone()
    assert m and "outubro" in m[0].lower() and "VOOS: 3" in m[1] and "1 pouso(s) duro(s)" in m[1]
    carreira.month_close_clock(conn, "2026-11-02T15:00:00Z")
    assert conn.execute("SELECT COUNT(*) FROM ops_message WHERE key LIKE 'fechamento:%'").fetchone()[0] == 1
