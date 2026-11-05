"""A escala da carreira lida de um lugar só.

Até a 2.5, cada tela montava a própria consulta em roster_leg, e cada uma esquecia uma coisa diferente: a origem da perna
(data_origin, que decide a tripulação composta em etapa internacional longa), as pernas do sobreaviso acionado ao apagar
a escala, o local_date de pernas antigas. Daqui em diante, quem precisa de pernas pede aqui:

* legs(): pernas vivas (não removidas por troca, corte ou limpeza), com todas as colunas, o voo reservado e o local_date
  sempre preenchido; filtros por período, só voos (sem sobreaviso) e "depois de" um instante UTC.
* crew_legs(): o que o crew.analyze do AeroScale precisa (voos com origem, atividade e data local), igual para o painel
  de limites, as trocas e o pernoite.
"""
from __future__ import annotations

from .db import rows

_SEL = """SELECT l.*, COALESCE(l.local_date, substr(l.dep_utc,1,10)) AS ld, f.id AS flight_id, f.state AS flight_state
          FROM roster_leg l LEFT JOIN flight f ON f.leg_id=l.id AND f.state<>'CANCELADO'
          WHERE l.removed_batch IS NULL"""


def legs(conn, start: str | None = None, end: str | None = None, *, flights_only: bool = False,
         after_utc: str | None = None) -> list[dict]:
    q, args = _SEL, []
    if start:
        q += " AND COALESCE(l.local_date, substr(l.dep_utc,1,10)) >= ?"
        args.append(start)
    if end:
        q += " AND COALESCE(l.local_date, substr(l.dep_utc,1,10)) <= ?"
        args.append(end)
    if flights_only:
        q += " AND l.activity<>'SOBREAVISO'"
    if after_utc:
        q += " AND l.dep_utc > ?"
        args.append(after_utc)
    out = rows(conn.execute(q + " ORDER BY l.dep_utc, l.seq", args))
    for l in out:
        l["local_date"] = l.pop("ld")
    return out


CREW_KEYS = ("id", "uid", "flight_number", "adep", "ades", "dep_utc", "arr_utc", "eet_min", "type_icao", "activity", "local_date",
             "data_origin")


def crew_legs(conn, start: str, end: str) -> list[dict]:
    """Voos do período no formato do crew.analyze (o sobreaviso não é jornada de voo)."""
    return [{k: l.get(k) for k in CREW_KEYS} for l in legs(conn, start, end, flights_only=True)]
