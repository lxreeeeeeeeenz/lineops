"""Pernoite imersivo: o hotel, o carro e o despertar, cada um na sua hora.

* Hotel: você escolhe um por aeroporto entre os hotéis que o OpenStreetMap conhece perto dele (Overpass API, sem chave).
  O LineOps diz "hotel escolhido por você": não sabemos (nem fingimos saber) qual hotel a GOL usa.
* Rota: tempo e distância de carro aeroporto → hotel pelo OSRM (servidor público da FOSSGIS sobre o OpenStreetMap),
  calculados uma vez quando você escolhe o hotel e guardados. Sem rota, o tempo é estimado e a mensagem diz isso.
* Linha do tempo do pernoite:
    fim da jornada = calço + 30 min (Lei 13.475, art. 35 §4) → carro no desembarque → check-in (rota + 10 min)
    saída           = apresentação − rota − folga (padrão 15 min) → despertar = saída − antecedência (padrão 60 min)
  Folga e antecedência são parâmetros seus (Ajustes), não regra da lei.
* As mensagens (carro no desembarque, check-in, despertar, carro na portaria) ficam agendadas e o motor da equipe entrega
  na hora certa, com aviso na bandeja do Windows. Se a escala mudar ou o voo for estornado, as pendentes são canceladas.
* Motorista e placa são fictícios, como os nomes da equipe. Transporte: "van da tripulação" (padrão; a lei manda a empresa
  fornecer o transporte, art. 47) ou visual de app de corrida, à sua escolha.

Uso das APIs públicas (sem abuso): Overpass só quando você clica em buscar; OSRM uma vez por hotel escolhido. As duas
pedem User-Agent identificado e a atribuição do OpenStreetMap, que aparece na tela.
"""
from __future__ import annotations

import json
import logging
import os
import random
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import __version__, geo, journey
from .db import audit, jloads, now_iso, one, rows, tx
from .errors import FGVError

log = logging.getLogger("lineops.stay")
FMT = "%Y-%m-%dT%H:%M:%SZ"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OSRM_URLS = ("https://routing.openstreetmap.de/routed-car/route/v1/driving/{a};{b}?overview=false",
             "https://router.project-osrm.org/route/v1/driving/{a};{b}?overview=false")
UA = f"LineOps/{__version__} (virtual airline pessoal, self-hosted)"
ATTRIBUTION = "Hotéis: © colaboradores do OpenStreetMap (ODbL). Rotas: OSRM sobre o OpenStreetMap."
CHECKIN_MIN = 10
RELEASE_AFTER_MIN = 30
STALE_H = 3                       # mensagem que passou da hora há mais de 3 h não é entregue (vira VENCIDO)
DEFAULTS = {"stay_transport": "van", "stay_buffer_min": 15, "stay_wakeup_min": 60}
OK_STATES = ("EM_VOO", "PIREP_RECEBIDO", "APROVADO")

DRIVERS = ("Sérgio", "Marcos", "Cláudia", "Wellington", "Rosana", "Edson", "Josué", "Luciene", "Adriano", "Valdir", "Simone", "Gilberto")
VANS = ("Renault Master branca", "Mercedes-Benz Sprinter prata", "Fiat Ducato branca", "Peugeot Boxer branca")
CARS = ("Chevrolet Onix prata", "Hyundai HB20 branco", "Toyota Corolla preto", "Fiat Cronos cinza", "Volkswagen Virtus prata", "Nissan Versa branco")


def _p(s):
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc) if s else None


def _iso(d):
    return d.strftime(FMT) if d else None


def _hm(m) -> str:
    m = int(round(m))
    return f"{m // 60}h{m % 60:02d}"


# ------------------------------------------------------------------ ajustes
def settings(conn) -> dict:
    cfg = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM setting WHERE key IN ('stay_transport','stay_buffer_min','stay_wakeup_min')")}
    out = dict(DEFAULTS)
    if cfg.get("stay_transport") in ("van", "app"):
        out["stay_transport"] = cfg["stay_transport"]
    for k, lo, hi in (("stay_buffer_min", 0, 90), ("stay_wakeup_min", 15, 180)):
        try:
            v = int(cfg.get(k) or "")
            if lo <= v <= hi:
                out[k] = v
        except ValueError:
            pass
    return out


