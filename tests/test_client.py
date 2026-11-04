import ctypes
import json
import socket
import struct
import threading
import time
import urllib.error
from datetime import datetime, timezone

import httpx
import pytest
import uvicorn

from lineops.app import create_app
from lineops_acars import sims
from lineops_acars.client import Api, Runner
from lineops_acars.phases import iso
from tests import synth
from tests.test_importer import L, make_db

T0 = datetime(2026, 11, 2, 1, 0, 0, tzinfo=timezone.utc)
H = {"X-Requested-With": "lineops"}


@pytest.fixture
def server(tmp_path):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = uvicorn.Server(uvicorn.Config(create_app(tmp_path / "d"), host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True); th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    web = httpx.Client(base_url=base, headers=H)
    web.post("/api/setup", json={"username": "piloto", "password": "senha-forte-1"})
    db = tmp_path / "as.db"
    make_db(db, legs=[L("a", "GLO1748", dep="2026-11-02T01:00:00Z", arr="2026-11-02T03:00:00Z")])
    web.post("/api/import/aeroscale", content=db.read_bytes())
    ac = web.post("/api/aircraft", json={"registration": "PR-TST", "airline": "GLO", "type_icao": "B738"}).json()
    leg = web.get("/api/roster").json()[0]
    f = web.post(f"/api/roster/{leg['id']}/book", json={"aircraft_id": ac["id"]}).json()
    tok = web.post("/api/tokens", json={"name": "t"}).json()["token"]
    yield {"web": web, "base": base, "token": tok, "flight": f["id"]}
    srv.should_exit = True
    th.join(5)


def write_replay(path, samples):
    path.write_text("\n".join(json.dumps({**s, "t": iso(s["t"])}) for s in samples))


def test_client_full_flight_via_replay(server, tmp_path):
    rp = tmp_path / "t.jsonl"; write_replay(rp, synth.flight(T0))
    log = []
    r = Runner(Api(server["base"], server["token"]), sims.Replay(rp), server["flight"], sim_label="REPLAY", state_dir=tmp_path / "st", log=log.append)
    r.start()
    res = r.run()
    assert res and res["pirep_id"]
    p = server["web"].get(f"/api/pireps/{res['pirep_id']}").json()
    assert p["state"] == "PENDING" and p["data"]["block_min"] > 100 and len(p["track"]) > 10
    assert p["data"]["landing_vs_fpm"] == -180
    assert [m["kind"] for m in p["messages"] if m["kind"] in ("OUT", "OFF", "ON", "IN")] == ["OUT", "OFF", "ON", "IN"]
    assert not list((tmp_path / "st").glob("session-*.json"))          # estado temporário removido ao concluir
    sess = server["web"].get("/api/flights/%d" % server["flight"]).json()
    assert sess["state"] == "PIREP_RECEBIDO"


class FlakyApi(Api):
    def __init__(self, *a, fail_reports=3, **k):
        super().__init__(*a, **k); self.fails = fail_reports

    def call(self, method, path, body=None):
        if path.endswith("/report") and self.fails > 0:
            self.fails -= 1
            raise urllib.error.URLError("rede fora")
        return super().call(method, path, body)


def test_network_outage_loses_nothing(server, tmp_path):
    rp = tmp_path / "t.jsonl"; write_replay(rp, synth.flight(T0))
    r = Runner(FlakyApi(server["base"], server["token"], fail_reports=5), sims.Replay(rp), server["flight"], sim_label="REPLAY", state_dir=tmp_path / "st", log=lambda *_: None)
    r.start()
    res = r.run()
    p = server["web"].get(f"/api/pireps/{res['pirep_id']}").json()
    assert len(p["messages"]) >= 4 and p["data"]["block_min"] > 100
    assert len(p["track"]) >= 30


def test_client_restart_resumes_state(server, tmp_path):
    samples = synth.flight(T0)
    cut = next(i for i, s in enumerate(samples) if s["alt_ft"] > 30000) + 5          # no meio do cruzeiro
    api = Api(server["base"], server["token"])
    r1 = Runner(api, sims.Replay(tmp_path / "x"), server["flight"], sim_label="REPLAY", state_dir=tmp_path / "st", log=lambda *_: None)
    r1.start()
    for s in samples[:cut]:
        r1.step(s)
    r1.flush(force=True)
    assert "OFF" in r1.pm.oooi and "IN" not in r1.pm.oooi
    r2 = Runner(api, sims.Replay(tmp_path / "x"), server["flight"], sim_label="REPLAY", state_dir=tmp_path / "st", log=lambda *_: None)   # "reiniciou"
    r2.start()
    assert r2.pm.phase == r1.pm.phase and r2.pm.oooi.keys() == r1.pm.oooi.keys()
    done = False
    for s in samples[cut:]:
        done = r2.step(s)
        if done:
            break
    assert done
    res = r2.finish()
    p = server["web"].get(f"/api/pireps/{res['pirep_id']}").json()
    assert p["data"]["out_utc"] and p["data"]["in_utc"] and p["data"]["block_min"] > 100


def test_server_rejects_blocked_aircraft_for_client(server, tmp_path):
    ac = server["web"].get("/api/aircraft").json()[0]
    server["web"].post(f"/api/aircraft/{ac['id']}/discrepancies", json={"title": "Fogo no motor 1", "restriction": "BLOQUEIA"})
    from lineops_acars.client import ApiError
    r = Runner(Api(server["base"], server["token"]), sims.Replay(tmp_path / "x"), server["flight"], sim_label="REPLAY", state_dir=tmp_path / "st", log=lambda *_: None)
    with pytest.raises(ApiError) as e:
        r.start()
    assert e.value.status == 409


# ---------------------------------------------------------------- conectores
def test_xplane_udp_rref_roundtrip():
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); srv.bind(("127.0.0.1", 0)); port = srv.getsockname()[1]; srv.settimeout(2)
    xp = sims.XPlaneUDP("127.0.0.1", port)
    xp.open()
    reqs = {}
    for _ in range(len(sims.XP_REFS)):
        data, addr = srv.recvfrom(2048)
        assert data[:5] == b"RREF\x00"
        freq, idx, name = struct.unpack("<ii400s", data[5:])
        reqs[idx] = name.rstrip(b"\x00").decode()
    assert reqs[0] == "sim/flightmodel/position/latitude" and len(reqs) == len(sims.XP_REFS)
    vals = {"lat": -23.0, "lon": -46.0, "alt_ft": 1000.0 / sims.FT_PER_M, "gs_kt": 100 / sims.KT_PER_MS, "on_ground": 0, "eng1": 1, "eng2": 1,
            "fuel_kg": 5000, "gw_kg": 60000, "g": 1.1, "parking_brake": 0, "vs_fpm": -700, "agl_ft": 300 / sims.FT_PER_M, "ias_kt": 140, "hdg": 90}
    pkt = b"RREF\x00" + b"".join(struct.pack("<if", i, vals.get(k, 0.0)) for i, (k, _r, _f) in enumerate(sims.XP_REFS))
    srv.sendto(pkt, addr)
    tel = None
    for _ in range(50):
        tel = xp.read()
        if tel:
            break
        time.sleep(0.02)
    assert tel and tel["lat"] == pytest.approx(-23.0) and tel["alt_ft"] == pytest.approx(1000, rel=1e-3) and tel["gs_kt"] == pytest.approx(100, rel=1e-3)
    assert tel["engines"] == 2 and tel["on_ground"] is False and tel["gw_kg"] == 60000 and tel["fuel_kg"] == 5000
    xp.close(); srv.close()


