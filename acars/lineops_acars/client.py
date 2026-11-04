"""Cliente ACARS: lê o simulador, roda a máquina de fases e reporta ao servidor LineOps (tolerante a quedas de rede)."""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .phases import PHASE_LABEL, PhaseMachine, from_state, iso, to_state

POINT_EVERY_S = 10
FLUSH_EVERY_S = 5
HEARTBEAT_S = 30          # avisa o servidor que o cliente continua vivo mesmo sem telemetria nova
MAX_BUFFER = 20000


class ApiError(Exception):
    def __init__(self, status, body):
        self.status, self.body = status, body
        msg = body.get("message") if isinstance(body, dict) else str(body)
        super().__init__(f"HTTP {status}: {msg}")


class Api:
    """Fala com o LineOps. Repete sozinho quando a rede falha; nunca repete quando o servidor recusou."""

    def __init__(self, base: str, token: str, timeout=15, tries=3, sleep=time.sleep):
        if not base.startswith(("http://", "https://")):
            raise ValueError("A URL do servidor deve começar com http:// ou https://")
        self.base, self.token, self.timeout = base.rstrip("/"), token, timeout
        self.tries, self.sleep = tries, sleep

    def call(self, method, path, body=None):
        erro = None
        for n in range(self.tries):
            req = urllib.request.Request(self.base + path, method=method,
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read() or b"null")
            except urllib.error.HTTPError as e:                  # o servidor respondeu: não adianta repetir
                try:
                    b = json.loads(e.read())
                except Exception:
                    b = {"message": e.reason}
                raise ApiError(e.code, b)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                erro = e
                if n + 1 < self.tries:
                    self.sleep(min(2 ** n, 8))                   # 1 s, 2 s, 4 s…
        raise erro


