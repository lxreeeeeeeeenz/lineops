"""Gerador de escalas, validação de continuidade, edição e exportação.

Princípios
* Toda perna "REAL" é um voo que existe no RPL ativo naquela data (UTC do EOBT).
* "PROJETADO" é um voo real do RPL repetido pelo padrão semanal numa data fora
  da validade do arquivo — plausível, mas NÃO garantido.
* "SUGESTAO" é uma perna criada pelo sistema (posicionamento) sem voo no RPL.
* "MANUAL" é uma perna digitada/alterada pelo usuário.
* O RPL não traz matrícula nem rodízio de aeronaves: a associação
  matrícula ↔ sequência de voos é sempre uma sugestão do AeroScale.
"""
from __future__ import annotations

import bisect
from collections import Counter
import math
import csv
import io
import json
import random
import re
import uuid
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone

from . import catalog
from .airports import offset_label, to_local, get_tz
from . import crew, crew_profiles, predict
from . import reserve as reserve_mod
from . import intl as intl_mod
from . import ops as ops_mod
from .db import get_settings, now_iso, row, rows
from .rpl_service import FlightIndex, all_route_eet, get_index

ORIGIN_LABEL = {
    "REAL": "Voo real (RPL)",
    "PROJETADO": "Projetado do RPL (fora da validade)",
    "SUGESTAO": "Sugestão do sistema",
    "MANUAL": "Manual",
    "OBSERVADO": "Internacional observado no ADS-B (fora do RPL)",
}


class PlanError(Exception):
    pass


def parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hhmm(s: str, default: str) -> time:
    try:
        h, m = (s or default).split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        h, m = default.split(":")
        return time(int(h), int(m))


# ------------------------------------------------------------------ parâmetros
def resolve_params(db, p: dict) -> dict:
    s = get_settings(db)
    out = dict(p)
    airline = (p.get("airline") or "").upper()
    if not airline:
        raise PlanError("Selecione a companhia.")
    out["airline"] = airline
    kind = p.get("kind") or "semanal"
    if kind not in ("mensal", "semanal", "avulsa", *LONG_KINDS):
        raise PlanError("Tipo de escala inválido.")
    try:
        if kind == "mensal":
            y, m = map(int, (p.get("month") or "").split("-"))
            start, end = date(y, m, 1), date(y, m, monthrange(y, m)[1])
        elif kind in LONG_KINDS:
            # experimental: 6 ou 12 meses a partir do mês inicial
            y, m = map(int, (p.get("month") or "").split("-"))
            start = date(y, m, 1)
            ey, em = divmod(m - 1 + LONG_KINDS[kind] - 1, 12)
            end = date(y + ey, em + 1, monthrange(y + ey, em + 1)[1])
        elif kind == "semanal":
            start = date.fromisoformat(p["start_date"])
            end = start + timedelta(days=6)
        else:
            start = date.fromisoformat(p["start_date"])
            end = date.fromisoformat(p.get("end_date") or p["start_date"])
    except (KeyError, ValueError):
        raise PlanError("Período inválido. Informe as datas no formato correto.")
    if end < start:
        raise PlanError("A data final é anterior à inicial.")
    if kind not in LONG_KINDS and (end - start).days > 62:
        raise PlanError("Período máximo de 62 dias por escala (para mais, use 6 meses ou Anual, experimentais).")
    out.update(kind=kind, start_date=start.isoformat(), end_date=end.isoformat())

    def num(k, default, lo, hi):
        try:
            v = int(p.get(k) if p.get(k) not in (None, "") else default)
        except (TypeError, ValueError):
            raise PlanError(f"Valor inválido em {k}.")
        if not lo <= v <= hi:
            raise PlanError(f"{k} deve ficar entre {lo} e {hi}.")
        return v

    out["min_turn"] = num("min_turn", s["min_turn_min"], 10, 600)
    out["max_turn"] = num("max_turn", s["max_turn_min"], out["min_turn"], 1440)
    out["min_rest"] = num("min_rest", s["min_rest_min"], 0, 2880)
    out["legs_min"] = num("legs_min", s["legs_min"], 0, 12)
    out["legs_max"] = num("legs_max", s["legs_max"], max(1, out["legs_min"]), 12)
    out["leg_min_minutes"] = num("leg_min_minutes", 0, 0, 1440)
    out["leg_max_minutes"] = num("leg_max_minutes", 1440, max(1, out["leg_min_minutes"]), 1440)
    out["day_start"] = p.get("day_start") or s["day_start"]
    out["day_end"] = p.get("day_end") or s["day_end"]
    # Máximo de pernas por dia da semana (0 = folga). Compatível com o antigo "weekdays".
    wm_raw = p.get("weekday_max")
    if wm_raw is None and p.get("weekdays") is not None:
        wm_raw = {str(i): 0 for i in range(7) if i not in [int(x) for x in p["weekdays"]]}
    if wm_raw is None:
        wm_raw = s.get("weekday_max") or {}
    wm = {}
    for k, v in (wm_raw or {}).items():
        if v in (None, "", "padrao"):
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise PlanError("Máximo de pernas por dia da semana inválido.")
        if not 0 <= n <= 12 or str(k) not in [str(i) for i in range(7)]:
            raise PlanError("Máximo de pernas por dia da semana deve ficar entre 0 (folga) e 12.")
        wm[str(k)] = n
    out["weekday_max"] = wm
    out["weekdays"] = [i for i in range(7) if wm.get(str(i)) != 0]
    # Regras de tripulação e tempo de solo
    out["turn_mode"] = p.get("turn_mode") or s.get("turn_mode", "auto")
    out["min_turn_fixed"] = out["min_turn"]
    out["crew_mode"] = p.get("crew_mode") or s.get("crew_mode", "rbac117")
    if out["crew_mode"] not in crew.MODES:
        raise PlanError("Perfil de regras de tripulação inválido.")
    for k in ("crew_days_off", "crew_folga_base", "crew_madrugada", "crew_monthly"):
        out[k] = bool(p.get(k, s.get(k, True)))
    out["days_off_min"] = num("days_off_min", s.get("days_off_min", 10), 0, 20)
    out["max_consecutive_days"] = num("max_consecutive_days", s.get("max_consecutive_days", 6), 1, 21)
    # apresentação: GOL e LATAM 1 h, Azul 50 min (perfil da companhia), salvo valor fixo nas configurações/pedido
    rb_set = s.get("report_before_min")
    rb_default = crew_profiles.profile(airline)["report_min"] if rb_set in (None, "", "auto") else rb_set
    out["report_before"] = num("report_before", rb_default, 0, 180)
    out["release_after"] = num("release_after", s.get("release_after_min", 30), 0, 180)
    out["model_realism"] = bool(p.get("model_realism", s.get("model_realism", True)))
    out["anomaly_repeat_pct"] = num("anomaly_repeat_pct", s.get("anomaly_repeat_pct", 5), 0, 100)
    # Reserva automática (bloco garantido; local e acionamento sorteados)
    # nas escalas de 6 meses e anual, o número de blocos de reserva vale por mês
    out["reserve_blocks"] = num("reserve_blocks", reserve_mod.default_blocks(kind, s), 0, 4)
    rmin = num("reserve_min_days", s.get("reserve_min_days", 3), 3, 9)
    rmax = min(num("reserve_max_days", s.get("reserve_max_days", 6), 3, 9), reserve_mod.HARD_MAX)   # nunca 7 seguidos
    rmin = min(rmin, rmax)
    if p.get("mode") == "tripulante" and p.get("profile_realism", True) and p.get("reserve_max_days") in (None, ""):
        rmax = max(rmin, min(rmax, 4))   # v2.2: nas escalas reais a reserva vem em blocos de até 3–4 dias
    if rmin > rmax:
        raise PlanError("Duração mínima da reserva maior que a máxima.")
    out["reserve_min_days"], out["reserve_max_days"] = rmin, rmax
    out["reserve_base_pct"] = num("reserve_base_pct", s.get("reserve_base_pct", 80), 0, 100)
    out["reserve_activation_pct"] = num("reserve_activation_pct", s.get("reserve_activation_pct", 50), 0, 100)
    out["reserve_activation_max_days"] = num("reserve_activation_max_days", s.get("reserve_activation_max_days", 3), 1, 9)
    out["international"] = bool(p.get("international", s.get("intl_enabled", True)))
    # Quantidade de internacionais escolhida pelo usuário (% das pernas). Sem valor = fração real do ADS-B.
    raw = p.get("intl_target_pct")
    out["intl_target_pct"] = None
    if out["international"] and raw not in (None, ""):
        out["intl_target_pct"] = num("intl_target_pct", 0, 0, 100)
        if out["intl_target_pct"] == 0:
            out["international"] = False
    out["return_to_base"] = p.get("return_to_base") or "none"
    # v2: escala do tripulante (troca de aeronave nos hubs, extra) ou da aeronave (padrão até a 1.x)
    out["mode"] = "tripulante" if p.get("mode") == "tripulante" else "aeronave"
    # pedido de escala (modo carreira): folgas pedidas já filtradas pela concessão, madrugada e período do dia
    out["requested_off"] = sorted({str(x)[:10] for x in (p.get("requested_off") or []) if x})
    out["avoid_madrugada"] = bool(p.get("avoid_madrugada"))
    out["time_pref"] = p.get("time_pref") if p.get("time_pref") in ("manha", "tarde") else ""
    out["swap_min"] = num("swap_min", 50, 20, 180)
    out["allow_deadhead"] = bool(p.get("allow_deadhead", True))
    # v2.2: realismo das escalas reais (perfil da companhia) – só no modo tripulante
    prof = crew_profiles.profile(airline)
    out["profile_realism"] = out["mode"] == "tripulante" and bool(p.get("profile_realism", True))
    out["sobreaviso_per_month"] = num("sobreaviso_per_month", prof["sobreaviso_per_month"], 0, 8) \
        if out["profile_realism"] else 0
    out["sobreaviso_activation_pct"] = num("sobreaviso_activation_pct", prof["sobreaviso_activation_pct"], 0, 100)
    out["long_off"] = out["profile_realism"] and bool(p.get("long_off", True))
    out["inactive_days"] = out["profile_realism"] and bool(p.get("inactive_days", True))
    out["long_off_block"] = list(prof["long_off_block"])
    out["type_match"] = p.get("type_match") or s.get("type_match", "family")
    out["projection"] = bool(p.get("projection", s["projection_enabled"]))
    out["allow_positioning"] = bool(p.get("allow_positioning", False))
    out["avoid_used"] = bool(p.get("avoid_used", s["avoid_used_flights"]))
    # aeroportos: aceita IATA (REC) e converte para ICAO (SBRF)
    iata = {a.get("iata"): k for k, a in catalog.airport_map(db).items() if a.get("iata")}
    to_icao = lambda c: iata.get(c, c) if len(c) == 3 else c   # noqa: E731
    out["preferred_airports"] = list(dict.fromkeys(to_icao(a.strip().upper()) for a in (p.get("preferred_airports") or [])
                                                   if a.strip()))
    out["avoid_airports"] = list(dict.fromkeys(to_icao(a.strip().upper()) for a in (p.get("avoid_airports") or [])
                                               if a.strip()))
    # v2.2.4: chance de chaves (dias de voo) com 2 e 3 etapas, escolhida pelo usuário (vazio = perfil da companhia)
    for k in ("legs2_pct", "legs3_pct"):
        out[k] = None if p.get(k) in (None, "") else num(k, 0, 0, 100)
    if (out["legs2_pct"] is None) != (out["legs3_pct"] is None):
        out["legs2_pct"] = out["legs2_pct"] or 0
        out["legs3_pct"] = out["legs3_pct"] or 0
    if out["legs2_pct"] is not None and out["legs2_pct"] + out["legs3_pct"] > 100:
        raise PlanError("A chance de 2 etapas mais a de 3 etapas passa de 100%.")
    out["pref_strength"] = p.get("pref_strength") if p.get("pref_strength") in ("leve", "normal", "forte") else "normal"
    out["base"] = to_icao((p.get("base") or "").strip().upper()) or None
    out["seed"] = int(p.get("seed") or random.randint(1, 10 ** 9))
    # semente própria da reserva; sem informar, deriva da semente principal (mesma semente = mesma escala)
    out["reserve_seed"] = int(p.get("reserve_seed") or (out["seed"] * 7919 + 104729) % 10 ** 9 + 1)
    out["equipment"] = {str(k): v for k, v in (p.get("equipment") or {}).items() if v}
    out["aircraft_mode"] = p.get("aircraft_mode") or ("specific" if p.get("aircraft_id") else "auto")
    out["icao_type"] = (p.get("icao_type") or "").upper() or None
    out["model_id"] = p.get("model_id") or None
    out["taxi"] = int(s["taxi_allowance_min"] or 0)
    return out


# Escalas longas (experimental): tipo -> meses
LONG_KINDS = {"semestral": 6, "anual": 12}


def month_windows(start: date, end: date) -> list[tuple[date, date]]:
    out = []
    d = start
    while d <= end:
        last = date(d.year, d.month, monthrange(d.year, d.month)[1])
        out.append((d, min(last, end)))
        d = last + timedelta(days=1)
    return out


# ------------------------------------------------------------------ repetição de trecho
REPEAT_PENALTY = 150    # por perna além de um bate-volta no mesmo par (vale mais que uma perna extra)
MONOTONY_PENALTY = 25   # dia idêntico ao anterior
# Diversidade (calibrada com o ADS-B: numa mesma matrícula, ~93% dos voos de 4 dias são diferentes e quase
# nunca há dois dias iguais). Sem isto o planejador repetia a sequência "mais provável" todo dia.
IDENT_REPEAT = 40       # voo repetido ontem; cai linearmente até zero em 10 dias
PAIR_RECENT = 6         # por vez que o mesmo trecho já foi voado nos últimos 7 dias
NEW_AIRPORT = 6         # destino não visitado nos últimos 5 dias
SAME_SEQUENCE = 80      # dia igual (voo a voo) a qualquer um dos últimos 10 dias
PICK_TEMPERATURE = 10   # sorteio entre as melhores opções do dia (0 = sempre a melhor)
# Fração de pernas internacionais medida no ADS-B (24–27/10/2026) por companhia e família de narrowbody.
# Widebody (B77W, A332…) só voa internacional e não precisa de alvo.
INTL_TARGET = {("TAM", "A320"): 0.047, ("GLO", "B737"): 0.039, ("AZU", "A320"): 0.01, ("AZU", "E-JET"): 0.0,
               ("AZU", "ATR"): 0.0}
INTL_BONUS = 110        # por perna internacional enquanto a escala está abaixo do alvo
# Quantidade escolhida pelo usuário (caixa "Definir quantidade de internacionais"): mais forte e nos dois sentidos
INTL_USER_BONUS = 220   # por perna internacional dentro da cota
INTL_USER_OVER = 150    # por perna internacional além da cota
INTL_USER_MISS = 60     # por perna internacional que o dia deixou de fazer estando devendo
# Pernoite (ops.py): a companhia só deixa aeronave dormir em ~30–45 aeroportos. Estoque = aeronaves compatíveis
# que o RPL exige no solo às 03:00; ADS-B = pernoites vistos por noite.
NIGHT_NONE = 350        # pernoite onde nem o RPL exige nem o ADS-B viu aeronave dormindo
NIGHT_RARE = 120        # só o ADS-B viu, e raramente (< 0,15 aeronave por noite)
NIGHT_OK = 20           # bônus máximo para pernoite em aeroporto com estoque
OBS_NIGHT_RATE = 0.15   # pernoites vistos por noite a partir do qual o ADS-B sozinho basta
LAST_FLIGHT = 30        # último voo do dia que na operação real costuma (ou nunca costuma) encerrar o dia
FIRST_FLIGHT = 15       # primeiro voo do dia que costuma abrir o dia
OBS_PRIORITY = 4        # sucessores observados (mais vistos) que entram primeiro na busca
REALISM_W = 12          # pontos por unidade de log-probabilidade do modelo de rodízio (antes 8)
# Escala do tripulante (v2)
DEADHEAD_COST = 40      # por deslocamento como extra (passageiro): resolve, mas não é o objetivo
# Pedido de escala (carreira): pesam menos que uma perna, para não esvaziar a escala
BID_MADRUGADA = 60      # jornada que toca a madrugada quando o piloto pediu para evitar
BID_TIME = 70           # apresentação fora do período pedido (manhã: antes das 12h; tarde: a partir das 11h)
# v2.2 – produtividade como nas escalas reais (perfil da companhia)
PROD_BLOCK_W = 20       # pontos por hora de voo no dia
PROD_PRIOR_W = 40       # pontos por unidade de ln(P(pernas)/P(moda)) da distribuição real de pernas por dia
PROD_TARGET_W = 100     # pontos por perna de diferença para a meta do dia (sorteada da distribuição real)
PROD_DAY_BONUS = 260    # dia de trabalho com voo (antes 100 por perna até 2): voar sempre vale mais que ficar parado
PROD_TARGET_CAP = 160   # teto da penalidade da meta de pernas
CUSTOM_TARGET_W = 160   # meta de etapas escolhida pelo usuário (sliders de 2 e 3 etapas)
PROD_SHORT_W = 20       # pontos por hora abaixo de 80% do ritmo do mês (tipo de etapa curta, como o E195-E2, voa mais pernas)
EDGE_ONE_LEG = 2.0      # 1º/último dia da viagem (sai ou volta de folga): 1 perna fica 2× mais provável
DHD_START_COST = 80     # dia que começa como extra (passageiro) até a base para voar de lá
GROUND_TRANSFER_COST = 45   # dia que começa noutro aeroporto da mesma cidade (ônibus da companhia)
# v2.2.3 – aeroportos de preferência ("escala direcionada"): antes era só +15 por perna e não fazia efeito
PREF_VISIT = 160        # 1ª chegada do dia num aeroporto de preferência
PREF_MORE = 50          # chegadas seguintes no mesmo dia
PREF_FEEDER = 45        # termina o dia num aeroporto com voo direto para um preferido (aproxima)
PREF_PERNOITE = 40      # dorme num aeroporto de preferência
PREF_STRENGTH = {"leve": 0.45, "normal": 1.0, "forte": 1.8}
PREF_EXPLORE = 4        # voos para preferidos sempre entram na busca, além dos BRANCH mais cedo
SWAP_OUTSTATION = 30    # troca de aeronave fora de hub (poucas aeronaves dormem lá): rara na vida real
SWAP_NEG_SCALE = 0.3    # no modo tripulante, sequência "nunca vista" é só troca de aeronave: pesa menos
REALISM_CAP = 4.0       # limite da log-probabilidade (antes 3)


def _pair_counts(chain) -> dict:
    out: dict = {}
    for o in chain:
        k = tuple(sorted((o["adep"], o["ades"])))
        out[k] = out.get(k, 0) + 1
    return out


def repeat_excess(chain) -> int:
    """Pernas além de um bate-volta (2 pernas) no mesmo par de aeroportos dentro de um dia/jornada."""
    return sum(max(0, c - 2) for c in _pair_counts(chain).values())


def repeated_pairs(chain) -> list[str]:
    return [f"{a}–{b} ({c}×)" for (a, b), c in _pair_counts(chain).items() if c > 2]


