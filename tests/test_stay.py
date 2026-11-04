"""0.13: pernoite imersivo — hotel do OpenStreetMap, rota do OSRM, carro, check-in, despertar e saída na hora certa."""
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from lineops import enroute, equipe, flights, ivao, stay
from lineops.errors import FGVError
from tests.test_enroute import SBBR_SBFL, fake_wx, fly, leg, setup

OVERPASS = {"elements": [
    {"type": "node", "id": 1, "lat": -27.60, "lon": -48.52, "tags": {"tourism": "hotel", "name": "Hotel Longe", "addr:city": "Florianópolis"}},
    {"type": "way", "id": 2, "center": {"lat": -27.668, "lon": -48.55}, "tags": {"tourism": "hotel", "name": "Hotel Aeroporto",
                                                                              "addr:street": "Rua A", "addr:housenumber": "10", "stars": "3"}},
    {"type": "node", "id": 3, "lat": -27.67, "lon": -48.54, "tags": {"tourism": "hotel"}},          # sem nome: fica de fora
]}
OSRM = {"code": "Ok", "routes": [{"duration": 1320.0, "distance": 14200.0}]}


def net(url, data=None):
    if "overpass" in url:
        assert b"tourism" in data
        return json.dumps(OVERPASS).encode()
    if "routing.openstreetmap.de" in url:
        raise OSError("fora do ar")                                      # cai no servidor reserva
    return json.dumps(OSRM).encode()


def test_search_route_and_choose(conn, tmp_path):
    setup(conn, tmp_path, [leg("a", "GLO1748", "SBBR", "SBFL", "2026-11-20T14:00:00Z", "2026-11-20T16:00:00Z")])
    r = stay.search_hotels(conn, "sbfl", opener=net)
    assert [h["name"] for h in r["hotels"]] == ["Hotel Aeroporto", "Hotel Longe"] and "OpenStreetMap" in r["attribution"]
    h = r["hotels"][0]
    assert h["ref"] == "way/2" and h["address"] == "Rua A 10" and h["km"] < 1
    info = stay.choose_hotel(conn, "SBFL", h, opener=net)
    assert info["route_min"] == 22 and info["route_km"] == 14.2 and "router.project-osrm.org" in info["route_src"] and not info["warning"]
    assert stay.drive_of(conn, "SBFL")["label"].startswith("22 min de carro (14,2 km, OSRM)")
    enroute.save_airport_info(conn, "SBFL", hotel="Hotel Aeroporto", transport="combinado no balcão")      # só o transporte: hotel continua
    assert enroute.airport_info(conn, "SBFL")["route_min"] == 22
    enroute.save_airport_info(conn, "SBFL", hotel="Outro Hotel")                                           # trocou à mão: rota zera
    i2 = enroute.airport_info(conn, "SBFL")
    assert i2["route_min"] is None and i2["hotel_lat"] is None and stay.drive_of(conn, "SBFL")["estimated"]
    bad = lambda url, data=None: json.dumps({"code": "NoRoute", "message": "sem rota"}).encode()
    w = stay.choose_hotel(conn, "SBFL", h, opener=bad)
    assert w["warning"] and w["route_min"] is None and "estimado pela distância" in stay.drive_of(conn, "SBFL")["label"]
    with pytest.raises(FGVError):
        stay.search_hotels(conn, "SBFL")                                 # testes rodam sem internet


