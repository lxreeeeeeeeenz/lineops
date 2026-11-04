"""Modo LATAM: carreira própria, escala do motor com o RPL da LATAM (família A320), equipe e regras do Airbus."""
import time

from lineops import careers, db, equipe, landing, motor, tripulacao


def test_latam_career_is_its_own_world(tmp_path):
    data = tmp_path / "dados"
    careers.bootstrap(data)
    core = db.connect(careers.core_path(data))
    core.execute("INSERT INTO user(id, username, pw_hash, display_name, created_at) VALUES (1,'t','x','T','x')")
    tid = core.execute("INSERT INTO auth_token(user_id, kind, token_hash, created_at) VALUES (1,'session','h','x')").lastrowid
    p = careers.select(data, core, tid, "TAM")
    assert p["name"] == "LATAM" and p["status"] == "ativo" and "LATAM Airlines Brasil" in careers.disclaimer("TAM")
    c = db.connect(careers.career_path(data, "TAM"))
    assert careers.code_of(c) == "TAM"
    equipe.run(c, now="2026-11-02T12:00:00Z")
    names = {r[0] for r in c.execute("SELECT name FROM ops_staff")}
    assert "Helena Siqueira" in names and "Marina Duarte" not in names          # gente própria (fictícia)
    welcome = c.execute("SELECT body FROM ops_message ORDER BY id LIMIT 1").fetchone()[0]
    assert "GOL" not in welcome


def test_airbus_landing_and_cabin_by_latam_config():
    # A320: LOAD<15> acima de 2,6 G ou 9 ft/s (540 fpm); 737: 2,2 G / 600 fpm
    assert landing.evaluate(560, 1.5, type_icao="A320")["tier"] == "duro"
    assert landing.evaluate(560, 1.5, type_icao="B738")["tier"] == "firme"
    assert landing.evaluate(300, 2.4, type_icao="A321")["tier"] != "duro"
    assert landing.evaluate(300, 2.4, type_icao="B738")["tier"] == "duro"
    ev = landing.evaluate(400, 1.8, type_icao="A20N", landing_weight_kg=70000, mlw_kg=67400)
    assert ev["overweight"] and ev["tier"] == "duro" and "LOAD<15>" in ev["insp_text"] and ev["maker"] == "Airbus"
    t = landing.texts(landing.evaluate(700, 2.0, type_icao="A320"), {"reg": "PR-MYA", "ades": "SBGR", "fn": "TAM3000", "seed": 0})
    assert "737" not in t["mnt"] and "LOAD<15>" in t["mnt"]
    assert landing.evaluate(650, 1.4, type_icao="B789")["tier"] == "duro"         # widebody: limite de projeto do trem
    # comissários pela configuração da LATAM Brasil (RBAC 121.391)
    assert tripulacao.cabin_count("A319", "TAM") == 3 and tripulacao.cabin_count("A320", "TAM") == 4
    assert tripulacao.cabin_count("A321", "TAM") == 5 and tripulacao.cabin_count("B77W", "TAM") == 9
    assert tripulacao.cabin_count("B738", "GLO") == 4


def test_engine_generates_a_latam_roster(tmp_path):
    data = tmp_path / "dados"
    motor.init(data)
    op = motor.options(data, "TAM")
    types = [t["type"] for t in op["types"]]
    assert types[0] in ("A320", "A321", "A319") and any(b["icao"] == "SBGR" for b in op["bases"])
    career = careers.career_path(data, "TAM")
    career.parent.mkdir(parents=True)
    c = db.connect(career)
    db.migrate(c)
    c.execute("INSERT INTO setting(key, value) VALUES ('career_code', 'TAM')")
    c.commit()
    c.close()
    j = motor.generate(data, "TAM", {"kind": "mensal", "month": "2026-11", "base": "SBGR", "icao_type": "A320", "seed": 5}, career)
    while motor.job(j["id"])["state"] == "running":
        time.sleep(0.2)
    r = motor.job(j["id"])
    assert r["state"] == "finished", r.get("error")
    c = db.connect(career)
    legs = c.execute("SELECT * FROM roster_leg WHERE removed_batch IS NULL AND activity<>'SOBREAVISO'").fetchall()
    assert legs and all(l["airline"] == "TAM" for l in legs) and all(l["flight_number"].startswith("TAM") for l in legs)
    assert {l["type_icao"] for l in legs} <= {"A319", "A320", "A321", "A20N", "A21N"}
    d = tripulacao.pairing(c, legs[0]["local_date"])
    assert d and len(d["cabin"]) in (3, 4, 5)
