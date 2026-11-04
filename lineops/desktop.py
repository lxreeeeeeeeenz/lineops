"""LineOps como aplicativo do Windows: janela própria, ícone na bandeja, servidor embutido.

O LineOps continua sendo um servidor local com interface web (é isso que deixa o celular e o PC do simulador acessarem).
O app só esconde isso:
* sobe o servidor num processo só, em segundo plano;
* abre uma janela própria (pywebview, motor do Edge/WebView2 que já vem no Windows 10 e 11);
* fica na bandeja do relógio: fechar a janela não para o LineOps (o laço da IVAO e a equipe seguem trabalhando);
* um segundo clique no atalho só traz a janela de volta (instância única);
* "Acesso pela rede" religa o servidor em 0.0.0.0 e mostra o endereço para o PC do simulador e o celular;
* avisa na bandeja quando chega mensagem nova da operação.
Sem pywebview, abre no navegador; sem pystray, roda sem bandeja. Nada disso muda os dados.
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sqlite3
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import __version__, paths

log = logging.getLogger("lineops.desktop")
DEFAULT_PORT = 8787
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def lan_ip() -> str | None:
    """IP desta máquina na rede local (o truque do UDP não envia nada)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None
    finally:
        s.close()


def already_running(port: int, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as r:
            return b'"LineOps"' in r.read()
    except Exception:
        return False


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class Server:
    """uvicorn numa thread, com religar em outro endereço (local ↔ rede)."""

    def __init__(self, app, port: int):
        self.app, self.port, self.host = app, port, "127.0.0.1"
        self.srv = None
        self.thread = None

    def start(self, host: str | None = None):
        import uvicorn
        self.host = host or self.host
        cfg = uvicorn.Config(self.app, host=self.host, port=self.port, log_config=None, access_log=False)
        self.srv = uvicorn.Server(cfg)
        self.srv.install_signal_handlers = lambda: None          # numa thread, sem capturar Ctrl+C
        self.thread = threading.Thread(target=self.srv.run, name="lineops-server", daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.srv.started:
                break
            time.sleep(0.05)
        log.info("servidor em %s:%s", self.host, self.port)

    def stop(self):
        if self.srv:
            self.srv.should_exit = True
            if self.thread:
                self.thread.join(timeout=8)
        self.srv = None

    def restart(self, host: str):
        self.stop()
        self.start(host)

    @property
    def lan(self) -> bool:
        return self.host == "0.0.0.0"


# ------------------------------------------------------------------ iniciar com o Windows
LEGACY_RUN = "Gol" "Ops"


def migrate_autostart() -> bool:
    """Quem tinha o GolOps iniciando com o Windows continua com a LineOps (e a entrada antiga sai)."""
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_ALL_ACCESS) as k:
            try:
                winreg.QueryValueEx(k, LEGACY_RUN)
            except OSError:
                return False
            winreg.DeleteValue(k, LEGACY_RUN)
        set_autostart(True)
        return True
    except OSError:
        return False


def autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "LineOps")
            return True
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    if sys.platform != "win32":
        return
    import winreg
    exe = sys.executable if getattr(sys, "frozen", False) else f'"{sys.executable}" -m lineops.desktop'
    cmd = f'"{exe}" --hidden' if getattr(sys, "frozen", False) else f"{exe} --hidden"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, "LineOps", 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, "LineOps")
            except OSError:
                pass


def open_path(p: Path):
    if sys.platform == "win32":
        os.startfile(str(p))                                    # noqa: S606 (abre a pasta no Explorer)
    else:
        webbrowser.open(p.as_uri())


def icon_image():
    """Ícone da bandeja desenhado na hora (mesmo desenho do logo), sem depender de arquivo."""
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(23, 25, 29, 255), outline=(38, 40, 46, 255), width=2)
    d.polygon([(10, 43), (52, 24), (37.6, 51), (31.4, 40.6)], fill=(255, 106, 19, 255))
    return im


# ------------------------------------------------------------------ avisos de mensagem nova
class Notifier(threading.Thread):
    """Lê os bancos das carreiras a cada 30 s; quando chega mensagem nova da equipe, mostra na bandeja."""

    def __init__(self, data_dir: Path, notify):
        super().__init__(name="lineops-notify-tray", daemon=True)
        self.data_dir, self.notify, self.last = Path(data_dir), notify, {}
        self.stop_ev = threading.Event()

    def run(self):
        from . import careers
        while not self.stop_ev.wait(30):
            files = careers.career_files(self.data_dir)
            for f in files:
                try:
                    c = sqlite3.connect(f"file:{f}?mode=ro", uri=True, timeout=5)
                    try:
                        last = self.last.get(f.stem)
                        rows = c.execute("SELECT id, sender, subject, channel FROM ops_message WHERE id > ? ORDER BY id",
                                         (last or 0,)).fetchall() if last is not None else []
                        top = c.execute("SELECT COALESCE(MAX(id),0) FROM ops_message").fetchone()[0]
                    finally:
                        c.close()
                    tag = f"[{careers.PROFILES.get(f.stem, {}).get('name', f.stem)}] " if len(files) > 1 else ""
                    for _id, sender, subject, channel in rows[-3:]:
                        self.notify(tag + (f"ACARS · {sender}" if channel == "acars" else f"⏰ {sender}" if channel == "alarme" else f"{sender}"), subject)
                    self.last[f.stem] = top
                except Exception:
                    pass


