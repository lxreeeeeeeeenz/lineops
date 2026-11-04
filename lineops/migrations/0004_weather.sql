-- Cache de METAR/TAF usado pelo weather.py do AeroScale (mesmo esquema dele).
CREATE TABLE weather_cache (
    kind TEXT NOT NULL, icao TEXT NOT NULL, raw TEXT, data TEXT, obs_time TEXT,
    source TEXT, fetched_at TEXT NOT NULL, error TEXT,
    PRIMARY KEY (kind, icao)
);
