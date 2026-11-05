"""Pernoite pedido no gerador e o aviso de pernoite no Discord.

1) PERNOITE PEDIDO ("quero dormir em FOR na noite de 12/11"). O gerador do AeroScale já sabe terminar um dia num
   aeroporto obrigatório (Planner.plan_day(must_end=...): +10 000 pontos para a sequência que termina lá, −10 000 para a
   que não termina). A LineOps usa exatamente isso, sem mexer na cópia do motor: uma subclasse do Planner passa o
   aeroporto pedido como must_end no dia da noite pedida. Os voos continuam sendo só voos reais do RPL e todas as regras
   (jornada, repouso, conexão, limites) valem. Para o pernoite fazer sentido, o dia da noite e o seguinte precisam ser de
   trabalho: se o motor tinha posto uma folga regulamentar ali, ela é trocada com um dia de trabalho próximo, sem passar
   do máximo de dias seguidos. Folga pedida, folga fixa do dia da semana, indisponibilidade e reserva não mudam: o
   pedido fica como não atendido e o diagnóstico diz por quê. Nunca se inventa voo para cumprir o pedido.

2) AVISO NO DISCORD (para todos os usuários desde a 3.0.1; antes, só o cmdt079). A caixa "ESSE VOO É PERNOITE" no dia da escala manda para uma webhook do
   Discord: voo, trecho, data, chegada (hora local), cidade do pernoite, próxima apresentação e hotel (o que você
   cadastrou em Ajustes › Pernoites; sem cadastro, a mensagem diz que falta). Desmarcar apaga a mensagem.
"""
from __future__ import annotations

import json
import pathlib
import threading
import logging
import re
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

from . import __version__, journey
from .db import audit, now_iso, one, rows, tx
from .errors import Conflict, FGVError, NotFound

log = logging.getLogger("lineops.pernoite")
MAX_REQUESTS = 8
FIXED_OFF = ("indisponível", "folga pedida", "folga fixa do dia da semana")


# ================================================================== 1) pernoite pedido no gerador
def parse_requests(conn, raw, start: str, end: str, requested_off=()) -> list[dict]:
    """[{"date": "2026-11-12", "airport": "FOR"}] → [{"night": "2026-11-12", "icao": "SBFZ"}], conferido."""
    from .troca import resolve_airport
    if raw in (None, "", []):
        return []
    if not isinstance(raw, list) or len(raw) > MAX_REQUESTS:
        raise FGVError(f"Pernoites pedidos: até {MAX_REQUESTS}.", code="campo_invalido")
    out, seen = [], set()
    for r in raw:
        if not isinstance(r, dict):
            raise FGVError("Pernoite pedido inválido.", code="campo_invalido")
        d, code = str(r.get("date") or r.get("night") or "")[:10], str(r.get("airport") or r.get("icao") or "").strip()
        try:
            date.fromisoformat(d)
        except ValueError:
            raise FGVError("Pernoite pedido: data inválida.", code="campo_invalido")
        icao = resolve_airport(conn, code)
        if not icao:
            raise FGVError(f"Pernoite pedido: aeroporto {code or '(vazio)'} não encontrado (use ICAO, ex. SBFZ, ou IATA, ex. FOR).",
                           code="campo_invalido")
        if not (start <= d <= end):
            raise FGVError(f"Pernoite pedido em {d[8:10]}/{d[5:7]}: fora do período da escala ({start[8:10]}/{start[5:7]} a "
                           f"{end[8:10]}/{end[5:7]}).", code="campo_invalido")
        if d in set(requested_off or ()):
            raise FGVError(f"Pernoite pedido em {d[8:10]}/{d[5:7]}: esse dia está como folga pedida.", code="campo_invalido")
        if d in seen:
            raise FGVError(f"Dois pernoites pedidos na mesma noite ({d[8:10]}/{d[5:7]}).", code="campo_invalido")
        seen.add(d)
        out.append({"night": d, "icao": icao})
    return sorted(out, key=lambda x: x["night"])


