"""METAR/TAF gratuitos e sem chave.

Fontes:
* principal: aviationweather.gov Data API (NOAA/AWC) – https://aviationweather.gov/api/data/metar?ids=...&format=json
  e /api/data/taf?ids=...&format=json
* reserva (só METAR): NOAA tgftp – https://tgftp.nws.noaa.gov/data/observations/metar/stations/XXXX.TXT

Os dados ficam em cache no SQLite (tabela weather_cache). Se a API falhar, a última observação em cache
é devolvida marcada como desatualizada. A decodificação é feita localmente a partir do texto bruto,
assim o significado de cada grupo fica rastreável.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

UA = "AeroScale/1.2 (simulacao; contato local)"
TIMEOUT = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS weather_cache (
    kind TEXT NOT NULL, icao TEXT NOT NULL, raw TEXT, data TEXT, obs_time TEXT,
    source TEXT, fetched_at TEXT NOT NULL, error TEXT,
    PRIMARY KEY (kind, icao)
);
"""


def _awc_base():
    return os.environ.get("AEROSCALE_AWC_URL", "https://aviationweather.gov/api/data")


def _noaa_base():
    return os.environ.get("AEROSCALE_NOAA_URL", "https://tgftp.nws.noaa.gov/data/observations/metar/stations")


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json,text/plain"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", errors="replace")


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ------------------------------------------------------------------ decodificação
COVER = {"FEW": "poucas (1–2/8)", "SCT": "esparsas (3–4/8)", "BKN": "nublado (5–7/8)", "OVC": "encoberto (8/8)",
         "NSC": "sem nuvens significativas", "NCD": "nenhuma nuvem detectada", "SKC": "céu claro", "CLR": "céu claro"}
INTENSITY = {"-": "fraca", "+": "forte", "VC": "nas vizinhanças"}
DESCR = {"MI": "baixo", "BC": "em bancos", "PR": "parcial", "DR": "flutuante", "BL": "soprado", "SH": "pancadas de",
         "TS": "trovoada", "FZ": "congelante"}
PHEN = {"DZ": "chuvisco", "RA": "chuva", "SN": "neve", "SG": "grãos de neve", "IC": "cristais de gelo",
        "PL": "pelotas de gelo", "GR": "granizo", "GS": "granizo pequeno", "UP": "precipitação desconhecida",
        "BR": "névoa úmida", "FG": "nevoeiro", "FU": "fumaça", "VA": "cinzas vulcânicas", "DU": "poeira",
        "SA": "areia", "HZ": "névoa seca", "PY": "spray", "PO": "redemoinhos de poeira", "SQ": "tempestade (rajada)",
        "FC": "funil/tromba", "SS": "tempestade de areia", "DS": "tempestade de poeira"}
WX_RE = re.compile(r"^(?P<int>[-+]|VC)?(?P<desc>MI|BC|PR|DR|BL|SH|TS|FZ)?(?P<ph>(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|"
                   r"DU|SA|HZ|PY|PO|SQ|FC|SS|DS)*)$")


def _wx_text(tok: str) -> str | None:
    m = WX_RE.match(tok)
    if not m or not (m.group("desc") or m.group("ph")):
        return None
    phs = re.findall(r"..", m.group("ph") or "")
    parts = []
    if m.group("desc") == "TS":
        parts.append("trovoada" + (" com" if phs else ""))
    elif m.group("desc") and m.group("desc") not in ("FZ",):
        parts.append(DESCR[m.group("desc")])
    parts += [PHEN.get(p, p) for p in phs]
    if m.group("desc") == "FZ":
        parts.append("congelante")
    if m.group("int"):
        parts.append(INTENSITY[m.group("int")])
    return " ".join(parts)


