from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from lineops.app import create_app
from lineops_acars.phases import PhaseMachine, iso
from tests import synth
from tests.test_importer import L, make_db
from tests.test_ofp import FIX

H = {"X-Requested-With": "lineops"}
T0 = datetime(2026, 11, 2, 1, 0, 0, tzinfo=timezone.utc)


def ac_of(cl, reg, type_icao="B738"):
    """Cadastra a aeronave; se ela já veio na frota de referência (lista de frota), usa a que existe."""
    r = cl.post("/api/aircraft", headers=H, json={"registration": reg, "airline": "GLO", "type_icao": type_icao})
    if r.status_code == 200:
        return r.json()
    return next(a for a in cl.get("/api/aircraft?all=true").json() if a["registration"] == reg)


def setup_user(cl):
    r = cl.post("/api/setup", json={"username": "piloto", "password": "senha-forte-1"})
    assert r.status_code == 200
    return r


def test_auth_required_and_setup_once(cl):
    assert cl.get("/api/health").json()["needs_setup"] is True
    assert cl.get("/api/aircraft").status_code == 401
    setup_user(cl)
    assert cl.get("/api/health").json()["needs_setup"] is False
    assert cl.post("/api/setup", json={"username": "outro", "password": "senha-forte-2"}).status_code == 409
    assert cl.get("/api/me").json()["username"] == "piloto"
    # CSRF: POST via cookie sem cabeçalho é bloqueado
    assert cl.post("/api/aircraft", json={"registration": "PR-AAA", "airline": "GLO", "type_icao": "B738"}).status_code == 403
    assert cl.post("/api/aircraft", json={"registration": "PR-AAA", "airline": "GLO", "type_icao": "B738"}, headers=H).status_code == 200


def test_login_throttle_and_logout(cl):
    setup_user(cl)
    cl.cookies.clear()
    for _ in range(5):
        assert cl.post("/api/login", json={"username": "piloto", "password": "errada-errada"}).status_code == 401
    assert cl.post("/api/login", json={"username": "piloto", "password": "senha-forte-1"}).status_code == 429
    assert cl.get("/api/me").status_code == 401


def test_import_book_fly_approve_over_http(cl, tmp_path):
    setup_user(cl)
    db = tmp_path / "as.db"
    make_db(db, legs=[L("a", "GLO1748", dep="2026-11-02T01:00:00Z", arr="2026-11-02T03:00:00Z", flight_id=7)])
    raw = db.read_bytes()
    r = cl.post("/api/import/aeroscale?dry_run=true", content=raw, headers=H)
    assert r.status_code == 200 and r.json()["legs_new"] == 1 and r.json()["dry_run"]
    assert cl.get("/api/roster").json() == []
    assert cl.post("/api/import/aeroscale", content=raw, headers=H).json()["legs_new"] == 1
    assert cl.post("/api/import/aeroscale", content=b"lixo", headers=H).status_code == 400
    leg = cl.get("/api/roster").json()[0]
    assert leg["data_origin"] == "REAL" and leg["can_book"]
    ac = cl.post("/api/aircraft", headers=H, json={"registration": "PR-TST", "airline": "GLO", "type_icao": "B738"}).json()
    f = cl.post(f"/api/roster/{leg['id']}/book", headers=H, json={"aircraft_id": ac["id"]}).json()
    assert f["state"] == "RESERVADO"

    tok = cl.post("/api/tokens", headers=H, json={"name": "PC do simulador"}).json()["token"]
    acars = TestClient(cl.app)                                   # cliente sem cookie, só Bearer
    A = {"Authorization": f"Bearer {tok}"}
    assert acars.get("/api/acars/flights", headers=A).json()[0]["id"] == f["id"]
    assert acars.post("/api/aircraft", headers=A, json={"registration": "PR-X", "airline": "G", "type_icao": "B738"}).status_code == 403   # token ACARS não administra
    s = acars.post("/api/acars/sessions", headers=A, json={"flight_id": f["id"], "sim": "TESTE"}).json()
    pm, pts, msgs, seq = PhaseMachine(), [], [], 0
    for i, x in enumerate(synth.flight(T0)):
        for e in pm.feed(x):
            seq += 1
            msgs.append({"seq": seq, "t": iso(e["t"]), "kind": e["kind"], "text": e["text"]})
        if i % 3 == 0:
            pts.append({"t": iso(x["t"]), "lat": x["lat"], "lon": x["lon"], "alt_ft": x["alt_ft"], "gs_kt": x["gs_kt"], "vs_fpm": x["vs_fpm"], "phase": pm.phase})
    r = acars.post(f"/api/acars/sessions/{s['id']}/report", headers=A, json={"points": pts[:20], "messages": msgs, "snapshot": {"phase": pm.phase}})
    assert r.status_code == 200 and r.json()["new_points"] == 20
    acars.post(f"/api/acars/sessions/{s['id']}/report", headers=A, json={"points": pts[20:]})
    assert cl.get("/api/acars/live").json()[0]["registration"] == "PR-TST"
    done = acars.post(f"/api/acars/sessions/{s['id']}/complete", headers=A, json=pm.summary()).json()
    assert cl.get("/api/acars/live").json() == []
    p = cl.get(f"/api/pireps/{done['pirep_id']}").json()
    assert p["state"] == "PENDING" and len(p["track"]) == len(pts) and p["messages"]
    assert acars.post(f"/api/pireps/{p['id']}/approve", headers=A, json={}).status_code == 403   # só você aprova
    ok = cl.post(f"/api/pireps/{p['id']}/approve", headers=H, json={})
    assert ok.status_code == 200 and ok.json()["state"] == "APPROVED"
    lb = cl.get("/api/logbook").json()
    assert lb["totals"]["flights"] == 1 and lb["entries"][0]["registration"] == "PR-TST"
    a = cl.get(f"/api/aircraft/{ac['id']}").json()
    assert a["totals"]["flown_cycles"] == 1 and a["location"]["icao"] == "SBFL"
    dash = cl.get("/api/dashboard").json()
    assert dash["totals"]["flights"] == 1 and "GOL" in dash["disclaimer"]


