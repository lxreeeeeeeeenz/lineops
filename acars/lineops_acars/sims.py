"""Conectores de simulador: X-Plane 12 (UDP/RREF), MSFS 2024 e Prepar3D v5.x (SimConnect.dll via ctypes), e replay de arquivo.

Todos devolvem o mesmo dicionário de telemetria. read() não bloqueia: devolve a leitura mais nova ou None.
ATENÇÃO: os conectores de simulador reais ainda NÃO foram testados contra um simulador de verdade neste projeto
(só o protocolo/estruturas, com simuladores falsos). O replay e a máquina de fases são testados.
"""
from __future__ import annotations

import ctypes
import glob
import json
import os
import socket
import struct
import time as _time
from datetime import datetime, timezone
from pathlib import Path

KT_PER_MS = 1.943844
FT_PER_M = 3.28084
KG_PER_LB = 0.45359237


def _now():
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ X-Plane 12
XP_REFS = [
    ("lat", "sim/flightmodel/position/latitude", 1),
    ("lon", "sim/flightmodel/position/longitude", 1),
    ("alt_ft", "sim/flightmodel/position/elevation", FT_PER_M),
    ("agl_ft", "sim/flightmodel/position/y_agl", FT_PER_M),
    ("gs_kt", "sim/flightmodel/position/groundspeed", KT_PER_MS),
    ("ias_kt", "sim/flightmodel/position/indicated_airspeed", 1),
    ("vs_fpm", "sim/flightmodel/position/vh_ind_fpm", 1),
    ("hdg", "sim/flightmodel/position/true_psi", 1),
    ("on_ground", "sim/flightmodel/failures/onground_any", 1),
    ("parking_brake", "sim/cockpit2/controls/parking_brake_ratio", 1),
    ("eng1", "sim/flightmodel/engine/ENGN_running[0]", 1),
    ("eng2", "sim/flightmodel/engine/ENGN_running[1]", 1),
    ("eng3", "sim/flightmodel/engine/ENGN_running[2]", 1),
    ("eng4", "sim/flightmodel/engine/ENGN_running[3]", 1),
    ("fuel_kg", "sim/flightmodel/weight/m_fuel_total", 1),
    ("gw_kg", "sim/flightmodel/weight/m_total", 1),
    ("g", "sim/flightmodel2/misc/gforce_normal", 1),
]


def rref_request(index: int, dataref: str, freq: int) -> bytes:
    return b"RREF\x00" + struct.pack("<ii400s", freq, index, dataref.encode("ascii"))


