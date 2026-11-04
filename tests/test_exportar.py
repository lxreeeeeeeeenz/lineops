"""Escala em texto: sai igual ao exportador do AeroScale e acompanha o que mudou na carreira (corte, regerar o mês)."""
import json
import shutil

import pytest

from lineops import db, exportar, motor, posicao
from lineops.aeroscale import scheduler
from lineops.aeroscale_import import import_backup
from lineops.errors import FGVError
from tests.test_gerador import BASE, _run, eng  # noqa: F401


def _engine_text(data, **kw):
    c = motor.connect(data)
    try:
        sid = c.execute("SELECT id FROM schedules WHERE airline='GLO' ORDER BY id DESC LIMIT 1").fetchone()[0]
        return sid, scheduler.export_txt(c, sid, **kw)
    finally:
        c.close()


@pytest.mark.parametrize("style,times", [("companhia", False), ("companhia", True), ("classico", False)])
def test_same_text_as_aeroscale(eng, style, times):  # noqa: F811
    data, career = eng
    _run(data, career, {**BASE, "seed": 8})                                         # regerar o mês: a escala nova vale inteira
    _sid, want = _engine_text(data, style=style, times=times)
    c = db.connect(career)
    got = exportar.roster_text(c, "2026-11-01", "2026-11-30", style=style, times=times)
    c.close()
    assert got["text"] == want and got["legs"] > 0
    assert got["text"].startswith("ESCALA DE NOVEMBRO:\n(01) ")


def test_cut_shows_no_schedule_and_survives_reimport(eng, tmp_path):  # noqa: F811
    data, career = eng
    sid = _run(data, career, {**BASE, "seed": 3})["schedule_id"]
    p = tmp_path / "GLO.db"
    shutil.copy(career, p)
    c = db.connect(p)
    pos = posicao.cut(c, "2026-11-10T15:00:00Z")["position"]
    day = int(pos["date"][8:10])
    t = exportar.roster_text(c, "2026-11-01", "2026-11-30")
    assert t["end"] == pos["date"]                                                   # depois do corte não há escala: o texto para ali
    assert "SEM ESCALA" not in t["text"]
    import_backup(c, str(motor.paths(data)[0]), airline="GLO", schedule_ids=[sid])  # a mesma escala do motor chega de novo
    s = c.execute("SELECT end_date, params FROM roster_schedule WHERE id=(SELECT MAX(id) FROM roster_schedule)").fetchone()
    assert s["end_date"] == pos["date"] and json.loads(s["params"]).get("cut_after")    # o corte continua valendo
    assert exportar.roster_text(c, "2026-11-01", "2026-11-30")["text"] == t["text"]
    assert f"({day:02d})" in t["text"]
    c.close()


def test_bad_period(eng):  # noqa: F811
    data, career = eng
    c = db.connect(career)
    with pytest.raises(FGVError):
        exportar.roster_text(c, "2026-11-30", "2026-11-01")
    with pytest.raises(FGVError):
        exportar.roster_text(c, "ontem", "2026-11-01")
    assert exportar.roster_text(c, "2030-01-01", "2030-01-31")["empty"]
    c.close()
