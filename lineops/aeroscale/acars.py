"""ACARS (opcional, para quem quer realismo total): lê o simulador, registra as fases e os horários OOOI e envia o
PIREP ao diário de bordo, fechando a perna da escala.

OOOI, como no ACARS real:
  OUT  solta o freio de estacionamento e começa a andar (push ou táxi)
  OFF  sai do chão
  ON   toca o chão (razão de descida e força G do toque)
  IN   para no portão com freio de estacionamento ou motores cortados

Fases: PRE → TAXI_OUT → CLIMB → CRUISE → DESCENT → APPROACH → LANDED → TAXI_IN → IN.
A telemetria chega normalizada pelos conectores (acars_sims.py): lat, lon, alt_ft, agl_ft, gs_kt, ias_kt, vs_fpm,
hdg, on_ground, parking_brake, engines, fuel_kg, g.
"""
from __future__ import annotations

import math
import threading
import time as _time
from collections import deque
from datetime import datetime, timedelta, timezone

from . import catalog
from .db import get_settings, now_iso, row, rows

SCHEMA = """
CREATE TABLE IF NOT EXISTS acars_flights (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sim TEXT, leg_id INTEGER, schedule_id INTEGER, callsign TEXT,
    adep TEXT, ades TEXT, aircraft TEXT, phase TEXT, started_at TEXT, out_utc TEXT, off_utc TEXT, on_utc TEXT,
    in_utc TEXT, fuel_out_kg REAL, fuel_off_kg REAL, fuel_on_kg REAL, fuel_in_kg REAL, landing_vs_fpm INTEGER,
    landing_g REAL, bounces INTEGER DEFAULT 0, max_alt_ft INTEGER, distance_nm REAL, logbook_id INTEGER,
    closed INTEGER NOT NULL DEFAULT 0, note TEXT
);
CREATE TABLE IF NOT EXISTS acars_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT, flight_id INTEGER NOT NULL, t TEXT, lat REAL, lon REAL, alt_ft INTEGER,
    gs_kt INTEGER, vs_fpm INTEGER, phase TEXT
);
CREATE INDEX IF NOT EXISTS ix_acars_points ON acars_points(flight_id, id);
CREATE TABLE IF NOT EXISTS acars_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, flight_id INTEGER, t TEXT, kind TEXT, text TEXT
);
"""

PHASE_LABEL = {"PRE": "Pré-voo", "TAXI_OUT": "Táxi de saída", "CLIMB": "Subida", "CRUISE": "Cruzeiro",
               "DESCENT": "Descida", "APPROACH": "Aproximação", "LANDED": "Pouso", "TAXI_IN": "Táxi de chegada",
               "IN": "No portão"}
AIR = ("CLIMB", "CRUISE", "DESCENT", "APPROACH")
POINT_EVERY_S = 10


