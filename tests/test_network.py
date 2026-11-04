"""Rede: cartões dos pilotos da companhia na IVAO, relações com a sua escala, amostras e colegas."""
import json

import pytest

from lineops import aeroscale_import as ai, feed, ivao, network
from lineops.errors import FGVError, NotFound
from tests.test_importer import L, make_db


def P(cs, vid, dep, arr, alt, gs, og, state, dd=None, ad=None, rating=7):
    return {"id": vid * 10, "userId": vid, "callsign": cs, "rating": rating, "createdAt": "2026-11-02T08:00:00.000Z",
            "pilotSession": {"simulatorId": "MSFS"},
            "flightPlan": {"departureId": dep, "arrivalId": arr, "aircraftId": "B738", "level": "F370"},
            "lastTrack": {"latitude": -20.0, "longitude": -45.0, "altitude": alt, "groundSpeed": gs, "heading": 200, "onGround": og,
                          "state": state, "departureDistance": dd, "arrivalDistance": ad, "timestamp": "2026-11-02T09:00:00.000Z"}}


def W(*pilots):
    return json.dumps({"updatedAt": "x", "clients": {"pilots": list(pilots), "atcs": []}}).encode()


@pytest.fixture
def conn2(conn, tmp_path):
    p = tmp_path / "as.db"
    make_db(p, legs=[L("a", "GLO1748", dep="2026-11-02T14:00:00Z", arr="2026-11-02T16:00:00Z")])   # próxima perna SBBR→SBFL
    ai.import_backup(conn, p)
    conn.execute("INSERT INTO setting(key, value) VALUES ('ivao_vid', '111')")
    ivao._cache.update(at=0, data=None)
    return conn


def test_overview_cards_and_relations(conn2):
    data = W(P("GLO1000", 111, "SBGR", "SBFL", 35000, 450, False, "En Route", 200, 100),          # você
             P("GLO2000", 222, "SBRJ", "SBFL", 30000, 440, False, "En Route", 100, 300),          # mesmo destino que você
             P("GLO3000", 333, "SBBR", "SBFL", 0, 0, True, "Boarding", 0, 709, rating=5),       # sua próxima perna
             P("TAM4000", 444, "SBGR", "SBBR", 0, 0, True, "Boarding"))
    o = network.overview(conn2, now="2026-11-02T09:00:00Z", opener=lambda u: data)
    cs = [c["callsign"] for c in o["pilots"]]
    assert cs[0] == "GLO1000" and "TAM4000" not in cs and o["counts"] == {"online": 3, "airborne": 2, "ground": 1, "network_pilots": 4}
    me = o["pilots"][0]
    assert me["is_me"] and me["progress"] == 67 and me["eta_utc"] and me["rating_label"] == "CP" and me["simulator"] == "MSFS"
    c2 = next(c for c in o["pilots"] if c["vid"] == 222)
    assert "vai para SBFL como você" in c2["relations"]
    c3 = next(c for c in o["pilots"] if c["vid"] == 333)
    assert any("próxima perna" in r for r in c3["relations"]) and c3["rating_label"] == "PP" and c3["eta_utc"] is None
    assert o["today"]["pilots"] == 3 and ("SBRJ→SBFL", 1) in o["routes"] and len(o["map_airports"]) > 50


def test_sightings_history_together_and_friends(conn2):
    t1 = W(P("GLO1000", 111, "SBGR", "SBFL", 35000, 450, False, "En Route", 200, 100),
           P("GLO2000", 222, "SBRJ", "SBFL", 30000, 440, False, "En Route", 100, 300))
    t2 = W(P("GLO2000", 222, "SBRJ", "SBFL", 50, 20, True, "Landed", 400, 3))
    assert network.maybe_record(conn2, now="2026-11-02T09:00:00Z", opener=lambda u: t1) == 2
    assert network.maybe_record(conn2, now="2026-11-02T09:02:00Z", opener=lambda u: t1) == 0          # menos de 5 min
    ivao._cache.update(at=0, data=None)
    network.maybe_record(conn2, now="2026-11-02T09:10:00Z", opener=lambda u: t2)
    r = conn2.execute("SELECT * FROM ivao_sighting WHERE vid=222").fetchone()
    assert r["samples"] == 2 and r["airborne"] == 1 and r["arrived"] == 1 and r["max_alt_ft"] == 30000
    h = network.pilot_history(conn2, 222)
    assert h["flights"] == 1 and h["routes"] == [("SBRJ→SBFL", 1)] and h["together"][0]["my_callsign"] == "GLO1000"
    f = network.add_friend(conn2, "222", "Zé do 737")
    assert f["nickname"] == "Zé do 737"
    o = network.overview(conn2, now="2026-11-02T09:11:00Z", opener=lambda u: t2)
    assert o["friends"][0]["online"] and o["pilots"][0]["friend"]["nickname"] == "Zé do 737"
    assert any(x["vid"] == 222 and x["friend"] for x in o["frequent"])
    network.remove_friend(conn2, 222)
    with pytest.raises(NotFound):
        network.remove_friend(conn2, 222)
    with pytest.raises(FGVError):
        network.add_friend(conn2, "abc")


def test_old_sightings_are_pruned(conn2):
    network.record(conn2, json.loads(W(P("GLO2000", 222, "SBRJ", "SBFL", 0, 0, True, "Boarding")).decode()), "GLO", now="2026-01-01T00:00:00Z")
    network.record(conn2, json.loads(W(P("GLO5000", 555, "SBRJ", "SBFL", 0, 0, True, "Boarding")).decode()), "GLO", now="2026-11-02T00:00:00Z")
    assert [r[0] for r in conn2.execute("SELECT vid FROM ivao_sighting")] == [555]
