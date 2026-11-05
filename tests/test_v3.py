"""3.0: leitura única da escala, agenda .ics, conquistas e foto de perfil."""
import pytest

from lineops import avatar, conquistas, db, escala, exportar, journey
from lineops.errors import FGVError
from tests.test_gerador import BASE, _run, eng  # noqa: F401


def test_one_reader_for_the_roster(eng):  # noqa: F811
    data, career = eng
    _run(data, career, {**BASE, "seed": 3})
    c = db.connect(career)
    all_ = escala.legs(c, "2026-11-01", "2026-11-30")
    fl = escala.legs(c, "2026-11-01", "2026-11-30", flights_only=True)
    assert all_ and all(l["local_date"] for l in all_) and all(l["activity"] != "SOBREAVISO" for l in fl)
    assert len(fl) <= len(all_)
    cl = escala.crew_legs(c, "2026-11-01", "2026-11-30")
    assert cl and all("data_origin" in x for x in cl)                                # a origem decide a tripulação composta
    after = escala.legs(c, after_utc=all_[5]["dep_utc"])
    assert after and all(l["dep_utc"] > all_[5]["dep_utc"] for l in after)
    raw = c.execute("SELECT COUNT(*) FROM roster_leg WHERE removed_batch IS NULL AND COALESCE(local_date, substr(dep_utc,1,10)) "
                    "BETWEEN '2026-11-01' AND '2026-11-30'").fetchone()[0]
    assert raw == len(all_)
    c.close()


def test_calendar_export(eng):  # noqa: F811
    data, career = eng
    _run(data, career, {**BASE, "seed": 3})
    c = db.connect(career)
    r = exportar.roster_ics(c, "2026-11-01", "2026-11-30", "GOL")
    ics = r["ics"]
    n = len(escala.legs(c, "2026-11-01", "2026-11-30"))
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.rstrip().endswith("END:VCALENDAR") and r["legs"] == n
    assert ics.count("BEGIN:VEVENT") == n + r["days"] and "X-WR-CALNAME:Escala GOL" in ics
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))               # RFC 5545
    offs = [d["date"] for d in (journey.day(c, f"2026-11-{k:02d}") for k in range(1, 31)) if d["day_off"]]
    if offs:
        assert "SUMMARY:Folga" in ics and f"DTSTART;VALUE=DATE:{offs[0].replace('-', '')}" in ics
    assert exportar._ics("a;b,c" + chr(10) + "d") == "a" + chr(92) + ";b" + chr(92) + ",c" + chr(92) + "nd"
    with pytest.raises(FGVError):
        exportar.roster_ics(c, "2026-11-30", "2026-11-01")
    c.close()


def test_achievements_come_from_the_logbook(tmp_path):
    c = db.connect(tmp_path / "c.db")
    db.migrate(c)
    assert conquistas.compute(c)["done"] == 0
    for k, (a, b, vs) in enumerate([("SBGR", "SBRF", -80), ("SBRF", "SBBE", -150), ("SBBE", "SBPA", -95), ("SBPA", "SBBR", -700),
                                    ("SBBR", "SAEZ", -60)]):
        c.execute("""INSERT INTO logbook_entry(date, adep, ades, type_icao, registration, block_min, night_min, landings, landing_vs_fpm,
                     landing_g, network, source, created_at) VALUES (?,?,?,?,?,?,?,1,?,1.2,'IVAO','MANUAL','x')""",
                  (f"2026-11-0{k + 1}", a, b, "B738", f"PR-G{k}A", 120, 30, vs))
    c.commit()
    r = {x["key"]: x for x in conquistas.compute(c)["items"]}
    assert r["primeiro"]["done"] and r["manteiga"]["current"] == 3 and r["exterior"]["done"]
    assert r["regioes"]["done"] and conquistas.compute(c)["regions"] == ["Centro-Oeste", "Nordeste", "Norte", "Sudeste", "Sul"]
    assert r["serie"]["current"] == 3                                                # o pouso de 700 fpm quebra a sequência
    assert r["h100"]["current"] == 10.0 and not r["h100"]["done"]
    c.close()


def test_profile_photo(tmp_path):
    u = {"id": 7, "username": "piloto"}
    assert avatar.of(u, tmp_path) is None
    with pytest.raises(FGVError):
        avatar.save(u, tmp_path, b"GIF89a....")
    with pytest.raises(FGVError):
        avatar.save(u, tmp_path, b"\xff\xd8\xff" + b"0" * (avatar.MAX_BYTES + 1))
    f = avatar.save(u, tmp_path, b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    assert f.name == "7.png" and avatar.of(u, tmp_path) == f and avatar.media_type(f) == "image/png"
    f2 = avatar.save(u, tmp_path, b"\xff\xd8\xff\xe0" + b"0" * 100)                  # trocar a foto apaga a anterior
    assert f2.name == "7.jpg" and not f.exists()
    assert avatar.remove(u, tmp_path) and avatar.of(u, tmp_path) is None
    cm = {"id": 8, "username": "cmdt079"}
    assert avatar.of(cm, tmp_path).name == "cmdt079.jpg"                              # sem foto enviada: a fixa dele
    avatar.save(cm, tmp_path, b"\x89PNG\r\n\x1a\n" + b"1" * 10)
    assert avatar.of(cm, tmp_path).name == "8.png"                                    # a enviada vale mais
