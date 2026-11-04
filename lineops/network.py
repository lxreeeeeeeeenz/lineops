"""Rede: os outros pilotos da companhia na IVAO, ao vivo e ao longo do tempo.

Fonte única: o whazzup público da IVAO (o mesmo dado do Webeye). Ele traz callsign, VID, plano de voo e a última posição;
não traz nome nem foto, então o LineOps mostra o VID e o apelido que você der a quem marcar como colega.

* Ao vivo: cada voo da companhia com rota, fase, nível, velocidade, progresso e a relação com a sua escala
  (mesmo destino, mesmo aeroporto, sua próxima perna, sua base).
* Ao longo do tempo: a cada 5 min o servidor anota quem estava online (ivao_sighting). Daí saem os colegas que você
  mais encontra, o histórico de cada VID e quem estava na rede junto com você. Guarda 90 dias.
* Derivados, não medidos: progresso (distância à origem ÷ soma das distâncias) e ETA (distância ao destino ÷ velocidade
  no solo) são estimativas e aparecem como tal.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import ivao, rules
from .db import audit, now_iso, one, rows, tx
from .errors import FGVError, NotFound

SAMPLE_EVERY_S = 300
KEEP_DAYS = 90
FMT = "%Y-%m-%dT%H:%M:%SZ"
# Ratings de piloto da IVAO (campo rating do whazzup)
RATING = {2: "FS1", 3: "FS2", 4: "FS3", 5: "PP", 6: "SPP", 7: "CP", 8: "ATP", 9: "SFI", 10: "CFI"}
PHASE = {"BOARDING": "Embarque", "DEPARTING": "Saindo", "INITIAL CLIMB": "Subida", "EN ROUTE": "Cruzeiro", "APPROACH": "Aproximação",
         "LANDED": "Pousou", "ON BLOCKS": "No portão"}


def _iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc).strftime(FMT)
    except ValueError:
        return None


def _place(conn, icao):
    if not icao:
        return None
    a = rules.airport(conn, icao) or {}
    return {"icao": icao, "city": a.get("city") or a.get("name"), "lat": a.get("lat"), "lon": a.get("lon")}


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def pilot_card(conn, p: dict) -> dict:
    fp, t = p.get("flightPlan") or {}, p.get("lastTrack") or {}
    dd, ad, gs = _num(t.get("departureDistance")), _num(t.get("arrivalDistance")), _num(t.get("groundSpeed")) or 0
    prog = round(100 * dd / (dd + ad)) if dd is not None and ad is not None and (dd + ad) > 0 else None
    on_ground = t.get("onGround")
    eta = None
    if ad is not None and gs >= 80 and on_ground is False:
        eta = (datetime.now(timezone.utc) + timedelta(hours=ad / gs)).strftime(FMT)
    st = str(t.get("state") or "")
    try:
        vid = int(p.get("userId"))
    except (TypeError, ValueError):
        vid = None
    try:
        rating = int(p.get("rating"))
    except (TypeError, ValueError):
        rating = None
    cs = p.get("callsign")
    return {"vid": vid, "callsign": cs, "rating": rating, "rating_label": RATING.get(rating),
            "aircraft": fp.get("aircraftId"), "dep": _place(conn, fp.get("departureId")), "arr": _place(conn, fp.get("arrivalId")),
            "alternate": fp.get("alternativeId") or None, "level": fp.get("level"), "route": fp.get("route"),
            "lat": _num(t.get("latitude")), "lon": _num(t.get("longitude")), "alt_ft": _num(t.get("altitude")), "gs_kt": gs,
            "hdg": _num(t.get("heading")), "on_ground": on_ground, "state": st or None, "phase": PHASE.get(st.upper(), st or None),
            "arr_nm": round(ad) if ad is not None else None, "dep_nm": round(dd) if dd is not None else None,
            "progress": prog, "eta_utc": eta, "online_since": _iso(p.get("createdAt")),
            "simulator": (p.get("pilotSession") or {}).get("simulatorId"), "webeye": ivao.WEBEYE_URL.format(cs) if cs else None}


# ------------------------------------------------------------------ amostras ao longo do tempo
def record(conn, data: dict, airline: str, now: str | None = None) -> int:
    """Anota quem da companhia está online agora. Repetir no mesmo minuto não duplica (UNIQUE + upsert)."""
    now = now or now_iso()
    al = (airline or "GLO").upper()
    n = 0
    with tx(conn):
        for p in ivao.pilots(data):
            cs = str(p.get("callsign") or "").upper()
            if not cs.startswith(al):
                continue
            try:
                vid = int(p.get("userId"))
            except (TypeError, ValueError):
                continue
            fp, t = p.get("flightPlan") or {}, p.get("lastTrack") or {}
            dep, arr = (fp.get("departureId") or "").upper(), (fp.get("arrivalId") or "").upper()
            day = (_iso(p.get("createdAt")) or now)[:10]
            alt = _num(t.get("altitude"))
            air = 1 if t.get("onGround") is False else 0
            ad = _num(t.get("arrivalDistance"))
            conn.execute(
                """INSERT INTO ivao_sighting(vid, callsign, day, dep, arr, aircraft, first_seen, last_seen, samples, max_alt_ft, airborne, arrived, last_state)
                   VALUES (?,?,?,?,?,?,?,?,1,?,?,0,?)
                   ON CONFLICT(vid, callsign, day, dep, arr) DO UPDATE SET
                     last_seen=excluded.last_seen, samples=samples+1, aircraft=COALESCE(excluded.aircraft, aircraft),
                     max_alt_ft=MAX(COALESCE(max_alt_ft,0), COALESCE(excluded.max_alt_ft,0)),
                     airborne=MAX(airborne, excluded.airborne),
                     arrived=CASE WHEN airborne=1 AND ?=1 THEN 1 ELSE arrived END, last_state=excluded.last_state""",
                (vid, cs, day, dep, arr, fp.get("aircraftId"), now, now, alt, air, t.get("state"),
                 1 if (t.get("onGround") is True and ad is not None and ad < 15) else 0))
            n += 1
        cut = (datetime.strptime(now, FMT) - timedelta(days=KEEP_DAYS)).strftime(FMT)
        conn.execute("DELETE FROM ivao_sighting WHERE last_seen < ?", (cut,))
    return n


def maybe_record(conn, data: dict | None = None, now: str | None = None, opener=None) -> int:
    """Grava uma amostra se a última tiver mais de 5 min. Usado pelo laço do servidor e quando você abre a Rede."""
    now = now or now_iso()
    last = conn.execute("SELECT MAX(last_seen) FROM ivao_sighting").fetchone()[0]
    if last and (datetime.strptime(now, FMT) - datetime.strptime(last, FMT)).total_seconds() < SAMPLE_EVERY_S - 5:
        return 0
    data = data or ivao.fetch(60, opener)
    return record(conn, data, airline(conn), now)


def airline(conn) -> str:
    r = one(conn.execute("SELECT airline FROM roster_schedule ORDER BY id DESC LIMIT 1"))
    from .careers import code_of
    return code_of(conn) if conn.execute("SELECT 1 FROM setting WHERE key='career_code'").fetchone() else ((r or {}).get("airline") or "GLO")


# ------------------------------------------------------------------ colegas
def friends(conn) -> dict[int, dict]:
    return {r["vid"]: r for r in rows(conn.execute("SELECT * FROM ivao_friend ORDER BY COALESCE(nickname, vid)"))}


def add_friend(conn, vid, nickname=None, note=None, user_id=None) -> dict:
    try:
        vid = int(str(vid).strip())
    except (TypeError, ValueError):
        raise FGVError("VID inválido.", code="campo_invalido")
    if vid <= 0:
        raise FGVError("VID inválido.", code="campo_invalido")
    nick = (nickname or "").strip()[:40] or None
    with tx(conn):
        conn.execute("""INSERT INTO ivao_friend(vid, nickname, note, created_at) VALUES (?,?,?,?)
                        ON CONFLICT(vid) DO UPDATE SET nickname=excluded.nickname, note=excluded.note""",
                     (vid, nick, (note or "").strip()[:200] or None, now_iso()))
        audit(conn, user_id, "network.friend", "ivao_friend", vid, {"nickname": nick})
    return one(conn.execute("SELECT * FROM ivao_friend WHERE vid=?", (vid,)))


def remove_friend(conn, vid: int, user_id=None):
    with tx(conn):
        if not conn.execute("DELETE FROM ivao_friend WHERE vid=?", (vid,)).rowcount:
            raise NotFound("Esse VID não está nos seus colegas.")
        audit(conn, user_id, "network.unfriend", "ivao_friend", vid)


# ------------------------------------------------------------------ visão da rede
def _my_context(conn, now: str) -> dict:
    """O que da sua escala serve para ligar você aos outros: base, onde está a aeronave/onde você termina, próxima perna."""
    from .journey import schedule_params
    p, _s = schedule_params(conn, now[:10])
    base = p.get("base_resolved") or p.get("base")
    nxt = one(conn.execute("""SELECT flight_number, adep, ades, dep_utc FROM roster_leg WHERE removed_batch IS NULL
                              AND activity<>'SOBREAVISO' AND arr_utc >= ? ORDER BY dep_utc LIMIT 1""", (now,)))
    last = one(conn.execute("SELECT ades FROM logbook_entry WHERE reversed_at IS NULL ORDER BY COALESCE(in_utc, date) DESC LIMIT 1"))
    return {"base": base, "next": nxt, "at": (last or {}).get("ades") or (nxt or {}).get("adep") or base}


def _relations(card: dict, ctx: dict, me: dict | None) -> list[str]:
    rel = []
    dep, arr = (card["dep"] or {}).get("icao"), (card["arr"] or {}).get("icao")
    nxt = ctx.get("next") or {}
    if me and me.get("vid") != card["vid"]:
        md, ma = (me.get("dep") or {}).get("icao"), (me.get("arr") or {}).get("icao")
        if ma and arr == ma:
            rel.append(f"vai para {arr} como você")
        if md and dep == md and card["on_ground"]:
            rel.append(f"no solo em {dep} com você")
    if nxt and dep == nxt.get("adep") and arr == nxt.get("ades"):
        rel.append(f"voando a sua próxima perna ({nxt.get('flight_number') or dep + '→' + arr})")
    elif nxt and arr == nxt.get("adep"):
        rel.append(f"chegando em {arr}, de onde sai o seu próximo voo")
    if ctx.get("base") and ctx["base"] in (dep, arr) and not any(ctx["base"] in r for r in rel):
        rel.append(f"{'saindo da' if dep == ctx['base'] else 'indo para a'} sua base")
    return rel


def overview(conn, now: str | None = None, opener=None) -> dict:
    now = now or now_iso()
    data = ivao.fetch(ivao.PULSE_TTL, opener)
    try:
        maybe_record(conn, data, now)
    except Exception:                                     # a amostra é bônus: a tela ao vivo não depende dela
        pass
    al = airline(conn)
    vid = ivao.vid_of(conn)
    fr = friends(conn)
    ctx = _my_context(conn, now)
    cards = [pilot_card(conn, p) for p in ivao.pilots(data) if str(p.get("callsign") or "").upper().startswith(al)]
    me = None
    if vid:
        mp = ivao.find_pilot(data, vid)
        me = pilot_card(conn, mp) if mp else None
    stats_seen = {r["vid"]: r for r in rows(conn.execute(
        """SELECT vid, COUNT(DISTINCT day) days, COUNT(*) flights, MAX(last_seen) last FROM ivao_sighting
           WHERE last_seen >= ? GROUP BY vid""", ((datetime.strptime(now, FMT) - timedelta(days=30)).strftime(FMT),)))}
    for c in cards:
        c["is_me"] = bool(vid) and str(c["vid"]) == str(vid)
        c["friend"] = fr.get(c["vid"])
        c["relations"] = _relations(c, ctx, me)
        s = stats_seen.get(c["vid"])
        c["seen_30d"] = {"days": s["days"], "flights": s["flights"]} if s else None
    cards.sort(key=lambda c: (not c["is_me"], not c["friend"], not c["relations"], c["on_ground"] is not False, c["callsign"] or ""))
    online = {c["vid"] for c in cards}
    # colegas: online ou quando foram vistos pela última vez
    fl = []
    for v, f in fr.items():
        last = one(conn.execute("SELECT callsign, dep, arr, last_seen FROM ivao_sighting WHERE vid=? ORDER BY last_seen DESC LIMIT 1", (v,)))
        fl.append({**f, "online": v in online, "card": next((c for c in cards if c["vid"] == v), None), "last": last})
    fl.sort(key=lambda x: (x["online"], (x["last"] or {}).get("last_seen") or ""), reverse=True)
    # quem você mais encontra (30 dias), fora você e quem já é colega
    frequent = [dict(r) for r in rows(conn.execute(
        """SELECT vid, COUNT(DISTINCT day) days, COUNT(*) flights, MAX(last_seen) last,
                  (SELECT callsign FROM ivao_sighting s2 WHERE s2.vid=s.vid ORDER BY last_seen DESC LIMIT 1) callsign
           FROM ivao_sighting s WHERE last_seen >= ? GROUP BY vid ORDER BY days DESC, flights DESC LIMIT 12""",
        ((datetime.strptime(now, FMT) - timedelta(days=30)).strftime(FMT),)))
        if not (vid and str(r["vid"]) == str(vid))]
    for r in frequent:
        r["friend"] = r["vid"] in fr
        r["online"] = r["vid"] in online
    routes, airports = {}, {}
    for c in cards:
        d, a = (c["dep"] or {}).get("icao"), (c["arr"] or {}).get("icao")
        if d and a:
            routes[f"{d}→{a}"] = routes.get(f"{d}→{a}", 0) + 1
        for x in (d, a):
            if x:
                airports[x] = airports.get(x, 0) + 1
    today = now[:10]
    day_stats = one(conn.execute("""SELECT COUNT(DISTINCT vid) pilots, COUNT(*) flights, SUM(arrived) arrived FROM ivao_sighting
                                    WHERE day=?""", (today,)))
    info = ivao.cache_info()
    return {"airline": al, "updated_at": info["fetched_at"], "stale": bool(info["error"]), "error": info["error"],
            "vid": vid, "me": me, "context": ctx, "pilots": cards,
            "counts": {"online": len(cards), "airborne": sum(1 for c in cards if c["on_ground"] is False),
                       "ground": sum(1 for c in cards if c["on_ground"] is not False), "network_pilots": len(ivao.pilots(data))},
            "friends": fl, "frequent": frequent,
            "routes": sorted(routes.items(), key=lambda x: -x[1])[:6], "airports": sorted(airports.items(), key=lambda x: -x[1])[:8],
            "today": {"pilots": day_stats["pilots"] or 0, "flights": day_stats["flights"] or 0, "arrived": day_stats["arrived"] or 0},
            "map_airports": [[a["icao"], a["lat"], a["lon"]] for a in rules.airport_map(conn).values()
                             if a.get("lat") is not None and a["icao"][:2] in ("SB", "SD", "SI", "SJ", "SN", "SS", "SW")],
            "source": "whazzup público da IVAO (o mesmo dado do Webeye); amostras gravadas a cada 5 min no seu servidor"}


def pilot_history(conn, vid: int) -> dict:
    hist = rows(conn.execute("SELECT * FROM ivao_sighting WHERE vid=? ORDER BY last_seen DESC LIMIT 60", (vid,)))
    mine = ivao.vid_of(conn)
    together = []
    if mine and str(mine) != str(vid):
        together = rows(conn.execute(
            """SELECT o.callsign, o.dep, o.arr, o.day, m.callsign AS my_callsign, m.dep AS my_dep, m.arr AS my_arr
               FROM ivao_sighting o JOIN ivao_sighting m ON m.vid=? AND m.first_seen <= o.last_seen AND o.first_seen <= m.last_seen
               WHERE o.vid=? ORDER BY o.last_seen DESC LIMIT 20""", (int(mine), vid)))
    routes = {}
    for h in hist:
        if h["dep"] and h["arr"]:
            routes[f"{h['dep']}→{h['arr']}"] = routes.get(f"{h['dep']}→{h['arr']}", 0) + 1
    return {"vid": vid, "friend": friends(conn).get(vid), "history": hist, "together": together,
            "routes": sorted(routes.items(), key=lambda x: -x[1])[:8],
            "days": len({h["day"] for h in hist}), "flights": len(hist), "arrived": sum(h["arrived"] for h in hist)}
