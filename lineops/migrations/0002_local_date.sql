-- Dia da escala e diagnósticos, como o AeroScale faz.
-- local_date: data LOCAL da partida no aeroporto de origem (o AeroScale agrupa a escala por esse campo).
ALTER TABLE roster_leg ADD COLUMN local_date TEXT;
ALTER TABLE roster_schedule ADD COLUMN diagnostics TEXT NOT NULL DEFAULT '[]';
CREATE INDEX ix_leg_local ON roster_leg(local_date);
