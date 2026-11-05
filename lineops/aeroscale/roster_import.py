"""Importar uma escala real em texto (v2.2).

Aceita o formato das escalas que circulam entre tripulantes, um dia por linha:

    ESCALA DE NOVEMBRO :
    (01) REC-FOR-REC-GIG (4655,4656,4625)
    (06)VCP-PNZ-VCP +1 (4220,4221)        chegada depois da meia-noite
    (07) FR | DO | FOLGA                  folga
    (08) INATIVO VCP | INATIVO EM BEL     dia sem voo fora da base
    (17) SOBREAVISO | SA                  sobreaviso
    (20) RESERVA GRU                      reserva
    (25) DHD 2801                         extra (passageiro)
    (28) MCZ-REC ( 4557 DHD) / REC-UDI-REC (2592,2438)

Cada voo é procurado no RPL (e nos internacionais observados no ADS-B) pelo número e pelo trecho, na data. Não
existindo nessa data, usa o horário do mesmo voo numa data próxima (até 7 dias) e avisa; não existindo de jeito
nenhum, estima o horário pela duração real do trecho e marca como sugestão. A escala criada vira uma escala normal do
AeroScale (briefing, SimBrief, ACARS, diário de bordo), marcada como importada.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone

from . import catalog, crew_profiles
from . import intl as intl_mod
from .airports import get_tz, to_local
from .db import get_settings, now_iso
from .rpl_service import all_route_eet, get_index

MESES = ["JANEIRO", "FEVEREIRO", "MARCO", "ABRIL", "MAIO", "JUNHO", "JULHO", "AGOSTO", "SETEMBRO", "OUTUBRO",
         "NOVEMBRO", "DEZEMBRO"]
OFF_WORDS = {"FR", "DO", "FOLGA", "OFF", "F", "FOLGA PEDIDA", "FP", "FER", "FERIAS", "FÉRIAS"}
CODE_HINT = {"FR": "AZU", "DO": "TAM"}


class ImportError_(ValueError):
    pass


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return s.upper().replace("\t", " ")


def parse(text: str, year: int, month: int) -> tuple[list[dict], list[str]]:
    """Texto → lista de dias {date, kind, ...}. kind: voo | folga | sobreaviso | inativo | reserva | dhd."""
    days, warns = [], []
    y, m, last_day = year, month, 0
    for n_line, raw in enumerate(text.splitlines(), 1):
        line = _norm(raw).strip()
        if not line:
            continue
        hdr = re.match(r"^(?:ESCALA\s+DE\s+)?(" + "|".join(MESES) + r")(?:\s+(?:DE\s+)?(\d{4}))?\s*:?\s*$", line)
        if hdr:
            nm = MESES.index(hdr.group(1)) + 1
            if hdr.group(2):
                y = int(hdr.group(2))
            elif last_day and nm < m:
                y += 1
            m, last_day = nm, 0
            continue
        line = re.sub(r"^\d+\.\s*(?=\()", "", line)          # "1. (01) ..." (lista numerada)
        mm = re.match(r"^\(\s*(\d{1,2})\s*\)\s*(.*)$", line) or re.match(r"^(\d{1,2})[\s.:-]+(.*)$", line)
        if not mm:
            warns.append(f"Linha {n_line} ignorada (sem dia entre parênteses): {raw.strip()[:60]}")
            continue
        dn, body = int(mm.group(1)), mm.group(2).strip()
        if last_day and dn < last_day:                        # virou o mês sem cabeçalho
            m += 1
            if m == 13:
                y, m = y + 1, 1
        last_day = dn
        try:
            d = date(y, m, dn)
        except ValueError:
            warns.append(f"Linha {n_line}: dia {dn:02d} não existe em {m:02d}/{y}.")
            continue
        days.append({"date": d, "line": n_line, "raw": raw.strip(), **_body(body, warns, n_line)})
    return days, warns


def _body(body: str, warns: list, n_line: int) -> dict:
    b = re.sub(r"\s+", " ", body).strip()
    if b in OFF_WORDS or re.fullmatch(r"(FR|DO|FOLGA)\b.*", b) and not re.search(r"[A-Z]{3}-[A-Z]{3}", b):
        return {"kind": "folga", "code": b.split(" ")[0]}
    if re.match(r"^(SOBREAVISO|SA|SBA|STBY|STANDBY)\b", b):
        return {"kind": "sobreaviso"}
    mi = re.match(r"^INATIVO(?:\s+(?:EM\s+)?([A-Z]{3,4}))?", b)
    if mi:
        return {"kind": "inativo", "loc": mi.group(1)}
    mr = re.match(r"^(?:RESERVA|RES|RSV)(?:\s+([A-Z]{3,4}))?\b", b)
    if mr:
        return {"kind": "reserva", "loc": mr.group(1)}
    md = re.match(r"^(?:DHD|DH)\s*([0-9][0-9 ,]*)$", b)
    if md:
        return {"kind": "dhd", "numbers": [x for x in re.split(r"[ ,]+", md.group(1)) if x]}
    segs = []
    # "REC-CNF(4605)/CNF-PMW-CNF (4408,4409) +1": blocos separados por "/" (com ou sem espaço), números entre
    # parênteses colados ou não ao trecho, separados por vírgula OU espaço ("8045 3699 3252")
    for part in [x.strip() for x in re.split(r"\s*/\s*", b) if x.strip()]:
        ms = re.match(r"^([A-Z]{3,4}(?:\s*-\s*[A-Z]{3,4})+)\s*(\+1)?\s*(?:\(([^)]*)\)?)?\s*(\+1)?\s*(DHD)?$", part)
        if not ms:
            warns.append(f"Linha {n_line}: trecho não entendido: {part[:50]}")
            continue
        route = [x.strip() for x in ms.group(1).split("-")]
        toks = [t.strip() for t in re.split(r"[,;\s]+", ms.group(3) or "") if t.strip()]
        nums, dh = [], []
        for t in toks:
            is_dh = "DH" in t or bool(ms.group(5))
            digits = re.sub(r"\D", "", t)
            nums.append(digits)
            dh.append(is_dh)
        if not toks and ms.group(5):
            dh = [True] * (len(route) - 1)
        if nums and len(nums) != len(route) - 1:
            warns.append(f"Linha {n_line}: {len(route) - 1} trecho(s) e {len(nums)} número(s) de voo em {part[:40]}.")
        segs.append({"route": route, "numbers": nums, "dhd": dh, "plus1": bool(ms.group(2) or ms.group(4))})
    if not segs:
        return {"kind": "desconhecido", "text": b}
    return {"kind": "voo", "segments": segs}


# ------------------------------------------------------------------ casar com o RPL
def _digits(ident: str | None) -> str:
    return re.sub(r"^\D+", "", re.sub(r"^(?:[A-Z]{3}|[A-Z][A-Z0-9]|[0-9][A-Z])(?=\d)", "", ident or "")).lstrip("0")


class Matcher:
    def __init__(self, db, airline: str, d1: date, d2: date):
        s = get_settings(db)
        self.db, self.airline = db, airline
        self.apmap = catalog.airport_map(db)
        self.i2i = {}
        for k, a in self.apmap.items():
            if a.get("iata"):
                self.i2i.setdefault(a["iata"], k)
        self.taxi = int(s["taxi_allowance_min"] or 0)
        idx = get_index(db, airline, self.taxi)
        occ = idx.occurrences_range(d1 - timedelta(days=8), d2 + timedelta(days=8), True) if idx.flights else []
        occ = list(occ) + intl_mod.occurrences(db, airline, d1 - timedelta(days=8), d2 + timedelta(days=8))
        self.by_key: dict[tuple, list] = {}
        self.by_num: dict[str, list] = {}
        for o in occ:
            n = _digits(o.get("ident"))
            self.by_key.setdefault((n, o["adep"], o["ades"]), []).append(o)
            self.by_num.setdefault(n, []).append(o)
        self.eet = None
        self.added: list[str] = []

    def icao(self, code: str) -> str | None:
        code = (code or "").upper()
        if len(code) == 4 and code in self.apmap:
            return code
        if code in self.i2i:
            return self.i2i[code]
        # estrangeiro fora do catálogo (ex.: ROS, PUJ): cadastra a partir da base mundial do OurAirports
        from .airport_geo_world import WORLD_AIRPORTS
        from .world_tz import tz_for
        for ic, (iata, lat, lon, city, country) in WORLD_AIRPORTS.items():
            if iata == code:
                catalog.upsert_airport(self.db, {"icao": ic, "iata": iata, "name": city, "city": city,
                                                 "tz": tz_for(country, lon) or ""})
                self.apmap[ic] = {"icao": ic, "iata": iata, "city": city, "tz": tz_for(country, lon), "lat": lat,
                                  "lon": lon}
                self.i2i[iata] = ic
                self.added.append(f"{iata} ({ic}, {city})")
                return ic
        return None

    def tz(self, icao):
        return catalog.airport_tz(self.apmap, icao) or "America/Sao_Paulo"

    def local_date(self, o):
        return to_local(o["dep"], self.tz(o["adep"])).date()

    def find(self, num: str, a: str, b: str, d: date, after: datetime | None, allow_next: bool):
        """(ocorrência, como) – como: 'rpl' (mesma data), 'copiado' (data próxima), None."""
        n = (num or "").lstrip("0")
        cands = self.by_key.get((n, a, b), [])
        same = [o for o in cands if self.local_date(o) == d or (allow_next and self.local_date(o) == d + timedelta(days=1)
                                                                and to_local(o["dep"], self.tz(a)).hour < 8)]
        if after:
            ok = [o for o in same if o["dep"] >= after]
            same = ok or same
        if same:
            return min(same, key=lambda o: o["dep"]), "rpl"
        near = sorted(cands, key=lambda o: abs((self.local_date(o) - d).days))
        if near and abs((self.local_date(near[0]) - d).days) <= 7:
            o = near[0]
            shift = d - self.local_date(o)
            c = {**o, "dep": o["dep"] + shift, "arr": o["arr"] + shift, "copied_from": self.local_date(o).isoformat()}
            if after and c["dep"] < after:   # voo de madrugada copiado: dia seguinte
                c = {**c, "dep": c["dep"] + timedelta(days=1), "arr": c["arr"] + timedelta(days=1)}
            return c, "copiado"
        return None, None

    def find_number(self, num: str, d: date, origin: str | None):
        n = (num or "").lstrip("0")
        cands = [o for o in self.by_num.get(n, []) if self.local_date(o) == d]
        if origin:
            cands = [o for o in cands if o["adep"] == origin] or cands
        return min(cands, key=lambda o: o["dep"]) if cands else None

    def estimate(self, num: str, a: str, b: str, d: date, after: datetime | None) -> dict:
        if self.eet is None:
            self.eet = all_route_eet(self.db)
        eet = self.eet.get((a, b))
        if not eet:
            A, B = self.apmap.get(a) or {}, self.apmap.get(b) or {}
            import math
            if A.get("lat") is not None and B.get("lat") is not None:
                r = math.pi / 180
                h = math.sin((B["lat"] - A["lat"]) * r / 2) ** 2 + math.cos(A["lat"] * r) * math.cos(B["lat"] * r) * \
                    math.sin((B["lon"] - A["lon"]) * r / 2) ** 2
                eet = int(3440 * 2 * math.asin(math.sqrt(h)) / 430 * 60 + 15)
            else:
                eet = 90
        tz = get_tz(self.tz(a)) or timezone.utc
        dep = datetime.combine(d, time(8, 0), tz).astimezone(timezone.utc)
        if after:
            dep = max(dep, after + timedelta(minutes=15))
        return {"ref": f"TXT:{num}:{a}:{b}:{d}", "flight_id": None, "airline": self.airline,
                "ident": f"{self.airline}{num}" if num else None, "type_icao": "", "adep": a, "ades": b, "dep": dep,
                "arr": dep + timedelta(minutes=eet + self.taxi), "eet_min": eet, "origin": "SUGESTAO", "ref_date": None}


def import_text(db, text: str, airline: str | None, month: str, base: str | None = None, name: str | None = None) -> dict:
    from .scheduler import _insert_legs, save_days, fmt_utc  # noqa: F401  (import tardio: evita ciclo)
    try:
        y, m = map(int, (month or "").split("-"))
    except ValueError:
        raise ImportError_("Informe o mês da primeira linha (AAAA-MM).")
    days, warns = parse(text or "", y, m)
    if not days:
        raise ImportError_("Nenhum dia reconhecido. Use uma linha por dia, como (01) REC-FOR-REC (4655,4656).")
    if not airline:
        codes = Counter(CODE_HINT.get(d.get("code")) for d in days if d["kind"] == "folga")
        codes.pop(None, None)
        airline = codes.most_common(1)[0][0] if codes else None
    airline = (airline or "").upper()
    if airline not in ("AZU", "GLO", "TAM"):
        raise ImportError_("Escolha a companhia da escala (Azul, GOL ou LATAM).")
    d1, d2 = min(d["date"] for d in days), max(d["date"] for d in days)
    mt = Matcher(db, airline, d1, d2)
    legs, recs, offs = [], [], []
    stats = Counter()
    unknown_ap = set()
    prev_arr, loc = None, None
    s = get_settings(db)
    gap = int(s["min_turn_min"] or 40)
    for day in sorted(days, key=lambda x: x["date"]):
        d = day["date"]
        k = day["kind"]
        if k == "folga":
            offs.append(d.isoformat())
            continue
        if k in ("sobreaviso", "inativo", "reserva"):
            ap = mt.icao(day.get("loc")) if day.get("loc") else (loc if k == "inativo" else None)
            recs.append({"date": d, "kind": k, "location": ap, "status": "", "data": {"source": "texto"},
                         "note": f"Importado do texto: {day['raw']}"})
            stats[k] += 1
            continue
        if k == "desconhecido":
            warns.append(f"{d:%d/%m}: não entendi “{day['text'][:50]}”; dia ficou vazio.")
            continue
        if k == "dhd":
            for n in day["numbers"]:
                o = mt.find_number(n, d, loc)
                if not o:
                    warns.append(f"{d:%d/%m}: extra DHD {n} não encontrado no RPL nessa data.")
                    stats["nao_encontrado"] += 1
                    continue
                legs.append({**o, "deadhead": True})
                prev_arr, loc = o["arr"], o["ades"]
                stats["dhd"] += 1
            continue
        for seg in day["segments"]:
            route = [mt.icao(x) for x in seg["route"]]
            for code, ic in zip(seg["route"], route):
                if not ic:
                    unknown_ap.add(code)
            if not all(route):
                continue
            for i, (a, b) in enumerate(zip(route, route[1:])):
                num = seg["numbers"][i] if i < len(seg["numbers"]) else ""
                dh = seg["dhd"][i] if i < len(seg["dhd"]) else False
                after = prev_arr + timedelta(minutes=gap if not dh else 20) if prev_arr and \
                    to_local(prev_arr, mt.tz(a)).date() >= d - timedelta(days=1) else None
                o, how = mt.find(num, a, b, d, after, allow_next=seg["plus1"] or i > 0) if num else (None, None)
                if not o:
                    o = mt.estimate(num, a, b, d, after)
                    how = "estimado"
                    warns.append(f"{d:%d/%m}: voo {num or '?'} {seg['route'][i]}-{seg['route'][i + 1]} não está no RPL; "
                                 "horário estimado pela duração do trecho.")
                elif how == "copiado":
                    o = {**o, "origin": "MANUAL"}
                    warns.append(f"{d:%d/%m}: voo {num} {seg['route'][i]}-{seg['route'][i + 1]} não opera nessa data no "
                                 f"RPL; horário copiado do dia {o['copied_from'][8:10]}/{o['copied_from'][5:7]}.")
                stats[how] += 1
                if dh:
                    stats["dhd"] += 1
                o = {**o, "deadhead": dh, "note": {"copiado": f"Escala importada: voo não opera nesta data no RPL; "
                                                              f"horário do dia {(o.get('copied_from') or '')[8:10]}/"
                                                              f"{(o.get('copied_from') or '')[5:7]}",
                                                   "estimado": "Escala importada: voo não encontrado no RPL; horário "
                                                               "estimado pela duração do trecho"}.get(how, "")}
                if after and o["dep"] < after:
                    # a escala real é a referência da ordem: horário do RPL/cópia que conflita é empurrado
                    sh = after - o["dep"]
                    sh = timedelta(minutes=-(-sh.total_seconds() // 300) * 5)
                    o = {**o, "dep": o["dep"] + sh, "arr": o["arr"] + sh,
                         "origin": "MANUAL" if o["origin"] != "SUGESTAO" else "SUGESTAO",
                         "note": "Escala importada: horário ajustado para seguir a sequência da escala real"}
                    stats["ajustado"] += 1
                legs.append(o)
                prev_arr, loc = o["arr"], b
    if mt.added:
        warns.append("Aeroporto(s) estrangeiro(s) cadastrado(s) a partir da base mundial: " + ", ".join(mt.added) + ".")
    if unknown_ap:
        warns.append("Aeroporto(s) desconhecido(s): " + ", ".join(sorted(unknown_ap)) + " (cadastre em Configurações).")
    if not legs:
        raise ImportError_("Nenhum voo reconhecido no texto." + (" " + warns[0] if warns else ""))
    # base: informada, ou onde começam as viagens (aeroporto mais comum ao sair e voltar de folga)
    b_icao = mt.icao(base) if base else None
    if not b_icao:
        offset = set(offs)
        ends = Counter()
        for o in legs:
            dd = to_local(o["dep"], mt.tz(o["adep"])).date()
            if (dd - timedelta(days=1)).isoformat() in offset or o is legs[0]:
                ends[o["adep"]] += 2
            if (dd + timedelta(days=1)).isoformat() in offset or o is legs[-1]:
                ends[o["ades"]] += 2
        b_icao = ends.most_common(1)[0][0] if ends else legs[0]["adep"]
    ops = [o for o in legs if not o.get("deadhead")]
    types = Counter(o["type_icao"] for o in ops if o.get("type_icao"))
    ac_type = types.most_common(1)[0][0] if types else None
    for r in recs:
        if r["kind"] in ("sobreaviso", "reserva") and not r["location"]:
            r["location"] = b_icao
    params = {"airline": airline, "kind": "avulsa", "start_date": d1.isoformat(), "end_date": d2.isoformat(),
              "mode": "tripulante", "imported": True, "crew_mode": "rbac117", "crew_days_off": True,
              "crew_folga_base": True, "crew_madrugada": True, "crew_monthly": True, "days_off_min": 10,
              "max_consecutive_days": 6, "report_before": crew_profiles.profile(airline)["report_min"], "release_after": 30, "base_resolved": b_icao,
              "icao_type_resolved": ac_type, "planned_days_off": sorted(offs), "type_match": "family",
              "min_turn": gap, "max_turn": int(s["max_turn_min"] or 240), "reserve_blocks": 0, "seed": 0,
              "import_stats": dict(stats)}
    diags = [{"level": "info", "code": "importada",
              "message": f"Escala importada do texto: {len(legs)} perna(s) — {stats['rpl']} no RPL na data, "
                         f"{stats['copiado']} com horário de data próxima, {stats['estimado']} estimada(s), "
                         f"{stats['dhd']} extra(s) DHD, {stats['ajustado']} com horário ajustado à sequência; "
                         f"{len(offs)} folga(s), {stats['sobreaviso']} sobreaviso(s), "
                         f"{stats['inativo']} inativo(s), {stats['reserva']} reserva(s)."}]
    diags += [{"level": "warning", "code": "importacao", "message": w} for w in warns[:40]]
    nm = (name or "").strip() or f"{airline} escala real — {d1:%d/%m} a {d2:%d/%m/%Y}"
    ts = now_iso()
    sid = db.execute(
        "INSERT INTO schedules(name, airline, kind, start_date, end_date, aircraft_id, params, diagnostics,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (nm, airline, "avulsa", d1.isoformat(), d2.isoformat(), None, json.dumps(params, ensure_ascii=False, default=str),
         json.dumps(diags, ensure_ascii=False), ts, ts)).lastrowid
    for o in legs:
        o.setdefault("ref_date", None)
        o.setdefault("flight_id", None)
        o.setdefault("eet_min", int((o["arr"] - o["dep"]).total_seconds() // 60))
    _insert_legs(db, sid, legs, None, ac_type=ac_type)
    save_days(db, sid, recs)
    return {"id": sid, "legs": len(legs), "stats": dict(stats), "warnings": warns, "base": b_icao, "airline": airline}