def test_validation_errors_are_clear(cl):
    setup_user(cl)
    r = cl.post("/api/aircraft", headers=H, json={"registration": "!!", "airline": "GLO", "type_icao": "B738"})
    assert r.status_code == 400 and r.json()["error"] == "matricula_invalida"
    r = cl.post("/api/flights", headers=H, json={"adep": "SBBR"})
    assert r.status_code == 422 and r.json()["error"] == "requisicao_invalida"
    assert cl.get("/api/flights/999").status_code == 404


def test_backup_roundtrip(cl, tmp_path):
    setup_user(cl)
    cl.post("/api/aircraft", headers=H, json={"registration": "PR-BKP", "airline": "GLO", "type_icao": "B738"})
    b = cl.post("/api/backup", headers=H).json()
    assert cl.get(f"/api/backups/{b['name']}").status_code == 200
    assert cl.get("/api/backups/..%2Flineops.db").status_code == 404
    from lineops import backup
    import sqlite3
    restored = tmp_path / "restaurado"
    assert "carreiras/GLO.db" in backup.restore(tmp_path / "data" / "backups" / b["name"], restored)
    assert sqlite3.connect(restored / "carreiras" / "GLO.db").execute("SELECT registration FROM aircraft").fetchone()[0] == "PR-BKP"


def test_journey_endpoints(cl, tmp_path):
    setup_user(cl)
    db = tmp_path / "as2.db"
    make_db(db, legs=[L("a", "GLO1", dep="2026-11-02T10:00:00Z", arr="2026-11-02T11:00:00Z", flight_id=7),
                      L("b", "GLO2", dep="2026-11-02T12:00:00Z", arr="2026-11-02T13:30:00Z"),
                      L("c", "GLO3", dep="2026-11-03T15:00:00Z", arr="2026-11-03T16:00:00Z")])
    cl.post("/api/import/aeroscale", content=db.read_bytes(), headers=H)
    d = cl.get("/api/day/2026-11-02").json()
    assert d["kind"] == "VOO" and len(d["legs"]) == 2
    assert d["label"].startswith("segunda-feira, 2 de novembro")
    assert d["legs"][0]["from"]["city"] == "Brasília" and d["legs"][0]["dep_local"]["hhmm"] == "07:00"
    assert d["turns"][0]["minutes"] == 60 and d["turns"][0]["at"]["icao"] == "SBFL"
    assert d["ends_at"]["icao"] == "SBFL" and d["next_duty"]["date"] == "2026-11-03"
    assert d["duty"]["block_min"] == 150 and d["duty"]["report_utc"] is None      # fixture sem report_before: não inventa
    w = cl.get("/api/week/2026-11-02").json()
    assert len(w["days"]) == 7 and w["days"][0]["flights"] == 2 and w["days"][1]["flights"] == 1
    assert w["days"][2]["kind"] == "LIVRE"
    assert cl.get("/api/roster/bounds").json() == {"first": "2026-11-02", "last": "2026-11-03"}
    assert cl.get("/api/day/ontem").status_code == 400


