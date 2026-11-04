import json
from pathlib import Path

import pytest

from lineops import ofp
from lineops.errors import FGVError

FIX = Path(__file__).parent / "fixtures"
H = {"X-Requested-With": "lineops"}


def raw_ofp(name="ofp_simbrief.json"):
    return json.loads((FIX / name).read_text())


def test_parse_reads_the_plan():
    d, w = ofp.parse(raw_ofp())
    assert d["adep"] == "SBBR" and d["ades"] == "SBFL" and d["altn"] == "SBNF"
    assert d["est_zfw"] == 56775 and d["est_tow"] == 64165 and d["max_tow"] == 79016
    assert d["fuel_ramp"] == 7570 and d["fuel_trip"] == 4820 and d["fuel_taxi"] == 180
    assert d["eet_min"] == 114 and d["taxi_out_min"] == 10            # o SimBrief manda segundos
    assert d["est_out"] == "2026-11-01T23:55:00Z" and d["generated_at"].startswith("2026-11-01")
    assert d["registration"] == "PR-TST" and d["type_icao"] == "B738" and d["navlog_points"] == 3
    assert d["units_stored"] == "KG" and d["alternates"] == ["SBNF"]
    assert not [x for x in w if x["level"] == "warn"]


def test_pounds_are_converted_and_declared():
    d, w = ofp.parse(raw_ofp("ofp_simbrief_lbs.json"))
    assert d["units_stored"] == "KG" and d["converted_from"] == "LBS"
    assert d["est_zfw"] == pytest.approx(125000 * 0.45359237, abs=1)
    assert any(x["code"] == "conversao" for x in w)


def test_missing_fields_are_listed_not_invented():
    r = raw_ofp()
    del r["weights"]["max_ldw"]
    r["fuel"]["contingency"] = ""
    d, w = ofp.parse(r)
    assert d["max_ldw"] is None and d["fuel_contingency"] is None
    assert "max_ldw" in d["missing"] and "fuel_contingency" in d["missing"]
    assert any(x["code"] == "campos_ausentes" for x in w)


def test_garbage_is_refused():
    with pytest.raises(FGVError):
        ofp.parse({})
    with pytest.raises(FGVError):
        ofp.parse({"general": {"flight_number": "1"}})       # sem origem/destino não é OFP


def test_attach_warns_when_plan_does_not_match_the_flight(conn):
    from lineops import fleet, flights
    a = fleet.add_aircraft(conn, registration="PR-OUT", airline="GLO", type_icao="B738")
    f = flights.create_unplanned(conn, airline="GLO", flight_number="GLO999", adep="SBGR", ades="SBSV",
                                 type_icao="B738", aircraft_id=a["id"])
    o = ofp.attach(conn, f["id"], raw_ofp(), source="arquivo")
    codes = {w["code"] for w in o["warnings"]}
    assert "trecho_diferente" in codes and "numero_diferente" in codes and "matricula_diferente" in codes
    assert o["data"]["adep"] == "SBBR"                        # guarda o que veio, sem corrigir
    assert ofp.latest(conn, f["id"])["id"] == o["id"]


def test_raw_is_kept_without_the_html(conn):
    from lineops import fleet, flights
    a = fleet.add_aircraft(conn, registration="PR-RAW", airline="GLO", type_icao="B738")
    f = flights.create_unplanned(conn, airline="GLO", flight_number="GLO1748", adep="SBBR", ades="SBFL",
                                 type_icao="B738", aircraft_id=a["id"])
    o = ofp.attach(conn, f["id"], raw_ofp(), source="arquivo")
    stored = json.loads(conn.execute("SELECT raw FROM ofp WHERE id=?", (o["id"],)).fetchone()[0])
    assert "text" not in stored and stored["navlog"]["fix"][0]["ident"] == "KUKOL"
    assert not o["warnings"] or all(w["level"] != "warn" for w in o["warnings"])


