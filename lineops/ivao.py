"""IVAO como ACARS (provisório) e “quem está na rede agora”.

Fonte: o whazzup público da IVAO (API v2, sem chave, atualizado a cada 15 s), o mesmo dado que o Webeye desenha.
Quem busca é o servidor do LineOps. O LineOps não inventa telemetria: o que a IVAO não publica (combustível, razão no
toque, força G) fica em branco e o PIREP diz isso.

Acompanhamento: você liga no Flight Center; enquanto houver sessão IVAO ao vivo, um laço em segundo plano lê o whazzup a
cada 15 s, acha você pelo VID e grava a trilha. As fases saem do que a IVAO publica:
  OUT = primeira amostra no solo andando (≥ 3 kt) · OFF = primeira amostra no ar · ON = primeira no solo depois de voar ·
  IN  = parado no solo por duas amostras seguidas depois do pouso, ou desconexão depois do pouso.
Com amostras a cada 15 s, cada horário tem precisão de ±15 s. No IN, a sessão fecha e o PIREP vai para a sua revisão.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from datetime import datetime, timezone

from . import acars_service as acars, geo
from .db import audit, connect, jloads, now_iso, one, rows, tx
from .errors import Conflict, FGVError

log = logging.getLogger("lineops.ivao")
WHAZZUP_URL = "https://api.ivao.aero/v2/tracker/whazzup"
WEBEYE_URL = "https://webeye.ivao.aero/?callsign={}"
MIN_INTERVAL = 15            # a IVAO atualiza a cada 15 s: ler mais rápido não traz dado novo
PULSE_TTL = 60
FMT = "%Y-%m-%dT%H:%M:%SZ"
SIM = "ivao"
LOST_AFTER_AIR_S = 10 * 60   # sem sinal depois de decolar: avisa (não encerra sozinho)
GAP_S = 2 * 60               # 2 min ou mais entre duas leituras: buraco na trilha (LineOps fechada, sem internet)

_lock = threading.Lock()
_cache = {"at": 0.0, "data": None, "error": None, "fetched_at": None}


# ------------------------------------------------------------------ whazzup
def fetch(max_age: float = MIN_INTERVAL, opener=None) -> dict:
    """Whazzup com cache em memória. opener(url) -> bytes só existe para os testes."""
    with _lock:
        if _cache["data"] is not None and time.time() - _cache["at"] < max_age:
            return _cache["data"]
    try:
        if opener:
            raw = opener(WHAZZUP_URL)
        else:
            req = urllib.request.Request(WHAZZUP_URL, headers={"User-Agent": "LineOps (virtual airline pessoal, self-hosted)",
                                                               "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=12) as r:
                raw = r.read()
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or "clients" not in data:
            raise ValueError("resposta sem 'clients'")
    except Exception as e:
        with _lock:
            _cache["error"] = str(e)
            if _cache["data"] is not None:            # devolve o último bom, marcado como velho
                return _cache["data"]
        raise FGVError(f"IVAO indisponível agora ({e}). O servidor do LineOps tem internet?", code="ivao_indisponivel", status=503)
    with _lock:
        _cache.update(at=time.time(), data=data, error=None, fetched_at=now_iso())
    return data


def cache_info() -> dict:
    with _lock:
        return {"fetched_at": _cache["fetched_at"], "error": _cache["error"]}


def pilots(data: dict) -> list[dict]:
    return ((data or {}).get("clients") or {}).get("pilots") or []


def atcs(data: dict) -> list[dict]:
    return ((data or {}).get("clients") or {}).get("atcs") or []


def find_pilot(data: dict, vid) -> dict | None:
    try:
        v = int(str(vid).strip())
    except (TypeError, ValueError):
        return None
    for p in pilots(data):
        try:
            if int(p.get("userId")) == v:
                return p
        except (TypeError, ValueError):
            continue
    return None


def _sample_time(track: dict) -> str:
    ts = track.get("timestamp")
    if ts:
        try:
            d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return d.astimezone(timezone.utc).strftime(FMT)
        except ValueError:
            pass
    return now_iso()


def _brief(p: dict) -> dict:
    fp, t = p.get("flightPlan") or {}, p.get("lastTrack") or {}
    cs = p.get("callsign")
    return {"callsign": cs, "dep": fp.get("departureId"), "arr": fp.get("arrivalId"), "aircraft": fp.get("aircraftId"),
            "alt_ft": t.get("altitude"), "gs_kt": t.get("groundSpeed"), "state": t.get("state"), "on_ground": t.get("onGround"),
            "arr_nm": round(t["arrivalDistance"]) if isinstance(t.get("arrivalDistance"), (int, float)) else None,
            "webeye": WEBEYE_URL.format(cs) if cs else None}


def pulse(airline: str = "GLO", airports: list[str] | None = None, vid=None, opener=None) -> dict:
    """Quem está na rede agora: voos da companhia, controle nos seus aeroportos e você (se o VID estiver online)."""
    data = fetch(PULSE_TTL, opener)
    aps = [a.upper() for a in (airports or []) if a and len(a) == 4]
    al = (airline or "GLO").upper()
    mine = [_brief(p) for p in pilots(data) if str(p.get("callsign") or "").upper().startswith(al)]
    mine.sort(key=lambda x: (x["on_ground"] is not False, x["callsign"] or ""))
    ctl = []
    for a in atcs(data):
        cs = str(a.get("callsign") or "").upper()
        icao = cs.split("_")[0]
        if icao in aps:
            sess = a.get("atcSession") or {}
            ctl.append({"callsign": cs, "airport": icao, "frequency": sess.get("frequency"), "position": sess.get("position"),
                        "webeye": WEBEYE_URL.format(cs)})
    ctl.sort(key=lambda x: (aps.index(x["airport"]), x["callsign"]))
    traffic = {}
    for p in pilots(data):
        fp = p.get("flightPlan") or {}
        for k, key in (("departureId", "dep"), ("arrivalId", "arr")):
            i = str(fp.get(k) or "").upper()
            if i in aps:
                traffic.setdefault(i, {"dep": 0, "arr": 0})[key] += 1
    me = find_pilot(data, vid) if vid else None
    info = cache_info()
    return {"source": "whazzup público da IVAO (o mesmo dado do Webeye)", "updated_at": data.get("updatedAt"),
            "fetched_at": info["fetched_at"], "stale": bool(info["error"]), "error": info["error"],
            "totals": {"pilots": len(pilots(data)), "atcs": len(atcs(data))}, "airline": al,
            "airline_flights": mine[:40], "airline_count": len(mine), "atc": ctl, "traffic": traffic,
            "me": _brief(me) if me else None}


# ------------------------------------------------------------------ acompanhamento de voo
def vid_of(conn) -> str | None:
    r = conn.execute("SELECT value FROM setting WHERE key='ivao_vid'").fetchone()
    return (r[0].strip() or None) if r else None


def start(conn, flight_id: int, *, confirm_restrictions=False, user_id=None) -> dict:
    vid = vid_of(conn)
    if not vid:
        raise Conflict("Informe o seu VID da IVAO em Ajustes antes.", code="sem_vid")
    s = acars.start_session(conn, flight_id, sim=SIM, client_version="IVAO whazzup", confirm_restrictions=confirm_restrictions, user_id=user_id)
    _msg(conn, s["id"], "INFO", f"Acompanhamento pela IVAO ligado. Conecte com o VID {vid}; o LineOps lê a rede a cada 15 s.")
    return s


def _msg(conn, sid, kind, text, t=None):
    seq = conn.execute("SELECT COALESCE(MAX(client_seq),0)+1 FROM acars_message WHERE session_id=?", (sid,)).fetchone()[0]
    conn.execute("INSERT OR IGNORE INTO acars_message(session_id, client_seq, t, kind, text) VALUES (?,?,?,?,?)",
                 (sid, seq, t or now_iso(), kind, text[:300]))


def _secs(a, b):
    return (datetime.strptime(b, FMT) - datetime.strptime(a, FMT)).total_seconds()


def process(conn, session: dict, flight: dict, pilot: dict | None, now: str | None = None) -> str | None:
    """Aplica uma leitura do whazzup a uma sessão IVAO. Devolve 'complete' quando o voo terminou."""
    now = now or now_iso()
    sid = session["id"]
    snap = jloads(session["snapshot"], {})
    events = []
    with tx(conn):
        if not pilot:
            if snap.get("ivao_online"):
                snap["ivao_online"] = False
                snap["ivao_offline_since"] = now
                events.append(("WARN", "Você saiu da IVAO."))
            done = None
            if snap.get("on_utc") and not snap.get("in_utc"):          # desconectou depois do pouso: fecha no último sinal
                snap["in_utc"] = snap.get("ivao_last_seen") or now
                snap["phase"] = "IN"
                events.append(("INFO", f"Desconexão depois do pouso: calço registrado no último sinal ({snap['in_utc'][11:16]}Z)."))
                done = "complete"
            elif snap.get("off_utc") and not snap.get("on_utc") and snap.get("ivao_offline_since") and \
                    _secs(snap["ivao_offline_since"], now) >= LOST_AFTER_AIR_S and not snap.get("ivao_lost_warned"):
                snap["ivao_lost_warned"] = True
                events.append(("WARN", "Sem sinal na IVAO há 10 min com o avião no ar. Reconecte ou encerre o acompanhamento."))
            for k, txt in events:
                _msg(conn, sid, k, txt, now)
            conn.execute("UPDATE acars_session SET snapshot=?, phase=COALESCE(?,phase), last_seen_at=? WHERE id=?",
                         (json.dumps(snap), snap.get("phase"), now, sid))
            return done
        t, fp = pilot.get("lastTrack") or {}, pilot.get("flightPlan") or {}
        ts = _sample_time(t)
        if snap.get("ivao_last_sample") == ts:                         # mesma amostra de antes: nada novo
            conn.execute("UPDATE acars_session SET last_seen_at=? WHERE id=?", (now, sid))
            return None
        first = not snap.get("ivao_callsign")
        gap_min = None
        prev_ts = snap.get("ivao_last_sample")
        if prev_ts and ts and _secs(prev_ts, ts) >= GAP_S:
            gap_min = int(_secs(prev_ts, ts) // 60)
            snap.setdefault("gaps", []).append({"from": prev_ts, "to": ts, "min": gap_min})
            events.append(("WARN", f"{gap_min} min sem leitura da IVAO ({prev_ts[11:16]}Z a {ts[11:16]}Z): a LineOps estava fechada "
                                   "(atualização?) ou sem internet. A trilha tem um buraco nesse trecho; o acompanhamento continua."))
        late = (lambda: f" (primeiro sinal depois de {gap_min} min sem leitura: o horário real foi até {gap_min} min antes)") if gap_min else (lambda: "")
        snap.update(ivao_online=True, ivao_offline_since=None, ivao_last_seen=ts, ivao_last_sample=ts,
                    ivao_callsign=pilot.get("callsign"), ivao_state=t.get("state"), ivao_session_id=pilot.get("id"))
        if first:
            events.append(("INFO", f"Encontrado na IVAO como {pilot.get('callsign')} ({fp.get('departureId') or '?'}→{fp.get('arrivalId') or '?'})."))
            if fp.get("departureId") and fp["departureId"] != flight["adep"] or fp.get("arrivalId") and fp["arrivalId"] != flight["ades"]:
                events.append(("WARN", f"O plano na IVAO ({fp.get('departureId')}→{fp.get('arrivalId')}) difere do voo "
                                       f"({flight['adep']}→{flight['ades']})."))
        lat, lon = t.get("latitude"), t.get("longitude")
        gs, alt, on_ground = t.get("groundSpeed") or 0, t.get("altitude"), t.get("onGround")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            prev = snap.get("lat"), snap.get("lon")
            if snap.get("out_utc") and not snap.get("in_utc") and None not in prev:
                snap["distance_nm"] = round((snap.get("distance_nm") or 0) + geo.nm(prev[0], prev[1], lat, lon), 1)
            conn.execute("INSERT OR IGNORE INTO track_point(session_id, t, lat, lon, alt_ft, gs_kt, vs_fpm, hdg, phase) VALUES (?,?,?,?,?,?,?,?,?)",
                         (sid, ts, lat, lon, alt, gs, None, t.get("heading"), snap.get("phase") or "PRE"))
            snap.update(lat=lat, lon=lon)
        snap.update(alt_ft=alt, gs_kt=gs, hdg=t.get("heading"))
        if isinstance(alt, (int, float)) and snap.get("off_utc"):
            snap["max_alt_ft"] = max(snap.get("max_alt_ft") or 0, alt)
        done = None
        if not snap.get("out_utc") and on_ground is not False and gs >= 3:
            snap["out_utc"], snap["phase"] = ts, "TAXI_OUT"
            events.append(("INFO", f"Calços fora (OUT) {ts[11:16]}Z{late()}."))
        if not snap.get("off_utc") and on_ground is False:
            snap.setdefault("out_utc", ts)
            snap["off_utc"], snap["phase"] = ts, "AIRBORNE"
            events.append(("INFO", f"Decolagem (OFF) {ts[11:16]}Z{late()}."))
        elif snap.get("off_utc") and not snap.get("on_utc") and on_ground is True:
            snap["on_utc"], snap["phase"] = ts, "TAXI_IN"
            events.append(("INFO", f"Pouso (ON) {ts[11:16]}Z{late()}."))
        elif snap.get("on_utc") and not snap.get("in_utc"):
            if gs <= 1:
                if snap.get("ivao_stopped_since"):
                    snap["in_utc"], snap["phase"] = snap["ivao_stopped_since"], "IN"
                    events.append(("INFO", f"Calços (IN) {snap['in_utc'][11:16]}Z. Voo encerrado; PIREP enviado para revisão."))
                    done = "complete"
                else:
                    snap["ivao_stopped_since"] = ts
            else:
                snap["ivao_stopped_since"] = None
        if snap.get("off_utc") and not snap.get("on_utc"):
            st = str(t.get("state") or "").upper()
            snap["phase"] = {"INITIAL CLIMB": "CLIMB", "EN ROUTE": "CRUISE", "APPROACH": "APPROACH"}.get(st, snap.get("phase"))
        for k, txt in events:
            _msg(conn, sid, k, txt, ts)
        conn.execute("UPDATE acars_session SET snapshot=?, phase=?, last_seen_at=? WHERE id=?",
                     (json.dumps(snap), snap.get("phase") or "PRE", now, sid))
    return done


def finish(conn, sid: int, *, user_id=None, force=False) -> dict:
    """Fecha a sessão IVAO e cria o PIREP com o que a rede mostrou (o PIREP marca o que faltou)."""
    s = one(conn.execute("SELECT * FROM acars_session WHERE id=?", (sid,)))
    if not s or s["sim"] != SIM:
        raise Conflict("Esta sessão não é de acompanhamento pela IVAO.", code="nao_ivao")
    snap = jloads(s["snapshot"], {})
    if not snap.get("out_utc") and not force:
        raise Conflict("A IVAO ainda não mostrou você saindo do portão. Cancele o acompanhamento em vez de encerrar.", code="sem_dados")
    summary = {k: snap.get(k) for k in ("out_utc", "off_utc", "on_utc", "in_utc", "max_alt_ft", "distance_nm") if snap.get(k) is not None}
    out = acars.complete(conn, sid, summary, user_id=user_id)
    if not out.get("duplicate"):
        with tx(conn):
            p = one(conn.execute("SELECT data, warnings FROM pirep WHERE id=?", (out["pirep_id"],)))
            data, warns = jloads(p["data"], {}), jloads(p["warnings"], [])
            data["measured_by"] = f"rede IVAO (whazzup público, amostras a cada ~15 s) como {snap.get('ivao_callsign') or '—'}"
            data["network"] = "IVAO"
            data["webeye"] = WEBEYE_URL.format(snap["ivao_callsign"]) if snap.get("ivao_callsign") else None
            warns.append({"level": "info", "code": "ivao_limites", "message": "A IVAO não publica combustível, razão no toque nem força G: "
                                                                             "esses campos ficam em branco. Horários com precisão de ±15 s."})
            conn.execute("UPDATE pirep SET data=?, warnings=? WHERE id=?", (json.dumps(data, ensure_ascii=False),
                                                                          json.dumps(warns, ensure_ascii=False), out["pirep_id"]))
            audit(conn, user_id, "ivao.pirep", "pirep", out["pirep_id"], {"callsign": snap.get("ivao_callsign")})
    return out


def tick(conn, opener=None, now: str | None = None) -> int:
    """Uma volta do laço: lê o whazzup só se houver sessão IVAO ao vivo. Devolve quantas sessões processou."""
    live = rows(conn.execute("SELECT * FROM acars_session WHERE status='LIVE' AND sim=?", (SIM,)))
    if not live:
        return 0
    vid = vid_of(conn)
    try:
        data = fetch(MIN_INTERVAL, opener)
    except FGVError:
        return 0
    pilot = find_pilot(data, vid) if vid else None
    for s in live:
        f = one(conn.execute("SELECT * FROM flight WHERE id=?", (s["flight_id"],)))
        try:
            if process(conn, s, f, pilot, now) == "complete":
                finish(conn, s["id"])
        except Exception:
            log.exception("falha ao processar a sessão IVAO %s", s["id"])
    return len(live)


class Poller(threading.Thread):
    """Laço em segundo plano do servidor, para cada carreira: IVAO (dorme sem voo acompanhado), equipe uma vez por minuto,
    Rede a cada 5 min e o backup automático uma vez por hora (se o último tem mais de 24 h)."""

    def __init__(self, data_dir):
        super().__init__(name="lineops-ivao", daemon=True)
        from pathlib import Path
        self.data_dir, self.stop_ev, self.n = Path(data_dir), threading.Event(), 0

    def run(self):
        from . import careers
        while not self.stop_ev.wait(MIN_INTERVAL):
            self.n += 1
            try:
                core = connect(careers.core_path(self.data_dir))
            except Exception:
                log.exception("núcleo indisponível")
                continue
            try:
                for path in careers.career_files(self.data_dir):
                    try:
                        c = connect(path)
                        try:
                            careers.sync_settings(core, c)
                            tick(c)
                            if self.n % 4 == 1:            # a equipe trabalha uma vez por minuto (lembretes, DOV, mensagens)
                                from . import equipe
                                equipe.run(c)
                            if vid_of(c):                  # a Rede anota quem da companhia está online, a cada 5 min
                                from . import network
                                try:
                                    network.maybe_record(c)
                                except FGVError:
                                    pass
                        finally:
                            c.close()
                    except Exception:
                        log.exception("laço da carreira %s", path.stem)
            finally:
                core.close()
            if self.n % 240 == 2:
                from . import backup
                try:
                    b = backup.auto_backup(self.data_dir)
                    if b:
                        log.info("backup automático: %s", b.name)
                except Exception:
                    log.exception("backup automático")

    def stop(self):
        self.stop_ev.set()