def validate_setting(key, value):
    if key == "stay_transport" and value not in ("", "van", "app"):
        raise FGVError("Transporte: 'van' ou 'app'.", code="campo_invalido")
    if key in ("stay_buffer_min", "stay_wakeup_min") and value not in (None, ""):
        lo, hi = (0, 90) if key == "stay_buffer_min" else (15, 180)
        try:
            v = int(value)
        except (TypeError, ValueError):
            raise FGVError("Informe minutos inteiros.", code="campo_invalido")
        if not lo <= v <= hi:
            raise FGVError(f"Fora da faixa ({lo} a {hi} min).", code="campo_invalido")


# ------------------------------------------------------------------ rede (Overpass e OSRM)
def _http(url, data=None, opener=None) -> bytes:
    if opener:
        return opener(url, data)
    if os.environ.get("LINEOPS_OFFLINE") == "1":
        raise FGVError("Sem internet neste modo.", code="offline", status=503)
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def search_hotels(conn, icao: str, radius_km: float = 15, opener=None) -> dict:
    """Hotéis e motéis que o OpenStreetMap conhece num raio do aeroporto, do mais perto para o mais longe."""
    icao = (icao or "").upper()
    pl = journey.place(conn, icao)
    if pl["lat"] is None:
        raise FGVError(f"Sem coordenadas de {icao} na base do AeroScale.", code="sem_coordenadas")
    r = int(max(2, min(40, float(radius_km))) * 1000)
    q = (f'[out:json][timeout:25];(node["tourism"~"^(hotel|motel)$"](around:{r},{pl["lat"]},{pl["lon"]});'
         f'way["tourism"~"^(hotel|motel)$"](around:{r},{pl["lat"]},{pl["lon"]}););out center tags 80;')
    try:
        raw = _http(OVERPASS_URL, urllib.parse.urlencode({"data": q}).encode(), opener)
        data = json.loads(raw.decode("utf-8"))
    except FGVError:
        raise
    except Exception as e:
        raise FGVError(f"O OpenStreetMap (Overpass) não respondeu agora ({e}). Tente de novo em instantes.", code="osm_indisponivel", status=503)
    out = []
    for el in data.get("elements") or []:
        t = el.get("tags") or {}
        name = (t.get("name") or "").strip()
        lat = el.get("lat", (el.get("center") or {}).get("lat"))
        lon = el.get("lon", (el.get("center") or {}).get("lon"))
        if not name or lat is None or lon is None:
            continue
        street = " ".join(x for x in (t.get("addr:street"), t.get("addr:housenumber")) if x)
        addr = ", ".join(x for x in (street, t.get("addr:suburb"), t.get("addr:city")) if x) or None
        out.append({"name": name[:120], "address": addr, "lat": float(lat), "lon": float(lon), "ref": f"{el.get('type')}/{el.get('id')}",
                    "kind": t.get("tourism"), "stars": t.get("stars"), "website": t.get("website") or t.get("contact:website"),
                    "km": round(geo.nm(pl["lat"], pl["lon"], float(lat), float(lon)) * 1.852, 1)})
    out.sort(key=lambda x: x["km"])
    return {"icao": icao, "airport": pl, "radius_km": r // 1000, "hotels": out[:30], "attribution": ATTRIBUTION,
            "note": "Distância em linha reta; o tempo de carro é calculado pelo OSRM quando você escolhe."}


