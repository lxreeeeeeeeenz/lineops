"""Tarefas em segundo plano (gerar/regerar escalas longas) com progresso e cancelamento.

Cada tarefa roda numa thread com conexão própria ao SQLite. A interface consulta /api/jobs/<id> e mostra a
barra de progresso; "Cancelar" liga um aviso que o planejador confere a cada dia planejado.
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
MAX_KEEP = 30


class Cancelled(Exception):
    pass


def start(db_path: str, label: str, fn, *args, **kwargs) -> dict:
    """fn(db, progress, cancel_event, *args, **kwargs) -> resultado (JSON). Devolve o estado inicial."""
    from .db import connect
    jid = uuid.uuid4().hex[:12]
    cancel = threading.Event()
    job = {"id": jid, "label": label, "state": "running", "progress": 0.0, "stage": "começando",
           "started": time.time(), "finished": None, "result": None, "error": None}
    with _LOCK:
        if len(_JOBS) >= MAX_KEEP:
            for k in sorted(_JOBS, key=lambda k: _JOBS[k]["started"])[: len(_JOBS) - MAX_KEEP + 1]:
                if _JOBS[k]["state"] != "running":
                    _JOBS.pop(k, None)
        _JOBS[jid] = job
        job["_cancel"] = cancel

    def progress(frac: float, stage: str | None = None):
        if cancel.is_set():
            raise Cancelled()
        job["progress"] = max(job["progress"], min(1.0, float(frac)))
        if stage:
            job["stage"] = stage

    def run():
        db = connect(db_path)
        try:
            res = fn(db, progress, cancel, *args, **kwargs)
            db.commit()
            job.update(state="finished", progress=1.0, stage="concluído", result=res)
        except Cancelled:
            db.rollback()
            job.update(state="cancelled", stage="cancelado")
        except Exception as e:  # erro de regra (PlanError, ValueError) ou inesperado
            db.rollback()
            job.update(state="error", stage="erro", error=str(e) or type(e).__name__)
            if not isinstance(e, (ValueError, LookupError)) and type(e).__name__ not in ("PlanError",):
                job["trace"] = traceback.format_exc(limit=6)
        finally:
            job["finished"] = time.time()
            db.close()

    threading.Thread(target=run, name=f"job-{jid}", daemon=True).start()
    return public(job)


def public(job: dict) -> dict:
    out = {k: v for k, v in job.items() if not k.startswith("_") and k != "trace"}
    out["elapsed_s"] = round((job["finished"] or time.time()) - job["started"], 1)
    return out


def get(jid: str) -> dict | None:
    j = _JOBS.get(jid)
    return public(j) if j else None


def cancel(jid: str) -> bool:
    j = _JOBS.get(jid)
    if not j or j["state"] != "running":
        return False
    j["_cancel"].set()
    return True


def wait(jid: str, timeout: float = 120) -> dict | None:
    """Usado nos testes: espera a tarefa terminar."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = _JOBS.get(jid)
        if j and j["state"] != "running":
            return public(j)
        time.sleep(0.05)
    return get(jid)
