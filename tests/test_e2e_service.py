from datetime import datetime, timezone

import pytest

from lineops import acars_service as ac, fleet, flights, pireps
from lineops.errors import Conflict, FGVError
from lineops_acars.phases import PhaseMachine, iso
from tests import synth

T0 = datetime(2026, 11, 2, 1, 0, 0, tzinfo=timezone.utc)


def mk_leg(conn, adep="SBBR", ades="SBFL", typ="B738"):
    conn.execute("INSERT INTO import_batch(source_kind, imported_at) VALUES ('t', '2026-11-01T00:00:00Z')")
    conn.execute("INSERT OR IGNORE INTO roster_schedule(id, skey, airline, first_batch, last_batch) VALUES (1,'k','GLO',1,1)")
    cur = conn.execute("""INSERT INTO roster_leg(uid, schedule_id, seq, airline, flight_number, type_icao, adep, ades, dep_utc, arr_utc, eet_min,
                          data_origin, content_hash, first_batch, last_batch) VALUES ('u1',1,1,'GLO','GLO1748',?,?,?,'2026-11-02T01:00:00Z','2026-11-02T03:00:00Z',120,'REAL','h',1,1)""",
                       (typ, adep, ades))
    return cur.lastrowid


def fly(conn, sid, samples, batch=50):
    pm, pend, seq = PhaseMachine(), [], 0
    msgs, pts = [], []
    for i, s in enumerate(samples):
        for e in pm.feed(s):
            seq += 1
            msgs.append({"seq": seq, "t": iso(e["t"]), "kind": e["kind"], "text": e["text"]})
        if i % 3 == 0:
            pts.append({"t": iso(s["t"]), "lat": s["lat"], "lon": s["lon"], "alt_ft": s["alt_ft"], "gs_kt": s["gs_kt"], "vs_fpm": s["vs_fpm"], "phase": pm.phase})
    for i in range(0, len(pts), batch):
        ac.report(conn, sid, points=pts[i:i + batch], messages=msgs if i == 0 else [], snapshot={"phase": pm.phase})
    return pm, pts, msgs


def setup(conn):
    a = fleet.add_aircraft(conn, registration="PR-TST", airline="GLO", type_icao="B738", baseline_hours=100, baseline_cycles=50)
    leg = mk_leg(conn)
    f = flights.book_leg(conn, leg, aircraft_id=a["id"])
    return a, f


def test_full_flow_approval_updates_logbook_and_aircraft(conn):
    a, f = setup(conn)
    s = ac.start_session(conn, f["id"], sim="TESTE")
    assert flights.get_flight(conn, f["id"])["state"] == "EM_VOO"
    pm, pts, msgs = fly(conn, s["id"], synth.flight(T0))
    res = ac.complete(conn, s["id"], pm.summary())
    p = pireps.get_pirep(conn, res["pirep_id"])
    assert p["state"] == "PENDING" and p["data"]["block_min"] > 100 and p["data"]["air_min"] < p["data"]["block_min"]
    assert p["data"]["night_min"] is not None
    assert p["data"]["detected_adep"] == "SBBR" and p["data"]["detected_ades"] == "SBFL"
    assert not [w for w in p["warnings"] if w["level"] == "error"]
    assert flights.get_flight(conn, f["id"])["state"] == "PIREP_RECEBIDO"
    # aeronave NÃO muda antes da aprovação
    assert fleet.get_aircraft(conn, a["id"])["totals"]["total_cycles"] == 50
    pireps.approve(conn, p["id"])
    t = fleet.get_aircraft(conn, a["id"])
    assert t["totals"]["total_cycles"] == 51
    assert t["totals"]["total_hours"] == pytest.approx(100 + p["data"]["air_min"] / 60, abs=0.01)
    assert t["location"]["icao"] == "SBFL"
    lb = pireps.logbook(conn)
    assert len(lb) == 1 and lb[0]["registration"] == "PR-TST" and lb[0]["source"] == "ACARS"
    assert flights.get_flight(conn, f["id"])["state"] == "APROVADO"
    with pytest.raises(Conflict):
        pireps.approve(conn, p["id"])                      # não aprova duas vezes
    # estorno desfaz horas e ciclos, preserva histórico
    pireps.reverse(conn, p["id"], reason="teste")
    assert fleet.get_aircraft(conn, a["id"])["totals"]["total_cycles"] == 50
    assert pireps.logbook(conn) == []
    pireps.approve(conn, p["id"])
    assert fleet.get_aircraft(conn, a["id"])["totals"]["total_cycles"] == 51


