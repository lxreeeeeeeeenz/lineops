"""Persistência SQLite (biblioteca padrão do Python, sem servidor)."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

try:                                    # LineOps: o motor roda sem Flask (só get_db/close_db usam isto, no app do AeroScale)
    from flask import current_app, g
except ImportError:                     # pragma: no cover
    current_app = g = None

SCHEMA_VERSION = 1

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS airlines (
    code TEXT PRIMARY KEY,            -- designador ICAO (GLO, AZU, TAM...)
    iata TEXT, name TEXT NOT NULL, color TEXT DEFAULT '#FF7A1A',
    active INTEGER NOT NULL DEFAULT 1,
    needs_review INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS airports (   -- cadastro manual / sobrescreve a tabela embutida
    icao TEXT PRIMARY KEY, iata TEXT, name TEXT, city TEXT, uf TEXT, tz TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rpl_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    airline TEXT NOT NULL, filename TEXT NOT NULL, stored_path TEXT,
    sha256 TEXT NOT NULL, encoding TEXT, header_validity TEXT,
    valid_from TEXT, valid_to TEXT,
    record_count INTEGER DEFAULT 0, warning_count INTEGER DEFAULT 0, error_count INTEGER DEFAULT 0,
    file_issues TEXT DEFAULT '[]', layout TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rpl_flights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id INTEGER NOT NULL REFERENCES rpl_imports(id) ON DELETE CASCADE,
    airline TEXT NOT NULL, ident TEXT NOT NULL, flight_number TEXT,
    valid_from TEXT, valid_to TEXT, days TEXT,
    type_icao TEXT, wake TEXT, adep TEXT, eobt TEXT, speed TEXT, fl TEXT,
    route TEXT, ades TEXT, eet TEXT, eet_min INTEGER,
    remarks TEXT, remarks_json TEXT,
    raw_text TEXT NOT NULL, line_no INTEGER,
    status TEXT NOT NULL DEFAULT 'ok', issues TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_rpl_f_air ON rpl_flights(airline, adep, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS ix_rpl_f_ident ON rpl_flights(ident);
CREATE INDEX IF NOT EXISTS ix_rpl_f_imp ON rpl_flights(import_id);

CREATE TABLE IF NOT EXISTS aircraft_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    airline TEXT NOT NULL, name TEXT NOT NULL, icao_type TEXT NOT NULL, family TEXT,
    source TEXT DEFAULT 'manual', created_at TEXT NOT NULL,
    UNIQUE(airline, icao_type, name)
);

CREATE TABLE IF NOT EXISTS aircraft (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    airline TEXT NOT NULL, registration TEXT NOT NULL UNIQUE,
    model_id INTEGER REFERENCES aircraft_models(id),
    model_source TEXT DEFAULT 'manual',        -- manual | import (protege edição manual na reimportação)
    msn TEXT, notes TEXT DEFAULT '',
    blocked_airports TEXT DEFAULT '',          -- ICAO separados por vírgula
    unavailable TEXT DEFAULT '[]',             -- [{"from":"2026-11-01","to":"2026-11-03","reason":"..."}]
    active INTEGER NOT NULL DEFAULT 1,
    source TEXT DEFAULT 'manual', is_demo INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_aircraft_airline ON aircraft(airline);

CREATE TABLE IF NOT EXISTS config_fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL, label TEXT NOT NULL,
    airline TEXT,          -- NULL = todas as companhias
    family TEXT,           -- NULL = todas as famílias
    kind TEXT NOT NULL DEFAULT 'tri',   -- tri | text | number | select
    options TEXT DEFAULT '[]', description TEXT DEFAULT '',
    sort INTEGER DEFAULT 100, active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(key, airline, family)
);

CREATE TABLE IF NOT EXISTS aircraft_config (
    aircraft_id INTEGER NOT NULL REFERENCES aircraft(id) ON DELETE CASCADE,
    field_id INTEGER NOT NULL REFERENCES config_fields(id) ON DELETE CASCADE,
    value TEXT, raw_value TEXT, source TEXT NOT NULL DEFAULT 'manual',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (aircraft_id, field_id)
);

CREATE TABLE IF NOT EXISTS aircraft_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    aircraft_id INTEGER NOT NULL REFERENCES aircraft(id) ON DELETE CASCADE,
    import_id INTEGER, line_no INTEGER, raw_text TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS aircraft_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    aircraft_id INTEGER NOT NULL REFERENCES aircraft(id) ON DELETE CASCADE,
    ts TEXT NOT NULL, action TEXT NOT NULL, field TEXT, old_value TEXT, new_value TEXT,
    source TEXT, note TEXT
);
CREATE INDEX IF NOT EXISTS ix_hist_ac ON aircraft_history(aircraft_id);

CREATE TABLE IF NOT EXISTS fleet_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    airline TEXT, filename TEXT, stored_path TEXT, sha256 TEXT,
    stats TEXT DEFAULT '{}', mapping TEXT DEFAULT '{}', imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity TEXT NOT NULL, entity_id INTEGER, severity TEXT NOT NULL DEFAULT 'warning',
    code TEXT NOT NULL, message TEXT NOT NULL, data TEXT DEFAULT '{}',
    resolved INTEGER NOT NULL DEFAULT 0, resolution TEXT,
    created_at TEXT NOT NULL, resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_flags_open ON review_flags(resolved, entity);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, airline TEXT NOT NULL, kind TEXT NOT NULL,
    start_date TEXT NOT NULL, end_date TEXT NOT NULL,
    aircraft_id INTEGER, params TEXT DEFAULT '{}', diagnostics TEXT DEFAULT '[]',
    notes TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedule_legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL UNIQUE,
    schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    aircraft_id INTEGER, registration TEXT,
    airline TEXT NOT NULL, flight_id INTEGER, flight_number TEXT,
    type_icao TEXT, planned_type TEXT,
    adep TEXT NOT NULL, ades TEXT NOT NULL,
    dep_utc TEXT NOT NULL, arr_utc TEXT NOT NULL, eet_min INTEGER,
    data_origin TEXT NOT NULL,      -- REAL | PROJETADO | SUGESTAO | MANUAL
    ref_date TEXT, notes TEXT DEFAULT '', edited INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_legs_sched ON schedule_legs(schedule_id, seq);
CREATE INDEX IF NOT EXISTS ix_legs_ac ON schedule_legs(aircraft_id, dep_utc);
CREATE INDEX IF NOT EXISTS ix_legs_flight ON schedule_legs(flight_id);
CREATE INDEX IF NOT EXISTS ix_legs_airline_dep ON schedule_legs(airline, dep_utc);

CREATE TABLE IF NOT EXISTS schedule_days (      -- v2.2: dias sem voo com significado (SOBREAVISO, INATIVO, RESERVA)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    date TEXT NOT NULL, kind TEXT NOT NULL,                   -- sobreaviso | inativo | reserva
    location TEXT, status TEXT DEFAULT '', note TEXT DEFAULT '', data TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sched_days ON schedule_days(schedule_id, date);

CREATE TABLE IF NOT EXISTS schedule_reserves (   -- bloco de reserva sorteado na geração (resultado salvo)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL UNIQUE,
    schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    start_date TEXT NOT NULL, end_date TEXT NOT NULL, days INTEGER NOT NULL,
    location TEXT NOT NULL, location_kind TEXT NOT NULL,      -- base | outro
    location_roll REAL, base_pct REAL,
    activation_pct REAL, activation_roll REAL, activated INTEGER NOT NULL DEFAULT 0,
    act_start TEXT, act_days INTEGER,                         -- dias sorteados para o acionamento
    status TEXT NOT NULL DEFAULT 'pendente',                  -- sem_acionamento | acionado | inviavel
    note TEXT DEFAULT '', seed INTEGER,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reserves_sched ON schedule_reserves(schedule_id, start_date);

CREATE TABLE IF NOT EXISTS intl_flights (   -- voos internacionais observados no ADS-B (fora do RPL do CGNA)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    airline TEXT NOT NULL, ident TEXT NOT NULL, adep TEXT NOT NULL, ades TEXT NOT NULL, type_icao TEXT,
    dep_min INTEGER NOT NULL, block_min INTEGER NOT NULL, weekdays TEXT NOT NULL,   -- '0123456' = seg..dom
    days_seen INTEGER, days_total INTEGER, first_seen TEXT, last_seen TEXT,
    times_source TEXT, endpoints_source TEXT, freq_source TEXT,
    source TEXT NOT NULL,                     -- embutido | adsb
    active INTEGER NOT NULL DEFAULT 1, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_intl_airline ON intl_flights(airline, ident);

CREATE TABLE IF NOT EXISTS weather_cache (
    kind TEXT NOT NULL, icao TEXT NOT NULL, raw TEXT, data TEXT, obs_time TEXT,
    source TEXT, fetched_at TEXT NOT NULL, error TEXT,
    PRIMARY KEY (kind, icao)
);

CREATE TABLE IF NOT EXISTS adsb_days (      -- dias do histórico adsb.lol já processados
    day TEXT PRIMARY KEY, tag TEXT, status TEXT, aircraft INTEGER, runs INTEGER, matched INTEGER,
    mb REAL, seconds INTEGER, prefixes TEXT, scanned_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS adsb_aircraft (  -- matrículas reais vistas no ADS-B
    hex TEXT PRIMARY KEY, registration TEXT, icao_type TEXT, descr TEXT, airline TEXT,
    first_day TEXT, last_day TEXT, days INTEGER DEFAULT 0, days_list TEXT DEFAULT '', last_callsign TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS adsb_runs (      -- trechos com o mesmo indicativo (dado bruto resumido)
    id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, hex TEXT NOT NULL, callsign TEXT, airline TEXT,
    t0 TEXT, t1 TEXT, lat0 REAL, lon0 REAL, alt0 INTEGER, lat1 REAL, lon1 REAL, alt1 INTEGER, points INTEGER
);
CREATE TABLE IF NOT EXISTS adsb_legs (      -- pernas observadas (ligadas ao RPL ou por posição)
    id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, hex TEXT NOT NULL, airline TEXT, callsign TEXT,
    ref TEXT, origin TEXT, adep TEXT, ades TEXT, dep_utc TEXT, arr_utc TEXT, seen_from TEXT, seen_to TEXT,
    source TEXT
);
CREATE INDEX IF NOT EXISTS ix_adsb_runs_day ON adsb_runs(day);
CREATE INDEX IF NOT EXISTS ix_adsb_runs_hex ON adsb_runs(hex, t0);
CREATE INDEX IF NOT EXISTS ix_adsb_legs_hex ON adsb_legs(hex, dep_utc);
CREATE INDEX IF NOT EXISTS ix_adsb_legs_day ON adsb_legs(day);
CREATE INDEX IF NOT EXISTS ix_adsb_aircraft_reg ON adsb_aircraft(registration);

CREATE TABLE IF NOT EXISTS gcal_events (
    leg_uid TEXT NOT NULL, calendar_id TEXT NOT NULL, event_id TEXT NOT NULL,
    schedule_id INTEGER, content_hash TEXT, synced_at TEXT NOT NULL,
    PRIMARY KEY (leg_uid, calendar_id)
);
"""