class XPlaneUDP:
    name = "X-Plane 12"

    def __init__(self, host="127.0.0.1", port=49000, freq=10):
        self.host, self.port, self.freq = host, int(port), freq
        self.sock = None
        self.vals: dict[str, float] = {}
        self.last_rx = None
        self._last_req = 0.0

    def open(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.setblocking(False)
        self._request(self.freq)

    def _request(self, freq):
        for i, (_k, ref, _f) in enumerate(XP_REFS):
            self.sock.sendto(rref_request(i, ref, freq), (self.host, self.port))
        self._last_req = _time.time()

    def read(self):
        got = False
        while True:
            try:
                data, _a = self.sock.recvfrom(65535)
            except (BlockingIOError, InterruptedError, OSError):
                break
            if data[:4] != b"RREF" or len(data) < 5:
                continue
            body = data[5:]
            for off in range(0, len(body) - 7, 8):
                idx, val = struct.unpack_from("<if", body, off)
                if 0 <= idx < len(XP_REFS):
                    k, _r, f = XP_REFS[idx]
                    self.vals[k] = val * f
                    got = True
        if got:
            self.last_rx = _now()
        elif _time.time() - self._last_req > 5 and (self.last_rx is None or (_now() - self.last_rx).total_seconds() > 5):
            self._request(self.freq)        # X-Plane abriu depois do cliente: pede de novo
        if not got or "lat" not in self.vals:
            return None
        v = self.vals
        eng = sum(1 for k in ("eng1", "eng2", "eng3", "eng4") if v.get(k, 0) > 0.5)
        return {"t": self.last_rx, "lat": v.get("lat"), "lon": v.get("lon"), "alt_ft": v.get("alt_ft"), "agl_ft": v.get("agl_ft"),
                "gs_kt": v.get("gs_kt"), "ias_kt": v.get("ias_kt"), "vs_fpm": v.get("vs_fpm"), "hdg": v.get("hdg"),
                "on_ground": bool(v.get("on_ground")), "parking_brake": (v.get("parking_brake") or 0) > 0.5, "engines": eng,
                "fuel_kg": v.get("fuel_kg"), "gw_kg": v.get("gw_kg"), "g": v.get("g")}

    def close(self):
        if self.sock:
            try:
                self._request(0)
            except OSError:
                pass
            self.sock.close()
            self.sock = None


# ------------------------------------------------------------------ SimConnect
SC_VARS = [
    ("lat", "PLANE LATITUDE", "degrees"), ("lon", "PLANE LONGITUDE", "degrees"), ("alt_ft", "PLANE ALTITUDE", "feet"),
    ("agl_ft", "PLANE ALT ABOVE GROUND", "feet"), ("gs_kt", "GROUND VELOCITY", "knots"), ("ias_kt", "AIRSPEED INDICATED", "knots"),
    ("vs_fpm", "VERTICAL SPEED", "feet per minute"), ("hdg", "PLANE HEADING DEGREES TRUE", "degrees"),
    ("on_ground", "SIM ON GROUND", "bool"), ("parking_brake", "BRAKE PARKING INDICATOR", "bool"),
    ("eng1", "GENERAL ENG COMBUSTION:1", "bool"), ("eng2", "GENERAL ENG COMBUSTION:2", "bool"),
    ("eng3", "GENERAL ENG COMBUSTION:3", "bool"), ("eng4", "GENERAL ENG COMBUSTION:4", "bool"),
    ("fuel_lb", "FUEL TOTAL QUANTITY WEIGHT", "pounds"), ("gw_lb", "TOTAL WEIGHT", "pounds"), ("g", "G FORCE", "GForce"),
]
SIMCONNECT_DATATYPE_FLOAT64, SIMCONNECT_OBJECT_ID_USER, SIMCONNECT_PERIOD_SIM_FRAME, SIMCONNECT_UNUSED = 4, 0, 3, 0xFFFFFFFF
RECV_EXCEPTION, RECV_OPEN, RECV_QUIT, RECV_SIMOBJECT_DATA = 1, 2, 3, 8
DATA_OFFSET = 40   # SIMCONNECT_RECV_SIMOBJECT_DATA: cabeçalho (3 DWORD) + 7 DWORD; dados a partir daqui
DEF_ID, REQ_ID = 1, 1


class RECV(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("dwSize", ctypes.c_uint32), ("dwVersion", ctypes.c_uint32), ("dwID", ctypes.c_uint32)]


def dll_candidates(sim: str, configured: str | None = None) -> list[str]:
    here = Path(__file__).resolve().parent.parent
    out = [configured] if configured else []
    out.append(str(here / "simconnect" / sim / "SimConnect.dll"))
    if sim == "msfs":
        for env in ("MSFS2024_SDK", "MSFS_SDK"):
            if os.environ.get(env):
                out.append(os.path.join(os.environ[env], "SimConnect SDK", "lib", "SimConnect.dll"))
        out += [r"C:\MSFS 2024 SDK\SimConnect SDK\lib\SimConnect.dll", r"C:\MSFS SDK\SimConnect SDK\lib\SimConnect.dll"]
    else:
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        out += sorted(glob.glob(os.path.join(pf, "Lockheed Martin", "Prepar3D v5 SDK*", "lib", "SimConnect", "SimConnect.dll")), reverse=True)
        out += sorted(glob.glob(os.path.join(pf, "Lockheed Martin", "Prepar3D v5*", "SimConnect.dll")), reverse=True)
    return out


def find_dll(sim, configured=None):
    return next((p for p in dll_candidates(sim, configured) if p and os.path.isfile(p)), None)


class SimConnectConn:
    def __init__(self, sim: str, dll_path: str | None = None, dll=None):
        self.sim = sim
        self.name = "MSFS 2024" if sim == "msfs" else "Prepar3D v5.3/v5.4"
        self.dll_path, self.dll = dll_path, dll          # testes injetam um SimConnect falso
        self.h = ctypes.c_void_p()
        self.vals: dict[str, float] = {}
        self.last_rx = None
        self.quit = False
        self.exceptions: list[int] = []

    def _load(self):
        if self.dll is not None:
            return
        if os.name != "nt":
            raise ConnectionError("SimConnect só funciona no Windows (no mesmo PC do simulador).")
        path = self.dll_path or find_dll(self.sim)
        if not path:
            raise ConnectionError("SimConnect.dll não encontrado. Instale o SDK do simulador, copie o arquivo para a pasta "
                                  f"simconnect\\{self.sim} ou informe o caminho com --simconnect-dll.")
        self.dll = ctypes.WinDLL(path)
        d = self.dll
        d.SimConnect_Open.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
        d.SimConnect_AddToDataDefinition.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_float, ctypes.c_uint32]
        d.SimConnect_RequestDataOnSimObject.argtypes = [ctypes.c_void_p] + [ctypes.c_uint32] * 8
        d.SimConnect_GetNextDispatch.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(RECV)), ctypes.POINTER(ctypes.c_uint32)]
        d.SimConnect_Close.argtypes = [ctypes.c_void_p]
        for f in ("SimConnect_Open", "SimConnect_AddToDataDefinition", "SimConnect_RequestDataOnSimObject", "SimConnect_GetNextDispatch", "SimConnect_Close"):
            getattr(d, f).restype = ctypes.c_long

    def open(self):
        self._load()
        hr = self.dll.SimConnect_Open(ctypes.byref(self.h), b"LineOps ACARS", None, 0, None, 0)
        if hr != 0:
            raise ConnectionError(f"{self.name} não respondeu ao SimConnect (simulador aberto, com voo carregado?). Código {hr & 0xFFFFFFFF:#x}.")
        for _k, name, unit in SC_VARS:
            self.dll.SimConnect_AddToDataDefinition(self.h, DEF_ID, name.encode(), unit.encode(), SIMCONNECT_DATATYPE_FLOAT64, ctypes.c_float(0), SIMCONNECT_UNUSED)
        self.dll.SimConnect_RequestDataOnSimObject(self.h, REQ_ID, DEF_ID, SIMCONNECT_OBJECT_ID_USER, SIMCONNECT_PERIOD_SIM_FRAME, 0, 0, 5, 0)

    def read(self):
        if self.quit:
            raise ConnectionError(f"{self.name} foi fechado.")
        got = False
        for _ in range(200):
            p = ctypes.POINTER(RECV)()
            cb = ctypes.c_uint32()
            hr = self.dll.SimConnect_GetNextDispatch(self.h, ctypes.byref(p), ctypes.byref(cb))
            if hr != 0 or not p:
                break
            rid = p.contents.dwID
            if rid == RECV_SIMOBJECT_DATA:
                arr = (ctypes.c_double * len(SC_VARS)).from_address(ctypes.addressof(p.contents) + DATA_OFFSET)
                for i, (k, _n, _u) in enumerate(SC_VARS):
                    self.vals[k] = arr[i]
                got = True
            elif rid == RECV_QUIT:
                self.quit = True
            elif rid == RECV_EXCEPTION:
                self.exceptions.append(ctypes.c_uint32.from_address(ctypes.addressof(p.contents) + 12).value)
        if got:
            self.last_rx = _now()
        if not got or "lat" not in self.vals:
            return None
        v = self.vals
        eng = sum(1 for k in ("eng1", "eng2", "eng3", "eng4") if v[k] > 0.5)
        return {"t": self.last_rx, "lat": v["lat"], "lon": v["lon"], "alt_ft": v["alt_ft"], "agl_ft": v["agl_ft"], "gs_kt": v["gs_kt"],
                "ias_kt": v["ias_kt"], "vs_fpm": v["vs_fpm"], "hdg": v["hdg"], "on_ground": v["on_ground"] > 0.5,
                "parking_brake": v["parking_brake"] > 0.5, "engines": eng, "fuel_kg": v["fuel_lb"] * KG_PER_LB,
                "gw_kg": v["gw_lb"] * KG_PER_LB, "g": v["g"]}

    def close(self):
        if self.dll is not None and self.h:
            try:
                self.dll.SimConnect_Close(self.h)
            except Exception:
                pass


