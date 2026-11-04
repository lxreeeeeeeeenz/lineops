"""Equipe da operação: escala publicada, alteração, lembretes, reserva, DOV com despacho conjunto, MCC e fases do voo."""
import json

import pytest

from lineops import aeroscale_import as ai, equipe, fleet, flights, ivao, ofp as ofp_mod
from lineops.errors import Conflict
from tests.test_importer import L, make_db
from tests.test_ofp import FIX as OFP_FIX


def _ofp_raw(adep="SBBR", ades="SBFL"):
    raw = json.loads((OFP_FIX / "ofp_simbrief.json").read_text(encoding="utf-8"))
    raw["origin"]["icao_code"], raw["destination"]["icao_code"] = adep, ades
    return raw


def subjects(conn):
    return [m["subject"] for m in equipe.messages(conn, limit=200)]


@pytest.fixture
def env(conn, tmp_path):
    p = tmp_path / "as.db"
    make_db(p, legs=[L("a", "GLO1748", dep="2026-11-20T14:00:00Z", arr="2026-11-20T16:00:00Z"),
                     L("b", "GLO1749", dep="2026-11-20T17:00:00Z", arr="2026-11-20T19:00:00Z")])
    ai.import_backup(conn, p, user_id=1)
    return conn, tmp_path


def test_publication_welcome_and_change(env):
    conn, tmp = env
    equipe.run(conn, now="2026-11-10T12:00:00Z")
    s = subjects(conn)
    assert "Bem-vindo à equipe" in s and any(x.startswith("Escala publicada") for x in s)
    pub = next(m for m in equipe.messages(conn) if m["subject"].startswith("Escala publicada"))
    assert pub["sector"] == "escala" and pub["sender"] == "Marina Duarte" and pub["ack_required"]
    assert "art. 26" in pub["body"] or "em vigor" in pub["body"]
    p2 = tmp / "b.db"
    make_db(p2, legs=[L("a", "GLO1748", dep="2026-11-20T15:00:00Z", arr="2026-11-20T17:00:00Z")])     # mudou e a b saiu
    ai.import_backup(conn, p2, user_id=1)
    equipe.run(conn, now="2026-11-10T12:05:00Z")
    ch = next(m for m in equipe.messages(conn) if m["subject"].startswith("Alteração de escala"))
    assert ch["sector"] == "tripulantes" and "saiu da sua escala" in ch["body"] and "partida 14:00Z → 15:00Z" in ch["body"]
    equipe.mark(conn, ch["id"], ack=True)
    assert equipe.counts(conn)["pending_ack"] == 2                      # faltam os cientes das duas publicações
    assert any(x.startswith("Escala atualizada") for x in subjects(conn))
    with pytest.raises(Conflict):
        equipe.mark(conn, next(m["id"] for m in equipe.messages(conn) if m["subject"] == "Bem-vindo à equipe"), ack=True)
    n = len(subjects(conn))
    equipe.run(conn, now="2026-11-10T12:06:00Z")
    assert len(subjects(conn)) == n                                      # idempotente


def test_shift_changes_sender(env):
    conn, _ = env
    assert equipe.who(conn, "despacho", "2026-11-10T12:00:00Z")["name"] == "Carla Menezes"     # 09:00 em Brasília
    assert equipe.who(conn, "despacho", "2026-11-10T20:00:00Z")["name"] == "Bruno Tavares"     # 17:00
    assert equipe.who(conn, "despacho", "2026-11-11T04:00:00Z")["name"] == "Fernanda Costa"    # 01:00
    equipe.save_staff(conn, [{"sector": "despacho", "shift": "manha", "name": "Ana Lúcia"}])
    assert equipe.who(conn, "despacho", "2026-11-10T12:00:00Z")["name"] == "Ana Lúcia"


