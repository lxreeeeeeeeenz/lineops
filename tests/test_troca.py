"""Pedido de troca de escala: opções do RPL, regras do AeroScale, chance, resposta da equipe e a escala mudando de verdade."""
import random
import shutil
import time
from datetime import datetime, timedelta, timezone

import pytest

from lineops import careers, db, motor, troca
from lineops.aeroscale_import import import_backup

FMT = "%Y-%m-%dT%H:%M:%SZ"


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    data = tmp_path_factory.mktemp("troca") / "dados"
    motor.init(data)
    career = careers.career_path(data, "GLO")
    career.parent.mkdir(parents=True)
    c = db.connect(career)
    db.migrate(c)
    c.close()
    j = motor.generate(data, "GLO", {"kind": "mensal", "month": "2026-11", "base": "SBGR", "icao_type": "B738", "seed": 3}, career)
    while motor.job(j["id"])["state"] == "running":
        time.sleep(0.2)
    r = motor.job(j["id"])
    assert r["state"] == "finished", r.get("error")
    return data, career, r["result"]["schedule_id"]


@pytest.fixture
def env(engine, tmp_path):
    data, career, sid = engine
    p = tmp_path / "GLO.db"
    shutil.copy(career, p)
    c = db.connect(p)
    legs = [dict(r) for r in c.execute("SELECT * FROM roster_leg WHERE removed_batch IS NULL AND activity<>'SOBREAVISO' ORDER BY dep_utc")]
    now = "2026-10-25T12:00:00Z"                                                          # um domingo, uma semana antes do mês
    yield {"c": c, "data": data, "legs": legs, "now": now, "sid": sid}
    c.close()


class Fixed(random.Random):
    """Sorteio controlado: a 1ª chamada de randint devolve o valor pedido para o dado (1..100); o resto segue normal."""
    def __init__(self, roll):
        super().__init__(1)
        self.roll = roll

    def randint(self, a, b):
        if (a, b) == (1, 100):
            return self.roll
        return super().randint(a, b)


def _swap_leg(env):
    for l in env["legs"]:
        o = troca.options(env["c"], env["data"], l["id"], env["now"])
        ok = [x for x in o["troca"]["options"] if x["ok"]]
        if ok:
            return l, o, ok[0]
    pytest.skip("nenhuma perna com troca possível nesta escala")


def test_options_come_from_rpl_and_follow_aeroscale_rules(env):
    l, o, c = _swap_leg(env)
    for old, new in zip(c["replaces"], c["legs"]):                                       # mesmo trecho, voo real do RPL
        assert (new["adep"], new["ades"]) == (old["adep"], old["ades"]) and new["origin"] in ("REAL", "PROJETADO")
        assert new["flight_number"].startswith("GLO")
    assert 5 <= c["chance"] <= 95
    assert c["factors"][0]["base"] and any(f["key"] == "antecedencia" and f["delta"] == 10 for f in c["factors"])   # 10 dias antes
    if c["block"]:
        assert any(f["key"] == "etapas" for f in c["factors"])
    whys = [x["why"] for leg in env["legs"] for x in troca.options(env["c"], env["data"], leg["id"], env["now"])["troca"]["options"] if not x["ok"]]
    assert any("ilegal" in w or "conexão" in w or "partida" in w or "chegar" in w for w in whys)   # as regras cortam opções de verdade
    for y in o["liberacao"]["options"]:
        if y["mode"] == "ciclo":
            assert y["replaces"][0]["adep"] == y["replaces"][-1]["ades"]
    assert "regra de jogo" in o["disclaimer"]


