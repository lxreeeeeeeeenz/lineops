"""LineOps: núcleo + carreiras por companhia, migração do banco antigo, logo, backup de tudo e o motor do AeroScale integrado."""
import json
import os
import time
import zipfile

import pytest

from lineops import backup, careers, db, motor
from lineops.aeroscale_import import ImportError_, import_backup
from lineops.errors import Conflict, FGVError
from tests.test_importer import L, make_db


def _legacy(data):
    """Um banco de antes das carreiras: piloto, avião, voo externo e ajustes."""
    data.mkdir(parents=True, exist_ok=True)
    c = db.connect(data / ("gol" "ops.db"))
    db.migrate(c)
    c.execute("INSERT INTO user(id, username, pw_hash, display_name, created_at) VALUES (1,'triple','x','Triple','2026-01-01T00:00:00Z')")
    c.execute("INSERT INTO auth_token(user_id, kind, token_hash, created_at) VALUES (1,'session','h1','2026-01-01T00:00:00Z')")
    c.execute("INSERT INTO aircraft(registration, airline, type_icao, active, source, created_at, updated_at) VALUES ('PR-GUZ','GLO','B738',1,'MANUAL','x','x')")
    c.execute("INSERT INTO setting(key, value) VALUES ('ivao_vid','123456'), ('landing_style','just_culture'), ('tg_token','abc:def')")
    c.execute("""INSERT INTO ext_flight(adep, ades, dep_utc, source, created_at) VALUES ('SBGR','LPPT','2026-11-07T23:00:00Z','MANUAL','x')""")
    c.close()


def test_legacy_database_becomes_gol_career(tmp_path):
    data = tmp_path / "dados"
    _legacy(data)
    (data / "aeroscale").mkdir()
    (data / "aeroscale" / "source.db").write_bytes(b"x")
    assert careers.bootstrap(data) == "gol" "ops.db"
    assert (data / "carreiras" / "GLO.aeroscale" / "source.db").exists()          # as regras continuam achando o banco do AeroScale
    assert not (data / ("gol" "ops.db")).exists() and careers.career_path(data, "GLO").exists()
    core = db.connect(careers.core_path(data))
    assert core.execute("SELECT username FROM user").fetchone()[0] == "triple"
    assert core.execute("SELECT career FROM auth_token").fetchone()[0] == "GLO"
    assert core.execute("SELECT value FROM setting WHERE key='ivao_vid'").fetchone()[0] == "123456"
    assert core.execute("SELECT value FROM setting WHERE key='tg_token'").fetchone()[0] == "abc:def"
    assert core.execute("SELECT COUNT(*) FROM setting WHERE key='landing_style'").fetchone()[0] == 0     # é da carreira, não do núcleo
    assert core.execute("SELECT COUNT(*) FROM ext_flight").fetchone()[0] == 1
    g = db.connect(careers.career_path(data, "GLO"))
    assert g.execute("SELECT registration FROM aircraft").fetchone()[0] == "PR-GUZ"
    assert careers.bootstrap(data) is None                                                      # só uma vez


def test_careers_select_sync_and_logo(tmp_path):
    data = tmp_path / "dados"
    careers.bootstrap(data)
    core = db.connect(careers.core_path(data))
    core.execute("INSERT INTO user(id, username, pw_hash, display_name, created_at) VALUES (1,'t','x','T','x')")
    tid = core.execute("INSERT INTO auth_token(user_id, kind, token_hash, created_at) VALUES (1,'session','h','x')").lastrowid
    with pytest.raises(Conflict):
        careers.select(data, core, tid, "WORLD")                                                # Mundo ainda "em breve"
    p = careers.select(data, core, tid, "glo")
    assert p["name"] == "GOL" and core.execute("SELECT career FROM auth_token WHERE id=?", (tid,)).fetchone()[0] == "GLO"
    g = db.connect(careers.career_path(data, "GLO"))
    assert g.execute("SELECT username FROM user").fetchone()[0] == "t" and careers.code_of(g) == "GLO"
    core.execute("INSERT INTO setting(key, value) VALUES ('ivao_vid','777')")
    careers.sync_settings(core, g)
    assert g.execute("SELECT value FROM setting WHERE key='ivao_vid'").fetchone()[0] == "777"
    lst = {x["code"]: x for x in careers.list_careers(data, core)}
    assert lst["GLO"]["created"] and lst["GLO"]["stats"]["flights"] == 0 and lst["TAM"]["status"] == "ativo" and lst["AZU"]["status"] == "ativo" and lst["WORLD"]["status"] == "em_breve" and lst["WORLD"]["source"] == "adsb"
    with pytest.raises(FGVError):
        careers.save_logo(data, core, "GLO", "logo.exe", b"x")
    with pytest.raises(FGVError):
        careers.save_logo(data, core, "GLO", "logo.svg", b"<svg><script>alert(1)</script></svg>")
    careers.save_logo(data, core, "GLO", "meu-logo.png", b"\x89PNG fake")
    f, mime = careers.logo_file(data, core, "GLO")
    assert f.name == "GLO.logo.png" and mime == "image/png"
    careers.remove_logo(data, core, "GLO")
    assert careers.logo_file(data, core, "GLO") is None
    assert "GOL Linhas Aéreas" in careers.disclaimer("GLO")


