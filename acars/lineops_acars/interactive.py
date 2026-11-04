"""Modo guiado do ACARS (o que o iniciar-acars.bat fazia), para o .exe aberto com dois cliques.

Pergunta servidor, token e simulador, guarda em acars.cfg (na pasta do programa ou em %APPDATA%\\LineOps ACARS),
lista os voos e inicia. O token fica só neste computador.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

SIMS = {"1": ("xplane", "X-Plane 12"), "2": ("msfs", "MSFS 2024"), "3": ("p3d", "Prepar3D v5")}


def cfg_path() -> Path:
    here = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    local = here / "acars.cfg"
    if local.exists() or os.access(here, os.W_OK):
        return local
    base = Path(os.environ.get("APPDATA") or Path.home()) / "LineOps ACARS"
    old = Path(os.environ.get("APPDATA") or Path.home()) / ("Gol" "Ops ACARS") / "acars.cfg"
    if not (base / "acars.cfg").exists() and old.exists():           # configuração do GolOps ACARS: aproveita
        base.mkdir(parents=True, exist_ok=True)
        (base / "acars.cfg").write_bytes(old.read_bytes())
    base.mkdir(parents=True, exist_ok=True)
    return base / "acars.cfg"


def load(p: Path) -> dict:
    out = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip().upper()] = v.strip()
    return out


def save(p: Path, cfg: dict):
    p.write_text("".join(f"{k}={cfg[k]}\n" for k in ("SERVER", "TOKEN", "SIM") if cfg.get(k)), encoding="utf-8")


def ask(prompt, default=""):
    v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return v or default


def configure(cfg: dict) -> dict:
    print("\n Endereço do servidor LineOps. Exemplo: http://192.168.0.10:8787")
    print(" (no PC do LineOps: ícone da bandeja → Acesso pela rede mostra o endereço)")
    cfg["SERVER"] = ask(" Servidor", cfg.get("SERVER") or "http://127.0.0.1:8787").rstrip("/")
    print("\n Token do ACARS: no LineOps, Ajustes → Acesso do ACARS → Gerar token.")
    cfg["TOKEN"] = ask(" Token", cfg.get("TOKEN", ""))
    print("\n Simulador:  1 = X-Plane 12   2 = MSFS 2024   3 = Prepar3D v5")
    cur = next((k for k, (s, _n) in SIMS.items() if s == cfg.get("SIM")), "1")
    cfg["SIM"] = SIMS.get(ask(" Escolha", cur), SIMS["1"])[0]
    return cfg


def run(main_fn) -> int:
    print("\n LineOps ACARS (simulação — sem afiliação com a GOL)\n " + "=" * 50)
    p = cfg_path()
    cfg = load(p)
    if all(cfg.get(k) for k in ("SERVER", "TOKEN", "SIM")):
        print(f" Servidor: {cfg['SERVER']}\n Simulador: {cfg['SIM']}")
        if ask(" Mudar configuração? (s/N)", "n").lower().startswith("s"):
            cfg = configure(cfg)
    else:
        cfg = configure(cfg)
    save(p, cfg)
    print(f" Configuração salva em {p} (contém o seu token: não compartilhe).\n")
    base = ["--server", cfg["SERVER"], "--token", cfg["TOKEN"], "--sim", cfg["SIM"]]
    try:
        main_fn(base + ["--list"])
    except SystemExit as e:
        if e.code not in (0, None):
            input("\n Enter para sair.")
            return int(e.code) if isinstance(e.code, int) else 1
    fid = ask("\n ID do voo (A para abortar uma sessão aberta, Enter para sair)")
    if not fid:
        return 0
    if fid.lower() == "a":
        fid = ask(" ID do voo a abortar")
        main_fn(base + ["--flight", fid, "--abort"])
        input("\n Enter para sair.")
        return 0
    print("\n Abra o simulador com o voo carregado. No X-Plane, deixe a rede liberada (UDP 49000).")
    print(" Ctrl+C sai e mantém a sessão aberta para retomar.\n")
    try:
        main_fn(base + ["--flight", fid])
    finally:
        input("\n Enter para sair.")
    return 0