def test_report_is_idempotent_and_complete_is_idempotent(conn):
    a, f = setup(conn)
    s = ac.start_session(conn, f["id"])
    samples = synth.flight(T0)
    pm, pts, msgs = fly(conn, s["id"], samples)
    n1 = len(ac.track_for(conn, s["id"]))
    ac.report(conn, s["id"], points=pts[:20], messages=msgs)       # reenvio
    assert len(ac.track_for(conn, s["id"])) == n1
    assert len(ac.messages_for(conn, s["id"])) == len(msgs)
    r1 = ac.complete(conn, s["id"], pm.summary())
    r2 = ac.complete(conn, s["id"], pm.summary())
    assert r2["duplicate"] and r1["pirep_id"] == r2["pirep_id"]
    assert conn.execute("SELECT COUNT(*) FROM pirep").fetchone()[0] == 1


def test_resume_live_session(conn):
    a, f = setup(conn)
    s1 = ac.start_session(conn, f["id"])
    s2 = ac.start_session(conn, f["id"])
    assert s2["id"] == s1["id"] and s2["resumed"]


def test_abort_returns_flight_to_reserved(conn):
    a, f = setup(conn)
    s = ac.start_session(conn, f["id"])
    ac.abort(conn, s["id"], reason="falha no sim")
    assert flights.get_flight(conn, f["id"])["state"] == "RESERVADO"
    assert ac.start_session(conn, f["id"])["id"] != s["id"]


def test_blocking_discrepancy_prevents_start_and_booking(conn):
    a, f = setup(conn)
    fleet.add_discrepancy(conn, a["id"], "Falha de pressurização", restriction="BLOQUEIA")
    with pytest.raises(Conflict) as e:
        ac.start_session(conn, f["id"])
    assert e.value.code == "aeronave_indisponivel"
    other = mk_leg_2(conn)
    with pytest.raises(Conflict):
        flights.book_leg(conn, other, aircraft_id=a["id"])


def mk_leg_2(conn):
    cur = conn.execute("""INSERT INTO roster_leg(uid, schedule_id, seq, airline, flight_number, type_icao, adep, ades, dep_utc, arr_utc,
                          data_origin, content_hash, first_batch, last_batch) VALUES ('u2',1,2,'GLO','GLO1','B738','SBFL','SBBR','2026-11-03T01:00:00Z','2026-11-03T03:00:00Z','REAL','h',1,1)""")
    return cur.lastrowid


def test_confirm_restriction_requires_confirmation(conn):
    a, f = setup(conn)
    fleet.add_discrepancy(conn, a["id"], "Reversor 2 inop", restriction="CONFIRMA", restriction_text="pista molhada limitada", deferred=True, due_date="2026-12-01")
    with pytest.raises(Conflict) as e:
        ac.start_session(conn, f["id"])
    assert e.value.code == "confirmacao_necessaria"
    assert ac.start_session(conn, f["id"], confirm_restrictions=True)["restrictions"]


def test_overlap_and_unassigned(conn):
    a, f = setup(conn)
    conn.execute("""INSERT INTO roster_leg(uid, schedule_id, seq, airline, flight_number, type_icao, adep, ades, dep_utc, arr_utc,
                    data_origin, content_hash, first_batch, last_batch) VALUES ('u3',1,3,'GLO','GLO2','B738','SBFL','SBBR','2026-11-02T02:00:00Z','2026-11-02T04:00:00Z','REAL','h',1,1)""")
    lid = conn.execute("SELECT id FROM roster_leg WHERE uid='u3'").fetchone()[0]
    with pytest.raises(Conflict) as e:
        flights.book_leg(conn, lid, aircraft_id=a["id"])
    assert e.value.code == "aeronave_ocupada"
    nf = flights.book_leg(conn, lid)                            # sem aeronave é permitido…
    with pytest.raises(Conflict) as e:
        ac.start_session(conn, nf["id"])                        # …mas ACARS exige aeronave
    assert e.value.code == "sem_aeronave"


