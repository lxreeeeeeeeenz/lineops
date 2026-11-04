"""Toque: classificação pelos critérios do 737NG, toque obrigatório e a bronca da Chefia de Manutenção."""
import pytest

from lineops import aeroscale_import as ai, equipe, fleet, landing, manual
from lineops.errors import Conflict
from tests.test_importer import L, make_db


def test_tiers_follow_737ng_criteria():
    ev = landing.evaluate
    assert ev(-80, 1.1)["tier"] == "manteiga"
    assert ev(-180, 1.2)["tier"] == "suave" and ev(-180, 1.2)["inspection"] is None
    assert ev(-300, 1.3)["tier"] == "normal"
    assert ev(-450, 1.5)["tier"] == "firme" and ev(-450, 1.5)["inspection"] is None
    assert ev(-250, 2.25)["tier"] == "duro" and ev(-250, 2.25)["inspection"] == "fase1"        # 2,2 G do AMM 05-51
    assert ev(-610, 1.6)["inspection"] == "fase1"                                                # 600 fpm de projeto
    assert ev(-950, 2.7)["inspection"] == "fase2"
    assert ev(-200, 1.2, reported=True)["inspection"] == "fase1"                                 # julgamento da tripulação
    ow = ev(-400, 1.4, landing_weight_kg=67000, mlw_kg=65317)
    assert ow["overweight"] and ow["tier"] == "duro" and ow["inspection"]
    assert ev(None, 1.2)["tier"] is None


def test_styles():
    e = landing.evaluate(-720, 2.3)
    folk = landing.texts(e, {"reg": "PR-GUZ", "ades": "SBFL", "fn": "GLO1748", "seed": 0}, "folclorico")
    just = landing.texts(e, {"reg": "PR-GUZ", "ades": "SBFL", "fn": "GLO1748", "seed": 0}, "just_culture")
    assert "queda controlada" in folk["mnt"] and "BLOQUEADA" in folk["mnt"] and folk["chefia"]
    assert "aprender, não punir" in just["mnt"] and "cultura justa" in just["chefia"]
    assert landing.texts(landing.evaluate(-300, 1.3), {"seed": 1})["mnt"] is None               # pouso normal: silêncio


@pytest.fixture
def env(conn, tmp_path):
    p = tmp_path / "as.db"
    make_db(p, legs=[L("a", "GLO1748", dep="2026-10-20T10:00:00Z", arr="2026-10-20T12:00:00Z"),
                     L("b", "GLO1749", dep="2026-10-21T10:00:00Z", arr="2026-10-21T12:00:00Z")])
    ai.import_backup(conn, p)
    ac = fleet.add_aircraft(conn, registration="PR-GUZ", airline="GLO", type_icao="B738")
    legs = {r["uid"]: r["id"] for r in conn.execute("SELECT id, uid FROM roster_leg")}
    equipe.run(conn, now="2026-10-19T12:00:00Z")
    return conn, ac["id"], legs


def test_touchdown_required(env):
    conn, aid, legs = env
    with pytest.raises(Conflict) as e:
        manual.log_flight(conn, leg_id=legs["a"], aircraft_id=aid, out_utc="2026-10-20T10:00", in_utc="2026-10-20T12:00")
    assert e.value.code == "toque_ausente"


def test_hard_landing_grounds_aircraft_and_gets_scolded(env):
    conn, aid, legs = env
    manual.log_flight(conn, leg_id=legs["a"], aircraft_id=aid, out_utc="2026-10-20T10:00", in_utc="2026-10-20T12:00",
                      landing_vs_fpm=-720, landing_g=2.31)
    equipe.run(conn, now="2026-10-20T12:30:00Z")
    msgs = equipe.messages(conn, limit=100)
    mnt = next(m for m in msgs if m["sector"] == "manutencao")
    assert mnt["sender"] == "Eng. Walter Bastos" and "POUSO DURO" in mnt["subject"] and mnt["ack_required"] and "720 fpm" in mnt["body"]
    assert any(m["sector"] == "chefia" and "compareça" in m["subject"] for m in msgs)
    assert fleet.availability(conn, aid)["status"] == "INDISPONIVEL"
    d = conn.execute("SELECT * FROM discrepancy WHERE aircraft_id=?", (aid,)).fetchone()
    assert d["mel_ref"] == "AMM 05-51" and d["restriction"] == "BLOQUEIA"
    equipe.run(conn, now="2026-10-20T12:31:00Z")                       # não duplica
    assert conn.execute("SELECT COUNT(*) FROM discrepancy").fetchone()[0] == 1
    fleet.close_discrepancy(conn, d["id"])                             # Fase 1 sem dano: volta a voar
    assert fleet.availability(conn, aid)["status"] == "DISPONIVEL"


def test_normal_landing_is_quiet_and_style_setting(env):
    conn, aid, legs = env
    conn.execute("INSERT INTO setting(key, value) VALUES ('landing_style', 'just_culture')")
    manual.log_flight(conn, leg_id=legs["a"], aircraft_id=aid, out_utc="2026-10-20T10:00", in_utc="2026-10-20T12:00",
                      landing_vs_fpm=-210, landing_g=1.18)
    manual.log_flight(conn, leg_id=legs["b"], aircraft_id=aid, out_utc="2026-10-21T10:00", in_utc="2026-10-21T12:00",
                      landing_vs_fpm=-480, landing_g=1.62)
    equipe.run(conn, now="2026-10-21T12:30:00Z")
    mnt = [m for m in equipe.messages(conn) if m["sector"] == "manutencao"]
    assert len(mnt) == 1 and "toque firme" in mnt[0]["subject"] and "Dentro dos limites" in mnt[0]["body"]
    assert fleet.availability(conn, aid)["status"] == "DISPONIVEL"
    from lineops import pireps
    t = pireps.logbook_totals(conn)
    assert t["avg_vs_fpm"] == 345 and t["best_vs_fpm"] == 210 and t["hard_landings"] == 0
