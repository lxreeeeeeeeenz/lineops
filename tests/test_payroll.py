"""Salários: valores da CCT, impostos de 2026, horas da escala, diárias e fechamento."""
import pytest

from lineops import aeroscale_import as ai, payroll as pr
from lineops.errors import Conflict, FGVError
from tests.test_importer import L, make_db

NOW = "2026-12-03T15:00:00Z"            # dezembro: novembro já terminou


def legs_nov():
    """Novembro/2026 (escala sem base: fuso de Brasília). 1/11 é domingo; 2/11 Finados; 15/11 domingo e feriado; 20/11 feriado."""
    out = []
    # 30 pernas de 2 h às 14:00Z (11:00 local) nos dias 1..30: 60 h de voo, todas na janela do almoço
    for d in range(1, 31):
        out.append(L(f"n{d}", f"GLO{1000 + d}", dep=f"2026-11-{d:02d}T14:00:00Z", arr=f"2026-11-{d:02d}T16:00:00Z"))
    return out


@pytest.fixture
def setup(conn, tmp_path):
    p = tmp_path / "as.db"
    make_db(p, legs=legs_nov())          # make_db já cria uma reserva de 24 a 27/11 sem acionamento
    ai.import_backup(conn, p)
    pr.set_enabled(conn, True)
    return conn


def test_cct_presets_and_piso_split():
    c = pr.preset("Comandante", "jato_nb")
    assert c["fixed_total"] == 11643.24 and c["hour_rate"] == 127.31 and c["guarantee_h"] == 54
    assert pr.preset("Comandante", "jato_wb")["hour_rate"] == 147.43
    assert pr.preset("Copiloto", "jato_nb")["fixed_total"] == 6069.34
    assert pr.preset("Copiloto", "tp_mono")["hour_rate"] == 13.39
    assert pr.preset("Comandante", "tp_mono")["fixed_total"] == 6670.80
    assert pr.CATEGORIES[pr.FAMILY_CATEGORY["B737"]] == "Jato narrowbody"
    for (role, cat), piso in pr.PISO.items():          # base + compensação orgânica fecha o piso no centavo
        base = pr.money(piso / 1.2)
        assert pr.money(base + pr.money(piso - base)) == piso
    with pytest.raises(FGVError):
        pr.preset("Mecânico", "jato_nb")


def test_inss_irrf_2026():
    assert pr.inss(6000) == 641.51
    assert pr.inss(50000) == pr.inss(8475.55) == 988.09
    ir = pr.irrf(6000, 641.51)
    assert ir["table"] == 564.85 and ir["reducer"] == 179.75 and ir["value"] == 385.10
    assert pr.irrf(5000, pr.inss(5000))["value"] == 0.0
    assert pr.irrf(3000, pr.inss(3000))["method"] == "desconto simplificado"


def test_holidays():
    h = pr.holidays(2026)
    assert h["2026-04-03"] == "Paixão de Cristo"            # Páscoa de 2026: 5 de abril
    assert "2026-11-20" in h and "2026-11-02" in h
    assert "2026-03-19" in pr.holidays(2026, ["03-19"])


def test_disabled_by_default(conn):
    assert pr.enabled(conn) is False
    with pytest.raises(Conflict):
        pr.require(conn)


def test_needs_contract_then_compute(setup):
    conn = setup
    d = pr.compute(conn, "2026-11", "programado", now=NOW)
    assert d["needs_contract"] and d["suggestion"]["category"] == "jato_nb"      # tipo B738 da escala
    pr.save_contract(conn, "2026-11", {"role": "Comandante", "category": "jato_nb", "taxes_enabled": True})
    d = pr.compute(conn, "2026-11", "programado", now=NOW)
    h = d["hours"]
    assert h["flight_min"] == 60 * 60
    assert h["reserve_days"] == ["2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27"] and h["reserve_min"] == 24 * 60
    # 60 h de voo + 24 h de reserva − 54 h de garantia = 30 h × R$ 127,31
    lines = {l["code"]: l for l in d["lines"]}
    assert lines["010"]["amount"] == pr.money(30 * 127.31) == 3819.30
    assert pr.money(lines["001"]["amount"] + lines["002"]["amount"]) == 11643.24
    # domingos 1, 8, 15, 22, 29 + feriados 2 e 20 (15 já é domingo): 7 pernas de 2 h
    assert h["sunday_holiday_min"] == 14 * 60
    assert lines["011"]["amount"] == pr.money(14 * 127.31)
    # DSR: (variável + dobra) ÷ 22 × 8
    assert lines["013"]["amount"] == pr.money((3819.30 + pr.money(14 * 127.31)) / 22 * 8)
    # diárias: 30 jornadas 10:00–13:30 locais (apresentação 60 min do perfil GOL, encerramento 30 min) -> 30 almoços
    assert d["meals"]["domestic"]["main"] == 30 and lines["050"]["amount"] == pr.money(30 * 109.95)
    assert lines["050"]["taxable"] is False
    t = d["totals"]
    assert t["taxable"] == pr.money(sum(l["amount"] for l in d["lines"] if l["kind"] == "provento" and l["taxable"]))
    assert lines["090"]["amount"] == 988.09                       # teto do INSS
    assert t["net"] == pr.money(t["gross"] - t["deductions"])
    assert "noturno" not in " ".join(l["label"].lower() for l in d["lines"])   # desligado por padrão


