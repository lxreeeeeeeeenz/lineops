"""Salários (opcional): contracheque simulado do aeronauta a partir da escala do AeroScale e do diário do LineOps.

Nada aqui é inventado. Os valores padrão vêm da CCT 2025/2026 da aviação regular (SNA × SNEA) com a cláusula
de cada um; o que você mudar fica marcado como informado por você. As horas saem da mesma escala que o AeroScale
gerou e as jornadas (apresentação, encerramento, pernoite fora da base) do crew.analyze do próprio AeroScale.

Base:   programado = pernas da escala importada (data local da partida, como o AeroScale data a perna);
        realizado  = voos aprovados no diário (calço a calço). Reserva e sobreaviso não são voados no simulador,
                     então vêm da escala nas duas bases.
Regras: parte fixa (salário base + compensação orgânica, cl. 2.2 e 3.2.3); garantia de 54 h, com reserva paga
        como hora de voo e sobreaviso a 1/3 (cl. 3.2.7 e 3.2.8); horas em domingos e feriados da base em dobro
        (cl. 3.2.6); DSR com base em 8 folgas (cl. 3.4.8); diárias de alimentação nas janelas da cl. 2.3.
Impostos: INSS (Portaria Interministerial MPS/MF nº 13/2026) e IRRF (tabela de 2026 + redutor da Lei 15.270/2025).
        É estimativa: cada empresa tem acordo próprio e o RH pode tratar parcelas de outro jeito.
"""
from __future__ import annotations

import calendar
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from . import rules
from .aeroscale import career, catalog, crew
from .aeroscale.airports import get_tz
from .db import audit, jloads, now_iso, one, rows, tx
from .errors import Conflict, FGVError, NotFound

UTC = timezone.utc
FMT = "%Y-%m-%dT%H:%M:%SZ"
SETTING = "payroll_enabled"
DEFAULT_TZ = "America/Sao_Paulo"          # CCT cl. 3.3.14 usa o horário de Brasília quando falta referência

# ------------------------------------------------------------------ CCT 2025/2026 (SNA × SNEA), aviação regular
CCT_ID = "cct-2025-2026"
CCT = {
    "id": CCT_ID,
    "label": "CCT 2025/2026 — aviação regular (SNA × SNEA)",
    "valid_from": "2025-12-01", "valid_to": "2026-11-30",
    "url": "https://aeronautas.org.br/wp-content/uploads/2026/01/2025.12.30-CCT-25-26-AERONAUTAS-para-site-1.pdf",
}
ROLES = ("Comandante", "Copiloto")
CATEGORIES = {"tp_mono": "Monomotor turbo-hélice", "tp_bi": "Bimotor turbo-hélice",
              "jato_nb": "Jato narrowbody", "jato_wb": "Jato widebody"}
# família de tipo do catalog do AeroScale -> categoria da CCT
FAMILY_CATEGORY = {"A320": "jato_nb", "B737": "jato_nb", "E-JET": "jato_nb",
                   "A330": "jato_wb", "A350": "jato_wb", "B767": "jato_wb", "B777": "jato_wb", "B787": "jato_wb",
                   "ATR": "tp_bi", "C208": "tp_mono"}
# cl. 2.2: piso = salário base + compensação orgânica. "Qualquer outro tipo" cobre o monomotor turbo-hélice.
PISO = {("Comandante", "tp_bi"): 11643.24, ("Comandante", "jato_nb"): 11643.24, ("Comandante", "jato_wb"): 11643.24,
        ("Comandante", "tp_mono"): 6670.80,
        ("Copiloto", "tp_bi"): 6069.34, ("Copiloto", "jato_nb"): 6069.34, ("Copiloto", "jato_wb"): 6069.34,
        ("Copiloto", "tp_mono"): 5641.81}
# cl. 3.2.8: valor da hora de voo acima da 54ª
HORA = {("Comandante", "tp_mono"): 56.28, ("Comandante", "tp_bi"): 127.31, ("Comandante", "jato_nb"): 127.31,
        ("Comandante", "jato_wb"): 147.43,
        ("Copiloto", "tp_mono"): 13.39, ("Copiloto", "tp_bi"): 56.28, ("Copiloto", "jato_nb"): 56.28, ("Copiloto", "jato_wb"): 56.28}
MEAL_INTL = {"NA": ["USD", 23.00], "SA": ["USD", 21.00], "EU": ["EUR", 23.00], "UK": ["GBP", 23.00], "OT": ["USD", 21.00]}
REGION_LABEL = {"NA": "América do Norte e México", "SA": "América do Sul e Caribe", "EU": "Europa", "UK": "Inglaterra",
                "OT": "Demais países"}
MEALS = (("cafe", "Café da manhã", time(5, 0), time(8, 0)), ("almoco", "Almoço", time(11, 0), time(13, 0)),
         ("jantar", "Jantar", time(19, 0), time(20, 0)), ("ceia", "Ceia", time(0, 0), time(1, 0)))

SRC_CCT = {
    "fixed_total": "CCT cl. 2.2 (piso = salário base + compensação orgânica)",
    "organic_pct": "CCT cl. 3.2.3",
    "hour_rate": "CCT cl. 3.2.8",
    "guarantee_h": "CCT cl. 3.2.7 e 3.2.8 (garantia de 54 h)",
    "reserve_hour_value": "CCT cl. 3.2.7 I (reserva = hora de voo)",
    "standby_hour_value": "CCT cl. 3.2.7 II (sobreaviso = 1/3 da hora)",
    "reserve_hours_per_day": "teto da reserva na CCT cl. 3.3.12 (3 a 6 h); o AeroScale não informa o horário",
    "sunday_holiday_double": "CCT cl. 3.2.6",
    "dsr_rest_days": "CCT cl. 3.4.8",
    "meal_value": "CCT cl. 2.3",
    "breakfast_pct": "CCT cl. 2.3 §1",
    "breakfast_in_hotel": "CCT cl. 2.3 §1 (café incluso na conta do hotel não é devido)",
    "meal_intl": "CCT cl. 2.3.1",
    "food_voucher": "CCT cl. 2.4 (após 90 dias de experiência)",
}