def test_journey_shows_booked_registration(cl, tmp_path):
    setup_user(cl)
    db = tmp_path / "as3.db"
    make_db(db, legs=[L("a", "GLO1", dep="2026-11-02T10:00:00Z", arr="2026-11-02T11:00:00Z")])
    cl.post("/api/import/aeroscale", content=db.read_bytes(), headers=H)
    ac = cl.post("/api/aircraft", headers=H, json={"registration": "PR-REG", "airline": "GLO", "type_icao": "B738"}).json()
    leg = cl.get("/api/roster").json()[0]
    assert leg["registration"] is None                                             # a escala do AeroScale não traz matrícula
    f = cl.post(f"/api/roster/{leg['id']}/book", headers=H, json={"aircraft_id": ac["id"]}).json()
    day = cl.get("/api/day/2026-11-02").json()
    assert day["legs"][0]["registration"] == "PR-REG" and day["legs"][0]["flight_state"] == "RESERVADO"
    c = cl.get(f"/api/flight-center/{f['id']}").json()
    assert c["aircraft"]["registration"] == "PR-REG"
    keys = {s["key"]: s for s in c["stages"]}
    assert keys["aeronave"]["state"] == "PRONTO" and keys["acars"]["state"] == "PENDENTE"
    assert keys["loadsheet"]["state"] == "PENDENTE"            # sem OFP não dá para fechar o loadsheet
    assert keys["meteo"]["state"] in ("SEM_DADO", "PRONTO", "ATENCAO")      # sem internet no teste, fica SEM_DADO
    assert "simbrief.com" in c["simbrief_url"] and "orig=SBBR" in c["simbrief_url"] and "dest=SBFL" in c["simbrief_url"]
    assert c["simbrief"]["params"]["route"] == "KUKOL UZ5 EDREX"          # rota vem do RPL, pelo módulo do AeroScale


def test_flight_center_blocks_on_grounded_aircraft(cl, tmp_path):
    setup_user(cl)
    db = tmp_path / "as4.db"
    make_db(db, legs=[L("a", "GLO1", dep="2026-11-02T10:00:00Z", arr="2026-11-02T11:00:00Z")])
    cl.post("/api/import/aeroscale", content=db.read_bytes(), headers=H)
    ac = cl.post("/api/aircraft", headers=H, json={"registration": "PR-MEL", "airline": "GLO", "type_icao": "B738"}).json()
    leg = cl.get("/api/roster").json()[0]
    f = cl.post(f"/api/roster/{leg['id']}/book", headers=H, json={"aircraft_id": ac["id"]}).json()
    cl.post(f"/api/aircraft/{ac['id']}/discrepancies", headers=H, json={"title": "Trem de pouso", "restriction": "BLOQUEIA"})
    c = cl.get(f"/api/flight-center/{f['id']}").json()
    keys = {s["key"]: s for s in c["stages"]}
    assert keys["mel"]["state"] == "BLOQUEADO" and keys["aeronave"]["state"] == "BLOQUEADO"


def test_crew_report_uses_aeroscale_rules(cl, tmp_path):
    setup_user(cl)
    db = tmp_path / "crew.db"
    make_db(db, legs=[L("a", "GLO1", dep="2026-11-02T10:00:00Z", arr="2026-11-02T11:00:00Z"),
                      L("b", "GLO2", dep="2026-11-02T12:00:00Z", arr="2026-11-02T13:30:00Z"),
                      L("c", "GLO3", dep="2026-11-04T15:00:00Z", arr="2026-11-04T16:00:00Z")])
    cl.post("/api/import/aeroscale", content=db.read_bytes(), headers=H)
    r = cl.get("/api/crew").json()
    assert r["available"] and r["summary"]["duties"] == 2
    d = r["duties"][0]
    assert d["legs"] == 2 and d["fdp_limit_h"] and "RBAC 117" in (d["limit_source"] or "") + r["summary"]["mode_label"]
    assert r["source"].startswith("crew.analyze")
    assert r["profile"]["off_code"] == "FOLGA"


def test_rules_endpoint_reports_source(cl, tmp_path):
    setup_user(cl)
    r0 = cl.get("/api/rules").json()
    assert r0["version"] == "2.2.4" and r0["has_source_db"] is False
    db = tmp_path / "src.db"
    make_db(db, legs=[L("a", "GLO1")])
    cl.post("/api/import/aeroscale", content=db.read_bytes(), headers=H)
    r = cl.get("/api/rules").json()
    assert r["has_source_db"] and "catalog" in r["modules"] and r["profile"]["label"] == "GOL"


def test_foreign_airport_timezone_comes_from_aeroscale(cl, tmp_path):
    """SAEZ não está na base brasileira: o fuso vem do world_tz do AeroScale, não de um palpite."""
    setup_user(cl)
    a = cl.get("/api/airports/SAEZ").json()
    assert a["tz"] == "America/Argentina/Buenos_Aires" and a["city"].startswith("Buenos Aires")
    db = tmp_path / "intl.db"
    make_db(db, legs=[dict(uid="x", fn="GLO7632", adep="SBFL", ades="SAEZ", dep="2026-11-02T02:50:00Z", arr="2026-11-02T05:00:00Z"),
                      dict(uid="y", fn="GLO7655", adep="SAEZ", ades="SBGL", dep="2026-11-02T20:26:00Z", arr="2026-11-02T23:32:00Z")])
    cl.post("/api/import/aeroscale", content=db.read_bytes(), headers=H)
    d = cl.get("/api/day/2026-11-01").json()                     # 02:50Z em Florianópolis = 23:50 do dia 01
    assert [l["flight_number"] for l in d["legs"]] == ["GLO7632"]
    d2 = cl.get("/api/day/2026-11-02").json()
    assert d2["legs"][0]["dep_local"]["hhmm"] == "17:26" and d2["tz_unknown"] == []


