"""1.0: tripulação do dia, pontualidade e código de atraso, marcos, cheque de rota, boletim e notificações no celular."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from lineops import carreira, enroute, equipe, flights, ivao, manual, notify, tripulacao
from lineops.errors import Conflict, FGVError
from tests.test_enroute import SBBR_SBFL, fake_wx, fly, leg, setup
from tests.test_ofp import FIX as OFP_FIX

TD = {"landing_vs_fpm": 150, "landing_g": 1.15}


def legs_trip():
    return [leg("a", "GLO1748", "SBBR", "SBFL", "2026-11-20T14:00:00Z", "2026-11-20T16:00:00Z"),
            leg("c", "GLO1750", "SBFL", "SBBR", "2026-11-21T11:00:00Z", "2026-11-21T13:00:00Z"),
            leg("d", "GLO1800", "SBBR", "SBGR", "2026-11-25T11:00:00Z", "2026-11-25T13:00:00Z")]


# ------------------------------------------------------------------ tripulação
def test_crew_pairing_follows_the_trip(conn, tmp_path):
    setup(conn, tmp_path, legs_trip())
    a, c = tripulacao.pairing(conn, "2026-11-20"), tripulacao.pairing(conn, "2026-11-21")
    assert len(a["cabin"]) == 4 and a["cabin"][0]["role"] == "Chefe de cabine"            # 737-800: 186 assentos → 4
    assert c["fo"] == a["fo"] and c["cabin"] == a["cabin"] and c["trip_day"] == 2           # pernoitou fora: mesma equipe
    assert tripulacao.pairing(conn, "2026-11-20") == a                                       # guardado
    assert tripulacao.pairing(conn, "2026-11-22") is None                                    # sem voo, sem tripulação
    assert tripulacao.cabin_count("B737") == 3 and tripulacao.cabin_count("B38M") == 4 and tripulacao.cabin_count("E195") == 3   # E195 da Azul: 118 assentos
    assert "FO " in tripulacao.crew_line(conn, "2026-11-20")


def test_briefing_cabin_chief_and_fo_comments(conn, tmp_path):
    aid, ids = setup(conn, tmp_path, legs_trip())
    conn.execute("INSERT INTO setting(key, value) VALUES ('ivao_vid', '123456')")
    equipe.run(conn, now="2026-11-10T12:00:00Z")
    f = flights.book_leg(conn, ids["a"], aircraft_id=aid)
    equipe.run(conn, now="2026-11-20T13:10:00Z")                                             # depois da apresentação
    crew = tripulacao.pairing(conn, "2026-11-20")
    b = next(m for m in equipe.messages(conn, limit=200) if m["key"] == "crew_brief:2026-11-20")
    assert b["sender"] == crew["fo"]["name"] and b["sector"] == "tripulacao" and "GLO1748 SBBR→SBFL" in b["body"] and crew["chief"]["name"] in b["body"]
    ivao.start(conn, f["id"])
    fly(conn, f["id"], SBBR_SBFL[:3])
    cab = next(m for m in equipe.messages(conn, flight_id=f["id"]) if m["key"].startswith("er_cab"))
    assert cab["sender"] == crew["chief"]["name"] and cab["sender_title"] == "Chefe de cabine"
    fly(conn, f["id"], SBBR_SBFL[3:])
    p = conn.execute("SELECT id FROM pirep WHERE flight_id=?", (f["id"],)).fetchone()
    from lineops import pireps
    pireps.approve(conn, p[0], overrides=TD)
    equipe.run(conn, now="2026-11-20T16:05:00Z", wx_fetch=fake_wx)
    msgs = {m["key"].split(":")[0]: m for m in equipe.messages(conn, flight_id=f["id"], limit=200)}
    assert msgs["crew_td"]["sender"] == crew["fo"]["name"] and "150 fpm" in msgs["crew_td"]["body"]
    assert "Amanhã estamos juntos" in msgs["crew_end"]["body"]
    assert "marco_ap" in msgs                                                                # primeiro pouso em SBFL


# ------------------------------------------------------------------ pontualidade, marcos
def test_delay_code_and_otp(conn, tmp_path):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    dep = (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    arr = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    aid, ids = setup(conn, tmp_path, [leg("a", "GLO1748", "SBBR", "SBFL", dep, arr)])
    equipe.run(conn)
    out = (now - timedelta(hours=4, minutes=30)).strftime("%Y-%m-%dT%H:%M")             # 30 min de atraso
    inn = (now - timedelta(hours=2, minutes=35)).strftime("%Y-%m-%dT%H:%M")
    r = manual.log_flight(conn, leg_id=ids["a"], aircraft_id=aid, out_utc=out, in_utc=inn, **TD)
    equipe.run(conn)
    d = carreira.delay_of(conn, r["id"])
    assert d["out_delay_min"] == 30 and d["in_delay_min"] == 25 and d["late"] and not d["code"]
    ask = next(m for m in equipe.messages(conn) if m["key"] == f"delay_ask:{r['id']}")
    assert ask["sector"] == "cco" and "30 min" in ask["subject"]
    with pytest.raises(FGVError):
        carreira.set_delay_code(conn, r["id"], "12345")
    carreira.set_delay_code(conn, r["id"], "93", "aeronave chegou atrasada")
    o = carreira.otp(conn)
    assert o["dep_otp"] == 0 and o["arr_otp"] == 0 and o["codes"][0]["code"] == "93" and o["pending_codes"] == 0
    ms = {m["key"] for m in carreira.summary(conn)["milestones"]}
    assert {"airport:SBFL", "reg:PR-GUZ", "hours:1", "landings:1"} <= ms


def test_off_schedule_is_not_a_delay(conn, tmp_path):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    dep = (now + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    arr = (now + timedelta(days=3, hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    aid, ids = setup(conn, tmp_path, [leg("a", "GLO1748", "SBBR", "SBFL", dep, arr)])
    r = manual.log_flight(conn, leg_id=ids["a"], aircraft_id=aid, out_utc=(now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M"),
                          in_utc=(now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"), ignore_schedule="real", **TD)
    equipe.run(conn)
    assert carreira.delay_of(conn, r["id"]) is None


# ------------------------------------------------------------------ cheque de rota e boletim
def test_line_check_scheduled_and_evaluated(conn, tmp_path):
    aid, ids = setup(conn, tmp_path, legs_trip())
    equipe.run(conn, now="2026-11-10T12:00:00Z")
    f0 = flights.book_leg(conn, ids["d"], aircraft_id=aid)
    carreira.check_clock(conn, "2026-11-12T12:00:00Z")
    assert carreira.check_status(conn, "2026-11-12T12:00:00Z")["scheduled"] is None        # sem nenhum voo no diário ainda
    f = flights.book_leg(conn, ids["a"], aircraft_id=aid)
    conn.execute("""INSERT INTO logbook_entry(flight_id, date, adep, ades, out_utc, in_utc, block_min, landings, source, created_at)
                    VALUES (NULL,'2026-11-01','SBGR','SBBR','2026-11-01T10:00:00Z','2026-11-01T11:40:00Z',100,1,'MANUAL','2026-11-01T12:00:00Z')""")
    carreira.check_clock(conn, "2026-11-18T12:00:00Z")
    st = carreira.check_status(conn, "2026-11-18T12:00:00Z")
    assert st["scheduled"]["flight_id"] == f["id"] and st["due"]
    lc = conn.execute("SELECT * FROM line_check").fetchone()
    # voo acompanhado, despacho assinado, toque suave, no horário
    ofp = json.loads((OFP_FIX / "ofp_simbrief.json").read_text(encoding="utf-8"))
    ofp["origin"]["icao_code"], ofp["destination"]["icao_code"] = "SBBR", "SBFL"
    from lineops import ofp as ofp_mod
    ofp_mod.attach(conn, f["id"], ofp)
    equipe.run(conn, now="2026-11-20T11:00:00Z")
    equipe.sign_release(conn, f["id"])
    conn.execute("INSERT INTO setting(key, value) VALUES ('ivao_vid', '123456')")
    conn.execute("UPDATE dispatch_release SET pic_signed_at='2026-11-20T13:00:00Z' WHERE flight_id=?", (f["id"],))
    ivao.start(conn, f["id"])
    fly(conn, f["id"], SBBR_SBFL)
    p = conn.execute("SELECT id FROM pirep WHERE flight_id=?", (f["id"],)).fetchone()[0]
    from lineops import pireps
    pireps.approve(conn, p, overrides=TD)
    equipe.run(conn, now="2026-11-20T16:10:00Z", wx_fetch=fake_wx)
    lc = conn.execute("SELECT * FROM line_check WHERE id=?", (lc["id"],)).fetchone()
    items = {i["item"]: i for i in json.loads(lc["items"])}
    assert lc["status"] in ("APROVADO", "APROVADO_OBS") and items["Despacho"]["ok"] and items["Toque"]["ok"] and items["Pontualidade"]["ok"]
    res = next(m for m in equipe.messages(conn, limit=300) if m["key"].startswith("lc_res"))
    assert res["sender"] == lc["checker"] and "APROVADO" in res["body"]
    st = carreira.check_status(conn, "2026-11-21T12:00:00Z")
    assert st["valid_until"] == "2027-11-30" and not st["due"]


def test_bulletin_monday_morning(conn, tmp_path):
    aid, ids = setup(conn, tmp_path, legs_trip())
    conn.execute("""INSERT INTO logbook_entry(flight_id, date, adep, ades, out_utc, in_utc, block_min, landings, landing_vs_fpm, landing_g, source, created_at)
                    VALUES (NULL,'2026-11-18','SBGR','SBBR','2026-11-18T10:00:00Z','2026-11-18T11:40:00Z',100,1,-180,1.2,'MANUAL','2026-11-18T12:00:00Z')""")
    carreira.bulletin_clock(conn, "2026-11-23T10:00:00Z")                                    # segunda, 07:00 em Brasília: cedo
    assert not any(m["key"].startswith("boletim") for m in equipe.messages(conn))
    carreira.bulletin_clock(conn, "2026-11-23T11:30:00Z")                                    # 08:30
    b = next(m for m in equipe.messages(conn) if m["key"].startswith("boletim"))
    assert "VOOS: 1 · 1h40 de bloco" in b["body"] and "180 fpm" in b["body"] and "ESTA SEMANA" in b["body"]
    carreira.bulletin_clock(conn, "2026-11-23T12:30:00Z")
    assert sum(m["key"].startswith("boletim") for m in equipe.messages(conn)) == 1


# ------------------------------------------------------------------ notificações
class FakeNet:
    def __init__(self):
        self.calls = []
        self.fail = False

    def __call__(self, url, data):
        body = json.loads(data.decode("utf-8")) if data else {}
        self.calls.append((url, body))
        if self.fail:
            raise OSError("sem rede")
        if url.endswith("/getMe"):
            return json.dumps({"ok": True, "result": {"username": "lineops_teste_bot"}}).encode()
        if url.endswith("/getUpdates"):
            return json.dumps({"ok": True, "result": self.updates if hasattr(self, "updates") else []}).encode()
        return json.dumps({"ok": True, "result": {}}).encode()


def test_telegram_pairing_routing_buttons_and_commands(conn, tmp_path):
    aid, ids = setup(conn, tmp_path, legs_trip())
    net = FakeNet()
    equipe.run(conn, now="2026-11-10T12:00:00Z")
    with pytest.raises(FGVError):
        notify.save(conn, {"tg_token": "curto"}, opener=net)
    cfg = notify.save(conn, {"tg_token": "123456789:" + "A" * 35, "public_url": "http://192.168.0.10:8787"}, opener=net)
    assert cfg["telegram"]["bot"] == "lineops_teste_bot" and cfg["telegram"]["pair_code"] and not cfg["telegram"]["paired"]
    code = cfg["telegram"]["pair_code"]
    assert notify.handle_update(conn, {"update_id": 1, "message": {"chat": {"id": 555}, "text": "/start 000000"}}, net) == "codigo_errado"
    assert notify.handle_update(conn, {"update_id": 2, "message": {"chat": {"id": 555}, "text": f"/start {code}"}}, net) == "pareado"
    assert notify.handle_update(conn, {"update_id": 3, "message": {"chat": {"id": 999}, "text": "/hoje"}}, net) == "ignorado"
    assert notify.config(conn)["channels"] == ["telegram"]
    # nada do passado é despejado; o que chega depois vai
    assert notify.enqueue(conn) == 0
    f = flights.book_leg(conn, ids["a"], aircraft_id=aid)
    equipe.run(conn, now="2026-11-19T20:00:00Z")                                             # véspera: lembrete (ação)
    assert notify.enqueue(conn) >= 1
    net.calls.clear()
    assert notify.flush(conn, now="2026-11-19T20:01:00Z", opener=net) >= 1
    sends = [b for u, b in net.calls if u.endswith("/sendMessage")]
    rem = next(b for b in sends if "Apresentação 20/11" in b["text"])
    assert rem["chat_id"] == "555" and rem["parse_mode"] == "HTML" and "http://192.168.0.10:8787/#/escala/2026-11-20" in rem["text"]
    assert "reply_markup" not in rem or all("url" not in btn for row in rem["reply_markup"]["inline_keyboard"] for btn in row)
    # botão de ciente na publicação da escala (mensagem de ação com ciente)
    pub = next(m for m in equipe.messages(conn, limit=200) if m["subject"].startswith("Escala publicada"))
    p = notify.telegram_payload(conn, notify._raw(conn), pub)
    assert p["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"ack:{pub['id']}"
    r = notify.handle_update(conn, {"update_id": 4, "callback_query": {"id": "q1", "data": f"ack:{pub['id']}",
                                                                       "message": {"message_id": 10, "chat": {"id": 555}}}}, net)
    assert r == "Ciente registrado ✔" and equipe.messages_by_id(conn, pub["id"])["acked_at"]
    assert any(u.endswith("/editMessageReplyMarkup") for u, _ in net.calls)
    # comandos
    net.calls.clear()
    notify.handle_update(conn, {"update_id": 5, "message": {"chat": {"id": 555}, "text": "/proximo"}}, net)
    assert "GLO1748" in net.calls[-1][1]["text"]
    # falha de rede: tenta de novo depois, sem perder
    equipe.post(conn, "x1", "despacho", "Teste falha", "corpo", level="alerta", now="2026-11-19T20:05:00Z")
    notify.enqueue(conn)
    net.fail = True
    notify.flush(conn, now="2026-11-19T20:05:10Z", opener=net)
    o = conn.execute("SELECT o.* FROM notify_outbox o JOIN ops_message m ON m.id=o.message_id WHERE m.key='x1'").fetchone()
    assert o["status"] == "PENDENTE" and o["tries"] == 1 and o["next_at"] > "2026-11-19T20:05:10Z"
    net.fail = False
    assert notify.flush(conn, now="2026-11-19T20:07:00Z", opener=net) >= 1
    assert conn.execute("SELECT status FROM notify_outbox o JOIN ops_message m ON m.id=o.message_id WHERE m.key='x1'").fetchone()[0] == "ENVIADO"


def test_ntfy_quiet_hours_and_alarm(conn, tmp_path):
    setup(conn, tmp_path, legs_trip())
    net = FakeNet()
    equipe.run(conn, now="2026-11-10T12:00:00Z")
    cfg = notify.save(conn, {"ntfy_new_topic": True, "quiet": "23:00-06:00", "public_url": "https://lineops.exemplo.com"})
    assert cfg["ntfy"]["topic"].startswith("lineops-") and cfg["channels"] == ["ntfy"]
    with pytest.raises(FGVError):
        notify.save(conn, {"quiet": "25:00-06:00"})
    equipe.post(conn, "n1", "despacho", "Despacho liberado", "corpo", level="acao", now="2026-11-20T04:00:00Z",
                links=[{"label": "Flight Center", "href": "#/voo/1"}])
    equipe.post(conn, "n2", "hotelaria", "Despertar", "Bom dia", level="acao", channel="alarme", now="2026-11-20T04:00:00Z")
    equipe.post(conn, "n3", "chefia", "Info qualquer", "corpo", level="info", now="2026-11-20T04:00:00Z")
    notify.enqueue(conn)
    assert notify.flush(conn, now="2026-11-20T04:00:30Z", opener=net) == 1                  # 01:00 local: só o despertar
    url, body = net.calls[-1]
    assert url == "https://ntfy.sh/" and body["priority"] == 5 and body["tags"] == ["alarm_clock"] and body["title"].startswith("Amanda")
    assert notify.flush(conn, now="2026-11-20T09:30:00Z", opener=net) == 1                  # 06:30: sai o resto
    b1 = net.calls[-1][1]
    assert b1["priority"] == 4 and b1["click"] == "https://lineops.exemplo.com/#/voo/1"
    st = {r["key"]: r["status"] for r in conn.execute("SELECT m.key, o.status FROM notify_outbox o JOIN ops_message m ON m.id=o.message_id")}
    assert st["n3"] == "IGNORADO"                                                           # info não vai no nível "importantes"
    assert notify.test(conn, opener=net) == {"ntfy": "ok"}
    notify.save(conn, {"level": "desligado"})
    with pytest.raises(Conflict):
        notify.test(conn, opener=net)


# ------------------------------------------------------------------ regressões da revisão da 1.0
def test_line_check_never_gets_stuck(conn, tmp_path):
    aid, ids = setup(conn, tmp_path, legs_trip())
    f = flights.book_leg(conn, ids["a"], aircraft_id=aid)
    conn.execute("INSERT INTO line_check(flight_id, checker, announced_at) VALUES (?, 'Cmte. X', '2026-11-12T12:00:00Z')", (f["id"],))
    conn.execute("UPDATE flight SET state='PIREP_RECEBIDO' WHERE id=?", (f["id"],))            # PIREP nunca revisado
    carreira.check_clock(conn, "2026-11-21T12:00:00Z")
    assert conn.execute("SELECT status FROM line_check").fetchone()[0] == "AGENDADO"         # ainda dentro das 48 h
    carreira.check_clock(conn, "2026-11-23T12:00:00Z")
    assert conn.execute("SELECT status FROM line_check").fetchone()[0] == "NAO_REALIZADO"


def test_one_failing_reaction_does_not_kill_the_others(conn, tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    dep = (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    aid, ids = setup(conn, tmp_path, [leg("a", "GLO1748", "SBBR", "SBFL", dep, (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"))])
    equipe.run(conn)
    monkeypatch.setattr(equipe, "touchdown", lambda *a: 1 / 0)
    r = manual.log_flight(conn, leg_id=ids["a"], aircraft_id=aid, out_utc=(now - timedelta(hours=4, minutes=30)).strftime("%Y-%m-%dT%H:%M"),
                          in_utc=(now - timedelta(hours=2, minutes=35)).strftime("%Y-%m-%dT%H:%M"), **TD)
    equipe.run(conn)
    assert carreira.delay_of(conn, r["id"])["out_delay_min"] == 30                         # a carreira rodou mesmo com o toque quebrado


def test_alarm_not_starved_in_quiet_hours(conn, tmp_path):
    setup(conn, tmp_path, legs_trip())
    equipe.run(conn, now="2026-11-10T12:00:00Z")
    notify.save(conn, {"ntfy_new_topic": True, "quiet": "23:00-06:00"})
    for i in range(40):
        equipe.post(conn, f"q{i}", "despacho", f"Alerta {i}", "x", level="alerta", now="2026-11-20T04:00:00Z")
    equipe.post(conn, "wake", "hotelaria", "Despertar", "Bom dia", level="acao", channel="alarme", now="2026-11-20T04:00:00Z")
    notify.enqueue(conn)
    net = FakeNet()
    assert notify.flush(conn, now="2026-11-20T04:00:30Z", opener=net) == 1 and net.calls[0][1]["priority"] == 5


def test_rescheduling_revives_wakeup(conn, tmp_path):
    from lineops import stay
    aid, ids = setup(conn, tmp_path, legs_trip())
    f = flights.book_leg(conn, ids["a"], aircraft_id=aid)
    fr = enroute._flight(conn, f["id"])
    plan = enroute.plan_after(conn, fr)
    conn.execute("INSERT INTO setting(key, value) VALUES ('stay_wakeup_min', '180'), ('stay_buffer_min', '90')")
    stay.schedule(conn, fr, plan, "2026-11-20T15:00:00Z", in_utc="2026-11-21T06:00:00Z")    # chegada estimada muito tarde: não cabe
    st = {e["key"].split(":")[0]: e["status"] for e in stay.events_for(conn, f["id"])}
    assert st.get("stay_wake") in (None, "CANCELADO")
    stay.schedule(conn, fr, plan, "2026-11-20T15:30:00Z", in_utc="2026-11-20T16:00:00Z")    # chegou no horário: volta
    st = {e["key"].split(":")[0]: e["status"] for e in stay.events_for(conn, f["id"])}
    assert st["stay_wake"] == "PENDENTE" and st["stay_pickup"] == "PENDENTE"


def test_telegram_robustness(conn, tmp_path):
    setup(conn, tmp_path, legs_trip())
    equipe.run(conn, now="2026-11-10T12:00:00Z")
    net = FakeNet()
    cfg = notify.save(conn, {"tg_token": "123456789:" + "A" * 35, "public_url": "https://lineops.local"}, opener=net)
    old = cfg["telegram"]["pair_code"]
    for i in range(5):                                                                       # tentativa de adivinhar o código
        notify.handle_update(conn, {"update_id": i, "message": {"chat": {"id": 777}, "text": "/start 111111"}}, net)
    assert notify.config(conn)["telegram"]["pair_code"] != old
    notify.handle_update(conn, {"update_id": 9, "message": {"chat": {"id": 555}, "text": f"/start {notify.config(conn)['telegram']['pair_code']}"}}, net)
    # texto enorme com HTML: corta antes de escapar, nunca quebra uma tag
    big = {"id": 1, "key": "k", "sector": "despacho", "sender": "A", "subject": "S", "body": "<b>&" * 3000, "level": "info",
           "ack_required": 1, "channel": "acars", "links": [{"label": "Abrir", "href": "#/voo/1"}], "flight_id": 1}
    p = notify.telegram_payload(conn, notify._raw(conn), big)
    import html as _h, re as _re
    visible = _h.unescape(_re.sub(r"<[^>]+>", "", p["text"]))                               # o limite do Telegram é sobre o texto visível
    assert len(visible) < 4096 and p["text"].count("<pre>") == p["text"].count("</pre>") == 1 and "&amp;" in p["text"]
    # link recusado pelo Telegram: o botão de ciente fica
    calls = []

    def picky(url, data):
        b = json.loads(data.decode())
        calls.append(b)
        if url.endswith("/sendMessage") and any("url" in x for row in b.get("reply_markup", {}).get("inline_keyboard", []) for x in row):
            return json.dumps({"ok": False, "description": "Bad Request: inline keyboard button URL is invalid"}).encode()
        return json.dumps({"ok": True, "result": {}}).encode()
    notify.send_telegram(conn, notify._raw(conn), {**big, "body": "curto"}, picky)
    last = calls[-1]
    assert last["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "ack:1" and "https://lineops.local/#/voo/1" in last["text"]
    # botão que falha: responde com alerta e mantém o teclado
    net.calls.clear()
    r = notify.handle_update(conn, {"update_id": 10, "callback_query": {"id": "q", "data": "ack:99999", "message": {"message_id": 3, "chat": {"id": 555}}}}, net)
    assert "não encontrada" in r.lower() and not any(u.endswith("/editMessageReplyMarkup") for u, _ in net.calls)
    assert [b for u, b in net.calls if u.endswith("/answerCallbackQuery")][0]["show_alert"] is True
