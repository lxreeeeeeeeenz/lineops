"""Banco SQLite (WAL), migrações versionadas e utilitários de transação."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)   # autocommit; 1 conexão por requisição, nunca compartilhada
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -16000")        # ~16 MB de cache de páginas por conexão
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    """Transação atômica (BEGIN IMMEDIATE). Aninhamento vira SAVEPOINT."""
    if conn.in_transaction:
        name = f"sp_{id(object())}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def pending(conn: sqlite3.Connection) -> list[str]:
    """Migrações que ainda não rodaram neste banco."""
    has = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")} if has else set()
    return [f.stem for f in sorted(MIGRATIONS_DIR.glob("*.sql")) if f.stem not in done]


def migrate(conn: sqlite3.Connection) -> list[str]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    applied = []
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if f.stem in done:
            continue
        with tx(conn):
            for stmt in _split_sql(f.read_text(encoding="utf-8")):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (f.stem, now_iso()))
        applied.append(f.stem)
    return applied


def _split_sql(script: str) -> list[str]:
    out, buf = [], []
    for line in script.splitlines():
        if line.strip().startswith("--"):
            continue
        buf.append(line)
        if line.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
    if "".join(buf).strip():
        out.append("\n".join(buf).strip())
    return out


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur]


def one(cur) -> dict | None:
    r = cur.fetchone()
    return dict(r) if r else None


def jloads(s, default=None):
    if s in (None, ""):
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def audit(conn, user_id, action, entity=None, entity_id=None, detail=None):
    conn.execute("INSERT INTO audit_log(ts, user_id, action, entity, entity_id, detail) VALUES (?,?,?,?,?,?)",
                 (now_iso(), user_id, action, entity, None if entity_id is None else str(entity_id),
                  json.dumps(detail, ensure_ascii=False) if detail is not None else None))