def test_overnight_timeline_delivered_on_the_clock(conn, tmp_path):
    aid, ids = setup(conn, tmp_path, [leg("a", "GLO1748", "SBBR", "SBFL", "2026-11-20T14:00:00Z", "2026-11-20T16:00:00Z"),
                                      leg("c", "GLO1750", "SBFL", "SBBR", "2026-11-21T11:00:00Z", "2026-11-21T13:00:00Z")])
    conn.execute("INSERT INTO setting(key, value) VALUES ('ivao_vid', '123456')")
    stay.choose_hotel(conn, "SBFL", {"name": "Hotel Aeroporto", "lat": -27.668, "lon": -48.55, "ref": "way/2", "address": "Rua A 10"}, opener=net)
    f = flights.book_leg(conn, ids["a"], aircraft_id=aid)
    ivao.start(conn, f["id"])
    fly(conn, f["id"], SBBR_SBFL[:4])
    plan = next(m for m in equipe.messages(conn, flight_id=f["id"]) if m["key"].startswith("er_plan"))
    assert "HOTEL: Hotel Aeroporto · Rua A 10 (escolhido por você no OpenStreetMap)" in plan["body"]
    assert "TRANSPORTE: van da tripulação · 22 min de carro" in plan["body"] and "AMANHÃ: despertar" in plan["body"]
    ev = {e["key"].split(":")[0]: e for e in stay.events_for(conn, f["id"])}
    assert set(ev) == {"stay_car", "stay_checkin", "stay_wake", "stay_pickup"}
    fly(conn, f["id"], SBBR_SBFL[4:])                                    # pouso e calço às 15:58Z
    ev = {e["key"].split(":")[0]: e for e in stay.events_for(conn, f["id"])}
    assert ev["stay_car"]["due_utc"] == "2026-11-20T16:28:00Z"           # calço + 30 min (art. 35 §4)
    assert ev["stay_checkin"]["due_utc"] == "2026-11-20T17:00:00Z"       # + 22 min de carro + 10 de check-in
    p = enroute.plan_after(conn, enroute._flight(conn, f["id"]))
    rep = datetime.strptime(p["next_report_utc"], "%Y-%m-%dT%H:%M:%SZ")
    assert ev["stay_pickup"]["due_utc"] == (rep - timedelta(minutes=22 + 15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert ev["stay_wake"]["due_utc"] == (rep - timedelta(minutes=22 + 15 + 60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    arr = next(m for m in equipe.messages(conn, flight_id=f["id"]) if m["key"].startswith("er_arr"))
    assert "SUA VAN: " in arr["body"] and "no desembarque às 13:28 local" in arr["body"]
    keys = lambda: {m["key"].split(":")[0]: m for m in equipe.messages(conn, flight_id=f["id"], limit=200)}
    equipe.run(conn, now="2026-11-20T16:20:00Z", wx_fetch=fake_wx)
    assert "stay_car" not in keys()                                      # ainda não chegou a hora
    equipe.run(conn, now="2026-11-20T16:29:00Z", wx_fetch=fake_wx)
    car = keys()["stay_car"]
    assert car["ts"] == "2026-11-20T16:28:00Z" and re.search(r"placa [A-Z]{3}\d[A-Z]\d{2}", car["body"]) and "fictícios" in car["body"]
    assert car["sender"] == "Central de Transporte"
    equipe.run(conn, now="2026-11-20T17:01:00Z", wx_fetch=fake_wx)
    equipe.run(conn, now=ev["stay_wake"]["due_utc"], wx_fetch=fake_wx)
    k = keys()
    assert "stay_checkin" in k and k["stay_checkin"]["sender"] == "Amanda Ribeiro" and "DESPERTAR PROGRAMADO" in k["stay_checkin"]["body"]
    assert k["stay_wake"]["channel"] == "alarme" and k["stay_wake"]["level"] == "acao" and "Bom dia, comandante" in k["stay_wake"]["body"]
    assert "stay_pickup" not in k
    equipe.run(conn, now=ev["stay_pickup"]["due_utc"], wx_fetch=fake_wx)
    assert "Van na portaria" in keys()["stay_pickup"]["subject"]


def test_schedule_change_cancels_and_stale_expires(conn, tmp_path):
    aid, ids = setup(conn, tmp_path, [leg("a", "GLO1748", "SBBR", "SBFL", "2026-11-20T14:00:00Z", "2026-11-20T16:00:00Z"),
                                      leg("c", "GLO1750", "SBFL", "SBBR", "2026-11-21T11:00:00Z", "2026-11-21T13:00:00Z")])
    f = flights.book_leg(conn, ids["a"], aircraft_id=aid)
    conn.execute("UPDATE flight SET state='APROVADO' WHERE id=?", (f["id"],))
    fr = enroute._flight(conn, f["id"])
    stay.schedule(conn, fr, enroute.plan_after(conn, fr), "2026-11-20T15:00:00Z")
    conn.execute("UPDATE roster_leg SET dep_utc='2026-11-21T12:00:00Z' WHERE id=?", (ids["c"],))      # a escala mudou
    stay.dispatch(conn, "2026-11-21T12:00:00Z")
    st = {e["key"].split(":")[0]: e["status"] for e in stay.events_for(conn, f["id"])}
    assert st["stay_wake"] == "CANCELADO" and st["stay_pickup"] == "CANCELADO"
    assert st["stay_car"] == "VENCIDO" and st["stay_checkin"] == "VENCIDO"     # passou da hora há mais de 3 h: não entrega atrasado
    assert not any(m["key"].startswith("stay_") for m in equipe.messages(conn, limit=200))


def test_app_mode_and_settings(conn, tmp_path):
    aid, ids = setup(conn, tmp_path, [leg("a", "GLO1748", "SBBR", "SBFL", "2026-11-20T14:00:00Z", "2026-11-20T16:00:00Z"),
                                      leg("c", "GLO1750", "SBFL", "SBBR", "2026-11-21T11:00:00Z", "2026-11-21T13:00:00Z")])
    conn.execute("INSERT INTO setting(key, value) VALUES ('stay_transport','app'), ('stay_buffer_min','20'), ('stay_wakeup_min','45')")
    assert stay.settings(conn) == {"stay_transport": "app", "stay_buffer_min": 20, "stay_wakeup_min": 45}
    f = flights.book_leg(conn, ids["a"], aircraft_id=aid)
    fr = enroute._flight(conn, f["id"])
    tl = stay.schedule(conn, fr, enroute.plan_after(conn, fr), "2026-11-20T15:00:00Z")
    assert tl["drive"]["estimated"] and tl["drive"]["min"] == 30
    ev = {e["key"].split(":")[0]: e for e in stay.events_for(conn, f["id"])}
    assert ev["stay_car"]["subject"].startswith("Seu carro chegou")
    assert stay.ride(7, "app") == stay.ride(7, "app") and stay.ride(7, "van")["vehicle"] in stay.VANS
    for k, v in (("stay_transport", "uber"), ("stay_buffer_min", "500"), ("stay_wakeup_min", "x")):
        with pytest.raises(FGVError):
            stay.validate_setting(k, v)
