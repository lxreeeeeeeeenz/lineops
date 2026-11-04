"""Notificações no celular: a operação chega no seu bolso.

Por que Telegram e ntfy, e não "adicionar à tela inicial" (PWA) com notificação do navegador:
* notificação de navegador (Web Push) exige HTTPS com certificado válido, e o LineOps roda na sua rede local em http://;
  no iPhone ainda exige o app instalado na tela inicial. Sem um domínio e um túnel, não funciona.
* Telegram: um bot SEU (criado no @BotFather em 1 minuto). O LineOps fala com a API do Telegram por HTTPS, sem abrir porta
  nenhuma na sua rede. É de mão dupla: você dá ciente, assina o despacho e informa o código de atraso pelos botões, e
  pergunta /hoje, /proximo, /pernoite e /mensagens de onde estiver.
* ntfy: sem cadastro. Você instala o app ntfy, assina um tópico secreto que o LineOps gera, e pronto. Só de ida.
O atalho na tela inicial continua valendo para abrir o LineOps (o site tem ícone e nome de app), mas os avisos vêm daqui.

Regras: por padrão vão só as mensagens importantes (ação, alerta, ciente, ACARS em voo, pernoite, despertar, tripulação,
marcos, cheque e boletim). Há horário de silêncio (o despertar sempre toca). O que falha tenta de novo com intervalo
crescente. Nada é enviado antes de você configurar; ao configurar, o LineOps começa do agora (não despeja o histórico).
"""
from __future__ import annotations

import html
import ipaddress
import json
import logging
import os
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import __version__
from .db import connect, jloads, now_iso, one, rows, tx
from .errors import Conflict, FGVError

log = logging.getLogger("lineops.notify")
FMT = "%Y-%m-%dT%H:%M:%SZ"
TG_API = "https://api.telegram.org/bot{token}/{method}"
NTFY_DEFAULT = "https://ntfy.sh"
KEYS = ("notify_level", "notify_quiet", "public_url", "tg_token", "tg_chat", "tg_pair", "tg_pair_fails", "tg_bot", "tg_offset",
        "ntfy_server", "ntfy_topic", "ntfy_token", "notify_cursor")
LEVELS = ("importantes", "tudo", "desligado")
IMPORTANT_PREFIX = ("stay_", "crew_", "marco", "lc_", "boletim", "touchdown", "delay_ask", "dov_release", "report:", "reserve_act")
MAX_AGE_H = 12
MAX_TRIES = 10
SECTOR_TAG = {"despacho": "clipboard", "manutencao": "wrench", "mcc": "gear", "cabine": "coffee", "transporte": "oncoming_automobile",
              "hotelaria": "hotel", "tripulacao": "busts_in_silhouette", "chefia": "airplane", "escala": "calendar", "tripulantes": "telephone_receiver",
              "carregamento": "package", "folha": "moneybag", "cco": "satellite"}
SECTOR_EMOJI = {"despacho": "📋", "manutencao": "🔧", "mcc": "⚙️", "cabine": "☕", "transporte": "🚐", "hotelaria": "🏨", "tripulacao": "🧑‍✈️",
                "chefia": "✈️", "escala": "📅", "tripulantes": "📞", "carregamento": "📦", "folha": "💰", "cco": "🛰️"}


def _p(s):
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc) if s else None


# ------------------------------------------------------------------ configuração
def _raw(conn) -> dict:
    return {r["key"]: r["value"] for r in conn.execute(f"SELECT key, value FROM setting WHERE key IN ({','.join('?' * len(KEYS))})", KEYS)}