def test_comparison_only_uses_what_exists():
    d, _ = ofp.parse(raw_ofp())
    linhas = ofp.compare(d, {"fuel_out_kg": 7600, "fuel_used_kg": 5010, "air_min": 123})
    rot = {x["label"]: x for x in linhas}
    assert rot["Consumo"]["diff"] == 190 and rot["Consumo"]["diff_text"].startswith("+")
    assert "Peso no pouso" not in rot                          # o ACARS não mediu, então não compara
    assert rot["Tempo de voo"]["planned_text"] == "1:54"


def test_http_flow_with_file_upload(cl, tmp_path):
    from tests.test_http import setup_user
    setup_user(cl)
    ac = cl.post("/api/aircraft", headers=H, json={"registration": "PR-OFP", "airline": "GLO", "type_icao": "B738"}).json()
    f = cl.post("/api/flights", headers=H, json={"flight_number": "GLO1748", "adep": "SBBR", "ades": "SBFL",
                                                 "aircraft_id": ac["id"], "dep_utc": "2026-11-01T23:55:00Z"}).json()
    assert cl.get(f"/api/flights/{f['id']}/ofp").status_code == 404
    r = cl.post(f"/api/flights/{f['id']}/ofp/file", headers=H, content=(FIX / "ofp_simbrief.json").read_bytes())
    assert r.status_code == 200 and r.json()["data"]["fuel_ramp"] == 7570
    assert cl.post(f"/api/flights/{f['id']}/ofp/file", headers=H, content=b"nao e json").status_code == 400
    c = cl.get(f"/api/flight-center/{f['id']}").json()
    etapa = {s["key"]: s for s in c["stages"]}["despacho"]
    # desde a 2.x o despacho é conjunto: com o OFP, o DOV confere e libera, e só fica PRONTO com a sua assinatura
    assert etapa["state"] in ("ATENCAO", "PRONTO") and "SBNF" in etapa["detail"] and "OFP" in etapa["detail"]
    assert c["ofp"]["data"]["est_tow"] == 64165
    g = cl.get(f"/api/flights/{f['id']}/ofp").json()
    assert len(g["history"]) == 1
    assert cl.delete(f"/api/ofp/{g['id']}", headers=H).status_code == 200
    assert cl.get(f"/api/flights/{f['id']}/ofp").status_code == 404


def test_settings_remember_pilot_id(cl):
    from tests.test_http import setup_user
    setup_user(cl)
    st = cl.get("/api/settings").json()
    assert st["simbrief_userid"] == "" and st["simbrief_username"] == ""
    assert cl.put("/api/settings", headers=H, json={"simbrief_userid": "123456"}).json()["simbrief_userid"] == "123456"
    assert cl.put("/api/settings", headers=H, json={"qualquer": "x"}).status_code == 400


def test_fetch_explains_network_failure(monkeypatch):
    import urllib.error
    import urllib.request

    def boom(*a, **k):
        raise urllib.error.URLError("sem rede")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(FGVError) as e:
        ofp.fetch(userid="123456")
    assert e.value.code == "simbrief_sem_rede"
    with pytest.raises(FGVError) as e2:
        ofp.fetch()
    assert e2.value.code == "simbrief_sem_id"


# ---------------------------------------------------------------- LOADSHEET
def _voo_com_ofp(conn, limits=None):
    from lineops import fleet, flights, ofp as o
    a = fleet.add_aircraft(conn, registration="PR-LDS", airline="GLO", type_icao="B738", limits=limits or {})
    f = flights.create_unplanned(conn, airline="GLO", flight_number="GLO1748", adep="SBBR", ades="SBFL",
                                 type_icao="B738", aircraft_id=a["id"], dep_utc="2026-11-01T23:55:00Z")
    o.attach(conn, f["id"], raw_ofp(), source="arquivo")
    return f