def test_standby_counts_one_third(setup):
    conn = setup
    pr.save_contract(conn, "2026-11", {"role": "Copiloto", "category": "jato_nb"})
    conn.execute("""INSERT INTO roster_leg(uid, schedule_id, airline, adep, ades, dep_utc, arr_utc, data_origin, activity, content_hash,
                    first_batch, last_batch, local_date) VALUES ('sb1', 1, 'GLO', 'SBBR', 'SBBR', '2026-11-03T20:00:00Z',
                    '2026-11-04T08:00:00Z', 'MANUAL', 'SOBREAVISO', 'x', 1, 1, '2026-11-03')""")
    d = pr.compute(conn, "2026-11", "programado", now=NOW)
    assert d["hours"]["standby_min"] == 12 * 60
    # 60 + 24 + 12/3 = 88 h equivalentes − 54 = 34 h × 56,28
    exc = {l["code"]: l for l in d["lines"]}["010"]["amount"]
    assert abs(exc - 34 * 56.28) <= 0.03


def test_custom_values_are_marked_and_validated(setup):
    conn = setup
    c = pr.save_contract(conn, "2026-11", {"role": "Comandante", "category": "jato_nb", "hour_rate": 194.51, "night_pct": 20,
                                           "extra_holidays": ["11-21"]})["data"]
    assert c["sources"]["hour_rate"] == "informado por você"
    assert c["sources"]["fixed_total"].startswith("CCT")
    with pytest.raises(FGVError):
        pr.save_contract(conn, "2026-11", {"role": "Comandante", "category": "jato_nb", "hour_rate": -1})
    with pytest.raises(FGVError):
        pr.save_contract(conn, "2026-13", {"role": "Comandante", "category": "jato_nb"})


def test_realized_uses_logbook_and_close(setup):
    conn = setup
    pr.save_contract(conn, "2026-11", {"role": "Comandante", "category": "jato_nb"})
    conn.execute("""INSERT INTO logbook_entry(date, adep, ades, out_utc, in_utc, block_min, air_min, source, created_at)
                    VALUES ('2026-11-10', 'SBBR', 'SBFL', '2026-11-10T22:00:00Z', '2026-11-11T00:10:00Z', 130, 110, 'ACARS', 'x')""")
    d = pr.compute(conn, "2026-11", "realizado", now=NOW)
    assert d["hours"]["flight_min"] == 130
    assert d["meals"]["domestic"]["main"] == 1                     # jantar 19:00–20:00 local (22:00Z)
    with pytest.raises(Conflict):
        pr.close_month(conn, "2026-11", now="2026-11-20T12:00:00Z")   # mês em andamento
    cl = pr.close_month(conn, "2026-11", "teste", now=NOW)
    assert cl["data"]["totals"]["net"] == d["totals"]["net"]
    with pytest.raises(Conflict):
        pr.close_month(conn, "2026-11", now=NOW)
    pr.void_closing(conn, cl["id"], "refazer")
    assert pr.close_month(conn, "2026-11", now=NOW)["id"] != cl["id"]
    y = pr.year(conn, 2026, now=NOW)
    assert [m["month"] for m in y["months"]] == ["2026-11", "2026-12"] and y["months"][0]["closed"]


def test_meal_location_and_hotel_breakfast():
    tz = pr.get_tz("America/Sao_Paulo")
    c = pr.preset("Comandante", "jato_nb")
    from datetime import date
    legs = [{"adep": "SBGR", "ades": "KMIA", "_dep": pr.parse("2026-11-05T09:30:00Z"), "_arr": pr.parse("2026-11-05T18:00:00Z")}]
    duty = {"date": "2026-11-05", "report_utc": "2026-11-05T08:30:00Z", "release_utc": "2026-11-05T18:30:00Z", "start": "SBGR",
            "end": "KMIA", "legs": 1, "night_at": "SBGR", "_legs": legs}
    m = pr.meals([duty], c, tz, date(2026, 11, 1), date(2026, 11, 30))
    kinds = {x["kind"]: x for x in m["duties"][0]["meals"]}
    assert kinds["cafe"]["value"] == 0.0                           # 05:30 local, pernoite: incluso no hotel
    assert kinds["almoco"]["currency"] == "BRL"                    # em voo saindo de GRU: diária nacional
    assert m["domestic"]["hotel_skipped"] == 1
    assert pr.region("KMIA") == "NA" and pr.region("SAEZ") == "SA" and pr.region("EGLL") == "UK" and pr.region("LPPT") == "EU"