DEFAULT_SETTINGS = {
    "display_tz": "local",          # local | utc
    "taxi_allowance_min": 10,       # acréscimo EOBT+EET -> calço (estimativa)
    "min_turn_min": 40,
    "max_turn_min": 240,
    "min_rest_min": 600,
    "day_start": "05:00",
    "day_end": "23:59",
    "legs_min": 2,
    "legs_max": 5,
    "projection_enabled": True,
    "avoid_used_flights": True,
    "turn_mode": "auto",            # auto (medido no RPL) | fixed
    "crew_mode": "rbac117",         # rbac117 | lei | off
    "crew_days_off": True,
    "crew_folga_base": True,
    "crew_madrugada": True,
    "crew_monthly": True,
    "days_off_min": 10,
    "max_consecutive_days": 6,
    "report_before_min": "auto",    # auto = perfil da companhia (GOL/LATAM 60 min, Azul 50 min)
    "release_after_min": 30,
    "weekday_max": {},              # {"1": 1, "3": 0} = terça até 1 perna, quinta folga
    "metar_ttl_min": 10,            # cache do METAR (min)
    "metar_autorefresh_min": 10,    # atualização automática na tela (0 = desligada)
    "model_realism": True,          # gerador prioriza sequências prováveis (modelo de rodízio)
    "anomaly_repeat_pct": 5,        # % de dias em que a repetição de trecho é sorteada como anormalidade
    "simbrief_units": "KGS",
    "simbrief_rmk": "TCAS",         # texto do RMK/ enviado ao SimBrief
    "simbrief_alternates": {},      # {"SBFZ": "SBJP"} alternado fixo por destino
    "rpl_autoimport": True,         # importa na inicialização os RPL novos da pasta rpl/
    "rpl_auto_update": True,        # ao abrir, busca o RPL mais novo na internet (no máximo a cada 12 h)
    "rpl_update_url": "https://geradorderotas.com.br/voos/{airline}",
    "window_mode": "app",           # 'app' = janela própria (Edge/Chrome sem abas); 'navegador' = navegador padrão
    "update_repo": "",              # usuario/repositorio do AeroScale no GitHub (atualizador)
    "update_beta": False,
    "lan_enabled": False,           # acesso pelo celular na mesma rede Wi-Fi (página móvel e calendário, com chave)
    "lan_token": "",
    "mobile_schedule_id": 0,        # escala mostrada no celular (0 = a mais recente que cobre hoje)
    "acars_enabled": False,         # ACARS (opcional, realismo total): lê X-Plane 12 / MSFS 2024 / P3D v5.3-5.4
    "acars_sim": "xplane",
    "acars_xp_host": "127.0.0.1",
    "acars_xp_port": 49000,
    "acars_msfs_dll": "",
    "acars_p3d_dll": "",
    "aisweb_api_key": "",           # cartas de aeródromo pela API oficial do AISWEB (chave gratuita pedida pelo usuário)
    "aisweb_api_pass": "",
    "aisweb_url": "https://api.decea.mil.br/aisweb/",
    "aisweb_page_url": "https://aisweb.decea.gov.br/?i=aerodromos&codigo={icao}",   # sem chave: página pública
    "net_enabled": False,           # monitor IVAO/VATSIM (diário de bordo automático)
    "net_network": "ambas",
    "net_ivao_id": "",
    "net_vatsim_id": "",
    "type_match": "family",         # padrão do gerador: mesma família (B738↔B38M, A320↔A20N), como no ADS-B
    "intl_enabled": True,           # gerador inclui voos internacionais observados no ADS-B
    "reserve_enabled": True,        # reserva automática nas escalas semanais e mensais
    "reserve_per_month": 1,         # blocos de reserva por escala mensal (semanal: 1)
    "reserve_min_days": 3,          # duração sorteada entre min e max (limites 3 e 6: nunca 7 dias de reserva seguidos)
    "reserve_max_days": 6,
    "reserve_base_pct": 80,         # chance de a reserva ser na base (senão, outro aeroporto da operação)
    "reserve_activation_pct": 50,   # chance de acionamento durante a reserva
    "reserve_activation_max_days": 3,  # o acionamento dura de 1 até N dias
    "model_weights": {},            # pesos do modelo de rodízio calibrados com ADS-B, por companhia
    "model_weights_info": None,     # quando e com quantas transições foram calibrados
    "adsb_prefixes": "GLO,TAM,AZU", # indicativos lidos do histórico adsb.lol
    "google_calendar_id": "",
    "google_redirect_uri": "",
}

