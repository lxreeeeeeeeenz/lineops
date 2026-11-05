"""Pedido de troca de escala: o piloto pede, a companhia responde depois, e nem sempre aprova.

Dois pedidos, como se faz numa companhia de verdade:
* TROCA — sair de uma perna e entrar em outro voo no MESMO trecho e no mesmo dia (local); ou trocar o bate-volta inteiro
  (ida e volta) por outro par de voos no mesmo trecho. Os voos oferecidos são só os do RPL da companhia, o mesmo que o motor
  do AeroScale usa: nada de voo inventado. Voo de dia sem RPL publicado vem marcado PROJETADO, igual no AeroScale.
* TROCA com DESTINO escolhido — você escolhe para onde quer ir: no lugar do ciclo que contém a perna (ele sai e volta ao
  mesmo aeroporto), entra uma ida e volta real do RPL para o destino escolhido, nos mesmos dias (com pernoite lá, se o
  ciclo tinha pernoite) e sem invadir a perna anterior nem a seguinte.
* LIBERACAO — sair de uma ou mais pernas sem entrar em outra. Só é possível quando as pernas fecham um ciclo (saem e voltam
  ao mesmo aeroporto, ex.: bate-volta), senão você ficaria fora de posição para o resto da escala.

Antes de virar pedido, tudo passa pelas regras de verdade:
* conexão mínima com a perna anterior e a seguinte (e entre a ida e a volta): o swap_min do AeroScale, a conexão mínima de
  troca de aeronave (padrão 50 min);
* jornada, repouso, madrugadas e limites do mês: crew.analyze do AeroScale (Lei 13.475 / RBAC 117) roda na escala como
  ficaria depois da troca. Se a troca cria uma violação nova, ela aparece riscada, com o motivo.

A CHANCE de aprovação é uma regra de jogo da LineOps, montada com fatores que pesam numa escala real (antecedência,
reserva para cobrir, dia de pico, quantos pedidos você já fez, pontualidade). Os números são do jogo, não estatística de
nenhuma companhia, e a tela mostra cada fator. A resposta chega em minutos, como mensagem da equipe:
* 3 dias ou mais antes: Planejamento de Escala, de 3 a 12 min;
* menos de 3 dias: Controle de Tripulantes (CCO), de 1 a 6 min.
Aprovado, a escala muda na hora (com histórico, como uma alteração de escala) e a reserva do voo antigo é cancelada.
Se o AeroScale mandar a mesma escala de novo, a troca continua valendo; se ele mudar aquela perna, vale a escala nova.
"""
from __future__ import annotations

import itertools
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import rules
from .db import audit, jloads, now_iso, one, rows, tx
from .errors import Conflict, FGVError, NotFound

FMT = "%Y-%m-%dT%H:%M:%SZ"
BASE = {"TROCA": 75, "LIBERACAO": 55}
MIN_CHANCE, MAX_CHANCE = 5, 95
FLOWN = ("EM_VOO", "PIREP_RECEBIDO", "APROVADO", "REJEITADO")
RESPONSE_MIN = {"escala": (3, 12), "tripulantes": (1, 6)}    # minutos até a resposta
MAX_DEST_CHAINS = 40                # idas e voltas conferidas por destino escolhido
DEST_LEGAL_PEEK = 3                 # na lista de destinos, para de conferir depois de achar 3 que cabem
MAX_RELEASE_LEGS = 10
MAX_RELEASE_DAYS = 5
DEST_MAX_H = 72                     # no destino escolhido, até 3 dias (se a escala deixar)
MAX_VIA_LEGS = 4                    # para chegar ao destino escolhido, saem no máximo 4 pernas em sequência
MAX_BLOCK_SWAP = 3                  # trocar o bloco inteiro só até 3 pernas (bate-volta ou triangular)
MAX_OPTIONS = 14
DISCLAIMER = ("A chance é uma regra de jogo da LineOps (antecedência, reserva, pico, histórico de pedidos e pontualidade). "
              "Não é estatística de nenhuma companhia aérea.")


def _p(s: str | None) -> datetime | None:
    try:
        return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc) if s else None
    except ValueError:
        return None


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime(FMT)


def _fn(l: dict) -> str:
    return l.get("flight_number") or l.get("ident") or f"{l['adep']}→{l['ades']}"


def _hl(conn, utc_iso: str, icao: str) -> str:
    """'14/11 07:40 (10:40Z)' na hora local do aeroporto, quando o fuso é conhecido."""
    d = _p(utc_iso)
    loc = rules.local_dt(conn, d, icao)
    return f"{loc:%d/%m %H:%M} ({d:%H:%M}Z)" if loc else f"{d:%d/%m %H:%MZ}"


def _ld(l: dict) -> str:
    return l.get("local_date") or l["dep_utc"][:10]


# ------------------------------------------------------------------ escala ao redor da perna
_SEL = """SELECT l.*, f.id AS flight_id, f.state AS flight_state FROM roster_leg l
          LEFT JOIN flight f ON f.leg_id=l.id AND f.state<>'CANCELADO'"""


def _flights(conn, around: datetime, days: int = 7) -> list[dict]:
    """Pernas de voo (sem sobreaviso), vivas, ao redor da data, com o estado da reserva de cada uma."""
    a, b = _iso(around - timedelta(days=days)), _iso(around + timedelta(days=days))
    return rows(conn.execute(_SEL + """ WHERE l.removed_batch IS NULL AND l.activity<>'SOBREAVISO' AND l.dep_utc BETWEEN ? AND ?
                                        ORDER BY l.dep_utc, l.seq""", (a, b)))


def _leg(conn, leg_id: int) -> dict:
    l = one(conn.execute(_SEL + " WHERE l.id=?", (leg_id,)))
    if not l or l["removed_batch"]:
        raise NotFound("Perna não encontrada na escala atual.")
    return l


def _params(conn, leg: dict) -> dict:
    from .journey import schedule_params
    p, _s = schedule_params(conn, _ld(leg))
    return p


def _report(conn, leg: dict, fl: list[dict]) -> datetime:
    """Apresentação da jornada da perna (1ª perna do dia local menos o report_before da escala, ou o perfil da companhia)."""
    from .journey import _duty_times
    day = [x for x in fl if _ld(x) == _ld(leg)] or [leg]
    dt = _duty_times(_params(conn, leg), day[0]["dep_utc"], None, leg.get("airline"))
    return _p(dt["report_utc"]) or (_p(day[0]["dep_utc"]) - timedelta(minutes=60))


def _pending_for(conn, leg_id: int) -> dict | None:
    for r in rows(conn.execute("SELECT id, leg_ids FROM swap_request WHERE state='PENDENTE'")):
        if leg_id in jloads(r["leg_ids"], []):
            return r
    return None


def _blocked(conn, leg: dict, fl: list[dict], now: datetime) -> str | None:
    if leg["activity"] == "SOBREAVISO":
        return "Sobreaviso não é perna de voo: não há o que trocar."
    if leg.get("flight_state") in FLOWN:
        return "Este voo já foi voado (ou está em voo)."
    if now >= _report(conn, leg, fl):
        return "A apresentação desta jornada já passou: a partir daqui, só o CCO no aeroporto resolve."
    pend = _pending_for(conn, leg["id"])
    if pend:
        return f"Já existe um pedido aberto para esta perna (#{pend['id']})."
    return None