def test_loadsheet_matches_the_ofp_and_shows_sources(conn):
    from lineops import loadsheet
    f = _voo_com_ofp(conn, {"mtow_kg": 79016, "mlw_kg": 66361, "mzfw_kg": 62732})
    d = loadsheet.compute(conn, f["id"])
    por = {l["label"]: l for l in d["lines"]}
    assert por["Peso zero combustível (ZFW)"]["value"] == 56775      # igual ao do OFP, somando DOW + payload
    assert por["Peso de decolagem (TOW)"]["value"] == 64165
    assert por["Peso de pouso (LW)"]["value"] == 59345
    assert por["Peso de decolagem (TOW)"]["margin"] == 14851 and "sua frota" in por["Peso de decolagem (TOW)"]["limit_source"]
    assert por["Payload"]["source"] == "calculado" and por["Carga"]["source"] == "OFP do SimBrief"
    assert d["complete"] and not d["warnings"] and not d["cg"]["available"]


def test_loadsheet_flags_overweight_and_needs_justification(conn):
    from lineops import loadsheet
    from lineops.errors import Conflict
    f = _voo_com_ofp(conn, {"mtow_kg": 60000})
    d = loadsheet.compute(conn, f["id"])
    tow = next(l for l in d["lines"] if l["label"].startswith("Peso de decolagem"))
    assert tow["over"] and tow["margin"] < 0
    with pytest.raises(Conflict):
        loadsheet.issue(conn, f["id"])
    ls = loadsheet.issue(conn, f["id"], notes="teste de limite")
    assert ls["edition"] == 1 and ls["notes"] == "teste de limite"


def test_loadsheet_overrides_and_editions(conn):
    from lineops import loadsheet
    f = _voo_com_ofp(conn)
    ls1 = loadsheet.issue(conn, f["id"])
    ls2 = loadsheet.issue(conn, f["id"], {"cargo": 3000})
    assert ls2["edition"] == 2 and loadsheet.latest(conn, f["id"])["id"] == ls2["id"]
    carga = next(l for l in ls2["data"]["lines"] if l["label"] == "Carga")
    assert carga["value"] == 3000 and carga["source"] == "informado por você"
    zfw = next(l for l in ls2["data"]["lines"] if l["label"].startswith("Peso zero"))
    assert zfw["value"] == 56775 + 1750                               # 3000 no lugar de 1250
    assert len(loadsheet.history(conn, f["id"])) == 2


def test_loadsheet_without_ofp_says_what_is_missing(conn):
    from lineops import fleet, flights, loadsheet
    a = fleet.add_aircraft(conn, registration="PR-NOP", airline="GLO", type_icao="B738")
    f = flights.create_unplanned(conn, airline="GLO", flight_number="GLO1", adep="SBBR", ades="SBFL",
                                 type_icao="B738", aircraft_id=a["id"])
    d = loadsheet.compute(conn, f["id"])
    assert not d["complete"] and "peso vazio operacional" in d["missing"]
    assert any(w["code"] == "sem_ofp" for w in d["warnings"])


def test_loadsheet_pdf_is_generated(cl, conn, tmp_path):
    from tests.test_http import setup_user
    setup_user(cl)
    ac = cl.post("/api/aircraft", headers=H, json={"registration": "PR-PDF", "airline": "GLO", "type_icao": "B738",
                                                   "limits": {"mtow_kg": 79016}}).json()
    f = cl.post("/api/flights", headers=H, json={"flight_number": "GLO1748", "adep": "SBBR", "ades": "SBFL",
                                                 "aircraft_id": ac["id"], "dep_utc": "2026-11-01T23:55:00Z"}).json()
    cl.post(f"/api/flights/{f['id']}/ofp/file", headers=H, content=(FIX / "ofp_simbrief.json").read_bytes())
    prev = cl.get(f"/api/flights/{f['id']}/loadsheet").json()
    assert prev["complete"] and prev["issued"] is None
    ls = cl.post(f"/api/flights/{f['id']}/loadsheet", headers=H, json={}).json()
    r = cl.get(f"/api/loadsheet/{ls['id']}.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content[:5] == b"%PDF-"
    from pypdf import PdfReader
    import io
    txt = PdfReader(io.BytesIO(r.content)).pages[0].extract_text()
    assert "LOADSHEET" in txt and "SIMULAÇÃO" in txt and "64.165 kg" in txt and "LineOps" in txt
    c = cl.get(f"/api/flight-center/{f['id']}").json()
    assert {s["key"]: s for s in c["stages"]}["loadsheet"]["state"] == "PRONTO"
