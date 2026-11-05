-- 3.0: índices para as consultas que mais rodam (voo da perna, pernas vivas, PIREP do voo, dias especiais e reservas).
CREATE INDEX IF NOT EXISTS ix_flight_leg ON flight(leg_id, state);
CREATE INDEX IF NOT EXISTS ix_leg_alive ON roster_leg(removed_batch, dep_utc);
CREATE INDEX IF NOT EXISTS ix_pirep_flight ON pirep(flight_id, state);
CREATE INDEX IF NOT EXISTS ix_roster_day_date ON roster_day(date);
CREATE INDEX IF NOT EXISTS ix_reserve_dates ON roster_reserve(start_date, end_date);
