"""0.12: mensagens em rota (pernoite, próxima perna, tempo no destino, MCC, cabine) e voo fora do horário da escala."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from lineops import aeroscale_import as ai, enroute, equipe, fleet, flights, ivao, manual
from lineops.errors import FGVError
from tests.test_importer import L, make_db
from tests.test_manual_ivao import T, _whazzup

METAR = "METAR SBFL 201500Z 18012G25KT 6000 -RA BKN008 OVC020 22/20 Q1012"


def leg(uid, fn, adep, ades, dep, arr):
    return {**L(uid, fn, dep=dep, arr=arr), "adep": adep, "ades": ades}


def fake_wx(conn, icaos):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in icaos:
        conn.execute("INSERT OR REPLACE INTO weather_cache(kind, icao, raw, data, obs_time, source, fetched_at) VALUES ('metar',?,?,NULL,NULL,'teste',?)",
                     (i, METAR.replace("SBFL", i), now))
        conn.execute("INSERT OR REPLACE INTO weather_cache(kind, icao, raw, data, obs_time, source, fetched_at) VALUES ('taf',?,?,NULL,NULL,'teste',?)",
                     (i, f"TAF {i} 201100Z 2012/2112 18010KT 9999 SCT020", now))


def setup(conn, tmp_path, legs):
    p = tmp_path / "as.db"
    make_db(p, legs=legs)
    ai.import_backup(conn, p)
    ac = fleet.add_aircraft(conn, registration="PR-GUZ", airline="GLO", type_icao="B738")
    ids = {r["uid"]: r["id"] for r in conn.execute("SELECT id, uid FROM roster_leg")}
    return ac["id"], ids


def fly(conn, fid, samples, wx=fake_wx):
    """Alimenta o whazzup simulado e roda a equipe depois de cada leitura, como o laço do servidor."""
    for ts, lat, lon, alt, gs, ground, state in samples:
        data = json.dumps(_whazzup(T(ts, lat, lon, alt, gs, ground, state))).encode()
        ivao._cache.update(at=0, data=None)
        ivao.tick(conn, opener=lambda url, d=data: d, now=ts + "Z")
        equipe.run(conn, now=ts + "Z", wx_fetch=wx)


def acars_msgs(conn, fid):
    return {m["key"].split(":")[0]: m for m in equipe.messages(conn, flight_id=fid, limit=100) if m["key"].startswith("er_")}


SBBR_SBFL = [("2026-11-20T14:05:00", -15.87, -47.92, 3500, 12, True, "Departing"),
             ("2026-11-20T14:12:00", -15.95, -47.93, 6000, 180, False, "Initial Climb"),
             ("2026-11-20T14:26:00", -17.20, -48.00, 30000, 420, False, "Initial Climb"),
             ("2026-11-20T14:40:00", -18.90, -48.10, 37000, 450, False, "En Route"),
             ("2026-11-20T15:10:00", -23.00, -48.30, 37000, 450, False, "En Route"),
             ("2026-11-20T15:25:00", -24.80, -48.40, 20000, 380, False, "Approach"),
             ("2026-11-20T15:52:00", -27.67, -48.55, 50, 130, True, "Landed"),
             ("2026-11-20T15:58:00", -27.67, -48.55, 50, 0, True, "Landed"),
             ("2026-11-20T15:58:15", -27.67, -48.55, 50, 0, True, "Landed")]


def test_overnight_package_arrives_during_flight(conn, tmp_path):
    aid, ids = setup(conn, tmp_path, [leg("a", "GLO1748", "SBBR", "SBFL", "2026-11-20T14:00:00Z", "2026-11-20T16:00:00Z"),
                                      leg("c", "GLO1750", "SBFL", "SBBR", "2026-11-21T11:00:00Z", "2026-11-21T13:00:00Z")])
    conn.execute("INSERT INTO setting(key, value) VALUES ('ivao_vid', '123456')")
    enroute.save_airport_info(conn, "sbfl", hotel="Hotel da tripulação (exemplo)", transport="Van na saída do desembarque, 20 min")
    f = flights.book_leg(conn, ids["a"], aircraft_id=aid)
    ivao.start(conn, f["id"])
    fly(conn, f["id"], SBBR_SBFL[:3])
    m = acars_msgs(conn, f["id"])
    assert "er_cab" in m and "er_plan" not in m and m["er_cab"]["channel"] == "acars" and m["er_cab"]["sector"] == "cabine"
    fly(conn, f["id"], SBBR_SBFL[3:4])                                  # ~25%: o pacote do pernoite
    plan = acars_msgs(conn, f["id"])["er_plan"]
    assert plan["subject"] == "GLO1748: pernoite em Florianópolis" and plan["channel"] == "acars" and plan["sector"] == "tripulantes"
    b = plan["body"]
    assert "REPOUSO MÍNIMO: 12 h (art. 48" in b and "PRÓXIMA APRESENTAÇÃO" in b and "GLO1750 SBFL→SBBR" in b
    assert "HOTEL: Hotel da tripulação (exemplo)" in b and "Van na saída" in b and "acima do mínimo legal" in b
    assert "FIM DA JORNADA: 13:30 local (30 min após o corte, art. 35 §4)" in b and "SOL EM SBFL" in b and "Jantar 19:00–20:00" in b
    fly(conn, f["id"], SBBR_SBFL[4:5])                                  # ~60%: tempo no destino, lido na hora
    wx = acars_msgs(conn, f["id"])["er_wx"]
    assert METAR in wx["body"] and "IFR" in wx["body"] and "rajada 25" in wx["body"] and wx["level"] == "alerta" and "TAF SBFL" in wx["body"]
    fly(conn, f["id"], SBBR_SBFL[5:6])                                  # ~75%: MCC
    mcc = acars_msgs(conn, f["id"])["er_mcc"]
    assert "nenhum no LineOps" in mcc["body"] and "ITENS ABERTOS: nenhum" in mcc["body"]
    fly(conn, f["id"], SBBR_SBFL[6:])                                   # pouso e calço: boas-vindas
    arr = acars_msgs(conn, f["id"])["er_arr"]
    assert arr["subject"] == "GLO1748: bem-vindo a Florianópolis" and "SEU REPOUSO CONTA A PARTIR DE: 13:28 local" in arr["body"]
    assert arr["channel"] is None
    n = len(equipe.messages(conn, limit=500))
    equipe.run(conn, now="2026-11-20T16:10:00Z", wx_fetch=fake_wx)
    assert len(equipe.messages(conn, limit=500)) == n                    # idempotente


def test_next_leg_same_day(conn, tmp_path):
    aid, ids = setup(conn, tmp_path, [leg("a", "GLO1748", "SBBR", "SBFL", "2026-11-20T14:00:00Z", "2026-11-20T16:00:00Z"),
                                      leg("b", "GLO1749", "SBFL", "SBBR", "2026-11-20T17:00:00Z", "2026-11-20T19:00:00Z")])
    conn.execute("INSERT INTO setting(key, value) VALUES ('ivao_vid', '123456')")
    f = flights.book_leg(conn, ids["a"], aircraft_id=aid)
    flights.book_leg(conn, ids["b"], aircraft_id=aid)
    ivao.start(conn, f["id"])
    fly(conn, f["id"], SBBR_SBFL[:4])
    plan = acars_msgs(conn, f["id"])["er_plan"]
    assert plan["subject"] == "GLO1748: próxima perna GLO1749"
    assert "SOLO: 1h00 programados em SBFL" in plan["body"] and "AERONAVE: segue a PR-GUZ" in plan["body"] and "DOV começa" in plan["body"]


def test_rest_below_minimum_is_flagged(conn, tmp_path):
    aid, ids = setup(conn, tmp_path, [leg("a", "GLO1748", "SBBR", "SBFL", "2026-11-20T14:00:00Z", "2026-11-20T16:00:00Z"),
                                      leg("c", "GLO1750", "SBFL", "SBBR", "2026-11-21T04:30:00Z", "2026-11-21T06:30:00Z")])
    f = flights.book_leg(conn, ids["a"], aircraft_id=aid)
    plan = enroute.plan_after(conn, enroute._flight(conn, f["id"]))
    assert plan["kind"] == "pernoite" and plan["min_rest"] == 720 and plan["rest_min"] < 720
    subj, body, lv = enroute.plan_text(conn, enroute._flight(conn, f["id"]), plan)
    assert lv == "alerta" and "ABAIXO do mínimo de 12 h do art. 48" in body


def test_min_rest_and_sun():
    assert enroute.min_rest(600) == 720 and enroute.min_rest(721) == 960 and enroute.min_rest(900) == 960 and enroute.min_rest(901) == 1440
    ev = enroute.sun_events(-27.67, -48.55, datetime(2026, 11, 20, 16, 0, tzinfo=timezone.utc), 24)
    kinds = [k for k, _ in ev]
    assert kinds == ["poente", "nascente"]
    sunset_local = ev[0][1] - timedelta(hours=3)
    assert (18, 30) <= (sunset_local.hour, sunset_local.minute) <= (19, 15)   # Florianópolis, fim de novembro: ~18h50 (sem horário de verão)


def test_ignore_schedule_manual(conn, tmp_path):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    dep = (now + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    arr = (now + timedelta(days=5, hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    dep2 = (now + timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    arr2 = (now + timedelta(days=6, hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    aid, ids = setup(conn, tmp_path, [leg("a", "GLO1748", "SBBR", "SBFL", dep, arr), leg("c", "GLO1750", "SBFL", "SBBR", dep2, arr2)])
    TD = {"landing_vs_fpm": 180, "landing_g": 1.2}
    equipe.run(conn)                                                         # a equipe já estava trabalhando antes do lançamento
    with pytest.raises(FGVError) as e:                                       # sem a opção: recusado como antes
        manual.log_flight(conn, leg_id=ids["a"], aircraft_id=aid, out_utc=dep[:16], in_utc=arr[:16], **TD)
    assert "Ignorar horário" in e.value.message
    with pytest.raises(FGVError):                                            # "real" continua sem aceitar futuro
        manual.log_flight(conn, leg_id=ids["a"], aircraft_id=aid, out_utc=dep[:16], in_utc=arr[:16], ignore_schedule="real", **TD)
    out = (now - timedelta(hours=2, minutes=10)).strftime("%Y-%m-%dT%H:%M")
    inn = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")
    r = manual.log_flight(conn, leg_id=ids["a"], aircraft_id=aid, out_utc=out, in_utc=inn, ignore_schedule="real", **TD)
    e1 = conn.execute("SELECT * FROM logbook_entry WHERE id=?", (r["logbook_id"],)).fetchone()
    assert e1["time_basis"] == "REAL" and e1["sched_out_utc"] == dep and "voado fora do horário" in e1["remarks"]
    equipe.run(conn)
    plan = next(m for m in equipe.messages(conn, flight_id=r["id"]) if m["key"] == f"er_plan:{r['id']}")
    assert plan["channel"] is None and "pernoite em Florianópolis" in plan["subject"] and "fora do horário da escala" in plan["body"]
    # a outra perna: registrada no horário da escala, mesmo no futuro
    with pytest.raises(FGVError):                                            # longe demais da partida programada
        manual.log_flight(conn, leg_id=ids["c"], out_utc=(now + timedelta(days=9)).strftime("%Y-%m-%dT%H:%M"),
                          in_utc=(now + timedelta(days=9, hours=2)).strftime("%Y-%m-%dT%H:%M"), ignore_schedule="escala", **TD)
    r2 = manual.log_flight(conn, leg_id=ids["c"], aircraft_id=aid, out_utc=dep2[:16], in_utc=arr2[:16], ignore_schedule="escala", **TD)
    e2 = conn.execute("SELECT * FROM logbook_entry WHERE id=?", (r2["logbook_id"],)).fetchone()
    assert e2["time_basis"] == "ESCALA" and e2["out_utc"] == dep2 and "registrado no horário da escala" in e2["remarks"]
    with pytest.raises(FGVError):
        manual.log_flight(conn, leg_id=ids["c"], out_utc=out, in_utc=inn, ignore_schedule="talvez", **TD)


def test_report_reminder_skips_day_already_flown(conn, tmp_path):
    aid, ids = setup(conn, tmp_path, [leg("a", "GLO1748", "SBBR", "SBFL", "2026-11-20T14:00:00Z", "2026-11-20T16:00:00Z")])
    f = flights.book_leg(conn, ids["a"], aircraft_id=aid)
    conn.execute("UPDATE flight SET state='APROVADO' WHERE id=?", (f["id"],))
    equipe.run(conn, now="2026-11-19T20:00:00Z")
    assert not any(m["subject"].startswith("Apresentação 20/11") for m in equipe.messages(conn, limit=200))


def test_airport_info_crud(conn):
    with pytest.raises(FGVError):
        enroute.save_airport_info(conn, "XX", hotel="x")
    enroute.save_airport_info(conn, "sbfl", hotel="  Hotel X ", notes="perto da praia")
    lst = enroute.list_airport_info(conn)
    assert lst["items"][0]["icao"] == "SBFL" and lst["items"][0]["hotel"] == "Hotel X" and lst["items"][0]["source"] == "cadastro manual"
    assert enroute.save_airport_info(conn, "SBFL")["deleted"] and not enroute.list_airport_info(conn)["items"]