def decode_metar(raw: str, ref: datetime | None = None) -> dict:
    """Decodifica um METAR/SPECI (formato ICAO, como usado no Brasil)."""
    ref = ref or now_utc()
    toks = raw.strip().split()
    d: dict = {"raw": raw.strip(), "type": "METAR", "station": None, "time": None, "auto": False, "cor": False,
               "nil": False, "wind": None, "visibility": None, "rvr": [], "weather": [], "clouds": [],
               "vertical_visibility_ft": None, "temp_c": None, "dewpoint_c": None, "qnh_hpa": None,
               "trend": None, "remarks": None, "cavok": False, "unparsed": []}
    i = 0
    if toks and toks[0] in ("METAR", "SPECI"):
        d["type"] = toks[0]
        i += 1
    if i < len(toks) and re.fullmatch(r"[A-Z]{4}", toks[i]):
        d["station"] = toks[i]
        i += 1
    if i < len(toks) and re.fullmatch(r"\d{6}Z", toks[i]):
        day, hh, mm = int(toks[i][:2]), int(toks[i][2:4]), int(toks[i][4:6])
        y, mo = ref.year, ref.month
        if day > ref.day + 1:  # observação do mês anterior
            mo -= 1
            if mo == 0:
                mo, y = 12, y - 1
        try:
            d["time"] = iso(datetime(y, mo, day, hh, mm, tzinfo=timezone.utc))
        except ValueError:
            pass
        i += 1
    rest = toks[i:]
    in_rmk = False
    trend_toks: list[str] = []
    rmk_toks: list[str] = []
    for t in rest:
        if in_rmk:
            rmk_toks.append(t)
            continue
        if t == "RMK":
            in_rmk = True
            continue
        if trend_toks or t in ("NOSIG", "BECMG", "TEMPO"):
            trend_toks.append(t)
            continue
        if t == "AUTO":
            d["auto"] = True
        elif t == "COR":
            d["cor"] = True
        elif t == "NIL":
            d["nil"] = True
        elif m := re.fullmatch(r"(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?(KT|MPS)", t):
            spd = int(m.group(2))
            gst = int(m.group(3)) if m.group(3) else None
            if m.group(4) == "MPS":
                spd, gst = round(spd * 1.944), (round(gst * 1.944) if gst else None)
            d["wind"] = {"dir": None if m.group(1) == "VRB" else int(m.group(1)), "variable": m.group(1) == "VRB",
                         "speed_kt": spd, "gust_kt": gst, "var_from": None, "var_to": None, "calm": spd == 0}
        elif (m := re.fullmatch(r"(\d{3})V(\d{3})", t)) and d["wind"]:
            d["wind"]["var_from"], d["wind"]["var_to"] = int(m.group(1)), int(m.group(2))
        elif t == "CAVOK":
            d["cavok"] = True
            d["visibility"] = {"m": 10000, "text": "≥ 10 km (CAVOK)"}
        elif re.fullmatch(r"\d{4}", t) and d["visibility"] is None:
            v = int(t)
            d["visibility"] = {"m": v, "text": "≥ 10 km" if v >= 9999 else f"{v} m" if v < 5000 else f"{v / 1000:g} km"}
        elif re.fullmatch(r"\d{4}[NSEW]{1,2}", t):
            d.setdefault("min_visibility", t)
        elif re.fullmatch(r"M?\d+(?:/\d+)?SM", t):
            from fractions import Fraction
            try:
                val = float(Fraction(t[:-2].lstrip("M")))
            except (ValueError, ZeroDivisionError):
                val = None
            if val is not None:
                d["visibility"] = {"m": int(val * 1609), "text": t}
        elif m := re.fullmatch(r"R(\d{2}[LCR]?)/([PM]?\d{4})(?:V([PM]?\d{4}))?([UDN])?", t):
            d["rvr"].append({"runway": m.group(1), "value": m.group(2), "trend": m.group(4)})
        elif m := re.fullmatch(r"(FEW|SCT|BKN|OVC)(\d{3}|///)(CB|TCU|///)?", t):
            base = None if m.group(2) == "///" else int(m.group(2)) * 100
            typ = m.group(3) if m.group(3) in ("CB", "TCU") else None
            d["clouds"].append({"cover": m.group(1), "base_ft": base, "type": typ,
                                "text": f"{COVER[m.group(1)]} a {base if base is not None else '?'} ft"
                                        + (" – cumulonimbus" if typ == "CB" else " – cúmulos congestos" if typ == "TCU" else "")})
        elif t in ("NSC", "NCD", "SKC", "CLR"):
            d["clouds"].append({"cover": t, "base_ft": None, "type": None, "text": COVER[t]})
        elif m := re.fullmatch(r"VV(\d{3}|///)", t):
            d["vertical_visibility_ft"] = None if m.group(1) == "///" else int(m.group(1)) * 100
        elif m := re.fullmatch(r"(M?\d{2})/(M?\d{2})?", t):
            d["temp_c"] = int(m.group(1).replace("M", "-"))
            d["dewpoint_c"] = int(m.group(2).replace("M", "-")) if m.group(2) else None
        elif m := re.fullmatch(r"Q(\d{4})", t):
            d["qnh_hpa"] = int(m.group(1))
        elif m := re.fullmatch(r"A(\d{4})", t):
            d["qnh_hpa"] = round(int(m.group(1)) / 100 * 33.8639)
            d["altimeter_inhg"] = int(m.group(1)) / 100
        elif re.fullmatch(r"RE\w+", t):
            d.setdefault("recent", []).append(_wx_text(t[2:]) or t)
        elif re.fullmatch(r"WS\w*", t):
            d.setdefault("windshear", []).append(t)
        elif d.get("windshear") and re.fullmatch(r"R\d{2}[LCR]?|ALL|RWY", t):
            d["windshear"][-1] += " " + t
        elif (txt := _wx_text(t)) is not None:
            d["weather"].append({"raw": t, "text": txt})
        elif t.endswith("="):
            continue
        else:
            d["unparsed"].append(t)
    if trend_toks:
        d["trend"] = " ".join(trend_toks)
    if rmk_toks:
        d["remarks"] = " ".join(rmk_toks)
    # teto: menor camada BKN/OVC ou visibilidade vertical
    ceil = [c["base_ft"] for c in d["clouds"] if c["cover"] in ("BKN", "OVC") and c["base_ft"] is not None]
    if d["vertical_visibility_ft"] is not None:
        ceil.append(d["vertical_visibility_ft"])
    d["ceiling_ft"] = min(ceil) if ceil else None
    d["flight_category"] = flight_category(d)
    if d["temp_c"] is not None and d["dewpoint_c"] is not None:
        t, td = d["temp_c"], d["dewpoint_c"]
        import math
        a, b = 17.625, 243.04
        d["humidity_pct"] = round(100 * math.exp(a * td / (b + td)) / math.exp(a * t / (b + t)))
    d["alerts"] = metar_alerts(d)
    return d


