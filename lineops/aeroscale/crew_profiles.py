"""Perfis de escala do tripulante por companhia (v2.2).

Calibrados com escalas reais de piloto enviadas pelo usuário (4 da Azul, base REC; 4 da LATAM, base GRU;
setembro a novembro/2026; as mais novas estão em data/escalas_reais.json) e com a pesquisa sobre escalas nas três companhias (SNA, ANAC, imprensa). A GOL não tem escala de
referência: usa o padrão médio das outras duas.

O que cada perfil controla (só no modo tripulante, com "realismo das escalas reais" ligado):
  legs_prior            distribuição de pernas por dia de voo (o gerador mira nela em vez de encher o dia)
  off_code / ...        como a companhia escreve folga, reserva, sobreaviso, inativo e extra no texto da escala
  same_city             aeroportos ligados por transporte terrestre da companhia (além de GRU/CGH, GIG/SDU, CNF/PLU)
  sobreaviso_per_month  dias de sobreaviso (em casa, 3–12 h, Lei 13.475 art. 43: máx. 8/mês)
  inactive_*            dia INATIVO fora da base (descanso longo sem voo, não é folga)
  long_off_block        um bloco longo de folga no mês (dias)
"""
from __future__ import annotations

import math

from . import catalog

DEFAULT = {
    "label": "Genérico",
    "legs_prior": {1: 0.16, 2: 0.30, 3: 0.38, 4: 0.13, 5: 0.03},
    "off_code": "FOLGA", "reserve_code": "RESERVA", "sobreaviso_code": "SOBREAVISO", "inactive_code": "INATIVO EM",
    "dhd_style": "prefix",          # prefix: DH4557 | suffix: 4557 DHD
    "same_city": [],
    "ground_transfer_min": 120,
    "sobreaviso_per_month": 1,
    "sobreaviso_activation_pct": 35,
    "inactive_after_madr_pct": 70,  # depois de 2 madrugadas seguidas fora da base
    "inactive_mid_trip_pct": 10,    # por noite fora da base em hub (descanso longo no meio da viagem)
    "long_off_block": (5, 7),
    "report_min": 60,               # apresentação antes da 1ª partida (min)
    "source": "média das escalas de referência",
}

PROFILES = {
    "AZU": {
        **DEFAULT,
        "label": "Azul",
        # 4 escalas de comandante (67 dias de voo): 1 perna 16%, 2 31%, 3 40%, 4 11%, 5 2%
        "legs_prior": {1: 0.16, 2: 0.31, 3: 0.40, 4: 0.11, 5: 0.02},
        "off_code": "FR", "sobreaviso_code": "SOBREAVISO", "inactive_code": "INATIVO EM",
        "dhd_style": "suffix",
        # ônibus da Azul entre Viracopos e São Paulo: a escala termina em VCP e recomeça em CGH no dia seguinte
        "same_city": [{"SBKP", "SBSP", "SBGR"}],
        "ground_transfer_min": 120,
        "sobreaviso_per_month": 1,
        "inactive_mid_trip_pct": 15,   # 2 inativos em 1 das 3 escalas reais
        "report_min": 50,              # apresentação da Azul: 50 min
        "source": "4 escalas reais de comandante Azul (base REC)",
    },
    "TAM": {
        **DEFAULT,
        "label": "LATAM",
        # 4 escalas reais (57 dias de voo; nov/2026 e out–nov/2026, narrowbody e A320neo com internacionais):
        # 1 perna 33%, 2 30%, 3 31%, 4 5%. Dias de 1 perna são comuns (internacional longo, ou ida e pernoite)
        "legs_prior": {1: 0.33, 2: 0.30, 3: 0.31, 4: 0.05, 5: 0.01},
        "off_code": "DO", "dhd_style": "prefix",
        "sobreaviso_per_month": 0,      # a escala de referência tem reserva em bloco de 3 dias, sem sobreaviso
        "inactive_mid_trip_pct": 5,
        "long_off_block": (5, 6),
        "source": "4 escalas reais de pilotos LATAM (base GRU; narrowbody e A320neo)",
    },
    "GLO": {
        **DEFAULT,
        "label": "GOL",
        "off_code": "FOLGA",
        "source": "sem escala de referência: média de Azul e LATAM",
    },
}

# Valores medidos nas escalas reais de referência (faixa observada), para o painel de realismo
REFERENCE = {
    "legs_per_day": (1.8, 2.6),          # pernas por dia de voo
    "sector_nm": (700, 820),             # distância média por perna (base REC/GRU, que ficam longe do centro)
    "pct_4plus": (0.0, 0.15),            # dias com 4 pernas ou mais
    "pct_1leg": (0.10, 0.46),            # dias com 1 perna
    "max_off_block": (3, 12),            # maior bloco de folga do mês
    "nights_out": (7, 12),               # pernoites fora da base no mês
    "n": 8,
}


def profile(airline: str | None) -> dict:
    return PROFILES.get((airline or "").upper(), DEFAULT)


def groups(airline: str | None) -> list[set]:
    return list(catalog.SAME_CITY) + [set(g) for g in profile(airline)["same_city"]]


def same_city(airline: str | None, a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    return any(a in g and b in g for g in groups(airline))


def siblings(airline: str | None, ap: str) -> list[str]:
    out = set()
    for g in groups(airline):
        if ap in g:
            out |= g
    out.discard(ap)
    return sorted(out)


def legs_log_prior(airline: str | None, n: int) -> float:
    """ln(P(n)/P(moda)): 0 para o número de pernas mais comum, negativo para os raros."""
    pr = profile(airline)["legs_prior"]
    top = max(pr.values())
    v = pr.get(n, 0.005 if n > max(pr) else pr.get(min(pr), 0.1))
    return math.log(max(v, 0.005) / top)


def codes(airline: str | None) -> dict:
    p = profile(airline)
    return {k: p[k] for k in ("off_code", "reserve_code", "sobreaviso_code", "inactive_code", "dhd_style")}