def route(lat1, lon1, lat2, lon2, opener=None) -> dict:
    """Rota de carro pelo OSRM (servidor público da FOSSGIS; o demo do projeto OSRM como reserva)."""
    a, b = f"{lon1:.6f},{lat1:.6f}", f"{lon2:.6f},{lat2:.6f}"
    last = None
    for url in OSRM_URLS:
        try:
            d = json.loads(_http(url.format(a=a, b=b), None, opener).decode("utf-8"))
            if d.get("code") == "Ok" and d.get("routes"):
                rt = d["routes"][0]
                return {"min": max(1, round(rt["duration"] / 60)), "km": round(rt["distance"] / 1000, 1),
                        "src": "OSRM · " + urllib.parse.urlparse(url).netloc}
            last = d.get("message") or d.get("code")
        except FGVError:
            raise
        except Exception as e:
            last = str(e)
    raise FGVError(f"O OSRM não calculou a rota agora ({last}).", code="rota_indisponivel", status=503)


def choose_hotel(conn, icao: str, hotel: dict, *, opener=None, user_id=None) -> dict:
    """Guarda o hotel escolhido e calcula a rota (fora de transação: chamada de rede)."""
    from . import enroute
    icao = (icao or "").upper()
    name = str(hotel.get("name") or "").strip()[:160]
    try:
        lat, lon = float(hotel.get("lat")), float(hotel.get("lon"))
    except (TypeError, ValueError):
        raise FGVError("Hotel sem coordenadas.", code="campo_invalido")
    if not name or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise FGVError("Hotel inválido.", code="campo_invalido")
    cur = enroute.airport_info(conn, icao) or {}
    enroute.save_airport_info(conn, icao, hotel=name, transport=cur.get("transport"), notes=cur.get("notes"),
                              source="OpenStreetMap · escolhido por você", user_id=user_id)
    with tx(conn):
        conn.execute("""UPDATE airport_info SET hotel_address=?, hotel_lat=?, hotel_lon=?, hotel_ref=?, route_min=NULL, route_km=NULL,
                        route_src=NULL, route_at=NULL WHERE icao=?""",
                     (str(hotel.get("address") or "")[:200] or None, lat, lon, str(hotel.get("ref") or "")[:40] or None, icao))
    warn = None
    try:
        refresh_route(conn, icao, opener=opener)
    except FGVError as e:
        warn = e.message + " O tempo de carro fica estimado até você recalcular."
    out = enroute.airport_info(conn, icao)
    out["warning"] = warn
    return out


def refresh_route(conn, icao: str, opener=None) -> dict:
    from . import enroute
    info = enroute.airport_info(conn, icao)
    if not info or info.get("hotel_lat") is None:
        raise FGVError("Escolha o hotel primeiro (busca no OpenStreetMap).", code="sem_hotel")
    pl = journey.place(conn, icao)
    if pl["lat"] is None:
        raise FGVError(f"Sem coordenadas de {icao}.", code="sem_coordenadas")
    rt = route(pl["lat"], pl["lon"], info["hotel_lat"], info["hotel_lon"], opener)
    with tx(conn):
        conn.execute("UPDATE airport_info SET route_min=?, route_km=?, route_src=?, route_at=? WHERE icao=?",
                     (rt["min"], rt["km"], rt["src"], now_iso(), icao.upper()))
        audit(conn, None, "airport_info.route", "airport_info", icao.upper(), rt)
    return enroute.airport_info(conn, icao)


# ------------------------------------------------------------------ linha do tempo
def drive_of(conn, icao) -> dict:
    from . import enroute
    info = enroute.airport_info(conn, icao) or {}
    if info.get("route_min") is not None:
        return {"min": int(info["route_min"]), "label": f"{info['route_min']} min de carro ({str(info['route_km']).replace('.', ',')} km, OSRM)", "estimated": False}
    pl = journey.place(conn, icao)
    if info.get("hotel_lat") is not None and pl["lat"] is not None:
        km = geo.nm(pl["lat"], pl["lon"], info["hotel_lat"], info["hotel_lon"]) * 1.852 * 1.3
        m = max(8, round(km / 40 * 60))
        return {"min": m, "label": f"~{m} min de carro (estimado pela distância; recalcule a rota em Ajustes › Pernoites)", "estimated": True}
    return {"min": 30, "label": "~30 min de carro (estimado: hotel ainda não escolhido)", "estimated": True}


