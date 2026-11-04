-- LineOps: carreiras por companhia. O núcleo guarda o piloto e qual carreira cada sessão está usando.
-- (Esta migração roda em todos os bancos; as tabelas só têm uso no núcleo.)

-- carreira escolhida pela sessão (ou a carreira do token do ACARS)
ALTER TABLE auth_token ADD COLUMN career TEXT;

CREATE TABLE career (
    code TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    logo TEXT
);
