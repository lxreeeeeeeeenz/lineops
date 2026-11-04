-- Aviso de pernoite no Discord: a perna marcada "ESSE VOO É PERNOITE" e a mensagem que foi para a webhook.
-- state ENVIADO (mensagem no ar), ERRO (o Discord recusou) ou REMOVIDO (desmarcada; a mensagem foi apagada).
CREATE TABLE overnight_post (
    id INTEGER PRIMARY KEY,
    leg_uid TEXT NOT NULL UNIQUE,
    leg_id INTEGER,
    flight_number TEXT,
    adep TEXT,
    ades TEXT,
    arr_utc TEXT,
    message_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('ENVIADO', 'ERRO', 'REMOVIDO')),
    error TEXT,
    payload TEXT,
    user_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
