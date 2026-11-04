"""Apagar a escala daqui pra frente, memorizar onde o avião ficou, e gerar a escala nova (outra base) a partir dali."""
import json
import shutil
import time

import pytest

from lineops import db, journey, motor, posicao
from lineops.aeroscale_import import import_backup
from tests.test_troca import engine  # noqa: F401  (escala mensal gerada pelo motor)

NOW = "2026-11-10T15:00:00Z"


@pytest.fixture
def car(engine, tmp_path):  # noqa: F811
    data, career, sid = engine
    p = tmp_path / "carreiras" / "GLO.db"
    p.parent.mkdir(parents=True)
    shutil.copy(career, p)
    return data, p, sid


def test_cut_remembers_position_and_can_be_undone(car):
    data, p, sid = car
    c = db.connect(p)
    pv = posicao.preview(c, NOW)
    pos = pv["position"]
    assert pos["icao"] and pos["source"] in ("hoje", "ultimo") and pv["remove"]["legs"] > 0 and not pv["blocked"]
    alive = lambda: c.execute("SELECT COUNT(*) FROM roster_leg WHERE removed_batch IS NULL AND dep_utc>?", (pos["at_utc"],)).fetchone()[0]
    before = alive()
    before_all = c.execute("SELECT COUNT(*) FROM roster_leg WHERE removed_batch IS NULL").fetchone()[0]
    r = posicao.cut(c, NOW)
    assert r["removed"] == before and alive() == 0
    assert c.execute("SELECT COUNT(*) FROM roster_leg WHERE removed_batch IS NULL").fetchone()[0] == before_all - before   # o passado fica
    m = posicao.memorized(c)
    assert m["icao"] == pos["icao"] and m["at_utc"] == pos["at_utc"]
    s = c.execute("SELECT end_date, params FROM roster_schedule ORDER BY id DESC LIMIT 1").fetchone()
    assert s["end_date"] == pos["date"] and all(d <= pos["date"] for d in json.loads(s["params"]).get("planned_days_off") or [])
    after_day = journey.day(c, "2026-11-25")
    assert not after_day["legs"] and not after_day["day_off"]                       # nada de folga fantasma depois do corte
    msg = c.execute("SELECT body FROM ops_message WHERE key LIKE 'corte:%'").fetchone()[0]
    assert pos["icao"] in msg and "apaguei" in msg
    # o AeroScale manda a escala antiga de novo: o que foi apagado continua fora
    dbp, _ = motor.paths(data)
    st = import_backup(c, str(dbp), airline="GLO", schedule_ids=[sid])
    assert st["legs_restored"] == 0 and alive() == 0
    assert posicao.can_undo(c)
    assert posicao.undo(c)["restored"] == before and alive() == before and posicao.memorized(c) is None
    c.close()


def test_new_base_starts_where_the_plane_stopped(car):
    data, p, _sid = car
    c = db.connect(p)
    pos = posicao.cut(c, NOW)["position"]
    c.close()
    with pytest.raises(Exception):                                                  # período que acaba antes do dia seguinte
        motor.generate(data, "GLO", {"kind": "avulsa", "start_date": "2026-11-01", "end_date": pos["date"], "base": "SBBR",
                                     "icao_type": "B738", "from_position": True}, p)
    j = motor.generate(data, "GLO", {"kind": "mensal", "month": "2026-11", "base": "SBBR", "icao_type": "B738", "seed": 7,
                                     "from_position": True}, p)
    while motor.job(j["id"])["state"] == "running":
        time.sleep(0.2)
    jj = motor.job(j["id"])
    assert jj["state"] == "finished", jj.get("error")
    res = jj["result"]
    assert res["start_date"] > pos["date"] and res["end_date"] == "2026-11-30" and res["legs"] > 0
    assert any("onde o seu avião parou" in d["message"] for d in res["diagnostics"])
    c = db.connect(p)
    new = [dict(r) for r in c.execute("SELECT * FROM roster_leg WHERE removed_batch IS NULL AND dep_utc>? ORDER BY dep_utc", (pos["at_utc"],))]
    from lineops import rules
    city = lambda x: (rules.airport(c, x) or {}).get("city")
    assert new and (new[0]["adep"] == pos["icao"] or city(new[0]["adep"]) == city(pos["icao"]))   # mesma cidade: GRU↔CGH por terra, como no AeroScale
    assert any(l["ades"] == "SBBR" for l in new)                                    # o motor leva para a base nova
    rep = journey.crew_report(c, "2026-11-01", "2026-11-30")
    assert sum(1 for d in rep["duties"] for i in d["issues"] if i["level"] == "error") == 0
    assert posicao.memorized(c) is None and not posicao.can_undo(c)
    with pytest.raises(Exception):                                                  # a escala já continua: não gera de novo dali
        motor.generate(data, "GLO", {"kind": "mensal", "month": "2026-11", "base": "SBBR", "icao_type": "B738", "from_position": True}, p)
    c.close()