# ------------------------------------------------------------------ regras: conexão e jornada (AeroScale)
def _connection(conn, fl: list[dict], replaced: list[dict], new: list[dict], swap_min: int) -> str | None:
    """As pernas novas cabem entre a anterior e a seguinte da escala (e entre si)? Conexão mínima de troca de aeronave."""
    ids = {x["id"] for x in replaced}
    rest = [x for x in fl if x["id"] not in ids]
    t0 = _p(min(x["dep_utc"] for x in replaced))
    prev = [x for x in rest if _p(x["dep_utc"]) < t0]
    nxt = [x for x in rest if _p(x["dep_utc"]) >= t0]
    for a, b in zip(new, new[1:]):
        gap = (_p(b["dep_utc"]) - _p(a["arr_utc"])).total_seconds() / 60
        if gap < swap_min:
            return f"Entre o {_fn(a)} e o {_fn(b)} ficariam {int(gap)} min; a conexão mínima para trocar de aeronave é {swap_min} min."
    first, last = new[0], new[-1]
    # o mesmo horário da perna que sai (ex.: ir de extra no próprio voo): essa conexão já estava na escala
    keep_prev = any(x["dep_utc"] == first["dep_utc"] and x["adep"] == first["adep"] for x in replaced)
    keep_next = any(x["arr_utc"] == last["arr_utc"] and x["ades"] == last["ades"] for x in replaced)
    if prev and not keep_prev:
        pv = prev[-1]
        gap = (_p(first["dep_utc"]) - _p(pv["arr_utc"])).total_seconds() / 60
        if gap < 0:
            return f"Sairia antes de o {_fn(pv)} chegar ({_hl(conn, pv['arr_utc'], pv['ades'])})."
        if pv["ades"] == first["adep"] and gap < swap_min:
            return f"Só {int(gap)} min depois do {_fn(pv)}; a conexão mínima para trocar de aeronave é {swap_min} min."
    if nxt and not keep_next:
        nx = nxt[0]
        gap = (_p(nx["dep_utc"]) - _p(last["arr_utc"])).total_seconds() / 60
        if gap < 0:
            return (f"Chegaria às {_hl(conn, last['arr_utc'], last['ades'])}, depois da partida do {_fn(nx)} "
                    f"({_hl(conn, nx['dep_utc'], nx['adep'])}).")
        if nx["adep"] == last["ades"] and gap < swap_min:
            return f"Só {int(gap)} min antes do {_fn(nx)}; a conexão mínima para trocar de aeronave é {swap_min} min."
    return None


def _period_legs(conn, leg: dict) -> tuple[list[dict], dict, tuple]:
    from .journey import schedule_params
    p, s = schedule_params(conn, _ld(leg))
    start = (s or {}).get("start_date") or leg["dep_utc"][:10]
    end = (s or {}).get("end_date") or leg["dep_utc"][:10]
    from . import escala
    legs = escala.crew_legs(conn, start, end)
    return legs, p, (start, end)


def _errors(conn, legs: list[dict], p: dict, period) -> list[str] | None:
    from datetime import date
    base = p.get("base_resolved") or p.get("base")
    fam = rules.type_family(p.get("icao_type_resolved") or p.get("icao_type"))
    rdays = {r["start_date"] for r in conn.execute("SELECT start_date FROM roster_reserve")}
    try:
        rep = rules.crew_report(conn, sorted(legs, key=lambda x: x["dep_utc"]), p, base, fam,
                                (date.fromisoformat(period[0]), date.fromisoformat(period[1])), rdays)
    except Exception:                                            # noqa: BLE001 — regra indisponível: diz, não inventa
        return None
    out = [i["message"] for d in rep["duties"] for i in d["issues"] if i["level"] == "error"]
    return out + [v["message"] for v in rep["violations"] if v["level"] == "error"]


def legality(conn, leg: dict, remove_ids: set, new: list[dict]) -> dict:
    """Roda o crew.analyze do AeroScale na escala como está e como ficaria. Violação nova = não pode."""
    legs, p, period = _period_legs(conn, leg)
    before = _errors(conn, legs, p, period)
    # data_origin conta: etapa internacional longa observada no ADS-B leva tripulação composta (crew.analyze do AeroScale)
    add = [{**n, "id": -1 - k, "uid": f"novo{k}", "activity": n.get("activity") or leg["activity"],
            "data_origin": n.get("data_origin") or n.get("origin")} for k, n in enumerate(new)]
    after = _errors(conn, [x for x in legs if x["id"] not in remove_ids] + add, p, period) if before is not None else None
    if before is None or after is None:
        return {"ok": True, "checked": False, "new_errors": [],
                "note": "As regras de jornada do AeroScale não puderam rodar agora; a companhia confere na resposta."}
    if len(after) <= len(before):
        return {"ok": True, "checked": True, "new_errors": []}
    new_err = [m for m in after if m not in before] or after[len(before):]
    return {"ok": False, "checked": True, "new_errors": [m.rstrip(". ") for m in new_err[:3]]}


# ------------------------------------------------------------------ voos do RPL
def _index(data_dir, airline: str):
    from . import motor
    from .aeroscale import rpl_service
    from .aeroscale.db import get_settings
    c = motor.connect(data_dir)
    try:
        taxi = int(get_settings(c).get("taxi_allowance_min") or 0)
        return rpl_service.get_index(c, airline, taxi)
    finally:
        c.close()


def _same_pair(conn, idx, leg: dict, want: str | None, mode: str) -> list[dict]:
    """Voos do RPL no mesmo trecho e no mesmo dia local da perna, de tipo compatível com a sua habilitação (inclui ela)."""
    d0, out, seen = _p(leg["dep_utc"]), [], set()
    for k in (-1, 0, 1):
        for o in idx.occurrences((d0 + timedelta(days=k)).date(), projection=True):
            if o["adep"] != leg["adep"] or o["ades"] != leg["ades"]:
                continue
            if want and not rules.types_compatible(want, o["type_icao"], mode):
                continue
            dep = _iso(o["dep"])
            ld = rules.local_date(conn, o["dep"], o["adep"])
            if ld != _ld(leg) or (o["ident"], dep) in seen:
                continue
            seen.add((o["ident"], dep))
            out.append({"ref": o["ref"], "ident": o["ident"], "flight_number": o["ident"], "type_icao": o["type_icao"],
                        "adep": o["adep"], "ades": o["ades"], "dep_utc": dep, "arr_utc": _iso(o["arr"]), "eet_min": o["eet_min"],
                        "origin": o["origin"], "ref_date": o.get("ref_date"), "local_date": ld})
    out.sort(key=lambda x: x["dep_utc"])
    return out


def _is_same(o: dict, leg: dict) -> bool:
    return o["dep_utc"] == leg["dep_utc"] and (o["ident"] == leg.get("flight_number") or not leg.get("flight_number"))


def _mk(conn, o: dict, activity: str | None = None) -> dict:
    """Ocorrência do RPL no formato de perna."""
    return {"ref": o["ref"], "ident": o["ident"], "flight_number": o["ident"], "type_icao": o["type_icao"], "adep": o["adep"],
            "ades": o["ades"], "dep_utc": _iso(o["dep"]), "arr_utc": _iso(o["arr"]), "eet_min": o["eet_min"], "origin": o["origin"],
            "ref_date": o.get("ref_date"), "local_date": rules.local_date(conn, o["dep"], o["adep"]), "activity": activity}


