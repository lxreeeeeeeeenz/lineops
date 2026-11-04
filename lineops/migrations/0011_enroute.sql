-- 0.12: voo lançado fora do horário da escala, mensagens em rota (canal ACARS) e informações de pernoite por aeroporto.

-- REAL   = horários que você voou de verdade (padrão de sempre)
-- ESCALA = você voou em outro momento e pediu para registrar nos horários da escala (o diário mostra isso)
ALTER TABLE logbook_entry ADD COLUMN time_basis TEXT NOT NULL DEFAULT 'REAL';
-- partida programada, guardada quando o voo foi lançado com a opção de ignorar o horário
ALTER TABLE logbook_entry ADD COLUMN sched_out_utc TEXT;

-- canal da mensagem: NULL = caixa da operação; 'acars' = chegou no avião durante o voo (impressora do ACARS)
ALTER TABLE ops_message ADD COLUMN channel TEXT;

-- O que a hotelaria informa sobre cada aeroporto de pernoite. Preenchido por você (ou pelos briefings que você importar).
-- O LineOps não inventa hotel: sem cadastro, a mensagem de pernoite diz que falta.
CREATE TABLE airport_info (
    icao TEXT PRIMARY KEY,
    hotel TEXT,
    transport TEXT,
    notes TEXT,
    source TEXT,
    updated_at TEXT NOT NULL
);