def test_approved_swap_changes_the_roster(env):
    c = env["c"]
    l, o, cand = _swap_leg(env)
    from lineops import flights
    f = flights.book_leg(c, cand["replaces"][0]["id"])                                     # já estava reservado
    r = troca.request(c, env["data"], l["id"], "TROCA", ref=cand["ref"], reason="Aniversário do meu filho", now=env["now"], rnd=Fixed(1))
    assert r["state"] == "PENDENTE" and r["sector"] == "escala" and r["chance"] == cand["chance"]
    dec = datetime.strptime(r["decide_at"], FMT).replace(tzinfo=timezone.utc)
    from lineops import rules
    loc = rules.local_dt(c, dec, "SBGR")
    assert timedelta(minutes=3) <= dec - datetime.strptime(env["now"], FMT).replace(tzinfo=timezone.utc) <= timedelta(minutes=12)   # em minutos
    rx = c.execute("SELECT body FROM ops_message WHERE key=?", (f"troca_rx:{r['id']}",)).fetchone()
    assert rx and "Aniversário" in rx[0]
    with pytest.raises(Exception):
        troca.request(c, env["data"], l["id"], "LIBERACAO", now=env["now"])               # já tem pedido aberto
    assert troca.clock(c, env["now"], rnd=Fixed(1)) == 0                                  # ainda não é a hora
    assert troca.clock(c, r["decide_at"], rnd=Fixed(1)) == 1
    r = troca.get(c, r["id"])
    assert r["state"] == "APROVADO" and r["roll"] == 1
    gone = [c.execute("SELECT removed_batch FROM roster_leg WHERE id=?", (x["id"],)).fetchone()[0] for x in cand["replaces"]]
    assert all(gone)
    new = [dict(x) for x in c.execute("SELECT * FROM roster_leg WHERE uid LIKE ? ORDER BY dep_utc", (f"troca-{r['id']}-%",))]
    assert [(x["flight_number"], x["dep_utc"]) for x in new] == [(x["flight_number"], x["dep_utc"]) for x in cand["legs"]]
    assert all(x["data_origin"] in ("REAL", "PROJETADO") and x["registration"] is None for x in new)   # matrícula nunca inventada
    assert c.execute("SELECT state FROM flight WHERE id=?", (f["id"],)).fetchone()[0] == "CANCELADO"
    msg = c.execute("SELECT body, ack_required FROM ops_message WHERE key=?", (f"troca_res:{r['id']}",)).fetchone()
    assert "aprovada" in msg[0] and cand["legs"][0]["flight_number"] in msg[0] and "reserve o novo" in msg[0] and msg[1] == 1
    from lineops import journey
    d = journey.legs_of_day(c, new[0]["local_date"])
    assert any(x["uid"] == new[0]["uid"] for x in d)
    # o AeroScale manda a mesma escala de novo: a troca continua valendo
    dbp, _ = motor.paths(env["data"])
    st = import_backup(c, str(dbp), airline="GLO", schedule_ids=[env["sid"]])
    assert st["legs_restored"] == 0
    assert [c.execute("SELECT removed_batch FROM roster_leg WHERE id=?", (x["id"],)).fetchone()[0] for x in cand["replaces"]] == gone
    assert all(c.execute("SELECT removed_batch FROM roster_leg WHERE id=?", (x["id"],)).fetchone()[0] is None for x in new)


def test_denied_swap_keeps_the_roster_and_says_why(env):
    c = env["c"]
    l, o, cand = _swap_leg(env)
    r = troca.request(c, env["data"], l["id"], "TROCA", ref=cand["ref"], now=env["now"], rnd=Fixed(100))
    troca.clock(c, r["decide_at"], rnd=Fixed(100))
    r = troca.get(c, r["id"])
    assert r["state"] == "NEGADO" and r["answer"]
    assert all(c.execute("SELECT removed_batch FROM roster_leg WHERE id=?", (x["id"],)).fetchone()[0] is None for x in cand["replaces"])
    assert not c.execute("SELECT 1 FROM roster_leg WHERE uid LIKE 'troca-%'").fetchone()
    # o próximo pedido já pesa mais (pedidos nos últimos 30 dias)
    o2 = troca.options(c, env["data"], l["id"], env["now"])
    c2 = next(x for x in o2["troca"]["options"] if x["ref"] == cand["ref"])
    assert any(f["key"] == "pedidos" and f["delta"] == -7 for f in c2["factors"]) and c2["chance"] == max(5, cand["chance"] - 7)


def test_release_of_a_round_trip(env):
    c = env["c"]
    pick = None
    for x in env["legs"]:
        o = troca.options(c, env["data"], x["id"], env["now"])
        cyc = [y for y in o["liberacao"]["options"] if y["mode"] == "ciclo" and y["ok"]]
        if cyc:
            pick = (x, cyc[0])
            break
    if not pick:
        pytest.skip("nenhum ciclo liberável")
    l, opt = pick
    ids = [x["id"] for x in opt["replaces"]]
    legs = {x["id"]: x for x in env["legs"]}
    assert legs[ids[0]]["adep"] == legs[ids[-1]]["ades"] and opt["legs"] == []               # fecha o ciclo
    r = troca.request(c, env["data"], l["id"], "LIBERACAO", ref=opt["ref"], now=env["now"], rnd=Fixed(1))
    troca.clock(c, r["decide_at"], rnd=Fixed(1))
    assert troca.get(c, r["id"])["state"] == "APROVADO"
    assert all(c.execute("SELECT removed_batch FROM roster_leg WHERE id=?", (i,)).fetchone()[0] for i in ids)