def _occ_between(idx, a: datetime, b: datetime) -> list[dict]:
    out, d = [], (a - timedelta(days=1)).date()
    while d <= (b + timedelta(days=1)).date():
        out += idx.occurrences(d, projection=True)
        d += timedelta(days=1)
    return out


def _get_index(data_dir, airline):
    if not data_dir:
        return None, "Motor de escalas indisponível."
    try:
        idx = _index(data_dir, airline)
    except Exception as e:                                       # noqa: BLE001
        return None, f"Não consegui ler o RPL da companhia ({e})."
    if not idx.flights:
        return None, "Sem RPL desta companhia no motor."
    return idx, None


class _NoCargo:
    """O índice do RPL sem os voos de carga (GOL 9xxx), quando a escala não inclui carga."""

    def __init__(self, idx, airline):
        self.idx, self.airline = idx, airline

    def __getattr__(self, k):
        return getattr(self.idx, k)

    def occurrences(self, d, projection=True):
        from . import cargo
        return [o for o in self.idx.occurrences(d, projection=projection) if not cargo.is_cargo(self.airline, o.get("ident"))]


def _idx_for(data_dir, leg: dict, p: dict):
    from . import cargo
    idx, err = _get_index(data_dir, leg["airline"])
    if idx is not None and cargo.applies(leg["airline"], p.get("include_cargo")):
        idx = _NoCargo(idx, leg["airline"])
    return idx, err


def raw_options(conn, data_dir, leg: dict, fl: list[dict], p: dict, dest: str | None = None) -> tuple[list[dict], str | None]:
    """Opções de troca: [{'replaces': [pernas que saem], 'legs': [voos do RPL que entram]}]. Primeiro só a perna; depois o bloco.
    Com dest: ida e volta (ou passagem) pelo destino escolhido."""
    idx, err = _idx_for(data_dir, leg, p)
    if err:
        return [], err
    if dest:
        opts = _dest_chains(conn, idx, fl, leg, p, dest).get(dest, [])
        if not opts:
            return [], f"Nenhum voo real para {dest} e de volta cabe na sua escala nesses dias."
        return opts[:MAX_DEST_CHAINS], None
    mode = p.get("type_match") or "family"
    want = lambda l: l.get("type_icao") or l.get("planned_type")
    out = [{"replaces": [leg], "legs": [o]} for o in _same_pair(conn, idx, leg, want(leg), mode) if not _is_same(o, leg)]
    blk = release_block(fl, leg["id"])
    if blk and 2 <= len(blk) <= MAX_BLOCK_SWAP:
        lists = [_same_pair(conn, idx, b, want(b), mode) for b in blk]
        combos = []
        for combo in itertools.product(*lists):
            if sum(not _is_same(o, b) for o, b in zip(combo, blk)) < 2:
                continue                                        # o próprio bloco, ou só uma perna mudando (essa é a troca simples)
            if any(_p(y["dep_utc"]) < _p(x["arr_utc"]) for x, y in zip(combo, combo[1:])):
                continue
            combos.append(list(combo))
        combos.sort(key=lambda c: c[0]["dep_utc"])
        out += [{"replaces": blk, "legs": c} for c in combos[:MAX_OPTIONS]]
    return out, None


# ------------------------------------------------------------------ escolher o destino
def _window(fl: list[dict], blk: list[dict], swap_min: int) -> tuple[datetime | None, datetime | None]:
    """Entre o que fica antes e o que fica depois do bloco (com a conexão mínima de troca de aeronave)."""
    ids = {x["id"] for x in blk}
    rest = [x for x in fl if x["id"] not in ids]
    t0 = _p(blk[0]["dep_utc"])
    prev = [x for x in rest if _p(x["dep_utc"]) < t0]
    nxt = [x for x in rest if _p(x["dep_utc"]) >= t0]
    lo = _p(prev[-1]["arr_utc"]) + timedelta(minutes=swap_min) if prev else None
    hi = _p(nxt[0]["dep_utc"]) - timedelta(minutes=swap_min) if nxt else None
    return lo, hi


def _frames(fl: list[dict], leg: dict) -> list[dict]:
    """O que pode sair para dar lugar ao destino:
    * o ciclo que volta ao mesmo aeroporto (se houver): ida e volta para o destino no lugar dele;
    * a perna e, se preciso, as seguintes (até 4, em sequência, em até 3 dias): você vai ao destino e de lá segue direto para
      onde a escala continua. Ex.: no lugar de GIG→VIT e VIT→GRU, entra GIG→JPA e JPA→GRU."""
    out = []
    blk = release_block(fl, leg["id"])
    if blk:
        out.append({"mode": "ciclo", "replaces": blk})
    ids = [x["id"] for x in fl]
    i = ids.index(leg["id"]) if leg["id"] in ids else None
    if i is None:
        return out + [{"mode": "via", "replaces": [leg]}]
    for k in range(i, min(len(fl), i + MAX_VIA_LEGS)):
        seg = fl[i:k + 1]
        if k > i and seg[-2]["ades"] != seg[-1]["adep"]:
            break
        if (_p(seg[-1]["arr_utc"]) - _p(seg[0]["dep_utc"])).total_seconds() > DEST_MAX_H * 3600:
            break
        if blk and [x["id"] for x in seg] == [x["id"] for x in blk]:
            continue
        out.append({"mode": "via", "replaces": seg})
    return out


def _dest_chains(conn, idx, fl: list[dict], leg: dict, p: dict, dest: str | None = None) -> dict[str, list[dict]]:
    """Voos reais do RPL: origem → destino → onde a escala continua, sem invadir a perna anterior nem a seguinte.
    Fica até 3 dias no destino se a escala deixar (as regras de jornada e repouso decidem depois)."""
    swap_min = int(p.get("swap_min") or 50)
    mode = p.get("type_match") or "family"
    out: dict[str, list[dict]] = {}
    seen = set()
    for fr in _frames(fl, leg):
        rep = fr["replaces"]
        S, E = rep[0]["adep"], rep[-1]["ades"]
        want = rep[0].get("type_icao") or rep[0].get("planned_type")
        lo, hi = _window(fl, rep, swap_min)
        t0, t1 = _p(rep[0]["dep_utc"]), _p(rep[-1]["arr_utc"])
        lo = max(lo, t0 - timedelta(hours=12)) if lo else t0 - timedelta(hours=12)
        cap = max(t1, t0 + timedelta(hours=DEST_MAX_H))
        hi = min(hi, cap) if hi else cap
        outs, rets = {}, {}
        for o in _occ_between(idx, lo, hi):
            if want and not rules.types_compatible(want, o["type_icao"], mode):
                continue
            if o["dep"] < lo or o["arr"] > hi:
                continue
            if o["adep"] == S and o["ades"] not in (S, E) and (not dest or o["ades"] == dest):
                outs.setdefault(o["ades"], []).append(o)
            elif o["ades"] == E and o["adep"] not in (S, E) and (not dest or o["adep"] == dest):
                rets.setdefault(o["adep"], []).append(o)
        for D, os_ in outs.items():
            for a in sorted(os_, key=lambda x: x["dep"]):
                for b in sorted(rets.get(D, []), key=lambda x: x["dep"]):
                    if b["dep"] < a["arr"] + timedelta(minutes=swap_min):
                        continue
                    key = (a["ref"], b["ref"], tuple(x["id"] for x in rep))
                    if key in seen:
                        continue
                    seen.add(key)
                    out.setdefault(D, []).append({"replaces": rep, "legs": [_mk(conn, a), _mk(conn, b)], "dest": D, "mode": fr["mode"]})
    for D in out:
        out[D].sort(key=lambda o: (o["mode"] != "ciclo", o["legs"][0]["dep_utc"], o["legs"][1]["dep_utc"]))
    return out


