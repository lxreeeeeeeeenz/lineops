"""Voos Externos: long haul real do ADS-B, voo em grupo, convite, SimBrief, radar IVAO/VATSIM e pódio."""
import json

import pytest

from lineops import externos, ivao
from lineops.errors import FGVError, NotFound

NOW = "2026-11-02T12:00:00Z"
PLANES = {
    "B77W": [{"hex": "06a1", "flight": "QTR773  ", "t": "B77W", "r": "A7-BEA", "lat": 5.0, "lon": 10.0, "alt_baro": 37000, "gs": 490},
             {"hex": "06a2", "flight": "UAE261 ", "t": "B77W", "r": "A6-EGA", "lat": 20.0, "lon": 50.0, "alt_baro": 3000, "gs": 220}],   # baixo: fora
    "A359": [{"hex": "e48", "flight": "TAM8084", "t": "A359", "r": "PR-XTA", "lat": 30.0, "lon": -20.0, "alt_baro": 39000, "gs": 500},
             {"hex": "e49", "flight": "AFR456", "t": "A359", "lat": 40.0, "lon": -10.0, "alt_baro": 38000, "gs": 480},
             {"hex": "e50", "flight": "N123AB", "t": "A359", "lat": 1, "lon": 1, "alt_baro": 30000}],                                      # sem callsign de linha
    "A333": [{"hex": "a1", "flight": "TAP101", "t": "A333", "lat": 38.9, "lon": -9.0, "alt_baro": 25000, "gs": 400}],
}
ROUTES = [
    {"callsign": "QTR773", "airport_codes": "OTHH-SBGR", "plausible": 1,
     "_airports": [{"icao": "OTHH", "location": "Doha", "lat": 25.27, "lon": 51.61}, {"icao": "SBGR", "location": "São Paulo", "lat": -23.43, "lon": -46.47}]},
    {"callsign": "TAM8084", "airport_codes": "LFPG-SBGR", "plausible": True},
    {"callsign": "AFR456", "airport_codes": "LFPG-SBGR", "plausible": False},              # rota implausível: fora
    {"callsign": "TAP101", "airport_codes": "LPPT-LPPR", "plausible": 1},                  # curto: fora
]


def adsb(url, data=None):
    if "/v2/type/" in url:
        return json.dumps({"ac": PLANES.get(url.rsplit("/", 1)[1], [])}).encode()
    if url.endswith("/routeset"):
        asked = {p["callsign"] for p in json.loads(data)["planes"]}
        return json.dumps([r for r in ROUTES if r["callsign"] in asked]).encode()
    raise AssertionError(url)


def test_fetch_catalog_and_draw(conn):
    r = externos.fetch_live(conn, opener=adsb, now=NOW)
    assert r["kept"] == 2 and r["candidates"] == 4
    items = externos.catalog(conn, now=NOW)
    cs = {i["callsign"]: i for i in items}
    assert set(cs) == {"QTR773", "TAM8084"}
    q = cs["QTR773"]
    assert q["adep"] == "OTHH" and q["ades"] == "SBGR" and q["airline_name"] == "Qatar Airways" and q["family"] == "777"
    assert 11500 < q["km"] < 12500 and 13 * 60 < q["est_block_min"] < 15 * 60 and q["registration"] == "A7-BEA" and q["est_dep_utc"] < NOW
    assert [i["callsign"] for i in externos.catalog(conn, family="A350", now=NOW)] == ["TAM8084"]
    assert [i["callsign"] for i in externos.catalog(conn, origin="LF", now=NOW)] == ["TAM8084"]
    assert {i["callsign"] for i in externos.catalog(conn, dest="BR", now=NOW)} == {"QTR773", "TAM8084"}
    assert externos.catalog(conn, min_h=13.5, now=NOW)[0]["callsign"] == "QTR773"
    assert externos.draw(conn, seed=1, family="777", now=NOW)["callsign"] == "QTR773"
    externos.fetch_live(conn, opener=adsb, now="2026-11-02T13:00:00Z")
    assert conn.execute("SELECT times_seen FROM ext_seen WHERE callsign='QTR773'").fetchone()[0] == 2
    assert externos.catalog(conn, now="2026-11-20T12:00:00Z") == []                         # mais de 7 dias: some do sorteio


