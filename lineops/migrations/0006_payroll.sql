-- Salários (opcional, desligado por padrão: setting payroll_enabled).
-- Contrato versionado por competência: vale a versão mais recente com valid_from <= mês calculado.
CREATE TABLE payroll_contract (
    id INTEGER PRIMARY KEY,
    valid_from TEXT NOT NULL,          -- AAAA-MM
    data TEXT NOT NULL,                -- parâmetros do contrato (JSON), com a fonte de cada valor
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_payroll_contract ON payroll_contract(valid_from, id);

-- Fechamento do mês: foto do contracheque calculado no momento (como o LOADSHEET emitido). Anular marca, não apaga.
CREATE TABLE payroll_closing (
    id INTEGER PRIMARY KEY,
    month TEXT NOT NULL,               -- AAAA-MM
    contract_id INTEGER REFERENCES payroll_contract(id),
    data TEXT NOT NULL,
    notes TEXT,
    closed_at TEXT NOT NULL,
    voided_at TEXT,
    void_reason TEXT
);
CREATE INDEX ix_payroll_closing ON payroll_closing(month, id);