def test_every_leg_can_be_asked_for_unless_it_breaks_fatigue_limits(env):
    """Sempre dá para pedir: sair e ir de extra até onde a escala continua, ou escolher destino; só o limite de jornada barra."""
    c = env["c"]
    ok_any = 0
    for l in env["legs"]:
        o = troca.options(c, env["data"], l["id"], env["now"])
        if o["blocked"]:
            continue
        ex = [y for y in o["liberacao"]["options"] if y["mode"] == "extra"]
        assert ex and ex[0]["legs"][0]["activity"] == "EXTRA" and ex[0]["legs"][0]["ades"] == l["ades"]
        for y in o["liberacao"]["options"] + o["troca"]["options"]:
            assert y["ok"] or y["why"]                                                         # bloqueado sempre diz por quê
        ok_any += any(y["ok"] for y in o["liberacao"]["options"])
    assert ok_any >= len(env["legs"]) // 2
    # pedir para ir de extra numa perna que NÃO fecha ciclo
    l = next(x for x in env["legs"] if not troca.release_block(troca._flights(c, troca._p(x["dep_utc"])), x["id"])
             and any(y["ok"] and y["mode"] == "extra" for y in troca.options(c, env["data"], x["id"], env["now"])["liberacao"]["options"]))
    o = troca.options(c, env["data"], l["id"], env["now"])
    ex = next(y for y in o["liberacao"]["options"] if y["ok"] and y["mode"] == "extra")
    r = troca.request(c, env["data"], l["id"], "LIBERACAO", ref=ex["ref"], now=env["now"], rnd=Fixed(1))
    assert "extra" in r["label"]
    troca.clock(c, r["decide_at"], rnd=Fixed(1))
    assert troca.get(c, r["id"])["state"] == "APROVADO"
    new = c.execute("SELECT * FROM roster_leg WHERE uid LIKE ?", (f"troca-{r['id']}-%",)).fetchone()
    assert new["activity"] == "EXTRA" and new["ades"] == l["ades"]
    msg = c.execute("SELECT body FROM ops_message WHERE key=?", (f"troca_res:{r['id']}",)).fetchone()[0]
    assert "de extra" in msg


def test_late_request_goes_to_cco_and_can_expire_or_be_cancelled(env):
    c = env["c"]
    l, o, cand = _swap_leg(env)
    dep = datetime.strptime(l["dep_utc"], FMT).replace(tzinfo=timezone.utc)
    late = (dep - timedelta(hours=12)).strftime(FMT)
    o = troca.options(c, env["data"], l["id"], late)
    c2 = next((x for x in o["troca"]["options"] if x["ok"]), None)
    if not c2:
        pytest.skip("sem opção 12 h antes")
    assert any(f["key"] == "antecedencia" and f["delta"] == -35 for f in c2["factors"]) and o["answer_by"].startswith("Controle")
    r = troca.request(c, env["data"], l["id"], "TROCA", ref=c2["ref"], now=late, rnd=Fixed(1))
    assert r["sector"] == "tripulantes"
    d = datetime.strptime(r["decide_at"], FMT).replace(tzinfo=timezone.utc)
    assert timedelta(minutes=1) <= d - datetime.strptime(late, FMT).replace(tzinfo=timezone.utc) <= timedelta(minutes=6)
    troca.clock(c, (dep + timedelta(hours=1)).strftime(FMT), rnd=Fixed(1))                 # o servidor ficou desligado
    assert troca.get(c, r["id"])["state"] == "EXPIRADO"
    r2 = troca.request(c, env["data"], l["id"], "TROCA", ref=c2["ref"], now=late)
    assert troca.cancel(c, r2["id"])["state"] == "CANCELADO"
    assert troca.options(c, env["data"], l["id"], (dep + timedelta(minutes=1)).strftime(FMT))["blocked"]   # apresentação passou


def test_leg_that_cannot_be_released_explains(env):
    c = env["c"]
    fl = env["legs"]
    # um bate-volta: tirar só a ida deixaria fora de posição; o bloco sempre inclui a volta
    l = fl[0]
    blk = troca.release_block(troca._flights(c, datetime.strptime(l["dep_utc"], FMT).replace(tzinfo=timezone.utc)), l["id"])
    assert blk is None or (blk[0]["adep"] == blk[-1]["ades"] and len(blk) >= 2)


