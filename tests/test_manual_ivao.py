"""“Já fiz esse voo”, acompanhamento pela IVAO (whazzup simulado) e mensagens do CCO."""
import json

import pytest

from lineops import aeroscale_import as ai, feed, fleet, flights, ivao, manual, payroll
from lineops.errors import Conflict, FGVError
from tests.test_importer import L, make_db


@pytest.fixture
def base(conn, tmp_path):
    p = tmp_path / "as.db"
    make_db(p, legs=[L("a", "GLO1748", dep="2026-10-20T10:00:00Z", arr="2026-10-20T12:00:00Z"),
                     L("b", "GLO1749", dep="2026-10-21T10:00:00Z", arr="2026-10-21T12:00:00Z")])
    ai.import_backup(conn, p)
    ac = fleet.add_aircraft(conn, registration="PR-GUZ", airline="GLO", type_icao="B738")
    legs = {r["uid"]: r["id"] for r in conn.execute("SELECT id, uid FROM roster_leg")}
    return conn, ac["id"], legs


def test_manual_from_unbooked_leg_goes_to_logbook_and_ledger(base):
    conn, aid, legs = base
    f = manual.log_flight(conn, leg_id=legs["a"], aircraft_id=aid, out_utc="2026-10-20T10:05", off_utc="2026-10-20T10:20",
                          on_utc="2026-10-20T11:55", in_utc="2026-10-20T12:03", network="ivao", landing_vs_fpm=180, landing_g=1.21)
    assert f["state"] == "APROVADO" and f["warnings"] == []
    e = conn.execute("SELECT * FROM logbook_entry WHERE id=?", (f["logbook_id"],)).fetchone()
    assert e["source"] == "MANUAL" and e["block_min"] == 118 and e["air_min"] == 95 and e["network"] == "IVAO" and e["landing_vs_fpm"] == -180 and e["landing_g"] == 1.21
    led = conn.execute("SELECT * FROM usage_ledger WHERE logbook_id=?", (f["logbook_id"],)).fetchone()
    assert led["block_min"] == 118 and led["air_min"] == 95 and led["cycles"] == 1
    with pytest.raises(Conflict):                                   # já está no diário
        manual.log_flight(conn, flight_id=f["id"], out_utc="2026-10-20T10:05", in_utc="2026-10-20T12:03", landing_vs_fpm=200, landing_g=1.2)
    # salário realizado enxerga o voo declarado
    payroll.set_enabled(conn, True)
    payroll.save_contract(conn, "2026-10", {"role": "Comandante", "category": "jato_nb"})
    assert payroll.compute(conn, "2026-10", "realizado", now="2026-11-02T12:00:00Z")["hours"]["flight_min"] == 118
    manual.reverse(conn, f["logbook_id"], reason="lancei errado")
    assert flights.get_flight(conn, f["id"])["state"] == "RESERVADO"
    assert conn.execute("SELECT reversed_at FROM usage_ledger WHERE logbook_id=?", (f["logbook_id"],)).fetchone()[0]
    with pytest.raises(Conflict):
        manual.reverse(conn, f["logbook_id"], reason="de novo")
    texts = [x["text"] for x in feed.ops_feed(conn)]
    assert any("voo declarado" in t for t in texts) and any("estornado" in t for t in texts)


def test_manual_validation(base):
    conn, aid, legs = base
    with pytest.raises(Conflict):                                   # sem o toque não entra no diário
        manual.log_flight(conn, leg_id=legs["a"], out_utc="2026-10-20T10:00", in_utc="2026-10-20T12:00")
    T = {"landing_vs_fpm": 200, "landing_g": 1.2}
    with pytest.raises(FGVError):
        manual.log_flight(conn, leg_id=legs["a"], out_utc="2026-10-20T12:00", in_utc="2026-10-20T10:00", **T)
    with pytest.raises(FGVError):
        manual.log_flight(conn, leg_id=legs["a"], out_utc="2026-10-20T10:00", off_utc="2026-10-20T10:10", in_utc="2026-10-20T12:00", **T)
    with pytest.raises(FGVError):
        manual.log_flight(conn, leg_id=legs["a"], out_utc="2099-10-20T10:00", in_utc="2099-10-20T12:00", **T)
    f = manual.log_flight(conn, leg_id=legs["b"], out_utc="2026-10-21T10:00", in_utc="2026-10-21T12:00", **T)    # sem aeronave
    assert f["warnings"] and conn.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0] == 0


