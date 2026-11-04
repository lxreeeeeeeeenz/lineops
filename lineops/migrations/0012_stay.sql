-- 0.13: pernoite imersivo. Hotel escolhido por você no OpenStreetMap, rota de carro pelo OSRM e a linha do tempo do
-- pernoite (carro no desembarque, check-in, despertar, carro na portaria) entregue na hora certa.

ALTER TABLE airport_info ADD COLUMN hotel_address TEXT;
ALTER TABLE airport_info ADD COLUMN hotel_lat REAL;
ALTER TABLE airport_info ADD COLUMN hotel_lon REAL;
-- referência do OpenStreetMap, ex.: node/123456
ALTER TABLE airport_info ADD COLUMN hotel_ref TEXT;
-- rota aeroporto → hotel calculada pelo OSRM (minutos e km)
ALTER TABLE airport_info ADD COLUMN route_min INTEGER;
ALTER TABLE airport_info ADD COLUMN route_km REAL;
ALTER TABLE airport_info ADD COLUMN route_src TEXT;
ALTER TABLE airport_info ADD COLUMN route_at TEXT;

-- Mensagens do pernoite agendadas para a hora certa (o motor da equipe entrega quando chega a hora).
-- status: PENDENTE | ENVIADO | VENCIDO (passou da hora há muito) | CANCELADO (a escala mudou ou o voo foi estornado)
CREATE TABLE stay_event (
    key TEXT PRIMARY KEY,
    flight_id INTEGER REFERENCES flight(id),
    due_utc TEXT NOT NULL,
    sector TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    channel TEXT,
    ref TEXT,
    status TEXT NOT NULL DEFAULT 'PENDENTE',
    created_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX ix_stay_event_due ON stay_event(status, due_utc);