def destinations(conn, data_dir, leg_id: int, now: str | None = None) -> dict:
    """Para onde dá para ir: destinos com voos reais do RPL que cabem na sua escala (jornada e repouso conferidos)."""
    nowd = _p(now or now_iso())
    leg = _leg(conn, leg_id)
    fl = _flights(conn, _p(leg["dep_utc"]))
    p = _params(conn, leg)
    bl = _blocked(conn, leg, fl, nowd)
    if bl:
        return {"blocked": bl, "items": []}
    idx, err = _idx_for(data_dir, leg, p)
    if err:
        return {"blocked": None, "items": [], "why": err}
    swap_min = int(p.get("swap_min") or 50)
    items = []
    for D, opts in _dest_chains(conn, idx, fl, leg, p).items():
        good = []
        for o in opts[:MAX_DEST_CHAINS]:                         # só entra na lista o destino com opção que cabe de verdade
            if any(_blocked(conn, x, fl, nowd) for x in o["replaces"] if x["id"] != leg["id"]):
                continue
            if _connection(conn, fl, o["replaces"], o["legs"], swap_min) or not legality(conn, leg, {x["id"] for x in o["replaces"]}, o["legs"])["ok"]:
                continue
            good.append(o)
            if len(good) >= DEST_LEGAL_PEEK:
                break
        if not good:
            continue
        ap = rules.airport(conn, D) or {}
        items.append({"icao": D, "city": ap.get("city") or ap.get("name"), "name": ap.get("name"), "country": ap.get("country"),
                      "options": len(good), "more": len(good) >= DEST_LEGAL_PEEK,
                      "overnight": any(o["legs"][0]["local_date"] != o["legs"][1]["local_date"] for o in good),
                      "current": False, "first_dep": good[0]["legs"][0]["dep_label"] if "dep_label" in good[0]["legs"][0] else
                      _hl(conn, good[0]["legs"][0]["dep_utc"], good[0]["legs"][0]["adep"])})
    items.sort(key=lambda x: (-(x["options"]), x["city"] or x["icao"]))
    return {"blocked": None, "items": items, "origin": leg["adep"], "continues_at": leg["ades"],
            "why": None if items else "Nenhum voo real da companhia sai daqui para outro destino e chega a tempo de seguir a sua escala "
                                      "dentro dos limites de jornada e repouso.",
            "rule": "Voos reais do RPL: você sai de " + leg["adep"] + " para o destino e de lá segue para onde a sua escala continua, "
                    "sem invadir a perna anterior nem a seguinte. Pode ficar até 3 dias no destino, se jornada e repouso deixarem."}


def resolve_airport(conn, code: str) -> str | None:
    """ICAO (SBJP) ou IATA (JPA)."""
    code = (code or "").strip().upper()
    if len(code) == 4:
        return code if rules.airport(conn, code) else None
    if len(code) == 3:
        hit = next((k for k, v in rules.airport_map(conn).items() if (v.get("iata") or "").upper() == code), None)
        if not hit:                                   # fora do Brasil (ex.: BOG): o catálogo mundial do AeroScale
            from .aeroscale.airport_geo_world import WORLD_AIRPORTS
            hit = next((k for k, v in WORLD_AIRPORTS.items() if (v[0] or "").upper() == code), None)
        return hit
    return None


def explain_destination(conn, data_dir, leg_id: int, code: str, now: str | None = None) -> dict:
    """Por que dá (ou não dá) para ir a um destino que você digitou. Diz exatamente onde parou."""
    nowd = _p(now or now_iso())
    leg = _leg(conn, leg_id)
    D = resolve_airport(conn, code)
    ap = rules.airport(conn, D) if D else None
    name = (f"{D} ({ap.get('city') or ap.get('name')})" if ap and (ap.get("city") or ap.get("name")) else D) if D else code
    if not D:
        return {"ok": False, "icao": None, "why": f"Não achei o aeroporto {code}. Use o código ICAO (ex.: SBJP) ou IATA (ex.: JPA)."}
    fl = _flights(conn, _p(leg["dep_utc"]))
    p = _params(conn, leg)
    bl = _blocked(conn, leg, fl, nowd)
    if bl:
        return {"ok": False, "icao": D, "why": bl}
    idx, err = _idx_for(data_dir, leg, p)
    if err:
        return {"ok": False, "icao": D, "why": err}
    S = leg["adep"]
    cf, ct = idx.coverage()
    rpl = f"no RPL ({cf[8:10]}/{cf[5:7]} a {ct[8:10]}/{ct[5:7]})" if cf and ct else "no RPL"
    touches = [f for f in idx.flights if D in (f["adep"], f["ades"])]
    if not touches:
        return {"ok": False, "icao": D, "why": f"A companhia não tem nenhum voo para {name} {rpl}. "
                                               "A LineOps só oferece voos que existem de verdade; quando o RPL novo trouxer, aparece."}
    if not [f for f in touches if f["adep"] == S]:
        return {"ok": False, "icao": D, "why": f"A companhia voa para {name}, mas não sai de {S} para lá {rpl}."}
    opts = _dest_chains(conn, idx, fl, leg, p, D).get(D, [])
    if not opts:
        conts = sorted({fr["replaces"][-1]["ades"] for fr in _frames(fl, leg)})
        backs = sorted({f["ades"] for f in touches if f["adep"] == D})
        return {"ok": False, "icao": D, "why": f"Tem voo {S}→{D}, mas de {name} não há voo direto de volta para onde a sua escala "
                                               f"continua ({', '.join(conts)}) a tempo, nos próximos {DEST_MAX_H // 24} dias. "
                                               f"De {D} a companhia segue para: {', '.join(backs[:12]) or 'nenhum lugar no RPL'}."}
    swap_min = int(p.get("swap_min") or 50)
    first_why = None
    for o in opts[:MAX_DEST_CHAINS]:
        why = _connection(conn, fl, o["replaces"], o["legs"], swap_min)
        if not why:
            lg = legality(conn, leg, {x["id"] for x in o["replaces"]}, o["legs"])
            if lg["ok"]:
                return {"ok": True, "icao": D, "why": None}
            why = "passaria do limite de jornada/repouso: " + "; ".join(lg["new_errors"])
        first_why = first_why or why
    return {"ok": False, "icao": D, "why": f"Há voos para {name} e de volta, mas nenhum cabe: {first_why}"}


def _opt_ref(o: dict) -> str:
    return "+".join(x["ref"] for x in o["legs"]) + "|" + ",".join(str(x["id"]) for x in o["replaces"])


