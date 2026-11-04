-- Rede: quem da companhia apareceu na IVAO (amostras do whazzup público) e os pilotos que você marcou como colegas.
CREATE TABLE ivao_sighting (
    id INTEGER PRIMARY KEY,
    vid INTEGER NOT NULL,
    callsign TEXT NOT NULL,
    day TEXT NOT NULL,                 -- AAAA-MM-DD (UTC) da conexão
    dep TEXT, arr TEXT, aircraft TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    samples INTEGER NOT NULL DEFAULT 1,
    max_alt_ft INTEGER,
    airborne INTEGER NOT NULL DEFAULT 0,   -- foi visto no ar
    arrived INTEGER NOT NULL DEFAULT 0,    -- visto no solo perto do destino depois de voar
    last_state TEXT,
    UNIQUE (vid, callsign, day, dep, arr)
);
CREATE INDEX ix_sighting_vid ON ivao_sighting(vid, last_seen);
CREATE INDEX ix_sighting_seen ON ivao_sighting(last_seen);

CREATE TABLE ivao_friend (
    vid INTEGER PRIMARY KEY,
    nickname TEXT,
    note TEXT,
    created_at TEXT NOT NULL
);