def flight_category(d: dict) -> str | None:
    """VFR / MVFR / IFR / LIFR (critério teto + visibilidade, padrão FAA/AWC)."""
    vis = (d.get("visibility") or {}).get("m")
    ceil = d.get("ceiling_ft")
    if vis is None and ceil is None and not d.get("cavok"):
        return None
    vis = 10000 if vis is None else vis
    ceil = 99999 if ceil is None else ceil
    if ceil < 500 or vis < 1600:
        return "LIFR"
    if ceil < 1000 or vis < 4800:
        return "IFR"
    if ceil <= 3000 or vis <= 8000:
        return "MVFR"
    return "VFR"


def metar_alerts(d: dict) -> list[dict]:
    al = []
    w = d.get("wind") or {}
    if w.get("gust_kt") and w["gust_kt"] >= 25:
        al.append({"level": "warning", "text": f"Rajadas de {w['gust_kt']} kt"})
    elif w.get("speed_kt") and w["speed_kt"] >= 20:
        al.append({"level": "warning", "text": f"Vento forte: {w['speed_kt']} kt"})
    if w.get("variable") and (w.get("speed_kt") or 0) >= 6:
        al.append({"level": "info", "text": "Vento de direção variável"})
    wx = " ".join(x["raw"] for x in d.get("weather") or [])
    if "TS" in wx:
        al.append({"level": "error", "text": "Trovoada no aeródromo ou vizinhanças"})
    if any(c.get("type") == "CB" for c in d.get("clouds") or []):
        al.append({"level": "error", "text": "Cumulonimbus (CB) reportado"})
    elif any(c.get("type") == "TCU" for c in d.get("clouds") or []):
        al.append({"level": "warning", "text": "Cúmulos congestos (TCU)"})
    if "FG" in wx:
        al.append({"level": "error", "text": "Nevoeiro"})
    if re.search(r"\+\w*(RA|SH)", wx):
        al.append({"level": "warning", "text": "Precipitação forte"})
    if "FZ" in wx or "SN" in wx or "GR" in wx:
        al.append({"level": "error", "text": "Precipitação congelante/granizo"})
    if d.get("windshear"):
        al.append({"level": "error", "text": "Tesoura de vento (WS) reportada"})
    vis = (d.get("visibility") or {}).get("m")
    if vis is not None and vis < 1500:
        al.append({"level": "error", "text": f"Visibilidade {vis} m"})
    if d.get("ceiling_ft") is not None and d["ceiling_ft"] < 500:
        al.append({"level": "error", "text": f"Teto baixo: {d['ceiling_ft']} ft"})
    if d.get("temp_c") is not None and d.get("dewpoint_c") is not None and d["temp_c"] - d["dewpoint_c"] <= 2 \
            and "FG" not in wx:
        al.append({"level": "info", "text": "Temperatura próxima do ponto de orvalho: risco de névoa/nevoeiro"})
    if d.get("nil"):
        al.append({"level": "warning", "text": "METAR NIL (sem dados)"})
    return al