# ------------------------------------------------------------------ liberação
def release_block(fl: list[dict], leg_id: int) -> list[dict] | None:
    """O menor ciclo (sai e volta ao mesmo aeroporto) que contém a perna."""
    ids = [x["id"] for x in fl]
    if leg_id not in ids:
        return None
    i = ids.index(leg_id)
    best = None
    for a in range(max(0, i - MAX_RELEASE_LEGS + 1), i + 1):
        for b in range(i, min(len(fl), a + MAX_RELEASE_LEGS)):
            seg = fl[a:b + 1]
            if any(seg[k]["ades"] != seg[k + 1]["adep"] for k in range(len(seg) - 1)):
                break
            if seg[0]["adep"] != seg[-1]["ades"]:
                continue
            if (_p(seg[-1]["arr_utc"]) - _p(seg[0]["dep_utc"])).days > MAX_RELEASE_DAYS:
                continue
            if best is None or len(seg) < len(best):
                best = seg
    return best


def release_options(conn, data_dir, leg: dict, fl: list[dict], p: dict) -> list[dict]:
    """Sair da perna. Se ela fecha um ciclo, saem as etapas do ciclo e pronto. Sempre dá também para ir de EXTRA
    (passageiro, sem voar) até onde a escala continua: no próprio voo ou em outro voo real do mesmo trecho."""
    out = []
    blk = release_block(fl, leg["id"])
    if blk:
        out.append({"replaces": blk, "legs": [], "mode": "ciclo"})
    own = {"ref": f"escala:{leg['id']}", "ident": leg.get("flight_number") or "", "flight_number": leg.get("flight_number") or "",
           "type_icao": leg.get("type_icao"), "adep": leg["adep"], "ades": leg["ades"], "dep_utc": leg["dep_utc"], "arr_utc": leg["arr_utc"],
           "eet_min": leg.get("eet_min"), "origin": leg.get("data_origin"), "ref_date": None, "local_date": _ld(leg), "activity": "EXTRA"}
    out.append({"replaces": [leg], "legs": [own], "mode": "extra"})
    idx, _err = _idx_for(data_dir, leg, p)
    if idx:
        for o in _same_pair(conn, idx, leg, None, "family"):
            if not _is_same(o, leg):
                out.append({"replaces": [leg], "legs": [{**o, "activity": "EXTRA"}], "mode": "extra"})
    return out


# ------------------------------------------------------------------ a chance (regra de jogo, mostrada fator a fator)
def chance(conn, kind: str, legs: list[dict], now: datetime, new: list[dict] | None = None, *, exclude_id=None,
           dest: str | None = None, folga: list[str] | None = None, extra: bool = False) -> tuple[int, list[dict]]:
    first = min(legs, key=lambda x: x["dep_utc"])
    hrs = (_p(first["dep_utc"]) - now).total_seconds() / 3600
    f = [{"key": "base", "label": ("Troca por outro destino" if dest else "Troca por outro voo no mesmo trecho") if kind == "TROCA"
          else ("Sair da perna e ir de extra (a reserva cobre o voo)" if extra else "Sair da perna (a reserva cobre)"),
          "delta": BASE[kind], "base": True}]
    if hrs >= 168:
        f.append({"key": "antecedencia", "label": "Mais de 7 dias antes: a programação ainda está aberta no Planejamento", "delta": 10})
    elif hrs >= 72:
        f.append({"key": "antecedencia", "label": "Entre 3 e 7 dias antes: prazo normal", "delta": 0})
    elif hrs >= 24:
        f.append({"key": "antecedencia", "label": "Menos de 72 h: vai para o CCO, com menos margem", "delta": -15})
    else:
        f.append({"key": "antecedencia", "label": "Menos de 24 h: perto da operação, só com reserva sobrando", "delta": -35})
    since = _iso(now - timedelta(days=30))
    n = conn.execute("SELECT COUNT(*) FROM swap_request WHERE state<>'CANCELADO' AND created_at>=? AND id IS NOT ?",
                     (since, exclude_id)).fetchone()[0]
    if n:
        f.append({"key": "pedidos", "label": f"{n} pedido(s) seu(s) nos últimos 30 dias", "delta": -min(35, 7 * n), "n": n})
    loc = rules.local_dt(conn, _p(first["dep_utc"]), first["adep"]) or _p(first["dep_utc"])
    if loc.weekday() in (4, 6):
        f.append({"key": "pico", "label": f"{'Sexta' if loc.weekday() == 4 else 'Domingo'}: dia de pico, reserva mais disputada", "delta": -8})
    if len(legs) > 1:
        per = 6 if kind == "LIBERACAO" else 4
        f.append({"key": "etapas", "label": (f"{len(legs)} etapas para a reserva cobrir" if kind == "LIBERACAO"
                                             else f"{len(legs)} etapas para acertar com quem voa os outros voos"),
                  "delta": -per * (len(legs) - 1), "n": len(legs)})
    if folga:
        f.append({"key": "folga", "label": "Cai na sua folga de " + ", ".join(f"{d[8:10]}/{d[5:7]}" for d in folga), "delta": -15,
                  "days": folga})
    if dest:
        f.append({"key": "destino", "label": f"Destino escolhido por você ({dest}): precisa de alguém topando trocar", "delta": -10,
                  "dest": dest})
    if new and any(x.get("origin") == "PROJETADO" for x in new):
        f.append({"key": "projetado", "label": "Voo PROJETADO (sem RPL publicado para o dia): a malha ainda pode mudar", "delta": -10})
    try:
        from . import carreira
        o = carreira.otp(conn)
        if o["flights"] >= 10 and o["dep_otp"] is not None:
            if o["dep_otp"] >= 85:
                f.append({"key": "otp", "label": f"Sua pontualidade ({o['dep_otp']}% OTP15) conta a favor", "delta": 5})
            elif o["dep_otp"] < 60:
                f.append({"key": "otp", "label": f"Pontualidade baixa ({o['dep_otp']}% OTP15)", "delta": -5})
    except Exception:                                            # noqa: BLE001
        pass
    total = max(MIN_CHANCE, min(MAX_CHANCE, sum(x["delta"] for x in f)))
    return total, f


# ------------------------------------------------------------------ tela: o que dá para pedir nesta perna
def _brief(conn, l: dict) -> dict:
    return {"id": l.get("id"), "flight_number": _fn(l), "adep": l["adep"], "ades": l["ades"], "dep_utc": l["dep_utc"],
            "arr_utc": l["arr_utc"], "dep_label": _hl(conn, l["dep_utc"], l["adep"]), "arr_label": _hl(conn, l["arr_utc"], l["ades"]),
            "type_icao": l.get("type_icao"), "origin": l.get("origin") or l.get("data_origin")}


