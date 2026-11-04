-- Toque obrigatório no fim de cada voo: pouso duro reportado pela tripulação (critério que a Boeing considera o mais confiável).
ALTER TABLE logbook_entry ADD COLUMN hard_landing_reported INTEGER NOT NULL DEFAULT 0;
