-- LOADSHEET emitido (foto do cálculo no momento da emissão) e perfil de peso e balanceamento por tipo.
CREATE TABLE loadsheet (
    id INTEGER PRIMARY KEY,
    flight_id INTEGER NOT NULL REFERENCES flight(id) ON DELETE CASCADE,
    ofp_id INTEGER REFERENCES ofp(id),
    issued_at TEXT NOT NULL,
    edition INTEGER NOT NULL DEFAULT 1,
    data TEXT NOT NULL,
    notes TEXT
);
CREATE INDEX ix_loadsheet_flight ON loadsheet(flight_id, id);

-- Peso e balanceamento por tipo, lido do arquivo da própria aeronave do simulador (nada é inventado).
CREATE TABLE wb_profile (
    id INTEGER PRIMARY KEY,
    type_icao TEXT NOT NULL,
    model TEXT NOT NULL,
    source TEXT NOT NULL,
    source_note TEXT,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (type_icao, model)
);