def _whazzup(track, callsign="GLO1748", vid=123456, fp=("SBBR", "SBFL")):
    pilot = {"id": 999, "userId": vid, "callsign": callsign,
             "flightPlan": {"departureId": fp[0], "arrivalId": fp[1], "aircraftId": "B738"}, "lastTrack": track}
    return {"updatedAt": "2026-10-20T10:00:00Z", "clients": {"pilots": [pilot] if track else [],
            "atcs": [{"callsign": "SBBR_TWR", "atcSession": {"frequency": 118.1, "position": "TWR"}},
                     {"callsign": "SBGR_APP", "atcSession": {"frequency": 129.75}}]}}


def T(ts, lat, lon, alt, gs, on_ground, state="En Route"):
    return {"timestamp": ts + ".000Z", "latitude": lat, "longitude": lon, "altitude": alt, "groundSpeed": gs, "heading": 180,
            "onGround": on_ground, "state": state, "arrivalDistance": 100.0}


def test_ivao_tracking_end_to_end(base):
    conn, aid, legs = base
    conn.execute("INSERT INTO setting(key, value) VALUES ('ivao_vid', '123456')")
    f = flights.book_leg(conn, legs["a"], aircraft_id=aid)
    s = ivao.start(conn, f["id"])
    assert flights.get_flight(conn, f["id"])["state"] == "EM_VOO"
    seq = [None,                                                                    # ainda não conectou
           T("2026-10-20T10:00:00", -15.87, -47.92, 3500, 0, True, "Boarding"),
           T("2026-10-20T10:05:00", -15.87, -47.92, 3500, 12, True, "Departing"),
           T("2026-10-20T10:15:00", -15.90, -47.93, 5000, 160, False, "Initial Climb"),
           T("2026-10-20T11:00:00", -21.0, -48.2, 37000, 450, False, "En Route"),
           T("2026-10-20T11:50:00", -27.67, -48.54, 50, 130, True, "Landed"),
           T("2026-10-20T11:56:00", -27.67, -48.55, 50, 0, True, "Landed"),
           T("2026-10-20T11:56:15", -27.67, -48.55, 50, 0, True, "Landed")]
    for i, tr in enumerate(seq):
        data = json.dumps(_whazzup(tr)).encode()
        ivao._cache.update(at=0, data=None)                                         # força nova leitura
        ivao.tick(conn, opener=lambda url, d=data: d, now=f"2026-10-20T12:{i:02d}:00Z")
    sess = conn.execute("SELECT * FROM acars_session WHERE id=?", (s["id"],)).fetchone()
    assert sess["status"] == "COMPLETED"
    p = conn.execute("SELECT * FROM pirep WHERE session_id=?", (s["id"],)).fetchone()
    d = json.loads(p["data"])
    assert (d["out_utc"], d["off_utc"], d["on_utc"], d["in_utc"]) == ("2026-10-20T10:05:00Z", "2026-10-20T10:15:00Z",
                                                                      "2026-10-20T11:50:00Z", "2026-10-20T11:56:00Z")
    assert d["block_min"] == 111 and d["air_min"] == 95 and d["max_alt_ft"] == 37000 and d["network"] == "IVAO"
    assert d["webeye"].endswith("callsign=GLO1748") and "IVAO" in d["measured_by"]
    assert any(w["code"] == "ivao_limites" for w in json.loads(p["warnings"]))
    assert flights.get_flight(conn, f["id"])["state"] == "PIREP_RECEBIDO"
    msgs = [m["text"] for m in feed.flight_feed(conn, f["id"])]
    assert any("Encontrado na IVAO" in m for m in msgs) and any("Decolagem" in m for m in msgs)


