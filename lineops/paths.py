"""Onde a LineOps guarda os dados.

No app instalado, os dados ficam fora da pasta do programa, para uma atualização ou reinstalação nunca apagar nada:
  Windows: %LOCALAPPDATA%\\LineOps\\dados      outros: ~/.local/share/LineOps/dados
LINEOPS_DATA (ou --data) sempre vence. Na primeira execução, os dados do GolOps (o nome antigo do projeto) são copiados
uma vez: a pasta %LOCALAPPDATA%\\GolOps\\dados e a pasta dados da instalação por .bat. O banco antigo vira a carreira GOL.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP = "LineOps"
LEGACY_APP = "Gol" "Ops"                       # nome antigo (a pasta de dados dele é adotada uma vez)
LEGACY_DBS = ("gol" "ops.db", "flygolv.db")


def _base() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))


def default_data_dir() -> Path:
    env = os.environ.get("LINEOPS_DATA") or os.environ.get("GOL" "OPS_DATA")
    if env:
        return Path(env)
    return _base() / APP / "dados"


def legacy_dirs() -> list[Path]:
    return [_base() / LEGACY_APP / "dados"]


def adopt_legacy(new_dir: Path, candidates: list[Path]) -> Path | None:
    """Copia (não move) uma pasta de dados antiga para o lugar novo, se o novo ainda está vazio. Devolve a origem usada."""
    if (new_dir / "lineops.db").exists() or any(((new_dir / n).exists()) for n in LEGACY_DBS):
        return None
    for c in list(candidates) + legacy_dirs():
        if any((c / n).exists() for n in LEGACY_DBS + ("lineops.db",)):
            new_dir.mkdir(parents=True, exist_ok=True)
            for item in c.iterdir():
                dest = new_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)
            (new_dir / "COPIADO_DE.txt").write_text(f"Dados copiados de {c} na primeira execução da LineOps.\n", encoding="utf-8")
            return c
    return None