def test_backup_and_restore_everything(tmp_path):
    data = tmp_path / "dados"
    _legacy(data)
    careers.bootstrap(data)
    z = backup.make_backup(data)
    with zipfile.ZipFile(z) as zz:
        mf = json.loads(zz.read("manifest.json"))
    assert mf["app"] == "LineOps" and set(mf["files"]) == {"lineops.db", "carreiras/GLO.db"}
    dest = tmp_path / "restaurado"
    assert sorted(backup.restore(z, dest)) == ["carreiras/GLO.db", "lineops.db"]
    assert db.connect(dest / "carreiras" / "GLO.db").execute("SELECT registration FROM aircraft").fetchone()[0] == "PR-GUZ"
    # backup antigo (um banco só) volta como a carreira GOL
    old = tmp_path / "antigo.zip"
    raw = (data / "carreiras" / "GLO.db").read_bytes()
    import hashlib
    with zipfile.ZipFile(old, "w") as zz:
        zz.writestr("gol" "ops.db", raw)
        zz.writestr("manifest.json", json.dumps({"app": "Gol" "Ops", "db_sha256": hashlib.sha256(raw).hexdigest()}))
    assert backup.restore(old, tmp_path / "r2") == ["gol" "ops.db"]                              # máquina nova: adotado inteiro
    careers.bootstrap(tmp_path / "r2")
    assert db.connect(careers.core_path(tmp_path / "r2")).execute("SELECT username FROM user").fetchone()[0] == "triple"
    assert backup.restore(old, data) == ["carreiras/GLO.db"]                                      # já tem piloto: só a carreira
    assert backup.auto_backup(data) is None                                                       # já tem um de agora


def test_import_only_the_career_airline(conn, tmp_path):
    p = tmp_path / "as.db"
    make_db(p, legs=[L("a", "GLO1748", dep="2026-11-20T14:00:00Z", arr="2026-11-20T16:00:00Z")])
    with pytest.raises(ImportError_):
        import_backup(conn, p, airline="TAM")
    st = import_backup(conn, p, airline="GLO")
    assert st["legs_new"] == 1


def test_engine_generates_and_delivers_to_career(tmp_path):
    data = tmp_path / "dados"
    t = time.time()
    motor.init(data)
    op = motor.options(data, "GLO")
    assert op["types"][0]["type"] in ("B738", "B38M") and any(b["icao"] == "SBGR" for b in op["bases"])
    st = motor.status(data, "GLO")
    assert st["coverage"]["from"] and st["rpl_weeks"] and st["engine"].startswith("AeroScale 2.2.4")
    career = careers.career_path(data, "GLO")
    career.parent.mkdir(parents=True)
    c = db.connect(career)
    db.migrate(c)
    c.close()
    with pytest.raises(FGVError):
        motor.generate(data, "GLO", {"kind": "mensal", "month": "2026-13"}, career)
    with pytest.raises(FGVError):
        motor.generate(data, "GLO", {"turbo": 1}, career)
    j = motor.generate(data, "GLO", {"kind": "semanal", "start_date": "2026-10-22", "base": "SBGR", "icao_type": "B738", "seed": 3}, career)
    while motor.job(j["id"])["state"] == "running":
        time.sleep(0.2)
    r = motor.job(j["id"])
    assert r["state"] == "finished", r.get("error")
    res = r["result"]
    assert res["legs"] > 0 and res["import"]["legs_new"] == res["legs"]
    c = db.connect(career)
    legs = c.execute("SELECT * FROM roster_leg WHERE removed_batch IS NULL ORDER BY dep_utc").fetchall()
    assert len(legs) == res["legs"] and all(l["airline"] == "GLO" for l in legs) and legs[0]["adep"] in ("SBGR", "SBSP", "SBKP")
    assert any(l["data_origin"] == "REAL" for l in legs)                                         # semana coberta pelo RPL embutido
    # regerar o mesmo período substitui a escala (diferenças, como numa alteração de verdade)
    j2 = motor.generate(data, "GLO", {"kind": "semanal", "start_date": "2026-10-22", "base": "SBGR", "icao_type": "B738", "seed": 99}, career)
    while motor.job(j2["id"])["state"] == "running":
        time.sleep(0.2)
    r2 = motor.job(j2["id"])["result"]
    active = c.execute("SELECT COUNT(*) FROM roster_leg WHERE removed_batch IS NULL").fetchone()[0]
    assert active == r2["legs"]                                                                   # substituiu, não somou
    assert r2["import"]["legs_removed"] + r2["import"]["legs_unchanged"] == res["legs"]
    assert time.time() - t < 120


