"""API HTTP do LineOps (FastAPI). Autenticação obrigatória em tudo, exceto /api/health e a configuração inicial."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (DISCLAIMER, __version__, acars_service as acars, aeroscale_import, avatar, backup, biblioteca, conquistas, continuacao, estatisticas, exportar, fleet, frota_ref, inativo, pernoite, senha, legacy, flights, journey,
               careers, carreira, enroute, equipe, externos, feed, ivao, motor, notify, stay, tripulacao, loadsheet as ls_mod, logs, validity, manual, network, ofp as ofp_mod, payroll, pireps, rules, security, troca, posicao, visao)
from .db import audit, connect, migrate, now_iso, one, pending, rows, tx
from .errors import FGVError, NotFound

WEB = Path(__file__).parent / "web"
MAX_UPLOAD = 300 * 1024 * 1024
COOKIE = "lineops_session"


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=200)
    display_name: str | None = Field(default=None, max_length=80)


class TokenReq(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class SwapReq(BaseModel):
    leg_id: int
    kind: str
    ref: str | None = None
    reason: str | None = Field(default=None, max_length=300)
    destino: str | None = Field(default=None, pattern=r"^[A-Za-z0-9]{3,4}$")   # ICAO ou IATA


class BookReq(BaseModel):
    aircraft_id: int | None = None
    force_type: bool = False
    notes: str | None = None


class UnplannedReq(BaseModel):
    airline: str | None = None
    flight_number: str | None = None
    adep: str
    ades: str
    type_icao: str | None = None
    aircraft_id: int
    dep_utc: str | None = None
    arr_utc: str | None = None
    notes: str | None = None
    force_type: bool = False


class AssignReq(BaseModel):
    aircraft_id: int
    force_type: bool = False


class SessionReq(BaseModel):
    flight_id: int
    sim: str | None = None
    client_version: str | None = None
    confirm_restrictions: bool = False


class ReportReq(BaseModel):
    points: list[dict] = []
    messages: list[dict] = []
    snapshot: dict | None = None


class ApproveReq(BaseModel):
    overrides: dict = {}
    notes: str | None = None


class ReasonReq(BaseModel):
    reason: str | None = None
    notes: str | None = None


def _check_date(date: str):
    import re
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        raise FGVError("Data inválida (use AAAA-MM-DD).", code="data_invalida")


def create_app(data_dir: str | Path) -> FastAPI:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs.setup(data_dir)
    log = logging.getLogger("lineops")
    restored = backup.apply_pending_restore(data_dir)     # restauração pedida na tela: antes de abrir qualquer banco
    if restored:
        (log.info if restored.get("ok") else log.error)("restauração agendada: %s", restored)
    old_dbs = [] if careers.core_path(data_dir).exists() else [data_dir / n for n in careers.LEGACY if (data_dir / n).exists()]
    to_check = careers.all_db_files(data_dir) + old_dbs
    pend = []
    for f in to_check:
        c = connect(f)
        try:
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user'").fetchone():
                pend += pending(c)
        finally:
            c.close()
    if not (restored and restored.get("ok")):
        try:                                   # atualização: foto de tudo antes de mexer em qualquer coisa
            b = backup.before_update(data_dir, bool(pend))
            if b:
                log.info("backup antes da atualização (%s): %s", ", ".join(sorted(set(pend))) or "sem mudança de banco", b.name)
        except Exception:
            log.exception("não foi possível fazer o backup antes da atualização")
    backup.mark_version(data_dir)
    adopted = careers.bootstrap(data_dir)
    if adopted:
        log.info("banco antigo %s virou a carreira GOL", adopted)
    core_path = careers.core_path(data_dir)
    boot = connect(core_path)
    migrate(boot)
    for f in careers.career_files(data_dir):
        if f.stem.upper() not in careers.PROFILES:
            log.warning("banco de carreira desconhecida ignorado: %s", f.name)
            continue
        careers.ensure_career(data_dir, boot, f.stem)
        c = connect(f)
        try:
            with tx(c):
                journey.backfill_local_dates(c)
        finally:
            c.close()
    boot.close()
    db_path = core_path
    log.info("LineOps %s iniciado; dados em %s", __version__, data_dir)
    throttle = security.LoginThrottle()
    if os.environ.get("LINEOPS_IVAO_POLLER", "1") != "0":       # laço da IVAO: dorme enquanto não há voo acompanhado
        poller = ivao.Poller(data_dir)
        poller.start()
    else:
        poller = None
    app = FastAPI(title="LineOps", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.db_path, app.state.data_dir, app.state.ivao_poller, app.state.log_path = db_path, data_dir, poller, log_path
    try:
        motor.start_background(data_dir)                      # RPL mais novo ao abrir (no máximo a cada 12 h; sem internet, nada muda)
    except Exception:
        log.exception("motor de escalas")
    if os.environ.get("LINEOPS_NOTIFY", "1") != "0":            # celular: fila de envio e o Telegram ouvindo
        app.state.notify = (notify.Worker(data_dir), notify.TelegramPoller(data_dir))
        for t in app.state.notify:
            t.start()

    @app.exception_handler(FGVError)
    async def _fgv(_r, e: FGVError):
        return JSONResponse({"error": e.code, "message": e.message, "details": e.details}, status_code=e.status)

    @app.exception_handler(Exception)
    async def _boom(_r, e: Exception):
        code = uuid.uuid4().hex[:8]
        log.exception("erro interno %s em %s %s", code, _r.method, _r.url.path)
        return JSONResponse({"error": "erro_interno", "message": f"Erro interno (código {code}). O detalhe está no log, em Ajustes → Sobre."},
                            status_code=500)

    @app.exception_handler(RequestValidationError)
    async def _val(_r, e: RequestValidationError):
        return JSONResponse({"error": "requisicao_invalida", "message": "Dados inválidos.", "details": [
            {"campo": ".".join(str(x) for x in err["loc"][1:]), "problema": err["msg"]} for err in e.errors()]}, status_code=422)

    def get_core():
        c = connect(core_path)
        try:
            yield c
        finally:
            c.close()

    used = {"ok": False}

    def auth(request: Request, conn=Depends(get_core)) -> dict:
        tok, via_cookie = None, False
        h = request.headers.get("authorization", "")
        if h.lower().startswith("bearer "):
            tok = h[7:].strip()
        elif request.cookies.get(COOKIE):
            tok, via_cookie = request.cookies[COOKIE], True
        if not tok:
            raise FGVError("Autenticação necessária.", code="nao_autenticado", status=401)
        if via_cookie and request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-requested-with") != "lineops":
            raise FGVError("Requisição bloqueada (proteção CSRF).", code="csrf", status=403)
        u = security.lookup_token(conn, tok)
        if not u:
            raise FGVError("Sessão inválida ou expirada.", code="nao_autenticado", status=401)
        u["via"] = "cookie" if via_cookie else "bearer"
        if not used["ok"]:                     # primeiro uso de verdade: a cópia dos dados do GolOps nunca mais é refeita sozinha
            used["ok"] = True
            try:
                legacy.mark_used(conn)
            except Exception:                  # nunca pode impedir o login
                log.exception("não foi possível marcar o primeiro uso")
        u["career_chosen"] = bool(u.get("career"))
        u["career"] = (u.get("career") or careers.last_used(conn) or careers.DEFAULT).upper()
        u["pilot_code"] = careers.pilot_code(u.get("pilot_code"), u["career"])     # GOL001 só na GOL: segue a companhia
        return u

    def get_db(u=Depends(auth), core=Depends(get_core)):
        """Banco da carreira da sessão (a companhia escolhida no login)."""
        path = careers.career_path(data_dir, u["career"])
        if not path.exists():
            careers.ensure_career(data_dir, core, u["career"])
        c = connect(path)
        try:
            careers.sync_settings(core, c)
            careers.mirror_identity(c, u)                 # para as chaves estrangeiras da carreira (ACARS, PIREP)
            try:
                frota_ref.ensure(c, u["career"])          # a lista de frota que você mandou (uma vez por versão)
            except Exception:                             # noqa: BLE001
                log.exception("frota de referência")
            yield c
        finally:
            c.close()

    def session_only(u=Depends(auth)):
        if u["kind"] != "session":
            raise FGVError("Esta operação exige login interativo (token de ACARS não pode).", code="proibido", status=403)
        return u

    # ----------------------------------------------------------------- público
    @app.get("/api/health")
    def health(request: Request, conn=Depends(get_core)):
        tok = request.cookies.get(COOKIE)
        return {"ok": True, "authenticated": bool(tok and security.lookup_token(conn, tok)), "app": "LineOps", "version": __version__, "disclaimer": DISCLAIMER,
                "needs_setup": conn.execute("SELECT COUNT(*) FROM user").fetchone()[0] == 0}

    @app.post("/api/setup")
    def setup(c: Credentials, response: Response, conn=Depends(get_core)):
        with tx(conn):
            if conn.execute("SELECT COUNT(*) FROM user").fetchone()[0]:
                raise FGVError("O LineOps já foi configurado.", code="ja_configurado", status=409)
            uid = conn.execute("INSERT INTO user(username, pw_hash, display_name, created_at) VALUES (?,?,?,?)",
                               (c.username, security.hash_password(c.password), c.display_name or c.username, now_iso())).lastrowid
            audit(conn, uid, "user.setup", "user", uid)
            tok = security.new_token(conn, uid, "session")
        careers.ensure_career(data_dir, conn, careers.DEFAULT)
        _set_cookie(response, tok)
        return {"ok": True}

    @app.post("/api/login")
    def login(c: Credentials, response: Response, request: Request, conn=Depends(get_core)):
        key = c.username.lower()
        wait = throttle.check(key)
        if wait:
            raise FGVError(f"Muitas tentativas. Aguarde {wait}s.", code="muitas_tentativas", status=429)
        u = one(conn.execute("SELECT * FROM user WHERE username=?", (c.username,)))
        if not u or not security.verify_password(c.password, u["pw_hash"]):
            throttle.fail(key)
            raise FGVError("Usuário ou senha incorretos.", code="credenciais", status=401)
        throttle.ok(key)
        with tx(conn):
            tok = security.new_token(conn, u["id"], "session")
            audit(conn, u["id"], "user.login", "user", u["id"], {"ip": request.client.host if request.client else None})
        _set_cookie(response, tok)
        return {"ok": True}

    # ----------------------------------------------------------------- senha esquecida (só no próprio PC)
    def _local_only(request: Request):
        if not senha.is_local(request.client.host if request.client else None):
            raise FGVError("Por segurança, a senha só se redefine no computador onde a LineOps está instalada.",
                           code="so_local", status=403)
        if request.headers.get("x-requested-with") != "lineops":
            raise FGVError("Requisição bloqueada (proteção CSRF).", code="csrf", status=403)

    @app.post("/api/senha/codigo")
    def password_code(request: Request, core=Depends(get_core)):
        _local_only(request)
        if throttle.check("senha:codigo"):
            raise FGVError("Aguarde um pouco antes de pedir outro código.", code="muitas_tentativas", status=429)
        throttle.fail("senha:codigo")                     # no máximo alguns códigos seguidos
        return senha.start_local(core, data_dir)

    @app.post("/api/senha/redefinir")
    def password_reset(request: Request, b: dict = Body(...), core=Depends(get_core)):
        _local_only(request)
        if set(b) - {"username", "code", "password"}:
            raise FGVError("Campos desconhecidos.", code="campo_invalido")
        return senha.finish_local(core, data_dir, b.get("username") or "", str(b.get("code") or ""), b.get("password") or "")

    def _set_cookie(response, tok):
        response.set_cookie(COOKIE, tok, httponly=True, samesite="strict", max_age=security.SESSION_DAYS * 86400, path="/")

    @app.post("/api/logout")
    def logout(request: Request, response: Response, u=Depends(auth), conn=Depends(get_core)):
        conn.execute("UPDATE auth_token SET revoked_at=? WHERE id=?", (now_iso(), u["token_id"]))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def me(u=Depends(auth), conn=Depends(get_db)):
        p = careers.profile(u["career"])
        return {"id": u["id"], "username": u["username"], "display_name": u["display_name"], "pilot_code": u["pilot_code"], "token_kind": u["kind"],
                "features": {"payroll": payroll.enabled(conn), "pernoite_discord": pernoite.enabled_for(u)}, "career": p,
                "avatar": "/api/me/avatar" if avatar.of(u, data_dir) else None, "disclaimer": careers.disclaimer(u["career"])}

    @app.get("/api/me/avatar")
    def me_avatar(u=Depends(auth)):
        f = avatar.of(u, data_dir)
        if not f:
            raise NotFound("Sem foto de perfil.")
        return Response(f.read_bytes(), media_type=avatar.media_type(f), headers={"Cache-Control": "private, no-cache"})

    @app.put("/api/me/avatar")
    async def me_avatar_put(request: Request, u=Depends(session_only)):
        avatar.save(u, data_dir, await request.body())
        return {"ok": True, "avatar": "/api/me/avatar"}

    @app.delete("/api/me/avatar")
    def me_avatar_del(u=Depends(session_only)):
        avatar.remove(u, data_dir)
        return {"ok": True, "avatar": "/api/me/avatar" if avatar.of(u, data_dir) else None}

    @app.put("/api/me")
    def me_update(b: dict = Body(...), u=Depends(session_only), core=Depends(get_core)):
        if set(b) - {"display_name"}:
            raise FGVError("Campos desconhecidos.", code="campo_invalido")
        name = (b.get("display_name") or "").strip()
        if not (1 <= len(name) <= 40):
            raise FGVError("Nome de exibição: 1 a 40 caracteres.", code="campo_invalido")
        with tx(core):
            core.execute("UPDATE user SET display_name=? WHERE id=?", (name, u["id"]))
        return {"ok": True, "display_name": name}

    # ----------------------------------------------------------------- carreiras (modos por companhia)
    @app.get("/api/careers")
    def careers_list(u=Depends(auth), core=Depends(get_core)):
        return {"items": careers.list_careers(data_dir, core), "current": u["career"], "chosen": bool(u.get("career_chosen"))}

    @app.post("/api/careers/{code}/select")
    def careers_select(code: str, u=Depends(session_only), core=Depends(get_core)):
        return careers.select(data_dir, core, u["token_id"], code)

    @app.put("/api/careers/{code}/logo")
    async def careers_logo(code: str, request: Request, name: str = "logo.png", u=Depends(session_only), core=Depends(get_core)):
        data = await request.body()
        return careers.save_logo(data_dir, core, code, name, data)

    @app.delete("/api/careers/{code}/logo")
    def careers_logo_rm(code: str, u=Depends(session_only), core=Depends(get_core)):
        return careers.remove_logo(data_dir, core, code)

    @app.get("/api/careers/{code}/logo")
    def careers_logo_get(code: str, u=Depends(auth), core=Depends(get_core)):
        r = careers.logo_file(data_dir, core, code)
        if not r:
            raise NotFound("Sem logo.")
        return FileResponse(r[0], media_type=r[1], headers={"Cache-Control": "no-cache"})

    # ----------------------------------------------------------------- tokens do ACARS
    @app.get("/api/tokens")
    def tokens(u=Depends(session_only), conn=Depends(get_core)):
        return rows(conn.execute("SELECT id, name, created_at, last_used_at, career FROM auth_token WHERE user_id=? AND kind='api' AND revoked_at IS NULL "
                                 "AND COALESCE(career, ?)=? ORDER BY id DESC", (u["id"], careers.DEFAULT, u["career"])))

    @app.post("/api/tokens")
    def new_api_token(t: TokenReq, u=Depends(session_only), conn=Depends(get_core)):
        with tx(conn):
            tok = security.new_token(conn, u["id"], "api", t.name)
            conn.execute("UPDATE auth_token SET career=? WHERE id=(SELECT MAX(id) FROM auth_token)", (u["career"],))
            audit(conn, u["id"], "token.create", "auth_token", None, {"name": t.name})
        return {"token": tok, "note": "Guarde agora: o token não será mostrado novamente."}

    @app.delete("/api/tokens/{tid}")
    def revoke_token(tid: int, u=Depends(session_only), conn=Depends(get_core)):
        n = conn.execute("UPDATE auth_token SET revoked_at=? WHERE id=? AND user_id=? AND kind='api'", (now_iso(), tid, u["id"])).rowcount
        if not n:
            raise NotFound("Token não encontrado.")
        return {"ok": True}

    # ----------------------------------------------------------------- dashboard
    @app.get("/api/dashboard")
    def dashboard(u=Depends(auth), conn=Depends(get_db)):
        now = now_iso()
        nxt = rows(conn.execute("""SELECT f.id, f.flight_number, f.adep, f.ades, f.dep_utc, f.arr_utc, f.type_icao, f.state, a.registration FROM flight f
                                   LEFT JOIN aircraft a ON a.id=f.aircraft_id WHERE f.state='RESERVADO' ORDER BY COALESCE(f.dep_utc,'9999') LIMIT 5"""))
        imp = one(conn.execute("SELECT id, imported_at, source_name, source_version, source_created_at FROM import_batch ORDER BY id DESC LIMIT 1"))
        return {"now": now, "totals": pireps.logbook_totals(conn), "live": acars.live_status(conn), "next_flights": nxt,
                "pending_pireps": conn.execute("SELECT COUNT(*) FROM pirep WHERE state='PENDING'").fetchone()[0],
                "fleet": {"total": conn.execute("SELECT COUNT(*) FROM aircraft WHERE active=1").fetchone()[0]},
                "roster": {"legs": conn.execute("SELECT COUNT(*) FROM roster_leg WHERE removed_batch IS NULL").fetchone()[0], "last_import": imp},
                "disclaimer": careers.disclaimer(u["career"])}           # o aviso da companhia da sessão, como no /api/me

    # ----------------------------------------------------------------- jornada
    @app.get("/api/today")
    def today(u=Depends(auth), conn=Depends(get_db)):
        equipe.run(conn)
        if pernoite.enabled_for(u):                       # trocas aprovadas, cortes, regerações: os avisos do Discord seguem a escala
            pernoite.sync_later(careers.core_path(data_dir), careers.career_path(data_dir, u["career"]), u["id"], min_interval=300)
        d = journey.today_overview(conn)
        if pernoite.enabled_for(u) and d.get("legs"):
            pernoite.apply_marks(conn, d, pernoite.status_for(conn, [l["id"] for l in d["legs"] if l.get("id")]))
        d["messages"] = equipe.messages(conn, limit=6)
        d["message_counts"] = equipe.counts(conn)
        d["pilot"] = {"display_name": u["display_name"], "pilot_code": u["pilot_code"]}
        d["crew"] = tripulacao.pairing(conn, d["date"]) if any(l.get("is_flight") for l in d.get("legs") or []) else None
        d["stay_next"] = stay.upcoming(conn, limit=3)
        d["disclaimer"] = DISCLAIMER
        try:
            d["payroll"] = payroll.summary(conn)
        except Exception:                       # o painel não depende do módulo opcional
            d["payroll"] = None
        return d

    @app.get("/api/day/{date}")
    def day(date: str, u=Depends(auth), conn=Depends(get_db)):
        _check_date(date)
        d = journey.day(conn, date)
        d["swaps"] = troca.for_legs(conn, [l["id"] for l in d["legs"]])
        if pernoite.enabled_for(u):
            d["overnight_posts"] = pernoite.status_for(conn, [l["id"] for l in d["legs"]])
            pernoite.apply_marks(conn, d, d["overnight_posts"])
        return d

    # ----------------------------------------------------------------- aviso de pernoite no Discord (usuário cmdt079)
    @app.get("/api/pernoite/discord")
    def overnight_hook(u=Depends(auth), core=Depends(get_core)):
        return {**pernoite.webhook_info(core, u), "auto": pernoite.auto_enabled(core)}

    @app.put("/api/pernoite/auto")
    def overnight_auto(b: dict = Body(...), u=Depends(session_only), core=Depends(get_core)):
        on = pernoite.set_auto(core, bool(b.get("on")))
        if on:
            pernoite.sync_later(careers.core_path(data_dir), careers.career_path(data_dir, u["career"]), u["id"])
        return {"auto": on}

    @app.post("/api/pernoite/sincronizar")
    def overnight_sync(u=Depends(session_only), core=Depends(get_core), conn=Depends(get_db)):
        """Agora, na hora: edita/apaga os avisos enviados conforme a escala atual e, se ligado, manda os novos."""
        return pernoite.sync(conn, core, u)

    @app.put("/api/pernoite/discord")
    def overnight_hook_set(b: dict = Body(...), u=Depends(session_only), core=Depends(get_core)):
        if set(b) - {"url"}:
            raise FGVError("Campos desconhecidos.", code="campo_invalido")
        return pernoite.set_webhook(core, u, b.get("url"))

    def _accent(u):
        try:
            return int(str(careers.profile(u["career"]).get("accent") or "#FF6A13").lstrip("#"), 16)
        except ValueError:
            return None

    @app.post("/api/pernoite/{leg_id}/marcar")
    def overnight_mark(leg_id: int, u=Depends(session_only), conn=Depends(get_db), core=Depends(get_core)):
        return pernoite.mark(conn, core, u, leg_id, color=_accent(u))

    @app.post("/api/pernoite/{leg_id}/desmarcar")
    def overnight_unmark(leg_id: int, u=Depends(session_only), conn=Depends(get_db), core=Depends(get_core)):
        return pernoite.unmark(conn, core, u, leg_id)

    # ----------------------------------------------------------------- visões: mês, mapa, limites, passaporte, CSV
    def _check_month(ym: str):
        import re as _re
        if not _re.fullmatch(r"\d{4}-\d{2}", ym or "") or not 1 <= int(ym[5:7]) <= 12:
            raise FGVError("Mês inválido (AAAA-MM).", code="campo_invalido")

    @app.get("/api/month/{ym}")
    def month_view(ym: str, u=Depends(auth), conn=Depends(get_db)):
        _check_month(ym)
        return visao.month(conn, ym)

    @app.get("/api/mapa")
    def route_map(inicio: str | None = None, fim: str | None = None, fonte: str = "escala", u=Depends(auth), conn=Depends(get_db)):
        for x in (inicio, fim):
            if x:
                _check_date(x)
        return visao.route_map(conn, inicio, fim, "diario" if fonte == "diario" else "escala")

    @app.get("/api/limites/{ym}")
    def month_limits(ym: str, u=Depends(auth), conn=Depends(get_db)):
        _check_month(ym)
        return visao.limits(conn, ym)

    @app.get("/api/passaporte")
    def passport_view(u=Depends(auth), conn=Depends(get_db)):
        return visao.passport(conn)

    @app.get("/api/diario.csv")
    def logbook_csv(u=Depends(auth), conn=Depends(get_db)):
        return Response(visao.logbook_csv(conn), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="diario-{u["career"].lower()}.csv"'})

    # ----------------------------------------------------------------- escala em texto (exportador do AeroScale)
    def _roster_text(conn, inicio, fim, estilo, horarios):
        if not inicio or not fim:
            raise FGVError("Informe o período (inicio e fim).", code="campo_invalido")
        return exportar.roster_text(conn, inicio, fim, style=estilo, times=str(horarios).lower() in ("1", "true", "sim"))

    @app.get("/api/escala/texto")
    def roster_text(inicio: str = None, fim: str = None, estilo: str = None, horarios: str = "0", u=Depends(auth), conn=Depends(get_db)):
        return _roster_text(conn, inicio, fim, estilo, horarios)

    @app.get("/api/escala/texto.txt")
    def roster_text_file(inicio: str = None, fim: str = None, estilo: str = None, horarios: str = "0", u=Depends(auth), conn=Depends(get_db)):
        r = _roster_text(conn, inicio, fim, estilo, horarios)
        return Response(r["text"], media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="escala-{u["career"].lower()}-{r["start"]}-a-{r["end"]}.txt"'})

    @app.get("/api/conquistas")
    def achievements(u=Depends(auth), conn=Depends(get_db)):
        return conquistas.compute(conn)

    @app.get("/api/escala/agenda.ics")
    def roster_ics(inicio: str = None, fim: str = None, u=Depends(auth), conn=Depends(get_db)):
        if not inicio or not fim:
            raise FGVError("Informe o período (inicio e fim).", code="campo_invalido")
        r = exportar.roster_ics(conn, inicio, fim, careers.profile(u["career"]).get("name"))
        return Response(r["ics"], media_type="text/calendar; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="escala-{u["career"].lower()}-{inicio}-a-{fim}.ics"'})

    # ----------------------------------------------------------------- posição do avião / recomeçar a escala dali
    @app.get("/api/posicao")
    def position(u=Depends(auth), conn=Depends(get_db)):
        return posicao.preview(conn)

    @app.post("/api/posicao/cortar")
    def position_cut(u=Depends(session_only), conn=Depends(get_db)):
        return posicao.cut(conn, user_id=u["id"])

    @app.get("/api/escala/apagar-tudo")
    def wipe_preview(u=Depends(auth), conn=Depends(get_db)):
        return posicao.wipe_preview(conn)

    @app.get("/api/escala/inativos")
    def inactive_days(u=Depends(auth), conn=Depends(get_db)):
        return {"items": inativo.list_days(conn)}

    @app.post("/api/escala/inativos/tirar")
    def inactive_remove(b: dict = Body(default={}), u=Depends(session_only), conn=Depends(get_db)):
        b = b or {}
        dates = b.get("dates")
        if dates is not None and (not isinstance(dates, list) or not all(isinstance(x, str) for x in dates)):
            raise FGVError("dates: lista de datas.", code="campo_invalido")
        r = inativo.remove(conn, data_dir, dates, fill=bool(b.get("fill", True)))
        pernoite.sync_later(careers.core_path(data_dir), careers.career_path(data_dir, u["career"]), u["id"])
        return r

    @app.post("/api/escala/apagar-tudo")
    def wipe_all(b: dict = Body(default={}), u=Depends(session_only), conn=Depends(get_db)):
        if (b or {}).get("confirmar") != "APAGAR":
            raise FGVError("Para apagar todas as escalas, confirme digitando APAGAR.", code="confirmacao")
        try:
            backup.make_backup(data_dir, label="antes-de-apagar-escalas")      # foto de tudo antes, por garantia
        except Exception:
            log.exception("backup antes de apagar as escalas")
        r = posicao.wipe_all(conn, user_id=u["id"])
        r["engine"] = motor.wipe_engine(data_dir, u["career"])
        return r

    @app.post("/api/posicao/desfazer")
    def position_undo(u=Depends(session_only), conn=Depends(get_db)):
        return posicao.undo(conn, user_id=u["id"])

    # ----------------------------------------------------------------- pedido de troca de escala
    @app.get("/api/troca")
    def swaps(u=Depends(auth), conn=Depends(get_db)):
        return {"items": troca.listing(conn), "disclaimer": troca.DISCLAIMER}

    @app.get("/api/troca/opcoes/{leg_id}")
    def swap_options(leg_id: int, destino: str | None = None, u=Depends(auth), conn=Depends(get_db)):
        return troca.options(conn, data_dir, leg_id, dest=destino)

    @app.get("/api/troca/destinos/{leg_id}")
    def swap_destinations(leg_id: int, icao: str | None = None, u=Depends(auth), conn=Depends(get_db)):
        if icao:                                   # destino digitado: diz se dá e, se não der, exatamente por quê
            return troca.explain_destination(conn, data_dir, leg_id, icao[:8])
        return troca.destinations(conn, data_dir, leg_id)

    @app.post("/api/troca")
    def swap_request(b: SwapReq, u=Depends(session_only), conn=Depends(get_db)):
        return troca.request(conn, data_dir, b.leg_id, b.kind, ref=b.ref, reason=b.reason, user_id=u["id"], dest=b.destino)

    @app.post("/api/troca/{rid}/cancelar")
    def swap_cancel(rid: int, u=Depends(session_only), conn=Depends(get_db)):
        return troca.cancel(conn, rid, user_id=u["id"])

    @app.get("/api/week/{start}")
    def week(start: str, u=Depends(auth), conn=Depends(get_db)):
        _check_date(start)
        return journey.week(conn, start)

    @app.get("/api/roster/bounds")
    def roster_bounds(u=Depends(auth), conn=Depends(get_db)):
        return journey.bounds(conn)

    # ----------------------------------------------------------------- OFP do SimBrief
    @app.get("/api/settings")
    def get_settings(u=Depends(auth), conn=Depends(get_db)):
        keep = {"simbrief_userid", "simbrief_username", "ivao_vid", "landing_style", "acars_chime", "stay_transport", "stay_buffer_min", "stay_wakeup_min"}
        out = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM setting") if r["key"] in keep}
        return {k: out.get(k, "") for k in keep}

    @app.put("/api/settings")
    def put_settings(b: dict = Body(...), u=Depends(session_only), conn=Depends(get_db)):
        keep = {"simbrief_userid", "simbrief_username", "ivao_vid", "landing_style", "acars_chime", "stay_transport", "stay_buffer_min", "stay_wakeup_min"}
        bad = set(b) - keep
        if bad:
            raise FGVError(f"Ajuste desconhecido: {', '.join(sorted(bad))}", code="campo_invalido")
        if b.get("landing_style") not in (None, "", "folclorico", "just_culture"):
            raise FGVError("Estilo inválido.", code="campo_invalido")
        if b.get("acars_chime") not in (None, "", "on", "off"):
            raise FGVError("Som do ACARS: 'on' ou 'off'.", code="campo_invalido")
        for k in ("stay_transport", "stay_buffer_min", "stay_wakeup_min"):
            if k in b:
                stay.validate_setting(k, b[k])
        vid = str(b.get("ivao_vid") or "").strip()
        if vid and not vid.isdigit():
            raise FGVError("O VID da IVAO tem só números.", code="campo_invalido")
        with tx(conn):
            for k, v in b.items():
                conn.execute("INSERT INTO setting(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (k, str(v).strip()))
        glob = {k: v for k, v in b.items() if k in careers.GLOBAL_SYNC}
        if glob:                                       # vale para todas as carreiras: vai para o núcleo
            core = connect(core_path)
            try:
                with tx(core):
                    for k, v in glob.items():
                        core.execute("INSERT INTO setting(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v).strip()))
            finally:
                core.close()
        return get_settings(u, conn)

    @app.post("/api/flights/{fid}/ofp")
    def ofp_fetch(fid: int, b: dict = Body(default={}), u=Depends(session_only), conn=Depends(get_db)):
        cfg = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM setting")}
        uid = (b.get("userid") or cfg.get("simbrief_userid") or "").strip()
        unm = (b.get("username") or cfg.get("simbrief_username") or "").strip()
        raw = ofp_mod.fetch(uid or None, unm or None)
        out = ofp_mod.attach(conn, fid, raw, source="simbrief-api", user_id=u["id"])
        if b.get("remember") and (uid or unm):
            core = connect(core_path)                  # ajuste comum a todas as carreiras: o núcleo é a fonte
            try:
                with tx(core):
                    core.execute("INSERT INTO setting(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                 ("simbrief_userid" if uid else "simbrief_username", uid or unm))
            finally:
                core.close()
            careers.sync_settings_path(core_path, conn)
        return out

    @app.post("/api/flights/{fid}/ofp/file")
    async def ofp_file(fid: int, request: Request, u=Depends(session_only), conn=Depends(get_db)):
        body = await request.body()
        if len(body) > 20_000_000:
            raise FGVError("Arquivo grande demais.", code="arquivo_grande", status=413)
        import json as _json
        try:
            raw = _json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            raise FGVError("O arquivo não é um JSON válido. Baixe o OFP em JSON pela API do SimBrief.", code="ofp_invalido")
        return ofp_mod.attach(conn, fid, raw, source="arquivo", user_id=u["id"])

    @app.get("/api/flights/{fid}/ofp")
    def ofp_get(fid: int, u=Depends(auth), conn=Depends(get_db)):
        o = ofp_mod.latest(conn, fid)
        if not o:
            raise NotFound("Este voo ainda não tem OFP.")
        o["history"] = ofp_mod.history(conn, fid)
        return o

    @app.delete("/api/ofp/{oid}")
    def ofp_delete(oid: int, u=Depends(session_only), conn=Depends(get_db)):
        ofp_mod.delete(conn, oid, user_id=u["id"])
        return {"ok": True}

    @app.get("/api/flights/{fid}/loadsheet")
    def loadsheet_preview(fid: int, u=Depends(auth), conn=Depends(get_db)):
        out = ls_mod.compute(conn, fid)
        out["issued"] = ls_mod.latest(conn, fid)
        out["history"] = ls_mod.history(conn, fid)
        return out

    @app.post("/api/flights/{fid}/loadsheet")
    def loadsheet_issue(fid: int, b: dict = Body(default={}), u=Depends(session_only), conn=Depends(get_db)):
        return ls_mod.issue(conn, fid, b.get("overrides"), b.get("notes"), user_id=u["id"])

    @app.get("/api/loadsheet/{lid}.pdf")
    def loadsheet_pdf_(lid: int, u=Depends(auth), conn=Depends(get_db)):
        from fastapi.responses import Response
        try:
            from . import loadsheet_pdf
        except ImportError:
            raise FGVError("O PDF precisa do pacote reportlab: rode instalar.bat de novo.", code="sem_reportlab", status=501)
        ls = ls_mod.get(conn, lid)
        f = ls["data"]["flight"]
        nome = f"loadsheet_{f.get('flight_number') or f['id']}_{f['adep']}-{f['ades']}_ed{ls['edition']}.pdf"
        return Response(loadsheet_pdf.build(ls), media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{nome}"'})

    @app.get("/api/weather")
    def weather(icao: str, force: bool = False, u=Depends(auth), conn=Depends(get_db)):
        return rules.weather(conn, icao.split(","), force=force)

    @app.get("/api/crew")
    def crew(start: str | None = None, end: str | None = None, u=Depends(auth), conn=Depends(get_db)):
        if start:
            _check_date(start)
        if end:
            _check_date(end)
        return journey.crew_report(conn, start, end)

    @app.get("/api/rules")
    def rules_info(u=Depends(auth), conn=Depends(get_db)):
        """De onde saem as regras: módulos do AeroScale e a cópia do banco dele."""
        info = rules.source_info(conn)
        sched = one(conn.execute("SELECT airline FROM roster_schedule ORDER BY id DESC LIMIT 1"))
        info["profile"] = rules.crew_profile(sched["airline"]) if sched else None
        info["modules"] = ["catalog", "crew", "crew_profiles", "predict", "ops", "intl", "delays", "reserve", "career",
                           "logbook", "simbrief", "rpl_parser", "rpl_service", "weather", "airports", "world_tz"]
        return info

    @app.get("/api/flight-center/{fid}")
    def flight_center(fid: int, u=Depends(auth), conn=Depends(get_db)):
        equipe.run(conn)
        c = journey.flight_center(conn, fid)
        f = enroute._flight(conn, fid)
        c["crew"] = tripulacao.for_flight(conn, f)
        c["delay"] = carreira.delay_of(conn, fid)
        c["delay_codes"] = carreira.DELAY_CODES
        lc = one(conn.execute("SELECT * FROM line_check WHERE flight_id=? ORDER BY id DESC LIMIT 1", (fid,)))
        if lc:
            lc["items"] = json.loads(lc["items"] or "[]")
        c["line_check"] = lc
        return c

    @app.post("/api/flights/{fid}/delay")
    def flight_delay(fid: int, b: dict = Body(...), u=Depends(session_only), conn=Depends(get_db)):
        return carreira.set_delay_code(conn, fid, b.get("code"), b.get("remark"), user_id=u["id"])

    @app.get("/api/carreira")
    def carreira_summary(u=Depends(auth), conn=Depends(get_db)):
        equipe.run(conn)
        return carreira.summary(conn)

    # ----------------------------------------------------------------- Voos Externos (fora das OPS)
    @app.post("/api/externos/buscar")
    def ext_fetch(u=Depends(session_only), conn=Depends(get_core)):
        return externos.fetch_live(conn)

    @app.get("/api/externos/catalogo")
    def ext_catalog(family: str | None = None, type_icao: str | None = None, origin: str | None = None, dest: str | None = None,
                    country: str | None = None, min_h: float | None = None, max_h: float | None = None, airline: str | None = None,
                    u=Depends(auth), conn=Depends(get_core)):
        f = dict(family=family, type_icao=type_icao, origin=origin, dest=dest, country=country, min_h=min_h, max_h=max_h, airline=airline)
        return {"items": externos.catalog(conn, **f), "total": conn.execute("SELECT COUNT(*) FROM ext_seen").fetchone()[0],
                "families": sorted({v[1] for v in externos.TYPES.values()}), "attribution": externos.ATTRIBUTION, "min_km": externos.MIN_KM}

    @app.get("/api/externos/sortear")
    def ext_draw(family: str | None = None, origin: str | None = None, dest: str | None = None, country: str | None = None,
                 min_h: float | None = None, max_h: float | None = None, u=Depends(auth), conn=Depends(get_core)):
        r = externos.draw(conn, family=family, origin=origin, dest=dest, country=country, min_h=min_h, max_h=max_h)
        if not r:
            raise NotFound("Nenhum voo real no catálogo com esses filtros. Busque os voos no ar agora ou afrouxe os filtros.")
        return r

    @app.get("/api/externos")
    def ext_list(u=Depends(auth), conn=Depends(get_core)):
        return externos.list_flights(conn)

    @app.post("/api/externos")
    def ext_create(b: dict = Body(...), u=Depends(session_only), conn=Depends(get_core)):
        keys = {"callsign", "adep", "ades", "type_icao", "dep_utc", "stagger_min", "members", "notes"}
        bad = set(b) - keys
        if bad:
            raise FGVError(f"Campos desconhecidos: {', '.join(sorted(bad))}", code="campo_invalido")
        return externos.create(conn, user_id=u["id"], **b)

    @app.put("/api/externos/membro/{mid}/resultado")
    def ext_result(mid: int, b: dict = Body(...), u=Depends(session_only), conn=Depends(get_core)):
        keys = {"block", "landing_fpm", "landing_g", "remarks"}
        if set(b) - keys:
            raise FGVError("Campos desconhecidos.", code="campo_invalido")
        return externos.save_result(conn, mid, user_id=u["id"], **b)

    @app.get("/api/externos/{fid}")
    def ext_get(fid: int, u=Depends(auth), conn=Depends(get_core)):
        return externos.get(conn, fid)

    @app.get("/api/externos/{fid}/tempo")
    def ext_wx(fid: int, u=Depends(auth), conn=Depends(get_core)):
        return externos.weather(conn, externos.get(conn, fid))

    @app.get("/api/externos/{fid}/radar")
    def ext_live(fid: int, u=Depends(auth), conn=Depends(get_core)):
        return externos.live(conn, fid)

    @app.post("/api/externos/{fid}/cancelar")
    def ext_cancel(fid: int, u=Depends(session_only), conn=Depends(get_core)):
        return externos.cancel(conn, fid, user_id=u["id"])

    # ----------------------------------------------------------------- motor de escalas (AeroScale 2.2.4 integrado)
    @app.get("/api/motor")
    def motor_info(u=Depends(auth)):
        return {"status": motor.status(data_dir, u["career"]), "options": motor.options(data_dir, u["career"])}

    @app.post("/api/motor/gerar")
    def motor_generate(b: dict = Body(...), u=Depends(session_only), core=Depends(get_core)):
        return motor.generate(data_dir, u["career"], b, careers.career_path(data_dir, u["career"]), user_id=u["id"],
                              share_stats=estatisticas.share_enabled(core))

    # ----------------------------------------------------------------- continuações (beta)
    @app.get("/api/continuacoes")
    def continuations(q: str = "", u=Depends(auth), conn=Depends(get_db)):
        return continuacao.lookup(conn, data_dir, u["career"], q)

    @app.get("/api/continuacoes/dia/{date}")
    def continuations_day(date: str, u=Depends(auth), conn=Depends(get_db)):
        _check_date(date)
        return {str(l["id"]): continuacao.for_leg(conn, data_dir, l) for l in journey.legs_of_day(conn, date) if l["is_flight"]}

    @app.get("/api/continuacoes/regras")
    def continuation_rules(u=Depends(auth), conn=Depends(get_db)):
        return continuacao.rules_list(conn, u["career"])

    @app.post("/api/continuacoes/regras")
    def continuation_rule_add(b: dict = Body(...), u=Depends(session_only), conn=Depends(get_db)):
        if set(b) - {"from", "to", "weekdays", "note"}:
            raise FGVError("Campos desconhecidos.", code="campo_invalido")
        return continuacao.rule_add(conn, u["career"], b.get("from") or "", b.get("to") or "", b.get("weekdays") or [], b.get("note"))

    @app.delete("/api/continuacoes/regras/{rid}")
    def continuation_rule_del(rid: int, u=Depends(session_only), conn=Depends(get_db)):
        return continuacao.rule_delete(conn, rid)

    # ----------------------------------------------------------------- estatísticas
    @app.get("/api/estatisticas")
    def stats(inicio: str = None, fim: str = None, u=Depends(auth), conn=Depends(get_db)):
        return estatisticas.compute(conn, inicio, fim)

    @app.get("/api/estatisticas/envio")
    def stats_share(u=Depends(auth), core=Depends(get_core)):
        return {"enabled": estatisticas.share_enabled(core), "install": estatisticas.install_id(core)}

    @app.put("/api/estatisticas/envio")
    def stats_share_set(b: dict = Body(...), u=Depends(session_only), core=Depends(get_core)):
        return {"enabled": estatisticas.set_share(core, bool(b.get("enabled"))), "install": estatisticas.install_id(core)}

    @app.post("/api/motor/padrao")
    def motor_defaults(b: dict = Body(...), u=Depends(session_only)):
        return motor.save_defaults(data_dir, b)

    @app.get("/api/motor/tarefas/{jid}")
    def motor_job(jid: str, u=Depends(auth)):
        return motor.job(jid)

    @app.post("/api/motor/tarefas/{jid}/cancelar")
    def motor_cancel(jid: str, u=Depends(session_only)):
        return motor.cancel(jid)

    @app.post("/api/motor/rpl/atualizar")
    def motor_rpl(u=Depends(session_only)):
        return motor.update_rpl(data_dir, u["career"])

    @app.post("/api/motor/escala-real")
    def motor_real(b: dict = Body(...), u=Depends(session_only)):
        if set(b) - {"text", "month", "base"}:
            raise FGVError("Campos desconhecidos.", code="campo_invalido")
        return motor.import_real(data_dir, u["career"], b.get("text") or "", b.get("month") or "", careers.career_path(data_dir, u["career"]),
                                 user_id=u["id"], base=b.get("base"))

    @app.get("/api/notify")
    def notify_get(u=Depends(auth), conn=Depends(get_db), core=Depends(get_core)):
        return notify.config(core, conn)

    @app.put("/api/notify")
    def notify_put(b: dict = Body(...), u=Depends(session_only), conn=Depends(get_db), core=Depends(get_core)):
        notify.save(core, b)
        return notify.config(core, conn)

    @app.post("/api/notify/test")
    def notify_test(u=Depends(session_only), core=Depends(get_core)):
        return notify.test(core)

    @app.post("/api/flights/{fid}/dispatch/sign")
    def dispatch_sign(fid: int, u=Depends(session_only), conn=Depends(get_db)):
        return equipe.sign_release(conn, fid, user_id=u["id"])

    # ----------------------------------------------------------------- equipe e mensagens
    # ----------------------------------------------------------------- pernoites (hotel e transporte por aeroporto)
    @app.get("/api/airport-info")
    def airport_info_list(u=Depends(auth), conn=Depends(get_db)):
        return enroute.list_airport_info(conn)

    @app.put("/api/airport-info/{icao}")
    def airport_info_save(icao: str, b: dict = Body(...), u=Depends(session_only), conn=Depends(get_db)):
        bad = set(b) - {"hotel", "transport", "notes", "source"}
        if bad:
            raise FGVError(f"Campos desconhecidos: {', '.join(sorted(bad))}", code="campo_invalido")
        return enroute.save_airport_info(conn, icao, user_id=u["id"], **b)

    @app.get("/api/stay/{icao}/hotels")
    def stay_hotels(icao: str, radius: float = 15, u=Depends(auth), conn=Depends(get_db)):
        return stay.search_hotels(conn, icao, radius)

    @app.post("/api/stay/{icao}/hotel")
    def stay_choose(icao: str, b: dict = Body(...), u=Depends(session_only), conn=Depends(get_db)):
        return stay.choose_hotel(conn, icao, b, user_id=u["id"])

    @app.post("/api/stay/{icao}/route")
    def stay_route(icao: str, u=Depends(session_only), conn=Depends(get_db)):
        return stay.refresh_route(conn, icao)

    @app.get("/api/stay/flight/{fid}")
    def stay_flight(fid: int, u=Depends(auth), conn=Depends(get_db)):
        f = enroute._flight(conn, fid)
        if not f:
            raise NotFound("Voo não encontrado.")
        plan = enroute.plan_after(conn, f)
        tl = stay.timeline(conn, f, plan)
        info = enroute.airport_info(conn, f["ades"]) if tl else None
        evs = stay.events_for(conn, fid)
        if tl and evs:                                     # depois do calço real, valem os horários que estão agendados
            due = {e["key"].split(":")[0]: e["due_utc"] for e in evs}
            for k, col in (("stay_car", "car_utc"), ("stay_checkin", "checkin_utc"), ("stay_wake", "wake_utc"), ("stay_pickup", "pickup_utc")):
                if due.get(k):
                    tl[col] = due[k]
            if any(e["status"] != "PENDENTE" for e in evs) or tl.get("basis") != "pelo horário programado":
                tl["basis"] = "pela agenda do pernoite"
        return {"kind": plan["kind"], "timeline": tl, "hotel": info, "events": stay.events_for(conn, fid),
                "ride": stay.ride(fid, tl["transport"]) if tl else None, "attribution": stay.ATTRIBUTION,
                "tz": journey.place(conn, f["ades"])["tz"]}

    @app.get("/api/messages")
    def msgs(sector: str | None = None, flight_id: int | None = None, unread: bool = False, u=Depends(auth), conn=Depends(get_db)):
        equipe.run(conn)
        return {"messages": equipe.messages(conn, sector=sector, flight_id=flight_id, unread=unread), "counts": equipe.counts(conn),
                "sectors": equipe.SECTORS}

    @app.get("/api/messages/counts")
    def msg_counts(u=Depends(auth), conn=Depends(get_db)):
        return equipe.counts(conn)

    @app.post("/api/messages/{mid}/read")
    def msg_read(mid: int, u=Depends(session_only), conn=Depends(get_db)):
        return equipe.mark(conn, mid)

    @app.post("/api/messages/{mid}/ack")
    def msg_ack(mid: int, u=Depends(session_only), conn=Depends(get_db)):
        return equipe.mark(conn, mid, ack=True)

    @app.post("/api/messages/read-all")
    def msg_read_all(u=Depends(session_only), conn=Depends(get_db)):
        return {"updated": equipe.read_all(conn)}

    @app.get("/api/team")
    def team(u=Depends(auth), conn=Depends(get_db)):
        return {"staff": equipe.staff(conn), "sectors": equipe.SECTORS, "disclaimer": equipe.DISCLAIMER, "on_shift": equipe.shift_of(conn, now_iso())}

    @app.put("/api/team")
    def team_save(b: dict = Body(...), u=Depends(session_only), conn=Depends(get_db)):
        return {"staff": equipe.save_staff(conn, b.get("staff") or [], user_id=u["id"])}

    # ----------------------------------------------------------------- "já fiz esse voo"
    def _manual_kwargs(b: dict) -> dict:
        keys = ("aircraft_id", "out_utc", "off_utc", "on_utc", "in_utc", "landings", "fuel_used_kg", "landing_vs_fpm", "landing_g",
                "hard_landing_reported", "network", "remarks", "force_type", "ignore_schedule")
        bad = set(b) - set(keys)
        if bad:
            raise FGVError(f"Campos desconhecidos: {', '.join(sorted(bad))}", code="campo_invalido")
        return {k: b.get(k) for k in keys if k in b}

    @app.post("/api/flights/{fid}/manual")
    def manual_flight(fid: int, b: dict = Body(...), u=Depends(session_only), conn=Depends(get_db)):
        return manual.log_flight(conn, flight_id=fid, user_id=u["id"], **_manual_kwargs(b))

    @app.post("/api/roster/{leg_id}/manual")
    def manual_leg(leg_id: int, b: dict = Body(...), u=Depends(session_only), conn=Depends(get_db)):
        return manual.log_flight(conn, leg_id=leg_id, user_id=u["id"], **_manual_kwargs(b))

    @app.post("/api/logbook/{lid}/reverse")
    def manual_reverse(lid: int, b: ReasonReq, u=Depends(session_only), conn=Depends(get_db)):
        return manual.reverse(conn, lid, reason=b.reason or b.notes, user_id=u["id"])

    # ----------------------------------------------------------------- IVAO (acompanhamento e rede ao vivo)
    @app.get("/api/ivao/pulse")
    def ivao_pulse(airports: str = "", u=Depends(auth), conn=Depends(get_db)):
        sched = one(conn.execute("SELECT airline FROM roster_schedule ORDER BY id DESC LIMIT 1"))
        return ivao.pulse(careers.code_of(conn) if u.get("career") else ((sched or {}).get("airline") or "GLO"), [a.strip() for a in airports.split(",") if a.strip()][:8],
                          ivao.vid_of(conn))

    @app.post("/api/flights/{fid}/ivao/start")
    def ivao_start(fid: int, b: dict = Body(default={}), u=Depends(session_only), conn=Depends(get_db)):
        return ivao.start(conn, fid, confirm_restrictions=bool(b.get("confirm_restrictions")), user_id=u["id"])

    @app.post("/api/acars/sessions/{sid}/ivao/refresh")
    def ivao_refresh(sid: int, u=Depends(session_only), conn=Depends(get_db)):
        ivao.tick(conn)
        s = one(conn.execute("SELECT id, status, phase, snapshot, last_seen_at FROM acars_session WHERE id=?", (sid,)))
        if not s:
            raise NotFound("Sessão não encontrada.")
        import json as _j
        s["snapshot"] = _j.loads(s["snapshot"] or "{}")
        return s

    @app.post("/api/acars/sessions/{sid}/ivao/finish")
    def ivao_finish(sid: int, b: dict = Body(default={}), u=Depends(session_only), conn=Depends(get_db)):
        return ivao.finish(conn, sid, user_id=u["id"], force=bool(b.get("force")))

    # ----------------------------------------------------------------- Rede (pilotos da companhia na IVAO)
    @app.get("/api/network")
    def network_overview(u=Depends(auth), conn=Depends(get_db)):
        return network.overview(conn)

    @app.get("/api/network/pilots/{vid}")
    def network_pilot(vid: int, u=Depends(auth), conn=Depends(get_db)):
        return network.pilot_history(conn, vid)

    @app.post("/api/network/friends")
    def network_friend(b: dict = Body(...), u=Depends(session_only), conn=Depends(get_db)):
        return network.add_friend(conn, b.get("vid"), b.get("nickname"), b.get("note"), user_id=u["id"])

    @app.delete("/api/network/friends/{vid}")
    def network_unfriend(vid: int, u=Depends(session_only), conn=Depends(get_db)):
        network.remove_friend(conn, vid, user_id=u["id"])
        return {"ok": True}

    @app.get("/api/ops-feed")
    def ops_feed(u=Depends(auth), conn=Depends(get_db)):
        return feed.ops_feed(conn)

    # ----------------------------------------------------------------- salários (opcional)
    @app.get("/api/payroll/config")
    def payroll_config(u=Depends(auth), conn=Depends(get_db)):
        cur = payroll.contract_for(conn, now_iso()[:7])
        return {"enabled": payroll.enabled(conn), "contract": cur, "contracts": payroll.contracts(conn),
                "presets": payroll.presets(), "categories": payroll.CATEGORIES, "roles": payroll.ROLES,
                "regions": payroll.REGION_LABEL, "suggestion": payroll.suggestion(conn), "cct": payroll.CCT}

    @app.put("/api/payroll/enabled")
    def payroll_enable(b: dict = Body(...), u=Depends(session_only), conn=Depends(get_db)):
        return {"enabled": payroll.set_enabled(conn, bool(b.get("enabled")), user_id=u["id"])}

    @app.post("/api/payroll/contracts")
    def payroll_contract(b: dict = Body(...), u=Depends(session_only), conn=Depends(get_db)):
        payroll.require(conn)
        return payroll.save_contract(conn, str(b.get("valid_from") or ""), b.get("data") or {}, b.get("note"), user_id=u["id"])

    @app.get("/api/payroll/closings")
    def payroll_closings(u=Depends(auth), conn=Depends(get_db)):
        payroll.require(conn)
        return payroll.closings(conn)

    @app.post("/api/payroll/closings/{cid}/void")
    def payroll_void(cid: int, b: ReasonReq, u=Depends(session_only), conn=Depends(get_db)):
        payroll.require(conn)
        return payroll.void_closing(conn, cid, b.reason or b.notes, user_id=u["id"])

    @app.get("/api/payroll/year/{y}")
    def payroll_year(y: int, u=Depends(auth), conn=Depends(get_db)):
        payroll.require(conn)
        if not 2000 <= y <= 2100:
            raise FGVError("Ano inválido.", code="ano_invalido")
        return payroll.year(conn, y)

    @app.get("/api/payroll/{month}")
    def payroll_month(month: str, basis: str = "programado", u=Depends(auth), conn=Depends(get_db)):
        payroll.require(conn)
        return payroll.compute(conn, month, basis)

    @app.post("/api/payroll/{month}/close")
    def payroll_close(month: str, b: ReasonReq = Body(default=ReasonReq()), u=Depends(session_only), conn=Depends(get_db)):
        payroll.require(conn)
        return payroll.close_month(conn, month, b.notes or b.reason, user_id=u["id"])

    @app.get("/api/payroll/{month}/pdf")
    def payroll_pdf_(month: str, basis: str = "programado", closing: int | None = None, u=Depends(auth), conn=Depends(get_db)):
        from fastapi.responses import Response
        payroll.require(conn)
        try:
            from . import payroll_pdf
        except ImportError:
            raise FGVError("O PDF precisa do pacote reportlab: rode instalar.bat de novo.", code="sem_reportlab", status=501)
        if closing:
            cl_ = payroll.get_closing(conn, closing)
            data, info = cl_["data"], cl_
        else:
            data, info = payroll.compute(conn, month, basis), None
        if data.get("needs_contract"):
            raise FGVError("Cadastre o contrato antes.", code="sem_contrato", status=409)
        nome = f"contracheque_{data['month']}_{'fechado' if info else data['basis']}.pdf"
        return Response(payroll_pdf.build(data, info, me_name=u["display_name"]), media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{nome}"'})

    # ----------------------------------------------------------------- importação
    @app.post("/api/import/aeroscale")
    async def import_aeroscale(request: Request, dry_run: bool = False, u=Depends(session_only), conn=Depends(get_db)):
        """Corpo bruto = arquivo .zip (backup) ou .db do AeroScale."""
        size = 0
        with tempfile.NamedTemporaryFile(prefix="lineops_up_", delete=False) as tmp:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD:
                    os.unlink(tmp.name)
                    raise FGVError("Arquivo grande demais.", code="arquivo_grande", status=413)
                tmp.write(chunk)
        try:
            if size == 0:
                raise FGVError("Nenhum arquivo recebido.", code="arquivo_vazio")
            try:
                return aeroscale_import.import_backup(conn, tmp.name, dry_run=dry_run, user_id=u["id"], airline=u["career"])
            except aeroscale_import.ImportError_ as e:
                raise FGVError(str(e), code="importacao_invalida")
        finally:
            os.unlink(tmp.name)

    # ----------------------------------------------------------------- biblioteca técnica (PDF seus) e MEL
    @app.get("/api/biblioteca")
    def library(u=Depends(auth), conn=Depends(get_db)):
        return {"items": biblioteca.docs(conn, u["career"]), "kinds": list(biblioteca.KINDS),
                "note": "Os PDF ficam só no banco desta companhia, como texto por página. Nada vem com a LineOps: importe os seus."}

    @app.post("/api/biblioteca/importar")
    async def library_import(request: Request, name: str = "documento.pdf", kind: str | None = None, title: str | None = None,
                             u=Depends(session_only), conn=Depends(get_db)):
        """Corpo bruto = o PDF. ?name= nome do arquivo, ?kind= MEL/CDL/QRH/FCOM/OUTRO (vazio: detecta), ?title= título."""
        size = 0
        with tempfile.NamedTemporaryFile(prefix="lineops_pdf_", suffix=".pdf", delete=False) as tmp:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD:
                    os.unlink(tmp.name)
                    raise FGVError("Arquivo grande demais.", code="arquivo_grande", status=413)
                tmp.write(chunk)
        try:
            if size == 0:
                raise FGVError("Nenhum arquivo recebido.", code="arquivo_vazio")
            return biblioteca.import_pdf(conn, u["career"], tmp.name, name[:160], kind=kind or None, title=title or None, user_id=u["id"])
        finally:
            os.unlink(tmp.name)

    @app.delete("/api/biblioteca/{doc_id}")
    def library_delete(doc_id: int, u=Depends(session_only), conn=Depends(get_db)):
        return biblioteca.delete_doc(conn, u["career"], doc_id, user_id=u["id"])

    @app.get("/api/biblioteca/buscar")
    def library_search(q: str = "", doc: int | None = None, u=Depends(auth), conn=Depends(get_db)):
        return biblioteca.search(conn, u["career"], q, doc)

    @app.get("/api/biblioteca/{doc_id}/pagina/{pno}")
    def library_page(doc_id: int, pno: int, u=Depends(auth), conn=Depends(get_db)):
        return biblioteca.page(conn, u["career"], doc_id, pno)

    @app.get("/api/mel")
    def mel_list(q: str = "", doc: int | None = None, ata: str | None = None, u=Depends(auth), conn=Depends(get_db)):
        r = biblioteca.mel_search(conn, u["career"], q, doc, ata)
        r["docs"] = biblioteca.mel_docs(conn, u["career"])
        return r

    @app.get("/api/mel/{item_id}")
    def mel_detail(item_id: int, u=Depends(auth), conn=Depends(get_db)):
        return biblioteca.mel_item(conn, u["career"], item_id)

    @app.get("/api/import/batches")
    def batches(u=Depends(auth), conn=Depends(get_db)):
        out = rows(conn.execute("SELECT id, source_kind, source_name, source_version, source_created_at, imported_at, stats FROM import_batch ORDER BY id DESC LIMIT 50"))
        import json
        for b in out:
            b["stats"] = json.loads(b["stats"] or "{}")
        return out

    # ----------------------------------------------------------------- escala / voos
    @app.get("/api/roster")
    def roster(date_from: str | None = None, date_to: str | None = None, include_removed: bool = False, u=Depends(auth), conn=Depends(get_db)):
        return flights.list_roster(conn, date_from=date_from, date_to=date_to, include_removed=include_removed)

    @app.post("/api/roster/{leg_id}/book")
    def book(leg_id: int, b: BookReq, u=Depends(session_only), conn=Depends(get_db)):
        return flights.book_leg(conn, leg_id, aircraft_id=b.aircraft_id, user_id=u["id"], force_type=b.force_type, notes=b.notes)

    @app.get("/api/flights")
    def list_flights(state: str | None = None, u=Depends(auth), conn=Depends(get_db)):
        return flights.list_flights(conn, states=state.split(",") if state else None)

    @app.post("/api/flights")
    def unplanned(b: UnplannedReq, u=Depends(session_only), conn=Depends(get_db)):
        return flights.create_unplanned(conn, user_id=u["id"], **b.model_dump())

    @app.get("/api/flights/{fid}")
    def get_flight(fid: int, u=Depends(auth), conn=Depends(get_db)):
        return flights.get_flight(conn, fid)

    @app.put("/api/flights/{fid}/aircraft")
    def assign(fid: int, b: AssignReq, u=Depends(session_only), conn=Depends(get_db)):
        return flights.assign_aircraft(conn, fid, b.aircraft_id, user_id=u["id"], force_type=b.force_type)

    @app.post("/api/flights/{fid}/cancel")
    def cancel(fid: int, b: ReasonReq = Body(default=ReasonReq()), u=Depends(session_only), conn=Depends(get_db)):
        return flights.cancel(conn, fid, reason=b.reason, user_id=u["id"])

    @app.post("/api/flights/{fid}/reactivate")
    def reactivate(fid: int, u=Depends(session_only), conn=Depends(get_db)):
        return flights.reactivate(conn, fid, user_id=u["id"])

    # ----------------------------------------------------------------- frota
    @app.get("/api/aircraft")
    def aircraft_list(all: bool = False, u=Depends(auth), conn=Depends(get_db)):
        return fleet.list_aircraft(conn, include_inactive=all)

    @app.post("/api/aircraft")
    def aircraft_add(b: dict = Body(...), u=Depends(session_only), conn=Depends(get_db)):
        allowed = {"registration", "airline", "type_icao", "model_name", "variant", "msn", "limits", "config", "baseline_hours", "baseline_cycles", "baseline_note", "notes"}
        bad = set(b) - allowed
        if bad:
            raise FGVError(f"Campos desconhecidos: {', '.join(sorted(bad))}", code="campo_invalido")
        return fleet.add_aircraft(conn, user_id=u["id"], **b)

    @app.post("/api/aircraft/import-csv")
    async def aircraft_csv(request: Request, u=Depends(session_only), conn=Depends(get_db)):
        raw = await request.body()
        if len(raw) > 2_000_000:
            raise FGVError("CSV grande demais.", code="arquivo_grande", status=413)
        return fleet.import_csv(conn, raw.decode("utf-8-sig", errors="replace"), user_id=u["id"])

    @app.get("/api/aircraft/{aid}")
    def aircraft_get(aid: int, u=Depends(auth), conn=Depends(get_db)):
        a = fleet.get_aircraft(conn, aid)
        a["unavailability"] = rows(conn.execute("SELECT * FROM unavailability WHERE aircraft_id=? ORDER BY from_date DESC", (aid,)))
        a["discrepancies"] = rows(conn.execute("SELECT * FROM discrepancy WHERE aircraft_id=? ORDER BY id DESC", (aid,)))
        a["ledger"] = rows(conn.execute("SELECT * FROM usage_ledger WHERE aircraft_id=? ORDER BY id DESC LIMIT 100", (aid,)))
        a["history"] = rows(conn.execute("SELECT * FROM logbook_entry WHERE aircraft_id=? AND reversed_at IS NULL ORDER BY date DESC, id DESC LIMIT 100", (aid,)))
        return a

    @app.patch("/api/aircraft/{aid}")
    def aircraft_patch(aid: int, b: dict = Body(...), u=Depends(session_only), conn=Depends(get_db)):
        return fleet.update_aircraft(conn, aid, b, user_id=u["id"])

    @app.post("/api/aircraft/{aid}/unavailability")
    def unav(aid: int, b: dict = Body(...), u=Depends(session_only), conn=Depends(get_db)):
        return {"id": fleet.add_unavailability(conn, aid, b.get("from_date"), b.get("to_date"), b.get("reason"), user_id=u["id"])}

    @app.delete("/api/unavailability/{uid}")
    def unav_del(uid: int, u=Depends(session_only), conn=Depends(get_db)):
        if not conn.execute("DELETE FROM unavailability WHERE id=?", (uid,)).rowcount:
            raise NotFound("Indisponibilidade não encontrada.")
        audit(conn, u["id"], "unavailability.delete", "unavailability", uid)
        return {"ok": True}

    @app.post("/api/aircraft/{aid}/discrepancies")
    def disc(aid: int, b: dict = Body(...), u=Depends(session_only), conn=Depends(get_db)):
        return {"id": fleet.add_discrepancy(conn, aid, b.get("title"), description=b.get("description"), restriction=b.get("restriction", "NENHUMA"),
                                            restriction_text=b.get("restriction_text"), mel_ref=b.get("mel_ref"), deferred=bool(b.get("deferred")),
                                            due_date=b.get("due_date"), mel_item_id=b.get("mel_item_id"), airline=u["career"], user_id=u["id"])}

    @app.post("/api/discrepancies/{did}/close")
    def disc_close(did: int, u=Depends(session_only), conn=Depends(get_db)):
        fleet.close_discrepancy(conn, did, user_id=u["id"])
        return {"ok": True}

    @app.post("/api/aircraft/{aid}/adjust")
    def adjust(aid: int, b: dict = Body(...), u=Depends(session_only), conn=Depends(get_db)):
        return {"id": fleet.adjust_usage(conn, aid, block_min=int(b.get("block_min", 0)), air_min=int(b.get("air_min", 0)),
                                         cycles=int(b.get("cycles", 0)), note=b.get("note"), user_id=u["id"])}

    # ----------------------------------------------------------------- ACARS
    @app.get("/api/acars/flights")
    def acars_flights(u=Depends(auth), conn=Depends(get_db)):
        return acars.eligible_flights(conn)

    @app.post("/api/acars/sessions")
    def acars_start(b: SessionReq, u=Depends(auth), conn=Depends(get_db)):
        return acars.start_session(conn, b.flight_id, sim=b.sim, client_version=b.client_version, token_id=u["token_id"],
                                   confirm_restrictions=b.confirm_restrictions, user_id=u["id"])

    @app.post("/api/acars/sessions/{sid}/report")
    def acars_report(sid: int, b: ReportReq, u=Depends(auth), conn=Depends(get_db)):
        return acars.report(conn, sid, points=b.points, messages=b.messages, snapshot=b.snapshot)

    @app.post("/api/acars/sessions/{sid}/complete")
    def acars_complete(sid: int, b: dict = Body(...), u=Depends(auth), conn=Depends(get_db)):
        return acars.complete(conn, sid, b, user_id=u["id"])

    @app.post("/api/acars/sessions/{sid}/abort")
    def acars_abort(sid: int, b: ReasonReq = Body(default=ReasonReq()), u=Depends(auth), conn=Depends(get_db)):
        return acars.abort(conn, sid, reason=b.reason, user_id=u["id"])

    @app.get("/api/acars/live")
    def acars_live(u=Depends(auth), conn=Depends(get_db)):
        return acars.live_status(conn)

    @app.get("/api/acars/sessions/{sid}/track")
    def acars_track(sid: int, u=Depends(auth), conn=Depends(get_db)):
        return {"track": acars.track_for(conn, sid), "messages": acars.messages_for(conn, sid)}

    # ----------------------------------------------------------------- PIREP / diário
    @app.get("/api/pireps")
    def pirep_list(state: str | None = None, u=Depends(auth), conn=Depends(get_db)):
        return pireps.list_pireps(conn, state)

    @app.get("/api/pireps/{pid}")
    def pirep_get(pid: int, u=Depends(auth), conn=Depends(get_db)):
        p = pireps.get_pirep(conn, pid)
        p["track"] = acars.track_for(conn, p["session_id"])
        p["messages"] = acars.messages_for(conn, p["session_id"])
        return p

    @app.post("/api/pireps/{pid}/approve")
    def pirep_approve(pid: int, b: ApproveReq = Body(default=ApproveReq()), u=Depends(session_only), conn=Depends(get_db)):
        return pireps.approve(conn, pid, overrides=b.overrides, notes=b.notes, user_id=u["id"])

    @app.post("/api/pireps/{pid}/reject")
    def pirep_reject(pid: int, b: ReasonReq, u=Depends(session_only), conn=Depends(get_db)):
        return pireps.reject(conn, pid, notes=b.notes or b.reason, user_id=u["id"])

    @app.post("/api/pireps/{pid}/reverse")
    def pirep_reverse(pid: int, b: ReasonReq, u=Depends(session_only), conn=Depends(get_db)):
        return pireps.reverse(conn, pid, reason=b.reason or b.notes, user_id=u["id"])

    @app.get("/api/logbook")
    def logbook(year: int | None = None, u=Depends(auth), conn=Depends(get_db)):
        return {"entries": pireps.logbook(conn, year=year), "totals": pireps.logbook_totals(conn)}

    @app.get("/api/airports/{icao}")
    def airport(icao: str, u=Depends(auth), conn=Depends(get_db)):
        a = rules.airport(conn, icao)
        if not a:
            raise NotFound("Aeroporto não encontrado na base do AeroScale.")
        return {**a, "icao": icao.upper()}

    # ----------------------------------------------------------------- sobre, validade, log e primeira execução
    @app.get("/api/about")
    def about(u=Depends(auth), conn=Depends(get_db)):
        import platform
        import sys as _sys
        from .aeroscale import SOURCE_VERSION
        return {"version": __version__, "data_dir": str(data_dir), "db_size": sum(f.stat().st_size for f in careers.all_db_files(data_dir)),
                "log_path": str(log_path), "python": _sys.version.split()[0], "platform": platform.platform(),
                "desktop": os.environ.get("LINEOPS_DESKTOP") == "1", "aeroscale_modules": SOURCE_VERSION,
                "validity": validity.check(conn, data_dir / "backups"), "disclaimer": DISCLAIMER}

    @app.get("/api/validity")
    def validity_(u=Depends(auth), conn=Depends(get_db)):
        return [v for v in validity.check(conn, data_dir / "backups") if v["relevant"] and v["status"] != "ok"]

    @app.get("/api/logs")
    def log_tail(lines: int = 200, u=Depends(session_only)):
        return {"path": str(log_path), "lines": logs.tail(log_path, max(10, min(lines, 2000)))}

    @app.get("/api/logs/download")
    def log_download(u=Depends(session_only)):
        if not log_path.exists():
            raise NotFound("Ainda não há log.")
        return FileResponse(log_path, filename="lineops.log", media_type="text/plain")

    @app.get("/api/onboarding")
    def onboarding(u=Depends(auth), conn=Depends(get_db)):
        cfg = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM setting")}
        return {"done": cfg.get("onboarding_done") == "1",
                "steps": {"escala": bool(conn.execute("SELECT 1 FROM import_batch LIMIT 1").fetchone()),
                          "frota": conn.execute("SELECT COUNT(*) FROM aircraft WHERE active=1").fetchone()[0],
                          "ivao": bool(cfg.get("ivao_vid")), "simbrief": bool(cfg.get("simbrief_userid") or cfg.get("simbrief_username")),
                          "salarios": cfg.get("payroll_enabled") == "1"}}

    @app.post("/api/onboarding/done")
    def onboarding_done(u=Depends(session_only), conn=Depends(get_db)):
        conn.execute("INSERT INTO setting(key, value) VALUES ('onboarding_done','1') ON CONFLICT(key) DO UPDATE SET value='1'")
        audit(conn, u["id"], "onboarding.done", "setting", "onboarding_done")
        return {"ok": True}

    # ----------------------------------------------------------------- backup
    bdir = data_dir / "backups"

    @app.post("/api/backup")
    def do_backup(u=Depends(session_only)):
        p = backup.make_backup(data_dir, bdir)
        return {"name": p.name, "size": p.stat().st_size}

    @app.get("/api/backups")
    def backups(u=Depends(session_only)):
        return backup.list_backups(bdir)

    @app.get("/api/backups-info")
    def backups_info(u=Depends(session_only)):
        """O que entra no backup e se há restauração agendada (a tela de Ajustes)."""
        dbs, other = backup.data_files(data_dir)
        pr = backup.pending_restore(data_dir)
        return {"items": backup.list_backups(bdir), "folder": str(bdir), "pending_restore": pr and Path(pr.get("zip", "")).name,
                "last_restore": restored, "includes": sorted(f.relative_to(data_dir).as_posix() for f in dbs + other)[:60],
                "count": len(dbs) + len(other)}

    @app.post("/api/backups/enviar")
    async def backup_upload(request: Request, u=Depends(session_only)):
        """Um backup vindo de fora (de outro PC, do pen drive): conferido e guardado na pasta de backups."""
        bdir.mkdir(parents=True, exist_ok=True)
        tmp = bdir / f".upload-{uuid.uuid4().hex}.zip"
        size = 0
        try:
            with open(tmp, "wb") as fo:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 2 * 1024 * 1024 * 1024:
                        raise FGVError("Arquivo grande demais.", code="arquivo_grande", status=413)
                    fo.write(chunk)
            try:
                mf = backup.validate(tmp)
            except RuntimeError as e:
                raise FGVError(str(e), code="backup_invalido")
            stamp = (mf.get("created_at") or now_iso()).replace(":", "").replace("-", "").replace("T", "-").rstrip("Z")
            dest = bdir / f"{backup.PREFIX}{stamp}-enviado.zip"
            n = 2
            while dest.exists():
                dest = bdir / f"{backup.PREFIX}{stamp}-enviado-{n}.zip"
                n += 1
            tmp.replace(dest)
            return backup.info(dest)
        finally:
            tmp.unlink(missing_ok=True)

    @app.post("/api/backups/{name}/restaurar")
    def backup_restore(name: str, u=Depends(session_only)):
        f = bdir / name
        if "/" in name or "\\" in name or not name.startswith((backup.PREFIX, "gol" "ops-backup-", "flygolv-backup-")) or not f.is_file():
            raise NotFound("Backup não encontrado.")
        try:
            return backup.schedule_restore(data_dir, f)
        except RuntimeError as e:
            raise FGVError(str(e), code="backup_invalido")

    @app.delete("/api/backups/restaurar")
    def backup_restore_cancel(u=Depends(session_only)):
        backup.cancel_restore(data_dir)
        return {"scheduled": False}

    @app.get("/api/backups/{name}")
    def backup_dl(name: str, u=Depends(session_only)):
        f = (bdir / name)
        if "/" in name or "\\" in name or not name.startswith((backup.PREFIX, "gol" "ops-backup-", "flygolv-backup-")) or not f.is_file():
            raise NotFound("Backup não encontrado.")
        return FileResponse(f, filename=name, media_type="application/zip")

    # ----------------------------------------------------------------- front-end estático
    @app.middleware("http")
    async def no_stale_screens(request: Request, call_next):
        """A tela (index.html) sempre revalida. Script e estilo pedidos com ?v=<versão> ficam no cache para sempre: a
        versão nova muda o ?v= e o navegador busca de novo. Sem ?v=, revalida (ETag)."""
        resp = await call_next(request)
        path = request.url.path
        if not path.startswith("/api/"):
            versioned = request.query_params.get("v") == __version__ and path.endswith((".js", ".css"))
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable" if versioned else "no-cache"
        return resp

    from starlette.middleware.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware, minimum_size=1400)   # app.js de ~280 KB vai em ~80 KB

    if WEB.exists():
        app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
    return app