def test_group_flight_invite_simbrief_radar_and_podium(conn):
    externos.fetch_live(conn, opener=adsb, now=NOW)
    with pytest.raises(FGVError):
        externos.create(conn, callsign="QTR773", dep_utc="2026-11-07T21:00", members=[])
    f = externos.create(conn, callsign="QTR773", dep_utc="2026-11-07T21:00", stagger_min=3, notes="Discord às 20:30Z",
                        members=[{"name": "Triple", "network": "IVAO"}, {"name": "João", "network": "VATSIM"}, {"name": "Pedro", "network": "IVAO", "type_icao": "B789"}])
    assert f["source"] == "ADSB" and f["type_icao"] == "B77W" and f["real_type"] == "B77W" and f["airline_name"] == "Qatar Airways"
    m = f["members"]
    assert [x["callsign"] for x in m] == ["QTR773", "QTR773B", "QTR773C"] and [x["dep_utc"][11:16] for x in m] == ["21:00", "21:03", "21:06"]
    assert m[2]["type_icao"] == "B789" and "orig=OTHH" in m[0]["simbrief"] and "type=B77W" in m[0]["simbrief"] and "fltnum=773" in m[0]["simbrief"]
    inv = f["invite"]
    assert "QTR773 Doha → São Paulo" in inv and "sáb 07/11 · saída 21:00Z" in inv and "21:03Z · João · VATSIM · QTR773B" in inv and "B789" in inv
    assert "Discord às 20:30Z" in inv and "simbrief.com" in inv
    # radar: Triple na IVAO, João na VATSIM, Pedro offline
    wz = json.dumps({"clients": {"pilots": [{"callsign": "QTR773", "userId": 1, "flightPlan": {"departureId": "OTHH", "arrivalId": "SBGR"},
                                             "lastTrack": {"latitude": 0.0, "longitude": 0.0, "altitude": 37000, "groundSpeed": 495, "onGround": False}}], "atcs": []}}).encode()
    vt = json.dumps({"pilots": [{"callsign": "QTR773B", "latitude": 10.0, "longitude": 10.0, "altitude": 36000, "groundspeed": 480,
                                 "flight_plan": {"departure": "OTHH", "arrival": "SBGR"}}]}).encode()
    ivao._cache.update(at=0, data=None)
    r = externos.live(conn, f["id"], ivao_opener=lambda u: wz, vatsim_opener=lambda u, d=None: vt)
    st = {x["name"]: x for x in r["members"]}
    assert st["Triple"]["online"] and st["Triple"]["plan_ok"] and 0 < st["Triple"]["progress"] < 100
    assert st["João"]["online"] and st["João"]["alt_ft"] == 36000 and not st["Pedro"]["online"]
    # resultados e pódio
    externos.save_result(conn, m[0]["id"], block="14:32", landing_fpm=-180, landing_g=1.18)
    externos.save_result(conn, m[1]["id"], block="14:40", landing_fpm=95, landing_g=1.05)
    with pytest.raises(FGVError):
        externos.save_result(conn, m[2]["id"], block="14h3")
    g = externos.get(conn, f["id"])
    assert g["status"] == "VOADO" and [p["name"] for p in g["podium"]] == ["João", "Triple"] and g["members"][0]["block_min"] == 872
    lb = externos.leaderboard(conn)
    assert lb[0]["name"] == "João" and lb[0]["best_fpm"] == 95
    lst = externos.list_flights(conn)
    assert lst["past"][0]["id"] == f["id"]
    externos.cancel(conn, f["id"])
    assert externos.list_flights(conn)["past"] == [] and externos.leaderboard(conn) == []
    with pytest.raises(NotFound):
        externos.get(conn, 999)


def test_manual_route_without_adsb(conn):
    with pytest.raises(FGVError):
        externos.create(conn, adep="SBGR", ades="SBGR", dep_utc="2026-11-07T21:00", members=[{"name": "A"}])
    f = externos.create(conn, adep="sbgr", ades="lppt", type_icao="a339", callsign="TAP88", dep_utc="2026-11-07T23:00",
                        members=[{"name": "A"}, {"name": "B", "callsign": "TAP99", "network": "vatsim"}])
    assert f["source"] == "MANUAL" and f["type_icao"] == "A339" and 7800 < f["km"] < 8100 and f["est_block_min"] > 9 * 60
    assert [x["callsign"] for x in f["members"]] == ["TAP88", "TAP99"] and f["members"][1]["network"] == "VATSIM"
    with pytest.raises(FGVError):
        externos.create(conn, adep="SBGR", ades="LPPT", dep_utc="2026-11-07T23:00", members=[{"name": "A", "network": "XPTO"}])
    with pytest.raises(FGVError):
        externos.fetch_live(conn)                                                            # testes rodam sem internet
