-- OFP importado do SimBrief. Guarda o plano inteiro (sem o HTML) para nada se perder.
CREATE TABLE ofp (
    id INTEGER PRIMARY KEY,
    flight_id INTEGER NOT NULL REFERENCES flight(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('simbrief-api', 'arquivo')),
    ofp_id TEXT,
    fetched_at TEXT NOT NULL,
    generated_at TEXT,
    units TEXT,
    airline TEXT, flight_number TEXT, adep TEXT, ades TEXT, altn TEXT,
    data TEXT NOT NULL,
    raw TEXT NOT NULL,
    warnings TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX ix_ofp_flight ON ofp(flight_id, id);