def test_payroll_optional_over_http(cl, tmp_path):
    setup_user(cl)
    assert cl.get("/api/me").json()["features"]["payroll"] is False
    assert cl.get("/api/payroll/2026-11").status_code == 409                    # desligado
    assert cl.put("/api/payroll/enabled", headers=H, json={"enabled": True}).json()["enabled"] is True
    assert cl.get("/api/me").json()["features"]["payroll"] is True
    assert cl.get("/api/payroll/2026-11").json()["needs_contract"] is True
    cfg = cl.get("/api/payroll/config").json()
    assert cfg["enabled"] and len(cfg["presets"]) == 8
    r = cl.post("/api/payroll/contracts", headers=H, json={"valid_from": "2026-11", "data": {"role": "Copiloto", "category": "jato_nb"}})
    assert r.status_code == 200 and r.json()["data"]["hour_rate"] == 56.28
    d = cl.get("/api/payroll/2026-11?basis=realizado").json()
    assert d["needs_contract"] is False and d["totals"]["gross"] >= 6069.34
    assert cl.get("/api/payroll/2026-11/pdf").headers["content-type"] == "application/pdf"
    assert cl.get("/api/payroll/year/2026").status_code == 200
    assert cl.get("/api/payroll/2026-13").status_code == 400


def test_manual_flight_and_feed_over_http(cl, tmp_path):
    setup_user(cl)
    db = tmp_path / "as.db"
    make_db(db, legs=[L("a", "GLO1748", dep="2026-10-02T01:00:00Z", arr="2026-10-02T03:00:00Z")])
    cl.post("/api/import/aeroscale", content=db.read_bytes(), headers=H)
    leg = cl.get("/api/roster").json()[0]
    ac = ac_of(cl, "PR-GUZ")                                                       # já vem da frota de referência
    r = cl.post(f"/api/roster/{leg['id']}/manual", headers=H, json={"aircraft_id": ac["id"], "out_utc": "2026-10-02T01:05", "in_utc": "2026-10-02T03:02",
                                                                    "network": "IVAO", "landing_vs_fpm": -220, "landing_g": 1.25})
    assert r.status_code == 200 and r.json()["state"] == "APROVADO"
    e = cl.get("/api/logbook").json()["entries"][0]
    assert e["source"] == "MANUAL" and e["block_min"] == 117
    assert cl.post(f"/api/roster/{leg['id']}/manual", headers=H, json={"bogus": 1}).status_code == 400
    assert any("voo declarado" in m["text"] for m in cl.get("/api/ops-feed").json())
    fc = cl.get(f"/api/flight-center/{r.json()['id']}").json()
    assert fc["manual_entry"]["id"] == e["id"] and fc["timeline"]
    assert cl.post(f"/api/logbook/{e['id']}/reverse", headers=H, json={"reason": "teste"}).status_code == 200
    assert cl.put("/api/settings", headers=H, json={"ivao_vid": "abc"}).status_code == 400
    assert cl.put("/api/settings", headers=H, json={"ivao_vid": "123456"}).json()["ivao_vid"] == "123456"


def test_network_friends_over_http(cl):
    setup_user(cl)
    assert cl.post("/api/network/friends", headers=H, json={"vid": "222", "nickname": "Zé"}).json()["nickname"] == "Zé"
    assert cl.get("/api/network/pilots/222").json()["friend"]["nickname"] == "Zé"
    assert cl.delete("/api/network/friends/222", headers=H).json()["ok"] is True
    assert cl.delete("/api/network/friends/222", headers=H).status_code == 404
    assert cl.post("/api/network/friends", headers=H, json={"vid": "x"}).status_code == 400