class Runner:
    def __init__(self, api: Api, connector, flight_id: int, *, sim_label: str, state_dir: Path, confirm_restrictions=False, log=print, clock=time.time):
        self.api, self.conn, self.flight_id = api, connector, flight_id
        self.sim_label, self.state_dir, self.confirm = sim_label, Path(state_dir), confirm_restrictions
        self.log, self.clock = log, clock
        self.pm = PhaseMachine()
        self.sid = None
        self.points, self.msgs = [], []
        self.seq = 0
        self._last_point_t = self._last_flush = None

    # ------------------------------------------------------------ estado em disco (retomada)
    def _state_file(self):
        return self.state_dir / f"session-{self.sid}.json"

    def _save(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._state_file().with_suffix(".tmp")
        tmp.write_text(json.dumps({"pm": to_state(self.pm), "seq": self.seq, "points": self.points, "msgs": self.msgs}))
        tmp.replace(self._state_file())

    def _load(self):
        f = self._state_file()
        if f.exists():
            d = json.loads(f.read_text())
            self.pm, self.seq, self.points, self.msgs = from_state(d["pm"]), d["seq"], d["points"], d["msgs"]
            self.log(f"Sessão retomada (fase {PHASE_LABEL[self.pm.phase]}).")

    # ------------------------------------------------------------ rede
    def start(self):
        s = self.api.call("POST", "/api/acars/sessions", {"flight_id": self.flight_id, "sim": self.sim_label,
                                                          "client_version": __version__, "confirm_restrictions": self.confirm})
        self.sid = s["id"]
        f = s["flight"]
        self.log(f"ACARS {'retomado' if s.get('resumed') else 'iniciado'}: {f.get('flight_number') or 'sem nº'} {f['adep']}→{f['ades']} · {f.get('registration')}")
        for r in s.get("restrictions", []):
            self.log(f"  Restrição ({r['restriction']}): {r['title']} {r.get('text') or ''}")
        self._load()
        return s

    def flush(self, force=False, snapshot=None):
        if not self.sid or (not force and not (self.points or self.msgs)):
            return True
        pts, msgs = self.points[:500], self.msgs[:200]
        body = {"points": pts, "messages": msgs, "snapshot": snapshot or {"phase": self.pm.phase}}
        try:
            self.api.call("POST", f"/api/acars/sessions/{self.sid}/report", body)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            self.log("  (servidor inacessível — dados guardados, reenviando depois)")
            return False
        self.points, self.msgs = self.points[len(pts):], self.msgs[len(msgs):]
        self._save()
        return True

    def finish(self):
        for _ in range(60):                      # tenta por ~1 min; os dados já estão em disco
            if self.flush(force=True):
                if not (self.points or self.msgs):
                    break
            else:
                time.sleep(1)
        res = self.api.call("POST", f"/api/acars/sessions/{self.sid}/complete", self.pm.summary())
        self._state_file().unlink(missing_ok=True)
        self.log(f"PIREP #{res['pirep_id']} enviado para revisão." + ("" if not res.get("warnings") else f" {len(res['warnings'])} aviso(s) para você revisar."))
        return res

    # ------------------------------------------------------------ laço principal
    def _message(self, e):
        self.seq += 1
        self.msgs.append({"seq": self.seq, "t": iso(e["t"]), "kind": e["kind"], "text": e["text"]})
        self.log(f"  [{e['kind']}] {e['text']}")

    def step(self, tel) -> bool:
        """Processa uma amostra. Devolve True quando o voo terminou (calços)."""
        for e in self.pm.feed(tel):
            self._message(e)
            self._add_point(tel)
            self._save()
        if self._last_point_t is None or (tel["t"] - self._last_point_t).total_seconds() >= POINT_EVERY_S:
            self._add_point(tel)
        now = tel["t"]
        if self._last_flush is None or (now - self._last_flush).total_seconds() >= FLUSH_EVERY_S:
            self._last_flush = now
            snap = {"phase": self.pm.phase, "lat": tel.get("lat"), "lon": tel.get("lon"), "alt_ft": tel.get("alt_ft"), "gs_kt": tel.get("gs_kt"),
                    "vs_fpm": tel.get("vs_fpm"), "hdg": tel.get("hdg"), "fuel_kg": tel.get("fuel_kg"), "gw_kg": tel.get("gw_kg"),
                    "max_alt_ft": int(self.pm.max_alt), "distance_nm": round(self.pm.distance, 1)}
            snap.update({k: v for k, v in self.pm.summary().items() if k.endswith("_utc") or k.startswith("landing")})
            self.flush(force=True, snapshot=snap)
        return self.pm.complete

    def _add_point(self, tel):
        if tel.get("lat") is None or tel.get("lon") is None:
            return
        if len(self.points) >= MAX_BUFFER:
            del self.points[:1000]
        self.points.append({"t": iso(tel["t"]), "lat": round(tel["lat"], 5), "lon": round(tel["lon"], 5),
                            "alt_ft": None if tel.get("alt_ft") is None else int(tel["alt_ft"]),
                            "gs_kt": None if tel.get("gs_kt") is None else int(tel["gs_kt"]),
                            "vs_fpm": None if tel.get("vs_fpm") is None else int(tel["vs_fpm"]), "phase": self.pm.phase})
        self._last_point_t = tel["t"]

    def heartbeat(self):
        """Mantém a sessão viva no servidor enquanto o simulador não manda nada (pausa, menu, carregando)."""
        if not self.sid:
            return
        try:
            self.api.call("POST", f"/api/acars/sessions/{self.sid}/report", {"points": [], "messages": [], "snapshot": {"phase": self.pm.phase}})
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ApiError):
            pass

    def run(self, poll_s=0.1, max_idle_s=600):
        self.conn.open()
        idle_since = last_beat = self.clock()
        avisou = False
        try:
            while True:
                tel = self.conn.read()
                if tel is None:
                    parado = self.clock() - idle_since
                    if parado > max_idle_s:
                        self.log("Sem telemetria do simulador há muito tempo; encerrando (a sessão continua aberta no servidor).")
                        return None
                    if parado > 20 and not avisou:
                        self.log(f"Sem dados do {self.conn.name} há {int(parado)}s — simulador pausado, no menu ou fechado?")
                        avisou = True
                    if self.clock() - last_beat > HEARTBEAT_S:
                        last_beat = self.clock()
                        self.heartbeat()
                    time.sleep(poll_s)
                    continue
                if avisou:
                    self.log("Telemetria voltou.")
                    avisou = False
                idle_since = last_beat = self.clock()
                if self.step(tel):
                    return self.finish()
        except EOFError:
            self.flush(force=True)
            self.log("Replay terminou sem calços: sessão mantida aberta.")
            return None
        finally:
            self.conn.close()
