-- Voos Externos (fora das OPS): long haul real visto no ADS-B, voado em grupo com os amigos.

-- Catálogo de voos long haul reais vistos no ADS-B (adsb.lol, dados ODbL). Um registro por callsign.
CREATE TABLE ext_seen (
    callsign TEXT PRIMARY KEY,
    airline TEXT,
    number TEXT,
    type_icao TEXT,
    registration TEXT,
    adep TEXT NOT NULL,
    ades TEXT NOT NULL,
    adep_name TEXT,
    ades_name TEXT,
    adep_lat REAL,
    adep_lon REAL,
    ades_lat REAL,
    ades_lon REAL,
    km REAL,
    est_dep_utc TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    times_seen INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX ix_ext_seen_last ON ext_seen(last_seen);

-- Voo em grupo. status: PLANEJADO | VOADO | CANCELADO
CREATE TABLE ext_flight (
    id INTEGER PRIMARY KEY,
    callsign TEXT,
    airline TEXT,
    number TEXT,
    adep TEXT NOT NULL,
    ades TEXT NOT NULL,
    adep_name TEXT,
    ades_name TEXT,
    adep_lat REAL,
    adep_lon REAL,
    ades_lat REAL,
    ades_lon REAL,
    type_icao TEXT,
    real_type TEXT,
    km REAL,
    est_block_min INTEGER,
    dep_utc TEXT NOT NULL,
    stagger_min INTEGER NOT NULL DEFAULT 3,
    source TEXT NOT NULL,
    seen_at TEXT,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'PLANEJADO',
    created_at TEXT NOT NULL
);

-- Quem voa (você e os amigos), cada um no seu avião, e o resultado de cada um.
CREATE TABLE ext_member (
    id INTEGER PRIMARY KEY,
    flight_id INTEGER NOT NULL REFERENCES ext_flight(id),
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    network TEXT NOT NULL DEFAULT 'IVAO',
    callsign TEXT,
    type_icao TEXT,
    block_min INTEGER,
    landing_fpm INTEGER,
    landing_g REAL,
    remarks TEXT,
    done_at TEXT
);
CREATE INDEX ix_ext_member_flight ON ext_member(flight_id);
