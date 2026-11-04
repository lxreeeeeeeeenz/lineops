"""Backup da LineOps inteira em .zip com manifesto, e restauração.

O que entra: o núcleo (piloto, celular, Voos Externos), cada carreira (escala, voos, diário, MENSAGENS da equipe, pernoites,
trocas, acompanhamento da IVAO em andamento), o banco do motor de escalas (escalas geradas e o histórico do RPL), a cópia
do AeroScale de cada carreira, os logos que você colocou e os arquivos do RPL baixados. Bancos entram por foto consistente
(API de backup do SQLite, com o servidor rodando) e conferência de integridade; o resto entra como está.
Fora: a própria pasta de backups, os logs e as pastas de dados substituídos.

Restaurar troca os arquivos, então é feito com o servidor parado: pela linha de comando, ou pela tela (o pedido fica
agendado e é aplicado na próxima vez que a LineOps abrir, antes de qualquer banco ser aberto). Antes de restaurar, a LineOps
faz um backup do estado atual, então dá para voltar atrás.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

APP = "LineOps"
PREFIX = "lineops-backup-"
LEGACY_PREFIXES = ("gol" "ops-backup-*.zip", "flygolv-backup-*.zip")
LEGACY_APPS = ("Gol" "Ops", "FlyGolV")


def _snapshot(src_path: Path, dest: Path) -> tuple[dict, str]:
    src, dst = sqlite3.connect(src_path), sqlite3.connect(dest)
    try:
        with dst:
            src.backup(dst)
        n = {}
        for tb in ("aircraft", "roster_leg", "flight", "pirep", "logbook_entry", "ext_flight", "ops_message", "swap_request"):
            try:
                n[tb] = dst.execute(f"SELECT COUNT(*) FROM {tb}").fetchone()[0]
            except sqlite3.Error:
                pass
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        src.close(); dst.close()
    if ok != "ok":
        raise RuntimeError(f"Banco {src_path.name} com problemas de integridade: {ok}")
    return n, hashlib.sha256(dest.read_bytes()).hexdigest()


SKIP_DIRS = {"backups", "logs"}
SKIP_SUFFIX = ("-wal", "-shm", "-journal", ".tmp", ".before-restore")
RESTORE_FLAG = "restaurar.json"
KEEP_LABELED = 10                       # backups "antes-da-<versão>" e "antes-de-restaurar" têm a sua própria fila


def _is_sqlite(f: Path) -> bool:
    try:
        with open(f, "rb") as fh:
            return fh.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def data_files(data_dir: Path) -> tuple[list[Path], list[Path]]:
    """(bancos, outros arquivos) da pasta de dados que entram no backup."""
    from .careers import all_db_files
    data_dir = Path(data_dir)
    main = all_db_files(data_dir)
    dbs, other = list(main), []
    for f in sorted(data_dir.rglob("*")):
        rel = f.relative_to(data_dir)
        if not f.is_file() or f in main:
            continue
        top = rel.parts[0]
        if top in SKIP_DIRS or top.startswith("dados-substituidos") or f.name.endswith(SKIP_SUFFIX) or f.name == RESTORE_FLAG:
            continue
        if len(rel.parts) == 1 and f.suffix == ".db" and f.name != "lineops.db":
            continue                                             # banco antigo ainda não adotado / temporário: fica fora
        (dbs if _is_sqlite(f) else other).append(f)
    return dbs, other


def make_backup(data_dir: Path, out_dir: Path | None = None, keep: int = 30, label: str | None = None) -> Path:
    """A LineOps inteira num .zip: foto consistente de cada banco + os outros arquivos, com assinatura de cada um."""
    import logging
    data_dir = Path(data_dir)
    out_dir = Path(out_dir) if out_dir else data_dir / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    lab = "".join(ch for ch in (label or "") if ch.isalnum() or ch in "-._")[:40]
    dest = out_dir / f"{PREFIX}{stamp}{'-' + lab if lab else ''}.zip"
    n = 2
    while dest.exists():
        dest = out_dir / f"{PREFIX}{stamp}-{n}{'-' + lab if lab else ''}.zip"
        n += 1
    from .careers import all_db_files
    main = set(all_db_files(data_dir))
    dbs, other = data_files(data_dir)
    if not main:
        raise RuntimeError("Nada para guardar: nenhum banco encontrado.")
    from .fsutil import temp_dir
    mf = {"app": APP, "version": __version__, "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
          "label": label, "files": {}, "skipped": {}}
    tmpz = dest.with_name(dest.name + ".tmp")
    with temp_dir("lineops_bkp_") as t:
        with zipfile.ZipFile(tmpz, "w", zipfile.ZIP_DEFLATED) as z:
            for k, f in enumerate(dbs):
                arc = f.relative_to(data_dir).as_posix()
                snap = Path(t) / f"{k}-{f.name}"
                try:
                    counts, sha = _snapshot(f, snap)
                except Exception as e:                           # noqa: BLE001
                    if f in main:
                        raise                                    # núcleo e carreiras: backup sem eles não vale
                    mf["skipped"][arc] = str(e)
                    logging.getLogger("lineops.backup").warning("backup: %s ficou de fora (%s)", arc, e)
                    continue
                z.write(snap, arc)
                mf["files"][arc] = {"sha256": sha, "counts": counts, "kind": "db"}
                snap.unlink(missing_ok=True)
            for f in other:
                arc = f.relative_to(data_dir).as_posix()
                try:
                    data = f.read_bytes()
                except OSError as e:
                    mf["skipped"][arc] = str(e)
                    continue
                z.writestr(arc, data)
                mf["files"][arc] = {"sha256": hashlib.sha256(data).hexdigest(), "kind": "file", "size": len(data)}
            z.writestr("manifest.json", json.dumps(mf, indent=2, ensure_ascii=False))
    tmpz.replace(dest)
    _rotate(out_dir, keep)
    return dest


def _labeled(f: Path) -> bool:
    return "-antes-d" in f.name


def _rotate(out_dir: Path, keep: int) -> None:
    allz = sorted(out_dir.glob(f"{PREFIX}*.zip"))
    plain = [f for f in allz if not _labeled(f)]
    tagged = [f for f in allz if _labeled(f)]
    for f in plain[:-keep] + tagged[:-KEEP_LABELED]:
        f.unlink(missing_ok=True)


def info(f: Path) -> dict:
    """Nome, tamanho, data, versão, etiqueta e o que tem dentro (lido do manifesto)."""
    out = {"name": f.name, "size": f.stat().st_size, "created_at": None, "version": None, "label": None, "files": None,
           "messages": None, "flights": None, "ok": True}
    try:
        with zipfile.ZipFile(f) as z:
            mf = json.loads(z.read("manifest.json"))
        out.update(created_at=mf.get("created_at"), version=mf.get("version"), label=mf.get("label"))
        if "files" in mf:
            out["files"] = len(mf["files"])
            cs = [v.get("counts") or {} for v in mf["files"].values()]
            out["flights"] = sum(c.get("logbook_entry", 0) for c in cs)
            out["messages"] = sum(c.get("ops_message", 0) for c in cs) if any("ops_message" in c for c in cs) else None
    except Exception:                                            # noqa: BLE001
        out["ok"] = False
    return out


def list_backups(out_dir: Path) -> list[dict]:
    if not out_dir.exists():
        return []
    fs = sorted([f for pat in (f"{PREFIX}*.zip",) + LEGACY_PREFIXES for f in out_dir.glob(pat)], key=lambda f: f.name, reverse=True)
    return [info(f) for f in fs]


def validate(zip_path: Path) -> dict:
    """Confere um .zip antes de aceitar: é backup da LineOps (ou do GolOps), e cada arquivo bate com a assinatura."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            mf = json.loads(z.read("manifest.json"))
            if "files" in mf:
                if mf.get("app") != APP:
                    raise RuntimeError("Este .zip não é um backup da LineOps.")
                for arc, inf in mf["files"].items():
                    if ".." in Path(arc).parts or Path(arc).is_absolute():
                        raise RuntimeError("Backup com caminho inválido.")
                    if hashlib.sha256(z.read(arc)).hexdigest() != inf["sha256"]:
                        raise RuntimeError(f"Backup corrompido ({arc}: assinatura não confere).")
            elif mf.get("app") not in LEGACY_APPS:
                raise RuntimeError("Este .zip não é um backup da LineOps.")
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError):
        raise RuntimeError("Arquivo inválido: não é um backup da LineOps.")
    return mf


