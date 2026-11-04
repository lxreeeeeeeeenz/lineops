"""Gerador completo do AeroScale na LineOps: regerar o mesmo mês não esvazia a escala, o máximo de pernas por dia da semana
é respeitado, o padrão salvo vale para a próxima escala, e "apagar todas as escalas" limpa só a escala."""
import shutil
import time
from datetime import date

import pytest

from lineops import careers, db, journey, motor, posicao
from lineops.aeroscale_import import import_backup
from lineops.errors import FGVError


def _run(data, career, body):
    j = motor.generate(data, "GLO", body, career)
    while motor.job(j["id"])["state"] == "running":
        time.sleep(0.2)
    r = motor.job(j["id"])
    assert r["state"] == "finished", r.get("error")
    return r["result"]


@pytest.fixture(scope="module")
def eng(tmp_path_factory):
    data = tmp_path_factory.mktemp("gerador") / "dados"
    motor.init(data)
    career = careers.career_path(data, "GLO")
    career.parent.mkdir(parents=True)
    c = db.connect(career)
    db.migrate(c)
    c.close()
    return data, career


def _engine_schedules(data):
    c = motor.connect(data)
    try:
        return c.execute("SELECT COUNT(*) FROM schedules WHERE airline='GLO'").fetchone()[0]
    finally:
        c.close()


BASE = {"kind": "mensal", "month": "2026-11", "base": "SBGR", "icao_type": "B738", "start_day": "2026-11-01", "replace_from_day": True}


def test_regenerating_the_same_month_does_not_starve(eng):
    """O bug: o 'não reutilizar voos já atribuídos' via a sua própria escala anterior como ocupada e cada regerar saía mais vazio."""
    data, career = eng
    counts = [_run(data, career, {**BASE, "seed": s})["legs"] for s in (3, 4, 5)]
    assert all(n > 0 for n in counts), counts
    assert min(counts) >= 0.6 * max(counts), counts                                 # não decai a cada geração
    assert _engine_schedules(data) == 1                                              # a nova substitui a antiga do mesmo período
    c = db.connect(career)
    alive = c.execute("SELECT COUNT(*) FROM roster_leg WHERE removed_batch IS NULL").fetchone()[0]
    c.close()
    assert alive == counts[-1]


def test_weekday_max_is_honoured(eng):
    data, career = eng
    res = _run(data, career, {**BASE, "seed": 11, "weekday_max": {"3": 0, "1": 1}})  # quinta folga, terça até 1 perna
    assert res["legs"] > 0
    c = db.connect(career)
    legs = [dict(r) for r in c.execute("SELECT * FROM roster_leg WHERE removed_batch IS NULL AND activity NOT IN ('SOBREAVISO','EXTRA')")]
    c.close()
    by = {}
    for l in legs:
        by.setdefault(l["local_date"], []).append(l)
    thu = [d for d in by if date.fromisoformat(d).weekday() == 3]
    tue = [len(v) for d, v in by.items() if date.fromisoformat(d).weekday() == 1]
    assert not thu, thu
    assert all(n <= 1 for n in tue), tue
    with pytest.raises(Exception):                                                   # fora de 0..12: erro claro, nada gerado
        _run(data, career, {**BASE, "weekday_max": {"2": 40}})


def test_saved_defaults_apply_to_the_next_schedule(eng):
    data, career = eng
    try:
        s = motor.save_defaults(data, {"weekday_max": {"6": 0}, "max_consecutive_days": 5})
        assert s["weekday_max"] == {"6": 0} and s["max_consecutive_days"] == 5
        assert motor.options(data, "GLO")["settings"]["weekday_max"] == {"6": 0}
        with pytest.raises(FGVError):
            motor.save_defaults(data, {"base": "SBGR"})                              # só dias da semana e regras
        with pytest.raises(FGVError):
            motor.save_defaults(data, {"weekday_max": {"9": 1}})
        _run(data, career, {**BASE, "seed": 6})                                      # sem weekday_max no pedido: usa o padrão
        c = db.connect(career)
        days = {r[0] for r in c.execute("SELECT local_date FROM roster_leg WHERE removed_batch IS NULL AND activity NOT IN ('SOBREAVISO','EXTRA')")}
        c.close()
        assert days and not any(date.fromisoformat(d).weekday() == 6 for d in days)
    finally:
        motor.save_defaults(data, {"weekday_max": {}, "max_consecutive_days": 6})


def test_wipe_all_clears_only_the_roster(eng, tmp_path):
    data, career = eng
    sid = _run(data, career, {**BASE, "seed": 3})["schedule_id"]
    p = tmp_path / "GLO.db"
    shutil.copy(career, p)
    c = db.connect(p)
    c.execute("INSERT INTO logbook_entry(date, adep, ades, type_icao, block_min, source, created_at) VALUES ('2026-10-01','SBGR','SBRJ','B738',60,'MANUAL','2026-10-01T12:00:00Z')")
    c.commit()
    pv = posicao.wipe_preview(c)
    assert pv["legs"] > 0 and pv["logbook"] == 1
    r = posicao.wipe_all(c, now="2026-11-02T12:00:00Z")
    assert r["removed"] == pv["legs"]
    assert c.execute("SELECT COUNT(*) FROM roster_leg WHERE removed_batch IS NULL").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM roster_reserve").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM logbook_entry").fetchone()[0] == 1        # o diário fica
    d = journey.day(c, "2026-11-20")
    assert not d["legs"] and not d["day_off"]                                        # nada de folga fantasma da escala apagada
    assert c.execute("SELECT COUNT(*) FROM ops_message WHERE key LIKE 'limpeza:%'").fetchone()[0] == 1
    dbp, _ = motor.paths(data)
    st = import_backup(c, str(dbp), airline="GLO", schedule_ids=[sid])               # o AeroScale manda a mesma escala de novo
    assert st["legs_restored"] == 0
    assert c.execute("SELECT COUNT(*) FROM roster_leg WHERE removed_batch IS NULL").fetchone()[0] == 0
    c.close()
    assert motor.wipe_engine(data, "GLO") >= 1 and _engine_schedules(data) == 0


def test_days_off_none_and_manual(eng):
    data, career = eng
    with pytest.raises(FGVError):                                                    # manual sem nenhum dia marcado
        motor.generate(data, "GLO", {**BASE, "days_off_mode": "manual"}, career)
    with pytest.raises(FGVError):
        motor.generate(data, "GLO", {**BASE, "days_off_mode": "talvez"}, career)
    res = _run(data, career, {**BASE, "seed": 3, "days_off_mode": "none", "weekday_max": {"3": 0}, "requested_off": ["2026-11-10"]})
    assert res["diagnostics"][0]["code"] == "sem_folgas"
    c = db.connect(career)
    days = [journey.day(c, f"2026-11-{k:02d}") for k in range(1, 31)]
    assert not any(d["day_off"] for d in days)                                       # nem a quinta "Folga", nem a pedida
    rep = journey.crew_report(c, "2026-11-01", "2026-11-30")
    assert sum(1 for v in rep["violations"] if v["level"] == "error") == 0          # o painel não cobra folga
    c.close()
    want = ["2026-11-07", "2026-11-08", "2026-11-21"]
    res = _run(data, career, {**BASE, "seed": 3, "days_off_mode": "manual", "requested_off": want})
    assert res["diagnostics"][0]["code"] == "folgas_manuais"
    c = db.connect(career)
    offs = [d["date"] for d in (journey.day(c, f"2026-11-{k:02d}") for k in range(1, 31)) if d["day_off"]]
    c.close()
    assert offs == want                                                              # só as escolhidas
