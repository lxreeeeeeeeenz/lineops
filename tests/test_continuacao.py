"""Continuações (beta): para onde o avião segue depois de cada voo, as regras que você cadastra e o gerador obedecendo."""
from datetime import timedelta

import pytest

from lineops import continuacao, db, escala, journey, troca
from lineops.errors import FGVError
from tests.test_gerador import BASE, _run, eng  # noqa: F401


def test_analysis_crosses_rpl_and_adsb(eng):  # noqa: F811
    data, _career = eng
    a = continuacao.analyze(data, "GLO")
    assert a["flights"] and a["adsb_days"] > 0 and a["pair_sign"] in (1, -1)
    assert continuacao.analyze(data, "GLO") is a                                     # em cache
    with_c = [f for f in a["flights"].values() if f["candidates"]]
    assert with_c
    for f in with_c[:200]:
        for c in f["candidates"]:
            assert c["adep"] == f["ades"]                                             # sai de onde o voo chegou
            assert continuacao.GAP_MIN <= c["gap_min"] <= continuacao.GAP_MAX
            assert set(c["weekdays"]) <= set(f["weekdays"])
            assert c["adsb"] > 0 or c["pair"]                                         # só entra com alguma pista
            assert c["inverted"] == (c["pair"] and c["delta"] != a["pair_sign"])
            assert c["kind"] == ("volta" if c["ades"] == f["adep"] else "segue")
        for wd, y in f["best_by_weekday"].items():
            assert any(c["ident"] == y and wd in c["weekdays"] for c in f["candidates"])


def test_rules_and_lookup(eng):  # noqa: F811
    data, career = eng
    c = db.connect(career)
    r = continuacao.rule_add(c, "GLO", "1554", "PNZ", [2, 4], "o piloto disse")
    assert r["from_ident"] == "GLO1554" and r["to_ident"] == "@SBPL" and r["weekdays"] == [2, 4]
    r2 = continuacao.rule_add(c, "GLO", "GLO1554", "1555")
    assert r2["to_ident"] == "GLO1555" and r2["weekdays"] == []
    for bad in (("1554", "1554"), ("x", "1555"), ("1554", "XXX")):
        with pytest.raises(FGVError):
            continuacao.rule_add(c, "GLO", *bad)
    fm = continuacao.follow_map(c, data, "GLO", 100, auto=False)
    assert fm["GLO1554"]["2"]["to"] == "@SBPL" and fm["GLO1554"]["0"]["to"] == "GLO1555"   # a do dia vale; a geral cobre o resto
    assert fm["GLO1554"]["2"]["p"] == 1 and fm["GLO1554"]["2"]["src"] == "regra"
    assert continuacao.follow_map(c, data, "GLO", 0) == {}                            # força 0: desligado
    low, mid, full = (continuacao.follow_map(c, data, "GLO", s) for s in (10, 70, 100))
    assert len(full) >= len(mid) >= len(low) >= len(fm)
    autos = [e for v in mid.values() for e in v.values() if e["src"] == "auto"]
    assert autos and all(e["conf"] in ("alta", "média") and 0 < e["p"] <= 0.7 for e in autos)
    assert all(e["conf"] == "alta" for v in low.values() for e in v.values() if e["src"] == "auto")
    assert mid["GLO1554"]["2"]["src"] == "regra"                                       # a sua regra passa por cima
    lk = continuacao.lookup(c, data, "GLO", "1554")
    assert lk["items"] and all(x["ident"] == "GLO1554" for x in lk["items"]) and lk["items"][0]["rules"]
    for x in continuacao.rules_list(c):
        continuacao.rule_delete(c, x["id"])
    with pytest.raises(FGVError):
        continuacao.rule_delete(c, 999)
    with pytest.raises(FGVError):
        continuacao.lookup(c, data, "GLO", "")
    c.close()


def test_generator_follows_your_rule(eng):  # noqa: F811
    """Com a regra "depois de X, Y": sempre que X entra e Y existe no RPL naquela noite, o próximo voo é Y."""
    data, career = eng
    _run(data, career, {**BASE, "seed": 3})
    c = db.connect(career)
    legs = [l for l in escala.legs(c, "2026-11-01", "2026-11-30", flights_only=True) if l["flight_number"]]
    a = continuacao.analyze(data, "GLO")
    pick = None
    for x, z in zip(legs, legs[1:]):                                                 # um X da escala que tem outra continuação possível
        f = a["flights"].get(f"{x['flight_number']}:{x['adep']}-{x['ades']}")
        if f:
            alt = [cnd for cnd in f["candidates"] if cnd["ident"] != z["flight_number"]]
            if alt:
                pick = (x["flight_number"], alt[0]["ident"])
                break
    if not pick:
        pytest.skip("nenhum voo com continuação alternativa nesta escala")
    X, Y = pick
    continuacao.rule_add(c, "GLO", X, Y)
    c.close()
    _run(data, career, {**BASE, "seed": 3, "follow_strength": 100, "follow_mode": "regras"})
    c = db.connect(career)
    idx, _ = troca._get_index(data, "GLO")
    legs = [l for l in escala.legs(c, "2026-11-01", "2026-11-30", flights_only=True)]
    checked = 0
    for i, x in enumerate(legs):
        if x["flight_number"] != X:
            continue
        arr = journey.parse(x["arr_utc"])
        lo, hi = arr + timedelta(minutes=continuacao.GAP_MIN), arr + timedelta(minutes=continuacao.GAP_MAX)
        ys = [o for d in (arr.date(), (arr + timedelta(days=1)).date()) for o in idx.occurrences(d, projection=True)
              if o["ident"] == Y and o["adep"] == x["ades"] and lo <= o["dep"] <= hi]
        if ys:
            nxt = legs[i + 1] if i + 1 < len(legs) else None
            assert nxt and nxt["flight_number"] == Y, (X, Y, x["dep_utc"], nxt and nxt["flight_number"])
            checked += 1
    print("checagens da regra:", checked)
    for x in continuacao.rules_list(c):
        continuacao.rule_delete(c, x["id"])
    c.close()


