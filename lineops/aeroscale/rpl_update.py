"""Atualização dos RPL pela internet (botão "Atualizar RPLs" e verificação ao abrir o app).

Fonte padrão: geradorderotas.com.br/voos/<CIA>, que publica o RPL do CGNA mais recente em texto puro
(mesmo formato dos arquivos da pasta rpl/), com autorização para leitura automática e conservadora.

Cuidados com o site:
* uma requisição por companhia, em sequência, com pausa entre elas;
* verificação automática no máximo a cada 12 h por companhia (o RPL muda uma vez por semana);
* requisição condicional (If-None-Match / If-Modified-Since) quando o site informa ETag/Last-Modified;
* User-Agent identificando o AeroScale; tempo limite e tamanho máximo.

O arquivo só é importado se for um RPL válido da companhia pedida e diferente dos já importados. Semanas
anteriores continuam ativas (cada data usa a semana que a cobre; a projeção usa a mais nova); semanas
encerradas há mais de 5 semanas são desativadas para manter o índice leve.
"""
from __future__ import annotations

import json
import os
import threading
import time as _time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

from . import __version__
from .db import get_settings, now_iso, row, rows
from .rpl_parser import parse_rpl
from .rpl_service import ImportError_, import_rpl

DEFAULT_URL = "https://geradorderotas.com.br/voos/{airline}"
DEFAULT_AIRLINES = ("AZU", "GLO", "TAM")
USER_AGENT = f"AeroScale/{__version__} (gerador de escalas para simulacao; atualizacao de RPL)"
AUTO_INTERVAL_H = 12        # verificação automática: no máximo a cada 12 h por companhia
MANUAL_COOLDOWN_S = 60      # botão: no máximo 1 requisição por minuto por companhia
PAUSE_S = 2.0               # pausa entre companhias
TIMEOUT_S = 30
MAX_BYTES = 40 * 1024 * 1024
MIN_RECORDS = 50
KEEP_WEEKS = 5              # semanas encerradas há mais que isto são desativadas

_LOCK = threading.Lock()


class Busy(Exception):
    pass


def _url_for(db, airline: str) -> str:
    tpl = os.environ.get("AEROSCALE_RPL_URL") or get_settings(db).get("rpl_update_url") or DEFAULT_URL
    return tpl.replace("{airline}", airline).replace("{AIRLINE}", airline)


def _state(db) -> dict:
    r = db.execute("SELECT value FROM meta WHERE key='rpl_update'").fetchone()
    try:
        return json.loads(r[0]) if r else {}
    except ValueError:
        return {}


def _save_state(db, st: dict):
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('rpl_update', ?)", (json.dumps(st, ensure_ascii=False),))


