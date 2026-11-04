-- Pedidos de troca de escala: o piloto pede, o Planejamento (ou o CCO, perto do voo) responde depois.
-- kind TROCA: outro voo do RPL no mesmo trecho. LIBERACAO: sair de uma ou mais pernas que fecham um ciclo.

CREATE TABLE swap_request (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('TROCA', 'LIBERACAO')),
    leg_ids TEXT NOT NULL,
    leg_uids TEXT NOT NULL,
    first_dep_utc TEXT NOT NULL,
    label TEXT NOT NULL,
    candidate TEXT,
    reason TEXT,
    chance INTEGER NOT NULL,
    factors TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL CHECK (state IN ('PENDENTE', 'APROVADO', 'NEGADO', 'CANCELADO', 'EXPIRADO')),
    sector TEXT NOT NULL,
    decide_at TEXT NOT NULL,
    roll INTEGER,
    decided_at TEXT,
    answer TEXT,
    new_leg_id INTEGER REFERENCES roster_leg(id),
    batch_id INTEGER REFERENCES import_batch(id),
    user_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_swap_state ON swap_request(state, decide_at);
