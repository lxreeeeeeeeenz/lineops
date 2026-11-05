"""Pernoite pedido no gerador, voos de carga da GOL fora da escala, aviso de pernoite no Discord e frota de referência."""
import io
import json
import shutil
from datetime import date

import pytest

from lineops import db, frota_ref, journey, pernoite, troca
from lineops.errors import FGVError
from tests.test_gerador import BASE, _run, eng  # noqa: F401

REQS = [{"date": "2026-11-05", "airport": "REC"}, {"date": "2026-11-12", "airport": "SBFZ"}, {"date": "2026-11-20", "airport": "POA"}]


def _sleep(c, night: str) -> str | None:
    legs = []
    for l in c.execute("SELECT * FROM roster_leg WHERE removed_batch IS NULL ORDER BY dep_utc"):
        l = dict(l)
        dep, arr = journey.parse(l["dep_utc"]), journey.parse(l["arr_utc"])
        lt = journey.rules.local_dt(c, dep, l["adep"])
        legs.append({"ld": date.fromisoformat(l["local_date"]), "hour": lt.hour, "dep": dep, "arr": arr, "adep": l["adep"], "ades": l["ades"]})
    return pernoite.sleep_at(legs, date.fromisoformat(night), "SBGR")[0]


def test_requests_are_validated(eng):  # noqa: F811
    _data, career = eng
    c = db.connect(career)
    r = pernoite.parse_requests(c, REQS, "2026-11-01", "2026-11-30")
    assert [x["icao"] for x in r] == ["SBRF", "SBFZ", "SBPA"]
    for bad in ([{"date": "2026-12-05", "airport": "REC"}], [{"date": "2026-11-05", "airport": "XXX"}],
                [{"date": "2026-11-05", "airport": "REC"}, {"date": "2026-11-05", "airport": "FOR"}], [{"date": "ontem", "airport": "REC"}]):
        with pytest.raises(FGVError):
            pernoite.parse_requests(c, bad, "2026-11-01", "2026-11-30")
    with pytest.raises(FGVError):                                                  # folga pedida na mesma noite
        pernoite.parse_requests(c, REQS[:1], "2026-11-01", "2026-11-30", requested_off=["2026-11-05"])
    c.close()


def test_generator_ends_the_day_where_you_asked(eng):  # noqa: F811
    data, career = eng
    res = _run(data, career, {**BASE, "seed": 3, "overnights": REQS})
    head = res["diagnostics"][0]["message"]
    assert head.startswith("Pernoites pedidos:") and "Semente desta escala" in head
    ok = [d for d in res["diagnostics"] if d.get("code") == "pernoite_pedido_ok"]
    assert len(ok) >= 2, res["diagnostics"][:8]
    c = db.connect(career)
    want = {r["night"]: r["icao"] for r in pernoite.parse_requests(c, REQS, "2026-11-01", "2026-11-30")}
    for d in ok:                                                                    # o diagnóstico bate com a escala de verdade
        assert d["date"] in want and _sleep(c, d["date"]) == want[d["date"]]
    origins = {r[0] for r in c.execute("SELECT DISTINCT data_origin FROM roster_leg WHERE removed_batch IS NULL")}
    assert origins <= {"REAL", "PROJETADO", "OBSERVADO"}                            # nada inventado para cumprir o pedido
    rep = journey.crew_report(c, "2026-11-01", "2026-11-30")
    assert sum(1 for x in rep["duties"] for i in x["issues"] if i["level"] == "error") == 0
    c.close()
    again = _run(data, career, {**BASE, "seed": 3, "overnights": REQS})            # mesma semente, mesma escala
    assert again["diagnostics"][0]["message"] == head


def test_cargo_flights_stay_out_unless_asked(eng):  # noqa: F811
    from lineops import cargo
    assert cargo.is_cargo("GLO", "GLO9510") and not cargo.is_cargo("GLO", "GLO1510") and not cargo.is_cargo("TAM", "TAM9510")
    data, career = eng
    _run(data, career, {**BASE, "seed": 3})
    c = db.connect(career)
    assert c.execute("SELECT COUNT(*) FROM roster_leg WHERE removed_batch IS NULL AND flight_number LIKE 'GLO9___'").fetchone()[0] == 0
    leg = dict(c.execute("SELECT * FROM roster_leg WHERE removed_batch IS NULL AND activity='' ORDER BY dep_utc LIMIT 1").fetchone())
    idx, _ = troca._idx_for(data, leg, {})
    d = journey.parse(leg["dep_utc"]).date()
    assert not any(cargo.is_cargo("GLO", o["ident"]) for o in idx.occurrences(d))   # nem nas trocas
    idx2, _ = troca._idx_for(data, leg, {"include_cargo": True})
    assert not isinstance(idx2, troca._NoCargo)
    c.close()


class FakeDiscord:
    def __init__(self, code=200):
        self.calls, self.code = [], code

    def __call__(self, req, timeout=None):
        self.calls.append((req.get_method(), req.full_url, json.loads(req.data.decode()) if req.data else None))
        fake = self

        class R(io.BytesIO):
            status = fake.code

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R(json.dumps({"id": "1234567890"}).encode())


HOOK = "https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz_ABCDEF-123"