DEFAULT_AIRLINES = [
    ("AZU", "AD", "Azul Linhas Aéreas", "#2E8BFF"),
    ("GLO", "G3", "GOL Linhas Aéreas", "#FF7A1A"),
    ("TAM", "JJ", "LATAM Airlines Brasil", "#E5484D"),
]

# Campos de configuração fornecidos pelo usuário como exemplo LATAM.
# Escopo inicial: companhia TAM, família A320 (editável na tela de campos).
LATAM_FIELDS = [
    ("ISIS", "ISIS", "Instrumento integrado de standby"),
    ("ANALOGIC", "ANALOGIC", "Instrumentos de standby analógicos"),
    ("DDRMI", "DDRMI", "Digital Distance and Radio Magnetic Indicator"),
    ("DCDU", "DCDU", "Datalink Control and Display Unit (CPDLC)"),
    ("ROW_ROPS", "ROW/ROPS", "Runway Overrun Warning / Protection System"),
    ("DRAMS", "DRAMS", "Campo informado na lista LATAM"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, detect_types=0, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")   # seguro com WAL e bem mais rápido no Windows
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _migrate(conn):
    """Colunas novas em bancos criados por versões anteriores (sem perder dados)."""
    # v2.2.2: reserva nunca passa de 6 dias seguidos; apresentação passa a seguir a companhia (antes 30 min fixo)
    if not conn.execute("SELECT 1 FROM meta WHERE key='mig_222'").fetchone():
        r = conn.execute("SELECT value FROM settings WHERE key='reserve_max_days'").fetchone()
        if r and json.loads(r[0]) not in (None, "") and int(json.loads(r[0])) > 6:
            conn.execute("UPDATE settings SET value=? WHERE key='reserve_max_days'", (json.dumps(6),))
        r = conn.execute("SELECT value FROM settings WHERE key='reserve_min_days'").fetchone()
        if r and json.loads(r[0]) not in (None, "") and int(json.loads(r[0])) > 6:
            conn.execute("UPDATE settings SET value=? WHERE key='reserve_min_days'", (json.dumps(6),))
        r = conn.execute("SELECT value FROM settings WHERE key='report_before_min'").fetchone()
        if r and json.loads(r[0]) == 30:
            conn.execute("UPDATE settings SET value=? WHERE key='report_before_min'", (json.dumps("auto"),))
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('mig_222', '1')")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(schedule_legs)")}
    if "activity" not in cols:   # '' = voo normal | 'RESERVA' = acionamento de reserva (simulado)
        conn.execute("ALTER TABLE schedule_legs ADD COLUMN activity TEXT NOT NULL DEFAULT ''")
    if "reserve_id" not in cols:
        conn.execute("ALTER TABLE schedule_legs ADD COLUMN reserve_id INTEGER")
    # v2: simulação de atrasos e pernas voadas (diário de bordo)
    for c, ddl in (("sim_dep_utc", "TEXT"), ("sim_arr_utc", "TEXT"), ("sim_delay_min", "INTEGER"),
                   ("sim_status", "TEXT NOT NULL DEFAULT ''"), ("flown_status", "TEXT NOT NULL DEFAULT ''"),
                   ("act_dep_utc", "TEXT"), ("act_arr_utc", "TEXT"), ("logbook_id", "INTEGER")):
        if c not in cols:
            conn.execute(f"ALTER TABLE schedule_legs ADD COLUMN {c} {ddl}")