def test_team_messages_and_dispatch_over_http(cl, tmp_path):
    setup_user(cl)
    db = tmp_path / "as.db"
    make_db(db, legs=[L("a", "GLO1748", dep="2026-11-20T14:00:00Z", arr="2026-11-20T16:00:00Z")])
    cl.post("/api/import/aeroscale", content=db.read_bytes(), headers=H)
    d = cl.get("/api/messages").json()
    subs = [m["subject"] for m in d["messages"]]
    assert "Bem-vindo à equipe" in subs and any(s.startswith("Escala publicada") for s in subs) and d["counts"]["pending_ack"] >= 1
    pub = next(m for m in d["messages"] if m["subject"].startswith("Escala publicada"))
    assert cl.post(f"/api/messages/{pub['id']}/ack", headers=H).json()["acked_at"]
    t = cl.get("/api/team").json()
    assert any(s["sector"] == "despacho" for s in t["staff"])
    assert cl.put("/api/team", headers=H, json={"staff": [{"sector": "escala", "shift": "todos", "name": "Fulana"}]}).status_code == 200
    leg = cl.get("/api/roster").json()[0]
    ac = ac_of(cl, "PR-GUZ")                                                       # já vem da frota de referência
    f = cl.post(f"/api/roster/{leg['id']}/book", headers=H, json={"aircraft_id": ac["id"]}).json()
    assert cl.post(f"/api/flights/{f['id']}/dispatch/sign", headers=H).status_code == 409          # ainda sem OFP
    cl.post(f"/api/flights/{f['id']}/ofp/file", headers=H, content=(FIX / "ofp_simbrief.json").read_bytes())
    fc = cl.get(f"/api/flight-center/{f['id']}").json()
    assert fc["release"]["status"] == "LIBERADO" and any("despacho" in m["subject"] for m in fc["messages"])
    assert cl.post(f"/api/flights/{f['id']}/dispatch/sign", headers=H).json()["pic_signed_at"]


def test_stays_chime_and_ignore_schedule_over_http(cl, tmp_path):
    setup_user(cl)
    db = tmp_path / "as.db"
    make_db(db, legs=[L("a", "GLO1748", dep="2099-11-20T14:00:00Z", arr="2099-11-20T16:00:00Z")])
    cl.post("/api/import/aeroscale", content=db.read_bytes(), headers=H)
    r = cl.put("/api/airport-info/sbfl", headers=H, json={"hotel": "Hotel X", "transport": "van 20 min"})
    assert r.status_code == 200 and r.json()["icao"] == "SBFL"
    lst = cl.get("/api/airport-info").json()
    assert lst["items"][0]["hotel"] == "Hotel X"
    assert cl.put("/api/airport-info/SBFL", headers=H, json={"preco": 1}).status_code == 400
    assert cl.put("/api/settings", headers=H, json={"acars_chime": "off"}).json()["acars_chime"] == "off"
    assert cl.put("/api/settings", headers=H, json={"acars_chime": "talvez"}).status_code == 400
    leg = cl.get("/api/roster").json()[0]
    body = {"out_utc": "2099-11-20T14:00", "in_utc": "2099-11-20T16:00", "landing_vs_fpm": 150, "landing_g": 1.2}
    assert cl.post(f"/api/roster/{leg['id']}/manual", headers=H, json=body).status_code == 400          # futuro, sem a opção
    r = cl.post(f"/api/roster/{leg['id']}/manual", headers=H, json={**body, "ignore_schedule": "escala"})
    assert r.status_code == 200 and r.json()["state"] == "APROVADO"
    e = cl.get("/api/logbook").json()["entries"][0]
    assert e["time_basis"] == "ESCALA"


def test_stay_endpoints_over_http(cl, tmp_path):
    setup_user(cl)
    db = tmp_path / "as.db"
    make_db(db, legs=[{**L("a", "GLO1748", dep="2026-11-20T14:00:00Z", arr="2026-11-20T16:00:00Z"), "adep": "SBBR", "ades": "SBFL"},
                      {**L("c", "GLO1750", dep="2026-11-21T11:00:00Z", arr="2026-11-21T13:00:00Z"), "adep": "SBFL", "ades": "SBBR"}])
    cl.post("/api/import/aeroscale", content=db.read_bytes(), headers=H)
    assert cl.get("/api/stay/SBFL/hotels").status_code == 503                  # testes sem internet: erro claro, não trava
    r = cl.post("/api/stay/SBFL/hotel", headers=H, json={"name": "Hotel Aeroporto", "lat": -27.668, "lon": -48.55, "ref": "way/2"})
    assert r.status_code == 200 and r.json()["hotel"] == "Hotel Aeroporto" and r.json()["warning"]
    assert cl.put("/api/settings", headers=H, json={"stay_transport": "app", "stay_buffer_min": "20"}).json()["stay_transport"] == "app"
    assert cl.put("/api/settings", headers=H, json={"stay_wakeup_min": "5"}).status_code == 400
    leg = cl.get("/api/roster").json()[0]
    ac = ac_of(cl, "PR-GUZ")                                                       # já vem da frota de referência
    f = cl.post(f"/api/roster/{leg['id']}/book", headers=H, json={"aircraft_id": ac["id"]}).json()
    d = cl.get(f"/api/stay/flight/{f['id']}").json()
    assert d["kind"] == "pernoite" and d["timeline"]["transport"] == "app" and d["timeline"]["buffer_min"] == 20 and d["tz"]


