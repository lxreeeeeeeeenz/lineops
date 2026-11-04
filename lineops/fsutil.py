"""Arquivos no Windows: lá não dá para apagar ou substituir um arquivo aberto, e o antivírus costuma segurar por
alguns instantes o que acabou de ser criado. Estas funções tentam de novo em vez de derrubar a operação."""
from __future__ import annotations

import gc
import logging
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("lineops.fs")


def rmtree_retry(path, attempts: int = 8, wait: float = 0.25) -> bool:
    for i in range(attempts):
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            gc.collect()                       # solta conexões SQLite esquecidas (o arquivo só fecha quando elas somem)
            time.sleep(wait * (i + 1))
    log.warning("não foi possível apagar a pasta temporária %s (o Windows ainda a usa); fica para depois", path)
    return False


def replace_retry(src, dest, attempts: int = 8, wait: float = 0.25) -> None:
    for i in range(attempts):
        try:
            os.replace(src, dest)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            gc.collect()
            time.sleep(wait * (i + 1))


@contextmanager
def temp_dir(prefix: str):
    """Como tempfile.TemporaryDirectory, mas a limpeza nunca derruba a operação que já deu certo."""
    d = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield d
    finally:
        gc.collect()
        rmtree_retry(d)