def init_db(path: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        from . import logbook as _lb   # tabelas da v2
        conn.executescript(_lb.SCHEMA)
        from . import netmon as _nm
        conn.executescript(_nm.SCHEMA)
        from . import acars as _ac
        conn.executescript(_ac.SCHEMA)
        from . import charts as _ch   # v2.2.1: cartas do AISWEB
        conn.executescript(_ch.SCHEMA)
        _migrate(conn)
        ts = now_iso()
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, json.dumps(v)))
        for code, iata, name, color in DEFAULT_AIRLINES:
            conn.execute(
                "INSERT OR IGNORE INTO airlines(code, iata, name, color, created_at) VALUES (?,?,?,?,?)",
                (code, iata, name, color, ts),
            )
        for i, (key, label, desc) in enumerate(LATAM_FIELDS):
            exists = conn.execute(
                "SELECT 1 FROM config_fields WHERE key=? AND airline='TAM'", (key,)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO config_fields(key,label,airline,family,kind,description,sort,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (key, label, "TAM", "A320", "tri", desc, 10 + i, ts),
                )
        from . import intl  # catálogo internacional embutido (só na primeira vez)
        intl.seed_bundled(conn)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        conn.commit()
    finally:
        conn.close()


def get_settings(db) -> dict:
    out = dict(DEFAULT_SETTINGS)
    for r in db.execute("SELECT key, value FROM settings"):
        try:
            out[r["key"]] = json.loads(r["value"])
        except Exception:
            out[r["key"]] = r["value"]
    return out


def set_settings(db, values: dict):
    for k, v in values.items():
        db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (k, json.dumps(v)))


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur]


def row(cur) -> dict | None:
    r = cur.fetchone()
    return dict(r) if r else None


def add_flag(db, entity, entity_id, code, message, severity="warning", data=None):
    """Cria pendência de revisão; evita duplicar pendência aberta idêntica."""
    dup = db.execute(
        "SELECT id FROM review_flags WHERE resolved=0 AND entity=? AND entity_id IS ? AND code=? AND message=?",
        (entity, entity_id, code, message),
    ).fetchone()
    if dup:
        return dup["id"]
    cur = db.execute(
        "INSERT INTO review_flags(entity, entity_id, severity, code, message, data, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (entity, entity_id, severity, code, message, json.dumps(data or {}, ensure_ascii=False), now_iso()),
    )
    return cur.lastrowid