def test_v1_endpoints_over_http(cl, tmp_path):
    setup_user(cl)
    db = tmp_path / "as.db"
    make_db(db, legs=[{**L("a", "GLO1748", dep="2026-11-20T14:00:00Z", arr="2026-11-20T16:00:00Z"), "adep": "SBBR", "ades": "SBFL"},
                      {**L("c", "GLO1750", dep="2026-11-21T11:00:00Z", arr="2026-11-21T13:00:00Z"), "adep": "SBFL", "ades": "SBBR"}])
    cl.post("/api/import/aeroscale", content=db.read_bytes(), headers=H)
    c = cl.get("/api/carreira").json()
    assert c["milestones"] == [] and c["check"]["due"] and "93" in c["delay_codes"]
    n = cl.get("/api/notify").json()
    assert n["channels"] == [] and n["level"] == "importantes"
    assert cl.post("/api/notify/test", headers=H).status_code == 409
    n = cl.put("/api/notify", headers=H, json={"ntfy_new_topic": True, "quiet": "23:00-06:00"}).json()
    assert n["ntfy"]["topic"].startswith("lineops-") and n["channels"] == ["ntfy"]
    assert "ntfy" in cl.post("/api/notify/test", headers=H).json()                    # sem internet nos testes: volta o erro, não quebra
    assert cl.put("/api/notify", headers=H, json={"hack": 1}).status_code == 400
    leg = cl.get("/api/roster").json()[0]
    ac = ac_of(cl, "PR-GUZ")                                                       # já vem da frota de referência
    f = cl.post(f"/api/roster/{leg['id']}/book", headers=H, json={"aircraft_id": ac["id"]}).json()
    fc = cl.get(f"/api/flight-center/{f['id']}").json()
    assert len(fc["crew"]["cabin"]) == 4 and fc["delay"] is None and fc["line_check"] is None
    assert cl.post(f"/api/flights/{f['id']}/delay", headers=H, json={"code": "93"}).status_code == 409
    assert cl.get("/manifest.json").json()["short_name"] == "LineOps"
    assert cl.get("/icons/icon-192.png").status_code == 200


def test_externos_over_http(cl):
    setup_user(cl)
    c = cl.get("/api/externos/catalogo").json()
    assert c["items"] == [] and "777" in c["families"]
    assert cl.get("/api/externos/sortear").status_code == 404
    assert cl.post("/api/externos/buscar", headers=H).status_code == 503                       # testes sem internet
    f = cl.post("/api/externos", headers=H, json={"adep": "SBGR", "ades": "LPPT", "type_icao": "A339", "dep_utc": "2026-11-07T23:00",
                                                  "members": [{"name": "Triple"}, {"name": "João", "network": "VATSIM"}]}).json()
    assert f["source"] == "MANUAL" and len(f["members"]) == 2 and "SBGR" in f["invite"]
    assert cl.get("/api/externos").json()["upcoming"][0]["id"] == f["id"]
    r = cl.put(f"/api/externos/membro/{f['members'][1]['id']}/resultado", headers=H, json={"block": "9:41", "landing_fpm": 95, "landing_g": 1.05}).json()
    assert r["podium"][0]["name"] == "João"
    assert cl.post(f"/api/externos/{f['id']}/cancelar", headers=H).json()["ok"]
    assert cl.post("/api/externos", headers=H, json={"x": 1}).status_code == 400


def test_careers_and_engine_over_http(cl):
    setup_user(cl)
    c = cl.get("/api/careers").json()
    assert c["current"] == "GLO" and {x["code"] for x in c["items"]} >= {"GLO", "TAM", "AZU", "WORLD"}
    assert cl.post("/api/careers/WORLD/select", headers=H).status_code == 409   # o modo Mundo ainda não está pronto
    assert cl.post("/api/careers/GLO/select", headers=H).json()["name"] == "GOL"
    me = cl.get("/api/me").json()
    assert me["career"]["code"] == "GLO" and "GOL Linhas" in me["disclaimer"]
    m = cl.get("/api/motor").json()
    assert m["options"]["types"] and m["status"]["engine"].startswith("AeroScale")
    # sem reserva: nessa semana a reserva sorteada (24 a 27/10) e as folgas deixam um dia só, e ele não fecha nas regras
    j = cl.post("/api/motor/gerar", headers=H, json={"kind": "semanal", "start_date": "2026-10-22", "base": "SBGR", "icao_type": "B738", "seed": 5,
                                                    "reserve_blocks": 0}).json()
    import time
    for _ in range(200):
        r = cl.get(f"/api/motor/tarefas/{j['id']}").json()
        if r["state"] != "running":
            break
        time.sleep(0.1)
    assert r["state"] == "finished" and r["result"]["legs"] > 0
    assert len(cl.get("/api/roster").json()) == r["result"]["legs"]
    assert cl.put("/api/careers/GLO/logo?name=x.png", headers=H, content=b"\x89PNG").json()["ok"]
    assert cl.get("/api/careers/GLO/logo").status_code == 200