def sleep_at(legs: list[dict], night: date, start_loc: str | None) -> tuple[str | None, dict | None]:
    """Onde o tripulante dorme na noite que começa em `night`: o destino da última perna da jornada daquele dia.
    legs: {"ld": date local da partida, "hour": hora local da partida, "dep"/"arr": datetime UTC, "adep", "ades"}.
    Perna que sai antes das 05:00 do dia seguinte, menos de 10 h depois da anterior, ainda é da mesma jornada (é a mesma
    regra do dia da jornada do exportador do AeroScale)."""
    loc, last = start_loc, None
    for l in sorted(legs, key=lambda x: x["dep"]):
        if l["ld"] <= night:
            loc, last = l["ades"], l
            continue
        if l["ld"] == night + timedelta(days=1) and last is not None and l["hour"] < 5 \
                and (l["dep"] - last["arr"]).total_seconds() < 10 * 3600:
            loc, last = l["ades"], l
            continue
        break
    return loc, last


def results(diagnostics: list[dict]) -> dict:
    ok = [d for d in diagnostics or [] if d.get("code") == "pernoite_pedido_ok"]
    bad = [d for d in diagnostics or [] if d.get("code") == "pernoite_pedido_falhou"]
    return {"ok": len(ok), "failed": len(bad)}


# ================================================================== 2) aviso de pernoite no Discord
# 3.0.1: a caixa "ESSE VOO É PERNOITE" vale para todo usuário (antes era só do cmdt079)
WEBHOOK_KEY = "discord_pernoite_webhook"
# a webhook padrão (pedido do dono da instalação): vale enquanto você não cadastrar outra em Ajustes › Discord
DEFAULT_WEBHOOK = ("https://discord.com/api/webhooks/1567661679636906056/"
                   "KJXzu2hPigGFcINK8Vb4f_Y1iM0h1ePFhL1okQw_9Qi_dXo5Dx9-VFJdVH99RW02RJhG")
WEBHOOK_RE = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/(\d{5,25})/([A-Za-z0-9_\-]{20,120})/?$")
UA = f"LineOps/{__version__} (virtual airline pessoal, self-hosted)"


def enabled_for(user: dict | None) -> bool:
    return bool(user and user.get("id") is not None)


def _require(user):
    if not enabled_for(user):
        raise FGVError("Recurso não disponível para este usuário.", code="proibido", status=403)


def get_webhook(core) -> str | None:
    r = one(core.execute("SELECT value FROM setting WHERE key=?", (WEBHOOK_KEY,)))
    return r["value"] if r else DEFAULT_WEBHOOK


def webhook_info(core, user) -> dict:
    _require(user)
    w = get_webhook(core)
    m = WEBHOOK_RE.match(w or "")
    return {"configured": bool(m), "masked": f"…/webhooks/{m.group(1)}/••••" if m else None, "default": w == DEFAULT_WEBHOOK}


