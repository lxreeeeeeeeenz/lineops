"""Convivência com o GolOps (o nome antigo do projeto) na hora de abrir a LineOps.

O problema que isto resolve: o GolOps instalado inicia com o Windows e fica na bandeja ocupando a porta 8787. A LineOps
abria, não conseguia usar a porta, e o navegador mostrava o servidor que já estava lá: o GolOps antigo.

Agora, antes de subir o servidor:
  1. pergunta quem está na porta (/api/health diz o nome do app);
  2. se for o GolOps antigo, fecha ele (no Windows: o processo que escuta a porta e qualquer GolOps.exe), e a entrada
     "iniciar com o Windows" dele vira a da LineOps;
  3. só então copia os dados. Se a cópia já tinha sido feita com o GolOps aberto (a 2.0.0 fazia isso) e a LineOps nunca
     chegou a ser usada, a cópia é refeita com os dados mais novos do GolOps. O que havia na pasta da LineOps vai inteiro
     para uma pasta ao lado ("dados-substituidos-…"), nada é apagado.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import paths

log = logging.getLogger("lineops.legacy")
OLD_NAME = "Gol" "Ops"
OLD_EXES = ("Gol" "Ops.exe",)
MARK = ".adocao.json"                       # de onde e quando os dados antigos foram copiados
USED_KEY = "lineops_used_at"                # gravado no primeiro uso de verdade da LineOps (login/sessão)
_NOWIN = 0x08000000 if sys.platform == "win32" else 0      # CREATE_NO_WINDOW


# ------------------------------------------------------------------ quem está na porta
def probe(port: int, timeout: float = 1.5) -> str | None:
    """'lineops', 'golops' (o antigo), 'outro' (algo responde mas não é nosso) ou None (porta livre/ninguém responde)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError:
        return "outro"
    except Exception:
        return None if port_free(port) else "outro"
    try:
        app = str(json.loads(body).get("app") or "")
    except Exception:
        return "outro"
    return {"lineops": "lineops", OLD_NAME.lower(): "golops"}.get(app.lower(), "outro")


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _run(cmd: list[str], timeout: float = 10) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=_NOWIN)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:                                       # noqa: BLE001
        return f"erro: {e}"


def listening_pids(port: int, netstat_out: str | None = None) -> set[int]:
    """PIDs que escutam a porta (saída do netstat -ano do Windows)."""
    out = netstat_out if netstat_out is not None else _run(["netstat", "-ano", "-p", "TCP"])
    pids = set()
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 5 and p[0].upper() == "TCP" and p[1].rsplit(":", 1)[-1] == str(port) and (p[2] in ("0.0.0.0:0", "[::]:0", "*:*") or p[3].upper() in ("LISTENING", "OUVINDO", "ESCUTANDO")):   # o estado vem traduzido
            try:
                pids.add(int(p[4]))
            except ValueError:
                pass
    return pids


def stop_old(port: int, wait: float = 8.0) -> bool:
    """Fecha o GolOps antigo que está na porta. Só age se /api/health confirmar que é ele. True = porta liberada."""
    if probe(port) != "golops":
        return port_free(port)
    if sys.platform != "win32":
        log.warning("o %s antigo está na porta %s; feche-o (ícone da bandeja → Sair) e abra de novo", OLD_NAME, port)
        return False
    for pid in listening_pids(port):
        log.info("fechando o %s antigo (PID %s) que ocupava a porta %s", OLD_NAME, pid, port)
        _run(["taskkill", "/PID", str(pid), "/T", "/F"])
    for exe in OLD_EXES:                     # a bandeja do instalado, mesmo se estiver em outra porta
        _run(["taskkill", "/IM", exe, "/T", "/F"])
    t = time.time() + wait
    while time.time() < t:
        if port_free(port):
            time.sleep(0.5)                  # o Windows solta os arquivos um instante depois do processo
            return True
        time.sleep(0.3)
    return port_free(port)


def remove_old_autostart() -> bool:
    """Tira o "iniciar com o Windows" do GolOps (senão ele volta a ocupar a porta no próximo boot). True se havia."""
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_ALL_ACCESS) as k:
            try:
                winreg.QueryValueEx(k, OLD_NAME)
            except OSError:
                return False
            winreg.DeleteValue(k, OLD_NAME)
            return True
    except OSError:
        return False


