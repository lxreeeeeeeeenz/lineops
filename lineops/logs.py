"""Log em arquivo: <dados>/logs/lineops.log, girando em 5 arquivos de 1 MB. Quando alguém disser "deu erro", é aqui que olha."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup(data_dir: Path) -> Path:
    d = Path(data_dir) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "lineops.log"
    root = logging.getLogger()
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) and Path(h.baseFilename) == path for h in root.handlers):
        h = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        h.setFormatter(logging.Formatter(FMT))
        root.addHandler(h)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    return path


def tail(path: Path, lines: int = 200) -> list[str]:
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 200_000))
        data = f.read().decode("utf-8", errors="replace")
    return data.splitlines()[-lines:]