def _z(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%H%MZ")


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def nm(la1, lo1, la2, lo2) -> float:
    p1, p2 = math.radians(la1), math.radians(la2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 3440.065 * 2 * math.asin(math.sqrt(a))


class PhaseMachine:
    """Máquina de fases pura (sem banco): alimente com telemetria e receba os eventos."""

    def __init__(self):
        self.phase = "PRE"
        self.oooi: dict[str, datetime] = {}
        self.fuel: dict[str, float] = {}
        self.landing_vs = None
        self.landing_g = None
        self.bounces = 0
        self.max_alt = 0.0
        self.distance = 0.0
        self.recent = deque(maxlen=40)        # últimos segundos para o toque
        self.last = None
        self._since = None                    # início de uma condição sustentada
        self._level_since = None

    def _ev(self, kind, text, t):
        return {"kind": kind, "text": text, "t": t}

    def feed(self, tel: dict) -> list[dict]:
        t = tel["t"]
        ev = []
        if self.last and tel.get("lat") is not None and self.last.get("lat") is not None:
            d = nm(self.last["lat"], self.last["lon"], tel["lat"], tel["lon"])
            if d < 5:   # salto grande = reposicionamento no simulador, não conta
                self.distance += d
        self.recent.append(tel)
        gs, vs, agl = tel.get("gs_kt") or 0, tel.get("vs_fpm") or 0, tel.get("agl_ft") or 0
        ground = bool(tel.get("on_ground"))
        fuel = tel.get("fuel_kg")
        if not ground:
            self.max_alt = max(self.max_alt, tel.get("alt_ft") or 0)
        ph = self.phase
        if ph == "PRE" and ground and ((not tel.get("parking_brake") and gs >= 1.5) or gs >= 4):
            self.oooi["OUT"] = t
            self.fuel["out"] = fuel
            self.phase = "TAXI_OUT"
            ev.append(self._ev("OUT", f"OUT {_z(t)}" + (f" FOB {fuel / 1000:.1f}T" if fuel else ""), t))
        elif ph in ("PRE", "TAXI_OUT") and not ground and (agl > 15 or gs > 50):
            if "OUT" not in self.oooi:   # começou já rolando (sem calço registrado)
                self.oooi["OUT"] = t - timedelta(minutes=10)
                self.fuel["out"] = fuel
            self.oooi["OFF"] = t
            self.fuel["off"] = fuel
            self.phase = "CLIMB"
            ev.append(self._ev("OFF", f"OFF {_z(t)}" + (f" FOB {fuel / 1000:.1f}T" if fuel else ""), t))
        elif ph in AIR and ground and self.landing_vs is not None:
            self.phase = "LANDED"   # novo toque depois de um quique: o pouso registrado é o primeiro
            ev.append(self._ev("INFO", f"Toque {_z(t)}", t))
        elif ph in AIR and ground:
            window = [x for x in self.recent if (t - x["t"]).total_seconds() <= 1.5]   # o toque, não a final
            self.landing_vs = int(min((x.get("vs_fpm") or 0) for x in window))
            if self.landing_vs >= 0:   # amostras perdidas perto do toque: usa a última razão negativa ainda no ar
                air = [x for x in self.recent if not x.get("on_ground") and (x.get("vs_fpm") or 0) < 0]
                if air:
                    self.landing_vs = int(air[-1]["vs_fpm"])
            self.landing_g = round(max((x.get("g") or 1.0) for x in window), 2)
            self.oooi["ON"] = t
            self.fuel["on"] = fuel
            self.phase = "LANDED"
            ev.append(self._ev("ON", f"ON {_z(t)} VS {self.landing_vs} FPM {self.landing_g:.2f}G", t))
        elif ph == "LANDED" and not ground and agl > 5:
            self.bounces += 1
            self.phase = "APPROACH"
            ev.append(self._ev("INFO", f"Quique/arremetida às {_z(t)}", t))
        elif ph == "LANDED" and ground and gs < 35:
            self.phase = "TAXI_IN"
            ev.append(self._ev("PHASE", f"Táxi de chegada {_z(t)}", t))
        elif ph == "TAXI_IN" and ground and gs < 1 and (tel.get("parking_brake") or (tel.get("engines") or 0) == 0):
            self._since = self._since or t
            if (t - self._since).total_seconds() >= 5:
                self.oooi["IN"] = t
                self.fuel["in"] = fuel
                self.phase = "IN"
                blk = self.oooi["IN"] - self.oooi["OUT"]
                flt = (self.oooi["ON"] - self.oooi["OFF"]) if "ON" in self.oooi and "OFF" in self.oooi else None
                txt = f"IN {_z(t)} BLK {int(blk.total_seconds() // 3600)}:{int(blk.total_seconds() % 3600 // 60):02d}"
                if flt:
                    txt += f" FLT {int(flt.total_seconds() // 3600)}:{int(flt.total_seconds() % 3600 // 60):02d}"
                if fuel and self.fuel.get("out"):
                    txt += f" USED {(self.fuel['out'] - fuel) / 1000:.1f}T"
                ev.append(self._ev("IN", txt, t))
                ev.append(self._ev("PIREP", "PIREP pronto", t))
        elif ph in AIR and not ground:
            self._since = None
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
        else:
            if ph != "TAXI_IN":
                self._since = None
        self.last = tel
        return ev


# ------------------------------------------------------------------ sessão (conector + máquina + banco)
_SESSION: dict = {"obj": None}
_LOCK = threading.Lock()


def _nearest_airport(apmap, lat, lon, max_nm=15):
    best, bd = None, max_nm
    for icao, a in apmap.items():
        if a.get("lat") is None:
            continue
        d = nm(lat, lon, a["lat"], a["lon"])
        if d < bd:
            best, bd = icao, d
    return best


class Session:
    def __init__(self, db_path: str, sim: str, connector, leg_id: int | None = None):
        self.db_path, self.sim, self.conn = db_path, sim, connector
        self.leg_id = leg_id
        self.pm = PhaseMachine()
        self.flight_id = None
        self.tel = None
        self.status = "conectando"
        self.error = None
        self.stop_ev = threading.Event()
        self._last_point = None
        self.thread = None

    # -- banco
    def _db(self):
        from .db import connect
        return connect(self.db_path)

    def _start_flight(self, db, tel):
        leg = None
        apmap = catalog.airport_map(db)
        here = _nearest_airport(apmap, tel["lat"], tel["lon"]) if tel.get("lat") is not None else None
        if self.leg_id:
            leg = row(db.execute("SELECT * FROM schedule_legs WHERE id=?", (self.leg_id,)))
        elif here:   # perna da escala que parte daqui nas próximas horas
            now = tel["t"]
            leg = row(db.execute(
                "SELECT * FROM schedule_legs WHERE adep=? AND flown_status='' AND activity<>'EXTRA' AND dep_utc BETWEEN ? AND ?"
                " ORDER BY dep_utc LIMIT 1", (here, _iso(now - timedelta(hours=3)), _iso(now + timedelta(hours=6)))))
        self.flight_id = db.execute(
            "INSERT INTO acars_flights(sim, leg_id, schedule_id, callsign, adep, ades, aircraft, phase, started_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (self.sim, leg["id"] if leg else None, leg["schedule_id"] if leg else None,
             leg["flight_number"] if leg else None, (leg or {}).get("adep") or here, (leg or {}).get("ades"),
             (leg or {}).get("type_icao"), "PRE", now_iso())).lastrowid
        self._msg(db, "INFO", f"ACARS conectado ({self.sim})" + (
            f" · {leg['flight_number']} {leg['adep']}→{leg['ades']}" if leg else " · voo livre (sem perna da escala)"))
        db.commit()

    def _msg(self, db, kind, text, t=None):
        db.execute("INSERT INTO acars_messages(flight_id, t, kind, text) VALUES (?,?,?,?)",
                   (self.flight_id, _iso(t) if t else now_iso(), kind, text))

    def handle(self, db, tel):
        if self.flight_id is None:
            self._start_flight(db, tel)
        evs = self.pm.feed(tel)
        pm = self.pm
        if evs or self._last_point is None or (tel["t"] - self._last_point).total_seconds() >= POINT_EVERY_S:
            db.execute("INSERT INTO acars_points(flight_id, t, lat, lon, alt_ft, gs_kt, vs_fpm, phase) VALUES (?,?,?,?,?,?,?,?)",
                       (self.flight_id, _iso(tel["t"]), tel.get("lat"), tel.get("lon"), int(tel.get("alt_ft") or 0),
                        int(tel.get("gs_kt") or 0), int(tel.get("vs_fpm") or 0), pm.phase))
            self._last_point = tel["t"]
        for e in evs:
            if e["kind"] != "PIREP":
                self._msg(db, e["kind"], e["text"], e["t"])
        f = {"phase": pm.phase, "max_alt_ft": int(pm.max_alt), "distance_nm": round(pm.distance, 1),
             "out_utc": _iso(pm.oooi["OUT"]) if "OUT" in pm.oooi else None,
             "off_utc": _iso(pm.oooi["OFF"]) if "OFF" in pm.oooi else None,
             "on_utc": _iso(pm.oooi["ON"]) if "ON" in pm.oooi else None,
             "in_utc": _iso(pm.oooi["IN"]) if "IN" in pm.oooi else None,
             "fuel_out_kg": pm.fuel.get("out"), "fuel_off_kg": pm.fuel.get("off"), "fuel_on_kg": pm.fuel.get("on"),
             "fuel_in_kg": pm.fuel.get("in"), "landing_vs_fpm": pm.landing_vs, "landing_g": pm.landing_g,
             "bounces": pm.bounces}
        db.execute(f"UPDATE acars_flights SET {', '.join(f'{k}=?' for k in f)} WHERE id=?", (*f.values(), self.flight_id))
        if any(e["kind"] == "PIREP" for e in evs):
            self.pirep(db, tel)
        db.commit()
        return evs

    def pirep(self, db, tel):
        """Fecha o voo: diário de bordo (e perna da escala, se houver) com os horários OOOI e os dados do pouso."""
        from . import logbook
        fl = row(db.execute("SELECT * FROM acars_flights WHERE id=?", (self.flight_id,)))
        apmap = catalog.airport_map(db)
        ades = fl["ades"] or _nearest_airport(apmap, tel["lat"], tel["lon"]) or "ZZZZ"
        used = (fl["fuel_out_kg"] - fl["fuel_in_kg"]) if fl["fuel_out_kg"] and fl["fuel_in_kg"] else None
        rem = (f"ACARS ({self.sim}): pouso {fl['landing_vs_fpm']} fpm, {fl['landing_g']} G"
               + (f", {fl['bounces']} quique(s)" if fl["bounces"] else "")
               + (f"; combustível usado {used / 1000:.1f} t" if used else "")
               + f"; {fl['distance_nm']:.0f} nm; altitude máx. {fl['max_alt_ft']} ft")
        data = {"off_block_utc": fl["out_utc"], "takeoff_utc": fl["off_utc"], "landing_utc": fl["on_utc"],
                "on_block_utc": fl["in_utc"], "source": "acars", "network": None, "remarks": rem}
        if fl["leg_id"] and row(db.execute("SELECT id FROM schedule_legs WHERE id=? AND flown_status=''", (fl["leg_id"],))):
            lid = logbook.mark_flown(db, fl["leg_id"], data)["id"]
        else:
            lid = logbook.add(db, {**data, "adep": fl["adep"] or "ZZZZ", "ades": ades, "flight_number": fl["callsign"],
                                   "aircraft_type": fl["aircraft"]})
        db.execute("UPDATE acars_flights SET logbook_id=?, closed=1, ades=? WHERE id=?", (lid, ades, self.flight_id))
        self._msg(db, "PIREP", f"PIREP enviado ao diário de bordo · {rem}")

    # -- laço
    def run(self):
        db = self._db()
        try:
            self.conn.open()
            self.status = "aguardando dados"
            while not self.stop_ev.is_set():
                tel = self.conn.read()
                if tel:
                    self.tel = tel
                    self.status = "recebendo"
                    if self.pm.phase != "IN":
                        self.handle(db, tel)
                elif self.status == "recebendo" and self.tel and \
                        (datetime.now(timezone.utc) - self.tel["t"]).total_seconds() > 10:
                    self.status = "sem dados (simulador pausado ou fechado?)"
                _time.sleep(0.2)
        except Exception as e:   # simulador fechado, DLL ausente…
            self.error = str(e) or type(e).__name__
            self.status = "erro"
        finally:
            try:
                self.conn.close()
            except Exception:
                pass
            if self.flight_id and self.pm.phase != "IN":
                db.execute("UPDATE acars_flights SET closed=1, note=? WHERE id=? AND closed=0",
                           ("interrompido antes do portão", self.flight_id))
                db.commit()
            db.close()

    def start(self):
        self.thread = threading.Thread(target=self.run, name="acars", daemon=True)
        self.thread.start()


def start(db_path: str, sim: str, leg_id: int | None = None, connector=None) -> dict:
    from . import acars_sims
    with _LOCK:
        cur = _SESSION["obj"]
        if cur and cur.thread and cur.thread.is_alive():
            raise ValueError("O ACARS já está conectado. Desconecte antes de trocar de simulador.")
        from .db import connect
        d = connect(db_path)
        try:
            conn = connector or acars_sims.make(sim, get_settings(d))
        finally:
            d.close()
        s = Session(db_path, sim, conn, leg_id)
        _SESSION["obj"] = s
        s.start()
    return {"ok": True}


def stop() -> dict:
    s = _SESSION["obj"]
    if s:
        s.stop_ev.set()
        if s.thread:
            s.thread.join(timeout=3)
    return {"ok": True}


def status(db) -> dict:
    s = _SESSION["obj"]
    live = None
    fid = None
    if s:
        if s.thread and s.thread.is_alive():
            fid = s.flight_id
        t = s.tel
        live = {"sim": s.sim, "status": s.status, "error": s.error, "connected": bool(s.thread and s.thread.is_alive()),
                "phase": s.pm.phase, "phase_label": PHASE_LABEL.get(s.pm.phase),
                "tel": {k: (round(v, 5) if isinstance(v, float) else v) for k, v in t.items() if k != "t"} | {"t": _iso(t["t"])} if t else None}
    if not fid:
        r = row(db.execute("SELECT id FROM acars_flights ORDER BY id DESC LIMIT 1"))
        fid = r["id"] if r else None
    fl = row(db.execute("SELECT * FROM acars_flights WHERE id=?", (fid,))) if fid else None
    pts = rows(db.execute("SELECT t, lat, lon, alt_ft, gs_kt, phase FROM acars_points WHERE flight_id=? ORDER BY id", (fid,))) if fid else []
    msgs = rows(db.execute("SELECT t, kind, text FROM acars_messages WHERE flight_id=? ORDER BY id DESC LIMIT 60", (fid,))) if fid else []
    leg = row(db.execute("SELECT * FROM schedule_legs WHERE id=?", (fl["leg_id"],))) if fl and fl["leg_id"] else None
    apmap = catalog.airport_map(db)
    aps = {}
    for icao in {x for x in ((fl or {}).get("adep"), (fl or {}).get("ades")) if x}:
        a = apmap.get(icao)
        if a and a.get("lat") is not None:
            aps[icao] = {"lat": a["lat"], "lon": a["lon"], "iata": a.get("iata"), "city": a.get("city")}
    return {"enabled": bool(get_settings(db).get("acars_enabled")), "live": live, "flight": fl, "points": pts,
            "messages": msgs, "leg": leg, "airports": aps, "phase_labels": PHASE_LABEL}


def history(db, limit=20) -> list[dict]:
    return rows(db.execute("SELECT * FROM acars_flights ORDER BY id DESC LIMIT ?", (limit,)))
