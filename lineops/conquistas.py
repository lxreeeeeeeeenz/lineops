"""Conquistas: marcos da sua carreira, tirados só do diário de bordo (voos aprovados) e da frota. Nada é estimado:
cada conquista mostra de onde vem e quanto falta.

Toque: a classificação é a do landing.evaluate (critérios do fabricante do tipo: manteiga < 100 fpm, suave < 240, e
"duro" no limite do fabricante). Regiões do Brasil: a UF do aeroporto no catálogo do AeroScale e a divisão do IBGE.
"""
from __future__ import annotations

from . import landing, rules
from .db import rows

# IBGE: as cinco grandes regiões
REGIAO = {**{u: "Norte" for u in ("AC", "AM", "AP", "PA", "RO", "RR", "TO")},
          **{u: "Nordeste" for u in ("AL", "BA", "CE", "MA", "PB", "PE", "PI", "RN", "SE")},
          **{u: "Centro-Oeste" for u in ("DF", "GO", "MS", "MT")},
          **{u: "Sudeste" for u in ("ES", "MG", "RJ", "SP")},
          **{u: "Sul" for u in ("PR", "RS", "SC")}}


def country_of(a: dict) -> str | None:
    """País do aeroporto. No catálogo do AeroScale, aeroporto brasileiro tem a UF ("SP") e o estrangeiro tem o país no
    mesmo campo ("AR" para Ezeiza)."""
    if a.get("country"):
        return a["country"]
    uf = a.get("uf")
    if not uf:
        return None
    return "BR" if uf in REGIAO else uf


def _b(key, title, desc, cur, goal, unit="", src="diário de bordo"):
    cur = cur or 0
    return {"key": key, "title": title, "desc": desc, "current": cur, "goal": goal, "done": cur >= goal,
            "pct": min(100, round(100 * cur / goal)) if goal else 100, "unit": unit, "source": src}


def compute(conn) -> dict:
    lg = rows(conn.execute("""SELECT date, adep, ades, type_icao, registration, block_min, night_min, landings, landing_vs_fpm, landing_g,
                              network FROM logbook_entry WHERE reversed_at IS NULL ORDER BY date, id"""))
    n = len(lg)
    hours = sum(e["block_min"] or 0 for e in lg) / 60
    night = sum(e["night_min"] or 0 for e in lg) / 60
    tiers = [landing.evaluate(e["landing_vs_fpm"], e["landing_g"], type_icao=e["type_icao"])["tier"] for e in lg]
    butter = sum(1 for t in tiers if t == "manteiga")
    best_run = run = 0
    for t in tiers:                                     # pousos seguidos sem duro/severo (só os com dado do toque)
        if t is None:
            continue
        run = run + 1 if t not in ("duro", "severo") else 0
        best_run = max(best_run, run)
    aps = {x for e in lg for x in (e["adep"], e["ades"]) if x}
    countries, regions = set(), set()
    for icao in aps:
        cc = country_of(rules.airport(conn, icao) or {})
        if cc:
            countries.add(cc)
        uf = (rules.airport(conn, icao) or {}).get("uf")
        if cc == "BR" and REGIAO.get(uf):
            regions.add(REGIAO[uf])
    abroad = len(countries - {"BR"})
    regs = {e["registration"] for e in lg if e["registration"]}
    online = sum(1 for e in lg if (e["network"] or "").upper() in ("IVAO", "VATSIM"))
    types = {rules.type_family(e["type_icao"]) or e["type_icao"] for e in lg if e["type_icao"]}
    items = [
        _b("primeiro", "Primeiro voo", "O primeiro voo aprovado no diário.", n, 1, "voo"),
        _b("voos10", "Linha regular", "10 voos aprovados.", n, 10, "voos"),
        _b("voos50", "Veterano da escala", "50 voos aprovados.", n, 50, "voos"),
        _b("voos100", "Centenário", "100 voos aprovados.", n, 100, "voos"),
        _b("h100", "100 horas", "100 h de bloco no diário.", round(hours, 1), 100, "h"),
        _b("h500", "500 horas", "500 h de bloco no diário.", round(hours, 1), 500, "h"),
        _b("noite", "Coruja", "10 h de voo noturno.", round(night, 1), 10, "h"),
        _b("manteiga", "Manteiga", "Um pouso abaixo de 100 fpm (critério do fabricante: 'manteiga').", butter, 1, "pouso",
           "diário de bordo + landing.evaluate"),
        _b("manteiga10", "Mão de seda", "10 pousos manteiga.", butter, 10, "pousos", "diário de bordo + landing.evaluate"),
        _b("serie", "Sequência limpa", "20 pousos seguidos sem pouso duro (com razão e G registrados).", best_run, 20, "pousos",
           "diário de bordo + landing.evaluate"),
        _b("aeroportos10", "Turista", "Pousar ou decolar em 10 aeroportos diferentes.", len(aps), 10, "aeroportos"),
        _b("aeroportos30", "Conhece o Brasil", "30 aeroportos diferentes.", len(aps), 30, "aeroportos"),
        _b("regioes", "Cinco regiões", "Passar pelas cinco regiões do Brasil (IBGE).", len(regions), 5, "regiões",
           "diário de bordo + UF do catálogo do AeroScale"),
        _b("exterior", "Carimbo no passaporte", "Um voo para fora do Brasil.", abroad, 1, "país", "diário de bordo + catálogo do AeroScale"),
        _b("matriculas", "Conhece a frota", "Voar 10 matrículas diferentes.", len(regs), 10, "matrículas"),
        _b("online", "No ar com a rede", "10 voos online (IVAO ou VATSIM).", online, 10, "voos"),
        _b("familias", "Multi-tipo", "Voar duas famílias de avião diferentes.", len(types), 2, "famílias"),
    ]
    return {"items": items, "done": sum(1 for x in items if x["done"]), "total": len(items),
            "regions": sorted(regions), "note": "Conquistas saem só do diário de bordo (voos aprovados) e do catálogo do AeroScale."}