def test_generate_from_chosen_day_replaces_the_rest_of_the_month(car):
    """Mensal a partir do dia 15: o que vem antes fica, o resto é refeito continuando de onde o avião estava."""
    data, p, _sid = car
    c = db.connect(p)
    before = [dict(r) for r in c.execute("SELECT id, uid FROM roster_leg WHERE removed_batch IS NULL AND COALESCE(local_date, substr(dep_utc,1,10))<'2026-11-15'")]
    old_after = [r[0] for r in c.execute("SELECT id FROM roster_leg WHERE removed_batch IS NULL AND COALESCE(local_date, substr(dep_utc,1,10))>='2026-11-15'")]
    pos = posicao.position_before(c, "2026-11-15")
    c.close()
    assert old_after
    with pytest.raises(Exception):                                                  # sem "substituir", não sobrepõe
        motor.generate(data, "GLO", {"kind": "mensal", "month": "2026-11", "start_day": "2026-11-15", "replace_from_day": False,
                                     "base": "SBGR", "icao_type": "B738"}, p)
    with pytest.raises(Exception):                                                  # fora do mês escolhido
        motor.generate(data, "GLO", {"kind": "mensal", "month": "2026-11", "start_day": "2026-12-03", "base": "SBGR", "icao_type": "B738"}, p)
    j = motor.generate(data, "GLO", {"kind": "mensal", "month": "2026-11", "start_day": "2026-11-15", "base": "SBGR",
                                     "icao_type": "B738", "seed": 9}, p)
    while motor.job(j["id"])["state"] == "running":
        time.sleep(0.2)
    jj = motor.job(j["id"])
    assert jj["state"] == "finished", jj.get("error")
    assert jj["result"]["start_date"] == "2026-11-15" and jj["result"]["end_date"] == "2026-11-30"
    c = db.connect(p)
    still = {r[0] for r in c.execute("SELECT id FROM roster_leg WHERE removed_batch IS NULL")}
    assert all(b["id"] in still for b in before)                                    # antes do dia 15 nada muda
    assert not any(i in still for i in old_after)                                   # do dia 15 em diante, só a escala nova
    new = [dict(r) for r in c.execute("SELECT * FROM roster_leg WHERE removed_batch IS NULL AND COALESCE(local_date, substr(dep_utc,1,10))>='2026-11-15' ORDER BY dep_utc")]
    assert new and all(l["local_date"] >= "2026-11-15" for l in new)
    if pos:
        from lineops import rules
        city = lambda x: (rules.airport(c, x) or {}).get("city")
        assert new[0]["adep"] == pos["icao"] or city(new[0]["adep"]) == city(pos["icao"])   # continua de onde o avião estava
    rep = journey.crew_report(c, "2026-11-01", "2026-11-30")
    assert sum(1 for d in rep["duties"] for i in d["issues"] if i["level"] == "error") == 0
    c.close()