def test_screens_are_never_served_stale(cl):
    """Depois de atualizar, o navegador revalida a tela e os scripts (nada de mostrar o app antigo do cache)."""
    from lineops import __version__
    r = cl.get("/")
    assert r.status_code == 200 and r.headers.get("cache-control") == "no-cache"
    assert f"js/app.js?v={__version__}" in r.text and f"css/app.css?v={__version__}" in r.text
    assert cl.get("/js/app.js").headers.get("cache-control") == "no-cache"
    assert cl.get("/api/health").json()["app"] == "LineOps"


def test_swap_endpoints(cl, tmp_path):
    setup_user(cl)
    p = tmp_path / "as.db"
    make_db(p, legs=[L("a", "GLO1", dep="2099-11-02T10:00:00Z", arr="2099-11-02T11:00:00Z")])
    assert cl.post("/api/import/aeroscale", content=p.read_bytes(), headers=H).status_code == 200
    assert cl.get("/api/troca").json()["items"] == []
    assert cl.get("/api/troca/opcoes/99999").status_code == 404
    leg = cl.get("/api/roster").json()[0]
    o = cl.get(f"/api/troca/opcoes/{leg['id']}").json()
    assert o["leg"]["id"] == leg["id"] and "regra de jogo" in o["disclaimer"]
    ex = [x for x in o["liberacao"]["options"] if x["mode"] == "extra"]
    assert ex and ex[0]["ok"] and ex[0]["legs"][0]["activity"] == "EXTRA"          # sem ciclo: sai e vai de extra
    dd = cl.get(f"/api/troca/destinos/{leg['id']}").json()
    assert dd["items"] or dd["why"]
    assert cl.post("/api/troca", json={"leg_id": leg["id"], "kind": "TROCA", "ref": "x", "destino": "SB/FZ"}, headers=H).status_code == 422
    assert cl.post("/api/troca", json={"leg_id": leg["id"], "kind": "XYZ"}, headers=H).status_code in (400, 422)
    r = cl.post("/api/troca", json={"leg_id": leg["id"], "kind": "LIBERACAO", "ref": ex[0]["ref"]}, headers=H)
    assert r.status_code == 200 and r.json()["state"] == "PENDENTE"
    assert cl.post("/api/troca", json={"leg_id": leg["id"], "kind": "LIBERACAO", "ref": ex[0]["ref"]}, headers=H).status_code == 409
    d = cl.get(f"/api/day/{leg['local_date'] or leg['dep_utc'][:10]}").json()
    assert d["swaps"][str(leg["id"])]["state"] == "PENDENTE"
    assert cl.get("/api/troca").json()["items"][0]["kind"] == "LIBERACAO"


def test_backup_panel_upload_and_scheduled_restore(cl, tmp_path):
    setup_user(cl)
    b = cl.post("/api/backup", headers=H).json()
    info = cl.get("/api/backups-info").json()
    assert info["items"][0]["name"] == b["name"] and info["count"] >= 2 and info["pending_restore"] is None
    raw = cl.get("/api/backups/" + b["name"]).content
    up = cl.post("/api/backups/enviar", content=raw, headers=H)
    assert up.status_code == 200 and up.json()["name"].endswith("-enviado.zip")
    assert cl.post("/api/backups/enviar", content=b"lixo", headers=H).status_code == 400
    assert cl.post("/api/backups/../x/restaurar", headers=H).status_code in (404, 405)   # o caminho é normalizado: nada fora da pasta
    r = cl.post(f"/api/backups/{b['name']}/restaurar", headers=H)
    assert r.status_code == 200 and cl.get("/api/backups-info").json()["pending_restore"] == b["name"]
    assert cl.delete("/api/backups/restaurar", headers=H).json()["scheduled"] is False


def test_position_endpoints(cl, tmp_path):
    setup_user(cl)
    v = cl.get("/api/posicao").json()
    assert "position" in v and "remove" in v and v["can_undo"] is False
    assert cl.post("/api/posicao/desfazer", headers=H).status_code == 409