def options(conn, data_dir, leg_id: int, now: str | None = None, dest: str | None = None) -> dict:
    nowd = _p(now or now_iso())
    leg = _leg(conn, leg_id)
    fl = _flights(conn, _p(leg["dep_utc"]))
    p = _params(conn, leg)
    swap_min = int(p.get("swap_min") or 50)
    out = {"leg": _brief(conn, leg), "leg_label": f"{_fn(leg)} {leg['adep']}→{leg['ades']} · {_hl(conn, leg['dep_utc'], leg['adep'])}",
           "booked": leg.get("flight_state") == "RESERVADO", "blocked": _blocked(conn, leg, fl, nowd),
           "swap_min": swap_min, "disclaimer": DISCLAIMER, "troca": {"options": [], "note": None}, "liberacao": None,
           "answer_by": None}
    if out["blocked"]:
        return out
    first_h = (_p(leg["dep_utc"]) - nowd).total_seconds() / 3600
    out["answer_by"] = ("Planejamento de Escala, em alguns minutos" if first_h >= 72
                        else "Controle de Tripulantes · CCO, em poucos minutos")
    dest = resolve_airport(conn, dest) if dest else None
    out["dest"] = dest
    raw, note = raw_options(conn, data_dir, leg, fl, p, dest)
    out["troca"]["note"] = note
    off = set(p.get("planned_days_off") or [])
    out["troca"]["options"] = [_evaluate(conn, leg, fl, o, "TROCA", nowd, swap_min, off) for o in raw]
    out["troca"]["options"].sort(key=lambda x: (not x["ok"], x["block"], x["legs"][0]["dep_utc"]))
    if not raw and not note:
        out["troca"]["note"] = "Não há outro voo da companhia neste trecho, neste dia, para o seu tipo de avião."
    if dest:
        return out                                              # tela do destino: só as opções para ele
    lo = [_evaluate(conn, leg, fl, o, "LIBERACAO", nowd, swap_min, off) for o in release_options(conn, data_dir, leg, fl, p)]
    lo.sort(key=lambda x: (not x["ok"], x["mode"] != "ciclo", x["legs"][0]["dep_utc"] if x["legs"] else ""))
    out["liberacao"] = {"options": lo, "ok": any(x["ok"] for x in lo),
                        "why": None if any(x["ok"] for x in lo) else (lo[0]["why"] if lo else "Não é possível sair desta perna.")}
    return out


def _evaluate(conn, leg: dict, fl: list[dict], o: dict, kind: str, nowd: datetime, swap_min: int, off: set) -> dict:
    """Uma opção conferida: perna da escala bloqueada, conexão, jornada e repouso (AeroScale), e a chance."""
    why = next((f"{_fn(x)}: {_blocked(conn, x, fl, nowd)}" for x in o["replaces"] if x["id"] != leg["id"] and _blocked(conn, x, fl, nowd)), None)
    if not why and o["legs"]:
        why = _connection(conn, fl, o["replaces"], o["legs"], swap_min)
    lg = None if why else legality(conn, leg, {x["id"] for x in o["replaces"]}, o["legs"])
    if lg and not lg["ok"]:
        why = "Passaria do limite de jornada/repouso: " + "; ".join(lg["new_errors"])
    folga = sorted({x["local_date"] for x in o["legs"] if x.get("local_date") in off})
    ch, fac = chance(conn, kind, o["replaces"], nowd, o["legs"], dest=o.get("dest"), folga=folga,
                     extra=o.get("mode") == "extra")
    same = len(o["legs"]) == len(o["replaces"]) and not o.get("dest") and o.get("mode") != "extra"
    return {"ref": _opt_ref(o), "mode": o.get("mode") or ("bloco" if len(o["replaces"]) > 1 else "voo"),
            "block": len(o["replaces"]) > 1, "dest": o.get("dest"),
            "overnight_at": o["legs"][0]["ades"] if o.get("dest") and o["legs"][0]["local_date"] != o["legs"][-1]["local_date"] else None,
            "nights": ((datetime.fromisoformat(o["legs"][-1]["local_date"]) - datetime.fromisoformat(o["legs"][0]["local_date"])).days
                       if o.get("dest") else 0),
            "replaces": [_brief(conn, x) for x in o["replaces"]],
            "legs": [{**_brief(conn, x), "same": same and _is_same(x, r), "activity": x.get("activity")} for x, r in
                     itertools.zip_longest(o["legs"], o["replaces"] if same else [], fillvalue={}) if x],
            "ok": not why, "why": why, "chance": ch, "factors": fac, "rules_note": (lg or {}).get("note")}


# ------------------------------------------------------------------ pedir, cancelar, listar
def _decide_at(conn, sector: str, now: datetime, report: datetime, rnd) -> datetime:
    """Resposta em minutos: o CCO (perto do voo) é mais rápido que o Planejamento."""
    t = now + timedelta(minutes=rnd.randint(*RESPONSE_MIN[sector]))
    latest = report - timedelta(minutes=45)
    return max(now + timedelta(minutes=1), min(t, latest))


def request(conn, data_dir, leg_id: int, kind: str, *, ref: str | None = None, reason: str | None = None,
            user_id=None, now: str | None = None, rnd=None, dest: str | None = None) -> dict:
    kind = (kind or "").upper()
    if kind not in BASE:
        raise FGVError("Tipo de pedido inválido (TROCA ou LIBERACAO).", code="campo_invalido")
    rnd = rnd or random.SystemRandom()
    now = now or now_iso()
    op = options(conn, data_dir, leg_id, now, dest if kind == "TROCA" else None)
    if op["blocked"]:
        raise Conflict(op["blocked"], code="troca_bloqueada")
    if kind == "TROCA":
        c = next((x for x in op["troca"]["options"] if x["ref"] == ref), None)
        if not c:
            raise Conflict("Essa opção não está mais entre as trocas possíveis (a escala ou o RPL mudou).", code="troca_indisponivel")
        if not c["ok"]:
            raise Conflict(c["why"], code="troca_ilegal")
        legs = [_leg(conn, x["id"]) for x in c["replaces"]]
        cand = {"legs": _raw_new(conn, data_dir, _leg(conn, leg_id), c["ref"], op.get("dest")), "dest": op.get("dest")}
        ch, fac = c["chance"], c["factors"]
        label = (f"{' + '.join(_fn(x) for x in legs)} → {' + '.join(x['flight_number'] for x in c['legs'])}"
                 + (f" para {op['dest']}" if op.get("dest") else "") + f" ({c['legs'][0]['dep_label']})")
    else:
        lb = op["liberacao"] or {"options": []}
        okl = [x for x in lb["options"] if x["ok"]]
        c = next((x for x in lb["options"] if x["ref"] == ref), None) if ref else (okl[0] if okl else None)
        if not c:
            raise Conflict(lb.get("why") or "Não é possível sair desta perna.", code="troca_indisponivel")
        if not c["ok"]:
            raise Conflict(c["why"], code="troca_ilegal")
        legs = [_leg(conn, x["id"]) for x in c["replaces"]]
        newl = []
        if c["legs"]:
            ro = next(o for o in release_options(conn, data_dir, _leg(conn, leg_id), _flights(conn, _p(legs[0]["dep_utc"])),
                                                 _params(conn, legs[0])) if _opt_ref(o) == c["ref"])
            newl = ro["legs"]
        cand = {"legs": newl, "mode": c["mode"]} if newl else None
        ch, fac = c["chance"], c["factors"]
        label = "Sair de " + ", ".join(_fn(x) for x in legs) + (f" e ir de extra no {newl[0]['flight_number']}" if newl else "")
    nowd = _p(now)
    first = min(legs, key=lambda x: x["dep_utc"])
    fl = _flights(conn, _p(first["dep_utc"]))
    rep = _report(conn, first, fl)
    sector = "escala" if (_p(first["dep_utc"]) - nowd).total_seconds() >= 72 * 3600 else "tripulantes"
    dec = _decide_at(conn, sector, nowd, rep, rnd)
    with tx(conn):
        rid = conn.execute("""INSERT INTO swap_request(kind, leg_ids, leg_uids, first_dep_utc, label, candidate, reason, chance, factors,
                              state, sector, decide_at, user_id, created_at) VALUES (?,?,?,?,?,?,?,?,?, 'PENDENTE', ?,?,?,?)""",
                           (kind, json.dumps([x["id"] for x in legs]), json.dumps([x["uid"] for x in legs]), first["dep_utc"], label,
                            json.dumps(cand, ensure_ascii=False) if cand else None, (reason or "").strip()[:300] or None,
                            ch, json.dumps(fac, ensure_ascii=False), sector, _iso(dec), user_id, now)).lastrowid
        audit(conn, user_id, "escala.troca_pedido", "swap_request", rid, {"kind": kind, "chance": ch})
        from .equipe import post, _local
        quando = _local(conn, _iso(dec), first["adep"])
        post(conn, f"troca_rx:{rid}", sector, f"Pedido de troca recebido (#{rid})",
             f"Comandante, recebi o seu pedido: {label}."
             + (f"\nMotivo: {reason.strip()[:300]}" if reason and reason.strip() else "")
             + f"\n\nVou ver a cobertura e a legalidade e te respondo até {quando}.", now=now,
             links=[{"label": "Abrir o dia", "href": f"#/escala/{_ld(first)}"}])
    return get(conn, rid)


