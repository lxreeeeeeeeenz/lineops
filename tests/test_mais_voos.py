"""3.0.3: mínimo de pernas em dias escolhidos e "Mais voos" (regerar a partir de um dia)."""
from collections import Counter

import pytest

from lineops import db, escala, journey, motor
from lineops.errors import FGVError
from tests.test_gerador import BASE, _run, eng  # noqa: F401


def _per_day(career):
    c = db.connect(career)
    try:
        return Counter(l["local_date"] for l in escala.legs(c, "2026-11-01", "2026-11-30", flights_only=True) if l.get("activity") != "EXTRA")
    finally:
        c.close()


def test_more_flights_from_a_day(eng):  # noqa: F811
    data, career = eng
    _run(data, career, {**BASE, "seed": 3})
    before = _per_day(career)
    day = next(d for d, n in sorted(before.items()) if d >= "2026-11-10" and n <= 2)
    j = motor.more_flights(data, "GLO", career, day, {day: 3})
    while motor.job(j["id"])["state"] == "running":
        import time
        time.sleep(0.2)
    r = motor.job(j["id"])
    assert r["state"] == "finished", r.get("error")
    note = next(x for x in r["result"]["diagnostics"] if (x.get("code") or "").startswith("minimo_pernas"))
    after = _per_day(career)
    assert (after[day] >= 3) == (note["code"] == "minimo_pernas_ok")             # o diagnóstico bate com a escala
    assert all(after[d] == before[d] for d in before if d < day)                 # antes do dia nada muda
    c = db.connect(career)
    rep = journey.crew_report(c, "2026-11-01", "2026-11-30")
    assert sum(1 for x in rep["duties"] for i in x["issues"] if i["level"] == "error") == 0
    c.close()
    with pytest.raises(FGVError):
        motor.more_flights(data, "GLO", career, "2027-05-01", {"2027-05-01": 3})  # sem escala nesse dia
    with pytest.raises(FGVError):
        _run(data, career, {**BASE, "seed": 3, "day_legs_min": {"2026-11-12": 9}})


def test_start_airport_only_moves_the_first_day(eng):  # noqa: F811
    data, career = eng
    _run(data, career, {**BASE, "seed": 3})
    c = db.connect(career)
    day = next(l["local_date"] for l in escala.legs(c, "2026-11-12", "2026-11-30", flights_only=True))
    c.close()
    j = motor.more_flights(data, "GLO", career, day, {}, start_airport="REC")
    import time
    while motor.job(j["id"])["state"] == "running":
        time.sleep(0.2)
    r = motor.job(j["id"])
    assert r["state"] == "finished", r.get("error")
    assert any("onde você disse que a aeronave está" in d["message"] for d in r["result"]["diagnostics"])
    c = db.connect(career)
    first = escala.legs(c, day, "2026-11-30", flights_only=True)[0]
    assert first["adep"] == "SBRF"
    c.close()
    with pytest.raises(FGVError):
        _run(data, career, {**BASE, "seed": 3, "start_airport": "REC", "start_day": None})