# ------------------------------------------------------------------ replay (arquivo .jsonl de telemetria gravada)
class Replay:
    """Lê telemetria de um arquivo JSON Lines (um objeto por linha, 't' em ISO UTC). Serve para testar o cliente sem simulador."""
    name = "REPLAY"

    def __init__(self, path):
        self.path = path
        self.items = []
        self.i = 0

    def open(self):
        for line in Path(self.path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                d["t"] = datetime.strptime(d["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                self.items.append(d)

    def read(self):
        if self.i >= len(self.items):
            raise EOFError("fim do replay")
        self.i += 1
        return self.items[self.i - 1]

    def close(self):
        pass


SIMS = {"xplane": "X-Plane 12", "msfs": "MSFS 2024", "p3d": "Prepar3D v5.3/v5.4", "replay": "REPLAY"}


def make(sim: str, *, xp_host="127.0.0.1", xp_port=49000, dll=None, replay=None):
    if sim == "xplane":
        return XPlaneUDP(xp_host, xp_port)
    if sim in ("msfs", "p3d"):
        return SimConnectConn(sim, dll)
    if sim == "replay":
        if not replay:
            raise ValueError("--replay ARQUIVO é obrigatório com --sim replay.")
        return Replay(replay)
    raise ValueError("Simulador inválido (xplane, msfs, p3d, replay).")