# ------------------------------------------------------------------ antes de cada atualização
VERSION_FILE = "versao.txt"             # a versão que abriu estes dados por último


def last_version(data_dir: Path) -> str | None:
    try:
        f = Path(data_dir) / VERSION_FILE
        return f.read_text(encoding="utf-8").strip() or None if f.exists() else None
    except OSError:
        return None


def before_update(data_dir: Path, schema_pending: bool = False) -> Path | None:
    """Versão nova abrindo estes dados (ou banco para atualizar): backup completo 'antes-da-<versão>', uma vez."""
    from .careers import all_db_files
    lv = last_version(data_dir)
    if not all_db_files(data_dir) or not (schema_pending or (lv and lv != __version__)):
        return None
    return make_backup(Path(data_dir), label=f"antes-da-{__version__}")


def mark_version(data_dir: Path) -> None:
    try:
        (Path(data_dir) / VERSION_FILE).write_text(__version__, encoding="utf-8")
    except OSError:
        pass


# ------------------------------------------------------------------ restaurar na próxima abertura
def schedule_restore(data_dir: Path, zip_path: Path) -> dict:
    validate(zip_path)
    flag = Path(data_dir) / RESTORE_FLAG
    flag.write_text(json.dumps({"zip": str(zip_path), "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}), encoding="utf-8")
    return {"scheduled": True, "zip": Path(zip_path).name}


def pending_restore(data_dir: Path) -> dict | None:
    flag = Path(data_dir) / RESTORE_FLAG
    if not flag.exists():
        return None
    try:
        return json.loads(flag.read_text(encoding="utf-8"))
    except Exception:                                            # noqa: BLE001
        return None


def cancel_restore(data_dir: Path) -> None:
    (Path(data_dir) / RESTORE_FLAG).unlink(missing_ok=True)


def apply_pending_restore(data_dir: Path) -> dict | None:
    """Chamado ao abrir, antes de qualquer banco. Guarda o estado atual (antes-de-restaurar) e restaura."""
    import logging
    lg = logging.getLogger("lineops.backup")
    p = pending_restore(data_dir)
    if p is None:
        if (Path(data_dir) / RESTORE_FLAG).exists():
            cancel_restore(data_dir)
        return None
    z = Path(p.get("zip") or "")
    cancel_restore(data_dir)                                     # nunca fica em laço: sai antes, aconteça o que acontecer
    if not z.is_file():
        lg.error("restauração agendada, mas o arquivo sumiu: %s", z)
        return {"ok": False, "error": f"arquivo não encontrado: {z.name}"}
    try:
        validate(z)
        safety = make_backup(Path(data_dir), label="antes-de-restaurar") if any(Path(data_dir).glob("*.db")) else None
        files = restore(z, Path(data_dir))
        lg.info("backup %s restaurado (%d arquivo(s)); o estado anterior ficou em %s", z.name, len(files), safety.name if safety else "—")
        return {"ok": True, "zip": z.name, "files": files, "safety": safety.name if safety else None}
    except Exception as e:                                       # noqa: BLE001
        lg.exception("não foi possível restaurar %s", z.name)
        return {"ok": False, "zip": z.name, "error": str(e)}


def restore(zip_path: Path, data_dir: Path) -> list[str]:
    """Restauração OFFLINE (servidor parado). Cada banco atual é preservado como .before-restore.
    Backup antigo (de antes das carreiras, um banco só) volta como a carreira GOL."""
    from .careers import DEFAULT, career_path
    data_dir = Path(data_dir)
    out = []
    with zipfile.ZipFile(zip_path) as z:
        mf = json.loads(z.read("manifest.json"))
        if "files" in mf:
            if mf.get("app") != APP:
                raise RuntimeError("Este .zip não é um backup do LineOps.")
            items = [(arc, z.read(arc), info["sha256"]) for arc, info in mf["files"].items()]
        else:                                                     # backup de um banco só (antes da LineOps)
            nome = next((n for n in z.namelist() if n.endswith(".db")), None)
            if not nome or mf.get("app") not in LEGACY_APPS:
                raise RuntimeError("Backup inválido.")
            target = career_path(data_dir, DEFAULT).relative_to(data_dir).as_posix()
            core = data_dir / "lineops.db"
            empty = True
            if core.exists():
                c = sqlite3.connect(core)
                try:
                    empty = not c.execute("SELECT 1 FROM sqlite_master WHERE name='user'").fetchone() or not c.execute("SELECT 1 FROM user").fetchone()
                finally:
                    c.close()
            if empty:                                     # sem piloto no núcleo: o banco antigo é adotado inteiro na próxima abertura
                for x in (core, Path(str(core) + "-wal"), Path(str(core) + "-shm")):
                    x.unlink(missing_ok=True)
                target = "gol" "ops.db"
            items = [(target, z.read(nome), mf.get("db_sha256"))]
    for arc, data, sha in items:
        if hashlib.sha256(data).hexdigest() != sha:
            raise RuntimeError(f"Backup corrompido ({arc}: assinatura não confere).")
        if ".." in Path(arc).parts or Path(arc).is_absolute():
            raise RuntimeError("Backup com caminho inválido.")
    for arc, data, _sha in items:
        dest = data_dir / arc
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.replace(dest.with_suffix(".before-restore"))
        for ext in ("-wal", "-shm"):
            Path(str(dest) + ext).unlink(missing_ok=True)
        dest.write_bytes(data)
        out.append(arc)
    return out


def auto_backup(data_dir: Path, hours: float = 24, keep: int = 30):
    """Backup automático: um por dia, enquanto o LineOps estiver rodando (guarda os 30 mais recentes)."""
    import time
    out = Path(data_dir) / "backups"
    olds = [f for pat in (f"{PREFIX}*.zip",) + LEGACY_PREFIXES for f in out.glob(pat)] if out.exists() else []
    if olds and time.time() - max(f.stat().st_mtime for f in olds) < hours * 3600:
        return None
    return make_backup(Path(data_dir), out, keep)
