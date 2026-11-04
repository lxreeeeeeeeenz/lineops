"""Abrir a LineOps com o GolOps antigo aberto: quem está na porta, fechar o antigo e copiar (ou recopiar) os dados certos."""
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from lineops import careers, db, legacy, paths


def _serve(app_name):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"ok": True, "app": app_name}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_probe_tells_old_app_from_new():
    old, new, other = _serve("Gol" "Ops"), _serve("LineOps"), _serve("Outro")
    try:
        assert legacy.probe(old.server_port) == "golops"
        assert legacy.probe(new.server_port) == "lineops"
        assert legacy.probe(other.server_port) == "outro"
        with pytest.raises(legacy.PortBusy) as e:
            legacy.before_start(other.server_port)
        assert "ocupada" in str(e.value)
        assert legacy.before_start(new.server_port) == "lineops"
        if os.name != "nt":                                   # fora do Windows não fecha nada: explica o que fazer
            with pytest.raises(legacy.PortBusy) as e:
                legacy.before_start(old.server_port)
            assert "Sair do" in str(e.value)
    finally:
        for s in (old, new, other):
            s.shutdown()
    s = _serve("x")
    port = s.server_port
    s.shutdown(); s.server_close()
    assert legacy.probe(port) is None and legacy.before_start(port) == "livre"


def test_listening_pids_from_netstat():
    out = """
Conexões ativas
  Proto  Endereço local         Endereço externo       Estado         PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1012
  TCP    127.0.0.1:8787         0.0.0.0:0              LISTENING       5544
  TCP    127.0.0.1:8787         127.0.0.1:50123        ESTABLISHED     5544
  TCP    127.0.0.1:50123        127.0.0.1:8787         ESTABLISHED     900
  TCP    [::]:8787              [::]:0                 LISTENING       5544
  TCP    0.0.0.0:18787          0.0.0.0:0              LISTENING       77
"""
    assert legacy.listening_pids(8787, out) == {5544}


def _old_dir(base):
    d = base / "GolOps" / "dados"
    d.mkdir(parents=True)
    c = db.connect(d / ("gol" "ops.db"))
    db.migrate(c)
    c.execute("INSERT INTO user(id, username, pw_hash, display_name, created_at) VALUES (1,'triple','x','Triple','2026-01-01T00:00:00Z')")
    c.execute("INSERT INTO aircraft(registration, airline, type_icao, active, source, created_at, updated_at) VALUES ('PR-GUZ','GLO','B738',1,'MANUAL','x','x')")
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    return d


def _touch_old(d, reg):
    c = db.connect(d / ("gol" "ops.db"))
    c.execute("INSERT INTO aircraft(registration, airline, type_icao, active, source, created_at, updated_at) VALUES (?,'GLO','B738',1,'MANUAL','x','x')", (reg,))
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    t = time.time() + 120
    os.utime(d / ("gol" "ops.db"), (t, t))


def _regs(new):
    c = db.connect(careers.career_path(new, "GLO"))
    try:
        return {r[0] for r in c.execute("SELECT registration FROM aircraft")}
    finally:
        c.close()


def test_stale_copy_is_redone_while_lineops_was_never_used(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "_base", lambda: tmp_path)
    old = _old_dir(tmp_path)
    new = tmp_path / "LineOps" / "dados"
    r = legacy.prepare(new, [])
    assert r["action"] == "copiado" and (new / legacy.MARK).exists()
    careers.bootstrap(new)                                    # o servidor sobe, monta a carreira GOL... mas a porta estava com o GolOps
    _touch_old(old, "PR-XMA")                                 # e o piloto seguiu usando o GolOps antigo
    r = legacy.prepare(new, [])
    assert r["action"] == "recopiado" and os.path.isdir(r["kept"])
    careers.bootstrap(new)
    assert _regs(new) == {"PR-GUZ", "PR-XMA"}
    assert legacy.prepare(new, []) == {"action": None}        # nada mudou desde a nova cópia


def test_copy_is_kept_once_lineops_was_used(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "_base", lambda: tmp_path)
    old = _old_dir(tmp_path)
    new = tmp_path / "LineOps" / "dados"
    legacy.prepare(new, [])
    careers.bootstrap(new)
    core = db.connect(careers.core_path(new))
    legacy.mark_used(core)
    core.close()
    _touch_old(old, "PR-XMA")
    r = legacy.prepare(new, [])
    assert r["action"] is None and r["conflict"] == str(old)
    assert _regs(new) == {"PR-GUZ"}


def test_copy_made_by_200_is_understood(tmp_path, monkeypatch):
    """A 2.0.0 só deixava o COPIADO_DE.txt; um login depois da cópia conta como uso."""
    monkeypatch.setattr(paths, "_base", lambda: tmp_path)
    old = _old_dir(tmp_path)
    new = tmp_path / "LineOps" / "dados"
    assert paths.adopt_legacy(new, []) == old                 # o que a 2.0.0 fazia
    careers.bootstrap(new)
    a = legacy.adoption(new)
    assert a["src"] == str(old)
    assert not legacy.lineops_used(new, a["at"])
    core = db.connect(careers.core_path(new))
    core.execute("INSERT INTO auth_token(user_id, kind, token_hash, created_at, career) VALUES (1,'session','novo','2099-01-01T00:00:00Z','GLO')")
    core.commit()
    core.close()
    assert legacy.lineops_used(new, a["at"])


def test_index_assets_carry_the_version():
    """Cada versão muda a URL dos scripts: o navegador nunca junta tela nova com script velho."""
    from pathlib import Path
    from lineops import __version__
    html = (Path(legacy.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
    for a in ("css/app.css", "js/ui.js", "js/app.js"):
        assert f'"{a}?v={__version__}"' in html


def test_app_never_shadows_its_modules():
    """A 2.0.1 quebrou o login: uma variável local 'legacy' dentro do create_app escondia o módulo legacy."""
    import ast
    from pathlib import Path
    src = (Path(legacy.__file__).parent / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    mods = set()
    for n in tree.body:
        if isinstance(n, ast.ImportFrom) and n.level == 1 and n.module is None:
            mods |= {a.asname or a.name for a in n.names}
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "create_app")
    bad = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id in mods:
            bad.add(n.id)
        if isinstance(n, ast.arg) and n.arg in mods:
            bad.add(n.arg)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn and n.name in mods:
            bad.add(n.name)
    assert not bad, f"nomes de módulo reaproveitados como variável no create_app: {sorted(bad)}"


def test_conflict_explains_and_manual_recopy_works(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "_base", lambda: tmp_path)
    old = _old_dir(tmp_path)
    new = tmp_path / "LineOps" / "dados"
    legacy.prepare(new, [])
    careers.bootstrap(new)
    core = db.connect(careers.core_path(new))
    legacy.mark_used(core)
    core.close()
    _touch_old(old, "PR-XMA")
    r = legacy.prepare(new, [])
    assert r["conflict"] == str(old) and "login" in r["why"] and r["copied_at"] and r["old_changed_at"]
    r = legacy.force_recopy(new)                                # o piloto escolheu trazer os dados do GolOps
    assert r["action"] == "recopiado" and os.path.isdir(r["kept"])
    careers.bootstrap(new)
    assert _regs(new) == {"PR-GUZ", "PR-XMA"}
    assert legacy.prepare(new, []) == {"action": None}
