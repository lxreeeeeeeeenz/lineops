"""Máquina de fases e OOOI (pura, sem rede nem simulador). Alimente com amostras de telemetria.

Telemetria (dict): t (datetime UTC), lat, lon, alt_ft, agl_ft, gs_kt, ias_kt, vs_fpm, hdg, on_ground, parking_brake,
engines (nº de motores ligados), fuel_kg, gw_kg (opcional), g (opcional).
Valores ausentes ficam None — nunca viram zero.
"""
from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timedelta, timezone

AIR = ("CLIMB", "CRUISE", "DESCENT", "APPROACH")
PHASE_LABEL = {"PRE": "Pré-voo", "TAXI_OUT": "Táxi de saída", "CLIMB": "Subida", "CRUISE": "Cruzeiro",
               "DESCENT": "Descida", "APPROACH": "Aproximação", "LANDED": "Pousado", "TAXI_IN": "Táxi de chegada", "IN": "Calços"}


def nm(la1, lo1, la2, lo2) -> float:
    p1, p2 = math.radians(la1), math.radians(la2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 3440.065 * 2 * math.asin(math.sqrt(a))


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _z(t):
    return t.strftime("%H:%MZ")


class PhaseMachine:
    def __init__(self):
        self.phase = "PRE"
        self.oooi: dict[str, datetime] = {}
        self.fuel: dict[str, float | None] = {}
        self.gw: dict[str, float | None] = {}
        self.landing_vs: int | None = None
        self.landing_g: float | None = None
        self.bounces = 0
        self.max_alt = 0.0
        self.distance = 0.0
        self.recent = deque(maxlen=60)
        self.last = None
        self._stopped_since = None
        self._level_since = None
        self._rejected_takeoff = False

    @staticmethod
    def _ev(kind, text, t):
        return {"kind": kind, "text": text, "t": t}

    def feed(self, tel: dict) -> list[dict]:
        t, ev = tel["t"], []
        if self.last and tel.get("lat") is not None and self.last.get("lat") is not None:
            d = nm(self.last["lat"], self.last["lon"], tel["lat"], tel["lon"])
            dt_h = max((t - self.last["t"]).total_seconds(), 0.001) / 3600
            if d / dt_h <= 900:            # velocidade implícita absurda = reposicionamento no simulador; não conta
                self.distance += d
        self.recent.append(tel)
        gs, vs, agl = tel.get("gs_kt") or 0, tel.get("vs_fpm") or 0, tel.get("agl_ft") or 0
        ground, fuel, gw = bool(tel.get("on_ground")), tel.get("fuel_kg"), tel.get("gw_kg")
        if not ground:
            self.max_alt = max(self.max_alt, tel.get("alt_ft") or 0)
        ph = self.phase
        if ph == "PRE" and ground and ((not tel.get("parking_brake") and gs >= 1.5) or gs >= 4):
            self.oooi["OUT"], self.fuel["out"], self.gw["out"] = t, fuel, gw
            self.phase = "TAXI_OUT"
            ev.append(self._ev("OUT", f"OUT {_z(t)}" + (f" FOB {fuel / 1000:.1f}T" if fuel else ""), t))
        elif ph in ("PRE", "TAXI_OUT") and not ground and (agl > 15 or gs > 50):
            if "OUT" not in self.oooi:     # sessão iniciada já em movimento: sem calço real; marcado como estimado
                self.oooi["OUT"], self.fuel["out"], self.gw["out"] = t, fuel, gw
                ev.append(self._ev("WARN", "OUT não observado (ACARS iniciado em movimento) — usando o horário da decolagem", t))
            self.oooi["OFF"], self.fuel["off"], self.gw["off"] = t, fuel, gw
            self.phase = "CLIMB"
            ev.append(self._ev("OFF", f"OFF {_z(t)}" + (f" FOB {fuel / 1000:.1f}T" if fuel else ""), t))
        elif ph in AIR and ground and self.landing_vs is not None:
            self.phase = "LANDED"
            ev.append(self._ev("INFO", f"Novo toque {_z(t)}", t))
        elif ph in AIR and ground:
            window = [x for x in self.recent if (t - x["t"]).total_seconds() <= 1.5]
            self.landing_vs = int(min((x.get("vs_fpm") or 0) for x in window))
            if self.landing_vs >= 0:       # amostras perdidas perto do toque: última razão negativa ainda no ar
                air = [x for x in self.recent if not x.get("on_ground") and (x.get("vs_fpm") or 0) < 0]
                self.landing_vs = int(air[-1]["vs_fpm"]) if air else None
            gs_ = [x["g"] for x in window if x.get("g") is not None]
            self.landing_g = round(max(gs_), 2) if gs_ else None
            self.oooi["ON"], self.fuel["on"], self.gw["on"] = t, fuel, gw
            self.phase = "LANDED"
            txt = f"ON {_z(t)}" + (f" VS {self.landing_vs} FPM" if self.landing_vs is not None else " VS n/d")
            ev.append(self._ev("ON", txt + (f" {self.landing_g:.2f}G" if self.landing_g is not None else ""), t))
        elif ph == "LANDED" and not ground and agl > 5:
            self.bounces += 1
            self.phase = "APPROACH"
            ev.append(self._ev("INFO", f"Quique/arremetida às {_z(t)}", t))
        elif ph == "LANDED" and ground and gs < 35:
            self.phase = "TAXI_IN"
            ev.append(self._ev("PHASE", f"Táxi de chegada {_z(t)}", t))
        elif ph == "TAXI_IN" and ground and gs < 1 and (tel.get("parking_brake") or (tel.get("engines") or 0) == 0):
            self._stopped_since = self._stopped_since or t
            if (t - self._stopped_since).total_seconds() >= 5:
                self.oooi["IN"], self.fuel["in"], self.gw["in"] = t, fuel, gw
                self.phase = "IN"
                blk = self.oooi["IN"] - self.oooi["OUT"]
                txt = f"IN {_z(t)} BLK {int(blk.total_seconds() // 3600)}:{int(blk.total_seconds() % 3600 // 60):02d}"
                if fuel is not None and self.fuel.get("out") is not None:
                    txt += f" USADO {(self.fuel['out'] - fuel) / 1000:.1f}T"
                ev.append(self._ev("IN", txt, t))
        elif ph in AIR and not ground:
            self._stopped_since = None
            new = ph
            if agl < 3000 and vs < -200 and ph in ("DESCENT", "CRUISE"):
                new = "APPROACH"
            elif vs > 500 and ph != "APPROACH":
                new = "CLIMB"
            elif vs < -500 and ph in ("CLIMB", "CRUISE"):
                new = "DESCENT"
            elif abs(vs) < 300 and ph in ("CLIMB", "DESCENT") and agl > 3000:
                self._level_since = self._level_since or t
                if (t - self._level_since).total_seconds() >= 60:
                    new = "CRUISE"
            if abs(vs) >= 300:
                self._level_since = None
            if new != ph:
                self.phase = new
                ev.append(self._ev("PHASE", f"{PHASE_LABEL[new]} {_z(t)} FL{int((tel.get('alt_ft') or 0) / 100):03d}", t))
        elif ph != "TAXI_IN":
            self._stopped_since = None
        self.last = tel
        return ev

    @property
    def complete(self) -> bool:
        return self.phase == "IN"

    def summary(self) -> dict:
        o = self.oooi
        g = lambda k: iso(o[k]) if k in o else None
        return {"out_utc": g("OUT"), "off_utc": g("OFF"), "on_utc": g("ON"), "in_utc": g("IN"),
                "fuel_out_kg": self.fuel.get("out"), "fuel_off_kg": self.fuel.get("off"),
                "fuel_on_kg": self.fuel.get("on"), "fuel_in_kg": self.fuel.get("in"),
                "gw_out_kg": self.gw.get("out"), "gw_off_kg": self.gw.get("off"), "gw_on_kg": self.gw.get("on"),
                "landing_vs_fpm": self.landing_vs, "landing_g": self.landing_g, "bounces": self.bounces,
                "max_alt_ft": int(self.max_alt), "distance_nm": round(self.distance, 1), "phase": self.phase}


# ---------------------------------------------------------------- persistência do estado (retomada após reinício do cliente)
def _dt(s):
    return None if s is None else datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def to_state(pm: PhaseMachine) -> dict:
    return {"phase": pm.phase, "oooi": {k: iso(v) for k, v in pm.oooi.items()}, "fuel": pm.fuel, "gw": pm.gw,
            "landing_vs": pm.landing_vs, "landing_g": pm.landing_g, "bounces": pm.bounces, "max_alt": pm.max_alt, "distance": pm.distance}


def from_state(d: dict) -> PhaseMachine:
    pm = PhaseMachine()
    pm.phase = d["phase"]
    pm.oooi = {k: _dt(v) for k, v in d["oooi"].items()}
    pm.fuel, pm.gw = d["fuel"], d["gw"]
    pm.landing_vs, pm.landing_g, pm.bounces = d["landing_vs"], d["landing_g"], d["bounces"]
    pm.max_alt, pm.distance = d["max_alt"], d["distance"]
    return pm
