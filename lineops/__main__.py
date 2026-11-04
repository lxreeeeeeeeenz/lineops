"""CLI: python -m lineops serve|backup|restore|import-aeroscale|create-user"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from . import backup, security
from .db import connect, migrate, now_iso, tx


def _open_when_up(port: int) -> None:
    """Abre o navegador só quando quem responde na porta é a LineOps (nada de abrir a tela de outro programa)."""
    import threading
    import webbrowser

    def run():
        from . import legacy
        import time
        for _ in range(120):
            if legacy.probe(port, timeout=1) == "lineops":
                webbrowser.open(f"http://127.0.0.1:{port}/")
                return
            time.sleep(0.5)
    threading.Thread(target=run, daemon=True, name="abrir-navegador").start()


def _restore(a, data: Path) -> int:
    from . import legacy
    if legacy.probe(a.port) == "lineops":
        print("A LineOps está aberta. Feche-a (Ctrl+C na janela do servidor, ou Sair na bandeja) e rode de novo.\n"
              "Ou restaure pela tela: Ajustes → Backups → Restaurar (vale na próxima vez que abrir).", file=sys.stderr)
        return 1
    z = Path(a.zip) if a.zip else None
    if z is None:
        items = backup.list_backups(data / "backups")
        if not items:
            print("Nenhum backup em " + str(data / "backups"), file=sys.stderr)
            return 1
        z = data / "backups" / items[0]["name"]
    try:
        mf = backup.validate(z)
    except RuntimeError as e:
        print(f"Não deu: {e}", file=sys.stderr)
        return 1
    print(f"Restaurar {z.name} (LineOps {mf.get('version') or '?'}, de {mf.get('created_at') or '?'}).\n"
          "Antes, o estado atual vira um backup 'antes-de-restaurar', então dá para voltar atrás.")
    if not a.sim and input("Continuar? (s/n) ").strip().lower() not in ("s", "sim"):
        print("Nada foi mudado.")
        return 0
    safety = backup.make_backup(data, label="antes-de-restaurar")
    for f in backup.restore(z, data):
        print(f"Restaurado: {f}")
    print(f"Pronto. O estado anterior ficou em {safety}.")
    return 0


def _recopy(a) -> int:
    from . import legacy, paths
    data = Path(a.data) if a.data else paths.default_data_dir()
    kind = legacy.probe(a.port)
    if kind == "lineops":
        print("A LineOps está aberta. Feche-a (Ctrl+C na janela do servidor, ou Sair na bandeja) e rode de novo.", file=sys.stderr)
        return 1
    if kind == "golops" and not legacy.stop_old(a.port):
        print("O GolOps antigo está aberto e não deu para fechá-lo. Saia dele pela bandeja e rode de novo.", file=sys.stderr)
        return 1
    print(f"Isto traz de novo os dados do GolOps para a LineOps.\n"
          f"A pasta atual ({data}) NÃO é apagada: vai inteira para uma pasta dados-substituidos-... ao lado.\n"
          f"Tudo o que foi feito só na LineOps depois da cópia fica nessa pasta guardada.")
    if not a.sim and input("Continuar? (s/n) ").strip().lower() not in ("s", "sim"):
        print("Nada foi mudado.")
        return 0
    try:
        r = legacy.force_recopy(data)
    except (OSError, FileNotFoundError) as e:
        print(f"Não deu: {e}\nNada foi mudado (feche a LineOps e o GolOps e tente de novo).", file=sys.stderr)
        return 1
    print(f"Pronto: dados do GolOps trazidos de {r['src']}." + (f" A pasta anterior ficou em {r['kept']}." if r.get("kept") else ""))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lineops", description="LineOps — virtual airline pessoal (projeto independente, sem afiliação com companhias aéreas).")
    ap.add_argument("--data", default=None, help="pasta de dados (padrão: $LINEOPS_DATA ou %%LOCALAPPDATA%%\\LineOps\\dados, a mesma do app)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="inicia o servidor")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--lan", action="store_true", help="escuta na rede local (0.0.0.0). Use senha forte!")
    s.add_argument("--open", action="store_true", help="abre o navegador quando o servidor estiver no ar")
    sub.add_parser("backup", help="cria um backup agora")
    r = sub.add_parser("restore", help="restaura um backup (servidor parado); sem arquivo, o mais recente")
    r.add_argument("zip", nargs="?")
    r.add_argument("--port", type=int, default=8787)
    r.add_argument("--sim", action="store_true", help="não pergunta")
    i = sub.add_parser("import-aeroscale", help="importa um backup do AeroScale (.zip/.db)")
    i.add_argument("path")
    i.add_argument("--dry-run", action="store_true")
    sub.add_parser("create-user", help="cria o usuário inicial pela linha de comando")
    rs = sub.add_parser("redefinir-senha", help="troca a senha esquecida de um usuário (nada é apagado)")
    rs.add_argument("--usuario", default=None)
    rc = sub.add_parser("recopiar-golops", help="traz de novo os dados do GolOps antigo (a pasta atual da LineOps é guardada, não apagada)")
    rc.add_argument("--port", type=int, default=8787)
    rc.add_argument("--sim", action="store_true", help="não pergunta")
    dk = sub.add_parser("desktop", help="abre como app (janela própria + bandeja)")
    dk.add_argument("--port", type=int, default=8787)
    dk.add_argument("--lan", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "desktop":
        from . import paths
        from .desktop import main as desktop_main
        data = Path(a.data) if a.data else paths.default_data_dir()
        return desktop_main(["--data", str(data), "--port", str(a.port)] + (["--lan"] if a.lan else []))
    if a.cmd == "serve":                       # antes de copiar qualquer dado: o GolOps antigo não pode estar aberto
        from . import legacy
        if legacy.remove_old_autostart():
            print("O GolOps antigo não inicia mais com o Windows (ele ocupava a porta da LineOps).", file=sys.stderr)
        try:
            st = legacy.before_start(a.port)
        except legacy.PortBusy as e:
            print("\n" + str(e) + "\n", file=sys.stderr)
            return 1
        if st == "lineops":
            print(f"A LineOps já está aberta em http://127.0.0.1:{a.port}/ (outra janela).", file=sys.stderr)
            if a.open:
                import webbrowser
                webbrowser.open(f"http://127.0.0.1:{a.port}/")
            return 0
    if a.cmd == "recopiar-golops":
        return _recopy(a)
    if a.data is None:                         # todas as versões usam a mesma pasta; a antiga "dados" ao lado é copiada uma vez
        from . import legacy, paths
        data = paths.default_data_dir()
        here = Path(__file__).resolve().parents[1]
        r = legacy.prepare(data, [Path.cwd() / "dados", here / "dados", Path.cwd() / "data"])
        if r.get("action") == "copiado":
            print(f"Dados do GolOps copiados de {r['src']} para {data}.", file=sys.stderr)
        elif r.get("action") == "recopiado":
            print(f"A cópia dos dados do GolOps foi refeita com os dados mais novos de {r['src']} "
                  f"(a anterior, que não chegou a ser usada, ficou em {r['kept']}).", file=sys.stderr)
        elif r.get("conflict"):
            print(f"\nAviso: os dados do GolOps mudaram depois da cópia para a LineOps (cópia em {r.get('copied_at')}, "
                  f"GolOps mexido em {r.get('old_changed_at')}), mas a LineOps também foi usada ({r.get('why')}).\n"
                  f"Nada foi trocado sozinho. Se você registrou voos no GolOps depois da cópia e quer trazê-los,\n"
                  f"feche esta janela e rode recopiar-golops.bat (a pasta atual da LineOps é guardada, não apagada).\n",
                  file=sys.stderr)
    else:
        data = Path(a.data)
        from .desktop import main as desktop_main
        return desktop_main(["--data", str(data), "--port", str(a.port)] + (["--lan"] if a.lan else []))
    data.mkdir(parents=True, exist_ok=True)
    from . import careers
    careers.bootstrap(data)
    db_path = careers.core_path(data)                       # núcleo (piloto); cada companhia tem o seu banco em carreiras/

    if a.cmd == "serve":
        import uvicorn
        from .app import create_app
        host = "0.0.0.0" if a.lan else a.host
        if a.lan:
            print("⚠ Modo LAN: o LineOps ficará acessível na sua rede local. Sem HTTPS — não exponha à internet.", file=sys.stderr)
        app = create_app(data)
        if a.open:
            _open_when_up(a.port)
        uvicorn.run(app, host=host, port=a.port, log_level="info")
    elif a.cmd == "backup":
        c = connect(db_path); migrate(c); c.close()
        print(backup.make_backup(data))
    elif a.cmd == "restore":
        return _restore(a, data)
    elif a.cmd == "import-aeroscale":
        from .aeroscale_import import import_backup
        core = connect(db_path); migrate(core)
        careers.ensure_career(data, core, careers.DEFAULT)
        c = connect(careers.career_path(data, careers.DEFAULT))
        st = import_backup(c, a.path, dry_run=a.dry_run, airline=careers.DEFAULT)
        for k, v in st.items():
            print(f"{k}: {v}")
    elif a.cmd == "redefinir-senha":
        from . import senha
        c = connect(db_path); migrate(c)
        users = senha.usernames(c)
        if not users:
            sys.exit("Ainda não há usuário nesta instalação: abra a LineOps e crie no primeiro acesso.")
        u = a.usuario or (users[0] if len(users) == 1 else None)
        if not u:
            print("Usuários desta instalação: " + ", ".join(users))
            u = input("Qual usuário? ").strip()
        print(f"Trocando a senha de {u} (dados em {data}).")
        p1 = getpass.getpass("Senha nova (mín. 8): ")
        p2 = getpass.getpass("Repita a senha nova: ")
        if p1 != p2:
            sys.exit("As senhas não conferem. Nada mudou.")
        try:
            r = senha.reset_password(c, u, p1, via="cli")
        except Exception as e:                                      # noqa: BLE001
            sys.exit(getattr(e, "message", str(e)))
        print(f"Pronto: senha de {r['username']} trocada. Sessões abertas encerradas: {r['sessions_closed']}. Entre com a senha nova.")
    elif a.cmd == "create-user":
        c = connect(db_path); migrate(c)
        if c.execute("SELECT COUNT(*) FROM user").fetchone()[0]:
            sys.exit("Já existe usuário.")
        u = input("Usuário: ").strip()
        p = getpass.getpass("Senha (mín. 8): ")
        if len(p) < 8:
            sys.exit("Senha curta demais.")
        with tx(c):
            c.execute("INSERT INTO user(username, pw_hash, display_name, created_at) VALUES (?,?,?,?)", (u, security.hash_password(p), u, now_iso()))
        print("Usuário criado.")


if __name__ == "__main__":
    sys.exit(main() or 0)
