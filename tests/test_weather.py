"""Meteorologia: o módulo do AeroScale buscando num servidor de teste (sem internet de verdade)."""
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lineops import rules

METAR = "SBBR 012000Z 09012KT 9999 FEW030 26/14 Q1016"
METAR_RUIM = "SBFL 012000Z 18022G35KT 1200 +TSRA BKN005 OVC010 19/18 Q1009"
TAF = "SBBR 011700Z 0118/0224 09010KT 9999 FEW030 TX28/0118Z TN17/0209Z BECMG 0202/0204 VRB03KT"


class Stub(BaseHTTPRequestHandler):
    def do_GET(self):
        agora = int(datetime.now(timezone.utc).timestamp())
        if self.path.startswith("/metar"):
            body = [{"icaoId": "SBBR", "rawOb": METAR, "obsTime": agora, "name": "Brasilia", "fltCat": "VFR"},
                    {"icaoId": "SBFL", "rawOb": METAR_RUIM, "obsTime": agora, "name": "Florianopolis", "fltCat": "LIFR"}]
        else:
            body = [{"icaoId": "SBBR", "rawTAF": TAF, "issueTime": agora}]
        out = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture
def awc(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("AEROSCALE_AWC_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    yield
    srv.shutdown()


def test_metar_is_fetched_decoded_and_cached(conn, awc):
    w = rules.weather(conn, ["SBBR", "SBFL"])
    por = {m["icao"]: m for m in w["metars"]}
    assert por["SBBR"]["raw"] == METAR and por["SBBR"]["category"] in ("VFR", "MVFR", "IFR", "LIFR")
    assert por["SBBR"]["freshness"]["label"] and por["SBBR"]["available"]
    assert por["SBFL"]["alerts"], "tempestade e teto baixo deveriam gerar alerta"
    assert w["tafs"][0]["raw"].startswith("SBBR") and w["tafs"][0]["periods"]
    assert conn.execute("SELECT COUNT(*) FROM weather_cache WHERE kind='metar'").fetchone()[0] == 2
    de_novo = rules.weather(conn, ["SBBR"])                   # dentro do TTL: sai do cache
    assert de_novo["metars"][0]["raw"] == METAR


def test_without_network_it_says_so_instead_of_guessing(conn, monkeypatch):
    monkeypatch.setenv("AEROSCALE_AWC_URL", "http://127.0.0.1:9")      # porta fechada
    monkeypatch.setenv("AEROSCALE_NOAA_URL", "http://127.0.0.1:9")
    w = rules.weather(conn, ["SBBR"])
    m = w["metars"][0]
    assert not m.get("raw") and m.get("error") and m["available"] is False
    assert not m.get("category")


def test_weather_endpoint(cl, awc):
    from tests.test_http import setup_user
    setup_user(cl)
    r = cl.get("/api/weather?icao=SBBR,SBFL").json()
    assert {m["icao"] for m in r["metars"]} == {"SBBR", "SBFL"}
    assert "aviationweather.gov" in r["source"]
    assert cl.get("/api/weather?icao=XX").json()["metars"] == []