def test_discord_overnight_only_for_cmdt079(eng, tmp_path):  # noqa: F811
    data, career = eng
    _run(data, career, {**BASE, "seed": 3})
    p = tmp_path / "GLO.db"
    shutil.copy(career, p)
    c = db.connect(p)
    core = c                                                                         # mesma tabela setting serve de núcleo no teste
    friend, other = {"id": 1, "username": "CMDT079", "display_name": "Cmdt 079"}, {"id": 2, "username": "piloto"}
    assert pernoite.enabled_for(friend) and pernoite.enabled_for(other) and not pernoite.enabled_for(None)   # 3.0.1: todos
    with pytest.raises(FGVError):
        pernoite.set_webhook(core, friend, "https://example.com/hook")
    info = pernoite.webhook_info(core, friend)
    assert info["configured"] and info["default"] and pernoite.WEBHOOK_RE.match(pernoite.DEFAULT_WEBHOOK)   # a padrão vale sem cadastro
    assert pernoite.set_webhook(core, friend, HOOK)["configured"]
    # a última perna de um dia que termina em pernoite fora da base
    day = next(d for d in (journey.day(c, f"2026-11-{k:02d}") for k in range(1, 31)) if d["overnight"] and not d["overnight"]["at_base"])
    leg = [l for l in day["legs"] if l["is_flight"]][-1]
    c.execute("INSERT INTO airport_info(icao, hotel, updated_at) VALUES (?, 'Hotel da lista', '2026-11-01T00:00:00Z')", (leg["ades"],))
    c.commit()
    fake = FakeDiscord()
    r = pernoite.mark(c, core, friend, leg["id"], opener=fake, color=0xFF6A13)
    assert r["ok"] and r["detected"]
    meth, url, body = fake.calls[-1]
    assert meth == "POST" and url.endswith("?wait=true") and body["allowed_mentions"] == {"parse": []}
    emb = body["embeds"][0]
    vals = {f["name"]: f["value"] for f in emb["fields"]}
    assert leg["flight_number"] in vals["Voo"] and "→" in vals["Trecho"] and vals["Hotel"] == "Hotel da lista"
    assert "local" in vals["Chegada"] and vals["Próxima apresentação"] != "sem próximo voo na escala"
    assert pernoite.status_for(c, [leg["id"]])[str(leg["id"])]["state"] == "ENVIADO"
    pernoite.mark(c, core, friend, leg["id"], opener=fake)                          # marcar de novo edita a mesma mensagem
    assert fake.calls[-1][0] == "PATCH" and fake.calls[-1][1].endswith("/messages/1234567890")
    assert pernoite.unmark(c, core, friend, leg["id"], opener=fake)["state"] is None
    assert fake.calls[-1][0] == "DELETE"
    assert pernoite.status_for(c, [leg["id"]])[str(leg["id"])]["state"] == "REMOVIDO"
    bad = FakeDiscord(code=401)
    with pytest.raises(FGVError):
        pernoite.mark(c, core, friend, leg["id"], opener=bad)
    assert pernoite.status_for(c, [leg["id"]])[str(leg["id"])]["state"] == "ERRO"
    c.close()


def test_reference_fleet(tmp_path):
    from lineops import fleet
    p = tmp_path / "c.db"
    c = db.connect(p)
    db.migrate(c)
    fleet.add_aircraft(c, registration="PR-GXA", airline="GLO", type_icao="B738", config={"sfp": {"value": "meu valor", "source": "eu"}})
    r = frota_ref.ensure(c, "GLO")
    assert r["added"] == 73 and r["filled"] == 1
    gxa = fleet.get_aircraft(c, c.execute("SELECT id FROM aircraft WHERE registration='PR-GXA'").fetchone()[0], totals=False)
    assert gxa["config"]["sfp"]["value"] == "meu valor" and gxa["config"]["fmc"]["value"] == "Collins"   # o seu valor fica
    assert not c.execute("SELECT 1 FROM aircraft WHERE registration LIKE 'PS-GF%'").fetchone()           # cargueiros fora
    gyd = c.execute("SELECT config FROM aircraft WHERE registration='PR-GYD'").fetchone()[0]
    cfg = json.loads(gyd)
    assert cfg["sfp"]["value"] == "SFP 3.0" and cfg["standby"]["value"] == "ISFD" and cfg["thrust"]["value"] == "27K"
    assert cfg["remark"]["value"] == "NEW CUTOFF / LED" and cfg["fmc"]["source"] == frota_ref.SOURCE
    assert frota_ref.ensure(c, "GLO")["skipped"]                                     # uma vez por versão
    c.execute("DELETE FROM setting WHERE key=?", (frota_ref.KEY,))
    assert frota_ref.ensure(c, "GLO")["added"] == 0
    c.close()
    p2 = tmp_path / "t.db"
    t = db.connect(p2)
    db.migrate(t)
    assert frota_ref.ensure(t, "TAM")["added"] == 57 + 38 + 18
    tqb = json.loads(t.execute("SELECT config FROM aircraft WHERE registration='PR-TQB'").fetchone()[0])
    assert {k: v["value"] for k, v in tqb.items()} == {"standby": "ISIS", "ddrmi": "NÃO", "dcdu": "SIM"}
    lha = t.execute("SELECT type_icao, config FROM aircraft WHERE registration='PS-LHA'").fetchone()
    assert lha[0] == "A20N" and json.loads(lha[1]) == {}
    t.close()


def test_pilot_code_follows_the_career_and_avatar_only_for_cmdt079():
    from lineops import avatar, careers
    assert careers.pilot_code("GOL001", "GLO") == "GOL001"
    assert careers.pilot_code("GOL001", "TAM") == "TAM001" and careers.pilot_code("GOL001", "AZU") == "AZU001"
    assert careers.pilot_code("GOL123", "TAM") == "TAM123" and careers.pilot_code(None, "GLO") == "GOL001"
    f = avatar.of({"username": "cmdt079"})
    assert f and f.read_bytes()[:2] == b"\xff\xd8"                                   # JPEG
    assert avatar.of({"username": "CMDT079 "}) == f and avatar.of({"username": "piloto"}) is None