# ------------------------------------------------------------------ entrada
def main(argv=None):
    ap = argparse.ArgumentParser(prog="LineOps", description="LineOps — app do Windows")
    ap.add_argument("--data", default=None, help="pasta de dados (padrão: %LOCALAPPDATA%\\LineOps\\dados)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("LINEOPS_PORT") or DEFAULT_PORT))
    ap.add_argument("--lan", action="store_true", help="já começa com acesso pela rede local")
    ap.add_argument("--hidden", action="store_true", help="começa só na bandeja (usado ao iniciar com o Windows)")
    ap.add_argument("--browser", action="store_true", help="abre no navegador em vez da janela própria")
    a = ap.parse_args(argv)

    url = f"http://127.0.0.1:{a.port}/"
    from . import legacy
    migrate_autostart()                              # antes de tudo: o GolOps antigo não volta no próximo boot
    try:
        state0 = legacy.before_start(a.port)         # o GolOps antigo na porta é fechado aqui
    except legacy.PortBusy as e:
        _fatal(str(e))
        return 1
    if state0 == "lineops":                          # instância única: só mostra o que já está aberto
        webbrowser.open(url) if a.browser or not _has_webview() else _window_only(url)
        return 0

    data = Path(a.data) if a.data else paths.default_data_dir()
    here = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    adopt = legacy.prepare(data, [here / "dados", Path.cwd() / "dados"])
    os.environ["LINEOPS_DESKTOP"] = "1"
    from . import logs
    logs.setup(data)
    if adopt.get("action"):
        log.info("dados do GolOps %s de %s", adopt["action"], adopt.get("src"))
    from .app import create_app
    app = create_app(data)
    server = Server(app, a.port)
    server.start("0.0.0.0" if a.lan else "127.0.0.1")

    state = {"window": None, "quitting": False, "tray": None}

    def show():
        w = state["window"]
        if w is not None:
            try:
                w.show()
                w.restore()
                return
            except Exception:
                pass
        webbrowser.open(url)

    def quit_all(*_):
        state["quitting"] = True
        try:
            if state["tray"]:
                state["tray"].stop()
        except Exception:
            pass
        server.stop()
        if state["window"] is not None:
            try:
                state["window"].destroy()
            except Exception:
                pass
        os._exit(0)

    def notify(title, msg):
        t = state["tray"]
        if t is not None:
            try:
                t.notify(msg, title)
            except Exception:
                pass

    tray = _make_tray(server, data, url, show, quit_all)
    state["tray"] = tray
    if tray is not None:
        tray.run_detached()
        Notifier(data, notify).start()

    if _has_webview() and not a.browser:
        import webview
        w = webview.create_window(f"LineOps {__version__}", url, width=1380, height=900, min_size=(380, 600),
                                  background_color="#09090B", hidden=a.hidden)
        state["window"] = w

        def on_closing():
            if state["quitting"] or tray is None:
                return True
            w.hide()                                   # fechar a janela manda para a bandeja
            notify("LineOps continua rodando", "Está na bandeja, perto do relógio. Sair pelo menu do ícone.")
            return False
        w.events.closing += on_closing
        webview.start(private_mode=False, storage_path=str(data / "webview"))
        if tray is None:
            quit_all()
    else:
        if not a.hidden:
            webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            quit_all()
    return 0


def _has_webview() -> bool:
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        return False


def _window_only(url):
    """Segunda abertura do atalho: abre outra janela apontando para o LineOps que já roda."""
    try:
        import webview
        webview.create_window("LineOps", url, width=1380, height=900, background_color="#09090B")
        webview.start()
    except Exception:
        webbrowser.open(url)


def _fatal(msg):
    log.error(msg)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "LineOps", 0x10)
            return
        except Exception:
            pass
    print(msg, file=sys.stderr)


def _make_tray(server, data, url, show, quit_all):
    try:
        import pystray
    except Exception:
        return None
    from . import backup

    def lan_label(_item=None):
        ip = lan_ip()
        return f"Acesso pela rede ({ip}:{server.port})" if ip else "Acesso pela rede"

    def toggle_lan(icon, _item):
        server.restart("127.0.0.1" if server.lan else "0.0.0.0")
        ip = lan_ip()
        icon.notify(f"No PC do simulador e no celular: http://{ip}:{server.port}" if server.lan and ip else
                    "Acesso só por este computador.", "Acesso pela rede " + ("ligado" if server.lan else "desligado"))

    def do_backup(icon, _item):
        try:
            p = backup.make_backup(data)
            icon.notify(f"{p.name} em {p.parent}", "Backup criado")
        except Exception as e:
            icon.notify(str(e), "Backup falhou")

    def toggle_auto(_icon, _item):
        set_autostart(not autostart_enabled())

    menu = pystray.Menu(
        pystray.MenuItem("Abrir o LineOps", lambda *_: show(), default=True),
        pystray.MenuItem("Abrir no navegador", lambda *_: webbrowser.open(url)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lan_label, toggle_lan, checked=lambda _i: server.lan),
        pystray.MenuItem("Iniciar com o Windows", toggle_auto, checked=lambda _i: autostart_enabled(), visible=sys.platform == "win32"),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Fazer backup agora", do_backup),
        pystray.MenuItem("Abrir a pasta de dados", lambda *_: open_path(data)),
        pystray.MenuItem("Abrir o log", lambda *_: open_path(data / "logs")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Sair do LineOps", quit_all),
    )
    try:
        return pystray.Icon("LineOps", icon_image(), f"LineOps {__version__}", menu)
    except Exception:
        log.exception("bandeja indisponível")
        return None


if __name__ == "__main__":
    sys.exit(main())