def _raw_new(conn, data_dir, leg: dict, ref: str, dest: str | None = None) -> list[dict]:
    """Os voos do RPL da opção escolhida, completos (como serão gravados na escala)."""
    raw, _ = raw_options(conn, data_dir, leg, _flights(conn, _p(leg["dep_utc"])), _params(conn, leg), dest)
    for o in raw:
        if _opt_ref(o) == ref:
            return o["legs"]
    raise Conflict("Essa opção não está mais entre as trocas possíveis.", code="troca_indisponivel")


def cancel(conn, rid: int, user_id=None, now: str | None = None) -> dict:
    r = one(conn.execute("SELECT * FROM swap_request WHERE id=?", (rid,)))
    if not r:
        raise NotFound("Pedido não encontrado.")
    if r["state"] != "PENDENTE":
        raise Conflict("Só dá para cancelar um pedido que ainda está aguardando resposta.", code="troca_decidida")
    with tx(conn):
        conn.execute("UPDATE swap_request SET state='CANCELADO', decided_at=?, answer=? WHERE id=?",
                     (now or now_iso(), "Cancelado por você.", rid))
        audit(conn, user_id, "escala.troca_cancelada", "swap_request", rid)
    return get(conn, rid)


def _out(r: dict) -> dict:
    r = dict(r)
    for k, d in (("leg_ids", []), ("leg_uids", []), ("factors", []), ("candidate", None)):
        r[k] = jloads(r[k], d) if r.get(k) else d
    return r


def get(conn, rid: int) -> dict:
    r = one(conn.execute("SELECT * FROM swap_request WHERE id=?", (rid,)))
    if not r:
        raise NotFound("Pedido não encontrado.")
    return _out(r)


def listing(conn, limit: int = 60) -> list[dict]:
    return [_out(r) for r in rows(conn.execute("SELECT * FROM swap_request ORDER BY id DESC LIMIT ?", (limit,)))]


def for_legs(conn, leg_ids) -> dict:
    """Último pedido de cada perna (para a tela do dia)."""
    want, out = set(leg_ids), {}
    for r in listing(conn, 200):
        for i in r["leg_ids"]:
            if i in want and i not in out:
                out[i] = {"id": r["id"], "state": r["state"], "kind": r["kind"], "chance": r["chance"], "decide_at": r["decide_at"],
                          "label": r["label"], "answer": r["answer"]}
        if r.get("new_leg_id") in want and r["new_leg_id"] not in out:
            out[r["new_leg_id"]] = {"id": r["id"], "state": r["state"], "kind": r["kind"], "chance": r["chance"],
                                    "decide_at": r["decide_at"], "label": r["label"], "answer": r["answer"], "came_in": True}
    return {str(k): v for k, v in out.items()}


# ------------------------------------------------------------------ resposta (no relógio da equipe)
def _still_valid(conn, r: dict, now: datetime) -> tuple[str, str] | None:
    """(estado, motivo) se o pedido não pode mais ser decidido; None se pode."""
    legs = []
    for i, uid in zip(r["leg_ids"], r["leg_uids"]):
        l = one(conn.execute(_SEL + " WHERE l.id=?", (i,)))
        if not l or l["removed_batch"] or l["uid"] != uid:
            return "EXPIRADO", "a escala mudou desde o pedido (a perna não está mais nela)"
        if l["flight_state"] in FLOWN:
            return "EXPIRADO", f"o {_fn(l)} já foi voado"
        legs.append(l)
    first = min(legs, key=lambda x: x["dep_utc"])
    fl = _flights(conn, _p(first["dep_utc"]))
    if now >= _report(conn, first, fl):
        return "EXPIRADO", "a apresentação já passou e não deu tempo de mexer"
    new = (r["candidate"] or {}).get("legs") or []
    if new:
        why = _connection(conn, fl, legs, new, int(_params(conn, first).get("swap_min") or 50))
        if why:
            return "NEGADO", f"a escala mudou e o voo pedido não cabe mais ({why.rstrip('.')})"
    lg = legality(conn, first, {x["id"] for x in legs}, new)
    if not lg["ok"]:
        return "NEGADO", "a escala ficaria ilegal: " + "; ".join(lg["new_errors"])
    return None


DENY = {
    "antecedencia": lambda f: ("a operação desse dia já está fechada e não tenho reserva sobrando nesse horário" if f["delta"] <= -35
                               else "faltam menos de 72 h e a reserva dessa janela já está comprometida"),
    "pedidos": lambda f: f"você já fez {f.get('n')} pedido(s) nos últimos 30 dias e preciso distribuir as trocas entre todo o quadro",
    "pico": lambda f: "é dia de pico e a reserva está toda comprometida",
    "projetado": lambda f: "o voo que você pediu ainda não está no RPL publicado para o dia, então não consigo garantir",
    "etapas": lambda f: f"são {f.get('n')} etapas para acertar e não consegui fechar todas",
    "destino": lambda f: f"ninguém escalado para {f.get('dest')} topou trocar com você nesses dias",
    "folga": lambda f: "a troca cairia na sua folga e não consigo mexer nela",
    "otp": lambda f: "a sua pontualidade recente pesou contra",
}


def _deny_text(r: dict) -> str:
    neg = sorted([f for f in r["factors"] if not f.get("base") and f["delta"] < 0], key=lambda f: f["delta"])
    if neg and neg[0]["key"] in DENY:
        return DENY[neg[0]["key"]](neg[0])
    return ("não consegui fechar a troca com quem está escalado no outro voo" if r["kind"] == "TROCA"
            else "não consegui fechar a cobertura com a reserva disponível")