def test_choose_the_destination(env):
    """Você escolhe para onde ir: no lugar do ciclo, uma ida e volta real do RPL para o destino, conferida pelas regras."""
    c = env["c"]
    pick = None
    for l in env["legs"]:
        d = troca.destinations(c, env["data"], l["id"], env["now"])
        alt = [x for x in d["items"] if not x["current"]]
        if alt:
            pick = (l, d, alt[0])
            break
    if not pick:
        pytest.skip("nenhum destino alternativo nesta escala")
    l, d, D = pick
    assert d["origin"] == l["adep"] and D["icao"] not in (l["adep"], l["ades"]) and D["options"] >= 1 and D["city"]
    o = troca.options(c, env["data"], l["id"], env["now"], D["icao"])
    ok = [x for x in o["troca"]["options"] if x["ok"]]
    assert ok and o["dest"] == D["icao"]
    ch = ok[0]
    assert ch["legs"][0]["adep"] == ch["replaces"][0]["adep"] and ch["legs"][0]["ades"] == D["icao"]
    assert ch["legs"][-1]["adep"] == D["icao"] and ch["legs"][-1]["ades"] == ch["replaces"][-1]["ades"]   # segue de onde a escala continua
    assert any(f["key"] == "destino" and f["delta"] == -10 for f in ch["factors"]) and ch["factors"][0]["label"] == "Troca por outro destino"
    r = troca.request(c, env["data"], l["id"], "TROCA", ref=ch["ref"], dest=D["icao"], now=env["now"], rnd=Fixed(1))
    assert D["icao"] in r["label"] and r["candidate"]["dest"] == D["icao"]
    troca.clock(c, r["decide_at"], rnd=Fixed(1))
    assert troca.get(c, r["id"])["state"] == "APROVADO"
    assert all(c.execute("SELECT removed_batch FROM roster_leg WHERE id=?", (x["id"],)).fetchone()[0] for x in ch["replaces"])
    new = [dict(x) for x in c.execute("SELECT * FROM roster_leg WHERE uid LIKE ? ORDER BY dep_utc", (f"troca-{r['id']}-%",))]
    assert [x["ades"] for x in new] == [D["icao"], ch["replaces"][-1]["ades"]] and all(x["removed_batch"] is None for x in new)
    with pytest.raises(Exception):
        troca.request(c, env["data"], env["legs"][0]["id"], "TROCA", ref="nada|1", dest="XXXX", now=env["now"])


def test_destination_on_a_leg_that_does_not_close_a_cycle(env):
    """Perna que segue para outro lugar: dá para escolher destino mesmo assim (vai ao destino e segue de lá)."""
    c = env["c"]
    for l in env["legs"]:
        fl = troca._flights(c, troca._p(l["dep_utc"]))
        if troca.release_block(fl, l["id"]):
            continue
        d = troca.destinations(c, env["data"], l["id"], env["now"])
        if d["blocked"]:
            continue
        assert d["items"] or d["why"]
        if d["items"]:
            o = troca.options(c, env["data"], l["id"], env["now"], d["items"][0]["icao"])
            y = next(x for x in o["troca"]["options"] if x["ok"])
            assert y["mode"] == "via" and y["legs"][-1]["ades"] == y["replaces"][-1]["ades"] and y["replaces"][0]["id"] == l["id"]
            return
    pytest.skip("todas as pernas fecham ciclo nesta escala")


def test_typed_destination_says_exactly_why(env):
    """Destino digitado (ICAO ou IATA): ou abre as opções, ou diz onde parou (sem voo no RPL, sem volta, limite de jornada)."""
    c = env["c"]
    l = next(x for x in env["legs"] if not troca.options(c, env["data"], x["id"], env["now"])["blocked"])
    assert troca.resolve_airport(c, "jpa") == "SBJP" and troca.resolve_airport(c, "XXXX") is None
    e = troca.explain_destination(c, env["data"], l["id"], "XXXX", env["now"])
    assert not e["ok"] and "Não achei o aeroporto" in e["why"]
    e = troca.explain_destination(c, env["data"], l["id"], "JPA", env["now"])        # a GOL não tem SBJP no RPL embutido
    assert not e["ok"] and e["icao"] == "SBJP" and "não tem nenhum voo" in e["why"] and "João Pessoa" in e["why"]
    d = troca.destinations(c, env["data"], l["id"], env["now"])
    if d["items"]:
        e = troca.explain_destination(c, env["data"], l["id"], d["items"][0]["icao"], env["now"])
        assert e["ok"]


def test_destination_can_replace_the_following_legs_too(env):
    """GIG→VIT, VIT→GRU vira GIG→destino→GRU: saem as pernas seguintes até onde dá para voltar direto."""
    c = env["c"]
    for l in env["legs"]:
        fl = troca._flights(c, troca._p(l["dep_utc"]))
        fr = [f for f in troca._frames(fl, l) if f["mode"] == "via"]
        assert fr and fr[0]["replaces"][0]["id"] == l["id"]
        for f in fr:
            seg = f["replaces"]
            assert all(a["ades"] == b["adep"] for a, b in zip(seg, seg[1:])) and len(seg) <= troca.MAX_VIA_LEGS
        if len(fr) > 1:
            return
    pytest.skip("nenhuma perna com sequência")
