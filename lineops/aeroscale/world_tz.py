"""Fuso horário IANA de aeroportos fora do Brasil (para os voos internacionais observados no ADS-B).

Países com um fuso só usam a tabela por país; países com vários fusos são resolvidos pela longitude
(aproximação suficiente para os aeroportos que as companhias brasileiras atendem). Sem fuso conhecido,
o horário fica em UTC e o app avisa.
"""
from __future__ import annotations

BY_COUNTRY = {
    # América do Sul, Central e Caribe
    "AR": "America/Argentina/Buenos_Aires", "UY": "America/Montevideo", "PY": "America/Asuncion",
    "BO": "America/La_Paz", "PE": "America/Lima", "CO": "America/Bogota", "VE": "America/Caracas",
    "GY": "America/Guyana", "SR": "America/Paramaribo", "GF": "America/Cayenne", "PA": "America/Panama",
    "CR": "America/Costa_Rica", "GT": "America/Guatemala", "SV": "America/El_Salvador", "HN": "America/Tegucigalpa",
    "NI": "America/Managua", "BZ": "America/Belize", "CU": "America/Havana", "DO": "America/Santo_Domingo",
    "HT": "America/Port-au-Prince", "JM": "America/Jamaica", "PR": "America/Puerto_Rico", "AW": "America/Aruba",
    "CW": "America/Curacao", "BS": "America/Nassau", "TT": "America/Port_of_Spain", "BB": "America/Barbados",
    "SX": "America/Lower_Princes", "KY": "America/Cayman",
    # Europa
    "GB": "Europe/London", "IE": "Europe/Dublin", "FR": "Europe/Paris", "DE": "Europe/Berlin", "IT": "Europe/Rome",
    "NL": "Europe/Amsterdam", "BE": "Europe/Brussels", "CH": "Europe/Zurich", "AT": "Europe/Vienna",
    "DK": "Europe/Copenhagen", "SE": "Europe/Stockholm", "NO": "Europe/Oslo", "FI": "Europe/Helsinki",
    "PL": "Europe/Warsaw", "CZ": "Europe/Prague", "HU": "Europe/Budapest", "GR": "Europe/Athens",
    "TR": "Europe/Istanbul", "LU": "Europe/Luxembourg",
    # África, Oriente Médio e Ásia
    "ZA": "Africa/Johannesburg", "AO": "Africa/Luanda", "MA": "Africa/Casablanca", "CV": "Atlantic/Cape_Verde",
    "SN": "Africa/Dakar", "NG": "Africa/Lagos", "EG": "Africa/Cairo", "ET": "Africa/Addis_Ababa",
    "AE": "Asia/Dubai", "QA": "Asia/Qatar", "SA": "Asia/Riyadh", "IL": "Asia/Jerusalem", "CN": "Asia/Shanghai",
    "JP": "Asia/Tokyo", "KR": "Asia/Seoul", "SG": "Asia/Singapore", "IN": "Asia/Kolkata", "NZ": "Pacific/Auckland",
}


def tz_for(country: str | None, lon: float | None) -> str | None:
    c = (country or "").upper()
    if c == "US":
        if lon is None:
            return "America/New_York"
        if lon > -87.5:
            return "America/New_York"
        if lon > -101:
            return "America/Chicago"
        if lon > -115:
            return "America/Denver" if lon > -111 else "America/Phoenix"
        if lon > -130:
            return "America/Los_Angeles"
        return "Pacific/Honolulu" if lon < -150 else "America/Anchorage"
    if c == "CA":
        if lon is None or lon > -90:
            return "America/Toronto"
        return "America/Winnipeg" if lon > -102 else ("America/Edmonton" if lon > -120 else "America/Vancouver")
    if c == "MX":
        if lon is not None and lon > -88.5:
            return "America/Cancun"
        if lon is not None and lon < -114:
            return "America/Tijuana"
        return "America/Mexico_City"
    if c == "CL":
        return "Pacific/Easter" if lon is not None and lon < -100 else "America/Santiago"
    if c == "PT":
        return "Atlantic/Azores" if lon is not None and lon < -20 else ("Atlantic/Madeira" if lon is not None and lon < -15 else "Europe/Lisbon")
    if c == "ES":
        return "Atlantic/Canary" if lon is not None and lon < -10 else "Europe/Madrid"
    if c == "EC":
        return "Pacific/Galapagos" if lon is not None and lon < -85 else "America/Guayaquil"
    if c == "AU":
        if lon is None or lon > 150:
            return "Australia/Sydney"
        return "Australia/Perth" if lon < 129 else "Australia/Adelaide"
    return BY_COUNTRY.get(c)