# ------------------------------------------------------------------ cópia dos dados antigos
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mtime(p: Path) -> float:
    best = 0.0
    for x in (p, Path(str(p) + "-wal")):
        try:
            best = max(best, x.stat().st_mtime)
        except OSError:
            pass
    return best


def _legacy_db(d: Path) -> Path | None:
    return next((d / n for n in paths.LEGACY_DBS if (d / n).exists()), None)


def adoption(new_dir: Path) -> dict | None:
    """De onde e quando os dados foram copiados (a 2.0.0 só deixava o COPIADO_DE.txt: lido dele)."""
    m = new_dir / MARK
    if m.exists():
        try:
            return json.loads(m.read_text(encoding="utf-8"))
        except Exception:                                       # noqa: BLE001
            return None
    t = new_dir / "COPIADO_DE.txt"
    if t.exists():
        txt = t.read_text(encoding="utf-8", errors="replace")
        src = txt.split("copiados de ", 1)[-1].split(" na primeira", 1)[0].strip()
        return {"src": src, "at": t.stat().st_mtime}
    return None


def lineops_used(new_dir: Path, since: float) -> str | None:
    """A LineOps foi usada de verdade depois da cópia? Devolve o motivo (para o aviso) ou None. Na dúvida, diz que sim."""
    from . import careers
    since_iso = datetime.fromtimestamp(since + 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    files = [careers.core_path(new_dir), careers.career_path(new_dir, careers.DEFAULT)]
    for f in files:
        if not f.exists():
            continue
        try:
            c = sqlite3.connect(str(f), timeout=5)                  # só leitura de fato; ninguém mais tem o banco aberto aqui
            try:
                tabs = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "setting" in tabs:
                    r = c.execute("SELECT value FROM setting WHERE key=?", (USED_KEY,)).fetchone()
                    if r:
                        return f"login na LineOps em {r[0]}"
                if "auth_token" in tabs:
                    r = c.execute("SELECT MIN(created_at) FROM auth_token WHERE created_at > ?", (since_iso,)).fetchone()
                    if r and r[0]:
                        return f"login na LineOps em {r[0]}"
                if "audit_log" in tabs:
                    r = c.execute("SELECT MIN(ts) FROM audit_log WHERE user_id IS NOT NULL AND ts > ?", (since_iso,)).fetchone()
                    if r and r[0]:
                        return f"ação de piloto na LineOps em {r[0]}"
            finally:
                c.close()
        except sqlite3.Error as e:
            return f"não deu para conferir ({e})"
    return None


def _copy_dir(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in ("logs",):
            continue
        t = dest / item.name
        if item.is_dir():
            shutil.copytree(item, t, dirs_exist_ok=True)
        else:
            shutil.copy2(item, t)


def _write_mark(new_dir: Path, src: Path) -> None:
    odb = _legacy_db(src)
    (new_dir / MARK).write_text(json.dumps({"src": str(src), "at": time.time(), "src_mtime": _mtime(odb) if odb else 0}),
                                encoding="utf-8")
    (new_dir / "COPIADO_DE.txt").write_text(f"Dados copiados de {src} na primeira execução da LineOps.\n", encoding="utf-8")


def prepare(new_dir: Path, candidates: list[Path]) -> dict:
    """Copia os dados do GolOps (primeira vez) ou refaz a cópia se ela ficou velha sem a LineOps ter sido usada.
    Chamar com o GolOps antigo já fechado e antes de abrir qualquer banco. Devolve {'action': None|'copiado'|'recopiado', ...}."""
    new_dir = Path(new_dir)
    src = paths.adopt_legacy(new_dir, candidates)
    if src:
        _write_mark(new_dir, src)
        return {"action": "copiado", "src": str(src)}
    a = adoption(new_dir)
    if not a or not a.get("src"):
        return {"action": None}
    old = Path(a["src"])
    odb = _legacy_db(old)
    at = float(a.get("at") or 0)
    ref = float(a["src_mtime"]) + 1 if a.get("src_mtime") else at + 5   # a 2.0.0 não guardava a data do banco copiado
    if not odb or _mtime(odb) <= ref:
        return {"action": None}                                 # o GolOps não mexeu em nada depois da cópia
    why = lineops_used(new_dir, at)
    if why:
        info = {"action": None, "conflict": str(old), "copied_at": _local(at), "old_changed_at": _local(_mtime(odb)), "why": why}
        log.warning("o %s antigo mudou depois da cópia (cópia %s, %s mexido em %s), mas a LineOps também foi usada (%s): "
                    "nada é trocado sozinho. Para trazer os dados dele de novo: recopiar-golops.bat", OLD_NAME,
                    info["copied_at"], OLD_NAME, info["old_changed_at"], why)
        return info
    try:
        keep = _swap_in(new_dir, old)
    except OSError as e:                                        # algum arquivo preso (Windows): melhor não mexer
        log.warning("não foi possível refazer a cópia dos dados do %s (%s); nada foi trocado", OLD_NAME, e)
        return {"action": None, "conflict": str(old), "why": str(e)}
    log.info("cópia dos dados do %s refeita (a anterior estava velha e a LineOps não tinha sido usada); a anterior ficou em %s",
             OLD_NAME, keep)
    return {"action": "recopiado", "src": str(old), "kept": str(keep)}


def _local(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d/%m %H:%M") if ts else "?"


def _swap_in(new_dir: Path, old: Path) -> Path:
    """A pasta da LineOps vai inteira para dados-substituidos-… e os dados do GolOps entram no lugar. Devolve onde ficou."""
    keep = new_dir.parent / f"dados-substituidos-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    n = 2
    while keep.exists():
        keep = keep.with_name(f"{keep.name.split('~')[0]}~{n}")
        n += 1
    shutil.move(str(new_dir), str(keep))
    _copy_dir(old, new_dir)
    _write_mark(new_dir, old)
    return keep


def force_recopy(new_dir: Path) -> dict:
    """Trazer os dados do GolOps de novo, por escolha do piloto (recopiar-golops.bat). A LineOps e o GolOps fechados."""
    new_dir = Path(new_dir)
    a = adoption(new_dir) or {}
    old = Path(a["src"]) if a.get("src") else next((d for d in paths.legacy_dirs() if _legacy_db(d)), None)
    if not old or not _legacy_db(old):
        raise FileNotFoundError("Não achei os dados do GolOps (%LOCALAPPDATA%\\GolOps\\dados).")
    if not new_dir.exists():
        _copy_dir(old, new_dir)
        _write_mark(new_dir, old)
        return {"action": "copiado", "src": str(old)}
    keep = _swap_in(new_dir, old)
    return {"action": "recopiado", "src": str(old), "kept": str(keep)}


def mark_used(conn) -> None:
    """Primeiro uso de verdade da LineOps (chamado na autenticação). Daqui em diante a cópia nunca é refeita sozinha."""
    try:
        conn.execute("INSERT OR IGNORE INTO setting(key, value) VALUES (?, ?)", (USED_KEY, _now().strftime("%Y-%m-%dT%H:%M:%SZ")))
        conn.commit()
    except sqlite3.Error:
        pass


# ------------------------------------------------------------------ o passo único antes de subir o servidor
class PortBusy(Exception):
    pass


def before_start(port: int) -> str:
    """'livre' (pode subir), 'lineops' (já está aberta: só mostrar). Lança PortBusy com a explicação para o piloto."""
    kind = probe(port)
    if kind == "lineops":
        return "lineops"
    if kind == "golops":
        if stop_old(port):
            log.info("o %s antigo foi fechado; a LineOps assume a porta %s", OLD_NAME, port)
            return "livre"
        raise PortBusy(f"O {OLD_NAME} antigo continua aberto na porta {port} e não deu para fechá-lo daqui.\n\n"
                       f"Clique com o botão direito no ícone dele perto do relógio → \"Sair do {OLD_NAME}\" "
                       f"(ou finalize {OLD_EXES[0]} no Gerenciador de Tarefas) e abra a LineOps de novo.")
    if not port_free(port):
        raise PortBusy(f"A porta {port} está ocupada por outro programa. Feche-o ou abra a LineOps em outra porta "
                       f"(LINEOPS_PORT=8788 no iniciar.bat, ou --port 8788).")
    return "livre"