class FakeSimConnect:
    """Imita o SimConnect.dll: entrega um SIMCONNECT_RECV_SIMOBJECT_DATA com os doubles a partir do offset 40."""

    def __init__(self, values):
        n = len(sims.SC_VARS)
        self.buf = (ctypes.c_char * (sims.DATA_OFFSET + 8 * n))()
        struct.pack_into("<III", self.buf, 0, len(self.buf), 1, sims.RECV_SIMOBJECT_DATA)
        struct.pack_into("<" + "d" * n, self.buf, sims.DATA_OFFSET, *[values[k] for k, _n, _u in sims.SC_VARS])
        self.sent = False
        self.opened = self.defs = 0

    def SimConnect_Open(self, ph, *a):
        self.opened += 1
        return 0

    def SimConnect_AddToDataDefinition(self, *a):
        self.defs += 1
        return 0

    def SimConnect_RequestDataOnSimObject(self, *a):
        return 0

    def SimConnect_GetNextDispatch(self, h, ref, cb):
        if self.sent:
            return 1
        self.sent = True
        ptr = ref._obj
        ctypes.memmove(ctypes.addressof(ptr), ctypes.byref(ctypes.c_void_p(ctypes.addressof(self.buf))), ctypes.sizeof(ctypes.c_void_p))
        return 0

    def SimConnect_Close(self, h):
        return 0


