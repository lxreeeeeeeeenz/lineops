"""Senha esquecida: troca sem apagar nada, só no PC da instalação, com código no arquivo da pasta de dados."""
import pytest

from lineops import db, security, senha
from lineops.errors import FGVError


@pytest.fixture
def core(tmp_path):
    c = db.connect(tmp_path / "lineops.db")
    db.migrate(c)
    c.execute("INSERT INTO user(id, username, pw_hash, display_name, created_at) VALUES (1, 'cmdt079', ?, 'Cmdt', 'x')",
              (security.hash_password("velha-senha-1"),))
    c.commit()
    sess = security.new_token(c, 1, "session")
    api = security.new_token(c, 1, "api", "ACARS")
    c.commit()
    yield c, tmp_path, sess, api
    c.close()


def _ok(c, pw):
    return security.verify_password(pw, c.execute("SELECT pw_hash FROM user WHERE id=1").fetchone()[0])


def test_reset_keeps_data_and_closes_sessions(core):
    c, _d, sess, api = core
    with pytest.raises(FGVError):
        senha.reset_password(c, "cmdt079", "curta")
    with pytest.raises(FGVError):
        senha.reset_password(c, "ninguem", "senha-nova-123")
    r = senha.reset_password(c, "CMDT079", "senha-nova-123")                        # sem diferenciar maiúsculas
    assert r["username"] == "cmdt079" and r["sessions_closed"] == 1
    assert _ok(c, "senha-nova-123") and not _ok(c, "velha-senha-1")
    assert security.lookup_token(c, sess) is None and security.lookup_token(c, api)   # o ACARS continua


def test_local_code_flow(core, monkeypatch):
    c, d, _s, _a = core
    st = senha.start_local(c, d, open_file=False)
    f = d / senha.FILE
    assert st["file"] == str(f) and f.exists()
    code = next(ln.strip() for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip().isdigit())
    wrong = "000000" if code != "000000" else "111111"
    with pytest.raises(FGVError):
        senha.finish_local(c, d, "cmdt079", wrong, "senha-nova-123")
    assert _ok(c, "velha-senha-1")
    senha.finish_local(c, d, "cmdt079", code, "senha-nova-123")
    assert _ok(c, "senha-nova-123") and not f.exists()
    with pytest.raises(FGVError):                                                    # o código não serve duas vezes
        senha.finish_local(c, d, "cmdt079", code, "outra-senha-999")
    senha.start_local(c, d, open_file=False)
    for _ in range(senha.MAX_TRIES):
        with pytest.raises(FGVError):
            senha.finish_local(c, d, "cmdt079", wrong, "x" * 9)
    code2 = next(ln.strip() for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip().isdigit())
    with pytest.raises(FGVError):                                                    # 5 erros: até o certo deixa de valer
        senha.finish_local(c, d, "cmdt079", code2, "senha-nova-456")
    assert _ok(c, "senha-nova-123")


def test_only_local_hosts():
    assert senha.is_local("127.0.0.1") and senha.is_local("::1")
    assert not senha.is_local("192.168.0.20") and not senha.is_local(None)