def test_real_roster_text_goes_to_career(tmp_path):
    data = tmp_path / "dados"
    career = careers.career_path(data, "GLO")
    career.parent.mkdir(parents=True)
    c = db.connect(career)
    db.migrate(c)
    c.close()
    r = motor.import_real(data, "GLO", "(22) GRU-FOR-GRU (1554,1555)\n(23) FR\n(24) SOBREAVISO", "2026-10", career)
    assert r["schedule_id"] and r["import"]["legs_new"] == 2
    c = db.connect(career)
    assert [x["flight_number"] for x in c.execute("SELECT flight_number FROM roster_leg ORDER BY dep_utc")][:2] == ["GLO1554", "GLO1555"]


def test_backup_takes_everything_and_restores_on_next_open(tmp_path):
    """Backup da LineOps inteira (mensagens, motor, logo...), restauração agendada e o backup antes de cada atualização."""
    from lineops import equipe, __version__
    data = tmp_path / "dados"
    _legacy(data)
    careers.bootstrap(data)
    g = db.connect(careers.career_path(data, "GLO"))
    equipe.post(g, "teste:1", "escala", "Mensagem que não pode sumir", "Comandante, tudo certo?")
    g.commit()
    g.close()
    core = db.connect(careers.core_path(data))
    careers.save_logo(data, core, "GLO", "logo.png", b"\x89PNG meu logo")
    core.close()
    (data / "motor").mkdir()
    m = db.connect(data / "motor" / "aeroscale.db")
    m.execute("CREATE TABLE schedules(id INTEGER PRIMARY KEY, name TEXT)")
    m.execute("INSERT INTO schedules(name) VALUES ('Novembro')")
    m.commit()
    m.close()
    (data / "logs").mkdir(exist_ok=True)
    (data / "logs" / "lineops.log").write_text("log")
    z = backup.make_backup(data)
    with zipfile.ZipFile(z) as zz:
        mf = json.loads(zz.read("manifest.json"))
    assert {"lineops.db", "carreiras/GLO.db", "carreiras/GLO.logo.png", "motor/aeroscale.db"} <= set(mf["files"])
    assert not any(a.startswith(("logs/", "backups/")) for a in mf["files"])
    assert mf["files"]["carreiras/GLO.db"]["counts"]["ops_message"] >= 1
    inf = backup.list_backups(data / "backups")[0]
    assert inf["messages"] >= 1 and inf["version"] == __version__
    # some a mensagem; restaurar pela tela = agendado, aplicado ao abrir, com backup do estado de antes
    g = db.connect(careers.career_path(data, "GLO"))
    g.execute("DELETE FROM ops_message")
    g.commit()
    g.close()
    assert backup.schedule_restore(data, z)["scheduled"] and backup.pending_restore(data)
    r = backup.apply_pending_restore(data)
    assert r["ok"] and "antes-de-restaurar" in r["safety"] and backup.pending_restore(data) is None
    g = db.connect(careers.career_path(data, "GLO"))
    assert g.execute("SELECT subject FROM ops_message WHERE key='teste:1'").fetchone()[0] == "Mensagem que não pode sumir"
    g.close()
    assert backup.apply_pending_restore(data) is None                                         # nunca em laço
    bad = tmp_path / "x.zip"
    with zipfile.ZipFile(bad, "w") as zz:
        zz.writestr("manifest.json", json.dumps({"app": "LineOps", "files": {"lineops.db": {"sha256": "0" * 64}}}))
        zz.writestr("lineops.db", b"outro")
    with pytest.raises(RuntimeError):
        backup.schedule_restore(data, bad)
    # versão nova abrindo os dados: um backup "antes-da-<versão>", uma vez
    (data / backup.VERSION_FILE).write_text("2.0.2")
    b = backup.before_update(data)
    assert b and b.name.endswith(f"-antes-da-{__version__}.zip")
    backup.mark_version(data)
    assert backup.before_update(data) is None
    # a fila dos automáticos não apaga os de antes de atualizar
    for _ in range(4):
        backup.make_backup(data, keep=2)
    names = [x["name"] for x in backup.list_backups(data / "backups")]
    assert any("antes-da-" in n for n in names) and any("antes-de-restaurar" in n for n in names)
    assert len([n for n in names if "-antes-d" not in n]) == 2
