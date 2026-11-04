-- LineOps — esquema inicial. Datas e horas em UTC, texto ISO 8601 ("2026-11-01T18:02:54Z").
-- Convenção: valor desconhecido = NULL (nunca zero).

CREATE TABLE user (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    pw_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    pilot_code TEXT NOT NULL DEFAULT 'GOL001',
    created_at TEXT NOT NULL
);

CREATE TABLE auth_token (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('session', 'api')),
    name TEXT,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    last_used_at TEXT,
    revoked_at TEXT
);

CREATE TABLE setting (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    user_id INTEGER,
    action TEXT NOT NULL,
    entity TEXT,
    entity_id TEXT,
    detail TEXT
);

-- ------------------------------------------------------------------ importação do AeroScale (snapshot imutável)
CREATE TABLE import_batch (
    id INTEGER PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_name TEXT,
    source_sha256 TEXT,
    source_version TEXT,
    source_created_at TEXT,
    imported_at TEXT NOT NULL,
    stats TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE airline (
    code TEXT PRIMARY KEY,
    iata TEXT,
    name TEXT NOT NULL
);

CREATE TABLE roster_schedule (
    id INTEGER PRIMARY KEY,
    skey TEXT NOT NULL UNIQUE,
    airline TEXT NOT NULL,
    name TEXT,
    kind TEXT,
    start_date TEXT,
    end_date TEXT,
    params TEXT NOT NULL DEFAULT '{}',
    first_batch INTEGER NOT NULL REFERENCES import_batch(id),
    last_batch INTEGER NOT NULL REFERENCES import_batch(id)
);

CREATE TABLE roster_leg (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL UNIQUE,
    schedule_id INTEGER NOT NULL REFERENCES roster_schedule(id),
    seq INTEGER,
    airline TEXT NOT NULL,
    flight_number TEXT,
    type_icao TEXT,
    planned_type TEXT,
    adep TEXT NOT NULL,
    ades TEXT NOT NULL,
    dep_utc TEXT NOT NULL,
    arr_utc TEXT NOT NULL,
    eet_min INTEGER,
    data_origin TEXT NOT NULL,       -- exatamente como veio do AeroScale (REAL, PROJETADO, OBSERVADO, SUGESTAO, MANUAL...)
    activity TEXT NOT NULL DEFAULT '',
    reserve_uid TEXT,
    registration TEXT,               -- só se o AeroScale tiver uma aeronave atribuída; nunca inventada
    route TEXT,
    cruise_fl TEXT,
    speed TEXT,
    remarks TEXT,
    content_hash TEXT NOT NULL,
    first_batch INTEGER NOT NULL REFERENCES import_batch(id),
    last_batch INTEGER NOT NULL REFERENCES import_batch(id),
    changed_batch INTEGER REFERENCES import_batch(id),
    removed_batch INTEGER REFERENCES import_batch(id)
);
CREATE INDEX ix_leg_dep ON roster_leg(dep_utc);
CREATE INDEX ix_leg_sched ON roster_leg(schedule_id, seq);

CREATE TABLE roster_leg_change (
    id INTEGER PRIMARY KEY,
    leg_id INTEGER NOT NULL REFERENCES roster_leg(id) ON DELETE CASCADE,
    batch_id INTEGER NOT NULL REFERENCES import_batch(id),
    kind TEXT NOT NULL CHECK (kind IN ('CHANGED', 'REMOVED', 'RESTORED')),
    old_values TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE roster_reserve (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL UNIQUE,
    schedule_id INTEGER NOT NULL REFERENCES roster_schedule(id),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    days INTEGER,
    location TEXT,
    location_kind TEXT,
    status TEXT,
    act_start TEXT,
    act_days INTEGER,
    note TEXT,
    last_batch INTEGER NOT NULL REFERENCES import_batch(id)
);

CREATE TABLE roster_day (
    id INTEGER PRIMARY KEY,
    schedule_id INTEGER NOT NULL REFERENCES roster_schedule(id),
    date TEXT NOT NULL,
    kind TEXT NOT NULL,
    location TEXT,
    status TEXT,
    note TEXT,
    last_batch INTEGER NOT NULL REFERENCES import_batch(id),
    UNIQUE (schedule_id, date, kind)
);

-- ------------------------------------------------------------------ frota
CREATE TABLE aircraft (
    id INTEGER PRIMARY KEY,
    registration TEXT NOT NULL UNIQUE,
    airline TEXT NOT NULL,
    type_icao TEXT NOT NULL,
    model_name TEXT,
    variant TEXT,
    msn TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    config TEXT NOT NULL DEFAULT '{}',   -- cockpit, aviônicos, motorização, cabine: {campo: {"value":..., "source":...}}
    limits TEXT NOT NULL DEFAULT '{}',   -- {mtow_kg: {"value":..., "source":...}}; ausente = desconhecido
    baseline_hours REAL,
    baseline_cycles INTEGER,
    baseline_note TEXT,
    source TEXT NOT NULL DEFAULT 'MANUAL' CHECK (source IN ('MANUAL', 'AEROSCALE', 'CSV')),
    source_ref TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE unavailability (
    id INTEGER PRIMARY KEY,
    aircraft_id INTEGER NOT NULL REFERENCES aircraft(id) ON DELETE CASCADE,
    from_date TEXT NOT NULL,
    to_date TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE discrepancy (
    id INTEGER PRIMARY KEY,
    aircraft_id INTEGER NOT NULL REFERENCES aircraft(id) ON DELETE CASCADE,
    opened_at TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'ABERTA' CHECK (status IN ('ABERTA', 'DIFERIDA', 'CORRIGIDA')),
    restriction TEXT NOT NULL DEFAULT 'NENHUMA' CHECK (restriction IN ('NENHUMA', 'INFORMA', 'CONFIRMA', 'BLOQUEIA')),
    restriction_text TEXT,
    mel_ref TEXT,
    due_date TEXT,                     -- prazo do diferimento (definido por você)
    closed_at TEXT,
    source TEXT NOT NULL DEFAULT 'MANUAL'
);

-- ------------------------------------------------------------------ voos
CREATE TABLE flight (
    id INTEGER PRIMARY KEY,
    leg_id INTEGER UNIQUE REFERENCES roster_leg(id),
    kind TEXT NOT NULL CHECK (kind IN ('ROSTER', 'UNPLANNED')),
    airline TEXT NOT NULL,
    flight_number TEXT,
    adep TEXT NOT NULL,
    ades TEXT NOT NULL,
    dep_utc TEXT,
    arr_utc TEXT,
    type_icao TEXT,
    aircraft_id INTEGER REFERENCES aircraft(id),
    state TEXT NOT NULL CHECK (state IN ('RESERVADO', 'EM_VOO', 'PIREP_RECEBIDO', 'APROVADO', 'REJEITADO', 'CANCELADO')),
    pilot_id INTEGER REFERENCES user(id),
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX ix_flight_state ON flight(state, dep_utc);

CREATE TABLE flight_state_history (
    id INTEGER PRIMARY KEY,
    flight_id INTEGER NOT NULL REFERENCES flight(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT,
    user_id INTEGER
);

-- ------------------------------------------------------------------ ACARS
CREATE TABLE acars_session (
    id INTEGER PRIMARY KEY,
    flight_id INTEGER NOT NULL REFERENCES flight(id),
    token_id INTEGER REFERENCES auth_token(id),
    sim TEXT,
    client_version TEXT,
    started_at TEXT NOT NULL,
    last_seen_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('LIVE', 'COMPLETED', 'ABORTED')),
    phase TEXT,
    snapshot TEXT NOT NULL DEFAULT '{}',
    completed_at TEXT
);
CREATE INDEX ix_session_flight ON acars_session(flight_id, status);

CREATE TABLE track_point (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES acars_session(id) ON DELETE CASCADE,
    t TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    alt_ft INTEGER,
    gs_kt INTEGER,
    vs_fpm INTEGER,
    hdg INTEGER,
    phase TEXT,
    UNIQUE (session_id, t)
);

CREATE TABLE acars_message (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES acars_session(id) ON DELETE CASCADE,
    client_seq INTEGER NOT NULL,
    t TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    UNIQUE (session_id, client_seq)
);

-- ------------------------------------------------------------------ PIREP e logbook
CREATE TABLE pirep (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL UNIQUE REFERENCES acars_session(id),
    flight_id INTEGER NOT NULL REFERENCES flight(id),
    state TEXT NOT NULL CHECK (state IN ('PENDING', 'APPROVED', 'REJECTED')),
    submitted_at TEXT NOT NULL,
    data TEXT NOT NULL,                 -- medições do ACARS (JSON)
    warnings TEXT NOT NULL DEFAULT '[]',
    overrides TEXT NOT NULL DEFAULT '{}',
    review_notes TEXT,
    reviewed_at TEXT,
    reviewed_by INTEGER REFERENCES user(id)
);

CREATE TABLE logbook_entry (
    id INTEGER PRIMARY KEY,
    pirep_id INTEGER UNIQUE REFERENCES pirep(id),
    flight_id INTEGER REFERENCES flight(id),
    date TEXT NOT NULL,
    airline TEXT,
    flight_number TEXT,
    adep TEXT NOT NULL,
    ades TEXT NOT NULL,
    aircraft_id INTEGER REFERENCES aircraft(id),
    registration TEXT,
    type_icao TEXT,
    out_utc TEXT, off_utc TEXT, on_utc TEXT, in_utc TEXT,
    block_min INTEGER,
    air_min INTEGER,
    night_min INTEGER,
    landings INTEGER NOT NULL DEFAULT 1,
    fuel_out_kg REAL, fuel_in_kg REAL, fuel_used_kg REAL,
    landing_vs_fpm INTEGER, landing_g REAL,
    source TEXT NOT NULL,
    remarks TEXT,
    created_at TEXT NOT NULL,
    reversed_at TEXT
);
CREATE INDEX ix_logbook_date ON logbook_entry(date);

CREATE TABLE usage_ledger (
    id INTEGER PRIMARY KEY,
    aircraft_id INTEGER NOT NULL REFERENCES aircraft(id),
    pirep_id INTEGER REFERENCES pirep(id),
    kind TEXT NOT NULL CHECK (kind IN ('FLIGHT', 'ADJUST')),
    block_min INTEGER NOT NULL DEFAULT 0,
    air_min INTEGER NOT NULL DEFAULT 0,
    cycles INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    created_at TEXT NOT NULL,
    reversed_at TEXT
);
CREATE INDEX ix_ledger_ac ON usage_ledger(aircraft_id);