def ride(seed: int, mode: str) -> dict:
    """Motorista, veículo e placa fictícios, sempre os mesmos para o mesmo voo."""
    rnd = random.Random(f"lineops-ride-{seed}")
    L = "ABCDEFGHJKLMNPRSTUVWXYZ"
    plate = "".join(rnd.choice(L) for _ in range(3)) + str(rnd.randint(0, 9)) + rnd.choice(L) + f"{rnd.randint(0, 99):02d}"   # padrão Mercosul
    return {"driver": rnd.choice(DRIVERS), "vehicle": rnd.choice(VANS if mode == "van" else CARS), "plate": plate, "mode": mode}


def ride_text(r) -> str:
    return f"{r['vehicle']} · placa {r['plate']} · motorista {r['driver']}"


def timeline(conn, f: dict, plan: dict, in_utc: str | None = None) -> dict | None:
    """Horários do pernoite. in_utc = calço real (se já houver); senão, a chegada estimada ou a programada."""
    if not plan or plan.get("kind") != "pernoite":
        return None
    cfg = settings(conn)
    if in_utc:
        rel = _iso(_p(in_utc) + timedelta(minutes=RELEASE_AFTER_MIN))
        basis = "pelo calço real"
    elif plan.get("real") and plan["real"].get("release_utc"):
        rel, basis = plan["real"]["release_utc"], "pela chegada estimada"
    else:
        rel, basis = plan.get("release_utc"), "pelo horário programado"
    if not rel:
        return None
    drv = drive_of(conn, f["ades"])
    checkin = _iso(_p(rel) + timedelta(minutes=drv["min"] + CHECKIN_MIN))
    rep = plan.get("next_report_utc")
    pickup = wake = None
    if rep:
        pickup = _iso(_p(rep) - timedelta(minutes=drv["min"] + cfg["stay_buffer_min"]))
        wake = _iso(_p(pickup) - timedelta(minutes=cfg["stay_wakeup_min"]))
    hotel_min = int((_p(pickup) - _p(checkin)).total_seconds() // 60) if pickup else None
    return {"release_utc": rel, "basis": basis, "car_utc": rel, "checkin_utc": checkin, "report_utc": rep, "pickup_utc": pickup,
            "wake_utc": wake, "hotel_min": hotel_min, "drive": drv, "short": hotel_min is not None and wake and _p(wake) <= _p(checkin),
            "transport": cfg["stay_transport"], "buffer_min": cfg["stay_buffer_min"], "wakeup_min": cfg["stay_wakeup_min"]}


def lines_for_plan(conn, f, plan, tl) -> list[str]:
    """Linhas que entram na mensagem de pernoite do Controle de Tripulantes."""
    from .enroute import _lt, airport_info
    ades = f["ades"]
    info = airport_info(conn, ades) or {}
    r = ride(f["id"], tl["transport"])
    out = []
    if info.get("hotel"):
        out.append(f"HOTEL: {info['hotel']}" + (f" · {info['hotel_address']}" if info.get("hotel_address") else "")
                   + (" (escolhido por você no OpenStreetMap)" if info.get("hotel_ref") else ""))
    else:
        out.append(f"HOTEL: não escolhido para {ades}. Escolha em Ajustes › Pernoites (busca no OpenStreetMap). Fora da base, a empresa "
                   "fornece quarto individual com banheiro privativo e o transporte (Lei 13.475, art. 47).")
    out.append(("TRANSPORTE: van da tripulação" if tl["transport"] == "van" else "TRANSPORTE: carro por aplicativo") + f" · {tl['drive']['label']}")
    if info.get("transport"):
        out.append(f"COMBINADO: {info['transport']}")
    out.append(f"CARRO NO DESEMBARQUE: {_lt(conn, tl['car_utc'], ades)} ({r['vehicle']}, motorista {r['driver']}) · check-in por volta de {_lt(conn, tl['checkin_utc'], ades)}")
    if tl["wake_utc"]:
        nd = plan.get("next_duty") or {}
        at = (nd.get("at") or {}).get("icao") or ades
        out.append(f"AMANHÃ: despertar {_lt(conn, tl['wake_utc'], ades, True)} · carro na portaria {_lt(conn, tl['pickup_utc'], ades)} · "
                   f"apresentação {_lt(conn, tl['report_utc'], at)}")
        out.append(f"TEMPO NO HOTEL: {_hm(tl['hotel_min'])}" + (" · CURTO DEMAIS: o despertar cai antes do check-in" if tl["short"] else ""))
    return out


# ------------------------------------------------------------------ agenda do pernoite
def schedule(conn, f: dict, plan: dict, now: str, in_utc: str | None = None) -> dict | None:
    """Agenda (ou reagenda) as mensagens do pernoite. Só mexe nas que ainda não foram entregues."""
    from .enroute import _lt, airport_info
    tl = timeline(conn, f, plan, in_utc)
    if not tl:
        return None
    ades, fid = f["ades"], f["id"]
    info = airport_info(conn, ades) or {}
    hotel = info.get("hotel") or "hotel (não escolhido)"
    r = ride(fid, tl["transport"])
    nd = plan.get("next_duty") or {}
    at = (nd.get("at") or {}).get("icao") or ades
    ref = json.dumps({"leg_id": nd.get("leg_id"), "dep_utc": nd.get("dep_utc")}) if nd else None
    fn = f.get("flight_number") or "Voo"
    nxt = f"{nd.get('flight_number') or '—'} {at}→{(nd.get('to') or {}).get('icao') or '?'} às {_lt(conn, nd.get('dep_utc'), at)}" if nd else "—"
    app = tl["transport"] == "app"
    ev = [
        (f"stay_car:{fid}", tl["car_utc"], "transporte",
         ("Seu carro chegou" if app else "Van da tripulação no desembarque") + f" · {ades}",
         (f"Seu carro chegou no desembarque de {ades}.\n\n" if app else f"Comandante, a van da tripulação está no desembarque de {ades}.\n\n")
         + f"VEÍCULO: {r['vehicle']} · placa {r['plate']}\nMOTORISTA: {r['driver']}\nDESTINO: {hotel}"
         + (f" · {info['hotel_address']}" if info.get("hotel_address") else "") + f"\nTRAJETO: {tl['drive']['label']}\n"
         + f"CHEGADA PREVISTA NO HOTEL: {_lt(conn, _iso(_p(tl['car_utc']) + timedelta(minutes=tl['drive']['min'])), ades)}\n\n(motorista e placa fictícios)",
         "info", None, None),
        (f"stay_checkin:{fid}", tl["checkin_utc"], "hotelaria", f"Check-in · {hotel}",
         f"Comandante, check-in feito no {hotel}. A reserva da tripulação está no seu nome.\n\n"
         + (f"DESPERTAR PROGRAMADO: {_lt(conn, tl['wake_utc'], ades, True)}\nCARRO NA PORTARIA: {_lt(conn, tl['pickup_utc'], ades)}\n"
            f"APRESENTAÇÃO: {_lt(conn, tl['report_utc'], at)} em {at} · {nxt}\nTEMPO NO HOTEL: {_hm(tl['hotel_min'])}\n" if tl["wake_utc"] else
            "Sem programação seguinte na escala publicada: o despertar fica para quando a escala sair.\n")
         + "\nBom descanso.", "info", None, None),
    ]
    if tl["wake_utc"] and not tl["short"]:
        ev += [
            (f"stay_wake:{fid}", tl["wake_utc"], "hotelaria", f"Despertar · apresentação {_lt(conn, tl['report_utc'], at)}",
             f"Bom dia, comandante. Este é o seu despertar.\n\nCARRO NA PORTARIA: {_lt(conn, tl['pickup_utc'], ades)} ({ride_text(r)})\n"
             f"APRESENTAÇÃO: {_lt(conn, tl['report_utc'], at)} em {at}\nPRÓXIMO VOO: {nxt}\n\nBom trabalho hoje.", "acao", "alarme", ref),
            (f"stay_pickup:{fid}", tl["pickup_utc"], "transporte", ("Seu carro chegou" if app else "Van na portaria") + f" · {hotel}",
             (f"Seu carro chegou na portaria do {hotel}." if app else f"Comandante, a van da tripulação está na portaria do {hotel}.")
             + f"\n\nVEÍCULO: {r['vehicle']} · placa {r['plate']}\nMOTORISTA: {r['driver']}\nTRAJETO ATÉ {at}: {tl['drive']['label']}\n"
             f"APRESENTAÇÃO: {_lt(conn, tl['report_utc'], at)}\n\n(motorista e placa fictícios)", "acao", None, ref),
        ]
    ts = now_iso()
    for key, due, sector, subj, body, lvl, ch, rf in ev:
        conn.execute("""INSERT INTO stay_event(key, flight_id, due_utc, sector, subject, body, level, channel, ref, created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(key) DO UPDATE SET due_utc=excluded.due_utc, sector=excluded.sector, subject=excluded.subject,
                        body=excluded.body, level=excluded.level, channel=excluded.channel, ref=excluded.ref, status='PENDENTE'
                        WHERE stay_event.status IN ('PENDENTE','CANCELADO')""", (key, fid, due, sector, subj[:160], body, lvl, ch, rf, ts))
    if tl["short"]:                                      # despertar já não cabe: cancela os da manhã agendados antes
        conn.execute("UPDATE stay_event SET status='CANCELADO' WHERE status='PENDENTE' AND key IN (?,?)", (f"stay_wake:{fid}", f"stay_pickup:{fid}"))
    return tl


def dispatch(conn, now: str | None = None) -> int:
    """Entrega as mensagens do pernoite cuja hora chegou (dentro de transação)."""
    from . import equipe
    now = now or now_iso()
    nowd = _p(now)
    n = 0
    for ev in rows(conn.execute("SELECT * FROM stay_event WHERE status='PENDENTE' AND due_utc<=? ORDER BY due_utc", (now,))):
        f = one(conn.execute("SELECT state FROM flight WHERE id=?", (ev["flight_id"],)))
        st = "ENVIADO"
        if not f or f["state"] not in OK_STATES:
            st = "CANCELADO"                             # voo estornado, cancelado ou rejeitado
        elif ev["ref"]:
            ref = jloads(ev["ref"], {})
            leg = one(conn.execute("SELECT dep_utc FROM roster_leg WHERE id=? AND removed_batch IS NULL", (ref.get("leg_id"),))) if ref.get("leg_id") else None
            if ref.get("leg_id") and (not leg or leg["dep_utc"] != ref.get("dep_utc")):
                st = "CANCELADO"                         # a escala mudou: o Controle de Tripulantes já avisou a alteração
        if st == "ENVIADO" and (nowd - _p(ev["due_utc"])).total_seconds() > STALE_H * 3600:
            st = "VENCIDO"
        if st == "ENVIADO":
            equipe.post(conn, ev["key"], ev["sector"], ev["subject"], ev["body"], flight_id=ev["flight_id"], level=ev["level"],
                        now=ev["due_utc"], channel=ev["channel"])
            n += 1
        conn.execute("UPDATE stay_event SET status=?, sent_at=? WHERE key=?", (st, now, ev["key"]))
    return n


def events_for(conn, flight_id: int) -> list[dict]:
    return rows(conn.execute("SELECT key, due_utc, sector, subject, level, channel, status FROM stay_event WHERE flight_id=? ORDER BY due_utc",
                             (flight_id,)))


def upcoming(conn, now: str | None = None, limit=6) -> list[dict]:
    now = now or now_iso()
    out = rows(conn.execute("""SELECT e.key, e.flight_id, e.due_utc, e.sector, e.subject, e.level, f.flight_number, f.ades FROM stay_event e
                               JOIN flight f ON f.id=e.flight_id WHERE e.status='PENDENTE' AND e.due_utc>? ORDER BY e.due_utc LIMIT ?""", (now, limit)))
    return out