def test_bad_pirep_cannot_be_blindly_approved(conn):
    a, f = setup(conn)
    s = ac.start_session(conn, f["id"])
    res = ac.complete(conn, s["id"], {"off_utc": "2026-11-02T01:20:00Z", "on_utc": "2026-11-02T03:00:00Z"})   # sem OUT/IN
    assert {w["code"] for w in res["warnings"]} >= {"sem_out", "sem_in", "sem_trilha"}
    with pytest.raises(Conflict) as e:
        pireps.approve(conn, res["pirep_id"])
    assert e.value.code == "horas_ausentes"
    pireps.reject(conn, res["pirep_id"], notes="incompleto")
    assert flights.get_flight(conn, f["id"])["state"] == "REJEITADO"
    assert fleet.get_aircraft(conn, a["id"])["totals"]["flown_cycles"] == 0
    flights.reactivate(conn, f["id"])
    assert flights.get_flight(conn, f["id"])["state"] == "RESERVADO"


def test_invalid_payloads_rejected(conn):
    a, f = setup(conn)
    s = ac.start_session(conn, f["id"])
    with pytest.raises(FGVError):
        ac.report(conn, s["id"], points=[{"t": "ontem", "lat": 1, "lon": 1}])
    with pytest.raises(FGVError):
        ac.report(conn, s["id"], points=[{"t": "2026-11-02T01:00:00Z", "lat": 99, "lon": 1}])
    with pytest.raises(FGVError):
        ac.complete(conn, s["id"], {"out_utc": "x"})


# ---------------------------------------------------------------- regras de pernoite (portadas do AeroScale)
from datetime import datetime as _dt, timezone as _tz   # noqa: E402

from lineops import journey  # noqa: E402


def _u(s):
    return _dt.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_tz.utc)


@pytest.mark.parametrize("arr,dep,icao,esperado", [
    ("2026-11-02T01:59:00Z", "2026-11-02T02:50:00Z", "SBFL", "solo"),                  # 51 min: vira o dia UTC, mas é solo
    ("2026-11-02T01:59:00Z", "2026-11-02T01:30:00Z", "SBFL", "sobreposicao"),
    ("2026-11-01T23:00:00Z", "2026-11-02T11:00:00Z", "SBBR", "pernoite"),              # chega 20:00 local, sai 08:00 local
    ("2026-11-02T05:00:00Z", "2026-11-02T20:26:00Z", "SAEZ", "solo_longo"),            # 15h de dia: não é pernoite pela classificação
    ("2026-11-02T04:30:00Z", "2026-11-02T08:00:00Z", "SBBR", "parada_noturna_curta"),  # 01:30 → 05:00 local
    ("2026-11-02T12:00:00Z", "2026-11-04T12:00:00Z", "SBBR", "parada_longa"),
    ("2026-11-02T12:00:00Z", "2026-11-02T14:30:00Z", "SBBR", "solo"),
])
def test_gap_kind_matches_aeroscale_rules(conn, arr, dep, icao, esperado):
    assert journey.rules.gap_kind(conn, _u(arr), _u(dep), icao) == esperado


def test_gap_below_min_turn_is_flagged(conn):
    assert journey.rules.gap_kind(conn, _u("2026-11-02T12:00:00Z"), _u("2026-11-02T12:20:00Z"), "SBBR", 45) == "solo_curto"


def test_six_hour_stop_counts_as_overnight_like_aeroscale(conn):
    a = {"ades": "SAEZ", "arr_utc": "2026-11-02T05:00:00Z", "uid": "a"}
    b = {"adep": "SAEZ", "dep_utc": "2026-11-02T20:26:00Z"}
    g = journey._gap(conn, a, b)
    assert g["kind"] == "solo_longo" and g["overnight"] and g["overnight_label"] == "Pernoite"
    curto = journey._gap(conn, {"ades": "SBFL", "arr_utc": "2026-11-02T01:59:00Z", "uid": "x"}, {"adep": "SBFL", "dep_utc": "2026-11-02T02:50:00Z"})
    assert curto["kind"] == "solo" and not curto["overnight"]