# ------------------------------------------------------------------ planejador
class Planner:
    BEAM = 40
    BRANCH = 10

    def __init__(self, db, params: dict, schedule_id: int | None = None):
        self.db = db
        self.p = params
        self.schedule_id = schedule_id
        self.rng = random.Random(params["seed"])
        self.idx = get_index(db, params["airline"], params["taxi"])
        if not self.idx.flights:
            raise PlanError(f"Não há RPL ativo para {params['airline']}. Importe um arquivo CGNA/RPL primeiro.")
        self.apmap = catalog.airport_map(db)
        self.start = date.fromisoformat(params["start_date"])
        self.end = date.fromisoformat(params["end_date"])
        self.diags: list[dict] = []
        self.route_eet = None
        self.aircraft = None
        self.ac_type = None
        self._occ_by_adep: dict[str, list] = {}
        self._occ_deps: dict[str, list] = {}
        self.taken: set[str] = set()
        self.used_refs: set[str] = set()
        self.ac_windows: list = []
        # estado das regras de tripulação
        self.madr: list[tuple[datetime, date]] = []
        self.month_block: dict[str, float] = {}
        self.month_duty: dict[str, float] = {}
        self.week_duty: dict[tuple, float] = {}
        self.last_duty_h: float | None = None
        self.last_release: datetime | None = None
        self.day_plan: dict = {}
        self.duty_prefix: list = []   # pernas já existentes da jornada em andamento (regerar no meio do dia)
        self.prev_day_idents: tuple | None = None
        self.flown: list[tuple[date, tuple]] = []   # (dia, pernas do dia) para a memória de diversidade
        self.n_legs = 0
        self.n_intl = 0
        self.reserves: list[dict] = []   # blocos de reserva (sorteados ou carregados do banco)
        self.base_icao: str | None = params.get("base_resolved") or params.get("base")
        self.day_records: list[dict] = []   # v2.2: dias de SOBREAVISO e INATIVO (tabela schedule_days)
        self._feeders = None
        self.sa_rng = random.Random(params["seed"] * 104729 + 3)   # sorteios do sobreaviso e do inativo
        self.legs_rng = random.Random(params["seed"] * 7 + 1)      # meta de pernas por dia (perfil da companhia)
        self._goal_deck: list[int] = []
        self._ops = None
        self._night_stock: dict | None = None
        self.progress = None   # callback(fração, etapa) das tarefas em segundo plano; levanta Cancelled
        # sorteio separado para as anormalidades: não altera as escolhas normais de uma mesma semente
        self.anomaly_rng = random.Random(params["seed"] * 7919 + 17)

    # -- helpers
    def diag(self, level, code, msg, d=None):
        self.diags.append({"level": level, "code": code, "message": msg, "date": d})

    def tz(self, icao):
        return catalog.airport_tz(self.apmap, icao)

    def build_pool(self, d1: date, d2: date):
        occs = self.idx.occurrences_range(d1 - timedelta(days=1), d2 + timedelta(days=1), self.p["projection"])
        if self.p.get("international"):
            occs = list(occs) + intl_mod.occurrences(self.db, self.p["airline"], d1 - timedelta(days=1),
                                                    d2 + timedelta(days=1))
        pool: dict[str, list] = {}
        for o in occs:
            pool.setdefault(o["adep"], []).append(o)
        for k in pool:
            pool[k].sort(key=lambda o: o["dep"])
        self._occ_by_adep = pool
        self._night_stock = None
        self._feeders = None
        self._occ_deps = {k: [o["dep"] for o in v] for k, v in pool.items()}
        self._viable: dict[str, int] = {}  # partidas compatíveis dentro da janela, por aeroporto
        if not self.p["projection"]:
            cf, ct = self.idx.coverage()
            if cf and (d1.isoformat() > ct or d2.isoformat() < cf):
                self.diag("error", "fora_validade",
                          f"O período {d1:%d/%m/%Y}–{d2:%d/%m/%Y} está fora da validade do RPL ({cf} a {ct}) "
                          "e a projeção semanal está desativada.")

    def busy_sets(self):
        """Ocorrências e janelas já usadas por outras escalas."""
        used_refs = set()
        ac_windows = []
        q = ("SELECT l.flight_id, l.flight_number, l.dep_utc, l.arr_utc, l.aircraft_id, l.data_origin FROM schedule_legs l"
             " WHERE l.airline=? AND l.dep_utc>=? AND l.dep_utc<=?")
        args = [self.p["airline"], (self.start - timedelta(days=2)).isoformat(),
                (self.end + timedelta(days=2)).isoformat() + "T23:59:59Z"]
        if self.schedule_id:
            q += " AND l.schedule_id<>?"
            args.append(self.schedule_id)
        for r in self.db.execute(q, args):
            if r["flight_id"] and r["data_origin"] in ("REAL", "PROJETADO"):
                used_refs.add(f"{r['flight_id']}:{r['dep_utc'][:10]}")
            elif r["data_origin"] == "OBSERVADO" and r["flight_number"]:
                used_refs.add(f"I:{r['flight_number']}:{r['dep_utc'][:10]}")
            if self.aircraft and r["aircraft_id"] == self.aircraft["id"]:
                ac_windows.append((parse_utc(r["dep_utc"]), parse_utc(r["arr_utc"])))
        if self.aircraft:  # reservas de outras escalas da mesma aeronave também ocupam o período
            q2 = ("SELECT r.* FROM schedule_reserves r JOIN schedules s ON s.id=r.schedule_id WHERE s.aircraft_id=?"
                  " AND r.end_date>=? AND r.start_date<=?")
            a2 = [self.aircraft["id"], self.start.isoformat(), self.end.isoformat()]
            if self.schedule_id:
                q2 += " AND r.schedule_id<>?"
                a2.append(self.schedule_id)
            for r in self.db.execute(q2, a2):
                tz = get_tz(self.tz(r["location"])) or timezone.utc
                ac_windows.append((datetime.combine(date.fromisoformat(r["start_date"]), time(0, 0), tz).astimezone(timezone.utc),
                                   datetime.combine(date.fromisoformat(r["end_date"]) + timedelta(days=1), time(0, 0), tz)
                                   .astimezone(timezone.utc)))
        self.used_refs = used_refs if self.p["avoid_used"] else set()
        self.ac_windows = ac_windows

    # -- aeronave
    def crew_mode(self) -> bool:
        return self.p.get("mode") == "tripulante"

    def choose_aircraft(self):
        p = self.p
        if self.crew_mode():
            t = p["icao_type"]
            if not t and p["aircraft_mode"] == "specific" and p.get("aircraft_id"):
                r = row(self.db.execute("SELECT m.icao_type FROM aircraft a JOIN aircraft_models m ON m.id=a.model_id"
                                        " WHERE a.id=?", (p["aircraft_id"],)))
                t = r["icao_type"] if r else None
            if not t:
                raise PlanError("Na escala do tripulante, informe o tipo da habilitação (ex.: B738, A320).")
            self.ac_type = t
            self.aircraft = None
            self.diag("info", "modo_tripulante",
                      f"Escala do tripulante habilitado em {t} (família {catalog.type_family(t)}): troca de aeronave "
                      f"nos hubs com conexão mínima de {p['swap_min']} min"
                      + (", deslocamento como extra em voo real quando preciso." if p["allow_deadhead"] else "."))
            return
        if p["aircraft_mode"] == "type_only":
            if not p["icao_type"]:
                raise PlanError("Informe o tipo ICAO quando gerar sem matrícula.")
            self.ac_type = p["icao_type"]
            self.diag("warning", "sem_matricula",
                      f"Escala gerada só por tipo ({self.ac_type}), sem matrícula associada.")
            return
        if p["aircraft_mode"] == "specific":
            a = row(self.db.execute(
                "SELECT a.*, m.icao_type, m.family FROM aircraft a LEFT JOIN aircraft_models m ON m.id=a.model_id"
                " WHERE a.id=?", (p.get("aircraft_id"),)))
            if not a:
                raise PlanError("Aeronave não encontrada.")
            if a["airline"] != p["airline"]:
                raise PlanError(f"{a['registration']} pertence a {a['airline']}, não a {p['airline']}.")
            if not a["active"]:
                raise PlanError(f"{a['registration']} está desativada.")
            if not a["icao_type"]:
                raise PlanError(f"{a['registration']} não tem modelo associado; associe o modelo na Frota "
                                "para garantir a compatibilidade com os voos.")
            unmet = self._equipment_unmet(a["id"])
            if unmet:
                self.diag("warning", "equipamento",
                          f"{a['registration']} não atende (ou não tem confirmado) os requisitos: {', '.join(unmet)}.")
            self.aircraft = a
            self.ac_type = a["icao_type"]
            return
        # automática
        q = ("SELECT a.*, m.icao_type, m.family FROM aircraft a JOIN aircraft_models m ON m.id=a.model_id"
             " WHERE a.airline=? AND a.active=1")
        args = [p["airline"]]
        if p["icao_type"]:
            q += " AND m.icao_type=?"
            args.append(p["icao_type"])
        if p["model_id"]:
            q += " AND m.id=?"
            args.append(int(p["model_id"]))
        cands = rows(self.db.execute(q, args))
        if not cands:
            no_model = self.db.execute("SELECT COUNT(*) c FROM aircraft WHERE airline=? AND active=1 AND model_id IS NULL",
                                       (p["airline"],)).fetchone()["c"]
            total = self.db.execute("SELECT COUNT(*) c FROM aircraft WHERE airline=? AND active=1",
                                    (p["airline"],)).fetchone()["c"]
            if no_model:
                raise PlanError(
                    f"{no_model} aeronave(s) ativa(s) da {p['airline']} estão sem modelo associado, e sem modelo não dá "
                    "para saber quais voos do RPL elas podem fazer. Em Frota, filtre por 'Sem modelo', selecione as "
                    "matrículas e use 'Alterar em lote → Modelo'." + (" Também verifique o filtro de tipo escolhido."
                                                                      if p["icao_type"] else ""))
            if total:
                raise PlanError(f"Nenhuma aeronave ativa da {p['airline']} é do tipo/modelo filtrado.")
            raise PlanError(f"Não há aeronaves ativas da {p['airline']}. Importe a lista de frota (confira a companhia "
                            "escolhida na importação), carregue os dados fictícios ou gere 'somente por tipo'.")
        types_in_rpl = {f["type_icao"] for f in self.idx.flights}
        reasons = []
        scored = []
        for a in cands:
            if self._equipment_unmet(a["id"]):
                reasons.append(f"{a['registration']}: requisitos de equipamento não confirmados")
                continue
            compat = [t for t in types_in_rpl if catalog.types_compatible(a["icao_type"], t, p["type_match"])]
            if not compat:
                reasons.append(f"{a['registration']}: tipo {a['icao_type']} não aparece no RPL")
                continue
            busy = self.db.execute(
                "SELECT COUNT(*) c FROM schedule_legs WHERE aircraft_id=? AND dep_utc>=? AND dep_utc<=?"
                + (" AND schedule_id<>?" if self.schedule_id else ""),
                [a["id"], self.start.isoformat(), self.end.isoformat() + "T23:59:59Z"]
                + ([self.schedule_id] if self.schedule_id else [])).fetchone()["c"]
            busy += self.db.execute(
                "SELECT COUNT(*) c FROM schedule_reserves r JOIN schedules s ON s.id=r.schedule_id WHERE s.aircraft_id=?"
                " AND r.end_date>=? AND r.start_date<=?" + (" AND r.schedule_id<>?" if self.schedule_id else ""),
                [a["id"], self.start.isoformat(), self.end.isoformat()]
                + ([self.schedule_id] if self.schedule_id else [])).fetchone()["c"]
            if busy:
                reasons.append(f"{a['registration']}: já escalada em outra escala no período (voos ou reserva)")
                continue
            base = p["base"]
            support = sum(1 for f in self.idx.flights
                          if catalog.types_compatible(a["icao_type"], f["type_icao"], p["type_match"])
                          and (not base or f["adep"] == base))
            if base and support == 0:
                reasons.append(f"{a['registration']}: nenhum voo {a['icao_type']} parte de {base}")
                continue
            scored.append((support + self.rng.random(), a))
        if not scored:
            raise PlanError("Nenhuma aeronave compatível disponível. " + "; ".join(reasons[:6]))
        scored.sort(key=lambda x: -x[0])
        self.aircraft = scored[0][1]
        self.ac_type = self.aircraft["icao_type"]
        self.diag("info", "aeronave_auto",
                  f"Aeronave escolhida automaticamente: {self.aircraft['registration']} ({self.ac_type}).")

    def _equipment_unmet(self, aid) -> list[str]:
        unmet = []
        for fid, req in self.p["equipment"].items():
            r = row(self.db.execute(
                "SELECT c.value, f.label FROM config_fields f LEFT JOIN aircraft_config c"
                " ON c.field_id=f.id AND c.aircraft_id=? WHERE f.id=?", (aid, int(fid))))
            if r is None:
                continue
            if (r["value"] or "DESCONHECIDO") != req:
                unmet.append(f"{r['label']}={req}")
        return unmet

    def rotation_model(self):
        if not hasattr(self, "_model"):
            self._model = predict.RotationModel(self.idx, self.p["airline"], self.ac_type,
                                                lambda icao: catalog.airport_tz(self.apmap, icao),
                                                self.p["projection"],
                                                weights=predict.weights_for(get_settings(self.db), self.p["airline"]),
                                                ops=self.ops())
        return self._model

    # -- lógica operacional observada (pernoites e rodízio real)
    def ops(self):
        if self._ops is None:
            self._ops = ops_mod.Ops(ops_mod.get_profile(self.db), self.p["airline"],
                                    catalog.type_family(self.ac_type) if self.ac_type else None)
        return self._ops

    def night_stock(self, d: date) -> dict[str, int]:
        """Aeronaves compatíveis que o RPL exige pernoitando em cada aeroporto na noite d→d+1."""
        if self._night_stock is None:
            occs = [o for lst in self._occ_by_adep.values() for o in lst]
            dates = []
            dd = self.start - timedelta(days=1)
            while dd <= self.end:
                dates.append(dd)
                dd += timedelta(days=1)
            p = self.p
            self._night_stock = ops_mod.night_stock(
                occs, self.tz, dates, lambda t: catalog.types_compatible(self.ac_type, t, p["type_match"]))
        return self._night_stock.get(d) or {}

    def night_info(self, ap: str, d: date, base: str | None) -> tuple[float, int, float]:
        """(pontuação do pernoite, estoque RPL, pernoites ADS-B por noite)."""
        st = self.night_stock(d).get(ap, 0)
        q = self.ops()
        obs = q.nights_per_day(ap) if q.has_data else 0.0
        if self.same_place(ap, base):
            return 0.0, st, obs
        if st >= 1 or obs >= OBS_NIGHT_RATE:
            return NIGHT_OK * min(1.0, (st + obs) / 3), st, obs
        if obs > 0:
            return -NIGHT_RARE, st, obs
        return -NIGHT_NONE, st, obs

    def last_flight_score(self, o: dict) -> float:
        f = self.ops().flight(o.get("ident"), o["adep"], o["ades"]) if self.ops().has_data else None
        if not f or f["n"] < predict.OBS_MIN_N:
            return 0.0
        share = f["night"] / f["n"]
        if share >= 0.5:
            return LAST_FLIGHT * share
        if f["night"] == 0 and f["n"] >= predict.OBS_MIN_N + 1:
            return -LAST_FLIGHT
        return 0.0

    def first_flight_score(self, o: dict) -> float:
        f = self.ops().flight(o.get("ident"), o["adep"], o["ades"]) if self.ops().has_data else None
        if not f or f["first"] < 2:
            return 0.0
        return FIRST_FLIGHT * min(1.0, f["first"] / max(f["n"], f["first"]))

    def realism(self, a, b) -> float:
        """Quanto a transição a→b é mais provável que o acaso, segundo o modelo de rodízio (−3 a +3)."""
        if not self.p.get("model_realism") or {a.get("origin"), b.get("origin")} & {"SUGESTAO", "OBSERVADO"}:
            return 0.0
        if a.get("deadhead") or b.get("deadhead"):
            return 0.0
        m = self.rotation_model()
        cands = m.candidates(a)
        if not cands:
            return 0.0
        for c in cands:
            if c["occ"]["ref"] == b["ref"]:
                v = max(-REALISM_CAP, min(REALISM_CAP, math.log(c["p"] * len(cands))))
                return v * SWAP_NEG_SCALE if v < 0 and self.crew_mode() else v
        return -1.0 * (SWAP_NEG_SCALE if self.crew_mode() else 1)

    def resolve_turn(self):
        """Tempo de solo mínimo: medido no RPL (modo automático) ou o valor fixo informado."""
        p = self.p
        if p.get("turn_mode") != "auto":
            p["min_turn"] = p["min_turn_fixed"]
            return
        stats = self.idx.turn_stats().get(self.ac_type, [])
        fam = catalog.type_family(self.ac_type)
        if len(stats) >= 5:
            p10 = sorted(stats)[len(stats) // 10]
            val = max(15, p10 - p["taxi"])
            src = f"P10 de {len(stats)} continuações do mesmo voo no RPL"
        else:
            val = crew.TURN_FALLBACK.get(fam, 35)
            src = f"estimativa para a categoria {fam} (RPL com {len(stats)} amostra(s))"
        obs = predict.OBSERVED_TURN_P10.get(p["airline"])
        if obs is not None and fam not in ("ATR", "C208") and val > obs + 5:
            # chegada estimada (EOBT+EET+táxi) fica ~8 min antes do STA real: 40 min de STA→STD ≈ 45 min aqui
            val = obs + 5
            src += f"; limitado a {val} min pelas amostras observadas (solo programado mínimo de {obs} min)"
        p["min_turn"] = int(val)
        p["min_turn_source"] = src
        p["max_turn"] = max(p["max_turn"], p["min_turn"])
        self.diag("info", "solo_auto", f"Tempo de solo mínimo automático para {self.ac_type}: {val} min ({src}).")

    # -- regras de tripulação
    def crew_on(self) -> bool:
        return self.p.get("crew_mode", "off") != "off"

    def duty_of(self, chain):
        p = self.p
        rep = chain[0]["dep"] - timedelta(minutes=p["report_before"])
        rel = chain[-1]["arr"] + timedelta(minutes=p["release_after"])
        return rep, rel

    def chain_ok(self, chain) -> bool:
        if not self.crew_on() or not chain:
            return True
        chain = self.duty_prefix + list(chain)
        p = self.p
        rep, rel = self.duty_of(chain)
        tzn = self.tz(chain[0]["adep"])
        rep_l = to_local(rep, tzn)
        ops_ = [o for o in chain if not o.get("deadhead")]   # extra conta na jornada, não no voo nem nos pousos
        lim = crew.limits(rep_l, len(ops_), p["crew_mode"])
        dur = (rel - rep).total_seconds() / 3600
        block = sum((o["arr"] - o["dep"]).total_seconds() for o in ops_) / 3600
        if any(o.get("origin") == "OBSERVADO" and (o["arr"] - o["dep"]).total_seconds() >= crew.LONG_INTL_H * 3600
               for o in chain) and (dur > lim["fdp_h"] or block > lim["flight_h"]):
            lim = crew.augmented_limits(block, dur) or lim   # voo longo: tripulação composta/revezamento
        if len(ops_) > lim["max_legs"]:
            return False
        if dur > lim["fdp_h"]:
            return False
        if block > lim["flight_h"]:
            return False
        if p.get("crew_madrugada") and crew.touches_madrugada(rep, rel, self.madr_tz()):
            recent = [m for m, _d in self.madr if (rep - m).total_seconds() < 168 * 3600]
            if len(recent) >= 4:
                return False
            dates = sorted({d for _m, d in self.madr})
            dd = to_local(rep, self.madr_tz()).date()
            if dd - timedelta(days=1) in dates and dd - timedelta(days=2) in dates:
                return False
        if p.get("crew_monthly"):
            mk = rep_l.strftime("%Y-%m")
            if self.month_block.get(mk, 0) + block > crew.monthly_block_limit_h(catalog.type_family(self.ac_type)):
                return False
            if self.month_duty.get(mk, 0) + dur > 176:
                return False
            wk = rep_l.date().isocalendar()[:2]
            if self.week_duty.get(wk, 0) + dur > 44:
                return False
        return True

    def record_duty(self, chain):
        if self.duty_prefix:
            chain = self.duty_prefix + list(chain)
            self.duty_prefix = []
        rep, rel = self.duty_of(chain)
        tzn = self.tz(chain[0]["adep"])
        rep_l = to_local(rep, tzn)
        dur = (rel - rep).total_seconds() / 3600
        block = sum((o["arr"] - o["dep"]).total_seconds() for o in chain if not o.get("deadhead")) / 3600
        mk, wk = rep_l.strftime("%Y-%m"), rep_l.date().isocalendar()[:2]
        self.month_block[mk] = self.month_block.get(mk, 0) + block
        self.month_duty[mk] = self.month_duty.get(mk, 0) + dur
        self.week_duty[wk] = self.week_duty.get(wk, 0) + dur
        if crew.touches_madrugada(rep, rel, self.madr_tz()):
            self.madr.append((rep, to_local(rep, self.madr_tz()).date()))
        self.last_duty_h, self.last_release = dur, rel

    def seed_history(self, leg_rows: list[dict], continuing: bool = False):
        """Inicializa o estado das regras com pernas já existentes (regerar trecho)."""
        objs = [{"dep": parse_utc(r["dep_utc"]), "arr": parse_utc(r["arr_utc"]), "adep": r["adep"], "ades": r["ades"]}
                for r in leg_rows]
        by_day: dict = {}
        for r, o in zip(leg_rows, objs):
            dd = to_local(o["dep"], self.tz(r["adep"])).date()
            by_day.setdefault(dd, []).append({"ident": r.get("flight_number"), "adep": r["adep"], "ades": r["ades"]})
        for dd in sorted(by_day):
            self.remember(dd, by_day[dd])
        cur: list = []
        for o in objs:
            if cur and (o["dep"] - cur[-1]["arr"]).total_seconds() >= 10 * 3600:
                self.record_duty(cur)
                cur = []
            cur.append(o)
        if cur:
            if continuing:
                self.duty_prefix = cur  # a jornada continua no planejamento
            else:
                self.record_duty(cur)

    def choose_base(self) -> str:
        if self.p["base"]:
            return self.p["base"]
        counts: dict[str, int] = {}
        for f in self.idx.flights:
            if catalog.types_compatible(self.ac_type, f["type_icao"], self.p["type_match"]):
                counts[f["adep"]] = counts.get(f["adep"], 0) + 1
        if not counts:
            raise PlanError(f"Nenhum voo do tipo {self.ac_type} no RPL de {self.p['airline']}.")
        base = max(counts, key=counts.get)
        self.diag("info", "base_auto", f"Base escolhida automaticamente: {base} (aeroporto com mais partidas do tipo).")
        return base

    # -- restrições de uma ocorrência
    def occ_allowed(self, o) -> bool:
        p = self.p
        if o["ref"] in self.used_refs or o["ref"] in self.taken:
            return False
        if not catalog.types_compatible(self.ac_type, o["type_icao"], p["type_match"]):
            return False
        if not p["leg_min_minutes"] <= o["eet_min"] <= p["leg_max_minutes"]:
            return False
        if o["ades"] in p["avoid_airports"]:
            return False
        if self.aircraft:
            blocked = set(filter(None, (self.aircraft.get("blocked_airports") or "").split(",")))
            if o["adep"] in blocked or o["ades"] in blocked:
                return False
            for a, b in self.ac_windows:
                if o["dep"] < b and o["arr"] > a:
                    return False
        return True

    def unavailable_on(self, d: date) -> str | None:
        if not self.aircraft:
            return None
        for u in json.loads(self.aircraft.get("unavailable") or "[]"):
            if u["from"] <= d.isoformat() <= u["to"]:
                return u.get("reason") or "indisponível"
        return None

    def viable(self, ap: str) -> int:
        """Quantas partidas compatíveis (tipo e janela local) o aeroporto tem no período: evita pernoitar
        num aeroporto de onde a aeronave não consegue sair (ex.: só há voo de madrugada)."""
        if ap in self._viable:
            return self._viable[ap]
        p = self.p
        a, b = _hhmm(p["day_start"], "05:00"), _hhmm(p["day_end"], "23:59")
        n = 0
        for o in self._occ_by_adep.get(ap, []):
            if not catalog.types_compatible(self.ac_type, o["type_icao"], p["type_match"]):
                continue
            t = to_local(o["dep"], self.tz(ap)).time()
            if (a <= t <= b) if a < b else (t >= a or t <= b):
                n += 1
        self._viable[ap] = n
        return n

    def next_day_options(self, chain, d: date) -> int:
        """Partidas compatíveis do aeroporto final entre o fim do repouso mínimo e o fim da janela do dia seguinte."""
        p = self.p
        rep, rel = self.duty_of(self.duty_prefix + list(chain))
        rest_h = max(p["min_rest"] / 60, crew.rest_after((rel - rep).total_seconds() / 3600) if self.crew_on() else 0)
        earliest = rel + timedelta(hours=rest_h, minutes=p["report_before"] if self.crew_on() else 0)
        ap = chain[-1]["ades"]
        key = (ap, d, earliest.replace(minute=earliest.minute // 15 * 15, second=0, microsecond=0))
        cache = self.__dict__.setdefault("_next_opts", {})
        if key not in cache:
            lst, deps = self._occ_by_adep.get(ap, []), self._occ_deps.get(ap, [])
            tz = get_tz(self.tz(ap)) or timezone.utc
            we_t = _hhmm(p["day_end"], "23:59")
            nd = d + timedelta(days=1)
            end = datetime.combine(nd if we_t > _hhmm(p["day_start"], "05:00") else nd + timedelta(days=1), we_t, tz)
            end = end.astimezone(timezone.utc)
            i, n = bisect.bisect_left(deps, earliest), 0
            while i < len(lst) and lst[i]["dep"] <= end and n < 20:
                o = lst[i]
                if o["ref"] not in self.taken and catalog.types_compatible(self.ac_type, o["type_icao"], p["type_match"]):
                    n += 1
                i += 1
            cache[key] = n
        return cache[key]

    def departures(self, adep, t_from, t_to):
        lst = self._occ_by_adep.get(adep, [])
        deps = self._occ_deps.get(adep, [])
        i = bisect.bisect_left(deps, t_from)
        out = []
        while i < len(lst) and lst[i]["dep"] <= t_to:
            if self.occ_allowed(lst[i]):
                out.append(lst[i])
            i += 1
        return out

    # -- escala do tripulante
    def same_place(self, a: str | None, b: str | None) -> bool:
        """Mesmo lugar para fins de base: no modo tripulante, aeroportos da mesma cidade (GRU/CGH, GIG/SDU…)."""
        return a == b or (self.crew_mode() and crew_profiles.same_city(self.p["airline"], a, b))

    def madr_tz(self) -> str:
        """Madrugada (0h–6h) conta no fuso da base contratual (Lei 13.475 art. 42; a CCT usa o horário de Brasília)."""
        return self.tz(self.base_icao) if self.base_icao and self.tz(self.base_icao) else "America/Sao_Paulo"

    def realistic(self) -> bool:
        return bool(self.p.get("profile_realism")) and self.crew_mode()

    def is_swap(self, a: dict, b: dict) -> bool:
        """Troca de aeronave entre a e b: o rodízio real (ADS-B) mostra que a aeronave de a segue para outro voo.
        Sem dado observado para a, assume que o tripulante fica na mesma aeronave."""
        if a.get("deadhead") or b.get("deadhead"):
            return False
        if a.get("ident") and a.get("ident") == b.get("ident"):
            return False
        q = self.ops()
        if not q.has_data:
            return False
        f = q.flight(a.get("ident"), a["adep"], a["ades"])
        if not f or f["n"] < predict.OBS_MIN_N:
            return False
        nc = q.next_count(a, b)
        return not (nc and nc[0] > 0)

    def deadhead(self, adep: str, ades: str, t_from: datetime, t_to: datetime, chain: list) -> dict | None:
        """Primeiro voo real da companhia (qualquer tipo) de adep para ades na janela, viável pelas regras."""
        lst, deps = self._occ_by_adep.get(adep, []), self._occ_deps.get(adep, [])
        i = bisect.bisect_left(deps, t_from)
        while i < len(lst) and lst[i]["dep"] <= t_to:
            o = lst[i]
            i += 1
            if not self.same_place(o["ades"], ades) or o.get("origin") == "OBSERVADO":
                continue
            dh = {**o, "deadhead": True}
            if self.chain_ok(list(chain) + [dh]):
                return dh
        return None

    # -- um dia
    def plan_day(self, d: date, loc: str, t_avail: datetime, base: str, final_day: bool,
                 must_end: str | None = None, end_limit: datetime | None = None,
                 max_legs: int | None = None, end_at_base: bool = False, wide: bool = False,
                 anomaly: bool = False, target: str | None = None, edge: bool = False) -> list[dict]:
        p = self.p
        max_legs = p["legs_max"] if max_legs is None else max_legs
        if max_legs <= 0:
            return []
        tz = get_tz(self.tz(loc)) or timezone.utc
        ds_t = time(0, 0) if wide else _hhmm(p["day_start"], "05:00")
        we_t = time(23, 59) if wide else _hhmm(p["day_end"], "23:59")
        ws = datetime.combine(d, ds_t, tz).astimezone(timezone.utc)
        we = datetime.combine(d if we_t > ds_t else d + timedelta(days=1), we_t, tz)
        we = we.astimezone(timezone.utc)
        if end_limit and end_limit < we:
            we = end_limit
        start_t = max(ws, t_avail)
        if start_t > we:
            return []
        prefer = set(p["preferred_airports"])
        feeders = self.pref_feeders() if prefer else set()
        pref_yday = bool(prefer) and any(o["ades"] in prefer for dd, legs_ in self.flown if (d - dd).days == 1
                                          for o in legs_)
        nd = d + timedelta(days=1)
        next_off = nd in self.day_plan and self.day_plan[nd][0] == "off"
        want_base = p["return_to_base"] == "daily" or (p["return_to_base"] == "end" and final_day) or end_at_base
        target_end = must_end or target or (base if want_base else None)

        recent = [(dd, legs_) for dd, legs_ in self.flown if 0 < (d - dd).days <= 10]
        last_ident: dict[str, int] = {}
        pair_cnt: Counter = Counter()
        seen_aps: set = set()
        seqs = set()
        for dd, legs_ in recent:
            ago = (d - dd).days
            seqs.add(tuple(o["ident"] for o in legs_))
            for o in legs_:
                if o.get("ident"):
                    last_ident[o["ident"]] = min(ago, last_ident.get(o["ident"], 99))
                if ago <= 7:
                    pair_cnt[(min(o["adep"], o["ades"]), max(o["adep"], o["ades"]))] += 1
                if ago <= 5:
                    seen_aps.add(o["ades"])

        intl_goal = self.intl_target()
        intl_user = self.p.get("international") and self.p.get("intl_target_pct") not in (None, "")

        def diversity(chain):
            sc = 0.0
            for o in chain:
                ago = last_ident.get(o["ident"])
                if ago is not None:
                    sc -= IDENT_REPEAT * (11 - ago) / 10
                if o["ades"] not in prefer and o["adep"] not in prefer:   # trecho preferido pode repetir
                    sc -= PAIR_RECENT * pair_cnt[(min(o["adep"], o["ades"]), max(o["adep"], o["ades"]))]
                if o["ades"] not in seen_aps and o["ades"] != base:
                    sc += NEW_AIRPORT
            if chain and tuple(o["ident"] for o in chain) in seqs:
                sc -= SAME_SEQUENCE
            return sc

        # ritmo mensal: reparte as horas de voo que sobram no mês pelos dias de trabalho que faltam
        day_budget_h = None
        if self.crew_on() and p.get("crew_monthly") and self.day_plan:
            mk = d.strftime("%Y-%m")
            lim = crew.monthly_block_limit_h(catalog.type_family(self.ac_type))
            left_h = lim - self.month_block.get(mk, 0.0)
            left_days = sum(1 for dd, v in self.day_plan.items()
                            if dd >= d and dd.strftime("%Y-%m") == mk and v[0] in ("work",))
            if left_days > 0:
                day_budget_h = max(1.5, left_h / left_days)

        realistic = self.realistic()
        custom = p.get("legs2_pct") is not None
        goal_mode = realistic or custom   # pontua por horas de voo + meta de etapas (perfil ou sliders do usuário)
        legs_goal = None
        pr_day = self.legs_prior()
        if goal_mode:
            # meta de pernas do dia sorteada da distribuição real da companhia (ou da escolhida nos sliders); na
            # saída/volta de folga, 1 perna (deslocamento longo REC-POA, VCP-REC…) é bem mais comum
            pr = dict(pr_day)
            if edge and realistic and not custom:
                pr[1] = pr.get(1, 0.1) * EDGE_ONE_LEG
            ks = [k for k in sorted(pr) if k <= max_legs]
            if ks and custom:
                # escolhida nos sliders: a meta vai para o número de etapas mais abaixo do pedido até aqui (corrige
                # sozinha os dias em que 3 etapas não couberam nas regras)
                done = Counter(min(len([o for o in legs_ if not o.get("deadhead")]), max(pr)) for _d, legs_ in self.flown
                               if any(not o.get("deadhead") for o in legs_))
                tot = sum(done.values()) + 1
                legs_goal = max(ks, key=lambda k: (pr[k] * tot - done.get(k, 0), pr[k], -k))
            elif ks and edge:
                legs_goal = self.legs_rng.choices(ks, weights=[pr[k] for k in ks], k=1)[0]
            elif ks:
                # "baralho" com a proporção real (20 cartas): o mês sai perto da distribuição de verdade, sem o acaso
                # de um sorteio independente por dia
                if not self._goal_deck:
                    deck = [k for k in ks for _ in range(max(1, round(pr[k] * 20)))]
                    self.legs_rng.shuffle(deck)
                    self._goal_deck = deck
                legs_goal = self._goal_deck.pop()

        def score(chain, final=True):
            ops_ = [o for o in chain if not o.get("deadhead")]
            n = len(ops_)
            if goal_mode:
                # como nas escalas reais: horas de voo valem, não o número de pernas; a quantidade de pernas segue a
                # distribuição real da companhia (dia de 5 pernas curtas perde para 2–3 etapas longas)
                blk_h = sum((o["arr"] - o["dep"]).total_seconds() for o in ops_) / 3600
                sc = (PROD_DAY_BONUS if n else 0) + blk_h * PROD_BLOCK_W
                if n and final:
                    sc += PROD_PRIOR_W * _log_prior(pr_day, n)
                    if legs_goal:   # a meta orienta, mas nunca faz "não voar" valer mais que voar
                        if custom:   # escolhida nos sliders: segue mais à risca
                            sc -= min(PROD_DAY_BONUS - 20, CUSTOM_TARGET_W * abs(n - legs_goal))
                        else:
                            sc -= min(PROD_TARGET_CAP, PROD_TARGET_W * abs(n - legs_goal))
                    if day_budget_h:
                        sc -= PROD_SHORT_W * max(0.0, 0.8 * day_budget_h - blk_h)
                elif n:   # cadeia parcial: só pune o que já passou do comum
                    sc += PROD_PRIOR_W * min(0.0, _log_prior(pr_day, n)) if n >= 4 else 0
            else:
                lm = min(p["legs_min"], max_legs)
                sc = min(n, lm) * 100 + max(0, n - lm) * 45   # perna extra vale mais que a penalidade de repetição
                sc += sum(o["eet_min"] for o in chain) * 0.05
            if prefer:
                # escala direcionada: puxa a escala para os aeroportos de preferência (um pouco menos se já foi ontem)
                hits = sum(1 for o in chain if o["ades"] in prefer and not o.get("deadhead"))
                k = (0.7 if pref_yday else 1.0) * PREF_STRENGTH[p.get("pref_strength") or "normal"]
                if hits:
                    sc += k * (PREF_VISIT + PREF_MORE * (hits - 1))
                    if final and chain[-1]["ades"] in prefer:
                        sc += PREF_PERNOITE
                elif final and chain and chain[-1]["ades"] in feeders:
                    sc += k * PREF_FEEDER
            if chain and chain[0].get("deadhead"):
                sc -= DHD_START_COST
            if chain and chain[0]["adep"] != loc and not chain[0].get("deadhead"):
                sc -= GROUND_TRANSFER_COST
            for a, b in zip(chain, chain[1:]):
                if b.get("deadhead"):
                    sc -= DEADHEAD_COST
                    continue
                if a.get("deadhead"):
                    continue
                sc -= (b["dep"] - a["arr"]).total_seconds() / 60 * 0.05
                if a["ident"] == b["ident"]:
                    sc += 5
                sc += REALISM_W * self.realism(a, b)
                if self.crew_mode() and self.is_swap(a, b) and self.night_stock(d).get(a["ades"], 0) < 2 \
                        and a["ades"] != base:
                    sc -= SWAP_OUTSTATION
            extra = repeat_excess(self.duty_prefix + list(chain))
            if extra:
                # vai-e-volta repetido no mesmo par: anormalidade. Fora do dia sorteado, pesa mais que uma
                # perna a menos; no dia sorteado, é favorecido para que a anormalidade apareça.
                sc += 40 * extra if anomaly else -REPEAT_PENALTY * extra
            if chain and self.prev_day_idents and tuple(o["ident"] for o in chain) == self.prev_day_idents:
                sc -= MONOTONY_PENALTY  # mesmo dia de ontem, voo a voo
            sc += diversity(chain)
            n_i = sum(1 for o in chain if o.get("origin") == "OBSERVADO")
            if intl_user:
                # alvo escolhido pelo usuário: premia as pernas internacionais até a cota e cobra as que passam
                # dela; cobra também as domésticas quando o dia deixa a escala devendo internacionais
                owed = intl_goal * (self.n_legs + n) - self.n_intl
                due = max(0, int(owed + 0.5))
                sc += INTL_USER_BONUS * min(n_i, due) - INTL_USER_OVER * max(0, n_i - due)
                if n_i < due:
                    sc -= INTL_USER_MISS * (due - n_i)
            elif n_i:
                # quanto a escala "deve" de pernas internacionais para chegar à proporção real; espalha no mês
                owed = intl_goal * (self.n_legs + n) - self.n_intl
                sc += INTL_BONUS * n_i if owed >= 0.3 else -20 * n_i
            if day_budget_h is not None and chain:
                blk = sum((o["arr"] - o["dep"]).total_seconds() for o in chain) / 3600
                if blk > day_budget_h * 1.2:
                    sc -= 60 * (blk - day_budget_h * 1.2)   # guarda horas para o resto do mês
            if chain and not must_end:
                v = self.viable(chain[-1]["ades"])
                if v == 0:
                    sc -= 400
                elif not final_day and not next_off:
                    # bom senso: depois do repouso, há voo para sair daqui no dia seguinte?
                    nxt_n = self.next_day_options(chain, d)
                    sc += -400 if nxt_n == 0 else -40 if nxt_n == 1 else min(nxt_n, 20) * 0.5
                else:
                    sc += min(v, 20) * 0.5
            if target_end:
                end_ap = chain[-1]["ades"] if chain else loc
                if must_end:
                    sc += 10_000 if end_ap == must_end else -10_000
                elif self.same_place(end_ap, target_end):
                    sc += 90
                else:
                    # tripulante: folga e reserva começam em casa; a volta como extra existe para isso
                    sc -= 200 if (self.crew_mode() and end_at_base) else 60
            if chain and final and not must_end:
                # pernoite: a companhia deixa aeronave dormir aqui? (RPL + ADS-B) e o último voo costuma encerrar o dia?
                sc += self.night_info(chain[-1]["ades"], d, base)[0]
                if not self.crew_mode():   # no modo tripulante a aeronave segue com outra tripulação
                    sc += self.last_flight_score(chain[-1])
            if chain and not self.duty_prefix and not chain[0].get("deadhead"):
                sc += self.first_flight_score(chain[0])
            if chain and (p.get("avoid_madrugada") or p.get("time_pref")):
                rep_, rel_ = self.duty_of(self.duty_prefix + list(chain))
                tzn = self.tz(chain[0]["adep"])
                if p.get("avoid_madrugada") and crew.touches_madrugada(rep_, rel_, self.madr_tz()):
                    sc -= BID_MADRUGADA
                h = to_local(rep_, tzn).hour
                if p.get("time_pref") == "manha" and h >= 12 or p.get("time_pref") == "tarde" and h < 11:
                    sc -= BID_TIME
            return sc + self.rng.random() * 8

        beam = [([], loc, start_t)]
        finals = [([], score([]))]
        if realistic and not must_end and not self.duty_prefix:
            # v2.2: o dia pode começar noutro aeroporto da mesma cidade (transporte terrestre da companhia) ...
            gt = crew_profiles.profile(p["airline"])["ground_transfer_min"]
            for sib in crew_profiles.siblings(p["airline"], loc):
                if self._occ_by_adep.get(sib):
                    beam.append(([], sib, start_t + timedelta(minutes=gt)))
            # ... ou como extra (passageiro) até a base, voando de lá (ex.: MCZ→REC em extra e depois REC-UDI-REC)
            if p.get("allow_deadhead") and self.base_icao and not self.same_place(loc, self.base_icao):
                dh = self.deadhead(loc, self.base_icao, start_t, min(we, start_t + timedelta(hours=8)), [])
                if dh:
                    beam.append(([dh], dh["ades"], dh["arr"]))
                    finals.append(([dh], score([dh])))
        for depth in range(max_legs):
            nxt = []
            for chain, ap, t in beam:
                if chain:
                    lo, hi = t + timedelta(minutes=p["min_turn"]), t + timedelta(minutes=p["max_turn"])
                else:
                    lo, hi = t, we
                hi = min(hi, we)
                cands = [o for o in self.departures(ap, lo, hi) if all(o["ref"] != c["ref"] for c in chain)]
                if end_limit:
                    cands = [o for o in cands if o["arr"] + timedelta(minutes=p["min_turn"]) <= end_limit]
                self.rng.shuffle(cands)
                # sucessores vistos no rodízio real (ADS-B) entram primeiro na busca
                obs_next = set()
                if chain and self.ops().has_data:
                    f = self.ops().flight(chain[-1].get("ident"), chain[-1]["adep"], chain[-1]["ades"])
                    if f:
                        obs_next = {k for k, _v in sorted(f["next"].items(), key=lambda kv: -kv[1])[:OBS_PRIORITY]}
                # mais cedo primeiro, com folga aleatória (até 90 min) e empurrando voos repetidos recentes para trás
                cands.sort(key=lambda o: (ops_mod.flight_key(o.get("ident"), o["adep"], o["ades"]) not in obs_next,
                                          o["ades"] not in prefer, (o["dep"] - lo).total_seconds()
                                          + self.rng.random() * 5400
                                          + (3600 * (11 - last_ident[o["ident"]]) / 10 if o["ident"] in last_ident else 0)))
                added = 0
                extra_pref = 0
                for o in cands:
                    if added >= self.BRANCH:
                        # voos para aeroportos de preferência entram na busca mesmo depois dos BRANCH mais cedo
                        if not (o["ades"] in prefer or (depth == 0 and o["ades"] in feeders)) or extra_pref >= PREF_EXPLORE:
                            continue
                        extra_pref += 1
                    if chain and self.crew_mode() and (self.is_swap(chain[-1], o) or chain[-1].get("deadhead")) and \
                            o["dep"] < chain[-1]["arr"] + timedelta(minutes=p["swap_min"]):
                        continue   # troca de aeronave (ou chegada como extra): conexão mínima do tripulante
                    if not self.chain_ok(chain + [o]):
                        continue
                    nxt.append((chain + [o], o["ades"], o["arr"]))
                    added += 1
            if not nxt:
                break
            scored = sorted(nxt, key=lambda c: -score(c[0], False))[: self.BEAM]
            beam = scored
            finals += [(c[0], score(c[0])) for c in scored]
        if self.crew_mode() and p.get("allow_deadhead") and target_end and not must_end:
            # tripulante longe de onde precisa terminar (base antes da folga, reserva): volta como extra em voo real
            extra = []
            for chain, _sc in sorted(finals, key=lambda f: -f[1])[:12]:
                end_ap = chain[-1]["ades"] if chain else loc
                if self.same_place(end_ap, target_end):
                    continue
                t_from = chain[-1]["arr"] + timedelta(minutes=p["swap_min"]) if chain else start_t
                dh = self.deadhead(end_ap, target_end, t_from, we + timedelta(hours=2), chain)
                if dh:
                    extra.append((chain + [dh], score(chain + [dh])))
            finals += extra
        if must_end:
            ok = [f for f in finals if (f[0][-1]["ades"] if f[0] else loc) == must_end]
            if not ok:
                return []
            finals = ok
        best = self._pick(finals)
        return best

    def legs_prior(self) -> dict:
        """Distribuição de etapas por chave: a do perfil da companhia ou a dos sliders (2 e 3 etapas escolhidas, o resto
        repartido entre 1, 4 e 5 etapas na proporção do perfil)."""
        prof = dict(crew_profiles.profile(self.p["airline"])["legs_prior"])
        if self.p.get("legs2_pct") is None:
            return prof
        a, b = self.p["legs2_pct"] / 100, self.p["legs3_pct"] / 100
        rest = max(0.0, 1 - a - b)
        others = {k: v for k, v in prof.items() if k not in (2, 3)}
        tot = sum(others.values()) or 1
        out = {k: rest * v / tot for k, v in others.items()}
        out[2], out[3] = a, b
        return {k: v for k, v in sorted(out.items())}

    def pref_feeders(self) -> set:
        """Aeroportos com voo direto (tipo compatível) para algum aeroporto de preferência: aproximam a escala."""
        if self._feeders is None:
            prefer = set(self.p["preferred_airports"])
            out = set()
            for ap, lst in self._occ_by_adep.items():
                if ap in prefer:
                    continue
                if any(o["ades"] in prefer and catalog.types_compatible(self.ac_type, o["type_icao"], self.p["type_match"])
                       for o in lst):
                    out.add(ap)
            self._feeders = out
        return self._feeders

    def _pick(self, finals: list) -> list:
        """Sorteia entre as melhores opções do dia (dentro de 30 pontos da melhor), com peso exp(Δ/T).
        Dá variedade entre dias e sementes sem aceitar escolhas ruins."""
        uniq: dict = {}
        for chain, sc in finals:
            k = tuple(o["ref"] for o in chain)
            if k not in uniq or sc > uniq[k][1]:
                uniq[k] = (chain, sc)
        opts = sorted(uniq.values(), key=lambda x: -x[1])
        top = opts[0][1]
        if PICK_TEMPERATURE <= 0:
            return opts[0][0]
        near = [(c, sc) for c, sc in opts[:8] if sc >= top - 30]
        weights = [math.exp((sc - top) / PICK_TEMPERATURE) for _c, sc in near]
        return self.rng.choices([c for c, _ in near], weights=weights, k=1)[0]

    def remember(self, d: date, legs: list[dict]):
        if legs:
            self.flown.append((d, list(legs)))
            self.n_legs += len(legs)
            self.n_intl += sum(1 for o in legs if o.get("origin") == "OBSERVADO")

    def intl_target(self) -> float:
        if "intl_target_pct" in self.p and self.p["intl_target_pct"] not in (None, ""):
            return float(self.p["intl_target_pct"]) / 100
        return INTL_TARGET.get((self.p["airline"], catalog.type_family(self.ac_type)), 0.03)

    # -- reserva
    def reserve_candidates(self, base: str) -> list[str]:
        """Aeroportos da operação onde a reserva pode ficar: com partidas compatíveis (≈ 1 por dia ou mais)."""
        p = self.p
        ndays = (self.end - self.start).days + 1
        blocked = set(filter(None, ((self.aircraft or {}).get("blocked_airports") or "").split(",")))
        out = []
        for ap in self._occ_by_adep:
            if ap == base or ap in blocked or ap in p["avoid_airports"] or not self.tz(ap) or intl_mod.is_foreign(ap):
                continue
            if self.viable(ap) >= max(3, ndays):
                out.append(ap)
        return sorted(out)

    def fixed_off_days(self, d_from: date, d_to: date) -> set[date]:
        """Dias que a reserva não pode ocupar: indisponibilidade da aeronave e folga fixa do dia da semana."""
        out = set()
        if self.aircraft:
            for u in json.loads(self.aircraft.get("unavailable") or "[]"):
                a, b = date.fromisoformat(u["from"]), date.fromisoformat(u["to"])
                while a <= b:
                    out.add(a)
                    a += timedelta(days=1)
        d = d_from
        while d <= d_to:
            if self.p["weekday_max"].get(str(d.weekday())) == 0:
                out.add(d)
            d += timedelta(days=1)
        for a, b in self.ac_windows:  # voos ou reservas da mesma aeronave em outras escalas
            x = a.date() - timedelta(days=1)
            while x <= b.date() + timedelta(days=1):
                la = datetime.combine(x, time(0, 0), timezone.utc)
                if a < la + timedelta(days=1, hours=3) and b > la + timedelta(hours=3):  # dia local (UTC-3) aproximado
                    out.add(x)
                x += timedelta(days=1)
        return out

    def draw_reserves(self, base: str):
        if self.p.get("kind") in LONG_KINDS:
            # escala longa: reserve_blocks por mês, sorteados dentro de cada mês (não se amontoam)
            blocks, warns, cands = [], [], self.reserve_candidates(base)
            for k, (ms, me) in enumerate(month_windows(self.start, self.end)):
                pk = {**self.p, "reserve_seed": int(self.p["reserve_seed"]) + 7919 * (k + 1)}
                b, w = reserve_mod.draw(pk, ms, me, self.fixed_off_days(ms, me), base, cands)
                blocks += b
                warns += [f"{ms:%m/%Y}: {x}" for x in w]
        else:
            blocks, warns = reserve_mod.draw(self.p, self.start, self.end, self.fixed_off_days(self.start, self.end),
                                             base, self.reserve_candidates(base))
        self.reserves = blocks
        for w in warns:
            self.diag("warning", "reserva", w)
        for b in blocks:
            self.diag("info", "reserva_sorteio",
                      f"Reserva sorteada: {b['start_date']:%d/%m} a {b['end_date']:%d/%m} ({b['days']} dias) em "
                      f"{b['location']} ({'base' if b['location_kind'] == 'base' else 'fora da base'}; sorteio "
                      f"{b['location_roll']:.0f} vs {b['base_pct']:.0f}% base); acionamento "
                      + (f"sorteado ({b['activation_roll']:.0f} < {b['activation_pct']:.0f}%) em "
                         f"{b['act_start']:%d/%m} por {b['act_days']} dia(s)." if b["activated"] else
                         f"não sorteado ({b['activation_roll']:.0f} ≥ {b['activation_pct']:.0f}%)."))

    def _snapshot(self):
        return (set(self.taken), list(self.madr), dict(self.month_block), dict(self.month_duty), dict(self.week_duty),
                self.last_duty_h, self.last_release, list(self.duty_prefix), self.prev_day_idents, list(self.flown),
                self.n_legs, self.n_intl)

    def _restore(self, snap):
        (self.taken, self.madr, self.month_block, self.month_duty, self.week_duty, self.last_duty_h,
         self.last_release, self.duty_prefix, self.prev_day_idents, self.flown, self.n_legs, self.n_intl) = snap
        self.flown = list(self.flown)
        self.taken, self.madr = set(self.taken), list(self.madr)
        self.month_block, self.month_duty, self.week_duty = dict(self.month_block), dict(self.month_duty), dict(self.week_duty)
        self.duty_prefix = list(self.duty_prefix)

    def _day_t0(self, d: date, loc: str, last_arr: datetime, had_legs: bool) -> datetime:
        """Primeiro horário possível no dia (repouso desde a última jornada)."""
        p = self.p
        if not had_legs:
            return last_arr
        t0 = last_arr + timedelta(minutes=p["min_rest"])
        if self.crew_on() and self.last_release is not None:
            t0 = max(t0, self.last_release + timedelta(hours=crew.rest_after(self.last_duty_h), minutes=p["report_before"]))
        return t0

    def plan_activation(self, r: dict, loc: str, last_arr: datetime, had_legs: bool, base: str):
        """Voos reais do RPL no acionamento: saem do local da reserva e voltam a ele no último dia acionado.
        Tenta a duração sorteada e, sem sequência possível, durações menores; sem nenhuma: inviável."""
        p = self.p
        snap = self._snapshot()
        drawn = int(r["act_days"] or 1)
        for n in range(drawn, 0, -1):
            self._restore(snap)
            legs: list[dict] = []
            cur_loc, cur_arr, had, ok = loc, last_arr, had_legs, True
            for i in range(n):
                dd = r["act_start"] + timedelta(days=i)
                wmax = self.day_plan.get(dd, ("reserve", "", None))[2]
                max_legs = p["legs_max"] if wmax is None else wmax
                if p.get("crew_mode") == "lei":
                    max_legs = min(max_legs, 4)
                t0 = self._day_t0(dd, cur_loc, cur_arr, had)
                last = i == n - 1
                day = self.plan_day(dd, cur_loc, t0, base, False, must_end=r["location"] if last else None,
                                    max_legs=max_legs)
                if not day or any(o["origin"] == "SUGESTAO" for o in day):
                    ok = False
                    break
                # cópia: as ocorrências vêm do índice do RPL em cache e não podem ser marcadas
                day = [{**o, "activity": "RESERVA", "reserve_uid": r["uid"]} for o in day]
                for o in day:
                    self.taken.add(o["ref"])
                self.record_duty(day)
                self.prev_day_idents = tuple(o["ident"] for o in day)
                self.remember(dd, day)
                legs += day
                cur_loc, cur_arr, had = day[-1]["ades"], day[-1]["arr"], True
            if ok:
                r["status"] = "acionado"
                r["note"] = (r.get("note") or "") + (
                    f" Acionamento simulado: {len(legs)} perna(s) do RPL em {n} dia(s), saindo e voltando a {r['location']}."
                    + (f" O sorteio pediu {drawn} dia(s); não havia sequência compatível tão longa." if n < drawn else ""))
                self.diag("info", "reserva_acionada", f"Reserva acionada em {r['act_start']:%d/%m}: {len(legs)} perna(s) "
                                                      f"em {n} dia(s) a partir de {r['location']}.", r["act_start"].isoformat())
                return legs, cur_arr
        self._restore(snap)
        r["status"] = "inviavel"
        r["note"] = (r.get("note") or "") + (
            f" O sorteio indicou acionamento em {r['act_start']:%d/%m} ({drawn} dia(s)), mas não há sequência de voos do "
            f"RPL compatível saindo de {r['location']} e voltando a ele dentro das regras. Nenhum voo foi inventado.")
        self.diag("warning", "reserva_inviavel", f"Acionamento da reserva em {r['act_start']:%d/%m} inviável: sem sequência "
                                                 f"compatível a partir de {r['location']}.", r["act_start"].isoformat())
        return [], last_arr

    # -- v2.2: sobreaviso e inativo
    def plan_sobreaviso(self, d: date, loc: str, last_arr: datetime, had_legs: bool, base: str):
        """Dia de SOBREAVISO em casa (Lei 13.475 art. 43: 3 a 12 h, apresentação em até 90 min).
        Acionamento sorteado (sobreaviso_activation_pct); acionado, voa voos reais do RPL saindo da base."""
        p = self.p
        tz = get_tz(self.tz(base)) or timezone.utc
        w0 = datetime.combine(d, time(6, 0), tz)
        rec = {"date": d, "kind": "sobreaviso", "location": base, "status": "sem_acionamento",
               "data": {"window": "06:00–18:00", "roll": None}, "note": ""}
        self.day_records.append(rec)
        if not self.same_place(loc, base):
            rec["note"] = f"Tripulante em {loc} na véspera: sobreaviso cumprido na base {base} após deslocamento."
        roll = self.sa_rng.random() * 100
        rec["data"]["roll"] = round(roll, 1)
        if roll >= float(p.get("sobreaviso_activation_pct", 35)):
            rec["note"] = (rec["note"] + " " if rec["note"] else "") + \
                f"Não acionado (sorteio {roll:.0f} ≥ {p.get('sobreaviso_activation_pct', 35)}%)."
            self.diag("info", "sobreaviso", f"{d:%d/%m}: sobreaviso em {base}, não acionado.", d.isoformat())
            return [], last_arr
        call = w0 + timedelta(minutes=self.sa_rng.randint(0, 8 * 60))
        rec["data"]["call"] = call.strftime("%H:%M")
        t0 = max(self._day_t0(d, base, last_arr, had_legs), (call + timedelta(minutes=90)).astimezone(timezone.utc))
        nxt = d + timedelta(days=1)
        off_next = nxt in self.day_plan and self.day_plan[nxt][0] in ("off", "sobreaviso")
        day = self.plan_day(d, loc, t0, base, False, max_legs=min(p["legs_max"], 4),
                            end_at_base=off_next and bool(p.get("crew_folga_base")))
        if not day or any(o["origin"] == "SUGESTAO" for o in day):
            rec["status"] = "inviavel"
            rec["note"] = (rec["note"] + " " if rec["note"] else "") + \
                f"Sorteio indicou acionamento às {call:%H:%M}, mas não há voo compatível saindo de {base}."
            return [], last_arr
        day = [{**o, "activity": "SOBREAVISO"} for o in day]
        for o in day:
            self.taken.add(o["ref"])
        self.record_duty(day)
        self.prev_day_idents = tuple(o["ident"] for o in day)
        self.remember(d, day)
        rec["status"] = "acionado"
        rec["note"] = (rec["note"] + " " if rec["note"] else "") + \
            f"Acionado às {call:%H:%M} (apresentação em até 90 min): {len(day)} perna(s) do RPL."
        self.diag("info", "sobreaviso", f"{d:%d/%m}: sobreaviso acionado às {call:%H:%M} em {base}.", d.isoformat())
        return day, day[-1]["arr"]

    def inactive_today(self, d: date, loc: str, base: str, had_legs: bool) -> bool:
        """Dia INATIVO fora da base (descanso longo, sem voo, não é folga): depois de 2 madrugadas seguidas ou, às
        vezes, no meio da viagem num hub. Nunca na véspera de folga/sobreaviso nem no último dia."""
        p = self.p
        if not (self.realistic() and p.get("inactive_days") and had_legs) or self.same_place(loc, base):
            return False
        nxt = d + timedelta(days=1)
        if nxt not in self.day_plan or self.day_plan[nxt][0] != "work":
            return False
        if self.day_records and self.day_records[-1]["kind"] == "inativo" and \
                self.day_records[-1]["date"] == d - timedelta(days=1):
            return False
        prof = crew_profiles.profile(p["airline"])
        md = {dd for _m, dd in self.madr}
        roll = self.sa_rng.random() * 100
        why = None
        if d - timedelta(days=1) in md and d - timedelta(days=2) in md:
            if roll < prof["inactive_after_madr_pct"]:
                why = "descanso depois de 2 madrugadas seguidas"
        elif self.viable(loc) >= 10 and roll < prof["inactive_mid_trip_pct"]:
            why = "descanso longo no meio da viagem"
        if not why:
            return False
        self.day_records.append({"date": d, "kind": "inativo", "location": loc, "status": "", "data": {},
                                 "note": f"Sem voo em {loc} ({why}); não conta como folga (folga começa na base)."})
        self.diag("info", "inativo", f"{d:%d/%m}: INATIVO em {loc} ({why}).", d.isoformat())
        return True

    def plan(self, loc: str, t_avail: datetime, d_from: date, d_to: date, base: str, had_legs: bool,
             must_end: str | None = None, end_limit: datetime | None = None) -> list[dict]:
        legs: list[dict] = []
        p = self.p
        self.base_icao = base
        unav = set()
        if self.aircraft:
            for u in json.loads(self.aircraft.get("unavailable") or "[]"):
                a, b = date.fromisoformat(u["from"]), date.fromisoformat(u["to"])
                while a <= b:
                    unav.add(a)
                    a += timedelta(days=1)
        reserved = set()
        for r in self.reserves:
            x = r["start_date"]
            while x <= r["end_date"]:
                if d_from <= x <= d_to:
                    reserved.add(x)
                x += timedelta(days=1)
        self.day_plan = crew.plan_days_off(d_from, d_to, p["weekday_max"], unav, p, reserved)
        offs = [(dd, r) for dd, (k, r, _m) in sorted(self.day_plan.items()) if k == "off" and r != "indisponível"]
        prev_planned = set(p.get("planned_days_off") or []) - {dd.isoformat() for dd in self.day_plan}
        p["planned_days_off"] = sorted(prev_planned | {dd.isoformat() for dd, _r in offs})
        if offs and p.get("kind") in LONG_KINDS:
            per_m = Counter(f"{dd:%m/%Y}" for dd, _r in offs)
            self.diag("info", "folgas", f"Folgas: {len(offs)} dias no período (" +
                      ", ".join(f"{m}: {n}" for m, n in per_m.items()) + "). Datas no calendário.")
        elif offs:
            self.diag("info", "folgas", "Folgas: " + "; ".join(f"{dd:%d/%m} ({r})" for dd, r in offs) + ".")
        d = d_from
        last_arr = t_avail
        prev_off = False
        stuck = False
        span = max(1, (d_to - d_from).days + 1)
        while d <= d_to:
            if self.progress:
                self.progress(0.05 + 0.85 * (d - d_from).days / span, f"planejando {d:%d/%m/%Y}")
            kind, reason, wmax = self.day_plan[d]
            if kind == "reserve":
                r = reserve_mod.covering(self.reserves, d)
                r["note"] = ""
                if loc != r["location"]:
                    r["note"] = (r.get("note") or "") + (
                        f" Deslocamento {loc}→{r['location']} antes da reserva (tripulante em extra, sem voo atribuído).")
                    self.diag("info", "reserva_deslocamento", f"{d:%d/%m}: deslocamento {loc}→{r['location']} para a "
                                                              "reserva (tripulante em extra).", d.isoformat())
                    loc = r["location"]
                r["status"] = "sem_acionamento"
                if r["activated"] and r["act_start"]:
                    act, last_arr2 = self.plan_activation(r, loc, last_arr, had_legs, base)
                    if act:
                        legs += act
                        last_arr, had_legs = last_arr2, True
                        loc = act[-1]["ades"]
                elif not r.get("note"):
                    r["note"] = (f"Sem acionamento: sorteio {r['activation_roll']:.0f} ≥ {r['activation_pct']:.0f}%. "
                                 f"Período todo de reserva em {r['location']}, sem voos.")
                # o bloco inteiro foi tratado; segue no dia seguinte ao fim da reserva
                d = r["end_date"] + timedelta(days=1)
                prev_off = False
                stuck = False
                continue
            if kind == "sobreaviso":
                sa_legs, last_arr2 = self.plan_sobreaviso(d, loc, last_arr, had_legs, base)
                if sa_legs:
                    legs += sa_legs
                    last_arr, had_legs, loc = last_arr2, True, sa_legs[-1]["ades"]
                prev_off = False
                stuck = False
                d += timedelta(days=1)
                continue
            if kind == "work" and self.inactive_today(d, loc, base, had_legs):
                d += timedelta(days=1)
                prev_off = False
                continue
            if kind == "off":
                if reason == "indisponível":
                    self.diag("info", "indisponivel", f"{d:%d/%m}: aeronave indisponível ({self.unavailable_on(d)}).",
                              d.isoformat())
                if p.get("crew_folga_base") and self.crew_on() and had_legs and not self.same_place(loc, base) \
                        and reason != "indisponível":
                    self.diag("warning", "folga_fora_base", f"{d:%d/%m}: folga começa fora da base ({loc}).",
                              d.isoformat())
                prev_off = True
                d += timedelta(days=1)
                continue
            if had_legs:
                # só continua a mesma jornada no 1º dia de um "regerar a partir da perna";
                # entre dias planejados sempre há repouso.
                same_day = d == d_from and bool(self.duty_prefix)
                if same_day:
                    t0 = last_arr + timedelta(minutes=p["min_turn"])
                else:
                    t0 = last_arr + timedelta(minutes=p["min_rest"])
                    if self.crew_on() and self.last_release is not None:
                        rest = crew.rest_after(self.last_duty_h)
                        t0 = max(t0, self.last_release + timedelta(hours=rest, minutes=p["report_before"]))
            else:
                t0 = last_arr
            if prev_off and self.crew_on() and p.get("crew_days_off"):
                tz = get_tz(self.tz(loc)) or timezone.utc
                t0 = max(t0, datetime.combine(d, time(10, 0), tz).astimezone(timezone.utc)
                         + timedelta(minutes=p["report_before"]))
            edge_day = prev_off
            prev_off = False
            final = d == d_to
            nxt = d + timedelta(days=1)
            off_next = nxt in self.day_plan and (self.day_plan[nxt][0] == "sobreaviso" or (
                self.day_plan[nxt][0] == "off" and self.day_plan[nxt][1] != "indisponível"))
            max_legs = p["legs_max"] if wmax is None else wmax  # limite do dia da semana tem prioridade
            if p.get("crew_mode") == "lei":
                max_legs = min(max_legs, 4)
            anomaly = self.anomaly_rng.random() * 100 < p.get("anomaly_repeat_pct", 0)
            kw = dict(max_legs=max_legs, end_at_base=bool(off_next and p.get("crew_folga_base") and self.crew_on()),
                      anomaly=anomaly, edge=edge_day or off_next)
            nxt_res = reserve_mod.covering(self.reserves, nxt)
            if nxt_res and nxt_res["start_date"] == nxt:
                kw["target"] = nxt_res["location"]  # terminar o dia onde a reserva começa
            day = self.plan_day(d, loc, t0, base, final, must_end if final else None,
                                end_limit if final else None, **kw)
            if not day and stuck and max_legs > 0:
                # bom senso: preso há 1 dia, aceita sair fora da janela de partidas
                day = self.plan_day(d, loc, t0, base, final, must_end if final else None,
                                    end_limit if final else None, wide=True, **kw)
                if day:
                    self.diag("info", "fora_janela", f"{d:%d/%m}: saída de {loc} fora da janela de partidas para "
                                                     "não deixar a aeronave presa.", d.isoformat())
            if not day and self.crew_mode() and p.get("allow_deadhead") and not self.same_place(loc, base):
                tz_l = get_tz(self.tz(loc)) or timezone.utc
                dh = self.deadhead(loc, base, t0, datetime.combine(d + timedelta(days=1), time(1, 0), tz_l).astimezone(timezone.utc), [])
                if dh:
                    day = [dh]
                    self.diag("info", "extra", f"{d:%d/%m}: sem voo para operar em {loc}; volta para a base {base} "
                                               f"como extra no {dh['ident']}.", d.isoformat())
            if not day and p["allow_positioning"]:
                pos = self.positioning(d, loc, base, t0)
                if pos and self.chain_ok([pos]):
                    day = [pos]
            if not day:
                has_any = any(catalog.types_compatible(self.ac_type, o["type_icao"], p["type_match"])
                              for o in self._occ_by_adep.get(loc, []))
                why = ""
                if self.crew_on() and p.get("crew_monthly"):
                    mk = d.strftime("%Y-%m")
                    lim = crew.monthly_block_limit_h(catalog.type_family(self.ac_type))
                    if self.month_block.get(mk, 0) > lim - 1.5:
                        why = f" (limite mensal de {lim:g} h de voo praticamente atingido: {self.month_block[mk]:.1f} h)"
                    wk = d.isocalendar()[:2]
                    if not why and self.week_duty.get(wk, 0) > 44 - 6:
                        why = f" (limite de 44 h semanais de jornada quase atingido: {self.week_duty[wk]:.1f} h)"
                self.diag("warning", "sem_pernas",
                          f"{d:%d/%m}: nenhuma perna compatível partindo de {loc}"
                          + ("" if has_any else " (não há voos do tipo a partir desse aeroporto)")
                          + (why or " dentro das regras") + "; aeronave permanece no solo.", d.isoformat())
                stuck = max_legs > 0
            else:
                stuck = False
                rx = repeat_excess(self.duty_prefix + day)
                if rx:
                    pair = repeated_pairs(self.duty_prefix + day)
                    self.diag("info" if anomaly else "warning", "anormalidade_repeticao",
                              f"{d:%d/%m}: anormalidade – {', '.join(pair)} voado(s) mais de uma vez ida e volta "
                              + ("(sorteada pela chance de anormalidade)." if anomaly else
                                 "(não havia alternativa melhor dentro das regras)."), d.isoformat())
                self.prev_day_idents = tuple(o["ident"] for o in day)
                self.remember(d, day)
                for o in day:
                    self.taken.add(o["ref"])
                legs += day
                self.record_duty(day)
                loc = day[-1]["ades"]
                last_arr = day[-1]["arr"]
                had_legs = True
                if not self.realistic() and len([x for x in day if x["origin"] != "SUGESTAO"]) < min(p["legs_min"], max_legs):
                    self.diag("info", "poucas_pernas",
                              f"{d:%d/%m}: só {len(day)} perna(s) encontradas (mínimo desejado "
                              f"{min(p['legs_min'], max_legs)}).", d.isoformat())
            d += timedelta(days=1)
        if p["return_to_base"] in ("daily", "end") and loc != base and not must_end:
            self.diag("warning", "fora_base", f"A escala termina em {loc}, fora da base {base}.")
        return legs

    def positioning(self, d: date, loc: str, base: str, t0: datetime) -> dict | None:
        if loc == base:
            return None
        if self.route_eet is None:
            self.route_eet = all_route_eet(self.db)
        eet = self.route_eet.get((loc, base))
        if not eet:
            self.diag("warning", "posicionamento_impossivel",
                      f"{d:%d/%m}: sem voo de {loc} e sem dados reais de duração {loc}→{base} para sugerir "
                      "posicionamento.", d.isoformat())
            return None
        tz = get_tz(self.tz(loc)) or timezone.utc
        dep = max(t0, datetime.combine(d, _hhmm(self.p["day_start"], "05:00"), tz).astimezone(timezone.utc))
        dep = dep.replace(second=0, microsecond=0) + timedelta(minutes=(5 - dep.minute % 5) % 5)
        self.diag("warning", "posicionamento",
                  f"{d:%d/%m}: perna de posicionamento {loc}→{base} SUGERIDA pelo sistema (não existe no RPL; "
                  f"duração = mediana real do trecho, {eet} min).", d.isoformat())
        return {"ref": f"POS:{loc}:{base}:{d}", "flight_id": None, "airline": self.p["airline"], "ident": None,
                "type_icao": self.ac_type, "adep": loc, "ades": base, "dep": dep,
                "arr": dep + timedelta(minutes=eet + self.p["taxi"]), "eet_min": eet, "origin": "SUGESTAO",
                "ref_date": None, "date": d.isoformat()}


# ------------------------------------------------------------------ persistência
def _insert_legs(db, schedule_id: int, legs: list[dict], aircraft, start_seq=1, reserve_ids: dict | None = None,
                 ac_type: str | None = None):
    ts = now_iso()
    for i, o in enumerate(legs):
        rid = (reserve_ids or {}).get(o.get("reserve_uid"))
        note = o.get("note") or "Posicionamento sugerido – sem voo no RPL" if o["origin"] == "SUGESTAO" else \
            o.get("note") if o.get("note") else \
            f"Extra: deslocamento como passageiro no {o.get('ident')} ({o['type_icao']})" if o.get("deadhead") else \
            f"Troca de aeronave em {o['adep']}" if o.get("swap") else \
            f"Projeção sazonal: mesma semana do ano anterior (RPL de {o.get('ref_date')})" if o.get("seasonal") else \
            "Acionamento de reserva (simulado)" if o.get("activity") == "RESERVA" else \
            "Acionamento de sobreaviso (simulado)" if o.get("activity") == "SOBREAVISO" else \
            f"Voo internacional observado no ADS-B (horário {o.get('times_source') or 'estimado'})" \
            if o["origin"] == "OBSERVADO" else ""
        db.execute(
            "INSERT INTO schedule_legs(uid, schedule_id, seq, aircraft_id, registration, airline, flight_id,"
            " flight_number, type_icao, planned_type, adep, ades, dep_utc, arr_utc, eet_min, data_origin, ref_date,"
            " notes, activity, reserve_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, schedule_id, start_seq + i, aircraft["id"] if aircraft else None,
             aircraft["registration"] if aircraft else None, o["airline"], o["flight_id"], o["ident"],
             o["type_icao"] if o.get("deadhead") else aircraft["icao_type"] if aircraft else (ac_type or o["type_icao"]),
             o["type_icao"], o["adep"], o["ades"],
             fmt_utc(o["dep"]), fmt_utc(o["arr"]), o["eet_min"], o["origin"], o["ref_date"], note,
             "EXTRA" if o.get("deadhead") else (o.get("activity") or ""), rid, ts, ts))


def _night_msg(airline: str, fam: str | None, ap: str, d: date, st: int, obs: float, days: int) -> str:
    seen = (f"ADS-B: {obs:.1f} aeronave por noite em {days} dias" if obs else
            f"ADS-B: nenhuma aeronave vista dormindo lá em {days} dias") if days else "sem dados de ADS-B"
    return (f"{d:%d/%m}: pernoite em {ap}, onde a {airline} não costuma deixar aeronave"
            f"{' ' + fam if fam else ''} à noite (RPL: nenhuma aeronave compatível amanhece lá nessa noite; {seen}).")


def _mark_swaps(pl, legs) -> None:
    """Escala do tripulante: marca as trocas de aeronave (dentro da mesma jornada) e resume trocas e extras."""
    if not pl.crew_mode():
        return
    swaps, hub_swaps = 0, 0
    for a, b in zip(legs, legs[1:]):
        if b["adep"] != a["ades"] or (b["dep"] - a["arr"]) >= timedelta(hours=8):
            continue
        if pl.is_swap(a, b):
            b["swap"] = True
            swaps += 1
            d = ops_mod.night_date(a["arr"], pl.tz(a["ades"]))
            hub_swaps += pl.night_stock(d).get(a["ades"], 0) >= 2
    extras = [o for o in legs if o.get("deadhead")]
    if swaps or extras:
        pl.diag("info", "trocas", f"Trocas de aeronave: {swaps} ({hub_swaps} em aeroportos com várias aeronaves). "
                                  f"Deslocamentos como extra: {len(extras)}"
                + (f" ({', '.join(o['ident'] + ' ' + o['adep'] + '→' + o['ades'] for o in extras[:6])}"
                   + ("…" if len(extras) > 6 else "") + ")" if extras else "") + ".")


def _ops_diag(pl, legs, base) -> None:
    """Pernoites da escala conferidos com o RPL e o ADS-B, e quanto do rodízio foi visto na operação real."""
    flown = [o for o in legs if o.get("origin") != "SUGESTAO"]
    if len(flown) < 2:
        return
    q = pl.ops()
    fam = catalog.type_family(pl.ac_type) if pl.ac_type else None
    nights: Counter = Counter()
    short: Counter = Counter()
    bad = 0
    seen = known = 0
    for a, b in zip(flown, flown[1:]):
        if b["adep"] != a["ades"]:
            continue
        kind = predict.gap_kind(a["arr"], b["dep"], a["ades"], pl.tz, 0)
        if kind in ("pernoite", "parada_longa", "parada_noturna_curta") or (b["dep"] - a["arr"]) >= timedelta(hours=6):
            d = ops_mod.night_date(a["arr"], pl.tz(a["ades"]))
            sc, st, obs = pl.night_info(a["ades"], d, base)
            (short if kind == "parada_noturna_curta" else nights)[a["ades"]] += 1
            if sc < 0:
                bad += 1
                pl.diag("warning", "pernoite_improvavel", _night_msg(pl.p["airline"], fam, a["ades"], d, st, obs,
                                                                    q.days) + " Não havia alternativa compatível.",
                        d.isoformat())
        elif q.has_data and not a.get("deadhead") and not b.get("deadhead"):
            nc = q.next_count(a, b)
            if nc and nc[1] >= predict.OBS_MIN_N:
                known += 1
                seen += nc[0] > 0
    if nights or short:
        fmt = lambda c: ", ".join(f"{ap} ×{n}" if n > 1 else ap for ap, n in c.most_common())  # noqa: E731
        txt = (f"Pernoites: {fmt(nights)}." if nights else "") + (
            f" Paradas de madrugada (chegada depois da meia-noite, saída cedo): {fmt(short)}." if short else "")
        pl.diag("info", "pernoites", txt.strip() + (
            "" if bad else " Todos em aeroportos onde a companhia mantém aeronaves à noite (RPL e ADS-B)."))
    if known:
        pl.diag("info", "rodizio_observado",
                f"Rodízio: {seen} de {known} sequências com dados no ADS-B ({100 * seen // known}%) foram feitas pela "
                f"mesma aeronave na operação real (adsb.lol, {q.days} dias).")


class NightChecker:
    """Confere pernoites de uma escala salva (validação depois de editar)."""

    def __init__(self, db, sched: dict | None, apmap: dict):
        p = (sched or {}).get("params") or {}
        self.db, self.apmap = db, apmap
        self.airline = (sched or {}).get("airline")
        self.ac_type = p.get("icao_type_resolved") or p.get("icao_type")
        self.type_match = p.get("type_match") or "family"
        self.base = p.get("base_resolved") or p.get("base")
        self.projection = p.get("projection", True)
        self.intl = bool(p.get("international"))
        self.ok = bool(self.airline and self.ac_type)
        self._stock: dict = {}
        self._ops = None

    def tz(self, icao):
        return catalog.airport_tz(self.apmap, icao)

    def ops(self):
        if self._ops is None:
            self._ops = ops_mod.Ops(ops_mod.get_profile(self.db), self.airline, catalog.type_family(self.ac_type))
        return self._ops

    def stock(self, d: date) -> dict:
        if d not in self._stock:
            idx = get_index(self.db, self.airline, int(get_settings(self.db)["taxi_allowance_min"] or 0))
            occs = []
            for k in range(3):
                occs += idx.occurrences(d + timedelta(days=k), self.projection)
            if self.intl:
                occs += intl_mod.occurrences(self.db, self.airline, d, d + timedelta(days=2))
            self._stock[d] = ops_mod.night_stock(
                occs, self.tz, [d], lambda t: catalog.types_compatible(self.ac_type, t, self.type_match))[d]
        return self._stock[d]

    def message(self, ap: str, arr_utc: datetime) -> str | None:
        if not self.ok or ap == self.base:
            return None
        d = ops_mod.night_date(arr_utc, self.tz(ap))
        st = self.stock(d).get(ap, 0)
        q = self.ops()
        obs = q.nights_per_day(ap) if q.has_data else 0.0
        if st >= 1 or obs >= OBS_NIGHT_RATE:
            return None
        return _night_msg(self.airline, catalog.type_family(self.ac_type), ap, d, st, obs, q.days)


def _compact_diags(diags: list[dict], keep: int = 2) -> list[dict]:
    """Escalas longas: o mesmo aviso repetido em muitos dias vira as primeiras ocorrências + um resumo."""
    by: dict = {}
    for d in diags:
        by.setdefault((d["level"], d["code"]), []).append(d)
    out, done = [], set()
    for d in diags:
        k = (d["level"], d["code"])
        grp = by[k]
        if len(grp) <= keep + 1:
            out.append(d)
            continue
        if k in done:
            continue
        done.add(k)
        out += grp[:keep]
        dates = [x["date"] for x in grp[keep:] if x.get("date")]
        months = Counter(x[5:7] + "/" + x[:4] for x in dates)
        out.append({"level": d["level"], "code": d["code"], "date": None,
                    "message": f"… e mais {len(grp) - keep} ocorrência(s) deste aviso" +
                               (" (" + ", ".join(f"{m}: {n}" for m, n in sorted(months.items(), key=lambda kv: kv[0][3:] + kv[0][:2])) + ")"
                                if months else "") + "."})
    return out


def _log_prior(pr: dict, n: int) -> float:
    top = max(pr.values()) or 1
    v = pr.get(n, 0.0)
    return math.log(max(v, 0.005) / top)


def _legs_diag(pl) -> None:
    """Distribuição de etapas por chave obtida × pedida nos sliders (v2.2.4)."""
    if pl.p.get("legs2_pct") is None:
        return
    days = [sum(1 for o in legs_ if not o.get("deadhead")) for _d, legs_ in pl.flown]
    days = [n for n in days if n]
    if not days:
        return
    pct = lambda f: round(100 * sum(1 for n in days if f(n)) / len(days))   # noqa: E731
    msg = (f"Chaves com etapas escolhidas: 2 etapas em {pct(lambda n: n == 2)}% dos dias de voo (pedido "
           f"{pl.p['legs2_pct']}%), 3 etapas em {pct(lambda n: n == 3)}% (pedido {pl.p['legs3_pct']}%); 1 etapa "
           f"{pct(lambda n: n == 1)}%, 4 ou mais {pct(lambda n: n >= 4)}% ({len(days)} dias de voo).")
    if pl.p["legs_max"] < 3 and pl.p["legs3_pct"]:
        msg += f" O máximo de pernas por dia ({pl.p['legs_max']}) impede chaves de 3 etapas."
    pl.diag("info", "chaves", msg)


def _pref_diag(pl, legs) -> None:
    """Resumo dos aeroportos de preferência: quantas pernas foram para lá e se o RPL tem voos do tipo."""
    prefer = pl.p.get("preferred_airports") or []
    if not prefer:
        return
    ops_ = [o for o in legs if not o.get("deadhead")]
    parts = []
    for ap in prefer:
        n = sum(1 for o in ops_ if o["ades"] == ap)
        days = len({o.get("date") or o["dep"].date().isoformat() for o in ops_ if o["ades"] == ap or o["adep"] == ap})
        weekly = {f["ident"] for f in pl.idx.flights if f["ades"] == ap and
                  catalog.types_compatible(pl.ac_type, f["type_icao"], pl.p["type_match"])}
        parts.append(f"{ap} em {n} perna(s), {days} dia(s)")
        if not weekly:
            pl.diag("warning", "preferencia", f"Aeroporto de preferência {ap}: o RPL não tem voo do tipo {pl.ac_type} "
                                              f"para lá; a preferência não tem como ser atendida.")
        elif len(weekly) < 4:
            origins = sorted({f["adep"] for f in pl.idx.flights if f["ades"] == ap and
                              catalog.types_compatible(pl.ac_type, f["type_icao"], pl.p["type_match"])})
            pl.diag("info", "preferencia", f"Aeroporto de preferência {ap}: só {len(weekly)} voo(s) do tipo {pl.ac_type} "
                                           f"no RPL (saindo de {', '.join(origins)}); poucas oportunidades no mês.")
    pl.diag("info", "preferencia_resumo", "Escala direcionada (" + (pl.p.get("pref_strength") or "normal") + "): "
            + "; ".join(parts) + f" de {len(ops_)} perna(s).")


def _intl_diag(pl, legs) -> None:
    """Compara a quantidade de internacionais escolhida pelo usuário com o que a escala conseguiu."""
    tgt = pl.p.get("intl_target_pct")
    if not pl.p.get("international") or tgt in (None, ""):
        return
    flown = [o for o in legs if o.get("origin", o.get("data_origin")) != "SUGESTAO"]
    if not flown:
        return
    n_i = sum(1 for o in flown if o.get("origin", o.get("data_origin")) == "OBSERVADO")
    got = 100 * n_i / len(flown)
    msg = f"Voos internacionais: {n_i} de {len(flown)} pernas ({got:.0f}%); quantidade pedida: {tgt}%."
    if got + max(3, tgt * 0.2) >= tgt:
        pl.diag("info", "intl_alvo", msg)
        return
    avail = intl_mod.list_flights(pl.db, pl.p["airline"])
    fam = catalog.type_family(pl.ac_type)
    n_cat = sum(1 for f in avail if catalog.type_family(f.get("type_icao") or "") == fam)
    why = []
    if n_cat < 10:
        why.append(f"o catálogo tem só {n_cat} voo(s) internacionais de {pl.p['airline']} na família {fam} "
                   "(atualize em Dados ADS-B com mais dias)")
    else:
        why.append("pernas internacionais são longas e o limite de horas de voo do mês e da jornada corta dias")
        why.append("a aeronave precisa de voos domésticos para chegar aos aeroportos com saídas internacionais "
                   "e voltar à base")
    pl.diag("warning", "intl_alvo", msg + " Ficou abaixo porque " + "; ".join(why) + ".")


def generate(db, raw_params: dict, progress=None) -> int:
    p = resolve_params(db, raw_params)
    pl = Planner(db, p)
    pl.progress = progress
    pl.choose_aircraft()
    pl.resolve_turn()
    base = pl.choose_base()
    p["base_resolved"] = base
    pl.build_pool(pl.start, pl.end)
    pl.busy_sets()
    pl.taken = set()
    if p["reserve_blocks"]:
        pl.draw_reserves(base)
    tz = get_tz(pl.tz(base)) or timezone.utc
    t0 = datetime.combine(pl.start, time(0, 0), tz).astimezone(timezone.utc)
    legs = pl.plan(base, t0, pl.start, pl.end, base, had_legs=False)
    cf, ct = pl.idx.coverage()
    if any(o["origin"] == "PROJETADO" for o in legs):
        n_proj = sum(1 for o in legs if o["origin"] == "PROJETADO")
        n_seas = sum(1 for o in legs if o.get("seasonal"))
        if n_seas:
            pl.diag("info", "sazonal", f"{n_seas} perna(s) projetada(s) da mesma semana do ano anterior (histórico de "
                                       "RPL): alta temporada e feriados entram como foram no ano passado.")
        pl.diag("info", "projecao",
                f"{n_proj} perna(s) em dias ainda sem RPL publicado (o RPL ativo vale de {cf} a {ct}): repetem a "
                "semana mais nova do RPL e estão marcadas como projetadas. Use Atualizar RPLs para trazer o mais novo.")
    _intl_diag(pl, legs)
    _mark_swaps(pl, legs)
    _ops_diag(pl, legs, base)
    _pref_diag(pl, legs)
    _legs_diag(pl)
    if not legs:
        pl.diag("error", "vazia", "Nenhuma perna gerada com os critérios informados. Relaxe filtros "
                                  "(tempo de solo, duração, janela) ou mude a base.")
    if p["kind"] in LONG_KINDS:
        pl.diag("info", "experimental",
                f"Escala de {LONG_KINDS[p['kind']]} meses (experimental): dias sem RPL publicado repetem a semana mais nova "
                "do RPL; mudanças de malha, temporada e feriados não são previstas. Limites mensais e folgas valem mês a mês.")
        pl.diags = _compact_diags(pl.diags)
    ac = pl.aircraft
    name = (raw_params.get("name") or "").strip() or (
        f"{p['airline']} {ac['registration'] if ac else pl.ac_type} — "
        f"{pl.start:%d/%m}" + (f" a {pl.end:%d/%m/%Y}" if pl.end != pl.start else f"/{pl.start:%Y}"))
    ts = now_iso()
    p["icao_type_resolved"] = pl.ac_type
    sid = db.execute(
        "INSERT INTO schedules(name, airline, kind, start_date, end_date, aircraft_id, params, diagnostics,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (name, p["airline"], p["kind"], p["start_date"], p["end_date"], ac["id"] if ac else None,
         json.dumps(p, ensure_ascii=False, default=str), json.dumps(pl.diags, ensure_ascii=False), ts, ts)).lastrowid
    reserve_mod.save(db, sid, pl.reserves)
    _insert_legs(db, sid, legs, ac, reserve_ids={r["uid"]: r["id"] for r in pl.reserves}, ac_type=pl.ac_type)
    save_days(db, sid, pl.day_records)
    return sid


def save_days(db, sid: int, recs: list[dict]):
    ts = now_iso()
    for r in recs:
        db.execute("INSERT INTO schedule_days(schedule_id, date, kind, location, status, note, data, created_at)"
                   " VALUES (?,?,?,?,?,?,?,?)", (sid, r["date"].isoformat() if hasattr(r["date"], "isoformat") else r["date"],
                                                 r["kind"], r.get("location"), r.get("status") or "", r.get("note") or "",
                                                 json.dumps(r.get("data") or {}, ensure_ascii=False), ts))


def load_days(db, sid: int) -> list[dict]:
    out = rows(db.execute("SELECT * FROM schedule_days WHERE schedule_id=? ORDER BY date, id", (sid,)))
    for r in out:
        r["data"] = json.loads(r.get("data") or "{}")
    return out


def _schedule_aircraft(db, sched) -> dict | None:
    if not sched["aircraft_id"]:
        return None
    return row(db.execute("SELECT a.*, m.icao_type, m.family FROM aircraft a LEFT JOIN aircraft_models m"
                          " ON m.id=a.model_id WHERE a.id=?", (sched["aircraft_id"],)))


def regenerate(db, sid: int, from_leg_id: int | None = None, day: str | None = None, new_seed=True,
               redraw_reserve: bool | None = None, progress=None) -> dict:
    sched = row(db.execute("SELECT * FROM schedules WHERE id=?", (sid,)))
    if not sched:
        raise LookupError("Escala não encontrada.")
    raw = json.loads(sched["params"])
    if raw.get("imported"):
        raise ValueError("Escala importada de texto: os voos são os da escala real. Para mudar, edite, adicione ou "
                         "remova pernas na própria escala.")
    full = not from_leg_id and not day
    if redraw_reserve is None:
        redraw_reserve = full and new_seed      # "Regerar tudo" sorteia a reserva de novo
    if redraw_reserve and not full:
        raise ValueError("Novo sorteio da reserva só regerando a escala inteira.")
    if new_seed:
        raw["seed"] = random.randint(1, 10 ** 9)
    if redraw_reserve:
        raw["reserve_seed"] = random.randint(1, 10 ** 9)
    if "reserve_blocks" not in raw:  # escala criada antes da reserva automática: mantém sem reserva
        raw["reserve_blocks"] = 0
    if sched["aircraft_id"]:
        raw["aircraft_mode"] = "specific"
        raw["aircraft_id"] = sched["aircraft_id"]
    elif raw.get("aircraft_mode") != "type_only":
        raw["aircraft_mode"] = "type_only"
        raw["icao_type"] = raw.get("icao_type_resolved")
    p = resolve_params(db, raw)
    p["start_date"], p["end_date"] = sched["start_date"], sched["end_date"]
    pl = Planner(db, p, schedule_id=sid)
    pl.progress = progress
    pl.choose_aircraft()
    pl.resolve_turn()
    base = raw.get("base_resolved") or pl.choose_base()
    legs = rows(db.execute("SELECT * FROM schedule_legs WHERE schedule_id=? ORDER BY seq", (sid,)))
    apmap = pl.apmap
    pl.reserves = reserve_mod.load(db, sid)

    def local_date(leg):
        return to_local(parse_utc(leg["dep_utc"]), catalog.airport_tz(apmap, leg["adep"])).date().isoformat()

    if day:
        r = reserve_mod.covering(pl.reserves, date.fromisoformat(day))
        if r:
            raise ValueError(f"{day[8:10]}/{day[5:7]} faz parte da reserva de {r['start_date']:%d/%m} a "
                             f"{r['end_date']:%d/%m}. Use “Novo sorteio da reserva” para mudá-la.")
    if from_leg_id:
        pos0 = next((i for i, l in enumerate(legs) if l["id"] == from_leg_id), None)
        if pos0 is not None:
            r = reserve_mod.covering(pl.reserves, date.fromisoformat(local_date(legs[pos0])))
            if r:  # dentro de uma reserva: refaz desde o início do bloco (acionamento inteiro)
                first = next((l for l in legs if local_date(l) >= r["start_date"].isoformat()), legs[pos0])
                from_leg_id = first["id"]

    if day:
        keep_before = [l for l in legs if local_date(l) < day]
        removed = [l for l in legs if local_date(l) == day]
        keep_after = [l for l in legs if local_date(l) > day]
        d_from = d_to = date.fromisoformat(day)
    elif from_leg_id:
        pos = next((i for i, l in enumerate(legs) if l["id"] == from_leg_id), None)
        if pos is None:
            raise LookupError("Perna não encontrada nesta escala.")
        keep_before, removed, keep_after = legs[:pos], legs[pos:], []
        d_from = date.fromisoformat(local_date(legs[pos]))
        d_to = pl.end
    else:
        keep_before, removed, keep_after = [], legs, []
        d_from, d_to = pl.start, pl.end

    if from_leg_id:
        r = next((r for r in pl.reserves if r["start_date"] < d_from <= r["end_date"]), None)
        if r:
            d_from = r["start_date"]
    pl.build_pool(d_from, d_to)
    pl.busy_sets()
    if redraw_reserve:
        db.execute("DELETE FROM schedule_reserves WHERE schedule_id=?", (sid,))
        pl.reserves = []
        if p["reserve_blocks"]:
            pl.draw_reserves(base)
    pl.taken = {f"{l['flight_id']}:{l['dep_utc'][:10]}" if l["flight_id"] else f"I:{l['flight_number']}:{l['dep_utc'][:10]}"
                for l in keep_before + keep_after if l["flight_id"] or l["data_origin"] == "OBSERVADO"}
    if keep_before:
        last_local = to_local(parse_utc(keep_before[-1]["arr_utc"]), pl.tz(keep_before[-1]["ades"])).date()
        pl.seed_history(keep_before, continuing=bool(from_leg_id) and last_local >= d_from)
        loc, t_avail, had = keep_before[-1]["ades"], parse_utc(keep_before[-1]["arr_utc"]), True
        if day:
            # mesmo dia: continua com tempo de solo, não com descanso
            pass
    else:
        tz = get_tz(pl.tz(base)) or timezone.utc
        loc, t_avail, had = base, datetime.combine(d_from, time(0, 0), tz).astimezone(timezone.utc), False
    must_end = keep_after[0]["adep"] if keep_after else None
    end_limit = parse_utc(keep_after[0]["dep_utc"]) if keep_after else None
    new = pl.plan(loc, t_avail, d_from, d_to, base, had, must_end, end_limit)
    if not from_leg_id and not day:
        _intl_diag(pl, new)
        _mark_swaps(pl, new)
        _ops_diag(pl, new, base)
    if day and keep_after and not new and removed:
        pl.diag("warning", "dia_sem_alternativa",
                f"Não há combinação para {day} que mantenha a continuidade com o dia seguinte; dia ficou vazio.")
    for l in removed:
        db.execute("DELETE FROM schedule_legs WHERE id=?", (l["id"],))
    db.execute("DELETE FROM schedule_days WHERE schedule_id=? AND date BETWEEN ? AND ?",
               (sid, d_from.isoformat(), d_to.isoformat()))
    save_days(db, sid, pl.day_records)
    ac = pl.aircraft
    if redraw_reserve:
        reserve_mod.save(db, sid, pl.reserves)
    else:
        for r in pl.reserves:
            if d_from <= r["start_date"] <= d_to:
                reserve_mod.update_result(db, r)
    _insert_legs(db, sid, new, ac, start_seq=len(keep_before) + 1,
                 reserve_ids={r["uid"]: r["id"] for r in pl.reserves}, ac_type=pl.ac_type)
    for i, l in enumerate(keep_after):
        db.execute("UPDATE schedule_legs SET seq=? WHERE id=?", (len(keep_before) + len(new) + 1 + i, l["id"]))
    stored = json.loads(sched["params"] or "{}")
    stored.update({k: p[k] for k in ("min_turn", "max_turn", "min_turn_source", "reserve_seed", "reserve_blocks",
                                       "planned_days_off") if k in p})
    if new_seed:
        stored["seed"] = p["seed"]
    db.execute("UPDATE schedules SET params=? WHERE id=?", (json.dumps(stored, ensure_ascii=False, default=str), sid))
    diags = json.loads(sched["diagnostics"] or "[]")
    stamp = {"level": "info", "code": "regerado", "message": (
        f"Regerado {'o dia ' + day if day else 'a partir da perna selecionada' if from_leg_id else 'por completo'}"
        f" em {now_iso()[:16].replace('T', ' ')} UTC: {len(removed)} removida(s), {len(new)} nova(s).")}
    if not from_leg_id and not day:
        diags = [d for d in diags if d.get("code") in ("aeronave_auto", "base_auto")]
    if p.get("kind") in LONG_KINDS:
        pl.diags = _compact_diags(pl.diags)
    diags = diags + [stamp] + pl.diags
    db.execute("UPDATE schedules SET diagnostics=?, updated_at=? WHERE id=?",
               (json.dumps(diags[-80:], ensure_ascii=False), now_iso(), sid))
    return {"removed": len(removed), "added": len(new)}


# ------------------------------------------------------------------ validação / serialização
def serialize_schedule(db, sid: int) -> dict:
    sched = row(db.execute("SELECT * FROM schedules WHERE id=?", (sid,)))
    if not sched:
        raise LookupError("Escala não encontrada.")
    sched["params"] = json.loads(sched["params"] or "{}")
    sched["diagnostics"] = json.loads(sched["diagnostics"] or "[]")
    ac = _schedule_aircraft(db, sched)
    sched["aircraft"] = {k: ac[k] for k in ("id", "registration", "icao_type", "family", "airline", "active",
                                            "is_demo")} if ac else None
    legs = rows(db.execute("SELECT * FROM schedule_legs WHERE schedule_id=? ORDER BY seq, dep_utc", (sid,)))
    res_rows = reserve_mod.load(db, sid)
    sched["_reserves"] = res_rows
    sched["legs"] = validate_legs(db, legs, sched)
    b = db.execute("SELECT value FROM meta WHERE key=?", (f"bid:{sid}",)).fetchone()
    sched["bid"] = json.loads(b[0]) if b else None   # pedido de escala (carreira) que gerou esta escala
    from . import delays as delays_mod   # pontualidade real por perna (VRA importado ou ADS-B embutido)
    pq = delays_mod.Punctuality(db)
    for l in sched["legs"]:
        l["punctuality"] = delays_mod.describe(pq.stats(l["airline"], l.get("flight_number"), l["adep"], l["ades"])) \
            if l.get("flight_number") else None
    sched["reserves"] = [reserve_mod.serialize(r, catalog.airport_map(db), sched["legs"]) for r in res_rows]
    sched.pop("_reserves", None)
    sched["days"] = serialize_days(db, sid, sched)
    sched["summary"] = summarize(sched["legs"])
    sched["summary"]["reserve_days"] = sum(r["days"] for r in res_rows)
    sched["crew"] = crew_report(db, sched)
    return sched


DAY_STATUS = {"sem_acionamento": "NÃO ACIONADO", "acionado": "ACIONADO (simulado)", "inviavel": "INVIÁVEL", "": ""}


def serialize_days(db, sid: int, sched: dict) -> list[dict]:
    apmap = catalog.airport_map(db)
    codes = crew_profiles.codes(sched["airline"])
    out = []
    for r in load_days(db, sid):
        iata = (apmap.get(r["location"]) or {}).get("iata") or r["location"]
        label = {"sobreaviso": codes["sobreaviso_code"], "inativo": f"{codes['inactive_code']} {iata}",
                 "reserva": f"{codes['reserve_code']} {iata}"}.get(r["kind"], r["kind"].upper())
        out.append({**r, "location_iata": iata, "label": label, "status_label": DAY_STATUS.get(r["status"], r["status"]),
                    "legs": sum(1 for l in sched.get("legs") or [] if l["local_date"] == r["date"]
                                and l.get("activity") == "SOBREAVISO")})
    return out


def crew_report(db, sched: dict) -> dict:
    """Jornadas, repousos, folgas e limites da escala, pelas regras escolhidas na geração."""
    p = dict(sched.get("params") or {})
    if "crew_mode" not in p:  # escala criada antes das regras existirem
        p["crew_mode"] = "off"
    apmap = catalog.airport_map(db)
    fam = (sched.get("aircraft") or {}).get("family") or catalog.type_family(p.get("icao_type_resolved"))
    rdays = set()
    for r in sched.get("reserves") or []:
        x = date.fromisoformat(r["start_date"])
        while x <= date.fromisoformat(r["end_date"]):
            rdays.add(x.isoformat())
            x += timedelta(days=1)
    special = {d["date"] for d in sched.get("days") or []}   # sobreaviso, inativo e reserva importada: não são folga
    rdays |= special
    n_res = len(rdays - special)
    rep = crew.analyze(sched["legs"], p, lambda icao: catalog.airport_tz(apmap, icao), p.get("base_resolved"), fam,
                       (date.fromisoformat(sched["start_date"]), date.fromisoformat(sched["end_date"])), rdays)
    if "reserve_days" in rep["summary"]:
        rep["summary"]["reserve_days"] = n_res
    # nunca 7 dias de reserva seguidos (reserva sorteada ou importada da escala real)
    res_days = sorted((rdays - special) | {d["date"] for d in sched.get("days") or [] if d["kind"] == "reserva"})
    run = best = 0
    prev_d = None
    for ds in res_days:
        dd = date.fromisoformat(ds)
        run = run + 1 if prev_d and (dd - prev_d).days == 1 else 1
        best, prev_d = max(best, run), dd
    rep["summary"]["max_reserve_run"] = best
    if best > reserve_mod.HARD_MAX and p.get("crew_mode", "off") != "off":
        rep["violations"].append({"level": "error", "message": f"{best} dias de reserva seguidos; nunca mais que "
                                                                f"{reserve_mod.HARD_MAX} (7 dias seguidos de reserva não podem)."})
        rep["summary"]["errors"] += 1
    kinds = Counter(d["kind"] for d in sched.get("days") or [])
    rep["summary"]["sobreaviso_days"] = kinds.get("sobreaviso", 0)
    rep["summary"]["inactive_days"] = kinds.get("inativo", 0)
    if kinds.get("sobreaviso", 0) and p.get("crew_mode", "off") != "off":
        per_m = Counter(d["date"][:7] for d in sched["days"] if d["kind"] == "sobreaviso")
        for mk, n in per_m.items():
            if n > 8:
                rep["violations"].append({"level": "error", "message": f"{mk}: {n} sobreavisos; máximo 8 por mês "
                                                                       "(Lei 13.475 art. 43)."})
                rep["summary"]["errors"] += 1
    rep["realism"] = realism_report(sched, apmap, rep)
    wm = p.get("weekday_max") or {}
    by_day: dict[str, int] = {}
    for l in sched["legs"]:
        by_day[l["local_date"]] = by_day.get(l["local_date"], 0) + 1
    names = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
    for ds, n in sorted(by_day.items()):
        lim = wm.get(str(date.fromisoformat(ds).weekday()))
        if lim is not None and n > lim:
            rep["violations"].append({"level": "warning", "message": (
                f"{ds[8:10]}/{ds[5:7]} ({names[date.fromisoformat(ds).weekday()]}): {n} perna(s); "
                + ("dia configurado como folga." if lim == 0 else f"limite configurado {lim}."))})
            rep["summary"]["warnings"] += 1
    rep["summary"]["min_turn"] = p.get("min_turn")
    rep["summary"]["min_turn_source"] = p.get("min_turn_source") or ("valor fixo" if p.get("turn_mode") == "fixed" else None)
    return rep


def _nm(apmap, a, b) -> float | None:
    A, B = apmap.get(a) or {}, apmap.get(b) or {}
    if A.get("lat") is None or B.get("lat") is None:
        return None
    r = math.pi / 180
    h = math.sin((B["lat"] - A["lat"]) * r / 2) ** 2 + math.cos(A["lat"] * r) * math.cos(B["lat"] * r) * \
        math.sin((B["lon"] - A["lon"]) * r / 2) ** 2
    return 3440.065 * 2 * math.asin(math.sqrt(h))


def realism_report(sched: dict, apmap: dict, crew_rep: dict) -> dict | None:
    """v2.2: compara a escala do tripulante com as escalas reais de referência (crew_profiles.REFERENCE)."""
    p = sched.get("params") or {}
    if p.get("mode") != "tripulante" and not p.get("imported"):
        return None
    ops_ = [l for l in sched["legs"] if l.get("activity") != "EXTRA"]
    # pernas por JORNADA (dia de trabalho), não por data: voo depois da meia-noite (+1) é do mesmo dia
    by_day = Counter({f"{i}": d["legs"] for i, d in enumerate(crew_rep.get("duties") or []) if d["legs"]})
    if not by_day:
        by_day = Counter(l["local_date"] for l in ops_)
    if not by_day:
        return None
    n_days = len(by_day)
    dist = [x for x in (_nm(apmap, l["adep"], l["ades"]) for l in ops_) if x is not None]
    busy = set(by_day) | {d["date"] for d in sched.get("days") or []} | {l["local_date"] for l in sched["legs"]}
    for r in sched.get("reserves") or []:
        x = date.fromisoformat(r["start_date"])
        while x <= date.fromisoformat(r["end_date"]):
            busy.add(x.isoformat())
            x += timedelta(days=1)
    d, run, best = date.fromisoformat(sched["start_date"]), 0, 0
    while d <= date.fromisoformat(sched["end_date"]):
        run = 0 if d.isoformat() in busy else run + 1
        best = max(best, run)
        d += timedelta(days=1)
    ref = crew_profiles.REFERENCE
    prof = crew_profiles.profile(sched["airline"])
    target = sum(k * v for k, v in prof["legs_prior"].items()) / sum(prof["legs_prior"].values())
    months = max(1, round(((date.fromisoformat(sched["end_date"]) - date.fromisoformat(sched["start_date"])).days + 1) / 30))
    vals = [
        ("legs_per_day", "Pernas por dia de voo", sum(by_day.values()) / n_days, ref["legs_per_day"], "{:.1f}"),
        ("sector_nm", "Distância média por perna (nm)", sum(dist) / len(dist) if dist else None, ref["sector_nm"], "{:.0f}"),
        ("pct_4plus", "Dias com 4 pernas ou mais", sum(1 for v in by_day.values() if v >= 4) / n_days, ref["pct_4plus"], "{:.0%}"),
        ("pct_1leg", "Dias com 1 perna", sum(1 for v in by_day.values() if v == 1) / n_days, ref["pct_1leg"], "{:.0%}"),
        ("max_off_block", "Maior bloco de folga (dias)", best, ref["max_off_block"], "{:.0f}"),
        ("nights_out", "Pernoites fora da base por mês", crew_rep["summary"].get("nights_out", 0) / months,
         ref["nights_out"], "{:.0f}"),
    ]
    items = []
    tol = {"legs_per_day": 0.3, "pct_4plus": 0.10, "pct_1leg": 0.08}   # um mês é amostra pequena: margem
    for key, label, v, (lo, hi), fmt in vals:
        if v is None:
            continue
        ok = lo - tol.get(key, 0) <= v <= hi + tol.get(key, 0)
        # distância depende da base (REC e GRU ficam longe do centro da malha): fora da faixa só informa
        level = "ok" if ok else ("info" if key in ("sector_nm", "nights_out", "max_off_block") else "warn")
        items.append({"key": key, "label": label, "value": round(v, 3), "text": fmt.format(v),
                      "ref": f"{fmt.format(lo)} a {fmt.format(hi)}", "level": level})
    return {"items": items, "profile": prof["label"], "source": prof["source"], "target_legs": round(target, 2),
            "n_reference": ref["n"], "ok": sum(1 for i in items if i["level"] == "ok"), "total": len(items)}


def summarize(legs: list[dict]) -> dict:
    by = {k: 0 for k in ORIGIN_LABEL}
    for l in legs:
        by[l["data_origin"]] = by.get(l["data_origin"], 0) + 1
    return {
        "legs": len(legs), "by_origin": by,
        "block_min": sum(l["block_min"] for l in legs),
        "errors": sum(1 for l in legs if l["status"] == "error"),
        "warnings": sum(1 for l in legs if l["status"] == "warning"),
        "anomalies": sum(1 for l in legs if any(c["code"] == "anormalidade_repeticao" for c in l["checks"])),
        "reserve_legs": sum(1 for l in legs if l.get("activity") == "RESERVA"),
        "airports": sorted({l["adep"] for l in legs} | {l["ades"] for l in legs}),
        "days": len({l["local_date"] for l in legs}),
    }


def _settings_turn(db, sched):
    s = get_settings(db)
    p = sched.get("params") or {}
    return int(p.get("min_turn") or s["min_turn_min"]), int(p.get("max_turn") or s["max_turn_min"])


def validate_legs(db, legs: list[dict], sched: dict | None = None) -> list[dict]:
    apmap = catalog.airport_map(db)
    min_turn, max_turn = _settings_turn(db, sched or {})
    type_mode = ((sched or {}).get("params") or {}).get("type_match", "exact")
    idx_cache: dict[str, FlightIndex] = {}
    ac_cache: dict[int, dict] = {}
    # consultas em lote (antes eram 2 por perna)
    busy: dict[int, list] = {}
    dups: dict[tuple, str] = {}
    if legs:
        sid0 = legs[0]["schedule_id"]
        lo, hi = min(l["dep_utc"] for l in legs), max(l["arr_utc"] for l in legs)
        ac_ids = sorted({l["aircraft_id"] for l in legs if l.get("aircraft_id")})
        if ac_ids:
            for r in db.execute(
                    "SELECT x.aircraft_id, x.dep_utc, x.arr_utc, s.name FROM schedule_legs x JOIN schedules s"
                    " ON s.id=x.schedule_id WHERE x.schedule_id<>? AND x.dep_utc<? AND x.arr_utc>? AND x.aircraft_id IN (%s)"
                    % ",".join("?" * len(ac_ids)), [sid0, hi, lo, *ac_ids]):
                busy.setdefault(r["aircraft_id"], []).append((r["dep_utc"], r["arr_utc"], r["name"]))
        fids = sorted({l["flight_id"] for l in legs if l.get("flight_id")})
        for i in range(0, len(fids), 500):
            chunk = fids[i:i + 500]
            for r in db.execute(
                    "SELECT x.flight_id, substr(x.dep_utc,1,10) d, s.name FROM schedule_legs x JOIN schedules s"
                    " ON s.id=x.schedule_id WHERE x.schedule_id<>? AND x.flight_id IN (%s)" % ",".join("?" * len(chunk)),
                    [sid0, *chunk]):
                dups.setdefault((r["flight_id"], r["d"]), r["name"])
    out = []
    prev = None
    reserves = (sched or {}).get("_reserves")
    if reserves is None and legs:
        reserves = reserve_mod.load(db, legs[0]["schedule_id"])
    reserves = reserves or []
    pair_seen: dict = {}   # (dia local, par) -> quantas vezes já apareceu
    intl_cache: dict = {}
    nights = NightChecker(db, sched, apmap)
    for l in legs:
        checks = []
        dep, arr = parse_utc(l["dep_utc"]), parse_utc(l["arr_utc"])
        tz_d, tz_a = catalog.airport_tz(apmap, l["adep"]), catalog.airport_tz(apmap, l["ades"])
        ld, la = to_local(dep, tz_d), to_local(arr, tz_a)
        pk = (ld.date(), tuple(sorted((l["adep"], l["ades"]))))
        pair_seen[pk] = pair_seen.get(pk, 0) + 1
        if pair_seen[pk] > 2:
            checks.append(("warning", "anormalidade_repeticao",
                           f"Anormalidade: {pair_seen[pk]}ª perna entre {pk[1][0]} e {pk[1][1]} no mesmo dia "
                           "(vai-e-volta repetido)."))
        if not tz_d or not tz_a:
            checks.append(("warning", "fuso_desconhecido",
                           "Aeroporto sem fuso cadastrado: horário local exibido em UTC. Cadastre em Configurações."))
        if arr <= dep:
            checks.append(("error", "horario_invertido", "Chegada anterior ou igual à partida."))
        ground = None
        if prev:
            pa = parse_utc(prev["arr_utc"])
            ground = int((dep - pa).total_seconds() // 60)
            if l["adep"] != prev["ades"]:
                pdate = to_local(pa, catalog.airport_tz(apmap, prev["adep"])).date()
                crossing = next((r for r in reserves if (pdate < r["start_date"] <= ld.date()) or
                                 (pdate <= r["end_date"] < ld.date())), None)
                crew_sched = ((sched or {}).get("params") or {}).get("mode") == "tripulante" or \
                    ((sched or {}).get("params") or {}).get("imported")
                if crew_sched and crew_profiles.same_city(l["airline"], prev["ades"], l["adep"]) and ground >= 60:
                    checks.append(("info", "transferencia_terrestre",
                                   f"Transferência por terra {prev['ades']}→{l['adep']} (mesma cidade; transporte da "
                                   "companhia)."))
                elif crossing:
                    checks.append(("info", "deslocamento_reserva",
                                   f"Deslocamento {prev['ades']}→{l['adep']} ligado à reserva de "
                                   f"{crossing['start_date']:%d/%m} a {crossing['end_date']:%d/%m} "
                                   "(tripulante em extra, sem voo atribuído)."))
                else:
                    checks.append(("error", "quebra_continuidade",
                                   f"Quebra de continuidade: perna anterior chega em {prev['ades']}, esta parte de "
                                   f"{l['adep']}."))
            if l["adep"] == prev["ades"] and ground >= 6 * 60 and prev.get("data_origin") != "SUGESTAO":
                kind = predict.gap_kind(pa, dep, l["adep"], lambda i: catalog.airport_tz(apmap, i), 0)
                if kind in ("pernoite", "parada_longa"):
                    msg = nights.message(l["adep"], pa)
                    if msg:
                        checks.append(("warning", "pernoite_improvavel", msg))
            if ground < 0:
                checks.append(("error", "sobreposicao", "Partida antes da chegada da perna anterior."))
            elif ground < min_turn:
                checks.append(("warning", "solo_curto", f"Tempo de solo de {ground} min (mínimo {min_turn})."))
            if prev.get("aircraft_id") != l.get("aircraft_id"):
                checks.append(("warning", "troca_aeronave",
                               f"Matrícula diferente da perna anterior ({prev.get('registration') or '—'} → "
                               f"{l.get('registration') or '—'})."))
        # Existência no RPL
        idx = idx_cache.get(l["airline"])
        if idx is None:
            idx = idx_cache[l["airline"]] = get_index(db, l["airline"], int(get_settings(db)["taxi_allowance_min"]))
        if l["data_origin"] in ("REAL", "PROJETADO"):
            occ = idx.occurrence_by_ref(f"{l['flight_id']}:{l['dep_utc'][:10]}") if l["flight_id"] else None
            if not occ:
                checks.append(("warning", "voo_nao_encontrado",
                               "Voo não encontrado nos RPL ativos para esta data (importação desativada/removida?)."))
            elif occ["origin"] != l["data_origin"]:
                checks.append(("info", "origem_mudou",
                               f"Com os RPL atuais esta perna seria classificada como {occ['origin']}."))
            if l["data_origin"] == "PROJETADO":
                checks.append(("info", "nao_garantido",
                               f"Projetado do padrão semanal (referência {l['ref_date']}); não garantido pelo RPL."))
        elif l["data_origin"] == "SUGESTAO":
            checks.append(("info", "sugestao", "Perna sugerida pelo sistema; não existe no RPL."))
        elif l["data_origin"] == "OBSERVADO":
            f = intl_cache.get(l["flight_number"])
            if f is None and l["flight_number"] not in intl_cache:
                f = intl_cache[l["flight_number"]] = row(db.execute(
                    "SELECT * FROM intl_flights WHERE ident=? AND active=1", (l["flight_number"],)))
            if not f:
                checks.append(("warning", "intl_nao_encontrado",
                               "Voo internacional não está mais no catálogo observado (ADS-B)."))
            else:
                checks.append(("info", "intl_observado",
                               f"Voo internacional real observado no ADS-B ({f['freq_source']}); horário "
                               f"{f['times_source']}. Os RPL do CGNA só trazem voos domésticos."))
        elif l["data_origin"] == "MANUAL":
            match = None
            for o in idx.occurrences(dep.date(), True):
                if o["ident"] == l["flight_number"] and o["adep"] == l["adep"] and o["ades"] == l["ades"]:
                    match = o
                    break
            if match:
                diff = int(abs((match["dep"] - dep).total_seconds()) // 60)
                checks.append(("info" if diff == 0 else "warning", "manual_confere",
                               "Confere com o RPL." if diff == 0 else
                               f"Voo existe no RPL, mas com partida {match['dep']:%H:%M}Z ({diff} min de diferença)."))
            else:
                checks.append(("warning", "manual_nao_consta", "Voo/trecho não consta no RPL nesta data."))
        if l.get("activity") == "RESERVA":
            checks.append(("info", "acionamento_reserva",
                           "Acionamento de reserva SIMULADO pelo AeroScale (o voo existe no RPL; o acionamento não)."))
        else:
            r_in = next((r for r in reserves if r["start_date"] <= ld.date() <= r["end_date"]), None)
            if r_in:
                checks.append(("warning", "voo_na_reserva",
                               f"Voo dentro da reserva de {r_in['start_date']:%d/%m} a {r_in['end_date']:%d/%m} sem ser "
                               "acionamento (conflito)."))
        # Aeronave
        if l.get("aircraft_id"):
            ac = ac_cache.get(l["aircraft_id"])
            if ac is None:
                ac = ac_cache[l["aircraft_id"]] = row(db.execute(
                    "SELECT a.*, m.icao_type FROM aircraft a LEFT JOIN aircraft_models m ON m.id=a.model_id"
                    " WHERE a.id=?", (l["aircraft_id"],))) or {}
            if not ac:
                checks.append(("error", "aeronave_removida", "Aeronave não existe mais no cadastro."))
            else:
                if not ac["active"]:
                    checks.append(("warning", "aeronave_inativa", f"{ac['registration']} está desativada."))
                if l.get("planned_type") and ac.get("icao_type") and not catalog.types_compatible(
                        ac["icao_type"], l["planned_type"], "exact"):
                    lvl = "info" if catalog.types_compatible(ac["icao_type"], l["planned_type"], "family") \
                        else "error"   # mesma família é comum na operação real (visto no ADS-B)
                    checks.append((lvl, "tipo_diferente",
                                   f"RPL prevê {l['planned_type']}; {ac['registration']} é {ac['icao_type']}."))
                blocked = set(filter(None, (ac.get("blocked_airports") or "").split(",")))
                hit = blocked & {l["adep"], l["ades"]}
                if hit:
                    checks.append(("error", "aeroporto_restrito",
                                   f"{', '.join(sorted(hit))} está restrito para {ac['registration']}."))
                for u in json.loads(ac.get("unavailable") or "[]"):
                    if u["from"] <= ld.date().isoformat() <= u["to"]:
                        checks.append(("error", "indisponivel",
                                       f"{ac['registration']} indisponível ({u.get('reason') or 'sem motivo'})."))
                other = next((n for a, b, n in busy.get(l["aircraft_id"], []) if a < l["arr_utc"] and b > l["dep_utc"]),
                             None)
                if other:
                    checks.append(("error", "aeronave_ocupada",
                                   f"{ac['registration']} já voa neste horário na escala '{other}'."))
        else:
            checks.append(("info", "sem_matricula", "Sem matrícula associada."))
        if l["flight_id"] and l["data_origin"] in ("REAL", "PROJETADO"):
            dup = dups.get((l["flight_id"], l["dep_utc"][:10]))
            if dup:
                checks.append(("warning", "voo_em_outra_escala",
                               f"O mesmo voo também está na escala '{dup}' (um voo real tem só uma aeronave)."))
        levels = {c[0] for c in checks}
        ad, aa = apmap.get(l["adep"]) or {}, apmap.get(l["ades"]) or {}
        item = dict(l)
        item.update(
            dep_local=ld.strftime("%Y-%m-%dT%H:%M"), arr_local=la.strftime("%Y-%m-%dT%H:%M"),
            dep_off=offset_label(ld) if tz_d else "UTC", arr_off=offset_label(la) if tz_a else "UTC",
            local_date=ld.date().isoformat(), adep_city=ad.get("city"), ades_city=aa.get("city"),
            adep_name=ad.get("name"), ades_name=aa.get("name"), adep_iata=ad.get("iata"), ades_iata=aa.get("iata"),
            block_min=int((arr - dep).total_seconds() // 60), ground_before=ground,
            origin_label=ORIGIN_LABEL.get(l["data_origin"], l["data_origin"]),
            checks=[{"level": a, "code": b, "message": c} for a, b, c in checks],
            status="error" if "error" in levels else "warning" if "warning" in levels else "ok",
        )
        out.append(item)
        prev = l
    return out


# ------------------------------------------------------------------ edição
def _sched_for_leg(db, leg_id):
    l = row(db.execute("SELECT * FROM schedule_legs WHERE id=?", (leg_id,)))
    if not l:
        raise LookupError("Perna não encontrada.")
    s = row(db.execute("SELECT * FROM schedules WHERE id=?", (l["schedule_id"],)))
    s["params"] = json.loads(s["params"] or "{}")
    return l, s


def _touch(db, sid):
    db.execute("UPDATE schedules SET updated_at=? WHERE id=?", (now_iso(), sid))


def renumber(db, sid):
    for i, r in enumerate(rows(db.execute("SELECT id FROM schedule_legs WHERE schedule_id=? ORDER BY seq, dep_utc",
                                          (sid,))), start=1):
        db.execute("UPDATE schedule_legs SET seq=? WHERE id=?", (i, r["id"]))


def delete_leg(db, leg_id):
    l, s = _sched_for_leg(db, leg_id)
    db.execute("DELETE FROM schedule_legs WHERE id=?", (leg_id,))
    renumber(db, s["id"])
    _touch(db, s["id"])


def move_leg(db, leg_id, direction: int):
    l, s = _sched_for_leg(db, leg_id)
    legs = rows(db.execute("SELECT id, seq FROM schedule_legs WHERE schedule_id=? ORDER BY seq", (s["id"],)))
    i = next(i for i, x in enumerate(legs) if x["id"] == leg_id)
    j = i + (1 if direction > 0 else -1)
    if 0 <= j < len(legs):
        db.execute("UPDATE schedule_legs SET seq=? WHERE id=?", (legs[j]["seq"], legs[i]["id"]))
        db.execute("UPDATE schedule_legs SET seq=? WHERE id=?", (legs[i]["seq"], legs[j]["id"]))
    _touch(db, s["id"])


def sort_by_time(db, sid):
    for i, r in enumerate(rows(db.execute("SELECT id FROM schedule_legs WHERE schedule_id=? ORDER BY dep_utc", (sid,))),
                          start=1):
        db.execute("UPDATE schedule_legs SET seq=? WHERE id=?", (i, r["id"]))
    _touch(db, sid)


def update_leg(db, leg_id, data: dict):
    l, s = _sched_for_leg(db, leg_id)
    sets, args = [], []
    if "notes" in data:
        sets.append("notes=?")
        args.append(data["notes"] or "")
    if "aircraft_id" in data:
        aid = data["aircraft_id"] or None
        reg, icao = None, l["type_icao"]
        if aid:
            a = row(db.execute("SELECT a.registration, a.airline, m.icao_type FROM aircraft a LEFT JOIN aircraft_models"
                               " m ON m.id=a.model_id WHERE a.id=?", (aid,)))
            if not a:
                raise ValueError("Aeronave não encontrada.")
            if a["airline"] != l["airline"]:
                raise ValueError("A aeronave pertence a outra companhia.")
            reg, icao = a["registration"], a["icao_type"] or icao
        sets += ["aircraft_id=?", "registration=?", "type_icao=?"]
        args += [aid, reg, icao]
    timing = {}
    for k in ("dep_utc", "arr_utc"):
        if data.get(k) and data[k] != l[k]:
            try:
                timing[k] = fmt_utc(parse_utc(data[k] if data[k].endswith("Z") else data[k] + "Z"))
            except ValueError:
                raise ValueError("Horário inválido (use AAAA-MM-DDTHH:MM em UTC).")
    for k in ("flight_number", "adep", "ades"):
        if data.get(k) and data[k].upper() != (l[k] or ""):
            timing[k] = data[k].strip().upper()
    if timing:
        for k, v in timing.items():
            sets.append(f"{k}=?")
            args.append(v)
        sets += ["edited=1", "data_origin='MANUAL'"]
        if l["data_origin"] != "MANUAL":
            sets.append("notes=?")
            args.append(((data.get("notes") if "notes" in data else l["notes"]) or "")
                        + f" [editado a partir de {l['data_origin']}]")
    if sets:
        sets.append("updated_at=?")
        args.append(now_iso())
        db.execute(f"UPDATE schedule_legs SET {', '.join(sets)} WHERE id=?", (*args, leg_id))
        _touch(db, s["id"])


def alternatives(db, leg_id: int | None = None, sid: int | None = None, mode="replace", limit=40) -> dict:
    """Candidatos reais para substituir uma perna ou inserir após ela."""
    if leg_id:
        l, s = _sched_for_leg(db, leg_id)
    else:
        s = row(db.execute("SELECT * FROM schedules WHERE id=?", (sid,)))
        s["params"] = json.loads(s["params"] or "{}")
        l = None
    legs = rows(db.execute("SELECT * FROM schedule_legs WHERE schedule_id=? ORDER BY seq", (s["id"],)))
    p = s["params"]
    min_turn, max_turn = _settings_turn(db, s)
    i = next((k for k, x in enumerate(legs) if l and x["id"] == l["id"]), None)
    if mode == "replace" and l:
        prev = legs[i - 1] if i and i > 0 else None
        nxt = legs[i + 1] if i is not None and i + 1 < len(legs) else None
        from_ap = prev["ades"] if prev else l["adep"]
        t_from = parse_utc(prev["arr_utc"]) + timedelta(minutes=min_turn) if prev else \
            parse_utc(l["dep_utc"]) - timedelta(hours=12)
        t_to = parse_utc(nxt["dep_utc"]) if nxt else parse_utc(l["dep_utc"]) + timedelta(hours=12)
    elif mode == "after" and l:
        prev, nxt = l, (legs[i + 1] if i + 1 < len(legs) else None)
        from_ap = l["ades"]
        t_from = parse_utc(l["arr_utc"]) + timedelta(minutes=min_turn)
        t_to = parse_utc(nxt["dep_utc"]) if nxt else t_from + timedelta(hours=24)
    else:  # escala vazia: a partir da base no início
        prev = nxt = None
        from_ap = p.get("base_resolved") or p.get("base")
        t_from = datetime.combine(date.fromisoformat(s["start_date"]), time(0), timezone.utc)
        t_to = t_from + timedelta(hours=36)
    taxi = int(get_settings(db)["taxi_allowance_min"] or 0)
    idx = get_index(db, s["airline"], taxi)
    occs = idx.occurrences_range(t_from.date() - timedelta(days=1), t_to.date() + timedelta(days=1),
                                 bool(p.get("projection", True)))
    ac = _schedule_aircraft(db, s)
    ac_type = ac["icao_type"] if ac else p.get("icao_type_resolved")
    used = {(x["flight_id"], x["dep_utc"][:10]) for x in legs if x["flight_id"]}
    out = []
    for o in occs:
        if o["adep"] != from_ap or not (t_from <= o["dep"] <= t_to):
            continue
        if (o["flight_id"], o["dep"].date().isoformat()) in used:
            continue
        fits_next = bool(nxt) and o["ades"] == nxt["adep"] and \
            o["arr"] + timedelta(minutes=min_turn) <= parse_utc(nxt["dep_utc"])
        if nxt and o["arr"] + timedelta(minutes=min_turn) > parse_utc(nxt["dep_utc"]) and mode == "after":
            continue
        compat = catalog.types_compatible(ac_type, o["type_icao"], p.get("type_match", "exact"))
        out.append({
            "ref": o["ref"], "ident": o["ident"], "adep": o["adep"], "ades": o["ades"], "type_icao": o["type_icao"],
            "dep_utc": fmt_utc(o["dep"]), "arr_utc": fmt_utc(o["arr"]), "eet_min": o["eet_min"],
            "origin": o["origin"], "fits_next": fits_next, "type_ok": compat,
        })
    out.sort(key=lambda x: (not x["type_ok"], not x["fits_next"], x["dep_utc"]))
    apmap = catalog.airport_map(db)
    for x in out:
        x["dep_local"] = to_local(parse_utc(x["dep_utc"]), catalog.airport_tz(apmap, x["adep"])).strftime("%d/%m %H:%M")
        x["arr_local"] = to_local(parse_utc(x["arr_utc"]), catalog.airport_tz(apmap, x["ades"])).strftime("%d/%m %H:%M")
    return {"from_airport": from_ap, "window": [fmt_utc(t_from), fmt_utc(t_to)],
            "next_airport": nxt["adep"] if nxt else None, "candidates": out[:limit]}


def apply_occurrence(db, sid: int, ref: str, replace_leg_id: int | None = None, after_leg_id: int | None = None):
    s = row(db.execute("SELECT * FROM schedules WHERE id=?", (sid,)))
    if not s:
        raise LookupError("Escala não encontrada.")
    p = json.loads(s["params"] or "{}")
    idx = get_index(db, s["airline"], int(get_settings(db)["taxi_allowance_min"] or 0))
    o = idx.occurrence_by_ref(ref, bool(p.get("projection", True)))
    if not o:
        raise ValueError("Voo não encontrado nos RPL ativos.")
    ac = _schedule_aircraft(db, s)
    if replace_leg_id:
        l, _ = _sched_for_leg(db, replace_leg_id)
        seq = l["seq"]
        db.execute("DELETE FROM schedule_legs WHERE id=?", (replace_leg_id,))
    elif after_leg_id:
        l, _ = _sched_for_leg(db, after_leg_id)
        seq = l["seq"] + 1
        db.execute("UPDATE schedule_legs SET seq=seq+1 WHERE schedule_id=? AND seq>=?", (sid, seq))
    else:
        seq = (db.execute("SELECT MAX(seq) m FROM schedule_legs WHERE schedule_id=?", (sid,)).fetchone()["m"] or 0) + 1
    if not ac and p.get("icao_type_resolved"):
        pass
    _insert_legs(db, sid, [o], ac, start_seq=seq)
    renumber(db, sid)
    _touch(db, sid)


def add_manual_leg(db, sid: int, data: dict):
    s = row(db.execute("SELECT * FROM schedules WHERE id=?", (sid,)))
    if not s:
        raise LookupError("Escala não encontrada.")
    try:
        dep = parse_utc(data["dep_utc"] if data["dep_utc"].endswith("Z") else data["dep_utc"] + "Z")
        arr = parse_utc(data["arr_utc"] if data["arr_utc"].endswith("Z") else data["arr_utc"] + "Z")
    except (KeyError, ValueError):
        raise ValueError("Informe partida e chegada (UTC) válidas.")
    adep, ades = (data.get("adep") or "").upper(), (data.get("ades") or "").upper()
    if len(adep) != 4 or len(ades) != 4:
        raise ValueError("Origem e destino devem ser códigos ICAO de 4 letras.")
    if arr <= dep:
        raise ValueError("Chegada deve ser depois da partida.")
    ac = _schedule_aircraft(db, s)
    seq = (db.execute("SELECT MAX(seq) m FROM schedule_legs WHERE schedule_id=?", (sid,)).fetchone()["m"] or 0) + 1
    ts = now_iso()
    db.execute(
        "INSERT INTO schedule_legs(uid, schedule_id, seq, aircraft_id, registration, airline, flight_id, flight_number,"
        " type_icao, planned_type, adep, ades, dep_utc, arr_utc, eet_min, data_origin, notes, edited, created_at,"
        " updated_at) VALUES (?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,'MANUAL',?,1,?,?)",
        (uuid.uuid4().hex, sid, seq, ac["id"] if ac else None, ac["registration"] if ac else None, s["airline"],
         (data.get("flight_number") or "").upper() or None, ac["icao_type"] if ac else None, None, adep, ades,
         fmt_utc(dep), fmt_utc(arr), int((arr - dep).total_seconds() // 60), data.get("notes") or "", ts, ts))
    sort_by_time(db, sid)


# ------------------------------------------------------------------ exportação
def export_csv(db, sid: int) -> str:
    s = serialize_schedule(db, sid)
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["seq", "data_local", "companhia", "voo", "matricula", "tipo_aeronave", "tipo_rpl", "origem", "destino",
                "partida_utc", "chegada_utc", "partida_local", "fuso_partida", "chegada_local", "fuso_chegada",
                "bloco_min", "solo_antes_min", "dado", "atividade", "status", "alertas", "observacoes"])
    for l in s["legs"]:
        w.writerow([l["seq"], l["local_date"], l["airline"], l["flight_number"] or "POSICIONAMENTO",
                    l["registration"] or "", l["type_icao"] or "", l["planned_type"] or "", l["adep"], l["ades"],
                    l["dep_utc"], l["arr_utc"], l["dep_local"].replace("T", " "), l["dep_off"],
                    l["arr_local"].replace("T", " "), l["arr_off"], l["block_min"],
                    "" if l["ground_before"] is None else l["ground_before"], l["data_origin"],
                    "ACIONAMENTO DE RESERVA (simulado)" if l.get("activity") == "RESERVA" else "VOO", l["status"],
                    " | ".join(c["message"] for c in l["checks"] if c["level"] != "info"), l["notes"] or ""])
    for r in s.get("reserves") or []:
        w.writerow(["", r["start_date"], s["airline"], "RESERVA", "", "", "", r["location"], r["location"], "", "",
                    r["start_date"], "", r["end_date"], "", "", "", "SIMULADO", "RESERVA", r["status_label"], "",
                    f"{r['days']} dias; {r['location_label']}; {r['note'].strip()}"])
    return "\ufeff" + out.getvalue()


MESES = ["JANEIRO", "FEVEREIRO", "MARÇO", "ABRIL", "MAIO", "JUNHO", "JULHO", "AGOSTO", "SETEMBRO", "OUTUBRO",
         "NOVEMBRO", "DEZEMBRO"]


def _local_day(apmap, iso_utc: str, icao: str) -> str:
    return to_local(parse_utc(iso_utc), catalog.airport_tz(apmap, icao)).date().isoformat()


def _flight_digits(ident: str | None) -> str:
    """TAM3376 → 3376; G31855 → 1855; sem número (posicionamento) → POS."""
    if not ident:
        return "POS"
    return re.sub(r"^(?:[A-Z]{3}|[A-Z][A-Z0-9]|[0-9][A-Z])(?=\d)", "", ident)


def export_txt(db, sid: int, style: str | None = None, times: bool = False) -> str:
    """Escala em texto simples, um dia por linha:
    ESCALA DE OUTUBRO:
    (28) GRU-REC-PNZ-REC (3376,3371,3370)
    (31) FOLGA
    Aeroportos pelo código IATA (ICAO quando não houver); dia sem perna = FOLGA.

    style="companhia" (padrão da escala do tripulante, v2.2) escreve como a companhia: Azul FR, SOBREAVISO,
    INATIVO EM BEL, DHD 2801 e +1 (chegada depois da meia-noite); LATAM DO. times=True acrescenta apresentação
    e corte (primeira partida e última chegada, hora local)."""
    s = serialize_schedule(db, sid)
    apmap = catalog.airport_map(db)
    code = lambda icao: (apmap.get(icao) or {}).get("iata") or icao  # noqa: E731
    p = s.get("params") or {}
    if style not in ("companhia", "classico"):
        style = "companhia" if (p.get("mode") == "tripulante" or p.get("imported")) else "classico"
    cod = crew_profiles.codes(s["airline"]) if style == "companhia" else {
        "off_code": "FOLGA", "reserve_code": "RESERVA", "sobreaviso_code": "SOBREAVISO", "inactive_code": "INATIVO EM",
        "dhd_style": "prefix"}
    by_day: dict[str, list] = {}
    if style == "companhia":
        # como na escala real: o dia é o da JORNADA (voo depois da meia-noite fica no dia em que começou, com +1)
        cur_day, prev = None, None
        for l in sorted(s["legs"], key=lambda x: x["dep_utc"]):
            dep = parse_utc(l["dep_utc"])
            loc_dep = to_local(dep, catalog.airport_tz(apmap, l["adep"]))
            gap_h = (dep - parse_utc(prev["arr_utc"])).total_seconds() / 3600 if prev else 99
            if cur_day is None or gap_h >= 10 or (l["local_date"] != cur_day and loc_dep.hour >= 5):
                cur_day = l["local_date"]
            by_day.setdefault(cur_day, []).append(l)
            prev = l
    else:
        for l in s["legs"]:
            by_day.setdefault(l["local_date"], []).append(l)
    days_kind = {d["date"]: d for d in s.get("days") or []}
    d1 = min([date.fromisoformat(s["start_date"])] + [date.fromisoformat(d) for d in by_day])
    d2 = max([date.fromisoformat(s["end_date"])] + [date.fromisoformat(d) for d in by_day])
    reserves = s.get("reserves") or []

    def res_of(day: date):
        return next((r for r in reserves if r["start_date"] <= day.isoformat() <= r["end_date"]), None)

    def num(l):
        n = _flight_digits(l["flight_number"])
        if l.get("activity") == "EXTRA":
            return f"{n} DHD" if cod["dhd_style"] == "suffix" else f"DH{n}"
        return n

    def seg_txt(legs):
        route = [code(legs[0]["adep"])]
        for a, b in zip([None] + legs, legs):
            if a is not None and b["adep"] != a["ades"]:
                route.append("/ " + code(b["adep"]))  # quebra de continuidade no mesmo dia
            route.append(code(b["ades"]))
        return "-".join(route).replace("-/ ", " / ")

    def legs_txt(legs, ds=None):
        plus = ""
        if style == "companhia" and ds and any(l["arr_utc"] and _local_day(apmap, l["arr_utc"], l["ades"]) > ds
                                               for l in legs):
            plus = " +1"
        if style == "companhia" and all(l.get("activity") == "EXTRA" for l in legs) and cod["dhd_style"] == "suffix":
            txt = "DHD " + ",".join(_flight_digits(l["flight_number"]) for l in legs)
        elif style == "companhia" and any(l.get("activity") == "EXTRA" for l in legs) and \
                not all(l.get("activity") == "EXTRA" for l in legs):
            # extra e voo no mesmo dia: um trecho por atividade, separados por " / "
            parts, cur = [], [legs[0]]
            for l in legs[1:]:
                if (l.get("activity") == "EXTRA") != (cur[-1].get("activity") == "EXTRA"):
                    parts.append(cur)
                    cur = []
                cur.append(l)
            parts.append(cur)
            txt = " / ".join(f"{seg_txt(x)} ({','.join(num(l) for l in x)})" for x in parts)
            txt += plus
        else:
            txt = f"{seg_txt(legs)}{plus} ({','.join(num(l) for l in legs)})"
        if times:
            a = to_local(parse_utc(legs[0]["dep_utc"]), catalog.airport_tz(apmap, legs[0]["adep"]))
            b = to_local(parse_utc(legs[-1]["arr_utc"]), catalog.airport_tz(apmap, legs[-1]["ades"]))
            txt += f" · {a:%H:%M}–{b:%H:%M}"
        return txt

    lines: list[str] = []
    month = None
    d = d1
    while d <= d2:
        if (d.year, d.month) != month:
            month = (d.year, d.month)
            lines.append(f"ESCALA DE {MESES[d.month - 1]}{f' {d.year}' if d.year != d1.year else ''}:")
        ds = d.isoformat()
        legs = sorted(by_day.get(ds, []), key=lambda l: l["dep_utc"])
        r = res_of(d)
        sp = days_kind.get(ds)
        if r:
            act = [l for l in legs if l.get("activity") == "RESERVA"]
            lines.append(f"({d.day:02d}) {cod['reserve_code']} {r['location_iata']}" + (f" · ACIONADO: {legs_txt(act)}" if act else ""))
        elif sp:
            label = {"sobreaviso": cod["sobreaviso_code"], "inativo": f"{cod['inactive_code']} {sp['location_iata']}",
                     "reserva": f"{cod['reserve_code']} {sp['location_iata']}"}.get(sp["kind"], sp["label"])
            lines.append(f"({d.day:02d}) {label}" + (f" · ACIONADO: {legs_txt(legs, ds)}" if legs else ""))
        elif not legs:
            lines.append(f"({d.day:02d}) {cod['off_code']}")
        else:
            lines.append(f"({d.day:02d}) {legs_txt(legs, ds)}")
        d += timedelta(days=1)
    for r in reserves:
        a, b = date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"])
        loc = f"Base {r['location_iata']}" if r["location_kind"] == "base" else f"{r['location_iata']} (fora da base)"
        status = {"acionado": "SIMULADO", "sem_acionamento": "NÃO ACIONADO",
                  "inviavel": "INVIÁVEL (sem sequência de voos compatível no RPL)"}.get(r["status"], r["status_label"])
        lines += ["", f"RESERVA: {a:%d/%m} a {b:%d/%m} ({r['days']} dias)", f"Local: {loc}", f"Acionamento: {status}"]
        act_days = sorted({l["local_date"] for l in s["legs"] if l.get("reserve_id") == r["id"]})
        if act_days:
            lines += ["", "ACIONAMENTO RESERVAS"]
            for ds in act_days:
                dl = sorted([l for l in s["legs"] if l.get("reserve_id") == r["id"] and l["local_date"] == ds],
                            key=lambda l: l["dep_utc"])
                lines.append(f"({ds[8:10]}) {legs_txt(dl)}")
    return "\n".join(lines) + "\n"


def _ics_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def leg_text(l: dict, sched_name: str) -> tuple[str, str]:
    title = f"{l['flight_number'] or 'POSICIONAMENTO'} {l['adep']}→{l['ades']}" + \
        (f" ({l['registration']})" if l.get("registration") else "") + \
        (" · acionamento de reserva" if l.get("activity") == "RESERVA" else "")
    desc = "\n".join([
        f"Companhia: {l['airline']}",
        f"Voo: {l['flight_number'] or 'posicionamento (sem voo no RPL)'}",
        f"Matrícula: {l.get('registration') or 'não associada'}",
        f"Aeronave: {l.get('type_icao') or '—'} (RPL: {l.get('planned_type') or '—'})",
        f"Origem: {l['adep']} {l.get('adep_city') or ''}",
        f"Destino: {l['ades']} {l.get('ades_city') or ''}",
        f"Partida: {l['dep_local'].replace('T', ' ')} {l['dep_off']} ({l['dep_utc'][11:16]}Z)",
        f"Chegada: {l['arr_local'].replace('T', ' ')} {l['arr_off']} ({l['arr_utc'][11:16]}Z)",
        f"Dado: {l['origin_label']}",
        *(["Atividade: ACIONAMENTO DE RESERVA (simulado pelo AeroScale)"] if l.get("activity") == "RESERVA" else []),
        f"Escala: {sched_name}",
        *( [f"Observações: {l['notes']}"] if l.get("notes") else [] ),
        "Gerado pelo AeroScale (simulação). Chegada = EOBT + EET + táxi estimado.",
    ])
    return title, desc


def reserve_text(r: dict, sched: dict) -> tuple[str, str]:
    a, b = date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"])
    title = f"RESERVA – {r['location_label']} ({r['days']} dias) · acionamento: {r['status_label']}"
    act = [l for l in sched["legs"] if l.get("reserve_id") == r["id"]]
    desc = "\n".join([
        f"Reserva de {a:%d/%m/%Y} a {b:%d/%m/%Y} ({r['days']} dias).",
        f"Local: {r['location']}{' – ' + r['location_city'] if r.get('location_city') else ''} "
        f"({'base' if r['location_kind'] == 'base' else 'fora da base'}; sorteio {r['location_roll']:.0f} vs {r['base_pct']:.0f}% base).",
        f"Acionamento: {r['status_label']} (sorteio {r['activation_roll']:.0f} vs {r['activation_pct']:.0f}%).",
        *([f"Pernas do acionamento: " + ", ".join(f"{l['flight_number']} {l['adep']}-{l['ades']} {l['local_date'][8:10]}/"
                                                 f"{l['local_date'][5:7]}" for l in act)] if act else []),
        *([r["note"].strip()] if r.get("note") else []),
        f"Escala: {sched['name']}",
        "Resultado sorteado pelo AeroScale (simulação), não é operação confirmada.",
    ])
    return title, desc


def export_ics(db, sid: int) -> str:
    s = serialize_schedule(db, sid)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//AeroScale//Escalas de simulacao//PT-BR", "CALSCALE:GREGORIAN",
             f"X-WR-CALNAME:{_ics_escape(s['name'])}"]
    for l in s["legs"]:
        title, desc = leg_text(l, s["name"])
        lines += ["BEGIN:VEVENT", f"UID:{l['uid']}@aeroscale", f"DTSTAMP:{stamp}",
                  f"DTSTART:{l['dep_utc'].replace('-', '').replace(':', '')}",
                  f"DTEND:{l['arr_utc'].replace('-', '').replace(':', '')}",
                  f"SUMMARY:{_ics_escape(title)}", f"DESCRIPTION:{_ics_escape(desc)}",
                  f"LOCATION:{_ics_escape((l.get('adep_name') or l['adep']) + ' (' + l['adep'] + ')')}",
                  "END:VEVENT"]
    for r in s.get("reserves") or []:
        title, desc = reserve_text(r, s)
        end_excl = date.fromisoformat(r["end_date"]) + timedelta(days=1)
        lines += ["BEGIN:VEVENT", f"UID:reserva-{r['uid']}@aeroscale", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{r['start_date'].replace('-', '')}", f"DTEND;VALUE=DATE:{end_excl:%Y%m%d}",
                  f"SUMMARY:{_ics_escape(title)}", f"DESCRIPTION:{_ics_escape(desc)}",
                  f"LOCATION:{_ics_escape(r['location'])}", "TRANSP:TRANSPARENT", "END:VEVENT"]
    for x in s.get("days") or []:   # v2.2: sobreaviso / inativo / reserva importada (dia inteiro)
        end_excl = date.fromisoformat(x["date"]) + timedelta(days=1)
        title = x["label"] + (f" · {x['status_label']}" if x.get("status_label") else "")
        lines += ["BEGIN:VEVENT", f"UID:dia-{sid}-{x['id']}@aeroscale", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{x['date'].replace('-', '')}", f"DTEND;VALUE=DATE:{end_excl:%Y%m%d}",
                  f"SUMMARY:{_ics_escape(title)}", f"DESCRIPTION:{_ics_escape((x.get('note') or '') + chr(10) + 'Escala: ' + s['name'])}",
                  f"LOCATION:{_ics_escape(x.get('location') or '')}", "TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