def set_webhook(core, user, url: str | None) -> dict:
    _require(user)
    url = (url or "").strip()
    with tx(core):
        if not url:
            core.execute("DELETE FROM setting WHERE key=?", (WEBHOOK_KEY,))
        else:
            if not WEBHOOK_RE.match(url):
                raise FGVError("Isso não parece uma webhook do Discord (https://discord.com/api/webhooks/ID/TOKEN).",
                               code="campo_invalido")
            core.execute("INSERT INTO setting(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (WEBHOOK_KEY, url.rstrip("/")))
    return webhook_info(core, user)


def _http(method: str, url: str, body: dict | None = None, opener=None) -> tuple[int, bytes]:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    op = opener or urllib.request.urlopen
    try:
        with op(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except (urllib.error.URLError, OSError) as e:
        log.warning("discord: %s", e)
        return 0, b""


def _leg(conn, leg_id: int) -> dict:
    l = one(conn.execute("SELECT * FROM roster_leg WHERE id=? AND removed_batch IS NULL", (leg_id,)))
    if not l:
        raise NotFound("Perna não encontrada na escala atual.")
    if l["activity"] == "SOBREAVISO":
        raise Conflict("Sobreaviso não é voo.", code="nao_e_voo")
    return l


def describe(conn, leg: dict, user: dict | None = None) -> dict:
    """O que vai na mensagem: só dado da escala, do catálogo de aeroportos do AeroScale e do seu cadastro de hotel."""
    from . import enroute
    arr_l = journey.local_time(conn, leg["arr_utc"], leg["ades"])
    dep_l = journey.local_time(conn, leg["dep_utc"], leg["adep"])
    place = journey.place(conn, leg["ades"])
    p, _s = journey.schedule_params(conn, leg["local_date"] or leg["dep_utc"][:10])
    nxt = journey._next_leg(conn, leg["arr_utc"])
    nd = journey._duty_ref(conn, p, nxt) if nxt and nxt["id"] != leg["id"] else None
    gap = journey._gap(conn, leg, nxt) if nxt else None
    hotel = (enroute.airport_info(conn, leg["ades"]) or {}).get("hotel")
    base = p.get("base_resolved") or p.get("base")
    return {"leg_id": leg["id"], "flight_number": leg["flight_number"] or "posicionamento", "adep": leg["adep"], "ades": leg["ades"],
            "adep_iata": journey.place(conn, leg["adep"]).get("iata"), "ades_iata": place.get("iata"),
            "date": leg["local_date"] or leg["dep_utc"][:10], "dep_local": (dep_l or {}).get("hhmm"), "arr_local": (arr_l or {}).get("hhmm"),
            "arr_date": (arr_l or {}).get("date"), "arr_utc": leg["arr_utc"], "city": place.get("city") or place.get("name") or leg["ades"],
            "uf": place.get("uf"), "hotel": hotel, "at_base": bool(base) and leg["ades"] == base,
            "detected": bool(gap and gap["overnight"]), "rest_min": gap["minutes"] if gap and gap["same_airport"] else None,
            "next": {"flight_number": nd["flight_number"], "report_local": (nd["report_local"] or {}).get("hhmm"),
                     "report_date": (nd["report_local"] or {}).get("date") or nd["date"], "dep_local": (nd["dep_local"] or {}).get("hhmm"),
                     "adep": nd["at"]["icao"], "ades": nd["to"]["icao"], "report_utc": nd["report_utc"]} if nd else None,
            "pilot": (user or {}).get("display_name") or (user or {}).get("username"), "pilot_code": (user or {}).get("pilot_code"),
            "airline": leg["airline"], "type_icao": leg["type_icao"]}


def _dm(s: str | None) -> str:
    return f"{s[8:10]}/{s[5:7]}" if s and len(s) >= 10 else (s or "—")


def message(info: dict, color: int | None = None) -> dict:
    """Payload da webhook (um embed)."""
    hm = lambda s: s or "—"                                                              # noqa: E731
    ap = lambda icao, iata: f"{iata} ({icao})" if iata else icao                         # noqa: E731
    nx = info.get("next")
    fields = [
        {"name": "Voo", "value": f"{info['flight_number']}" + (f" · {info['type_icao']}" if info.get("type_icao") else ""), "inline": True},
        {"name": "Trecho", "value": f"{ap(info['adep'], info.get('adep_iata'))} → {ap(info['ades'], info.get('ades_iata'))}", "inline": True},
        {"name": "Data", "value": _dm(info["date"]) + f"/{info['date'][:4]}", "inline": True},
        {"name": "Chegada", "value": (f"{hm(info['arr_local'])} local" if info.get("arr_local") else "—")
         + (f" ({_dm(info['arr_date'])})" if info.get("arr_date") and info["arr_date"] != info["date"] else "")
         + f" · {info['arr_utc'][11:16]}Z", "inline": True},
        {"name": "Pernoite", "value": info["city"] + (f"/{info['uf']}" if info.get("uf") else "")
         + (f" · {info['rest_min'] // 60} h {info['rest_min'] % 60:02d} no solo" if info.get("rest_min") and info.get("detected")
            else " · marcado pelo piloto" if not info.get("detected") else ""), "inline": True},
        {"name": "Próxima apresentação", "value": (f"{_dm(nx['report_date'])} {hm(nx['report_local'])} local · {nx['flight_number'] or 'voo'} "
                                                   f"{nx['adep']}→{nx['ades']}" if nx and nx.get("report_local") else
                                                   (f"{nx['flight_number'] or 'voo'} {nx['adep']}→{nx['ades']}" if nx else "sem próximo voo na escala")),
         "inline": False},
        {"name": "Hotel", "value": info.get("hotel") or "não cadastrado (Ajustes › Pernoites)", "inline": False},
    ]
    return {"username": "LineOps · Pernoites", "allowed_mentions": {"parse": []},
            "embeds": [{"title": f"Pernoite em {info['city']}", "color": color if color is not None else 0xFF6A13, "fields": fields,
                        "footer": {"text": f"{info.get('pilot') or ''}{' · ' + info['pilot_code'] if info.get('pilot_code') else ''} · "
                                           "LineOps (escala de simulação)"}}]}


def status_for(conn, leg_ids) -> dict:
    ids = [int(i) for i in leg_ids]
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    out = {}
    for r in rows(conn.execute(f"SELECT p.* FROM overnight_post p JOIN roster_leg l ON l.uid=p.leg_uid WHERE l.id IN ({q})", ids)):
        out[str(r["leg_id"])] = {"state": r["state"], "error": r["error"], "updated_at": r["updated_at"]}
    return out


def apply_marks(conn, d: dict, posts: dict) -> dict:
    """O voo que você marcou como pernoite manda no dia: o pernoite (e o hotel, o carro e o resumo) passa a ser no destino
    dele, mesmo que a escala não detecte pernoite ali (fim da escala, solo curto, voo do meio do dia)."""
    from . import journey
    sent = [l for l in d.get("legs") or [] if (posts.get(str(l["id"])) or {}).get("state") == "ENVIADO"]
    if not sent:
        return d
    leg = sent[-1]
    ov = d.get("overnight")
    if ov and ov["at"] and ov["at"].get("icao") == leg["ades"]:
        ov["marked"] = True
        return d
    base = (d.get("base") or {}).get("icao")
    nd = d.get("next_duty") or {}
    d["overnight"] = {"at": journey.place(conn, leg["ades"]), "from_utc": leg["arr_utc"],
                      "until_utc": nd.get("report_utc"), "minutes": None, "kind": "marcado", "label": "Pernoite",
                      "detail": "marcado por você (ESSE VOO É PERNOITE)", "at_base": bool(base) and leg["ades"] == base,
                      "marked": True, "detected": ov, "source": "você marcou este voo como pernoite"}
    return d


def mark(conn, core, user, leg_id: int, *, color: int | None = None, opener=None, now: str | None = None) -> dict:
    """Marca a perna como pernoite e manda a mensagem para a webhook. Marcar de novo reenvia (edita a mensagem)."""
    _require(user)
    url = get_webhook(core)
    if not url or not WEBHOOK_RE.match(url):
        raise Conflict("Cadastre a webhook do Discord em Ajustes › Discord antes de marcar.", code="sem_webhook")
    leg = _leg(conn, leg_id)
    now = now or now_iso()
    info = describe(conn, leg, user)
    body = message(info, color)
    cur = one(conn.execute("SELECT * FROM overnight_post WHERE leg_uid=?", (leg["uid"],)))
    msg_id = cur["message_id"] if cur and cur["state"] == "ENVIADO" else None
    if msg_id:
        code, raw = _http("PATCH", f"{url}/messages/{msg_id}", body, opener)
        if code == 404:                                  # apagaram no Discord: manda de novo
            msg_id = None
    if not msg_id:
        code, raw = _http("POST", f"{url}?wait=true", body, opener)
    ok = 200 <= code < 300
    if ok:
        try:
            msg_id = json.loads(raw.decode() or "{}").get("id") or msg_id
        except ValueError:
            pass
    err = None if ok else _discord_error(code, raw)
    with tx(conn):
        conn.execute("""INSERT INTO overnight_post(leg_uid, leg_id, flight_number, adep, ades, arr_utc, message_id, state, error, payload,
                        user_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(leg_uid) DO UPDATE SET leg_id=excluded.leg_id, message_id=excluded.message_id, state=excluded.state,
                        error=excluded.error, payload=excluded.payload, updated_at=excluded.updated_at""",
                     (leg["uid"], leg["id"], leg["flight_number"], leg["adep"], leg["ades"], leg["arr_utc"], msg_id,
                      "ENVIADO" if ok else "ERRO", err, json.dumps(body, ensure_ascii=False), user.get("id"), now, now))
        audit(conn, user.get("id"), "pernoite.discord", "roster_leg", leg["id"], {"ok": ok, "code": code})
    if not ok:
        raise FGVError(f"O Discord recusou a mensagem: {err}", code="discord_erro", status=502)
    return {"ok": True, "state": "ENVIADO", "leg_id": leg["id"], "detected": info["detected"], "info": info}


def unmark(conn, core, user, leg_id: int, *, opener=None, now: str | None = None) -> dict:
    _require(user)
    leg = one(conn.execute("SELECT * FROM roster_leg WHERE id=?", (leg_id,)))
    if not leg:
        raise NotFound("Perna não encontrada.")
    cur = one(conn.execute("SELECT * FROM overnight_post WHERE leg_uid=?", (leg["uid"],)))
    if not cur or cur["state"] == "REMOVIDO":
        return {"ok": True, "state": None}
    url = get_webhook(core)
    note = None
    if cur["message_id"] and url and WEBHOOK_RE.match(url):
        code, raw = _http("DELETE", f"{url}/messages/{cur['message_id']}", None, opener)
        if not (200 <= code < 300 or code == 404):
            note = f"não consegui apagar a mensagem no Discord ({_discord_error(code, raw)}); apague por lá"
    with tx(conn):
        conn.execute("UPDATE overnight_post SET state='REMOVIDO', error=?, updated_at=? WHERE id=?", (note, now or now_iso(), cur["id"]))
    return {"ok": True, "state": None, "note": note}


def _discord_error(code: int, raw: bytes) -> str:
    try:
        m = json.loads(raw.decode() or "{}").get("message")
    except ValueError:
        m = None
    if code == 0:
        return "sem conexão"
    return f"HTTP {code}" + (f": {m}" if m else "")


# ------------------------------------------------------------------ a escala mudou: os avisos seguem a escala (3.0.2b7)
AUTO_KEY = "pernoite_auto"             # "1": todo pernoite fora da base detectado na escala é enviado sozinho
SYNC_DAYS = 62


def auto_enabled(core) -> bool:
    r = one(core.execute("SELECT value FROM setting WHERE key=?", (AUTO_KEY,)))
    return bool(r and r["value"] == "1")


def set_auto(core, on: bool) -> bool:
    with tx(core):
        core.execute("INSERT INTO setting(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (AUTO_KEY, "1" if on else "0"))
    return on


def _payload_key(body: dict) -> str:
    """O que, mudando, exige editar a mensagem no Discord (campos do embed, sem a cor)."""
    e = (body.get("embeds") or [{}])[0]
    return json.dumps({"t": e.get("title"), "f": e.get("fields")}, ensure_ascii=False, sort_keys=True)


def detected_overnights(conn, start: str, days: int = SYNC_DAYS) -> list[dict]:
    """A última perna de cada dia que termina em pernoite fora da base (a mesma regra do dia da escala)."""
    from datetime import date, timedelta
    from . import escala
    d0 = date.fromisoformat(start[:10])
    legs = escala.legs(conn, d0.isoformat(), (d0 + timedelta(days=days)).isoformat(), flights_only=True)
    out = []
    for a, b in zip(legs, legs[1:]):
        if a.get("activity") == "EXTRA" or not a.get("flight_number"):
            continue
        g = journey._gap(conn, a, b)
        if not g["overnight"]:
            continue
        p, _s = journey.schedule_params(conn, a.get("local_date") or a["dep_utc"][:10])
        base = p.get("base_resolved") or p.get("base")
        if base and a["ades"] == base:
            continue
        out.append(a)
    return out


def sync(conn, core, user, *, opener=None, now: str | None = None, auto: bool | None = None) -> dict:
    """Depois de qualquer mudança na escala: edita no Discord os avisos cujo voo mudou (horário, próxima apresentação,
    hotel), apaga os de voos que saíram da escala e, com o envio automático ligado, manda os pernoites novos."""
    _require(user)
    now = now or now_iso()
    url = get_webhook(core)
    out = {"edited": 0, "removed": 0, "sent": 0, "errors": 0, "unchanged": 0}
    if not (url and WEBHOOK_RE.match(url)):
        return {**out, "note": "sem webhook"}
    for post in rows(conn.execute("SELECT * FROM overnight_post WHERE state='ENVIADO'")):
        leg = one(conn.execute("SELECT * FROM roster_leg WHERE uid=?", (post["leg_uid"],)))
        if not leg or leg["removed_batch"] or leg["activity"] == "SOBREAVISO":
            note = None
            if post["message_id"]:
                code, raw = _http("DELETE", f"{url}/messages/{post['message_id']}", None, opener)
                if not (200 <= code < 300 or code == 404):
                    note = f"não consegui apagar no Discord ({_discord_error(code, raw)})"
            with tx(conn):
                conn.execute("UPDATE overnight_post SET state='REMOVIDO', error=?, updated_at=? WHERE id=?",
                             (note or "o voo saiu da escala", now, post["id"]))
            out["removed"] += 1
            continue
        try:
            body = message(describe(conn, leg, user))
        except Exception:                                    # noqa: BLE001 — perna estranha: não derruba os outros
            log.exception("pernoite sync: perna %s", post["leg_uid"])
            out["errors"] += 1
            continue
        old = json.loads(post["payload"] or "{}")
        if _payload_key(old) == _payload_key(body) and post["leg_id"] == leg["id"]:
            out["unchanged"] += 1
            continue
        try:
            mark(conn, core, user, leg["id"], opener=opener, now=now)
            out["edited"] += 1
        except FGVError:
            out["errors"] += 1
    if auto if auto is not None else auto_enabled(core):
        posted = {r["leg_uid"] for r in conn.execute("SELECT leg_uid FROM overnight_post WHERE state='ENVIADO'")}
        for leg in detected_overnights(conn, now):
            if leg["uid"] in posted:
                continue
            try:
                mark(conn, core, user, leg["id"], opener=opener, now=now)
                out["sent"] += 1
            except FGVError:
                out["errors"] += 1
    return out


_SYNC_LOCK = threading.Lock()
_LAST_SYNC: dict = {}


def sync_later(core_path, career_path, user_id, *, min_interval: int = 0) -> bool:
    """Roda o sync em segundo plano (nunca atrasa a resposta). min_interval: segundos desde o último sync desta carreira."""
    import time
    key = str(career_path)
    with _SYNC_LOCK:
        if min_interval and time.time() - _LAST_SYNC.get(key, 0) < min_interval:
            return False
        _LAST_SYNC[key] = time.time()

    def run():
        from .db import connect
        from . import careers
        try:
            core = connect(core_path)
            try:
                u = one(core.execute("SELECT * FROM user WHERE id=?", (user_id,)))
                if not u:
                    return
                u = dict(u)
                code = pathlib.Path(career_path).stem.upper()
                u["career"] = code
                u["pilot_code"] = careers.pilot_code(u.get("pilot_code"), code)
                cc = connect(career_path)
                try:
                    r = sync(cc, core, u)
                    if r.get("edited") or r.get("removed") or r.get("sent") or r.get("errors"):
                        log.info("pernoite sync %s: %s", code, r)
                finally:
                    cc.close()
            finally:
                core.close()
        except Exception:                                    # noqa: BLE001
            log.exception("pernoite sync")
    threading.Thread(target=run, name="pernoite-sync", daemon=True).start()
    return True