def test_planner_hooks_force_the_continuation():
    """Os ganchos no Planner do AeroScale: depois de X só Y (se Y existe na janela), e o dia não termina em X."""
    from datetime import datetime, timezone

    from lineops import planner_ext

    class Stub:
        def __init__(self):
            self.p = {"airline": "TAM", "follow": {"TAM3376": {"2": "@SBPL"}}}
            self.taken = set()
            t = lambda h, m=0: datetime(2026, 11, 4, h, m, tzinfo=timezone.utc) if h < 24 else datetime(2026, 11, 5, h - 24, m, tzinfo=timezone.utc)  # noqa: E731
            self.x = {"ref": "x", "ident": "TAM3376", "adep": "SBGR", "ades": "SBRF", "dep": t(20, 30), "arr": t(23, 15)}
            self.y = {"ref": "y", "ident": "TAM3262", "adep": "SBRF", "ades": "SBPL", "dep": t(24, 15), "arr": t(25, 30)}
            self.z = {"ref": "z", "ident": "TAM3375", "adep": "SBRF", "ades": "SBGR", "dep": t(24, 40), "arr": t(27, 30)}
            self._occ_by_adep = {"SBRF": [self.y, self.z]}

        def chain_ok(self, chain):
            return True

        def _pick(self, finals):
            return finals[0][0]

        def ops(self):
            class O:
                def flight(self, ident, adep, ades):
                    return None
            return O()

    P = planner_ext._make_planner_class(Stub)
    p = P()
    assert p._must_next(p.x) == {"y"}                                               # quarta (UTC): segue para Petrolina
    assert p.chain_ok([p.x, p.y]) and not p.chain_ok([p.x, p.z])
    assert p._pick([([p.x], 10), ([p.x, p.y], 5)]) == [p.x, p.y]                     # não termina o dia no 3376
    assert "TAM3262:SBRF-SBPL" in p.ops().flight("TAM3376", "SBGR", "SBRF")["next"]  # entra primeiro na busca
    p.p["follow"] = {"TAM3376": {"2": {"to": "@SBPL", "p": 0.0, "src": "auto", "conf": "alta"}}}
    assert p._must_next(p.x) == set()                                               # força sorteou "livre": sem obrigação
    p.p["follow"] = {"TAM3376": {"2": {"to": "@SBPL", "p": 1.0, "src": "auto", "conf": "alta"}}}
    assert p._must_next(p.x) == {"y"}
    p.x = {**p.x, "dep": p.x["dep"] + timedelta(days=1), "arr": p.x["arr"] + timedelta(days=1)}
    assert p._must_next(p.x) == set()                                               # quinta: sem regra (e sem voo na janela)
    p.taken.add("y")
    p.x = {**p.x, "dep": p.x["dep"] - timedelta(days=1), "arr": p.x["arr"] - timedelta(days=1)}
    assert p._must_next(p.x) == set()                                               # Y já usado: a regra não trava o dia


def test_strength_rolls_are_stable():
    """Continuação identificada com p < 1: obrigatória em parte das vezes, sempre a mesma para a mesma semente."""
    from datetime import date as D

    from lineops import planner_ext
    rolls = [continuacao.roll(7, "TAM3376", D(2026, 11, 1) + timedelta(days=k)) for k in range(400)]
    assert rolls == [continuacao.roll(7, "TAM3376", D(2026, 11, 1) + timedelta(days=k)) for k in range(400)]
    assert 0.4 < sum(r < 0.5 for r in rolls) / 400 < 0.6
    k = planner_ext.follow_stash({"A": {"0": "B"}})
    assert planner_ext.follow_get(k) == {"A": {"0": "B"}} and planner_ext.follow_get({"x": 1}) == {"x": 1}
    planner_ext.follow_drop(k)
    assert planner_ext.follow_get(k) == {}
    legs = [{"flight_number": "TAM3376", "adep": "SBGR", "ades": "SBRF", "dep_utc": "2026-11-04T20:30:00Z"},
            {"flight_number": "TAM3262", "adep": "SBRF", "ades": "SBPL", "dep_utc": "2026-11-05T00:15:00Z"}]
    assert continuacao.summarize(legs, {"TAM3376": {"2": {"to": "@SBPL", "p": 0.7, "src": "auto"}}}) == {"regra": 0, "auto": 1, "known": 1}
    assert continuacao.summarize(legs, {"TAM3376": {"3": "TAM3262"}}) == {"regra": 0, "auto": 0, "known": 0}
