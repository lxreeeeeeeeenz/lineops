"""0.11: pasta de dados, log, validade, migrações pendentes, servidor do app e modo guiado do ACARS."""
import json
import socket
import urllib.request
from datetime import date

from lineops import db, desktop, logs, paths, payroll, validity
from lineops_acars import interactive


def test_data_dir_and_legacy_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEOPS_DATA", str(tmp_path / "x"))
    assert paths.default_data_dir() == tmp_path / "x"
    monkeypatch.delenv("LINEOPS_DATA")
    assert paths.default_data_dir().parts[-2:] == ("LineOps", "dados")
    old = tmp_path / "old"
    (old / "backups").mkdir(parents=True)
    (old / "lineops.db").write_bytes(b"x")
    (old / "backups" / "b.zip").write_bytes(b"z")
    new = tmp_path / "new"
    assert paths.adopt_legacy(new, [tmp_path / "nada", old]) == old
    assert (new / "lineops.db").exists() and (new / "backups" / "b.zip").exists() and (old / "lineops.db").exists()
    assert paths.adopt_legacy(new, [old]) is None                       # já tem dados: não copia de novo


def test_log_file_and_tail(tmp_path):
    import logging
    p = logs.setup(tmp_path)
    logging.getLogger("lineops.teste").info("olá log")
    for h in logging.getLogger().handlers:
        h.flush()
    assert p.exists() and any("olá log" in l for l in logs.tail(p))


def test_pending_migrations(tmp_path):
    c = db.connect(tmp_path / "a.db")
    assert "0001_init" in db.pending(c)
    db.migrate(c)
    assert db.pending(c) == []


def test_validity(conn, tmp_path):
    v = {x["key"]: x for x in validity.check(conn, tmp_path, today=date(2026, 11, 10))}
    assert v["cct"]["status"] == "vencendo" and not v["cct"]["relevant"]          # salários desligados
    assert validity.check(conn, None, today=date(2026, 12, 2))[0]["status"] == "vencido"
    assert v["impostos"]["status"] == "ok"
    payroll.set_enabled(conn, True)
    assert validity.check(conn, None, today=date(2026, 11, 10))[0]["relevant"]
    assert "backup" not in v                                                     # banco vazio: sem cobrança de backup


def test_lan_ip_and_ports():
    ip = desktop.lan_ip()
    assert ip is None or not ip.startswith("127.")
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    assert not desktop.port_free(port)
    s.close()
    assert desktop.port_free(port) and not desktop.already_running(port, timeout=0.3)


def test_embedded_server_start_restart_stop():
    async def app(scope, receive, send):                                       # ASGI mínimo no lugar do LineOps
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": json.dumps({"app": "LineOps"}).encode()})
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = desktop.Server(app, port)
    srv.start()
    assert desktop.already_running(port)
    srv.restart("127.0.0.1")                  # religar (0.0.0.0 abriria o aviso do firewall do Windows no build)
    assert not srv.lan and desktop.already_running(port)
    srv.stop()
    assert not desktop.already_running(port, timeout=0.3)


def test_acars_cfg_roundtrip(tmp_path):
    p = tmp_path / "acars.cfg"
    interactive.save(p, {"SERVER": "http://192.168.0.10:8787", "TOKEN": "gol_abc", "SIM": "p3d"})
    assert interactive.load(p) == {"SERVER": "http://192.168.0.10:8787", "TOKEN": "gol_abc", "SIM": "p3d"}


def test_temp_dir_cleanup_survives_locked_file(tmp_path, monkeypatch):
    """No Windows, arquivo aberto não se apaga: a limpeza tenta de novo e nunca derruba a operação."""
    import shutil

    from lineops import fsutil
    calls = {"n": 0}
    real = shutil.rmtree

    def flaky(p, *a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(32, "O arquivo já está sendo usado por outro processo")
        return real(p, *a, **k)
    monkeypatch.setattr(fsutil.shutil, "rmtree", flaky)
    monkeypatch.setattr(fsutil.time, "sleep", lambda s: None)
    with fsutil.temp_dir("lineops_t_") as d:
        (d / "work.db").write_bytes(b"x")
    assert calls["n"] == 3 and not d.exists()
    monkeypatch.setattr(fsutil.shutil, "rmtree", lambda *a, **k: (_ for _ in ()).throw(PermissionError(32, "preso")))
    with fsutil.temp_dir("lineops_t_") as d2:                         # nunca solta: só registra, não quebra
        pass
    real(d2)


def test_import_cleans_temp_even_if_windows_locks(conn, tmp_path, monkeypatch):
    from lineops import aeroscale_import as ai, fsutil
    from tests.test_importer import L, make_db
    p = tmp_path / "a.db"
    make_db(p, legs=[L("a", "GLO1")])
    n = {"c": 0}
    real = fsutil.shutil.rmtree

    def locked_once(path, *a, **k):
        n["c"] += 1
        if n["c"] == 1:
            raise PermissionError(32, "work.db em uso")
        return real(path, *a, **k)
    monkeypatch.setattr(fsutil.shutil, "rmtree", locked_once)
    monkeypatch.setattr(fsutil.time, "sleep", lambda s: None)
    assert ai.import_backup(conn, p)["legs_new"] == 1
    assert ai.import_backup(conn, p, dry_run=True)["legs_unchanged"] == 1
