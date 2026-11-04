-- "Já fiz esse voo": lançamento declarado pelo piloto, sem PIREP. Liga o lançamento de horas ao diário para o estorno.
ALTER TABLE logbook_entry ADD COLUMN network TEXT;
ALTER TABLE usage_ledger ADD COLUMN logbook_id INTEGER REFERENCES logbook_entry(id);
