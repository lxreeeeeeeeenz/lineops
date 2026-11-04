-- Equipe da operação (personagens fictícios, editáveis), caixa de mensagens e despacho assinado pelo DOV e pelo comandante.
CREATE TABLE ops_staff (
    sector TEXT NOT NULL,
    shift TEXT NOT NULL DEFAULT 'todos',      -- manha | tarde | noite | todos
    name TEXT NOT NULL,
    title TEXT NOT NULL,
    PRIMARY KEY (sector, shift)
);

CREATE TABLE ops_message (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,                 -- idempotência: o mesmo evento nunca vira duas mensagens
    ts TEXT NOT NULL,
    sector TEXT NOT NULL,
    sender TEXT NOT NULL,
    sender_title TEXT,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    flight_id INTEGER REFERENCES flight(id),
    level TEXT NOT NULL DEFAULT 'info',       -- info | acao | alerta
    ack_required INTEGER NOT NULL DEFAULT 0,
    acked_at TEXT,
    read_at TEXT,
    links TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX ix_ops_message_ts ON ops_message(ts);
CREATE INDEX ix_ops_message_flight ON ops_message(flight_id);

CREATE TABLE dispatch_release (
    id INTEGER PRIMARY KEY,
    flight_id INTEGER NOT NULL REFERENCES flight(id) ON DELETE CASCADE,
    ofp_id INTEGER REFERENCES ofp(id),
    status TEXT NOT NULL CHECK (status IN ('LIBERADO', 'RETIDO')),
    reasons TEXT NOT NULL DEFAULT '[]',
    dov_name TEXT NOT NULL,
    dov_signed_at TEXT NOT NULL,
    pic_signed_at TEXT,
    superseded_at TEXT
);
CREATE INDEX ix_release_flight ON dispatch_release(flight_id, id);