def _apply(conn, r: dict, now: str) -> tuple[list[int], bool]:
    from .aeroscale_import import LEG_HASH_FIELDS, _hash
    from . import flights
    legs = [one(conn.execute("SELECT * FROM roster_leg WHERE id=?", (i,))) for i in r["leg_ids"]]
    legs.sort(key=lambda x: x["dep_utc"])
    cancelled = False
    bid = conn.execute("INSERT INTO import_batch(source_kind, source_name, imported_at, stats) VALUES ('troca', ?, ?, ?)",
                       (f"Pedido de troca #{r['id']}", now, json.dumps({"swap_request": r["id"], "kind": r["kind"]}))).lastrowid
    for l in legs:
        conn.execute("UPDATE roster_leg SET removed_batch=? WHERE id=?", (bid, l["id"]))
        conn.execute("INSERT INTO roster_leg_change(leg_id, batch_id, kind, old_values) VALUES (?,?, 'REMOVED', ?)",
                     (l["id"], bid, json.dumps({"troca": r["id"]})))
        f = one(conn.execute("SELECT id FROM flight WHERE leg_id=? AND state='RESERVADO'", (l["id"],)))
        if f:
            flights.transition(conn, f["id"], "CANCELADO", reason=f"troca de escala aprovada (pedido #{r['id']})")
            cancelled = True
    new_ids = []
    if (r["candidate"] or {}).get("legs"):
        for k, c in enumerate(r["candidate"]["legs"], 1):
            o = legs[min(k - 1, len(legs) - 1)]                  # destino escolhido: o bloco pode ter mais pernas do que entram
            new = {"seq": o["seq"], "airline": o["airline"], "flight_number": c["ident"], "type_icao": c["type_icao"],
                   "planned_type": o["planned_type"], "adep": c["adep"], "ades": c["ades"], "dep_utc": c["dep_utc"], "arr_utc": c["arr_utc"],
                   "eet_min": c["eet_min"], "data_origin": c["origin"] or o["data_origin"], "activity": c.get("activity") or o["activity"],
                   "reserve_uid": o["reserve_uid"],
                   "registration": None, "route": None, "cruise_fl": None, "speed": None,
                   "remarks": f"Troca de escala aprovada (pedido #{r['id']}); no lugar do {_fn(o)}."}
            new_ids.append(conn.execute(
                f"""INSERT INTO roster_leg(uid, schedule_id, {', '.join(LEG_HASH_FIELDS)}, local_date, content_hash, first_batch, last_batch)
                    VALUES (?,?,{','.join('?' * len(LEG_HASH_FIELDS))},?,?,?,?)""",
                (f"troca-{r['id']}-{k}", o["schedule_id"], *[new[x] for x in LEG_HASH_FIELDS], c["local_date"], _hash(new), bid, bid)).lastrowid)
    conn.execute("UPDATE swap_request SET batch_id=?, new_leg_id=? WHERE id=?", (bid, new_ids[0] if new_ids else None, r["id"]))
    return new_ids, cancelled


def clock(conn, now: str, rnd=None) -> int:
    """Responde os pedidos cuja hora chegou. Devolve quantos foram decididos."""
    rnd = rnd or random.SystemRandom()
    nowd = _p(now)
    n = 0
    from .equipe import post, _local
    for raw in rows(conn.execute("SELECT * FROM swap_request WHERE state='PENDENTE' AND decide_at<=? ORDER BY decide_at", (now,))):
        r = _out(raw)
        with tx(conn):
            leg0 = one(conn.execute("SELECT local_date FROM roster_leg WHERE id=?", (r["leg_ids"][0],))) or {}
            links = [{"label": "Abrir o dia", "href": f"#/escala/{leg0.get('local_date') or r['first_dep_utc'][:10]}"}]
            bad = _still_valid(conn, r, nowd)
            if bad:
                st, why = bad
                conn.execute("UPDATE swap_request SET state=?, decided_at=?, answer=? WHERE id=?", (st, now, why[:400], r["id"]))
                post(conn, f"troca_res:{r['id']}", r["sector"], f"Pedido de troca #{r['id']}: {'sem efeito' if st == 'EXPIRADO' else 'negado'}",
                     f"Comandante, sobre o seu pedido ({r['label']}): não deu, {why}. A sua escala segue como estava.",
                     level="acao", ack=True, links=links, now=now)
                n += 1
                continue
            roll = rnd.randint(1, 100)
            if roll <= r["chance"]:
                new_ids, cancelled = _apply(conn, r, now)
                if r["kind"] == "TROCA":
                    nl = r["candidate"]["legs"]
                    body = (f"Comandante, troca aprovada: {r['label']}. Agora você está no "
                            + " e no ".join(f"{x['ident']} ({_local(conn, x['dep_utc'], x['adep'])})" for x in nl)
                            + (". Há voo PROJETADO: confira quando o RPL do dia sair" if any(x.get("origin") == "PROJETADO" for x in nl) else "")
                            + ". Quem estava escalado nele assume o seu. A escala já está atualizada"
                            + ("; a reserva do voo antigo foi cancelada, reserve o novo." if cancelled else "."))
                else:
                    gone = ", ".join(_fn(one(conn.execute("SELECT * FROM roster_leg WHERE id=?", (i,)))) for i in r["leg_ids"])
                    nl = (r["candidate"] or {}).get("legs") or []
                    body = (f"Comandante, liberação aprovada: você sai de {gone} e a reserva cobre"
                            + (f". Você vai de extra (passageiro) no {nl[0]['flight_number']}, partida "
                               f"{_local(conn, nl[0]['dep_utc'], nl[0]['adep'])}, para seguir a escala em {nl[0]['ades']}" if nl else "")
                            + ". A escala já está atualizada" + ("; a reserva do voo foi cancelada." if cancelled else "."))
                conn.execute("UPDATE swap_request SET state='APROVADO', roll=?, decided_at=?, answer=? WHERE id=?",
                             (roll, now, "Aprovado.", r["id"]))
                post(conn, f"troca_res:{r['id']}", r["sector"], f"Pedido de troca #{r['id']} aprovado", body + "\n\nPreciso do seu ciente.",
                     level="acao", ack=True, links=links, now=now)
                audit(conn, r["user_id"], "escala.troca_aprovada", "swap_request", r["id"], {"new_leg_ids": new_ids})
            else:
                why = _deny_text(r)
                conn.execute("UPDATE swap_request SET state='NEGADO', roll=?, decided_at=?, answer=? WHERE id=?",
                             (roll, now, why, r["id"]))
                post(conn, f"troca_res:{r['id']}", r["sector"], f"Pedido de troca #{r['id']} negado",
                     f"Comandante, não consegui aprovar o seu pedido ({r['label']}): {why}. A escala segue como estava; "
                     "se quiser, tente outro voo ou outro dia.", level="acao", ack=True, links=links, now=now)
            n += 1
    return n


def data_dir_of(conn) -> Path | None:
    """Pasta de dados a partir do banco da carreira (dados/carreiras/GLO.db → dados)."""
    r = conn.execute("PRAGMA database_list").fetchone()
    p = Path(r[2]) if r and r[2] else None
    if not p:
        return None
    return p.parent.parent if p.parent.name == "carreiras" else p.parent
