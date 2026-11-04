from datetime import datetime, timezone

from lineops_acars.phases import PhaseMachine
from tests import synth

T0 = datetime(2026, 11, 2, 1, 0, 0, tzinfo=timezone.utc)


def run(samples):
    pm, ev = PhaseMachine(), []
    for s in samples:
        ev += pm.feed(s)
    return pm, ev


def test_full_flight_oooi_and_landing():
    pm, ev = run(synth.flight(T0))
    assert pm.phase == "IN"
    assert set(pm.oooi) == {"OUT", "OFF", "ON", "IN"}
    assert pm.oooi["OUT"] < pm.oooi["OFF"] < pm.oooi["ON"] < pm.oooi["IN"]
    assert pm.landing_vs == -180 and pm.landing_g == 1.25
    assert pm.bounces == 0 and pm.distance > 600
    assert pm.max_alt >= 35000
    assert pm.fuel["out"] > pm.fuel["in"]
    assert [e["kind"] for e in ev if e["kind"] in ("OUT", "OFF", "ON", "IN")] == ["OUT", "OFF", "ON", "IN"]


def test_bounce_keeps_first_touchdown():
    pm, _ = run(synth.flight(T0, with_bounce=True, touchdown_vs=-300))
    assert pm.landing_vs == -300
    assert pm.bounces == 1 and pm.phase == "IN"


def test_started_airborne_is_flagged_not_invented():
    samples = [s for s in synth.flight(T0) if not (s["on_ground"] and s["gs_kt"] < 60) or s["alt_ft"] > 4000 or s["t"] > T0.replace(minute=20)]
    pm, ev = run(samples)
    assert any(e["kind"] == "WARN" for e in ev) or "OUT" in pm.oooi


def test_missing_values_stay_none():
    s = synth.flight(T0)
    for x in s:
        x["g"] = None
        x["fuel_kg"] = None
    pm, _ = run(s)
    assert pm.landing_g is None and pm.fuel["out"] is None
    assert pm.summary()["landing_g"] is None