def test_report_reminder_reserve_and_dov_flow(env):
    conn, _ = env
    equipe.run(conn, now="2026-11-10T12:00:00Z")
    ac = fleet.add_aircraft(conn, registration="PR-GUZ", airline="GLO", type_icao="B738", user_id=1)
    leg = conn.execute("SELECT id FROM roster_leg WHERE uid='a'").fetchone()[0]
    f = flights.book_leg(conn, leg, aircraft_id=ac["id"])
    equipe.run(conn, now="2026-11-19T20:00:00Z")                         # véspera: lembrete de apresentação
    s = subjects(conn)
    assert "GLO1748 SBBR→SBFL confirmado na sua escala" in s and any(x.startswith("PR-GUZ: situação") for x in s)
    rem = next(m for m in equipe.messages(conn) if m["subject"].startswith("Apresentação 20/11"))
    assert "GLO1748" in rem["body"] and "PR-GUZ" in rem["body"] and rem["sender"] == "Juliana Prado"
    # 2 h antes: o DOV pede o plano e, com o Pilot ID salvo, busca no SimBrief e anexa sozinho
    conn.execute("INSERT INTO setting(key, value) VALUES ('simbrief_userid', '123')")
    equipe.run(conn, now="2026-11-20T12:00:00Z", opener=lambda: _ofp_raw())
    s = subjects(conn)
    assert "GLO1748 SBBR→SBFL: preparando o seu despacho" in s
    rel = equipe.release_of(conn, f["id"])
    assert rel and rel["status"] == "LIBERADO" and rel["dov_name"] in ("Carla Menezes", "Bruno Tavares", "Fernanda Costa") and not rel["pic_signed_at"]
    brief = next(m for m in equipe.messages(conn) if "despacho liberado" in m["subject"])
    assert "NOTAM" in brief["body"] and "MEL: nenhum item" in brief["body"] and "Falta a sua assinatura" in brief["body"]
    o = ofp_mod.latest(conn, f["id"])
    assert conn.execute("SELECT user_id FROM audit_log WHERE action='ofp.attach' AND entity_id=?", (str(o["id"]),)).fetchone()[0] is None
    equipe.sign_release(conn, f["id"], user_id=1)
    assert equipe.release_of(conn, f["id"])["pic_signed_at"]
    assert "GLO1748 SBBR→SBFL: despacho assinado" in subjects(conn)


def test_dov_holds_release_for_wrong_route_and_flight_following(env):
    conn, _ = env
    equipe.run(conn, now="2026-11-10T12:00:00Z")
    ac = fleet.add_aircraft(conn, registration="PR-GUZ", airline="GLO", type_icao="B738", user_id=1)
    leg = conn.execute("SELECT id FROM roster_leg WHERE uid='a'").fetchone()[0]
    f = flights.book_leg(conn, leg, aircraft_id=ac["id"])
    ofp_mod.attach(conn, f["id"], _ofp_raw("SBGR", "SBRJ"), user_id=1)
    equipe.run(conn, now="2026-11-20T12:00:00Z")
    rel = equipe.release_of(conn, f["id"])
    assert rel["status"] == "RETIDO" and "SBGR→SBRJ" in rel["reasons"][0]
    with pytest.raises(Conflict):
        equipe.sign_release(conn, f["id"])
    # fases da IVAO viram mensagens do DOV
    conn.execute("INSERT INTO setting(key, value) VALUES ('ivao_vid', '9')")
    s = ivao.start(conn, f["id"], user_id=1)
    ivao._msg(conn, s["id"], "INFO", "Decolagem (OFF) 14:18Z.", "2026-11-20T14:18:00Z")
    ivao._msg(conn, s["id"], "INFO", "Pouso (ON) 15:52Z.", "2026-11-20T15:52:00Z")
    equipe.run(conn, now="2026-11-20T16:00:00Z")
    sub = subjects(conn)
    assert "GLO1748 SBBR→SBFL: acompanhando o voo" in sub and "GLO1748 SBBR→SBFL: decolagem" in sub and "GLO1748 SBBR→SBFL: pouso" in sub
    follow = next(m for m in equipe.messages(conn) if m["subject"].endswith("acompanhando o voo"))
    assert "sem o despacho assinado" in follow["body"]


def test_upgrade_does_not_flood(conn, tmp_path):
    p = tmp_path / "as.db"
    make_db(p, legs=[L("a", "GLO1748", dep="2026-11-20T14:00:00Z", arr="2026-11-20T16:00:00Z")])
    ai.import_backup(conn, p, user_id=1)
    ac = fleet.add_aircraft(conn, registration="PR-GUZ", airline="GLO", type_icao="B738", user_id=1)
    flights.book_leg(conn, conn.execute("SELECT id FROM roster_leg").fetchone()[0], aircraft_id=ac["id"])
    equipe.run(conn, now="2026-11-10T12:00:00Z")                         # banco antigo com voos: só o boas-vindas
    assert subjects(conn) == ["Bem-vindo à equipe"]