def _age_s(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds()
    except ValueError:
        return None


def fetch(url: str, etag: str | None = None, last_modified: str | None = None):
    """(status HTTP, bytes, cabeçalhos). 304 = não mudou."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/plain, */*;q=0.5"})
    if etag:
        req.add_header("If-None-Match", etag)
    if last_modified:
        req.add_header("If-Modified-Since", last_modified)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            data = r.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("arquivo maior que o esperado para um RPL")
            return r.status, data, dict(r.headers)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, b"", dict(e.headers or {})
        raise


def _deactivate_old(db, airline: str) -> list[int]:
    limit = (date.today() - timedelta(weeks=KEEP_WEEKS)).isoformat()
    old = [r["id"] for r in rows(db.execute(
        "SELECT id FROM rpl_imports WHERE airline=? AND active=1 AND valid_to IS NOT NULL AND valid_to<?",
        (airline, limit)))]
    # nunca deixa a companhia sem nenhuma importação ativa
    if old and db.execute("SELECT COUNT(*) FROM rpl_imports WHERE airline=? AND active=1", (airline,)).fetchone()[0] > len(old):
        db.executemany("UPDATE rpl_imports SET active=0 WHERE id=?", [(i,) for i in old])
        # vira histórico (projeção sazonal): guarda horários, dias e trechos; tira texto bruto e rota para economizar
        db.executemany("UPDATE rpl_flights SET raw_text='', route='', remarks=NULL, remarks_json=NULL WHERE import_id=?",
                       [(i,) for i in old])
        return old
    return []


def update(db, storage_dir: str, airlines=None, auto: bool = False, force: bool = False, log=print) -> dict:
    """Verifica e importa o RPL mais novo de cada companhia. auto=True respeita o intervalo de 12 h."""
    if not _LOCK.acquire(blocking=False):
        raise Busy("Já existe uma atualização de RPL em andamento.")
    try:
        airlines = [a.strip().upper() for a in (airlines or DEFAULT_AIRLINES) if a and a.strip()]
        st = _state(db)
        results = []
        did_request = False
        for al in airlines:
            prev = st.get(al) or {}
            age = _age_s(prev.get("checked_at"))
            if auto and age is not None and age < AUTO_INTERVAL_H * 3600:
                results.append({"airline": al, "status": "pulado", "message": "verificado há menos de 12 h",
                                **{k: prev.get(k) for k in ("valid_from", "valid_to")}})
                continue
            if not auto and not force and age is not None and age < MANUAL_COOLDOWN_S:
                results.append({"airline": al, "status": "pulado", "message": "verificado há menos de 1 minuto",
                                **{k: prev.get(k) for k in ("valid_from", "valid_to")}})
                continue
            if did_request:
                _time.sleep(PAUSE_S)
            did_request = True
            url = _url_for(db, al)
            rec = {"airline": al, "url": url, "checked_at": now_iso()}
            try:
                code, data, hdr = fetch(url, None if force else prev.get("etag"),
                                        None if force else prev.get("last_modified"))
                rec["etag"] = hdr.get("ETag") or prev.get("etag")
                rec["last_modified"] = hdr.get("Last-Modified") or prev.get("last_modified")
                if code == 304:
                    rec.update(status="sem_mudanca", message="o site informa que o RPL não mudou")
                else:
                    res = parse_rpl(data)
                    if res.airline != al or len(res.records) < MIN_RECORDS or not res.valid_from:
                        raise ValueError(f"o conteúdo não parece um RPL de {al} ({len(res.records)} registro(s), "
                                         f"companhia {res.airline or '?'})")
                    rec.update(valid_from=res.valid_from, valid_to=res.valid_to, records=len(res.records),
                               sha256=res.sha256)
                    dup = row(db.execute("SELECT id, active FROM rpl_imports WHERE sha256=?", (res.sha256,)))
                    if dup:
                        rec.update(status="sem_mudanca", import_id=dup["id"],
                                   message="já importado" + ("" if dup["active"] else " (importação desativada por você)"))
                    else:
                        name = f"{al}_{res.valid_from}_{res.valid_to}.txt"
                        imp = import_rpl(db, data, name, storage_dir)
                        old = _deactivate_old(db, al)
                        rec.update(status="atualizado", import_id=imp["id"], replaced=imp["replaced_imports"] + old,
                                   message=f"RPL de {res.valid_from} a {res.valid_to} importado "
                                           f"({len(res.records)} registros)")
                db.commit()
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                db.rollback()
                reason = getattr(e, "reason", None) or e
                rec.update(status="erro", message=f"sem acesso ao site ({reason})")
            except (ValueError, ImportError_) as e:
                db.rollback()
                rec.update(status="erro", message=str(e))
            for k in ("valid_from", "valid_to", "records", "sha256", "import_id"):
                if k not in rec and prev.get(k):
                    rec[k] = prev[k]
            st[al] = {k: v for k, v in rec.items() if k not in ("replaced",)}
            results.append(rec)
            if log:
                log(f"  RPL {al}: {rec['message']}")
        _save_state(db, st)
        db.commit()
        return {"results": results, "updated": sum(1 for r in results if r["status"] == "atualizado")}
    finally:
        _LOCK.release()


def status(db) -> dict:
    st = _state(db)
    s = get_settings(db)
    hist = {r["airline"]: {"weeks": r["n"], "first": r["vf"]} for r in rows(db.execute(
        "SELECT airline, COUNT(*) n, MIN(valid_from) vf FROM rpl_imports GROUP BY airline"))}
    return {"auto": bool(s.get("rpl_auto_update", True)), "url": s.get("rpl_update_url") or DEFAULT_URL,
            "airlines": {al: st.get(al) for al in DEFAULT_AIRLINES}, "interval_h": AUTO_INTERVAL_H, "history": hist}


def start_background(db_path: str, storage_dir: str):
    """Verificação ao abrir o app, sem atrasar a abertura (sem internet, só registra o aviso)."""
    from .db import connect
    if os.environ.get("AEROSCALE_NO_RPL_UPDATE"):   # testes e uso sem rede: não procura o site
        return None

    def run():
        db = connect(db_path)
        try:
            if not get_settings(db).get("rpl_auto_update", True):
                return
            r = update(db, storage_dir, auto=True, log=None)
            for x in r["results"]:
                if x["status"] in ("atualizado", "erro"):
                    print(f"  RPL {x['airline']}: {x['message']}")
        except Busy:
            pass
        except Exception as e:  # nunca derruba o app
            print(f"  Aviso: atualização automática dos RPL falhou: {e}")
        finally:
            db.close()

    t = threading.Thread(target=run, name="rpl-update", daemon=True)
    t.start()
    return t
