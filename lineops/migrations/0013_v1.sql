-- 1.0: notificações no celular, tripulação do dia, pontualidade, marcos de carreira e cheque de rota.

-- Fila de envio para o celular (Telegram e ntfy). Uma linha por mensagem e canal.
-- status: PENDENTE | ENVIADO | ERRO | ADIADO (horário de silêncio) | IGNORADO
CREATE TABLE notify_outbox (
    message_id INTEGER NOT NULL REFERENCES ops_message(id),
    channel TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDENTE',
    tries INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    next_at TEXT,
    PRIMARY KEY (message_id, channel)
);
CREATE INDEX ix_notify_outbox_status ON notify_outbox(status);

-- Tripulação do dia (personagens fictícios). A mesma equipe segue junto enquanto a viagem não volta para a base.
CREATE TABLE crew_pairing (
    date TEXT PRIMARY KEY,
    fo TEXT NOT NULL,
    cabin TEXT NOT NULL,
    trip_start TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Pontualidade (D15) e o código de atraso informado por você.
CREATE TABLE flight_delay (
    flight_id INTEGER PRIMARY KEY REFERENCES flight(id),
    logbook_id INTEGER,
    out_delay_min INTEGER,
    in_delay_min INTEGER,
    code TEXT,
    remark TEXT,
    asked_at TEXT,
    answered_at TEXT
);

-- Marcos de carreira, calculados do seu diário.
CREATE TABLE milestone (
    key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT,
    logbook_id INTEGER,
    achieved_at TEXT NOT NULL
);

-- Cheque de rota (checador fictício; avaliação com os dados reais do voo).
-- status: AGENDADO | APROVADO | APROVADO_OBS | REPROVADO | NAO_REALIZADO
CREATE TABLE line_check (
    id INTEGER PRIMARY KEY,
    flight_id INTEGER REFERENCES flight(id),
    checker TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'AGENDADO',
    items TEXT NOT NULL DEFAULT '[]',
    announced_at TEXT NOT NULL,
    done_at TEXT,
    logbook_id INTEGER
);
