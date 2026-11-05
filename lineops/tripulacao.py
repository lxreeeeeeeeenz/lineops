"""Tripulação do dia: quem voa com você.

Numa companhia de verdade você não voa sozinho: tem um copiloto e a equipe de comissários, e a mesma tripulação costuma
seguir junta a viagem inteira (do dia em que sai da base até voltar). O LineOps monta isso:

* copiloto e comissários são PERSONAGENS FICTÍCIOS, sorteados de uma lista de nomes comuns e guardados por dia
  (a mesma pessoa continua amanhã se você pernoitou fora da base);
* quantos comissários: a regra do RBAC 121.391 (igual ao 14 CFR 121.391): acima de 100 assentos, 2 comissários mais 1
  para cada 50 assentos (ou fração) acima de 100. Assentos da configuração de cada companhia:
  GOL 737-800 e MAX 8: 186 → 4; 737-700: 138 → 3.
  LATAM Brasil (mapas de assento do seatmaps.com): A319 144 → 3; A320 e A320neo 180 → 4; A321 e A321neo 224 → 5;
  787-9 300 → 6; 777-300ER 410 → 9.
  Azul (seatmaps.com, conferido com a Wikipédia): ATR 72-600 70 → 2; E195 118 → 3; E195-E2 136 → 3; A320neo 174 → 4;
  A321neo 214 → 5; A330-200 271 → 6; A330-900neo 298 → 6. Tipo sem configuração conhecida na companhia usa a de outra
  companhia brasileira do mesmo tipo (e a tela diz). É o mínimo da regra: em voo longo a companhia leva mais gente, e o LineOps não inventa
  quantos;
* a tripulação fala com você: briefing na apresentação, o comentário do copiloto depois do toque e o fim da jornada.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

from . import equipe, journey, rules
from .db import jloads, now_iso, one, rows

FMT = "%Y-%m-%dT%H:%M:%SZ"
DISCLAIMER = "Tripulação fictícia do LineOps."
FO_NAMES = ("Lucas Ferreira", "Camila Rocha", "Diego Martins", "Renata Albuquerque", "Thiago Moura", "Beatriz Sales", "Felipe Antunes",
            "Larissa Pimentel", "Gustavo Rezende", "Natália Queiroz", "André Vasconcelos", "Mariana Teles", "Rodrigo Bastos",
            "Isabela Freitas", "Vinícius Prado", "Juliana Campos", "Eduardo Viana", "Tatiane Moreira")
CABIN_NAMES = ("Aline Costa", "Bruno Lacerda", "Carolina Mendes", "Daniel Ayres", "Elaine Furtado", "Fábio Nunes", "Gabriela Torres",
               "Henrique Lobo", "Ingrid Paiva", "Jéssica Amaral", "Kleber Souza", "Letícia Morais", "Mateus Cardoso", "Priscila Gomes",
               "Rafaela Duarte", "Sabrina Leal", "Tiago Holanda", "Vanessa Brito", "Wagner Pires", "Yasmin Serra", "Otávio Rangel",
               "Luana Barreto", "César Fontes", "Débora Matos")
# 3.0.1: muito mais gente. Além das listas acima, combinações de prenomes e sobrenomes comuns no Brasil (todas fictícias;
# qualquer semelhança é coincidência). Ordem fixa (semente fixa): a mesma escala sorteia as mesmas pessoas.
_FIRST_F = ("Ana", "Amanda", "Bianca", "Bruna", "Carla", "Cecília", "Clara", "Daniela", "Eduarda", "Fernanda", "Flávia", "Giovana",
            "Helena", "Isadora", "Joana", "Júlia", "Karina", "Laura", "Lívia", "Lorena", "Luíza", "Manuela", "Marina", "Melissa",
            "Mirela", "Nicole", "Olívia", "Paula", "Raquel", "Rebeca", "Sara", "Sofia", "Talita", "Valentina", "Vitória", "Yara",
            "Alícia", "Beatriz", "Débora", "Estela", "Gisele", "Heloísa", "Irene", "Jaqueline", "Lara", "Maitê", "Noemi", "Patrícia")
_FIRST_M = ("Arthur", "Bernardo", "Caio", "Cauã", "Davi", "Enzo", "Fernando", "Gabriel", "Guilherme", "Heitor", "Igor", "João",
            "Leonardo", "Lorenzo", "Marcelo", "Miguel", "Murilo", "Nicolas", "Otávio", "Pedro", "Rafael", "Ricardo", "Samuel", "Sérgio",
            "Vitor", "William", "Alexandre", "Augusto", "Benício", "Cristiano", "Douglas", "Emanuel", "Fabrício", "Gilberto", "Hugo",
            "Ivan", "Jorge", "Leandro", "Márcio", "Nelson", "Paulo", "Renan", "Saulo", "Teodoro", "Ulisses", "Wesley")
_SURN = ("Almeida", "Andrade", "Araújo", "Azevedo", "Barbosa", "Batista", "Bezerra", "Braga", "Cabral", "Caldeira", "Carvalho",
         "Castro", "Cavalcanti", "Coelho", "Correia", "Cunha", "Dias", "Esteves", "Farias", "Fernandes", "Figueiredo", "Fonseca",
         "Gonçalves", "Guimarães", "Ibrahim", "Lima", "Lopes", "Macedo", "Machado", "Magalhães", "Marques", "Medeiros", "Melo",
         "Miranda", "Monteiro", "Nascimento", "Neves", "Nogueira", "Oliveira", "Pacheco", "Peixoto", "Pereira", "Pinheiro", "Ramos",
         "Reis", "Ribeiro", "Rios", "Sampaio", "Santana", "Santos", "Siqueira", "Silveira", "Soares", "Tavares", "Teixeira",
         "Valente", "Vieira", "Xavier", "Yamamoto", "Zanetti", "Kowalski", "Okada", "Bittencourt", "Brandão", "Damasceno",
         "Espíndola", "Frota", "Galvão", "Hoffmann", "Jardim", "Leite", "Mourão", "Novaes", "Portela", "Quintela", "Sobral")


def _combos(seed: int, n: int, exclude: tuple) -> tuple:
    rnd = random.Random(seed)
    first = list(_FIRST_F + _FIRST_M)
    out, seen = [], set(exclude)
    while len(out) < n:
        nm = f"{rnd.choice(first)} {rnd.choice(_SURN)}"
        if nm not in seen:
            seen.add(nm)
            out.append(nm)
    return tuple(out)


FO_NAMES = FO_NAMES + _combos(79, 300, FO_NAMES + CABIN_NAMES)
CABIN_NAMES = CABIN_NAMES + _combos(121, 700, FO_NAMES + CABIN_NAMES)
SEATS = {"B738": 186, "B38M": 186, "B737": 138, "B37M": 138, "B739": 189, "B39M": 189}       # GOL
SEATS_BY_AIRLINE = {
    "GLO": SEATS,
    "TAM": {"A319": 144, "A320": 180, "A20N": 180, "A321": 224, "A21N": 224, "B789": 300, "B77W": 410},   # seatmaps.com (LATAM Brasil)
    "AZU": {"AT76": 70, "E195": 118, "E295": 136, "A20N": 174, "A21N": 214, "A332": 271, "A339": 298},   # seatmaps.com (Azul)
}
SOURCE = "RBAC 121.391 (14 CFR 121.391): acima de 100 assentos, 2 comissários + 1 a cada 50 assentos acima de 100"


def seats_of(type_icao: str | None, airline: str | None = None) -> int | None:
    t = (type_icao or "").upper()
    own = SEATS_BY_AIRLINE.get((airline or "").upper()) or {}
    return own.get(t) or next((m[t] for m in SEATS_BY_AIRLINE.values() if t in m), None)


def cabin_count(type_icao: str | None, airline: str | None = None) -> int:
    seats = seats_of(type_icao, airline) or 186
    if seats <= 50:
        return 1
    if seats <= 100:
        return 2
    return 2 + -(-(seats - 100) // 50)


def first_name(n: str) -> str:
    return (n or "").split()[0] if n else ""


def _day_flights(conn, date):
    return rows(conn.execute("""SELECT * FROM roster_leg WHERE removed_batch IS NULL AND activity NOT IN ('SOBREAVISO')
                                AND COALESCE(local_date, substr(dep_utc,1,10))=? ORDER BY dep_utc""", (date,)))


def _away_after(conn, date) -> bool:
    """O dia terminou fora da base (pernoite): a tripulação continua junta no dia seguinte."""
    fl = _day_flights(conn, date)
    if not fl:
        return False
    p, _s = journey.schedule_params(conn, date)
    base = p.get("base_resolved") or p.get("base")
    return bool(base) and fl[-1]["ades"] != base or (not base and fl[-1]["ades"] != fl[0]["adep"])


def pairing(conn, date: str, *, create=True) -> dict | None:
    """Tripulação do dia (só em dia com voo). Fica guardada: o mesmo dia sempre traz as mesmas pessoas."""
    r = one(conn.execute("SELECT * FROM crew_pairing WHERE date=?", (date,)))
    if r:
        return _out(r)
    fl = _day_flights(conn, date)
    if not fl or not create:
        return None
    flown = [l for l in fl if l.get("activity") != "EXTRA"] or fl
    n = max(cabin_count(l.get("type_icao"), l.get("airline")) for l in flown)     # o maior avião do dia (que você voa) manda
    prev = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    pr = one(conn.execute("SELECT * FROM crew_pairing WHERE date=?", (prev,)))
    if pr and _away_after(conn, prev):
        fo, cabin, trip = pr["fo"], jloads(pr["cabin"], []), pr["trip_start"]
        if len(cabin) != n:                                  # trocou de equipamento no meio da viagem: ajusta a cabine
            cabin = _cabin(random.Random(f"lineops-cabin-{date}"), n, exclude={c["name"] for c in cabin})
    else:
        rnd = random.Random(f"lineops-crew-{date}")
        fo, cabin, trip = rnd.choice(FO_NAMES), _cabin(rnd, n), date
    conn.execute("INSERT OR IGNORE INTO crew_pairing(date, fo, cabin, trip_start, created_at) VALUES (?,?,?,?,?)",
                 (date, fo, json.dumps(cabin, ensure_ascii=False), trip, now_iso()))
    return _out(one(conn.execute("SELECT * FROM crew_pairing WHERE date=?", (date,))))


def _cabin(rnd, n, exclude=()):
    names = [x for x in CABIN_NAMES if x not in exclude]
    rnd.shuffle(names)
    return [{"name": names[i], "role": "Chefe de cabine" if i == 0 else "Comissário(a)"} for i in range(n)]


def _out(r) -> dict:
    cabin = jloads(r["cabin"], [])
    return {"date": r["date"], "fo": {"name": r["fo"], "role": "Copiloto"}, "cabin": cabin, "chief": cabin[0] if cabin else None,
            "trip_start": r["trip_start"], "trip_day": (datetime.strptime(r["date"], "%Y-%m-%d") - datetime.strptime(r["trip_start"], "%Y-%m-%d")).days + 1,
            "cabin_rule": SOURCE, "disclaimer": DISCLAIMER}


def crew_line(conn, date) -> str | None:
    c = pairing(conn, date)
    if not c:
        return None
    return f"TRIPULAÇÃO: FO {c['fo']['name']} · chefe de cabine {c['chief']['name']} · " + ", ".join(x["name"] for x in c["cabin"][1:])


def for_flight(conn, f) -> dict | None:
    if not f or not f.get("leg_id"):
        return None
    leg = one(conn.execute("SELECT local_date, dep_utc FROM roster_leg WHERE id=?", (f["leg_id"],)))
    return pairing(conn, (leg["local_date"] or leg["dep_utc"][:10])) if leg else None


def say(conn, key, who: dict, subject, body, *, flight_id=None, now=None, level="info", channel=None) -> bool:
    return equipe.post(conn, key, "tripulacao", subject, body, flight_id=flight_id, now=now, level=level, channel=channel,
                       sender=who["name"], sender_title=who.get("role"))


# ------------------------------------------------------------------ falas
def _greet(conn, now, icao):
    lab = rules.local_label(conn, datetime.strptime(now, FMT).replace(tzinfo=timezone.utc), icao)
    h = int(lab["hhmm"][:2]) if lab else 12
    return "Bom dia" if 4 <= h < 12 else "Boa tarde" if h < 18 else "Boa noite"


def clock(conn, now: str) -> None:
    """Briefing na apresentação: o copiloto chega na sala de briefing e apresenta a tripulação (uma vez por dia)."""
    from .journey import _duty_times, schedule_params
    nowd = datetime.strptime(now, FMT).replace(tzinfo=timezone.utc)
    lo, hi = (nowd - timedelta(hours=3)).strftime(FMT), (nowd + timedelta(hours=3)).strftime(FMT)
    legs = rows(conn.execute("""SELECT l.*, f.id AS fid, f.state AS fstate, a.registration AS reg FROM roster_leg l
                                LEFT JOIN flight f ON f.leg_id=l.id AND f.state<>'CANCELADO' LEFT JOIN aircraft a ON a.id=f.aircraft_id
                                WHERE l.removed_batch IS NULL AND l.activity NOT IN ('SOBREAVISO') AND l.dep_utc BETWEEN ? AND ? ORDER BY l.dep_utc""", (lo, hi)))
    seen = set()
    for l in legs:
        day = l["local_date"] or l["dep_utc"][:10]
        if day in seen:
            continue
        seen.add(day)
        dl = _day_flights(conn, day)
        if not dl or dl[0]["id"] != l["id"]:
            continue                                            # o briefing é antes da primeira perna do dia
        if l["fstate"] in ("APROVADO", "PIREP_RECEBIDO"):
            continue                                            # dia já voado (fora do horário)
        p, sched = schedule_params(conn, day)
        rep = _duty_times(p, l["dep_utc"], None, (sched or {}).get("airline"))["report_utc"] or \
            (datetime.strptime(l["dep_utc"], FMT) - timedelta(minutes=60)).replace(tzinfo=timezone.utc).strftime(FMT)
        if not rep <= now <= l["dep_utc"]:
            continue
        c = pairing(conn, day)
        if not c:
            continue
        o = one(conn.execute("SELECT data FROM ofp WHERE flight_id=? ORDER BY id DESC LIMIT 1", (l["fid"],))) if l["fid"] else None
        od = jloads(o["data"], {}) if o else {}
        chk = one(conn.execute("SELECT checker FROM line_check WHERE flight_id=? AND status='AGENDADO'", (l["fid"],))) if l["fid"] else None
        lines = [f"{_greet(conn, now, l['adep'])}, comandante! {first_name(c['fo']['name'])} aqui, já estou na sala de briefing em {l['adep']}."
                 + (f" Dia {c['trip_day']} da viagem." if c["trip_day"] > 1 else ""), ""]
        lines.append("HOJE: " + " · ".join(f"{x['flight_number'] or '—'} {x['adep']}→{x['ades']} {x['dep_utc'][11:16]}Z" for x in dl))
        if l["reg"]:
            lines.append(f"AERONAVE: {l['reg']}")
        if od.get("fuel_ramp"):
            lines.append(f"OFP DA PRIMEIRA: {int(od['fuel_ramp']):,} kg na partida".replace(",", ".")
                         + (f", FL{round(od['initial_altitude'] / 100):03d}" if od.get("initial_altitude") else "")
                         + (f", {int(od['pax_count'])} pax" if od.get("pax_count") else ""))
        elif l["fid"]:
            lines.append("OFP: ainda não chegou. O DOV está esperando o seu plano do SimBrief.")
        lines.append(f"CABINE: {c['chief']['name']} (chefe) + " + ", ".join(first_name(x["name"]) for x in c["cabin"][1:]))
        if chk:
            lines.append(f"E hoje tem cheque de rota: o {chk['checker']} vai no jumpseat. Tranquilo, é só voar como sempre.")
        lines += ["", "Te espero para o briefing."]
        say(conn, f"crew_brief:{day}", c["fo"], f"Briefing em {l['adep']} · {day[8:10]}/{day[5:7]}", "\n".join(lines),
            flight_id=l["fid"], now=max(rep, lo), level="acao")


def after_landing(conn, f, e, ev_tier, now) -> None:
    """O copiloto comenta o toque e, no último voo do dia, fecha a jornada."""
    c = for_flight(conn, f)
    if not c:
        return
    fo = c["fo"]
    seed = e["id"] or 0
    v = abs(int(e["landing_vs_fpm"])) if e["landing_vs_fpm"] is not None else None
    talk = {
        "manteiga": [f"Que pouso, comandante! {v} fpm, nem senti tocar. Só não conta pro Walter que a gente flutuou um pouco 😄",
                     f"{v} fpm. A cabine inteira bateu palma lá atrás, a {first_name(c['chief']['name'])} me disse no interfone."],
        "suave": [f"Bonito, {v} fpm. Assim que eu gosto.", f"Pouso redondo, {v} fpm. Pode deixar que eu faço o checklist."],
        "normal": [f"Pouso dentro do padrão, {v} fpm. Tudo certo por aqui.", "Toque normal. Checklist depois do pouso completo."],
        "firme": [f"Firmezinho, hein? {v} fpm. Acordou a cabine 😅", f"{v} fpm... o Walter vai mandar mensagem. Pelo menos não flutuamos."],
        "duro": [f"Eita, comandante... {v} fpm. Tá todo mundo bem lá atrás, a {first_name(c['chief']['name'])} confirmou. Vamos fazer o relatório juntos.",
                 f"{v} fpm. Respira. Vamos anotar tudo da aproximação enquanto está fresco, o relatório vai ser pedido."],
        "severo": [f"Comandante, {v} fpm. Ninguém se machucou, a cabine confirmou. Vou chamar a manutenção e começar o relatório."],
    }.get(ev_tier)
    if talk:
        say(conn, f"crew_td:{e['id']}", fo, f"{f['flight_number'] or 'Voo'}: depois do pouso em {f['ades']}", talk[seed % len(talk)],
            flight_id=f["id"], now=now)
    # fim da jornada: último voo do dia
    leg = one(conn.execute("SELECT * FROM roster_leg WHERE id=?", (f["leg_id"],))) if f.get("leg_id") else None
    if not leg:
        return
    day = leg["local_date"] or leg["dep_utc"][:10]
    dl = _day_flights(conn, day)
    if not dl or dl[-1]["id"] != leg["id"]:
        return
    nxt = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    together = _away_after(conn, day) and _day_flights(conn, nxt)
    pick = one(conn.execute("SELECT due_utc FROM stay_event WHERE key=? AND status='PENDENTE'", (f"stay_pickup:{f['id']}",)))
    from .enroute import _lt
    if together:
        body = (f"Valeu pela jornada, comandante. Amanhã estamos juntos de novo"
                + (f": te vejo no lobby às {_lt(conn, pick['due_utc'], f['ades'])}." if pick else ".") + " Bom descanso!")
    else:
        body = "Fim de viagem pra mim. Foi um prazer voar com você, comandante. Até a próxima escala!"
    say(conn, f"crew_end:{day}", fo, "Fim da jornada", body, flight_id=f["id"], now=now)


def colleagues(conn, limit=10) -> list[dict]:
    """Com quem você mais voou (dias de voo já passados ou voados)."""
    out = {}
    for r in rows(conn.execute("""SELECT p.date, p.fo, p.cabin FROM crew_pairing p WHERE EXISTS (
                                     SELECT 1 FROM roster_leg l JOIN flight f ON f.leg_id=l.id
                                     WHERE COALESCE(l.local_date, substr(l.dep_utc,1,10))=p.date AND f.state='APROVADO')""")):
        for name, role in [(r["fo"], "Copiloto")] + [(c["name"], c["role"]) for c in jloads(r["cabin"], [])]:
            o = out.setdefault(name, {"name": name, "role": role, "days": 0, "last": r["date"]})
            o["days"] += 1
            o["last"] = max(o["last"], r["date"])
    return sorted(out.values(), key=lambda x: (-x["days"], x["name"]))[:limit]