def _set(conn, k, v):
    if v is None:
        conn.execute("DELETE FROM setting WHERE key=?", (k,))
    else:
        conn.execute("INSERT INTO setting(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))


def channels(cfg: dict) -> list[str]:
    if (cfg.get("notify_level") or "importantes") == "desligado":
        return []
    out = []
    if cfg.get("tg_token") and cfg.get("tg_chat"):
        out.append("telegram")
    if cfg.get("ntfy_topic"):
        out.append("ntfy")
    return out


def config(conn, recent_conn=None) -> dict:
    """Para a tela: nada de token inteiro. conn = núcleo (configuração); recent_conn = carreira (o que foi enviado)."""
    c = _raw(conn)
    rc = recent_conn or conn
    tok = c.get("tg_token") or ""
    return {"level": c.get("notify_level") or "importantes", "quiet": c.get("notify_quiet") or "", "public_url": c.get("public_url") or "",
            "telegram": {"configured": bool(tok), "token_hint": (tok[:6] + "…" + tok[-4:]) if tok else "", "bot": c.get("tg_bot") or "",
                         "paired": bool(c.get("tg_chat")), "pair_code": c.get("tg_pair") if tok and not c.get("tg_chat") else None},
            "ntfy": {"server": c.get("ntfy_server") or NTFY_DEFAULT, "topic": c.get("ntfy_topic") or "", "has_token": bool(c.get("ntfy_token"))},
            "channels": channels(c), "levels": list(LEVELS),
            "recent": rows(rc.execute("""SELECT o.channel, o.status, o.error, o.sent_at, m.subject FROM notify_outbox o JOIN ops_message m ON m.id=o.message_id
                                           ORDER BY o.created_at DESC, o.message_id DESC LIMIT 8"""))}


def _quiet_ok(v: str) -> bool:
    try:
        a, b = v.split("-")
        for x in (a, b):
            h, m = x.split(":")
            assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59 and len(h) == 2 and len(m) == 2
        return True
    except Exception:
        return False


def save(conn, b: dict, *, opener=None) -> dict:
    """Salva a configuração. Um token novo do Telegram é validado (getMe) e gera o código de pareamento."""
    allowed = {"level", "quiet", "public_url", "tg_token", "tg_unpair", "ntfy_server", "ntfy_topic", "ntfy_token", "ntfy_new_topic", "ntfy_off", "tg_off"}
    bad = set(b) - allowed
    if bad:
        raise FGVError(f"Campos desconhecidos: {', '.join(sorted(bad))}", code="campo_invalido")
    if b.get("level") not in (None, *LEVELS):
        raise FGVError("Nível inválido.", code="campo_invalido")
    q = (b.get("quiet") or "").strip() if b.get("quiet") is not None else None
    if q and not _quiet_ok(q):
        raise FGVError("Horário de silêncio no formato 23:00-06:00 (ou vazio).", code="campo_invalido")
    url = (b.get("public_url") or "").strip().rstrip("/") if b.get("public_url") is not None else None
    if url and not url.startswith(("http://", "https://")):
        raise FGVError("Endereço do LineOps começa com http:// ou https://.", code="campo_invalido")
    bot = None
    tok = (b.get("tg_token") or "").strip() if b.get("tg_token") else None
    if tok:
        if ":" not in tok or len(tok) < 30:
            raise FGVError("Token do Telegram inválido (o @BotFather manda algo como 123456:ABC...).", code="campo_invalido")
        me = tg_api(tok, "getMe", {}, opener)                         # fora da transação: chamada de rede
        bot = (me.get("result") or {}).get("username")
    srv = (b.get("ntfy_server") or "").strip().rstrip("/") if b.get("ntfy_server") is not None else None
    if srv and not srv.startswith("https://") and not srv.startswith("http://"):
        raise FGVError("Servidor do ntfy começa com https://.", code="campo_invalido")
    topic = (b.get("ntfy_topic") or "").strip() if b.get("ntfy_topic") is not None else None
    if topic and (len(topic) < 12 or not all(ch.isalnum() or ch in "-_" for ch in topic)):
        raise FGVError("Tópico do ntfy: 12+ letras, números, - ou _ (ele funciona como senha).", code="campo_invalido")
    with tx(conn):
        before = channels(_raw(conn))
        if b.get("level") is not None:
            _set(conn, "notify_level", b["level"])
        if q is not None:
            _set(conn, "notify_quiet", q or None)
        if url is not None:
            _set(conn, "public_url", url or None)
        if tok:
            _set(conn, "tg_token", tok)
            _set(conn, "tg_bot", bot)
            _set(conn, "tg_chat", None)
            _set(conn, "tg_offset", None)
            _set(conn, "tg_pair", f"{secrets.randbelow(900000) + 100000}")
        if b.get("tg_unpair"):
            _set(conn, "tg_chat", None)
            _set(conn, "tg_pair", f"{secrets.randbelow(900000) + 100000}")
        if b.get("tg_off"):
            for k in ("tg_token", "tg_chat", "tg_bot", "tg_pair", "tg_offset"):
                _set(conn, k, None)
        if srv is not None:
            _set(conn, "ntfy_server", srv or None)
        if b.get("ntfy_new_topic"):
            topic = "lineops-" + secrets.token_hex(8)
        if topic is not None:
            _set(conn, "ntfy_topic", topic or None)
        if b.get("ntfy_token") is not None:
            _set(conn, "ntfy_token", (b.get("ntfy_token") or "").strip() or None)
        if b.get("ntfy_off"):
            for k in ("ntfy_topic", "ntfy_token"):
                _set(conn, k, None)
        if not before and channels(_raw(conn)):                         # primeiro canal ligado: começa do agora
            _set(conn, "notify_cursor", conn.execute("SELECT COALESCE(MAX(id),0) FROM ops_message").fetchone()[0])
    return config(conn)


# ------------------------------------------------------------------ o que vai e quando
def important(m: dict) -> bool:
    return (m["level"] in ("acao", "alerta") or m.get("channel") in ("acars", "alarme") or bool(m["ack_required"])
            or str(m["key"]).startswith(IMPORTANT_PREFIX))


def in_quiet(conn, cfg: dict, now: str) -> bool:
    q = cfg.get("notify_quiet") or ""
    if not q or not _quiet_ok(q):
        return False
    from .aeroscale.airports import get_tz
    from .equipe import _base_tz
    _b, tzn = _base_tz(conn, now)
    t = _p(now).astimezone(get_tz(tzn) or timezone.utc).strftime("%H:%M")
    a, b = q.split("-")
    return (a <= t < b) if a < b else (t >= a or t < b)


def enqueue(conn, now: str | None = None, cfg: dict | None = None) -> int:
    """Põe na fila as mensagens novas da operação para cada canal configurado.
    conn = banco da carreira (mensagens e fila); cfg = configuração do núcleo (sem cfg, lê do próprio conn)."""
    now = now or now_iso()
    cfg = cfg if cfg is not None else _raw(conn)
    chans = channels(cfg)
    if not chans:
        return 0
    r = conn.execute("SELECT value FROM setting WHERE key='notify_cursor'").fetchone()
    if r is None:                                                       # carreira nova para o celular: começa do agora
        with tx(conn):
            _set(conn, "notify_cursor", conn.execute("SELECT COALESCE(MAX(id),0) FROM ops_message").fetchone()[0])
        return 0
    cur = int(r[0] or 0)
    n = 0
    lvl = cfg.get("notify_level") or "importantes"
    with tx(conn):
        for m in rows(conn.execute("SELECT * FROM ops_message WHERE id>? ORDER BY id LIMIT 200", (cur,))):
            ok = lvl == "tudo" or important(m)
            for ch in chans:
                conn.execute("INSERT OR IGNORE INTO notify_outbox(message_id, channel, status, created_at) VALUES (?,?,?,?)",
                             (m["id"], ch, "PENDENTE" if ok else "IGNORADO", now))
                n += ok
            cur = m["id"]
        _set(conn, "notify_cursor", cur)
        old = (_p(now) - timedelta(days=30)).strftime(FMT)                # a fila não cresce para sempre
        conn.execute("DELETE FROM notify_outbox WHERE created_at<? AND status IN ('ENVIADO','IGNORADO','ERRO')", (old,))
    return n


def flush(conn, now: str | None = None, opener=None, cfg: dict | None = None, career: str | None = None) -> int:
    """Envia o que está pendente (rede fora de transação). Devolve quantas foram enviadas."""
    now = now or now_iso()
    cfg = cfg if cfg is not None else _raw(conn)
    chans = channels(cfg)
    quiet = in_quiet(conn, cfg, now)
    sent = 0
    pend = rows(conn.execute(f"""SELECT o.*, m.key AS mkey FROM notify_outbox o JOIN ops_message m ON m.id=o.message_id
                                 WHERE o.status='PENDENTE' AND (o.next_at IS NULL OR o.next_at<=?) {"AND m.channel='alarme'" if quiet else ""}
                                 ORDER BY (m.channel='alarme') DESC, o.message_id LIMIT 30""", (now,)))
    for o in pend:
        m = one(conn.execute("SELECT * FROM ops_message WHERE id=?", (o["message_id"],)))
        if o["channel"] not in chans:
            _mark(conn, o, "IGNORADO", "canal desligado")
            continue
        if (_p(now) - _p(m["ts"][:19] + "Z" if not m["ts"].endswith("Z") else m["ts"])).total_seconds() > MAX_AGE_H * 3600:
            _mark(conn, o, "IGNORADO", "mensagem antiga")
            continue
        if quiet and m.get("channel") != "alarme":
            continue                                               # espera o fim do horário de silêncio
        try:
            if o["channel"] == "telegram":
                send_telegram(conn, cfg, m, opener, career)
            else:
                send_ntfy(cfg, m, opener, career)
            _mark(conn, o, "ENVIADO", None, now)
            sent += 1
        except Exception as e:
            tries = o["tries"] + 1
            nxt = (_p(now) + timedelta(seconds=min(3600, 30 * 2 ** (tries - 1)))).strftime(FMT)
            with tx(conn):
                conn.execute("UPDATE notify_outbox SET tries=?, error=?, next_at=?, status=? WHERE message_id=? AND channel=?",
                             (tries, str(e)[:300], nxt, "ERRO" if tries >= MAX_TRIES else "PENDENTE", o["message_id"], o["channel"]))
            log.warning("notificação %s via %s falhou: %s", o["message_id"], o["channel"], e)
    return sent


def _mark(conn, o, status, err, now=None):
    with tx(conn):
        conn.execute("UPDATE notify_outbox SET status=?, error=?, sent_at=? WHERE message_id=? AND channel=?",
                     (status, err, now, o["message_id"], o["channel"]))


# ------------------------------------------------------------------ formatos
def _url(cfg, href: str | None) -> str | None:
    base = (cfg.get("public_url") or "").rstrip("/")
    if not href:
        return None
    if href.startswith(("http://", "https://")):
        return href
    return f"{base}/{href.lstrip('/')}" if base else None


def _button_ok(url: str) -> bool:
    """O Telegram recusa botão de link para localhost/IP; nesses casos o link vai no texto."""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        if host in ("localhost",) or "." not in host:
            return False
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return True


def _tag(career):
    from .careers import PROFILES
    return f"[{PROFILES[career]['name']}] " if career and career in PROFILES and cfg_multi() else ""


_MULTI = {"v": False}


def cfg_multi() -> bool:
    return _MULTI["v"]


def telegram_payload(conn, cfg: dict, m: dict, career: str | None = None) -> dict:
    links = jloads(m.get("links"), []) if isinstance(m.get("links"), str) else (m.get("links") or [])
    emoji = "⏰" if m.get("channel") == "alarme" else "📟" if m.get("channel") == "acars" else SECTOR_EMOJI.get(m["sector"], "•")
    from .equipe import SECTORS
    raw = m["body"] if len(m["body"]) <= 3300 else m["body"][:3300] + "…"
    body = html.escape(raw)
    if m.get("channel") == "acars":
        body = f"<pre>{body}</pre>"
    text = f"{emoji} {html.escape(_tag(career))}<b>{html.escape(m['sender'])}</b> · {html.escape(SECTORS.get(m['sector'], m['sector']))}\n<b>{html.escape(m['subject'])}</b>\n\n{body}"
    cp = f"{career}:" if career else ""
    kb, extra = [], []
    if m["ack_required"] and not m.get("acked_at"):
        kb.append([{"text": "✔ Dar ciente", "callback_data": f"ack:{cp}{m['id']}"}])
    if str(m["key"]).startswith("dov_release:") and "liberado" in m["subject"] and m.get("flight_id"):
        kb.append([{"text": "✍ Assinar o despacho", "callback_data": f"sign:{cp}{m['flight_id']}"}])
    if str(m["key"]).startswith("delay_ask:") and m.get("flight_id"):
        from .carreira import QUICK_CODES
        btn = [{"text": c, "callback_data": f"dly:{cp}{m['flight_id']}:{c}"} for c in QUICK_CODES]
        kb += [btn[:4], btn[4:]]
        extra.append("Códigos: 93 rotação · 63 tripulação · 89 aeroporto/ATC · 81 fluxo · 15 embarque · 41 pane · 99 outros")
    for l in links:
        u = _url(cfg, l.get("href"))
        if not u:
            continue
        if _button_ok(u):
            kb.append([{"text": l.get("label") or "Abrir", "url": u}])
        else:
            extra.append(f"{l.get('label') or 'Abrir'}: {u}")
    if extra:
        text += "\n\n" + html.escape("\n".join(extra))
    out = {"chat_id": cfg.get("tg_chat"), "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if kb:
        out["reply_markup"] = {"inline_keyboard": kb}
    return out


def ntfy_payload(cfg: dict, m: dict, career: str | None = None) -> dict:
    links = jloads(m.get("links"), []) if isinstance(m.get("links"), str) else (m.get("links") or [])
    pr = 5 if m.get("channel") == "alarme" else 4 if (m["level"] in ("acao", "alerta") or m.get("channel") == "acars") else 3
    tags = ["alarm_clock"] if m.get("channel") == "alarme" else [SECTOR_TAG.get(m["sector"], "airplane")]
    if m["level"] == "alerta":
        tags.append("warning")
    out = {"topic": cfg.get("ntfy_topic"), "title": f"{_tag(career)}{m['sender']} · {m['subject']}"[:200], "message": m["body"][:3800],
           "priority": pr, "tags": tags}
    acts = []
    for l in links[:3]:
        u = _url(cfg, l.get("href"))
        if u:
            acts.append({"action": "view", "label": (l.get("label") or "Abrir")[:40], "url": u})
    if acts:
        out["actions"] = acts
        out["click"] = acts[0]["url"]
    return out


# ------------------------------------------------------------------ envio
def _http(url, payload: dict, headers=None, opener=None, timeout=20) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if opener:
        raw = opener(url, data)
        return json.loads(raw.decode("utf-8")) if raw else {}
    if os.environ.get("LINEOPS_OFFLINE") == "1":
        raise FGVError("Sem internet neste modo.", code="offline", status=503)
    h = {"Content-Type": "application/json; charset=utf-8", "User-Agent": f"LineOps/{__version__}"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise FGVError(f"HTTP {e.code}: {detail}", code="envio_falhou", status=502)
    return json.loads(raw.decode("utf-8")) if raw else {}


def tg_api(token: str, method: str, payload: dict, opener=None, timeout=20) -> dict:
    try:
        r = _http(TG_API.format(token=token, method=method), payload, opener=opener, timeout=timeout)
    except FGVError as e:
        if "401" in e.message or "404" in e.message:
            raise FGVError("O Telegram não reconheceu o token. Confira o que o @BotFather mandou.", code="telegram_token", status=400)
        raise
    except Exception as e:
        raise FGVError(f"Telegram indisponível agora ({e}).", code="telegram_indisponivel", status=503)
    if not r.get("ok", False):
        raise FGVError(f"Telegram: {r.get('description') or 'erro'}", code="telegram_erro", status=502)
    return r


def send_telegram(conn, cfg, m, opener=None, career=None):
    p = telegram_payload(conn, cfg, m, career)
    try:
        tg_api(cfg["tg_token"], "sendMessage", p, opener)
    except FGVError as e:
        if "reply_markup" in p and ("button" in e.message.lower() or "url" in e.message.lower()):
            kb = p["reply_markup"]["inline_keyboard"]
            urls = [b for row in kb for b in row if "url" in b]
            keep = [row for row in ([b for b in row if "url" not in b] for row in kb) if row]
            p["text"] += "\n\n" + html.escape("\n".join(f"{b['text']}: {b['url']}" for b in urls))
            if keep:
                p["reply_markup"] = {"inline_keyboard": keep}
            else:
                p.pop("reply_markup")
            tg_api(cfg["tg_token"], "sendMessage", p, opener)
        else:
            raise


def send_ntfy(cfg, m, opener=None, career=None):
    hdr = {"Authorization": f"Bearer {cfg['ntfy_token']}"} if cfg.get("ntfy_token") else {}
    _http((cfg.get("ntfy_server") or NTFY_DEFAULT).rstrip("/") + "/", ntfy_payload(cfg, m, career), hdr, opener)


def test(conn, opener=None) -> dict:
    """Mensagem de teste em todos os canais configurados (direto, sem passar pela fila)."""
    cfg = _raw(conn)
    chans = channels(cfg)
    if not chans:
        raise Conflict("Nenhum canal configurado (ou as notificações estão desligadas).", code="sem_canal")
    m = {"id": 0, "key": "test", "sector": "tripulantes", "sender": "LineOps", "subject": "Teste de notificação",
         "body": "Se você está lendo isto no celular, a operação já sabe onde te achar. Bons voos!", "level": "acao", "ack_required": 0,
         "channel": None, "links": [{"label": "Abrir o LineOps", "href": "#/painel"}], "flight_id": None}
    out = {}
    for ch in chans:
        try:
            send_telegram(conn, cfg, m, opener) if ch == "telegram" else send_ntfy(cfg, m, opener)
            out[ch] = "ok"
        except Exception as e:
            out[ch] = str(e)
    return out


# ------------------------------------------------------------------ Telegram de volta (comandos e botões)
def _fmt_local(conn, iso, icao):
    from .enroute import _lt
    return _lt(conn, iso, icao, True)


def text_today(conn, now=None) -> str:
    from . import journey, tripulacao
    now = now or now_iso()
    d = journey.today_overview(conn, now)
    out = [f"<b>Hoje · {html.escape(d['label'])}</b>"]
    fl = [l for l in d["legs"] if l.get("is_flight")]
    if fl:
        if d.get("duty") and d["duty"].get("report_utc"):
            out.append(f"Apresentação: {_fmt_local(conn, d['duty']['report_utc'], fl[0]['adep'])} em {fl[0]['adep']}")
        for l in fl:
            out.append(f"• {l['flight_number'] or '—'} {l['adep']}→{l['ades']} {l['dep_utc'][11:16]}–{l['arr_utc'][11:16]}Z"
                       + (f" · {l['flight_state'].lower()}" if l.get("flight_state") else ""))
        c = tripulacao.pairing(conn, d["date"], create=False)
        if c:
            out.append(f"Tripulação: FO {html.escape(c['fo']['name'])}, chefe de cabine {html.escape(c['chief']['name'])}")
        if d.get("overnight"):
            out.append(f"Pernoite: {html.escape(d['overnight']['at'].get('city') or d['overnight']['at']['icao'])}")
    else:
        out.append({"FOLGA": "Folga.", "RESERVA": "Reserva.", "LIVRE": "Sem voo hoje."}.get(d["kind"], "Sem voo hoje."))
    if d.get("next_duty"):
        nd = d["next_duty"]
        out.append(f"Próxima apresentação: {html.escape(nd['label'])}, {nd['flight_number'] or '—'} em {nd['at']['icao']}")
    return "\n".join(out)


def text_next(conn, now=None) -> str:
    from . import journey
    d = journey.today_overview(conn, now)
    n = d.get("next_flight")
    if not n:
        return "Nenhum voo à frente na escala publicada."
    s = f"<b>Próximo voo</b>\n{n['flight_number'] or '—'} {n['adep']}→{n['ades']}\nPartida {n['dep_utc'][11:16]}Z ({_fmt_local(conn, n['dep_utc'], n['adep'])})"
    if n.get("ac_registration"):
        s += f"\nAeronave {n['ac_registration']}"
    if n.get("starts_in_min") is not None and n["starts_in_min"] > 0:
        s += f"\nEm {n['starts_in_min'] // 60}h{n['starts_in_min'] % 60:02d}"
    return s


def text_stay(conn, now=None) -> str:
    now = now or now_iso()
    ev = rows(conn.execute("""SELECT e.*, f.ades FROM stay_event e JOIN flight f ON f.id=e.flight_id WHERE e.status='PENDENTE' AND e.due_utc>?
                              ORDER BY e.due_utc LIMIT 6""", (now,)))
    if not ev:
        return "Nenhum pernoite agendado agora."
    lab = {"stay_car": "Carro no desembarque", "stay_checkin": "Check-in", "stay_wake": "Despertar", "stay_pickup": "Carro na portaria"}
    return "<b>Pernoite</b>\n" + "\n".join(f"• {lab.get(e['key'].split(':')[0], e['subject'])}: {_fmt_local(conn, e['due_utc'], e['ades'])}" for e in ev)


def text_unread(conn) -> str:
    ms = rows(conn.execute("SELECT sender, subject FROM ops_message WHERE read_at IS NULL ORDER BY id DESC LIMIT 6"))
    if not ms:
        return "Nenhuma mensagem não lida. 👌"
    return "<b>Não lidas</b>\n" + "\n".join(f"• {html.escape(m['sender'])}: {html.escape(m['subject'])}" for m in ms)


HELP = ("Comandos do LineOps:\n/hoje: a sua jornada de hoje\n/proximo: o próximo voo\n/pernoite: carro, check-in e despertar\n"
        "/mensagens: o que você ainda não leu\n\nOs botões das mensagens dão ciente, assinam o despacho e informam o código de atraso.")


def handle_update(conn, upd: dict, opener=None, open_career=None) -> str | None:
    """Uma atualização do Telegram. Só o chat pareado manda; o pareamento é /start CÓDIGO.
    conn = núcleo (configuração). open_career(code) abre o banco da carreira (botões e comandos agem nele)."""
    cfg = _raw(conn)
    tok = cfg.get("tg_token")
    if not tok:
        return None
    send = lambda chat, txt: tg_api(tok, "sendMessage", {"chat_id": chat, "text": txt, "parse_mode": "HTML"}, opener)
    if upd.get("callback_query"):
        cq = upd["callback_query"]
        chat = str(((cq.get("message") or {}).get("chat") or {}).get("id"))
        if chat != str(cfg.get("tg_chat")):
            return "ignorado"
        data = cq.get("data") or ""
        reply, ok = "Feito.", False
        cc = None
        try:
            kind, *args = data.split(":")
            if args and not args[0].isdigit():                           # ack:GLO:123 → carreira GLO
                code = args.pop(0)
                if open_career:
                    cc = open_career(code)
            elif open_career:                                            # botão de antes das carreiras: a carreira ativa
                from .careers import active
                cc = open_career(active(conn))
            db = cc or conn
            if kind == "ack":
                from . import equipe
                equipe.mark(db, int(args[0]), ack=True)
                reply = "Ciente registrado ✔"
            elif kind == "sign":
                from . import equipe
                equipe.sign_release(db, int(args[0]))
                reply = "Despacho assinado ✍"
            elif kind == "dly":
                from . import carreira
                carreira.set_delay_code(db, int(args[0]), args[1])
                reply = f"Código {args[1]} registrado"
            else:
                reply = "Botão desconhecido."
            ok = kind in ("ack", "sign", "dly")
        except Exception as e:
            reply = (getattr(e, "message", None) or "Não deu certo agora. Tente pelo LineOps.")[:180]
        finally:
            if cc is not None:
                cc.close()
        tg_api(tok, "answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": reply, "show_alert": not ok}, opener)
        msg = cq.get("message") or {}
        if ok and msg.get("message_id"):
            try:
                tg_api(tok, "editMessageReplyMarkup", {"chat_id": chat, "message_id": msg["message_id"], "reply_markup": {"inline_keyboard": []}}, opener)
            except FGVError:
                pass
        return reply
    m = upd.get("message") or {}
    chat = str((m.get("chat") or {}).get("id") or "")
    txt = (m.get("text") or "").strip()
    if not chat or not txt:
        return None
    if txt.startswith("/start"):
        code = txt.split(maxsplit=1)[1].strip() if " " in txt else ""
        if cfg.get("tg_chat"):
            if chat == str(cfg["tg_chat"]):
                send(chat, "Já estamos conectados, comandante. " + HELP)
                return "ja_pareado"
            return "ignorado"                                          # outro chat tentando: silêncio
        if code and code == cfg.get("tg_pair"):
            with tx(conn):
                _set(conn, "tg_chat", chat)
                _set(conn, "tg_pair", None)
                if not conn.execute("SELECT 1 FROM setting WHERE key='notify_cursor'").fetchone():
                    _set(conn, "notify_cursor", conn.execute("SELECT COALESCE(MAX(id),0) FROM ops_message").fetchone()[0])
            send(chat, "✈️ <b>LineOps conectado.</b>\nA partir de agora a operação te avisa por aqui: escala, despacho, ACARS em voo, pernoite e despertar.\n\n" + HELP)
            return "pareado"
        fails = int(cfg.get("tg_pair_fails") or 0) + 1
        with tx(conn):
            if fails >= 5:                                             # tentativa de adivinhar: código novo, aparece no LineOps
                _set(conn, "tg_pair", f"{secrets.randbelow(900000) + 100000}")
                fails = 0
            _set(conn, "tg_pair_fails", fails)
        send(chat, "Para conectar, mande /start seguido do código de 6 dígitos que aparece no LineOps (Ajustes › Celular).")
        return "codigo_errado"
    if chat != str(cfg.get("tg_chat")):
        return "ignorado"
    cmd = txt.split()[0].split("@")[0].lower()
    db = None
    if open_career:
        from .careers import active
        db = open_career(active(conn))
    d = db or conn
    try:
        fn = {"/hoje": lambda: text_today(d), "/proximo": lambda: text_next(d), "/próximo": lambda: text_next(d),
              "/pernoite": lambda: text_stay(d), "/mensagens": lambda: text_unread(d)}.get(cmd)
        send(chat, fn() if fn else HELP)
    finally:
        if db is not None:
            db.close()
    return cmd


def poll_telegram(conn, opener=None, timeout=25, open_career=None) -> int:
    cfg = _raw(conn)
    if not cfg.get("tg_token"):
        return 0
    off = int(cfg.get("tg_offset") or 0)
    r = tg_api(cfg["tg_token"], "getUpdates", {"offset": off, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
               opener, timeout=timeout + 10)
    n = 0
    for upd in r.get("result") or []:
        try:
            handle_update(conn, upd, opener, open_career)
        except Exception:
            log.exception("atualização do Telegram")
        with tx(conn):
            _set(conn, "tg_offset", int(upd["update_id"]) + 1)
        n += 1
    return n


# ------------------------------------------------------------------ laços em segundo plano
def _career_opener(data_dir):
    from .careers import career_path

    def op(code):
        p = career_path(data_dir, code)
        return connect(p) if p.exists() else None
    return op


class Worker(threading.Thread):
    """Fila → celular, a cada 5 s, para cada carreira (a configuração vem do núcleo)."""

    def __init__(self, data_dir):
        super().__init__(name="lineops-notify", daemon=True)
        from pathlib import Path
        self.data_dir, self.stop_ev = Path(data_dir), threading.Event()

    def run(self):
        from . import careers
        while not self.stop_ev.wait(5):
            try:
                core = connect(careers.core_path(self.data_dir))
                try:
                    cfg = _raw(core)
                finally:
                    core.close()
                if not channels(cfg):
                    continue
                files = careers.career_files(self.data_dir)
                _MULTI["v"] = len(files) > 1
                for path in files:
                    c = connect(path)
                    try:
                        enqueue(c, cfg=cfg)
                        flush(c, cfg=cfg, career=path.stem)
                    finally:
                        c.close()
            except Exception:
                log.exception("fila de notificações")

    def stop(self):
        self.stop_ev.set()


class TelegramPoller(threading.Thread):
    """Ouve o Telegram (long polling: uma conexão aberta esperando; nada de porta aberta na sua rede)."""

    def __init__(self, data_dir):
        super().__init__(name="lineops-telegram", daemon=True)
        from pathlib import Path
        self.data_dir, self.stop_ev = Path(data_dir), threading.Event()

    def run(self):
        from . import careers
        op = _career_opener(self.data_dir)
        while not self.stop_ev.is_set():
            wait = 10
            try:
                c = connect(careers.core_path(self.data_dir))
                try:
                    if _raw(c).get("tg_token"):
                        poll_telegram(c, open_career=op)
                        wait = 0.5
                finally:
                    c.close()
            except Exception as e:
                log.warning("Telegram: %s", e)
                wait = 30
            self.stop_ev.wait(wait)

    def stop(self):
        self.stop_ev.set()
