"""Motor de escalas: o gerador do AeroScale 2.2.4 rodando dentro da LineOps.

Antes a LineOps só importava a escala que o AeroScale gerava. Agora ela gera:

  RPL do CGNA (AZU, GLO, TAM)  →  motor (aeroscale/ + dados/motor/aeroscale.db)  →  escala na carreira
     embutidos e atualizados        escala do tripulante, mesma lógica                (o mesmo importador de
     toda semana                    do AeroScale                                       antes: diferenças, avisos)

* O banco do motor tem o esquema do próprio AeroScale (é o mesmo db.init_db), guardado em dados/motor/.
* Na primeira vez, os RPL embutidos (aeroscale/rpl) entram sozinhos. Depois, como no AeroScale, o RPL mais novo de cada
  companhia vem do geradorderotas.com.br (fonte autorizada pelo site, no máximo a cada 12 h, uma requisição por companhia).
* "Gerar escala" chama scheduler.generate do AeroScale no modo tripulante (troca de aeronave nos hubs, extra em voo real,
  perfil da companhia calibrado com escalas reais de comandante, Lei 13.475 e RBAC 117) e, ao terminar, entrega a escala
  à carreira pelo importador de sempre. Regerar o mesmo período substitui a escala, e a equipe avisa a alteração.
* Nada é inventado: dias sem RPL publicado repetem a semana mais nova (ou a mesma semana do ano anterior) e vêm marcados
  como PROJETADO, exatamente como no AeroScale.
* O importador de backup do AeroScale continua valendo, para quem gera no app do AeroScale.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, timedelta
from pathlib import Path

from .aeroscale import __version__ as ENGINE_VERSION
from .aeroscale import crew_profiles, jobs, reserve, rpl_service, rpl_update, scheduler
from .aeroscale import db as edb
from .errors import Conflict, FGVError, NotFound

log = logging.getLogger("lineops.motor")
ENGINE_DIR = "motor"
RPL_DIR = Path(__file__).resolve().parent / "aeroscale" / "rpl"
_init_lock = threading.Lock()
_ready: set[str] = set()


def paths(data_dir) -> tuple[Path, Path]:
    d = Path(data_dir) / ENGINE_DIR
    return d / "aeroscale.db", d / "arquivos"


def init(data_dir, *, autoimport=True) -> Path:
    """Cria/atualiza o banco do motor e importa os RPL embutidos ainda não importados (uma vez por processo)."""
    dbp, store = paths(data_dir)
    key = str(dbp)
    with _init_lock:
        if key in _ready:
            return dbp
        dbp.parent.mkdir(parents=True, exist_ok=True)
        store.mkdir(parents=True, exist_ok=True)
        edb.init_db(str(dbp))
        if autoimport:
            c = edb.connect(str(dbp))
            try:
                for x in rpl_service.import_folder(c, str(RPL_DIR), str(store)):
                    if not x.get("skipped"):
                        log.info("RPL embutido %s: %s", x["filename"], "importado" if x["ok"] else x.get("error"))
            finally:
                c.close()
        _ready.add(key)
    return dbp


def connect(data_dir):
    return edb.connect(str(init(data_dir)))


def start_background(data_dir) -> None:
    """Atualização automática do RPL ao abrir (respeita o intervalo de 12 h do AeroScale). Sem internet, nada muda."""
    if os.environ.get("LINEOPS_OFFLINE") == "1":
        return
    dbp, store = paths(data_dir)
    init(data_dir)
    rpl_update.start_background(str(dbp), str(store))


# ------------------------------------------------------------------ informações para a tela
def status(data_dir, airline: str) -> dict:
    c = connect(data_dir)
    try:
        idx = rpl_service.get_index(c, airline)
        cf, ct = idx.coverage()
        weeks = edb.rows(c.execute("""SELECT id, filename, valid_from, valid_to, active, imported_at, record_count FROM rpl_imports
                                      WHERE airline=? ORDER BY valid_from DESC LIMIT 8""", (airline,)))
        st = rpl_update.status(c)
        gen = edb.rows(c.execute("SELECT id, name, kind, start_date, end_date, created_at FROM schedules WHERE airline=? ORDER BY id DESC LIMIT 10",
                                 (airline,)))
        return {"engine": f"AeroScale {ENGINE_VERSION}", "airline": airline, "coverage": {"from": cf, "to": ct}, "rpl_weeks": weeks,
                "update": {"auto": st["auto"], "state": st["airlines"].get(airline), "interval_h": st["interval_h"]},
                "generated": gen, "today": date.today().isoformat()}
    finally:
        c.close()


def options(data_dir, airline: str, conn=None) -> dict:
    """Tipos e bases que o RPL da companhia tem de verdade (nada de lista inventada)."""
    c = connect(data_dir)
    try:
        types = edb.rows(c.execute("""SELECT f.type_icao AS type, COUNT(*) n FROM rpl_flights f JOIN rpl_imports i ON i.id=f.import_id
                                      WHERE i.active=1 AND f.airline=? AND f.status<>'error' GROUP BY f.type_icao ORDER BY n DESC""", (airline,)))
        bases = edb.rows(c.execute("""SELECT f.adep AS icao, COUNT(*) n FROM rpl_flights f JOIN rpl_imports i ON i.id=f.import_id
                                      WHERE i.active=1 AND f.airline=? AND f.status<>'error' GROUP BY f.adep ORDER BY n DESC LIMIT 25""", (airline,)))
        from .aeroscale import catalog
        apmap = catalog.airport_map(c)
        for b in bases:
            a = apmap.get(b["icao"]) or {}
            b["city"], b["iata"] = a.get("city"), a.get("iata")
        prof = crew_profiles.profile(airline)
        s = edb.get_settings(c)
    finally:
        c.close()
    today = date.today()
    nm = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    return {"types": types, "bases": bases, "profile": {k: prof.get(k) for k in ("report_min", "sobreaviso_per_month", "sobreaviso_activation_pct",
                                                                                   "long_off_block", "legs_dist") if k in prof},
            "profile_text": PROFILE_TEXT.get(airline, ""), "chaves_profile": CH_PROF.get(airline, [30, 38]),
            "settings": {k: s.get(k) for k in SETTING_KEYS},
            "defaults": {"kind": "mensal", "month": nm.strftime("%Y-%m"), "start_date": today.isoformat(), "mode": "tripulante",
                         "profile_realism": True, "reserve_blocks": reserve.default_blocks("mensal", s),
                         "international": True, "type_match": s.get("type_match", "family")},
            "kinds": [["mensal", "Mês"], ["semanal", "Semana"], ["avulsa", "Período (até 62 dias)"], ["semestral", "6 meses (experimental)"],
                      ["anual", "Ano (experimental)"]]}


# o mesmo texto e as mesmas chances do gerador do AeroScale (static/js/views/generator.js)
PROFILE_TEXT = {
    "AZU": "Perfil Azul (3 escalas reais de comandante, base REC): 2,5 pernas por dia de voo em média, dias de 1 perna na saída e na "
           "volta de folga, FR para folga, 1 sobreaviso por mês, INATIVO fora da base, transferência por terra VCP↔CGH↔GRU.",
    "TAM": "Perfil LATAM (1 escala real de comandante, base GRU + média): mais dias de 1 perna (etapas longas e internacionais), DO para "
           "folga, reserva em bloco de 3 dias, sem sobreaviso.",
    "GLO": "Perfil GOL: sem escala de referência ainda; usa a média de Azul e LATAM. Se tiver uma escala real da GOL, importe em "
           "Importar escala real.",
}
CH_PROF = {"AZU": [30, 40], "TAM": [30, 33], "GLO": [30, 38]}
SETTING_KEYS = ("legs_min", "legs_max", "min_turn_min", "max_turn_min", "min_rest_min", "day_start", "day_end", "weekday_max", "turn_mode",
                "crew_mode", "crew_days_off", "crew_folga_base", "crew_madrugada", "crew_monthly", "days_off_min", "max_consecutive_days",
                "model_realism", "anomaly_repeat_pct", "projection_enabled", "avoid_used_flights", "intl_enabled", "type_match",
                "reserve_enabled", "reserve_per_month", "reserve_min_days", "reserve_max_days", "reserve_base_pct", "reserve_activation_pct",
                "reserve_activation_max_days")
DEFAULT_KEYS = ("weekday_max", "crew_mode", "crew_days_off", "crew_folga_base", "crew_madrugada", "crew_monthly", "days_off_min",
                "max_consecutive_days", "turn_mode")


def save_defaults(data_dir, values: dict) -> dict:
    """'Salvar dias da semana e regras como padrão', como no AeroScale: vale para as próximas escalas."""
    bad = set(values) - set(DEFAULT_KEYS)
    if bad:
        raise FGVError(f"Ajustes desconhecidos: {', '.join(sorted(bad))}", code="campo_invalido")
    wm = values.get("weekday_max")
    if wm is not None:
        if not isinstance(wm, dict) or any(str(k) not in "0123456" or not isinstance(v, int) or not 0 <= v <= 8 for k, v in wm.items()):
            raise FGVError("Dias da semana inválidos.", code="campo_invalido")
    c = connect(data_dir)
    try:
        edb.set_settings(c, {k: v for k, v in values.items() if v is not None})
        c.commit()
        s = edb.get_settings(c)
    finally:
        c.close()
    return {k: s.get(k) for k in SETTING_KEYS}


def _supersede(db, airline: str, start: str, end: str) -> int:
    """Escalas antigas do motor no mesmo período e da mesma companhia: a escala nova as substitui.
    Sem isso, o 'não reutilizar voos já atribuídos' do AeroScale via os voos da sua própria escala anterior como ocupados e
    a geração seguinte saía vazia (cada regerar ficava pior). A escala que já está na sua carreira não muda por isso."""
    return db.execute("DELETE FROM schedules WHERE airline=? AND start_date<=? AND end_date>=?", (airline, end, start)).rowcount


def wipe_engine(data_dir, airline: str) -> int:
    c = connect(data_dir)
    try:
        n = c.execute("DELETE FROM schedules WHERE airline=?", (airline,)).rowcount
        c.commit()
        return n
    finally:
        c.close()


# ------------------------------------------------------------------ gerar
ALLOWED = {"kind", "month", "start_date", "end_date", "base", "icao_type", "mode", "profile_realism", "requested_off", "avoid_madrugada",
           "time_pref", "reserve_blocks", "international", "intl_target_pct", "legs2_pct", "legs3_pct", "sobreaviso_per_month",
           "long_off", "inactive_days", "seed", "preferred_airports", "avoid_airports", "pref_strength", "allow_deadhead", "name",
           "type_match", "days_off_min", "crew_mode", "from_position", "start_day", "replace_from_day",
           # o resto do gerador do AeroScale (mesmos nomes e mesmas regras)
           "swap_min", "allow_deadhead", "sobreaviso_activation_pct", "return_to_base", "leg_min_minutes", "leg_max_minutes", "legs_min",
           "legs_max", "turn_mode", "min_turn", "max_turn", "min_rest", "day_start", "day_end", "weekday_max", "projection", "avoid_used",
           "allow_positioning", "model_realism", "anomaly_repeat_pct", "crew_days_off", "crew_folga_base", "crew_madrugada", "crew_monthly",
           "max_consecutive_days", "reserve_min_days", "reserve_max_days", "reserve_base_pct", "reserve_activation_pct",
           "reserve_activation_max_days", "equipment",
           # LineOps: pernoite pedido (o motor termina o dia no aeroporto pedido, com must_end do próprio AeroScale)
           "overnights", "include_cargo",
           # LineOps: folgas automáticas (regras), nenhuma folga, ou só as escolhidas a dedo
           "days_off_mode",
           # LineOps 3.0.2 (beta): seguir continuações (regras / regras + identificadas / não)
           "follow_mode", "follow_strength", "day_legs_min"}
DAYS_OFF_MODES = ("auto", "none", "manual")


def clean_params(airline: str, raw: dict) -> dict:
    bad = set(raw) - ALLOWED
    if bad:
        raise FGVError(f"Opções desconhecidas: {', '.join(sorted(bad))}", code="campo_invalido")
    p = {k: v for k, v in raw.items() if v not in (None, "")}
    p["airline"] = airline
    p.setdefault("mode", "tripulante")
    if p.get("icao_type"):
        p["icao_type"] = str(p["icao_type"]).upper()
        p["aircraft_mode"] = "type_only"           # escala do tripulante: segue a habilitação, não uma matrícula
    mode = p.get("days_off_mode") or "auto"
    if mode not in DAYS_OFF_MODES:
        raise FGVError("Modo de folgas inválido (auto, none ou manual).", code="campo_invalido")
    if mode != "auto":
        # o próprio interruptor do AeroScale "Folgas regulamentares" desliga: o motor não sorteia folga nenhuma
        # (nem o bloco longo) e o crew.analyze não cobra folga mínima, dias seguidos nem fim de semana do mês.
        p["crew_days_off"] = False
        p["long_off"] = False
        p["weekday_max"] = {k: v for k, v in (p.get("weekday_max") or {}).items() if v not in (0, "0")}  # "Folga" do dia da semana sai
        if mode == "none":
            p["requested_off"] = []
        elif not p.get("requested_off"):
            raise FGVError("Folgas manuais: marque pelo menos um dia (ou escolha “Sem folgas”).", code="campo_invalido")
    return p


def more_flights(data_dir, airline: str, career_path: Path, day: str, mins: dict, user_id=None, share_stats: bool = False) -> dict:
    """Regerar a escala a partir de um dia, com os mesmos parâmetros da escala que hoje é dona desse dia, pedindo um
    mínimo de pernas em dias escolhidos ({"AAAA-MM-DD": n}). O que vem antes do dia não muda; a nova parte continua de
    onde o avião estava (mesmo encaixe do "gerar a partir do dia"). Folgas pedidas, pernoites pedidos e a força das
    continuações da escala original continuam valendo."""
    from . import journey
    from .db import connect as lconnect
    try:
        date.fromisoformat(day)
    except ValueError:
        raise FGVError("Dia inválido.", code="campo_invalido") from None
    if not mins:
        mins = {}
    cc = lconnect(career_path)
    try:
        p, sched = journey.schedule_params(cc, day)
    finally:
        cc.close()
    if not sched:
        raise FGVError(f"Não há escala gerada que cubra {day[8:10]}/{day[5:7]}: gere a escala do período antes.", code="sem_escala", status=409)
    if p.get("imported"):
        raise FGVError("Esta parte da escala é uma escala real importada: o motor não a regera.", code="escala_importada", status=409)
    skip = {"name", "start_day", "replace_from_day", "from_position", "day_legs_min", "overnights", "follow_mode", "month"}
    body = {k: v for k, v in p.items() if k in ALLOWED and k not in skip and v not in (None, "")}
    body["kind"], body["start_date"], body["end_date"] = "avulsa", sched["start_date"], sched["end_date"]
    ov = [{"date": r.get("night") or r.get("date"), "airport": r.get("icao") or r.get("airport")} for r in (p.get("overnights") or [])
          if (r.get("night") or r.get("date") or "") >= day]
    if ov:
        body["overnights"] = ov
    body["requested_off"] = [x for x in (p.get("requested_off") or []) if x >= day]
    body.update({"start_day": day, "replace_from_day": True, "day_legs_min": {k: v for k, v in mins.items() if v}})
    if not body["day_legs_min"]:
        body.pop("day_legs_min")
    return generate(data_dir, airline, body, career_path, user_id=user_id, share_stats=share_stats)


def _min_note(db, sid: int, p: dict):
    """Diz no diagnóstico, dia a dia, se o mínimo de pernas pedido saiu (contado na escala pronta, pela data local)."""
    from datetime import datetime, timezone
    from .aeroscale import catalog
    from .aeroscale.airports import get_tz, to_local
    apm = catalog.airport_map(db)
    n = {}
    for r in db.execute("SELECT adep, dep_utc, notes FROM schedule_legs WHERE schedule_id=? AND data_origin<>'SUGESTAO'", (sid,)):
        if "DHD" in (r[2] or "").upper() or "extra" in (r[2] or "").lower():
            continue
        dt = datetime.fromisoformat(r[1].replace("Z", "+00:00"))
        ld = to_local(dt, catalog.airport_tz(apm, r[0])).date().isoformat()
        n[ld] = n.get(ld, 0) + 1
    rows_ = json.loads(db.execute("SELECT diagnostics FROM schedules WHERE id=?", (sid,)).fetchone()[0] or "[]")
    pin = []
    for d, need in sorted(p["day_legs_min"].items()):
        got = n.get(d, 0)
        dm = f"{d[8:10]}/{d[5:7]}"
        pin.append({"level": "info" if got >= need else "warning", "code": "minimo_pernas_ok" if got >= need else "minimo_pernas_falhou", "date": d,
                    "message": f"Mínimo de {need} perna(s) em {dm}: " + (f"encaixado ({got} perna(s))." if got >= need else
                               f"não deu ({got} perna(s)). Nenhuma sequência de voos reais do RPL com essa quantidade coube nas regras de "
                               "jornada e no horário do dia (ou o dia era reserva ou folga fixa).")})
    db.execute("UPDATE schedules SET diagnostics=? WHERE id=?", (json.dumps(pin + rows_, ensure_ascii=False), sid))


def _follow_note(db, sid: int, p: dict):
    """Diz no diagnóstico quantas vezes a escala seguiu a continuação do avião."""
    from . import continuacao
    legs = [dict(r) for r in db.execute("SELECT flight_number, adep, ades, dep_utc FROM schedule_legs WHERE schedule_id=? ORDER BY dep_utc", (sid,))]
    from .planner_ext import follow_get
    fol = follow_get(p["follow"])
    n = continuacao.summarize(legs, fol)
    fs = p.get("follow_strength")
    note = {"level": "info", "code": "continuacoes",
            "message": f"Continuações do avião (força {fs}): {n['known']} voo(s) da escala têm continuação conhecida; em "
                       f"{n['regra'] + n['auto']} o voo seguinte foi ela ({n['regra']} pelas suas regras, {n['auto']} pelas "
                       "identificadas no RPL e no ADS-B). Nos outros, o seu dia acabou ali (fim da jornada, folga ou fim do "
                       "período: o avião segue com outra tripulação), a continuação não existia naquele dia, ou a força sorteou deixar livre."}
    dg = json.loads(db.execute("SELECT diagnostics FROM schedules WHERE id=?", (sid,)).fetchone()[0] or "[]")
    db.execute("UPDATE schedules SET diagnostics=? WHERE id=?", (json.dumps(dg[:1] + [note] + dg[1:], ensure_ascii=False), sid))


def continuacao_default() -> int:
    from .continuacao import DEFAULT_STRENGTH
    return DEFAULT_STRENGTH


def generate(data_dir, airline: str, raw: dict, career_path: Path, user_id=None, share_stats: bool = False) -> dict:
    """Começa a geração em segundo plano. Devolve o estado da tarefa (a tela acompanha o progresso)."""
    from . import careers
    p = clean_params(airline, raw)
    if careers.PROFILES.get(airline, {}).get("source") != "aeroscale":
        raise Conflict("Esta companhia ainda não tem fonte de escala no motor.", code="sem_fonte")
    start_pos = None
    from_pos = p.pop("from_position", None)
    start_day = p.pop("start_day", None)
    replace = p.pop("replace_from_day", True) is not False
    cut_day = cut_until = None
    if from_pos and not start_day:
        start_pos = _start_position(career_path)
    dbp = init(data_dir)
    c = edb.connect(str(dbp))
    try:
        rp = scheduler.resolve_params(c, dict(p))     # valida já (erro de período aparece na hora, não depois)
        if start_day:
            cut_day, cut_until, start_pos = _from_day(p, rp, str(start_day)[:10], career_path, replace, from_pos)
        elif start_pos:
            _clip_to_position(p, rp, start_pos)
        fm = p.pop("follow_mode", None)                   # formato da 3.0.2 beta 1 (regras/beta/off)
        fs = p.pop("follow_strength", None)
        if fm is not None and fm not in ("regras", "beta", "off"):
            raise FGVError("Continuações: regras, beta ou off.", code="campo_invalido")
        auto = fm != "regras"
        if fs is None or fs == "":
            fs = {"off": 0, "regras": 100, "beta": continuacao_default()}.get(fm, continuacao_default())
        try:
            fs = int(fs)
        except (TypeError, ValueError):
            raise FGVError("Força das continuações: um número de 0 a 100.", code="campo_invalido") from None
        if not 0 <= fs <= 100:
            raise FGVError("Força das continuações: de 0 a 100.", code="campo_invalido")
        from . import continuacao
        from .db import connect as lconnect
        cc1 = lconnect(career_path)
        try:
            fol = continuacao.follow_map(cc1, data_dir, airline, fs, auto=auto)
        finally:
            cc1.close()
        if fol:
            from .planner_ext import follow_stash
            p["follow"] = follow_stash(fol)
        p["follow_strength"] = fs
        dlm = p.pop("day_legs_min", None)
        if dlm:
            if not isinstance(dlm, dict):
                raise FGVError("Mínimo de pernas por dia: {\"AAAA-MM-DD\": n}.", code="campo_invalido")
            rp = scheduler.resolve_params(c, dict(p))
            out = {}
            for k, v in dlm.items():
                try:
                    dd, n = date.fromisoformat(str(k)[:10]), int(v)
                except (TypeError, ValueError):
                    raise FGVError(f"Mínimo de pernas: dia ou número inválido ({k}: {v}).", code="campo_invalido") from None
                if not 1 <= n <= 8:
                    raise FGVError("Mínimo de pernas por dia: de 1 a 8.", code="campo_invalido")
                if not rp["start_date"] <= dd.isoformat() <= rp["end_date"]:
                    raise FGVError(f"O dia {dd:%d/%m} está fora do período da escala.", code="campo_invalido")
                if dd.isoformat() in (p.get("requested_off") or []):
                    raise FGVError(f"{dd:%d/%m} é folga pedida: não dá para exigir voos nesse dia.", code="campo_invalido")
                out[dd.isoformat()] = n
            p["day_legs_min"] = out
        if p.get("overnights"):
            from . import pernoite
            from .db import connect as lconnect
            rp = scheduler.resolve_params(c, dict(p))
            cc0 = lconnect(career_path)
            try:
                p["overnights"] = pernoite.parse_requests(cc0, p["overnights"], rp["start_date"], rp["end_date"], p.get("requested_off"))
            finally:
                cc0.close()
            if not p["overnights"]:
                p.pop("overnights")
    except FGVError:
        raise
    except Exception as e:
        raise FGVError(str(e), code="parametros_invalidos")
    finally:
        c.close()

    def run(db, progress, cancel):
        progress(0.02, "lendo o RPL")
        rp2 = scheduler.resolve_params(db, dict(p))
        gone = _supersede(db, airline, rp2["start_date"], rp2["end_date"])
        if gone:
            log.info("motor: %d escala(s) antiga(s) de %s no período substituída(s)", gone, airline)
        try:
            sid = _generate_with_nights(db, p, start_pos, progress)
            if p.get("follow"):
                _follow_note(db, sid, p)
            if p.get("day_legs_min"):
                _min_note(db, sid, p)
        finally:
            from .planner_ext import follow_drop
            follow_drop(p.get("follow"))
        mode = p.get("days_off_mode") or "auto"
        if mode != "auto":
            offs = p.get("requested_off") or []
            note = ({"level": "warning", "code": "sem_folgas",
                     "message": "Sem folgas, como você pediu: as folgas regulamentares (Lei 13.475, arts. 50 e 51) ficaram desligadas "
                                "nesta escala e o painel não cobra folga mínima nem dias seguidos. Os outros limites (jornada, repouso, "
                                "horas de voo do mês) continuam valendo, então pode haver dia sem voo quando eles apertam."}
                    if mode == "none" else
                    {"level": "info", "code": "folgas_manuais",
                     "message": f"Folgas manuais: só as {len(offs)} que você marcou (" + ", ".join(f"{x[8:10]}/{x[5:7]}" for x in sorted(offs)[:12])
                                + ("…" if len(offs) > 12 else "") + "). As folgas regulamentares automáticas ficaram desligadas."})
            dg = json.loads(db.execute("SELECT diagnostics FROM schedules WHERE id=?", (sid,)).fetchone()[0] or "[]")
            db.execute("UPDATE schedules SET diagnostics=? WHERE id=?", (json.dumps([note] + dg, ensure_ascii=False), sid))
        db.commit()
        progress(0.95, "entregando a escala à carreira")
        from .aeroscale_import import import_backup
        from .db import connect as lconnect
        cc = lconnect(career_path)
        try:
            if cut_day:
                if not db.execute("SELECT COUNT(*) FROM schedule_legs WHERE schedule_id=?", (sid,)).fetchone()[0]:
                    raise FGVError("O motor não achou nenhuma perna para esse período; a sua escala ficou como estava.",
                                   code="escala_vazia")
                from . import posicao
                posicao.cut_from(cc, cut_day, user_id=user_id, until=cut_until)   # só agora, com a escala nova pronta para entrar
            st = import_backup(cc, str(dbp), user_id=user_id, airline=airline, schedule_ids=[sid])
            link = _link_note(cc, db, sid) if start_day else None
            if start_pos:                                    # a escala nova já começa dali: a posição memorizada cumpriu o papel
                cc.execute("DELETE FROM setting WHERE key='posicao_memorizada'")
                cc.commit()
        finally:
            cc.close()
        s = scheduler.serialize_schedule(db, sid)
        diags = (s.get("diagnostics") or [])[:12]
        if start_day and link:
            diags = [link] + diags
        out = {"schedule_id": sid, "name": s.get("name"), "start_date": s.get("start_date"), "end_date": s.get("end_date"),
               "legs": len(s.get("legs") or []), "diagnostics": diags,
               "import": {k: st.get(k) for k in ("legs_new", "legs_changed", "legs_removed", "legs_unchanged", "legs_total", "warnings")}}
        if user_id is not None:
            from . import careers, pernoite
            pernoite.sync_later(careers.core_path(Path(data_dir)), career_path, user_id)   # os avisos de pernoite seguem a escala nova
        if out["legs"] and share_stats:
            from . import careers, estatisticas
            estatisticas.send_generated(careers.core_path(Path(data_dir)), career_path, airline, out, p)   # em segundo plano
        return out
    return jobs.start(str(dbp), f"Escala {airline}", run)


NIGHT_TRIES = 4      # pernoite pedido: até 4 sementes; fica a escala que atende mais pedidos


def _generate_with_nights(db, p: dict, start_pos, progress) -> int:
    """scheduler.generate (ou generate_from) do AeroScale. Com pernoite pedido, o Planner termina o dia da noite pedida no
    aeroporto pedido (must_end do motor). Se a sequência sorteada não deixar, tenta outras sementes e fica com a que
    atende mais pedidos; as tentativas descartadas saem do motor (não ocupam voos de ninguém)."""
    from . import pernoite, planner_ext
    gen = lambda pp, prog: generate_from(db, pp, start_pos, progress=prog) if start_pos else scheduler.generate(db, pp, progress=prog)  # noqa: E731
    if not p.get("overnights"):
        with planner_ext.planner_for(p):
            return gen(p, progress)
    import random as _r
    want = len(p["overnights"])
    best, best_ok, tried, best_seed = None, -1, 0, None
    seed0 = p.get("seed")
    for k in range(NIGHT_TRIES):
        pk = dict(p)
        if seed0 is None:
            seed0 = _r.SystemRandom().randint(1, 2_000_000_000)
        pk["seed"] = seed0 if k == 0 else (int(seed0) * 1_000_003 + k) % 2_000_000_000   # mesma semente, mesmas tentativas
        span = (0.05 + 0.85 * k / NIGHT_TRIES, 0.05 + 0.85 * (k + 1) / NIGHT_TRIES)
        prog = (lambda a, b: (lambda f, st=None: progress(a + (b - a) * f, (f"tentativa {k + 1}: " + st) if st else None)))(*span)
        with planner_ext.planner_for(pk):
            sid = gen(pk, prog)
        tried += 1
        diags = json.loads(db.execute("SELECT diagnostics FROM schedules WHERE id=?", (sid,)).fetchone()[0] or "[]")
        try:                                       # escala com erro de jornada/repouso nunca ganha de uma legal
            errs = scheduler.crew_report(db, scheduler.serialize_schedule(db, sid))["summary"].get("errors") or 0
        except Exception:                          # noqa: BLE001
            errs = 0
        ok = pernoite.results(diags)["ok"] - (100 if errs else 0)
        if ok > best_ok:
            if best is not None:
                db.execute("DELETE FROM schedules WHERE id=?", (best,))
            best, best_ok, best_seed = sid, ok, pk.get("seed")
        else:
            db.execute("DELETE FROM schedules WHERE id=?", (sid,))
        if best_ok >= want:
            break
    diags = json.loads(db.execute("SELECT diagnostics FROM schedules WHERE id=?", (best,)).fetchone()[0] or "[]")
    best_ok = pernoite.results(diags)["ok"]
    mine = [x for x in diags if x.get("code") in ("pernoite_pedido_ok", "pernoite_pedido_falhou", "pernoite_folga")]
    diags = mine + [x for x in diags if x not in mine]                    # o que você pediu vem primeiro
    diags.insert(0, {"level": "info" if best_ok >= want else "warning", "code": "pernoites_pedidos",
                     "message": f"Pernoites pedidos: {best_ok} de {want} atendido(s)"
                                + (f" (melhor de {tried} tentativas com sementes diferentes)" if tried > 1 else "")
                                + f". Semente desta escala: {best_seed}."})
    db.execute("UPDATE schedules SET diagnostics=? WHERE id=?", (json.dumps(diags, ensure_ascii=False), best))
    return best


# ------------------------------------------------------------------ começar de onde o avião está
def _start_position(career_path: Path) -> dict:
    """A posição memorizada ao apagar a escala (ou, sem ela, o fim do último voo de hoje) e o histórico antes dela."""
    from . import posicao
    from .db import connect as lconnect
    cc = lconnect(career_path)
    try:
        pos = posicao.memorized(cc) or posicao.current(cc)
        if not pos or not pos.get("icao"):
            raise FGVError("Não sei onde o seu avião está: sem escala nem base.", code="sem_posicao")
        later = cc.execute("SELECT flight_number, dep_utc FROM roster_leg WHERE removed_batch IS NULL AND activity<>'SOBREAVISO' "
                           "AND dep_utc>? ORDER BY dep_utc LIMIT 1", (pos["at_utc"],)).fetchone()
        if later:
            raise Conflict(f"A sua escala ainda continua depois de {pos['icao']} ({later[0] or 'voo'} em {later[1][:10]}). "
                           "Apague daqui pra frente antes de gerar a partir da posição.", code="escala_continua")
        return {**pos, "history": posicao.history(cc, pos)}
    finally:
        cc.close()


def _from_day(p: dict, rp: dict, day: str, career_path: Path, replace: bool, from_pos) -> tuple[str | None, dict | None]:
    """Gerar a partir do dia escolhido (o mensal sempre começaria no dia 1): o período vai do dia até o fim do que foi escolhido.
    Se já há escala desse dia em diante, ela é substituída (com replace) e a nova continua de onde o avião estava antes."""
    from . import posicao
    from .db import connect as lconnect
    try:
        d = date.fromisoformat(day)
    except ValueError:
        raise FGVError("Dia inicial inválido.", code="parametros_invalidos")
    start, end = date.fromisoformat(rp["start_date"]), date.fromisoformat(rp["end_date"])
    if not (start <= d <= end):
        raise FGVError(f"O dia {d:%d/%m} está fora do período escolhido ({start:%d/%m} a {end:%d/%m}).", code="parametros_invalidos")
    if d != start:
        p["kind"], p["start_date"], p["end_date"] = "avulsa", d.isoformat(), end.isoformat()
        p.pop("month", None)
        if (end - d).days > 61:
            p["end_date"] = (d + timedelta(days=61)).isoformat()
    until = p["end_date"] if p.get("end_date") else end.isoformat()
    cc = lconnect(career_path)
    try:
        existing = posicao.legs_from(cc, d.isoformat(), until)
        flying = [l for l in existing if l["flight_state"] in posicao.FLOWN]
        if flying:
            raise Conflict(f"O {flying[0]['flight_number'] or 'voo'} de {flying[0]['local_date']} já foi voado (ou está em voo): "
                           "escolha um dia depois dele.", code="escala_continua")
        if existing and not replace:
            raise Conflict(f"Já tem escala a partir de {d:%d/%m} ({len(existing)} etapa(s)). Marque “substituir” para refazer "
                           "desse dia em diante.", code="escala_continua")
        pos = (posicao.memorized(cc) if from_pos else None) or posicao.position_before(cc, d.isoformat())
        if pos:
            pos = {**pos, "history": posicao.history(cc, pos)}
    finally:
        cc.close()
    return (d.isoformat() if existing else None), until, pos


def _link_note(cc, db, sid) -> dict | None:
    """A escala que já existia depois do período: a nova termina onde ela começa?"""
    last = db.execute("SELECT ades, arr_utc FROM schedule_legs WHERE schedule_id=? ORDER BY dep_utc DESC LIMIT 1", (sid,)).fetchone()
    if not last:
        return None
    nxt = cc.execute("SELECT flight_number, adep, dep_utc, local_date FROM roster_leg WHERE removed_batch IS NULL AND activity<>'SOBREAVISO' "
                     "AND dep_utc>? AND uid NOT IN (SELECT uid FROM roster_leg WHERE schedule_id=(SELECT MAX(id) FROM roster_schedule)) "
                     "ORDER BY dep_utc LIMIT 1", (last["arr_utc"],)).fetchone()
    if not nxt or nxt["adep"] == last["ades"]:
        return None
    return {"level": "warning", "code": "ligacao",
            "message": f"A escala que já existia continua em {nxt['local_date']} saindo de {nxt['adep']} ({nxt['flight_number'] or 'voo'}), "
                       f"mas a nova termina em {last['ades']}. Gere de novo cobrindo esses dias, ou peça uma troca."}


def _clip_to_position(p: dict, rp: dict, pos: dict) -> None:
    """A escala nova começa no dia seguinte ao último voo e vai até o fim do período escolhido (no máximo 62 dias)."""
    day = date.fromisoformat(pos["date"]) + timedelta(days=1)
    start, end = date.fromisoformat(rp["start_date"]), date.fromisoformat(rp["end_date"])
    if end < day:
        raise FGVError(f"O período escolhido termina antes de {day:%d/%m}, o primeiro dia depois do seu último voo.",
                       code="parametros_invalidos")
    if start != day:                                  # sempre no dia seguinte ao último voo, sem buraco nem sobreposição
        p["kind"], p["start_date"], p["end_date"] = "avulsa", day.isoformat(), end.isoformat()
        p.pop("month", None)
        if (end - day).days > 61:
            p["end_date"] = (day + timedelta(days=61)).isoformat()


def generate_from(db, raw_params: dict, pos: dict, progress=None) -> int:
    """O mesmo scheduler.generate do AeroScale, com o planejador começando onde o avião está (Planner.plan aceita a
    posição e o horário de partida; seed_history carrega jornada e repouso dos dias anteriores, como no regerar)."""
    from datetime import datetime as _dt, time as _time, timezone as _tz
    from .aeroscale.airports import get_tz
    S = scheduler
    p = S.resolve_params(db, raw_params)
    pl = S.Planner(db, p)
    pl.progress = progress
    pl.choose_aircraft()
    pl.resolve_turn()
    base = pl.choose_base()
    p["base_resolved"] = base
    pl.build_pool(pl.start, pl.end)
    pl.busy_sets()
    pl.taken = set()
    if p["reserve_blocks"]:
        pl.draw_reserves(base)
    hist = pos.get("history") or []
    if hist:
        pl.seed_history(hist, continuing=False)
    tz = get_tz(pl.tz(pos["icao"])) or _tz.utc
    t_day = _dt.combine(pl.start, _time(0, 0), tz).astimezone(_tz.utc)
    t_arr = _dt.strptime(pos["at_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_tz.utc)
    legs = pl.plan(pos["icao"], max(t_day, t_arr), pl.start, pl.end, base, had_legs=bool(hist))
    pl.diag("info", "posicao", f"Começa em {pos['icao']}, onde o seu avião parou"
            + (f" (fim do {pos['flight_number']}" + (f", {pos['at_local']} local" if pos.get("at_local") else "") + ")" if pos.get("flight_number") else "")
            + f", com a base em {base}. Jornada e repouso dos dias anteriores contam.")
    cf, ct = pl.idx.coverage()
    if any(o["origin"] == "PROJETADO" for o in legs):
        n_proj = sum(1 for o in legs if o["origin"] == "PROJETADO")
        pl.diag("info", "projecao", f"{n_proj} perna(s) em dias ainda sem RPL publicado (o RPL ativo vale de {cf} a {ct}): repetem a "
                                    "semana mais nova do RPL e estão marcadas como projetadas. Use Atualizar RPLs para trazer o mais novo.")
    S._intl_diag(pl, legs)
    S._mark_swaps(pl, legs)
    S._ops_diag(pl, legs, base)
    S._pref_diag(pl, legs)
    S._legs_diag(pl)
    if not legs:
        pl.diag("error", "vazia", "Nenhuma perna gerada com os critérios informados. Relaxe filtros "
                                  "(tempo de solo, duração, janela) ou mude a base.")
    ac = pl.aircraft
    name = (raw_params.get("name") or "").strip() or (
        f"{p['airline']} {ac['registration'] if ac else pl.ac_type} — base {base} — {pl.start:%d/%m} a {pl.end:%d/%m/%Y}")
    ts = S.now_iso()
    p["icao_type_resolved"] = pl.ac_type
    p["start_position"] = {k: pos.get(k) for k in ("icao", "at_utc", "flight_number")}
    sid = db.execute(
        "INSERT INTO schedules(name, airline, kind, start_date, end_date, aircraft_id, params, diagnostics,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (name, p["airline"], p["kind"], p["start_date"], p["end_date"], ac["id"] if ac else None,
         json.dumps(p, ensure_ascii=False, default=str), json.dumps(pl.diags, ensure_ascii=False), ts, ts)).lastrowid
    S.reserve_mod.save(db, sid, pl.reserves)
    S._insert_legs(db, sid, legs, ac, reserve_ids={r["uid"]: r["id"] for r in pl.reserves}, ac_type=pl.ac_type)
    S.save_days(db, sid, pl.day_records)
    return sid


def job(jid: str) -> dict:
    j = jobs.get(jid)
    if not j:
        raise NotFound("Tarefa não encontrada (o servidor reiniciou?).")
    return j


def cancel(jid: str) -> dict:
    return {"cancelled": jobs.cancel(jid)}


def update_rpl(data_dir, airline: str) -> dict:
    """Botão "Atualizar RPL" (no máximo 1 requisição por minuto por companhia, regra do AeroScale)."""
    dbp, store = paths(data_dir)
    c = connect(data_dir)
    try:
        try:
            r = rpl_update.update(c, str(store), airlines=[airline], auto=False, log=None)
        except rpl_update.Busy as e:
            raise Conflict(str(e), code="ocupado")
        c.commit()
        return r
    finally:
        c.close()


def import_real(data_dir, airline: str, text: str, month: str, career_path: Path, user_id=None, base: str | None = None) -> dict:
    """Escala real colada em texto (como circula entre tripulantes): o motor casa os voos com o RPL e a carreira recebe."""
    from .aeroscale import roster_import
    dbp = init(data_dir)
    c = edb.connect(str(dbp))
    try:
        res = roster_import.import_text(c, text, airline, month, base=base or None)
        c.commit()
    except FGVError:
        raise
    except Exception as e:
        raise FGVError(str(e), code="escala_invalida")
    finally:
        c.close()
    sid = (res.get("id") or res.get("schedule_id")) if isinstance(res, dict) else res
    from .aeroscale_import import import_backup
    from .db import connect as lconnect
    cc = lconnect(career_path)
    try:
        st = import_backup(cc, str(dbp), user_id=user_id, airline=airline, schedule_ids=[sid])
    finally:
        cc.close()
    return {"schedule_id": sid, "engine": res if isinstance(res, dict) else None, "import": st}