# ------------------------------------------------------------------ impostos 2026
INSS_SOURCE = "Portaria Interministerial MPS/MF nº 13, de 09/01/2026 (vigência 01/01/2026)"
INSS_BRACKETS = ((1621.00, 0.075), (2902.84, 0.09), (4354.27, 0.12), (8475.55, 0.14))
IRRF_SOURCE = "Tabela mensal do IRRF de 2026 (Lei 15.191/2025) e redutor da Lei 15.270/2025"
IRRF_BRACKETS = ((2428.80, 0.0, 0.0), (2826.65, 0.075, 182.16), (3751.05, 0.15, 394.16), (4664.68, 0.225, 675.49),
                 (float("inf"), 0.275, 908.73))
IRRF_DEPENDENT = 189.59
IRRF_SIMPLIFIED = 607.20
TAX_YEAR = 2026


def money(x) -> float:
    return float(Decimal(str(x or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def brl(x) -> str:
    s = f"{money(x):,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def hhmm(minutes) -> str:
    m = int(round(minutes or 0))
    sign = "-" if m < 0 else ""
    m = abs(m)
    return f"{sign}{m // 60}:{m % 60:02d}"


def parse(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, FMT).replace(tzinfo=UTC)
    except ValueError:
        try:
            d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=UTC)
        except ValueError:
            return None


# ------------------------------------------------------------------ liga/desliga
def enabled(conn) -> bool:
    r = conn.execute("SELECT value FROM setting WHERE key=?", (SETTING,)).fetchone()
    return bool(r and r[0] == "1")


def set_enabled(conn, on: bool, user_id=None) -> bool:
    with tx(conn):
        conn.execute("INSERT INTO setting(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (SETTING, "1" if on else "0"))
        audit(conn, user_id, "payroll.enable" if on else "payroll.disable", "setting", SETTING)
    return on


def require(conn):
    if not enabled(conn):
        raise Conflict("O módulo de salários está desligado. Ative em Ajustes.", code="salarios_desligado")


# ------------------------------------------------------------------ contrato
def preset(role: str, category: str) -> dict:
    """Contrato pela CCT 2025/2026 para a função e a categoria de aeronave."""
    if role not in ROLES or category not in CATEGORIES:
        raise FGVError("Função ou categoria inválida.", code="campo_invalido")
    hora = HORA[(role, category)]
    return {"preset": CCT_ID, "role": role, "category": category,
            "fixed_total": PISO[(role, category)], "organic_pct": 20.0, "hour_rate": hora, "guarantee_h": 54.0,
            "reserve_hour_value": hora, "standby_hour_value": money(hora / 3), "reserve_hours_per_day": 6.0,
            "sunday_holiday_double": True, "extra_holidays": [], "dsr_enabled": True, "dsr_rest_days": 8,
            "night_pct": 0.0, "hazard_pct": 0.0, "meal_value": 109.95, "breakfast_pct": 25.0, "breakfast_in_hotel": True,
            "meal_intl": {k: list(v) for k, v in MEAL_INTL.items()}, "food_voucher": 591.95, "other_items": [],
            "dependents": 0, "taxes_enabled": True}


def presets() -> list[dict]:
    return [{"role": r, "category": c, "category_label": CATEGORIES[c], "values": preset(r, c)} for r in ROLES for c in CATEGORIES]


def _num(v, name, lo=0.0, hi=1e7, allow_none=False):
    if v in (None, ""):
        if allow_none:
            return None
        raise FGVError(f"Informe {name}.", code="campo_invalido")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise FGVError(f"{name}: número inválido.", code="campo_invalido")
    if not (lo <= x <= hi):
        raise FGVError(f"{name}: fora da faixa ({lo:g} a {hi:g}).", code="campo_invalido")
    return x


def normalize(data: dict) -> dict:
    """Valida o contrato e marca a fonte de cada valor (CCT quando igual ao preset, senão informado por você)."""
    role, cat = data.get("role"), data.get("category")
    base = preset(role, cat)
    out = dict(base)
    for k in base:
        if k in data:
            out[k] = data[k]
    out["preset"] = data.get("preset") or CCT_ID
    n = _num
    out["fixed_total"] = money(n(out["fixed_total"], "a parte fixa", 0.01, 1e6))
    out["organic_pct"] = n(out["organic_pct"], "o percentual da compensação orgânica", 0, 100)
    out["hour_rate"] = money(n(out["hour_rate"], "o valor da hora", 0, 1e4))
    out["guarantee_h"] = n(out["guarantee_h"], "a garantia de horas", 0, 300)
    out["reserve_hour_value"] = money(n(out["reserve_hour_value"], "o valor da hora de reserva", 0, 1e4))
    out["standby_hour_value"] = money(n(out["standby_hour_value"], "o valor da hora de sobreaviso", 0, 1e4))
    out["reserve_hours_per_day"] = n(out["reserve_hours_per_day"], "as horas por dia de reserva", 0, 24)
    out["dsr_rest_days"] = int(n(out["dsr_rest_days"], "as folgas do DSR", 1, 20))
    out["night_pct"] = n(out["night_pct"], "o adicional noturno", 0, 200)
    out["hazard_pct"] = n(out["hazard_pct"], "o adicional de periculosidade", 0, 100)
    out["meal_value"] = money(n(out["meal_value"], "o valor da refeição", 0, 1e4))
    out["breakfast_pct"] = n(out["breakfast_pct"], "o percentual do café", 0, 100)
    out["food_voucher"] = None if out["food_voucher"] in (None, "") else money(n(out["food_voucher"], "o vale-alimentação", 0, 1e5))
    out["dependents"] = int(n(out["dependents"], "os dependentes", 0, 30))
    for k in ("sunday_holiday_double", "dsr_enabled", "breakfast_in_hotel", "taxes_enabled"):
        out[k] = bool(out[k])
    hol = []
    for d in out.get("extra_holidays") or []:
        d = str(d).strip()
        try:
            (datetime.strptime(d, "%Y-%m-%d") if len(d) == 10 else datetime.strptime("2000-" + d, "%Y-%m-%d"))
        except ValueError:
            raise FGVError(f"Feriado inválido: {d} (use MM-DD ou AAAA-MM-DD).", code="campo_invalido")
        hol.append(d)
    out["extra_holidays"] = sorted(set(hol))
    mi = {}
    for reg, dflt in MEAL_INTL.items():
        v = (out.get("meal_intl") or {}).get(reg) or dflt
        cur = str(v[0]).upper()[:3]
        mi[reg] = [cur, money(n(v[1], f"a diária internacional ({REGION_LABEL[reg]})", 0, 1e4))]
    out["meal_intl"] = mi
    items = []
    for it in out.get("other_items") or []:
        lbl = str(it.get("label") or "").strip()[:60]
        if not lbl:
            continue
        items.append({"label": lbl, "value": money(n(it.get("value"), f"o valor de {lbl}", -1e6, 1e6)),
                      "taxable": bool(it.get("taxable", True))})
    out["other_items"] = items
    src = {}
    for k, s in SRC_CCT.items():
        same = out.get(k) == base.get(k)
        src[k] = s if (same and out["preset"] == CCT_ID) else "informado por você"
    for k in ("night_pct", "hazard_pct"):
        src[k] = "desligado (a CCT 2025/26 não traz percentual)" if not out[k] else "informado por você (seu acordo)"
    out["sources"] = src
    return out


def contract_for(conn, month: str) -> dict | None:
    r = one(conn.execute("SELECT * FROM payroll_contract WHERE valid_from<=? ORDER BY valid_from DESC, id DESC LIMIT 1", (month,)))
    if not r:
        return None
    r["data"] = jloads(r["data"], {})
    return r


def contracts(conn) -> list[dict]:
    out = rows(conn.execute("SELECT * FROM payroll_contract ORDER BY valid_from DESC, id DESC"))
    for r in out:
        r["data"] = jloads(r["data"], {})
    return out


def save_contract(conn, valid_from: str, data: dict, note: str | None = None, user_id=None) -> dict:
    _check_month(valid_from)
    c = normalize(data or {})
    with tx(conn):
        cid = conn.execute("INSERT INTO payroll_contract(valid_from, data, note, created_at) VALUES (?,?,?,?)",
                           (valid_from, json.dumps(c, ensure_ascii=False), note, now_iso())).lastrowid
        audit(conn, user_id, "payroll.contract", "payroll_contract", cid, {"valid_from": valid_from, "role": c["role"]})
    return one(conn.execute("SELECT * FROM payroll_contract WHERE id=?", (cid,))) | {"data": c}


def suggestion(conn) -> dict:
    """Função e categoria pelo que o AeroScale já sabe: perfil de carreira (cargo) e tipo da escala."""
    from .journey import schedule_params
    p, sched = schedule_params(conn, now_iso()[:10])
    out = {"role": None, "role_source": None, "icao_type": None, "category": None, "category_source": None}
    prof = None
    try:
        c = rules._open(conn)
        try:
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone() and \
                    c.execute("SELECT 1 FROM meta WHERE key='career'").fetchone():
                prof = career.get_profile(c)
        finally:
            c.close()
    except Exception:
        prof = None
    if prof and prof.get("rank") in ROLES:
        out["role"], out["role_source"] = prof["rank"], "perfil de carreira do AeroScale"
    t, where = p.get("icao_type_resolved") or p.get("icao_type"), "parâmetros da escala no AeroScale"
    if not t:
        r = conn.execute("""SELECT type_icao, COUNT(*) n FROM roster_leg WHERE removed_batch IS NULL AND type_icao IS NOT NULL
                            AND activity NOT IN ('SOBREAVISO','EXTRA') GROUP BY type_icao ORDER BY n DESC LIMIT 1""").fetchone()
        t, where = (r[0], "tipo mais frequente nas pernas da escala") if r else (None, None)
    if not t and prof and prof.get("icao_type"):
        t, where = prof["icao_type"], "perfil de carreira do AeroScale"
    if t:
        fam = catalog.type_family(str(t).upper())
        out["icao_type"] = str(t).upper()
        if fam in FAMILY_CATEGORY:
            out["category"] = FAMILY_CATEGORY[fam]
            out["category_source"] = f"{out['icao_type']} ({fam}): {where}"
    return out


# ------------------------------------------------------------------ calendário
def easter(y: int) -> date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = ((h + l_ - 7 * m + 114) % 31) + 1
    return date(y, month, day)


NATIONAL = {"01-01": "Confraternização Universal", "04-21": "Tiradentes", "05-01": "Dia do Trabalho",
            "09-07": "Independência", "10-12": "Nossa Senhora Aparecida", "11-02": "Finados",
            "11-15": "Proclamação da República", "11-20": "Dia Nacional de Zumbi e da Consciência Negra",
            "12-25": "Natal"}


def holidays(year: int, extra: list[str] | None = None) -> dict[str, str]:
    """Feriados nacionais (Lei 662/1949, 6.802/1980, 14.759/2023 e Paixão de Cristo) + os da sua base que você informar."""
    out = {f"{year}-{k}": v for k, v in NATIONAL.items()}
    out[(easter(year) - timedelta(days=2)).isoformat()] = "Paixão de Cristo"
    for d in extra or []:
        if len(d) == 5:
            out[f"{year}-{d}"] = "feriado da base (informado por você)"
        elif d.startswith(str(year)):
            out[d] = "feriado da base (informado por você)"
    return out


def _check_month(m: str):
    try:
        datetime.strptime(m or "", "%Y-%m")
    except ValueError:
        raise FGVError("Mês inválido (use AAAA-MM).", code="mes_invalido")


def month_bounds(m: str) -> tuple[date, date]:
    y, mo = map(int, m.split("-"))
    return date(y, mo, 1), date(y, mo, calendar.monthrange(y, mo)[1])


# ------------------------------------------------------------------ geometria do tempo
def _minutes_where(t0: datetime, t1: datetime, tz, pred) -> float:
    """Minutos de [t0, t1) cuja data local (no fuso tz) satisfaz pred(date)."""
    total, cur = 0.0, t0
    while cur < t1:
        loc = cur.astimezone(tz)
        nxt = datetime.combine(loc.date() + timedelta(days=1), time(0), tzinfo=tz).astimezone(UTC)
        end = min(t1, nxt)
        if pred(loc.date()):
            total += (end - cur).total_seconds() / 60
        cur = end
    return total


def _window_minutes(t0: datetime, t1: datetime, tz, start: time, end: time) -> float:
    """Minutos de [t0, t1) dentro da janela local diária start–end (pode cruzar a meia-noite)."""
    total = 0.0
    d = t0.astimezone(tz).date() - timedelta(days=1)
    last = t1.astimezone(tz).date()
    while d <= last:
        a = datetime.combine(d, start, tzinfo=tz).astimezone(UTC)
        b = datetime.combine(d + timedelta(days=1 if end <= start else 0), end, tzinfo=tz).astimezone(UTC)
        lo, hi = max(t0, a), min(t1, b)
        if hi > lo:
            total += (hi - lo).total_seconds() / 60
        d += timedelta(days=1)
    return total


def is_brazil(icao: str | None) -> bool:
    return bool(icao) and icao[:2].upper() in ("SB", "SD", "SI", "SJ", "SN", "SS", "SW")


def region(icao: str) -> str:
    i = icao.upper()
    if i[:2] == "EG":
        return "UK"
    if i[0] in "KC" or i[:2] in ("PA", "PH", "PF", "PP", "MM"):
        return "NA"
    if i[0] in ("S", "T") or i[0] == "M":
        return "SA"
    if i[0] in ("E", "L"):
        return "EU"
    return "OT"


# ------------------------------------------------------------------ coleta das horas
def _base(conn, month: str):
    from .journey import schedule_params
    p, sched = schedule_params(conn, month + "-15")
    base = p.get("base_resolved") or p.get("base")
    tzname = rules.airport_tz(conn, base) if base else None
    return p, sched, base, (tzname or DEFAULT_TZ), bool(tzname)


def _roster_flights(conn, d0: date, d1: date) -> list[dict]:
    out = []
    for r in rows(conn.execute(
            """SELECT uid, flight_number, adep, ades, dep_utc, arr_utc, eet_min, activity, data_origin, type_icao,
                      COALESCE(local_date, substr(dep_utc,1,10)) AS day FROM roster_leg
               WHERE removed_batch IS NULL AND activity<>'SOBREAVISO'
                 AND COALESCE(local_date, substr(dep_utc,1,10)) BETWEEN ? AND ? ORDER BY dep_utc""",
            (d0.isoformat(), d1.isoformat()))):
        dep, arr = parse(r["dep_utc"]), parse(r["arr_utc"])
        if not dep or not arr or arr <= dep:
            continue
        r["_dep"], r["_arr"] = dep, arr
        r["minutes"] = (arr - dep).total_seconds() / 60
        r["deadhead"] = r["activity"] == "EXTRA"
        out.append(r)
    return out


def _logbook_flights(conn, d0: date, d1: date, tz, warnings: list) -> list[dict]:
    out = []
    lo, hi = (d0 - timedelta(days=1)).isoformat(), (d1 + timedelta(days=1)).isoformat()
    for e in rows(conn.execute("""SELECT * FROM logbook_entry WHERE reversed_at IS NULL AND date BETWEEN ? AND ?
                                  ORDER BY COALESCE(out_utc, off_utc, date)""", (lo, hi))):
        dep, arr = parse(e["out_utc"] or e["off_utc"]), parse(e["in_utc"] or e["on_utc"])
        day = dep.astimezone(tz).date() if dep else date.fromisoformat(e["date"])
        if not (d0 <= day <= d1):
            continue
        blk = e["block_min"]
        if blk is None:
            warnings.append({"level": "warn", "message": f"{e['flight_number'] or 'Voo'} {e['adep']}→{e['ades']} em {day:%d/%m} "
                                                        "não tem tempo de bloco no diário e ficou fora da soma."})
            continue
        r = {"uid": f"log{e['id']}", "flight_number": e["flight_number"], "adep": e["adep"], "ades": e["ades"],
             "day": day.isoformat(), "minutes": float(blk), "deadhead": False, "activity": "", "data_origin": "DIARIO",
             "_dep": dep, "_arr": arr if (dep and arr and arr > dep) else None}
        if dep and r["_arr"]:
            r["dep_utc"], r["arr_utc"] = e["out_utc"] or e["off_utc"], e["in_utc"] or e["on_utc"]
            r["eet_min"] = blk
        else:
            warnings.append({"level": "info", "message": f"{r['flight_number'] or 'Voo'} {e['adep']}→{e['ades']} em {day:%d/%m} "
                                                        "não tem horário de calço no diário: conta as horas, mas fica fora das diárias, "
                                                        "do domingo/feriado por horário e do noturno."})
        out.append(r)
    return out


def _standby(conn, d0: date, d1: date) -> list[dict]:
    out = []
    for r in rows(conn.execute("""SELECT uid, adep, dep_utc, arr_utc, COALESCE(local_date, substr(dep_utc,1,10)) AS day
                                  FROM roster_leg WHERE removed_batch IS NULL AND activity='SOBREAVISO'
                                    AND COALESCE(local_date, substr(dep_utc,1,10)) BETWEEN ? AND ? ORDER BY dep_utc""",
                                 (d0.isoformat(), d1.isoformat()))):
        a, b = parse(r["dep_utc"]), parse(r["arr_utc"])
        if a and b and b > a:
            r["minutes"] = (b - a).total_seconds() / 60
            out.append(r)
    return out


def _reserve_days(conn, d0: date, d1: date) -> tuple[list[str], list[str]]:
    """Dias de reserva no mês (sem repetir entre escalas) e, deles, os acionados (viram voo, não reserva)."""
    held, act = set(), set()
    for r in rows(conn.execute("SELECT start_date, end_date, act_start, act_days FROM roster_reserve WHERE start_date<=? AND end_date>=?",
                               (d1.isoformat(), d0.isoformat()))):
        try:
            a, b = date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"])
        except (TypeError, ValueError):
            continue
        d = a
        while d <= b:
            if d0 <= d <= d1:
                held.add(d.isoformat())
            d += timedelta(days=1)
        if r["act_start"] and r["act_days"]:
            s = date.fromisoformat(r["act_start"])
            for i in range(int(r["act_days"])):
                x = s + timedelta(days=i)
                if d0 <= x <= d1:
                    act.add(x.isoformat())
    return sorted(held), sorted(act & held)


# ------------------------------------------------------------------ diárias (jornadas pelo crew.analyze do AeroScale)
def _duties(conn, legs: list[dict], p: dict, base: str | None, airline: str | None) -> list[dict]:
    timed = [l for l in legs if l.get("_dep") and l.get("_arr")]
    if not timed:
        return []
    p2 = dict(p)
    if p2.get("report_before") is None and airline:            # mesma apresentação que o painel mostra (journey._duty_times)
        p2["report_before"] = rules.crew_profile(airline).get("report_min")
    src = sorted(({"uid": l["uid"], "adep": l["adep"], "ades": l["ades"], "dep_utc": l["_dep"].strftime(FMT),
                   "arr_utc": l["_arr"].strftime(FMT), "eet_min": int(l["minutes"]), "activity": l.get("activity") or "",
                   "data_origin": l.get("data_origin") or ""} for l in timed), key=lambda x: x["dep_utc"])
    fam = rules.type_family(p.get("icao_type_resolved") or p.get("icao_type"))
    rep = rules.crew_report(conn, src, p2, base, fam, None, None)
    groups = crew.group_duties([{**x, "_dep": parse(x["dep_utc"]), "_arr": parse(x["arr_utc"])} for x in src])
    duties = rep["duties"]
    if len(groups) != len(duties):                                # o agrupamento é o mesmo do analyze; se divergir, não chuta
        raise RuntimeError("agrupamento de jornadas divergente")
    return [{**d, "_legs": g} for d, g in zip(duties, groups)]


def _where(duty_legs: list[dict], t: datetime) -> str:
    """Aeroporto onde o tripulante está no instante t da jornada (em voo conta a origem da perna)."""
    if t < duty_legs[0]["_dep"]:
        return duty_legs[0]["adep"]
    for i, l in enumerate(duty_legs):
        if l["_dep"] <= t <= l["_arr"]:
            return l["adep"]
        nxt = duty_legs[i + 1] if i + 1 < len(duty_legs) else None
        if nxt and l["_arr"] < t < nxt["_dep"]:
            return l["ades"]
    return duty_legs[-1]["ades"]


def meals(duties: list[dict], c: dict, tz, d0: date, d1: date) -> dict:
    main_v, cafe_v = c["meal_value"], money(c["meal_value"] * c["breakfast_pct"] / 100)
    dom = {"main": 0, "breakfast": 0, "hotel_skipped": 0, "value": 0.0}
    intl: dict[str, float] = {}
    detail = []
    for d in duties:
        a, b = parse(d["report_utc"]), parse(d["release_utc"])
        if not (d0 <= a.astimezone(tz).date() <= d1):
            continue
        items, cafe_seen = [], False
        day = a.astimezone(tz).date() - timedelta(days=1)
        while day <= b.astimezone(tz).date():
            for key, label, s, e in MEALS:
                ws = datetime.combine(day, s, tzinfo=tz).astimezone(UTC)
                we = datetime.combine(day, e, tzinfo=tz).astimezone(UTC)
                if not (a <= we and b >= ws):                    # janelas "inclusive" (cl. 2.3 §2)
                    continue
                t = max(a, ws)
                ap = _where(d["_legs"], t)
                it = {"kind": key, "label": label, "local": f"{day:%d/%m} {s:%H:%M}–{e:%H:%M}", "where": ap}
                if key == "cafe" and not cafe_seen and d.get("night_at") and c["breakfast_in_hotel"]:
                    cafe_seen = True
                    it.update(value=0.0, currency="BRL", note=f"incluso no hotel do pernoite em {d['night_at']}")
                    dom["hotel_skipped"] += 1
                elif is_brazil(ap) or not ap:
                    v = cafe_v if key == "cafe" else main_v
                    it.update(value=v, currency="BRL")
                    dom["breakfast" if key == "cafe" else "main"] += 1
                    dom["value"] += v
                else:
                    reg = region(ap)
                    cur, v = c["meal_intl"][reg]
                    v = money(v * c["breakfast_pct"] / 100) if key == "cafe" else v
                    it.update(value=v, currency=cur, note=REGION_LABEL[reg])
                    intl[cur] = money(intl.get(cur, 0) + v)
                if key == "cafe":
                    cafe_seen = True
                items.append(it)
            day += timedelta(days=1)
        detail.append({"date": d["date"], "report_utc": d["report_utc"], "release_utc": d["release_utc"], "start": d["start"],
                       "end": d["end"], "legs": d["legs"] + d.get("extra_legs", 0), "night_at": d.get("night_at"), "meals": items})
    dom["value"] = money(dom["value"])
    return {"domestic": dom, "intl": intl, "duties": detail, "main_value": main_v, "breakfast_value": cafe_v}


# ------------------------------------------------------------------ impostos
def inss(gross: float) -> float:
    base, prev, total = min(max(gross, 0), INSS_BRACKETS[-1][0]), 0.0, 0.0
    for top, rate in INSS_BRACKETS:
        if base > prev:
            total += (min(base, top) - prev) * rate
        prev = top
    return money(total)


def _ir_table(b: float) -> float:
    for top, rate, ded in IRRF_BRACKETS:
        if b <= top:
            return max(0.0, b * rate - ded)
    return 0.0


def irrf(gross: float, inss_v: float, dependents: int = 0) -> dict:
    legal = gross - inss_v - dependents * IRRF_DEPENDENT
    simp = gross - IRRF_SIMPLIFIED
    t_legal, t_simp = _ir_table(legal), _ir_table(simp)
    use_simp = t_simp < t_legal
    tax = money(t_simp if use_simp else t_legal)
    if gross <= 5000:
        red = min(tax, 312.89)
    elif gross <= 7350:
        red = min(tax, max(0.0, money(978.62 - 0.133145 * gross)))
    else:
        red = 0.0
    return {"value": money(tax - red), "table": tax, "reducer": money(red), "base": money(simp if use_simp else legal),
            "method": "desconto simplificado" if use_simp else "deduções legais (INSS e dependentes)"}


# ------------------------------------------------------------------ cálculo do mês
def compute(conn, month: str, basis: str = "programado", now: str | None = None) -> dict:
    _check_month(month)
    if basis not in ("programado", "realizado"):
        raise FGVError("Base inválida (programado ou realizado).", code="campo_invalido")
    ct = contract_for(conn, month)
    if not ct:
        return {"month": month, "basis": basis, "needs_contract": True, "suggestion": suggestion(conn)}
    c = ct["data"]
    d0, d1 = month_bounds(month)
    p, sched, base, tzname, tz_known = _base(conn, month)
    tz = get_tz(tzname) or UTC
    warnings = []
    if not tz_known:
        warnings.append({"level": "info", "message": "Sem base com fuso conhecido na escala: domingos, feriados, noturno e diárias "
                                                     "usam o horário de Brasília (como a CCT cl. 3.3.14)."})
    flights = _roster_flights(conn, d0, d1) if basis == "programado" else _logbook_flights(conn, d0, d1, tz, warnings)
    standby = _standby(conn, d0, d1)
    held, activated = _reserve_days(conn, d0, d1)
    flown_days = {f["day"] for f in flights if not f["deadhead"]}
    reserve_days = [d for d in held if d not in activated]
    rate = c["hour_rate"]
    hol = holidays(d0.year, c.get("extra_holidays"))
    hol.update({k: v for k, v in holidays(d1.year, c.get("extra_holidays")).items()})

    paid = [f for f in flights if not f["deadhead"]]
    flight_min = sum(f["minutes"] for f in paid)
    deadhead_min = sum(f["minutes"] for f in flights if f["deadhead"])
    reserve_min = len(reserve_days) * c["reserve_hours_per_day"] * 60
    standby_min = sum(s["minutes"] for s in standby)

    # domingos/feriados e noturno: só onde há horário de calço; proporcional ao bloco quando ele foi ajustado
    sun_min = night_min = 0.0
    sun_days = set()
    for f in paid:
        if f.get("_dep") and f.get("_arr"):
            span = (f["_arr"] - f["_dep"]).total_seconds() / 60
            k = f["minutes"] / span if span else 1
            pred = lambda dd: dd.weekday() == 6 or dd.isoformat() in hol
            m = _minutes_where(f["_dep"], f["_arr"], tz, pred) * k
            if m > 0:
                sun_days |= {x for x in (f["_dep"].astimezone(tz).date(), f["_arr"].astimezone(tz).date()) if pred(x)}
            sun_min += m
            night_min += _window_minutes(f["_dep"], f["_arr"], tz, time(22, 0), time(5, 0)) * k
        else:
            dd = date.fromisoformat(f["day"])
            if dd.weekday() == 6 or f["day"] in hol:
                sun_min += f["minutes"]
                sun_days.add(dd)

    # garantia em valor: com os padrões da CCT é exatamente (voo + reserva + sobreaviso/3 − 54 h) × hora
    v_flight = flight_min / 60 * rate
    v_res = reserve_min / 60 * c["reserve_hour_value"]
    v_stb = standby_min / 60 * c["standby_hour_value"]
    guar_v = c["guarantee_h"] * rate
    variable = money(max(0.0, v_flight + v_res + v_stb - guar_v))
    equiv_min = (v_flight + v_res + v_stb) / rate * 60 if rate else 0.0
    excess_min = max(0.0, equiv_min - c["guarantee_h"] * 60)

    lines = []

    def add(code, label, ref, amount, *, taxable=True, kind="provento", detail=None, source=None):
        amount = money(amount)
        if not amount and kind != "info":
            return
        lines.append({"code": code, "label": label, "ref": ref, "amount": amount, "kind": kind, "taxable": taxable,
                      "detail": detail, "source": source})

    pct = c["organic_pct"]
    base_sal = money(c["fixed_total"] / (1 + pct / 100))
    organic = money(c["fixed_total"] - base_sal)
    src = c.get("sources", {})
    add("001", "Salário base", "mensal", base_sal, source=src.get("fixed_total"),
        detail="Parte fixa do mês; não depende de quantas horas a escala tem.")
    add("002", "Compensação orgânica", f"{pct:g}%", organic, source=src.get("organic_pct"),
        detail="Integra a parte fixa. Tratada aqui como tributável (o enquadramento varia entre acordos).")
    add("010", "Horas excedentes à garantia", hhmm(excess_min), variable, source=src.get("hour_rate"),
        detail=f"Voo {hhmm(flight_min)} + reserva {hhmm(reserve_min)} + sobreaviso {hhmm(standby_min)} "
               f"(valendo {hhmm(v_stb / rate * 60) if rate else '0:00'}) = {hhmm(equiv_min)} equivalentes; "
               f"garantia de {c['guarantee_h']:g} h coberta pela parte fixa; excedente × {brl(rate)}/h.")
    sun_v = sun_min / 60 * rate if c["sunday_holiday_double"] else 0.0
    add("011", "Domingos e feriados (dobra)", hhmm(sun_min), sun_v, source=src.get("sunday_holiday_double"),
        detail="Horas voadas em domingo ou feriado da base, no fuso da base. A dobra é lançada à parte, inclusive "
               "dentro da garantia (leitura usual da cl. 3.2.6; confira o seu acordo).")
    night_v = night_min / 60 * rate * c["night_pct"] / 100
    add("012", "Adicional noturno", f"{hhmm(night_min)} · {c['night_pct']:g}%", night_v, source=src.get("night_pct"),
        detail="Horas de voo entre 22:00 e 05:00 no fuso da base (período noturno da CCT cl. 3.3.13).")
    var_total = variable + money(sun_v) + money(night_v)
    days_in = (d1 - d0).days + 1
    dsr_v = var_total / (days_in - c["dsr_rest_days"]) * c["dsr_rest_days"] if c["dsr_enabled"] and days_in > c["dsr_rest_days"] else 0.0
    add("013", "DSR sobre a parte variável", f"{c['dsr_rest_days']}/{days_in - c['dsr_rest_days']}", dsr_v,
        source=src.get("dsr_rest_days"), detail=f"Variáveis {brl(var_total)} ÷ {days_in - c['dsr_rest_days']} dias de trabalho × "
                                                f"{c['dsr_rest_days']} folgas.")
    add("020", "Adicional de periculosidade", f"{c['hazard_pct']:g}%", base_sal * c["hazard_pct"] / 100,
        source=src.get("hazard_pct"), detail="Sobre o salário base.")
    for i, it in enumerate(c.get("other_items") or []):
        add(f"03{i}", it["label"], "mensal", it["value"], taxable=it["taxable"], source="informado por você")

    trips = []
    meal = {"domestic": {"main": 0, "breakfast": 0, "hotel_skipped": 0, "value": 0.0}, "intl": {}, "duties": [],
            "main_value": c["meal_value"], "breakfast_value": money(c["meal_value"] * c["breakfast_pct"] / 100)}
    try:
        meal = meals(_duties(conn, flights, p, base, (sched or {}).get("airline") if sched else None), c, tz, d0, d1)
    except Exception as e:                       # sem as regras do AeroScale: diz, não chuta
        warnings.append({"level": "warn", "message": f"Não foi possível montar as jornadas para as diárias: {e}"})
    dm = meal["domestic"]
    add("050", "Diárias de alimentação", f"{dm['main']} + {dm['breakfast']} café", dm["value"], taxable=False,
        source=src.get("meal_value"), detail=f"{dm['main']} refeição(ões) × {brl(meal['main_value'])} e {dm['breakfast']} café(s) × "
                                            f"{brl(meal['breakfast_value'])}, nas janelas da cl. 2.3 em que você estava em jornada. "
                                            "Indenizatória: fora do INSS e do IR.")
    for cur, v in meal["intl"].items():
        trips.append({"currency": cur, "value": v})

    earnings = [l for l in lines if l["kind"] == "provento"]
    taxable = money(sum(l["amount"] for l in earnings if l["taxable"]))
    nontax = money(sum(l["amount"] for l in earnings if not l["taxable"]))
    tax = {"enabled": c["taxes_enabled"], "year_note": None}
    inss_v = ir_v = 0.0
    if c["taxes_enabled"]:
        inss_v = inss(taxable)
        ir = irrf(taxable, inss_v, c["dependents"])
        ir_v = ir["value"]
        top = INSS_BRACKETS[-1][0]
        add("090", "INSS", "teto" if taxable >= top else "progressivo", inss_v, kind="desconto", taxable=False,
            source=INSS_SOURCE, detail=f"Sobre {brl(min(taxable, top))} de remuneração tributável" + (f" (teto de {brl(top)})" if taxable > top else "") + ".")
        add("091", "IRRF", "simplificado" if ir["method"].startswith("desconto") else "deduções legais", ir_v, kind="desconto",
            taxable=False, source=IRRF_SOURCE,
            detail=f"Base {brl(ir['base'])} ({ir['method']}); imposto da tabela {brl(ir['table'])}" + (f"; redutor {brl(ir['reducer'])}" if ir["reducer"] else "")
                   + (f"; {c['dependents']} dependente(s)" if c["dependents"] else "") + ".")
        tax.update(inss=inss_v, irrf=ir)
        if d0.year != TAX_YEAR:
            tax["year_note"] = f"Tabelas de {TAX_YEAR} aplicadas a {d0.year}: atualize quando houver tabela nova."
            warnings.append({"level": "info", "message": tax["year_note"]})
    gross = money(taxable + nontax)
    deductions = money(inss_v + ir_v)

    if basis == "programado" and not flights and not standby and not held:
        warnings.append({"level": "warn", "message": "Não há escala importada para este mês: só a parte fixa entra."})
    if basis == "realizado" and not flights:
        warnings.append({"level": "info", "message": "Nenhum voo aprovado no diário neste mês."})
    if reserve_days:
        warnings.append({"level": "info", "message": f"{len(reserve_days)} dia(s) de reserva não acionada × {c['reserve_hours_per_day']:g} h "
                                                     "(o AeroScale dá o dia, não o horário da reserva; ajuste no contrato)."})
    if deadhead_min:
        warnings.append({"level": "info", "message": f"{hhmm(deadhead_min)} como tripulante extra (EXTRA na escala) contam na jornada e "
                                                     "nas diárias, mas não como hora de voo."})
    if trips:
        warnings.append({"level": "info", "message": "Diárias internacionais na moeda da CCT (cl. 2.3.1), sem conversão: "
                                                     + ", ".join(f"{t['currency']} {t['value']:.2f}".replace(".", ",") for t in trips) + "."})
    closing = one(conn.execute("SELECT id, closed_at, notes FROM payroll_closing WHERE month=? AND voided_at IS NULL ORDER BY id DESC LIMIT 1",
                               (month,)))
    today = (now or now_iso())
    today_local = parse(today).astimezone(tz).date() if parse(today) else date.today()
    return {
        "month": month, "basis": basis, "needs_contract": False, "computed_at": now_iso(),
        "contract": {"id": ct["id"], "valid_from": ct["valid_from"], "role": c["role"], "category": c["category"],
                     "category_label": CATEGORIES[c["category"]], "preset": c["preset"], "data": c},
        "base": {"icao": base, "tz": tzname, "tz_known": tz_known},
        "lines": lines,
        "totals": {"gross": gross, "taxable": taxable, "nontaxable": nontax, "deductions": deductions,
                   "net": money(gross - deductions), "foreign_meals": trips,
                   "food_voucher": c.get("food_voucher")},
        "hours": {"flight_min": round(flight_min), "deadhead_min": round(deadhead_min), "reserve_days": reserve_days,
                  "reserve_activated": activated, "reserve_min": round(reserve_min), "standby_min": round(standby_min),
                  "standby_count": len(standby), "equiv_min": round(equiv_min), "guarantee_min": round(c["guarantee_h"] * 60),
                  "excess_min": round(excess_min), "sunday_holiday_min": round(sun_min),
                  "sunday_holiday_days": sorted(d.isoformat() for d in sun_days), "night_min": round(night_min),
                  "legs": len(paid), "days_flown": len(flown_days), "hour_rate": rate},
        "holidays": {k: v for k, v in sorted(hol.items()) if k[:7] == month},
        "meals": meal, "tax": tax, "warnings": warnings, "closing": closing,
        "month_ended": today_local > d1,
        "source": {"cct": CCT, "basis_note": ("Escala importada do AeroScale (pernas pela data local da partida)." if basis == "programado"
                                              else "Diário de bordo: voos aprovados, calço a calço. Reserva e sobreaviso vêm da escala.")},
    }


# ------------------------------------------------------------------ fechamento e ano
def close_month(conn, month: str, notes: str | None = None, user_id=None, now: str | None = None) -> dict:
    data = compute(conn, month, "realizado", now=now)
    if data.get("needs_contract"):
        raise Conflict("Cadastre o contrato antes de fechar o mês.", code="sem_contrato")
    if not data["month_ended"]:
        raise Conflict("O mês ainda não terminou: o fechamento usa o realizado completo.", code="mes_aberto")
    with tx(conn):
        if conn.execute("SELECT 1 FROM payroll_closing WHERE month=? AND voided_at IS NULL", (month,)).fetchone():
            raise Conflict("Este mês já está fechado. Anule o fechamento para refazer.", code="ja_fechado")
        cid = conn.execute("INSERT INTO payroll_closing(month, contract_id, data, notes, closed_at) VALUES (?,?,?,?,?)",
                           (month, data["contract"]["id"], json.dumps(data, ensure_ascii=False), notes, now_iso())).lastrowid
        audit(conn, user_id, "payroll.close", "payroll_closing", cid, {"month": month, "net": data["totals"]["net"]})
    return get_closing(conn, cid)


def void_closing(conn, cid: int, reason: str | None, user_id=None) -> dict:
    if not (reason or "").strip():
        raise FGVError("Informe o motivo.", code="motivo_obrigatorio")
    with tx(conn):
        n = conn.execute("UPDATE payroll_closing SET voided_at=?, void_reason=? WHERE id=? AND voided_at IS NULL",
                         (now_iso(), reason.strip(), cid)).rowcount
        if not n:
            raise NotFound("Fechamento não encontrado ou já anulado.")
        audit(conn, user_id, "payroll.void", "payroll_closing", cid, {"reason": reason})
    return get_closing(conn, cid)


def get_closing(conn, cid: int) -> dict:
    r = one(conn.execute("SELECT * FROM payroll_closing WHERE id=?", (cid,)))
    if not r:
        raise NotFound("Fechamento não encontrado.")
    r["data"] = jloads(r["data"], {})
    return r


def closings(conn) -> list[dict]:
    out = []
    for r in rows(conn.execute("SELECT * FROM payroll_closing ORDER BY month DESC, id DESC")):
        d = jloads(r.pop("data"), {})
        r["totals"] = d.get("totals", {})
        r["flight_min"] = (d.get("hours") or {}).get("flight_min")
        out.append(r)
    return out


def year(conn, y: int, now: str | None = None) -> dict:
    today = (now or now_iso())[:7]
    out, tot = [], {"gross": 0.0, "deductions": 0.0, "net": 0.0, "inss": 0.0, "irrf": 0.0, "meals": 0.0, "flight_min": 0}
    closed = {r["month"]: r for r in rows(conn.execute(
        "SELECT * FROM payroll_closing WHERE voided_at IS NULL AND substr(month,1,4)=? ORDER BY id", (str(y),)))}
    for m in range(1, 13):
        mk = f"{y}-{m:02d}"
        if mk > today:
            break
        if mk in closed:
            d, is_closed = jloads(closed[mk]["data"], {}), True
        elif contract_for(conn, mk):
            d, is_closed = compute(conn, mk, "realizado", now=now), False
        else:
            continue
        t = d["totals"]
        tx_ = d.get("tax") or {}
        row_ = {"month": mk, "closed": is_closed, "closing_id": closed[mk]["id"] if is_closed else None, "gross": t["gross"], "deductions": t["deductions"], "net": t["net"],
                "inss": tx_.get("inss") or 0.0, "irrf": ((tx_.get("irrf") or {}).get("value")) or 0.0,
                "meals": (d["meals"]["domestic"]["value"]), "flight_min": d["hours"]["flight_min"]}
        for k in tot:
            tot[k] = money(tot[k] + row_[k]) if k != "flight_min" else tot[k] + row_[k]
        out.append(row_)
    return {"year": y, "months": out, "totals": tot}


def summary(conn, now: str | None = None) -> dict | None:
    """Para o painel: estimativa do mês corrente pela escala."""
    if not enabled(conn):
        return None
    m = (now or now_iso())[:7]
    d = compute(conn, m, "programado", now=now)
    if d.get("needs_contract"):
        return {"month": m, "needs_contract": True}
    return {"month": m, "gross": d["totals"]["gross"], "net": d["totals"]["net"], "flight_min": d["hours"]["flight_min"],
            "excess_min": d["hours"]["excess_min"], "guarantee_min": d["hours"]["guarantee_min"]}