def test_views_endpoints(cl, tmp_path):
    setup_user(cl)
    assert cl.get("/api/month/2026-11").json()["month"] == "2026-11"
    assert cl.get("/api/month/2026-13").status_code == 400
    assert cl.get("/api/mapa?fonte=diario").json()["source"] == "diario"
    assert cl.get("/api/limites/2026-11").status_code == 200
    assert cl.get("/api/passaporte").json()["flights"] == 0
    r = cl.get("/api/diario.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and "date;airline" in r.text


def test_generator_defaults_and_wipe_all_over_http(cl):
    setup_user(cl)
    s = cl.get("/api/motor").json()["options"]["settings"]
    assert "weekday_max" in s and "crew_mode" in s
    r = cl.post("/api/motor/padrao", headers=H, json={"weekday_max": {"6": 0, "1": 2}, "crew_mode": "lei"}).json()
    assert r["weekday_max"] == {"6": 0, "1": 2} and r["crew_mode"] == "lei"
    assert cl.get("/api/motor").json()["options"]["settings"]["weekday_max"] == {"6": 0, "1": 2}
    assert cl.post("/api/motor/padrao", headers=H, json={"base": "SBGR"}).status_code == 400
    w = cl.get("/api/escala/apagar-tudo").json()
    assert {"legs", "reserves", "logbook"} <= set(w)
    assert cl.post("/api/escala/apagar-tudo", headers=H, json={}).status_code == 400           # sem digitar APAGAR, nada
    r = cl.post("/api/escala/apagar-tudo", headers=H, json={"confirmar": "APAGAR"})
    assert r.status_code == 200 and r.json()["removed"] == 0


def test_roster_text_over_http(cl):
    setup_user(cl)
    r = cl.get("/api/escala/texto?inicio=2026-11-01&fim=2026-11-30").json()
    assert r["empty"] and r["text"] == ""
    assert cl.get("/api/escala/texto").status_code == 400
    f = cl.get("/api/escala/texto.txt?inicio=2026-11-01&fim=2026-11-30&estilo=classico&horarios=1")
    assert f.status_code == 200 and f.headers["content-type"].startswith("text/plain") and "attachment" in f.headers["content-disposition"]


def test_overnight_discord_is_only_for_cmdt079(cl):
    setup_user(cl)                                                                   # "piloto": sem o recurso
    assert cl.get("/api/me").json()["features"]["pernoite_discord"] is False
    assert cl.get("/api/pernoite/discord").status_code == 403
    assert cl.post("/api/pernoite/1/marcar", headers=H).status_code == 403


def test_overnight_discord_for_cmdt079(cl):
    r = cl.post("/api/setup", json={"username": "cmdt079", "password": "senha-forte-1"})
    assert r.status_code == 200
    assert cl.get("/api/me").json()["features"]["pernoite_discord"] is True
    first = cl.get("/api/pernoite/discord").json()
    assert first["configured"] and first["default"] and "1567661679636906056" in first["masked"]   # a padrão já vem
    assert cl.put("/api/pernoite/discord", headers=H, json={"url": "https://example.com/x"}).status_code == 400
    ok = cl.put("/api/pernoite/discord", headers=H, json={"url": "https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz_ABCDEF"}).json()
    assert ok["configured"] and "••••" in ok["masked"] and not ok["default"]
    assert cl.put("/api/pernoite/discord", headers=H, json={"url": ""}).json()["default"]           # apagar volta à padrão
    assert cl.post("/api/pernoite/999999/marcar", headers=H).status_code == 404
    assert cl.get("/api/aircraft").status_code == 200 and any(a["registration"] == "PR-GXA" for a in cl.get("/api/aircraft").json())


def test_pilot_code_and_avatar_over_http(cl):
    setup_user(cl)
    me = cl.get("/api/me").json()
    assert me["pilot_code"] == "GOL001" and me["avatar"] is None
    assert cl.get("/api/me/avatar").status_code == 404
    assert cl.post("/api/careers/TAM/select", headers=H).status_code == 200
    assert cl.get("/api/me").json()["pilot_code"] == "TAM001"                         # não fica GOL001 na LATAM
    assert cl.get("/api/today").json()["pilot"]["pilot_code"] == "TAM001"


def test_avatar_for_cmdt079_over_http(cl):
    assert cl.post("/api/setup", json={"username": "cmdt079", "password": "senha-forte-1"}).status_code == 200
    me = cl.get("/api/me").json()
    assert me["avatar"] == "/api/me/avatar"
    r = cl.get("/api/me/avatar")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"


def test_forgot_password_over_http(cl, tmp_path):
    setup_user(cl)
    assert cl.post("/api/senha/codigo").status_code == 403                          # sem o cabeçalho da tela: CSRF
    r = cl.post("/api/senha/codigo", headers=H).json()
    from pathlib import Path
    code = next(ln.strip() for ln in Path(r["file"]).read_text(encoding="utf-8").splitlines() if ln.strip().isdigit())
    assert cl.post("/api/senha/redefinir", headers=H, json={"username": "piloto", "code": "x", "password": "nova-senha-123"}).status_code == 400
    assert cl.post("/api/senha/redefinir", headers=H, json={"username": "piloto", "code": code, "password": "nova-senha-123"}).json()["ok"]
    assert cl.get("/api/me").status_code == 401                                      # a sessão antiga foi encerrada
    assert cl.post("/api/login", json={"username": "piloto", "password": "nova-senha-123"}).status_code == 200
