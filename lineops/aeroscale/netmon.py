"""Monitor de rede: reconhece o seu voo online na IVAO ou na VATSIM e fecha a perna no diário de bordo.

Lê os feeds públicos (sem login) a cada POLL_S segundos enquanto ligado:
* IVAO: https://api.ivao.aero/v2/tracker/whazzup  (clients.pilots[]: callsign, userId, lastTrack{latitude,
  longitude, altitude, groundSpeed, onGround}, flightPlan{departureId, arrivalId, aircraftId})
* VATSIM: https://data.vatsim.net/v3/vatsim-data.json  (pilots[]: callsign, cid, latitude, longitude, altitude,
  groundspeed, flight_plan{departure, arrival, aircraft_short})

O piloto é achado pelo VID/CID informado ou, sem ele, pelo indicativo de uma perna da escala nas próximas horas.
Fases: no solo → táxi (calço) → decolagem → em voo → pouso → calço. Ao parar no destino (ou sumir do feed depois
do pouso), a perna é marcada como voada com os horários reais; voo sem perna correspondente entra avulso no diário.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time as _time
import urllib.request
from datetime import datetime, timedelta, timezone

from . import __version__, catalog
from .db import get_settings, now_iso, row, rows

URLS = {"ivao": "https://api.ivao.aero/v2/tracker/whazzup", "vatsim": "https://data.vatsim.net/v3/vatsim-data.json"}
USER_AGENT = f"AeroScale/{__version__} (diario de bordo; leitura do feed publico)"
POLL_S = 60
TAXI_GS = 5          # kt: começou a andar (calço)
AIRBORNE_GS = 60     # kt: acima disto e fora do solo = decolou
STOP_GS = 3          # kt: parado (calço na chegada)
NEAR_KM = 12         # distância do aeroporto para considerar pouso/parada nele
LOST_MIN = 15        # some do feed por mais que isto: encerra a sessão

SCHEMA = """
CREATE TABLE IF NOT EXISTS net_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, network TEXT NOT NULL, callsign TEXT, user_id TEXT, leg_id INTEGER,
    adep TEXT, ades TEXT, aircraft TEXT, phase TEXT NOT NULL DEFAULT 'solo', first_seen TEXT, last_seen TEXT,
    off_block_utc TEXT, takeoff_utc TEXT, landing_utc TEXT, on_block_utc TEXT,
    lat REAL, lon REAL, alt INTEGER, gs INTEGER, max_alt INTEGER, logbook_id INTEGER, closed INTEGER NOT NULL DEFAULT 0,
    note TEXT
);
"""

_STATE = {"running": False, "last_poll": None, "last_error": None, "thread": None}
_LOCK = threading.Lock()


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def _km(la1, lo1, la2, lo2) -> float:
    p1, p2 = math.radians(la1), math.radians(la2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def fetch(network: str) -> dict:
    url = os.environ.get(f"AEROSCALE_{network.upper()}_URL") or URLS[network]
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def pilots(network: str, feed: dict) -> list[dict]:
    """Lista normalizada: callsign, uid, lat, lon, alt, gs, on_ground (None = desconhecido), adep, ades, aircraft."""
    out = []
    if network == "ivao":
        lst = ((feed.get("clients") or {}).get("pilots")) or []
        for p in lst:
            t = p.get("lastTrack") or {}
            fp = p.get("flightPlan") or {}
            ac = fp.get("aircraftId") or ((fp.get("aircraft") or {}).get("icaoCode"))
            out.append({"callsign": (p.get("callsign") or "").upper(), "uid": str(p.get("userId") or ""),
                        "lat": t.get("latitude"), "lon": t.get("longitude"), "alt": t.get("altitude"),
                        "gs": t.get("groundSpeed"), "on_ground": t.get("onGround"),
                        "adep": fp.get("departureId"), "ades": fp.get("arrivalId"), "aircraft": ac})
    else:
        for p in feed.get("pilots") or []:
            fp = p.get("flight_plan") or {}
            out.append({"callsign": (p.get("callsign") or "").upper(), "uid": str(p.get("cid") or ""),
                        "lat": p.get("latitude"), "lon": p.get("longitude"), "alt": p.get("altitude"),
                        "gs": p.get("groundspeed"), "on_ground": None,
                        "adep": fp.get("departure"), "ades": fp.get("arrival"), "aircraft": fp.get("aircraft_short")})
    return [p for p in out if p["callsign"] and p["lat"] is not None and p["lon"] is not None]


def _digits(s: str | None) -> str:
    import re
    m = re.search(r"(\d+)$", s or "")
    return m.group(1).lstrip("0") if m else ""


def _candidate_legs(db, now: datetime) -> list[dict]:
    lo, hi = _iso(now - timedelta(hours=5)), _iso(now + timedelta(hours=4))
    return rows(db.execute("SELECT * FROM schedule_legs WHERE dep_utc BETWEEN ? AND ? AND flown_status=''"
                           " AND activity<>'EXTRA' ORDER BY dep_utc", (lo, hi)))


def _match_leg(legs: list[dict], p: dict) -> dict | None:
    d = _digits(p["callsign"])
    if not d:
        return None
    best = None
    for l in legs:
        if _digits(l.get("flight_number")) != d:
            continue
        if (l.get("airline") or "")[:3] != p["callsign"][:3] and not p["callsign"][:2].isalpha():
            continue
        if p.get("adep") and p["adep"].upper() != l["adep"]:
            continue
        best = best or l
    return best


def _find_me(db, s: dict, net: str, plist: list[dict], now: datetime):
    uid = (s.get(f"net_{net}_id") or "").strip()
    if uid:
        me = next((p for p in plist if p["uid"] == uid), None)
        if me:
            return me, _match_leg(_candidate_legs(db, now), me)
        return None, None
    legs = _candidate_legs(db, now)
    for p in plist:
        l = _match_leg(legs, p)
        if l:
            return p, l
    return None, None


def poll_once(db, now: datetime | None = None, feeds: dict | None = None) -> dict:
    """Uma leitura de cada rede ligada. feeds={'ivao': {...}} injeta o feed (testes)."""
    s = get_settings(db)
    now = now or datetime.now(timezone.utc)
    nets = {"ivao": ["ivao"], "vatsim": ["vatsim"], "ambas": ["ivao", "vatsim"]}.get(s.get("net_network") or "ambas", [])
    apmap = catalog.airport_map(db)
    events = []
    for net in nets:
        feed = (feeds or {}).get(net)
        if feed is None and feeds is not None:
            continue
        if feed is None:
            feed = fetch(net)
        plist = pilots(net, feed)
        me, leg = _find_me(db, s, net, plist, now)
        sess = row(db.execute("SELECT * FROM net_sessions WHERE network=? AND closed=0 ORDER BY id DESC LIMIT 1", (net,)))
        if sess and not me:
            last = _parse(sess["last_seen"])
            if last and now - last > timedelta(minutes=LOST_MIN):
                events.append(_close(db, sess, now, apmap, lost=True))
            continue
        if not me:
            continue
        if sess and sess["callsign"] != me["callsign"]:
            events.append(_close(db, sess, now, apmap, lost=True))
            sess = None
        if not sess:
            adep = (me.get("adep") or (leg or {}).get("adep") or "").upper() or None
            ades = (me.get("ades") or (leg or {}).get("ades") or "").upper() or None
            sid = db.execute("INSERT INTO net_sessions(network, callsign, user_id, leg_id, adep, ades, aircraft, first_seen,"
                             " last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
                             (net, me["callsign"], me["uid"], leg["id"] if leg else None, adep, ades, me.get("aircraft"),
                              _iso(now), _iso(now))).lastrowid
            sess = row(db.execute("SELECT * FROM net_sessions WHERE id=?", (sid,)))
            events.append({"event": "online", "network": net, "callsign": me["callsign"], "leg_id": sess["leg_id"]})
        events += _advance(db, sess, me, now, apmap)
    _STATE["last_poll"] = now_iso()
    return {"events": events}


def _near(apmap, icao, lat, lon) -> bool:
    a = apmap.get(icao or "")
    return bool(a and a.get("lat") is not None and _km(a["lat"], a["lon"], lat, lon) <= NEAR_KM)


def _advance(db, sess: dict, me: dict, now: datetime, apmap) -> list[dict]:
    gs = float(me.get("gs") or 0)
    alt = int(me.get("alt") or 0)
    ground = me.get("on_ground")
    if ground is None:   # VATSIM não informa: no solo se lento
        ground = gs < 40
    ph = sess["phase"]
    upd = {"last_seen": _iso(now), "lat": me["lat"], "lon": me["lon"], "alt": alt, "gs": int(gs),
           "max_alt": max(sess.get("max_alt") or 0, alt)}
    ev = []
    if ph == "solo" and gs >= TAXI_GS:
        upd.update(phase="taxi", off_block_utc=_iso(now))
        ph = "taxi"
    if ph in ("solo", "taxi") and not ground and gs >= AIRBORNE_GS:
        upd.update(phase="voo", takeoff_utc=_iso(now), off_block_utc=sess["off_block_utc"] or upd.get("off_block_utc")
                   or _iso(now - timedelta(minutes=12)))
        ev.append({"event": "decolagem", "callsign": sess["callsign"]})
        ph = "voo"
    elif ph == "voo" and ground:
        upd.update(phase="pousado", landing_utc=_iso(now))
        ev.append({"event": "pouso", "callsign": sess["callsign"]})
        ph = "pousado"
    if ph == "pousado" and gs <= STOP_GS:
        upd.update(on_block_utc=_iso(now))
        db.execute(f"UPDATE net_sessions SET {', '.join(f'{k}=?' for k in upd)} WHERE id=?", (*upd.values(), sess["id"]))
        sess = row(db.execute("SELECT * FROM net_sessions WHERE id=?", (sess["id"],)))
        ev.append(_close(db, sess, now, apmap, lost=False))
        return ev
    db.execute(f"UPDATE net_sessions SET {', '.join(f'{k}=?' for k in upd)} WHERE id=?", (*upd.values(), sess["id"]))
    return ev


def _close(db, sess: dict, now: datetime, apmap, lost: bool) -> dict:
    """Encerra a sessão; se houve decolagem e pouso, grava no diário (e fecha a perna da escala)."""
    from . import logbook
    if not sess["takeoff_utc"] or not sess["landing_utc"]:
        db.execute("UPDATE net_sessions SET closed=1, note=? WHERE id=?",
                   ("saiu da rede antes do pouso" if lost else "encerrada", sess["id"]))
        return {"event": "incompleto", "callsign": sess["callsign"]}
    on_block = sess["on_block_utc"] or _iso(_parse(sess["landing_utc"]) + timedelta(minutes=8))
    data = {"off_block_utc": sess["off_block_utc"], "on_block_utc": on_block, "takeoff_utc": sess["takeoff_utc"],
            "landing_utc": sess["landing_utc"], "network": sess["network"].upper(), "source": sess["network"],
            "remarks": f"Detectado na {sess['network'].upper()} como {sess['callsign']}"
                       + (f"; altitude máx. {sess['max_alt']} ft" if sess.get("max_alt") else "")}
    ades_real = sess["ades"]
    if sess.get("lat") is not None and sess["ades"] and not _near(apmap, sess["ades"], sess["lat"], sess["lon"]):
        data["remarks"] += f"; parou fora de {sess['ades']} (alternado?)"
    if sess["leg_id"] and row(db.execute("SELECT id FROM schedule_legs WHERE id=? AND flown_status=''", (sess["leg_id"],))):
        e = logbook.mark_flown(db, sess["leg_id"], data)
        lid = e["id"]
    else:
        lid = logbook.add(db, {**data, "flight_number": sess["callsign"], "adep": sess["adep"] or "ZZZZ",
                               "ades": ades_real or "ZZZZ", "aircraft_type": sess.get("aircraft"),
                               "airline": sess["callsign"][:3]})
    db.execute("UPDATE net_sessions SET closed=1, logbook_id=?, phase='fechado' WHERE id=?", (lid, sess["id"]))
    return {"event": "registrado", "callsign": sess["callsign"], "logbook_id": lid, "leg_id": sess["leg_id"]}


# ------------------------------------------------------------------ laço em segundo plano
def start(db_path: str):
    with _LOCK:
        if _STATE["running"]:
            return
        _STATE["running"] = True

    def loop():
        from .db import connect
        while True:
            db = connect(db_path)
            try:
                if not get_settings(db).get("net_enabled"):
                    break
                poll_once(db)
                db.commit()
                _STATE["last_error"] = None
            except Exception as e:  # sem internet, feed fora do ar…
                db.rollback()
                _STATE["last_error"] = f"{type(e).__name__}: {e}"
            finally:
                db.close()
            _time.sleep(POLL_S)
        _STATE["running"] = False

    t = threading.Thread(target=loop, name="netmon", daemon=True)
    _STATE["thread"] = t
    t.start()


def status(db) -> dict:
    s = get_settings(db)
    cur = rows(db.execute("SELECT * FROM net_sessions WHERE closed=0 ORDER BY id DESC"))
    recent = rows(db.execute("SELECT * FROM net_sessions WHERE closed=1 ORDER BY id DESC LIMIT 8"))
    return {"enabled": bool(s.get("net_enabled")), "network": s.get("net_network") or "ambas",
            "ivao_id": s.get("net_ivao_id") or "", "vatsim_id": s.get("net_vatsim_id") or "",
            "running": _STATE["running"], "last_poll": _STATE["last_poll"], "last_error": _STATE["last_error"],
            "poll_s": POLL_S, "current": cur, "recent": recent}