def freshness(obs_iso: str | None, ref: datetime | None = None) -> dict:
    ref = ref or now_utc()
    if not obs_iso:
        return {"age_min": None, "status": "indisponivel", "label": "sem horário"}
    age = int((ref - parse_iso(obs_iso)).total_seconds() // 60)
    if age <= 70:
        st, lb = "atual", f"há {age} min"
    elif age <= 180:
        st, lb = "atrasado", f"há {age // 60} h {age % 60:02d} min (METAR é horário)"
    else:
        st, lb = "desatualizado", f"há {age // 60} h – desatualizado"
    return {"age_min": age, "status": st, "label": lb}


# ------------------------------------------------------------------ busca + cache
def _cache_get(db, kind, icao):
    r = db.execute("SELECT * FROM weather_cache WHERE kind=? AND icao=?", (kind, icao)).fetchone()
    return dict(r) if r else None


def _cache_put(db, kind, icao, raw, data, obs_time, source, error=None):
    db.execute("INSERT OR REPLACE INTO weather_cache(kind, icao, raw, data, obs_time, source, fetched_at, error)"
               " VALUES (?,?,?,?,?,?,?,?)",
               (kind, icao, raw, json.dumps(data, ensure_ascii=False) if data is not None else None, obs_time, source,
                iso(now_utc()), error))


def _fetch_awc_metar(icaos: list[str]) -> dict[str, dict]:
    url = f"{_awc_base()}/metar?" + urllib.parse.urlencode({"ids": ",".join(icaos), "format": "json", "hours": 3})
    txt = _get(url)
    data = json.loads(txt) if txt.strip() else []
    out = {}
    for item in sorted(data, key=lambda x: x.get("obsTime") or 0):
        icao = item.get("icaoId")
        raw = item.get("rawOb")
        if icao and raw:
            out[icao] = {"raw": raw, "obs_time": iso(datetime.fromtimestamp(item["obsTime"], timezone.utc))
                         if item.get("obsTime") else None, "name": item.get("name"), "awc_flight_category": item.get("fltCat")}
    return out


def _fetch_noaa_metar(icao: str) -> dict | None:
    txt = _get(f"{_noaa_base()}/{icao}.TXT")
    lines = [l.strip() for l in txt.strip().splitlines() if l.strip()]
    if len(lines) < 2:
        return None
    try:
        obs = datetime.strptime(lines[0], "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        obs = None
    return {"raw": " ".join(lines[1:]), "obs_time": iso(obs), "name": None}


def get_metars(db, icaos: list[str], ttl_min: int = 10, force: bool = False) -> list[dict]:
    icaos = [i.strip().upper() for i in icaos if re.fullmatch(r"[A-Za-z]{4}", (i or "").strip())]
    icaos = list(dict.fromkeys(icaos))[:40]
    now = now_utc()
    need = []
    for ic in icaos:
        c = _cache_get(db, "metar", ic)
        if force or not c or not c["fetched_at"] or (now - parse_iso(c["fetched_at"])).total_seconds() > ttl_min * 60:
            need.append(ic)
    errors: dict[str, str] = {}
    if need:
        got: dict[str, dict] = {}
        src = "aviationweather.gov"
        try:
            got = _fetch_awc_metar(need)
        except Exception as e:  # rede, HTTP, JSON
            errors["_awc"] = f"aviationweather.gov indisponível ({e.__class__.__name__})"
        for ic in need:
            item, source = got.get(ic), src
            if not item:
                try:
                    item = _fetch_noaa_metar(ic)
                    source = "NOAA tgftp"
                except urllib.error.HTTPError as e:
                    errors[ic] = "sem METAR recente" if e.code == 404 else f"NOAA HTTP {e.code}"
                except Exception as e:
                    errors[ic] = f"falha na consulta ({e.__class__.__name__})"
            if item:
                dec = decode_metar(item["raw"], now)
                if item.get("obs_time"):
                    dec["time"] = item["obs_time"]
                dec["awc_flight_category"] = item.get("awc_flight_category")
                dec["station_name"] = item.get("name")
                _cache_put(db, "metar", ic, item["raw"], dec, dec.get("time"), source)
            else:
                old = _cache_get(db, "metar", ic)
                msg = errors.get(ic) or errors.get("_awc") or "sem METAR disponível"
                if old and old["raw"]:
                    db.execute("UPDATE weather_cache SET error=? WHERE kind='metar' AND icao=?", (msg, ic))
                else:
                    _cache_put(db, "metar", ic, None, None, None, None, msg)
    out = []
    for ic in icaos:
        c = _cache_get(db, "metar", ic)
        dec = json.loads(c["data"]) if c and c["data"] else None
        out.append({"icao": ic, "available": bool(dec), "metar": dec, "source": c["source"] if c else None,
                    "fetched_at": c["fetched_at"] if c else None, "error": c["error"] if c else "sem dados",
                    "freshness": freshness(dec["time"] if dec else None, now)})
    return out


def get_tafs(db, icaos: list[str], ttl_min: int = 30, force: bool = False) -> list[dict]:
    icaos = list(dict.fromkeys(i.strip().upper() for i in icaos if re.fullmatch(r"[A-Za-z]{4}", (i or "").strip())))[:20]
    now = now_utc()
    need = [ic for ic in icaos if force or not (c := _cache_get(db, "taf", ic)) or
            (now - parse_iso(c["fetched_at"])).total_seconds() > ttl_min * 60]
    err = None
    if need:
        try:
            url = f"{_awc_base()}/taf?" + urllib.parse.urlencode({"ids": ",".join(need), "format": "json"})
            txt = _get(url)
            data = json.loads(txt) if txt.strip() else []
            seen = set()
            for item in data:
                ic = item.get("icaoId")
                if not ic or ic in seen:
                    continue
                seen.add(ic)
                vf = item.get("validTimeFrom")
                vt = item.get("validTimeTo")
                rec = {"raw": item.get("rawTAF"), "issue_time": item.get("issueTime"),
                       "valid_from": iso(datetime.fromtimestamp(vf, timezone.utc)) if vf else None,
                       "valid_to": iso(datetime.fromtimestamp(vt, timezone.utc)) if vt else None,
                       "segments": split_taf(item.get("rawTAF") or "")}
                _cache_put(db, "taf", ic, rec["raw"], rec, rec["valid_from"], "aviationweather.gov")
            for ic in need:
                if ic not in seen:
                    _cache_put(db, "taf", ic, None, None, None, "aviationweather.gov", "sem TAF emitido")
        except Exception as e:
            err = f"aviationweather.gov indisponível ({e.__class__.__name__})"
    out = []
    for ic in icaos:
        c = _cache_get(db, "taf", ic)
        data = json.loads(c["data"]) if c and c["data"] else None
        valid_now = None
        if data and data.get("valid_from") and data.get("valid_to"):
            valid_now = parse_iso(data["valid_from"]) <= now <= parse_iso(data["valid_to"])
        out.append({"icao": ic, "available": bool(data), "taf": data, "source": c["source"] if c else None,
                    "fetched_at": c["fetched_at"] if c else None, "error": (c["error"] if c else None) or err,
                    "valid_now": valid_now})
    return out


def split_taf(raw: str) -> list[str]:
    parts = re.split(r"\s(?=(?:BECMG|TEMPO|FM\d{6}|PROB\d{2}(?!\s+TEMPO)|PROB\d{2} TEMPO)\b)", raw.strip())
    return [p.strip() for p in parts if p.strip()]


def compare(o: dict | None, d: dict | None) -> list[dict]:
    """Diferenças meteorológicas relevantes entre origem e destino."""
    if not o or not d:
        return []
    out = []
    order = {"VFR": 0, "MVFR": 1, "IFR": 2, "LIFR": 3}
    fo, fd = o.get("flight_category"), d.get("flight_category")
    if fo and fd and fo != fd:
        worse = "destino" if order[fd] > order[fo] else "origem"
        out.append({"level": "warning" if order.get(fd, 0) >= 2 or order.get(fo, 0) >= 2 else "info",
                    "text": f"Categoria diferente: origem {fo}, destino {fd} (pior no {worse})."})
    if o.get("qnh_hpa") and d.get("qnh_hpa") and abs(o["qnh_hpa"] - d["qnh_hpa"]) >= 4:
        out.append({"level": "info", "text": f"QNH muda {d['qnh_hpa'] - o['qnh_hpa']:+d} hPa ({o['qnh_hpa']} → {d['qnh_hpa']})."})
    if o.get("temp_c") is not None and d.get("temp_c") is not None and abs(o["temp_c"] - d["temp_c"]) >= 8:
        out.append({"level": "info", "text": f"Temperatura {o['temp_c']} °C → {d['temp_c']} °C."})
    wo, wd = (o.get("wind") or {}), (d.get("wind") or {})
    so, sd = max(wo.get("speed_kt") or 0, wo.get("gust_kt") or 0), max(wd.get("speed_kt") or 0, wd.get("gust_kt") or 0)
    if sd - so >= 10:
        out.append({"level": "warning", "text": f"Vento mais forte no destino ({sd} kt contra {so} kt)."})
    if d.get("alerts") and not o.get("alerts"):
        out.append({"level": "warning", "text": "Fenômenos relevantes só no destino: "
                                                + ", ".join(a["text"] for a in d["alerts"][:3]) + "."})
    return out