def test_simconnect_parsing_with_fake_dll():
    v = {"lat": -15.0, "lon": -47.0, "alt_ft": 35000, "agl_ft": 34000, "gs_kt": 450, "ias_kt": 280, "vs_fpm": 0, "hdg": 180, "on_ground": 0,
         "parking_brake": 0, "eng1": 1, "eng2": 1, "eng3": 0, "eng4": 0, "fuel_lb": 10000, "gw_lb": 150000, "g": 1.0}
    fake = FakeSimConnect(v)
    c = sims.SimConnectConn("msfs", dll=fake)
    c.open()
    assert fake.defs == len(sims.SC_VARS)
    tel = c.read()
    assert tel["lat"] == -15.0 and tel["engines"] == 2 and tel["on_ground"] is False
    assert tel["fuel_kg"] == pytest.approx(10000 * sims.KG_PER_LB) and tel["gw_kg"] == pytest.approx(150000 * sims.KG_PER_LB)


def test_simconnect_refuses_politely_off_windows():
    import os
    if os.name == "nt":
        pytest.skip("só vale fora do Windows")
    with pytest.raises(ConnectionError, match="Windows"):
        sims.SimConnectConn("p3d").open()


# ---------------------------------------------------------------- resistência de rede
def test_api_retries_transient_network_errors():
    import urllib.error
    import urllib.request
    chamadas = {"n": 0}
    dormiu = []

    class Resp:
        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def flaky(req, timeout=None):
        chamadas["n"] += 1
        if chamadas["n"] < 3:
            raise urllib.error.URLError("rede caiu")
        return Resp()

    orig = urllib.request.urlopen
    urllib.request.urlopen = flaky
    try:
        api = Api("http://x", "t", sleep=dormiu.append)
        assert api.call("POST", "/api/x", {}) == {"ok": True}
        assert chamadas["n"] == 3 and dormiu == [1, 2]
    finally:
        urllib.request.urlopen = orig


def test_api_does_not_retry_when_server_refuses():
    import urllib.error
    import urllib.request
    chamadas = {"n": 0}

    def recusa(req, timeout=None):
        chamadas["n"] += 1
        raise urllib.error.HTTPError("http://x", 409, "Conflict", {}, None)

    orig = urllib.request.urlopen
    urllib.request.urlopen = recusa
    try:
        from lineops_acars.client import ApiError
        with pytest.raises(ApiError):
            Api("http://x", "t", sleep=lambda s: None).call("POST", "/api/x", {})
        assert chamadas["n"] == 1
    finally:
        urllib.request.urlopen = orig


def test_heartbeat_keeps_session_alive(server, tmp_path):
    r = Runner(Api(server["base"], server["token"]), sims.Replay(tmp_path / "x"), server["flight"],
               sim_label="REPLAY", state_dir=tmp_path / "st", log=lambda *_: None)
    s = r.start()
    antes = server["web"].get("/api/acars/live").json()[0]["last_seen_at"]
    time.sleep(1.1)
    r.heartbeat()
    vivo = server["web"].get("/api/acars/live").json()[0]
    assert vivo["last_seen_at"] >= antes and vivo["stale"] is False and vivo["silent_s"] is not None