def test_ivao_disconnect_after_landing_closes(base):
    conn, aid, legs = base
    conn.execute("INSERT INTO setting(key, value) VALUES ('ivao_vid', '123456')")
    f = flights.book_leg(conn, legs["a"], aircraft_id=aid)
    s = ivao.start(conn, f["id"])
    for i, tr in enumerate([T("2026-10-20T10:05:00", -15.87, -47.92, 3500, 10, True), T("2026-10-20T10:15:00", -15.9, -47.9, 6000, 170, False),
                            T("2026-10-20T11:50:00", -27.67, -48.54, 50, 120, True), None]):
        data = json.dumps(_whazzup(tr, fp=("SBBR", "SBSP"))).encode()
        ivao._cache.update(at=0, data=None)
        ivao.tick(conn, opener=lambda url, d=data: d, now=f"2026-10-20T12:{i:02d}:00Z")
    d = json.loads(conn.execute("SELECT data FROM pirep WHERE session_id=?", (s["id"],)).fetchone()[0])
    assert d["in_utc"] == "2026-10-20T11:50:00Z"
    msgs = [m["text"] for m in feed.flight_feed(conn, f["id"])]
    assert any("difere do voo" in m for m in msgs)


def test_ivao_needs_vid_and_pulse(base):
    conn, aid, legs = base
    f = flights.book_leg(conn, legs["a"], aircraft_id=aid)
    with pytest.raises(Conflict):
        ivao.start(conn, f["id"])
    data = json.dumps(_whazzup(T("2026-10-20T10:05:00", -15.87, -47.92, 37000, 450, False))).encode()
    ivao._cache.update(at=0, data=None)
    pl = ivao.pulse("GLO", ["SBBR", "SBFL"], vid="123456", opener=lambda url: data)
    assert pl["airline_count"] == 1 and pl["me"]["callsign"] == "GLO1748"
    assert [a["callsign"] for a in pl["atc"]] == ["SBBR_TWR"] and pl["atc"][0]["frequency"] == 118.1
    assert pl["traffic"]["SBBR"]["dep"] == 1 and pl["traffic"]["SBFL"]["arr"] == 1


def test_ivao_gap_while_lineops_was_closed(base):
    """Atualizou a LineOps em cruzeiro: o acompanhamento continua de onde parou e a lacuna fica registrada."""
    conn, aid, legs = base
    conn.execute("INSERT INTO setting(key, value) VALUES ('ivao_vid', '123456')")
    f = flights.book_leg(conn, legs["a"], aircraft_id=aid)
    s = ivao.start(conn, f["id"])
    seq = [T("2026-10-20T10:05:00", -15.87, -47.92, 3500, 12, True, "Departing"),
           T("2026-10-20T10:15:00", -15.90, -47.93, 5000, 160, False, "Initial Climb"),
           T("2026-10-20T10:15:15", -15.91, -47.93, 5600, 165, False, "Initial Climb"),
           T("2026-10-20T10:40:15", -19.0, -48.0, 37000, 450, False, "En Route"),      # 25 min fechada
           T("2026-10-20T10:40:30", -19.1, -48.0, 37000, 450, False, "En Route")]
    for i, tr in enumerate(seq):
        data = json.dumps(_whazzup(tr)).encode()
        ivao._cache.update(at=0, data=None)
        ivao.tick(conn, opener=lambda url, d=data: d, now=f"2026-10-20T12:{i:02d}:00Z")
    snap = json.loads(conn.execute("SELECT snapshot FROM acars_session WHERE id=?", (s["id"],)).fetchone()[0])
    assert snap["off_utc"] == "2026-10-20T10:15:00Z" and snap["phase"] == "CRUISE"
    assert {"from": "2026-10-20T10:15:15Z", "to": "2026-10-20T10:40:15Z", "min": 25} in snap["gaps"]
    msgs = [m["text"] for m in feed.flight_feed(conn, f["id"])]
    assert any("25 min sem leitura" in m and "o acompanhamento continua" in m for m in msgs)
