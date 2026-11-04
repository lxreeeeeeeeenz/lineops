"""Toque: classificação do pouso e reação da manutenção, com os critérios do fabricante do avião que você voou.

Fontes (pesquisadas, não inventadas):
BOEING 737 (NG e MAX)
* Boeing projeta o trem para 10 ft/s (600 fpm) no peso máximo de pouso e 6 ft/s (360 fpm) no peso máximo de decolagem
  (o mesmo do 14 CFR 25.473 para qualquer avião de transporte).
* AMM 05-51 do 737NG: inspeção de pouso duro quando a aceleração vertical de pico passa de 2,2 G (1,2 G incremental).
* A inspeção tem Fase 1 (visual: trem principal, fuse pins, amortecedores, longarina) e Fase 2 só se a Fase 1 achar dano.
* Para a Boeing, o julgamento da tripulação é o critério mais confiável: pouso que o piloto sentiu duro é inspecionado.
AIRBUS A320 (A319, A320, A321, neo)
* Relatório LOAD<15> (pouso duro) do sistema de monitoração, que obriga a inspeção: aceleração normal acima de 2,6 G no
  toque, ou razão de descida no radioaltímetro acima de 9 ft/s (540 fpm); acima do peso máximo de pouso, 1,7 G ou 6 ft/s
  (360 fpm). Assim citado no relatório do AAIB sobre o A319 G-EZAW e no da BFU sobre o A321 D-ASTP (via Aerossurance).
ATR 72
* O critério de inspeção do AMM da ATR não aparece nos relatórios públicos (ATSB, AAIB, BEA). O que está publicado é o
  limite de energia do trem: equivalente a 600 fpm no peso máximo de pouso, com reserva até 720 fpm (ATSB AO-2017-111).
  O LineOps usa 600 fpm; a G aparece, mas não decide sozinha.
EMBRAER E-JETS (E1 e E2)
* Nenhuma fonte pública traz o número do AMM. Um relatório da PKBWL (Polônia) sobre um E170 diz que 2,07 G passou do
  permitido em 0,07 G, o que sugere 2,0 G, mas o número não está escrito. O LineOps usa só o limite de projeto do trem
  (14 CFR 25.473): 600 fpm, e mostra a G.
BOEING 777 e 787
* Sem o número de G do manual de manutenção desses tipos nas fontes do LineOps: vale o limite de projeto do trem
  (14 CFR 25.473): 10 ft/s (600 fpm) no peso máximo de pouso, 6 ft/s (360 fpm) acima dele. A G de pico aparece, mas
  não decide sozinha.
"Severo" e as faixas manteiga/suave/normal/firme são do LineOps (servem para conversa, não para manutenção) e a tela diz isso.
A Boeing e a Airbus alertam que punir pouso firme leva a flare longo e saída de pista (just culture). Por isso o LineOps tem
dois estilos de chefia: "folclórico" (a bronca, pedida pelo usuário) e "just culture".
Pouso manteiga não é elogio automático: flare longo come pista.
"""
from __future__ import annotations

TIERS = {
    "manteiga": "Manteiga", "suave": "Suave", "normal": "Normal", "firme": "Firme", "duro": "Pouso duro", "severo": "Pouso duro severo",
}
AIRBUS_NB = ("A318", "A319", "A320", "A321", "A19N", "A20N", "A21N")
BOEING_WB = ("B762", "B763", "B764", "B772", "B773", "B77L", "B77W", "B788", "B789", "B78X")
AIRBUS_WB = ("A332", "A333", "A338", "A339", "A359", "A35K")
ATR = ("AT43", "AT45", "AT46", "AT72", "AT75", "AT76")
EJETS = ("E170", "E175", "E190", "E195", "E290", "E295")

CRITERIA = {
    "B737": {"maker": "Boeing", "model": "737", "hard_vs": 600, "hard_g": 2.2, "ow_vs": 360, "ow_g": None,
             "severe_vs": 900, "severe_g": 2.6, "doc": "AMM 05-51",
             "insp": "inspeção de pouso duro (AMM 05-51, Fase 1): trem principal, fuse pins, amortecedores e fixações",
             "source": "Boeing: limite de projeto 600 fpm (10 ft/s) no peso máximo de pouso e 360 fpm (6 ft/s) no de decolagem; "
                       "AMM 05-51 do 737NG: inspeção acima de 2,2 G de pico"},
    "A320": {"maker": "Airbus", "model": "A320", "hard_vs": 540, "hard_g": 2.6, "ow_vs": 360, "ow_g": 1.7,
             "severe_vs": 900, "severe_g": 3.0, "doc": "relatório LOAD<15>",
             "insp": "inspeção de pouso duro pedida pelo relatório LOAD<15> (trem principal, fixações e estrutura ao redor)",
             "source": "Airbus A320: relatório LOAD<15> acima de 2,6 G ou 9 ft/s (540 fpm) no toque; acima do peso máximo de pouso, "
                       "1,7 G ou 6 ft/s (360 fpm) (AAIB G-EZAW; BFU D-ASTP)"},
    "ATR": {"maker": "ATR", "model": "ATR 72", "hard_vs": 600, "hard_g": None, "ow_vs": 360, "ow_g": None,
            "severe_vs": 900, "severe_g": None, "doc": "limite de energia do trem",
            "insp": "inspeção de pouso duro (trem principal, fixações e estrutura ao redor)",
            "source": "ATR 72: trem projetado para o equivalente a 600 fpm no peso máximo de pouso, com reserva até 720 fpm "
                      "(ATSB AO-2017-111); o número de G do AMM não está publicado"},
    "EJET": {"maker": "Embraer", "model": "E-Jet", "hard_vs": 600, "hard_g": None, "ow_vs": 360, "ow_g": None,
             "severe_vs": 900, "severe_g": None, "doc": "limite de projeto do trem",
             "insp": "inspeção de pouso duro (trem principal, fixações e estrutura ao redor)",
             "source": "E-Jets: limite de projeto do trem (14 CFR 25.473), 600 fpm; o número de G do AMM não está publicado "
                       "(um relatório da PKBWL sobre um E170 sugere 2,0 G, sem escrever o número)"},
    "WIDE": {"maker": "Boeing", "model": "widebody", "hard_vs": 600, "hard_g": None, "ow_vs": 360, "ow_g": None,
             "severe_vs": 900, "severe_g": None, "doc": "limite de projeto do trem",
             "insp": "inspeção de pouso duro (trem principal, fixações e estrutura ao redor)",
             "source": "Limite de projeto do trem (14 CFR 25.473): 600 fpm (10 ft/s) no peso máximo de pouso e 360 fpm (6 ft/s) acima dele; "
                       "sem o número de G do manual deste tipo nas fontes do LineOps"},
}


def criteria(type_icao: str | None) -> dict:
    t = (type_icao or "").upper()
    if t in AIRBUS_NB:
        return CRITERIA["A320"]
    if t in BOEING_WB:
        return CRITERIA["WIDE"]
    if t in AIRBUS_WB:
        return {**CRITERIA["WIDE"], "maker": "Airbus", "model": "A330"}
    if t in ATR:
        return CRITERIA["ATR"]
    if t in EJETS:
        return CRITERIA["EJET"]
    return CRITERIA["B737"]


SOURCE = CRITERIA["B737"]["source"]


def evaluate(vs_fpm, g, *, landing_weight_kg=None, mlw_kg=None, reported=False, type_icao=None) -> dict:
    """Classifica o toque pelos critérios do fabricante do tipo. vs em fpm (negativo ou positivo, tanto faz), g de pico."""
    c = criteria(type_icao)
    v = abs(int(vs_fpm)) if vs_fpm is not None else None
    g = float(g) if g is not None else None
    reasons, inspection = [], None
    over = landing_weight_kg is not None and mlw_kg is not None and landing_weight_kg > mlw_kg
    if v is None or g is None:
        return {"tier": None, "label": "Sem dados do toque", "inspection": None, "reasons": ["razão ou G não informados"], "overweight": over}
    hg = c["hard_g"] if c["hard_g"] is not None else 99
    sg = c["severe_g"] if c["severe_g"] is not None else 99
    if v >= c["severe_vs"] or g >= sg:
        tier = "severo"
    elif v >= c["hard_vs"] or g > hg:
        tier = "duro"
    elif v >= 360 or g >= 1.6:
        tier = "firme"
    elif v >= 240 or g >= 1.4:
        tier = "normal"
    elif v >= 100:
        tier = "suave"
    else:
        tier = "manteiga"
    if v >= c["hard_vs"]:
        reasons.append(f"{v} fpm: no limite do {c['doc']} ({c['hard_vs']} fpm no peso máximo de pouso) ou acima")
    if c["hard_g"] is not None and g > c["hard_g"]:
        reasons.append(f"{g:.2f} G: acima dos {str(c['hard_g']).replace('.', ',')} G do {c['doc']}")
    if over:
        reasons.append(f"pouso com {landing_weight_kg:,.0f} kg, acima do MLW de {mlw_kg:,.0f} kg".replace(",", "."))
        if v >= c["ow_vs"] or (c["ow_g"] is not None and g > c["ow_g"]):
            reasons.append(f"{v} fpm / {g:.2f} G com sobrepeso: acima de {c['ow_vs']} fpm"
                           + (f" ou {str(c['ow_g']).replace('.', ',')} G" if c["ow_g"] else "") + " acima do peso máximo de pouso")
            tier = "duro" if tier in ("manteiga", "suave", "normal", "firme") else tier
    if reported:
        reasons.append("pouso duro reportado pela tripulação (o critério mais confiável, para o fabricante)")
    if tier in ("duro", "severo") or reported or over:
        inspection = "fase2" if tier == "severo" else "fase1"
    return {"tier": tier, "label": TIERS[tier], "inspection": inspection, "reasons": reasons, "overweight": over,
            "vs": v, "g": g, "reported": bool(reported), "source": c["source"], "maker": c["maker"], "model": c["model"],
            "insp_text": c["insp"]}


def _pick(options: list[str], seed: int) -> str:
    return options[seed % len(options)]


def texts(ev: dict, ctx: dict, style: str = "folclorico") -> dict:
    """Mensagens da Chefia de Manutenção e, se for o caso, a convocação da Chefia de Equipamento.
    ctx: reg, ades, fn, seed, lw, mlw."""
    v, g, t = ev["vs"], ev["g"], ev["tier"]
    reg, ades, fn = ctx.get("reg") or "a aeronave", ctx.get("ades") or "destino", ctx.get("fn") or "voo"
    gs = f"{g:.2f}".replace(".", ",")
    s = ctx.get("seed", 0)
    insp = ("A " + reg + " está BLOQUEADA para a " + (ev.get("insp_text") or "inspeção de pouso duro")
            + ". Volta para a escala quando a Manutenção fechar o item na Frota.") if ev["inspection"] else ""
    model = ev.get("model") if ev.get("model") not in (None, "widebody") else "widebody"
    maker = ev.get("maker") or "Boeing"
    if ev["inspection"] == "fase2":
        insp += " Pelos números, a Fase 2 é provável: desmontar amortecedor para medir e procurar trinca."
    out = {"mnt": None, "chefia": None, "level": "info"}
    folk = style != "just_culture"
    if ev["reported"] and t in ("manteiga", "suave", "normal", "firme"):
        out["mnt"] = (f"Comandante, obrigado por reportar o pouso do {fn} em {ades} ({v} fpm, {gs} G). Se você sentiu duro, "
                      f"a regra é inspecionar, e é assim que tem que ser. {insp} Isso é profissionalismo, não punição.")
        out["level"] = "alerta"
        return out
    if t == "manteiga":
        out["mnt"] = _pick([
            f"Pouso manteiga da {reg} em {ades}: {v} fpm, {gs} G. Bonito pra foto. Agora, se você flutuou meia pista pra conseguir isso, "
            "eu prefiro um toque firme na zona de toque. Flare longo come pista e é assim que se sai dela.",
            f"{v} fpm em {ades}. Os passageiros nem perceberam. Só confere se tocou dentro da zona de toque: pouso macio no fim da pista "
            "não é pouso bom, é sorte.",
        ], s) if folk else (f"Toque de {v} fpm e {gs} G em {ades}. Suave. Atenção só ao flare longo: a {maker} lista flare prolongado "
                            "e toque fora da zona como risco de saída de pista.")
    elif t in ("suave", "normal"):
        out["mnt"] = None                         # pouso normal não gera conversa
    elif t == "firme":
        out["mnt"] = _pick([
            f"Comandante, a {reg} chegou em {ades} com {v} fpm e {gs} G. Não é pouso duro, não vai pra inspeção, mas o trem sentiu. "
            "Arredonda um pouco antes da próxima, por favor. Eu não quero ver isso virar hábito na minha frota.",
            f"Recebi o seu toque em {ades}: {v} fpm, {gs} G. Tá no limite do que eu engulo calado. Da próxima, menos razão na final "
            "e flare no tempo certo. Combinado?",
            f"{v} fpm e {gs} G na {reg}. Firme. Os passageiros acordaram, o comissário segurou o carrinho e eu franzi a testa. "
            "Sem inspeção desta vez.",
        ], s) if folk else (f"Toque firme em {ades}: {v} fpm, {gs} G. Dentro dos limites de projeto, sem inspeção. "
                            "Se quiser, revise a estabilização e o início do flare.")
    elif t == "duro":
        out["level"] = "alerta"
        out["mnt"] = _pick([
            f"COMANDANTE. {v} fpm e {gs} G em {ades}. Isso não foi pouso, foi queda controlada. {insp} "
            f"Você sabe quanto custa um {model} parado no chão sem voar? Eu sei, e quem explica pra diretoria sou eu. "
            "Quero o relatório de pouso duro hoje, com a aproximação inteira: vento, velocidade, onde arredondou. "
            "E aprende a fazer flare antes que eu tenha que trocar um trem de pouso por sua causa.",
            f"Que porra foi essa em {ades}? {v} fpm, {gs} G. O meu pessoal vai passar a madrugada de lanterna embaixo da {reg} "
            f"procurando trinca por causa desse pouso. {insp} Relatório na minha mesa e conversa com o Chefe de Equipamento. "
            "Não me faz passar vergonha de novo.",
            f"{v} fpm. {gs} G. Na MINHA {reg}. Em {ades}. Eu li duas vezes achando que o ACARS tinha errado. Não errou. {insp} "
            "Até a Fase 1 sair limpa, esse avião não voa, e a escala vai ter que se virar sem ele. Parabéns.",
        ], s) if folk else (f"Toque de {v} fpm e {gs} G em {ades}: critério de pouso duro atingido ({'; '.join(ev['reasons'])}). {insp} "
                            "Pedimos o relatório de pouso duro com a aproximação. Objetivo: aprender, não punir.")
    else:
        out["level"] = "alerta"
        out["mnt"] = (f"Comandante, {v} fpm e {gs} G em {ades}. Eu nem sei o que dizer, e olha que eu tenho 30 anos de hangar. {insp} "
                      f"A {reg} sai da escala por tempo indeterminado. Relatório completo hoje, sem enfeite. Graças a Deus ninguém se machucou."
                      if folk else
                      f"Toque de {v} fpm e {gs} G em {ades}, bem acima dos critérios de pouso duro. {insp} Relatório completo, por favor.")
    if ev["overweight"] and out["mnt"]:
        out["mnt"] += f" E mais: pousou acima do MLW ({ctx.get('lw')} × {ctx.get('mlw')}), o que por si só pede inspeção de pouso com sobrepeso."
    elif ev["overweight"]:
        out["mnt"] = (f"Pouso do {fn} acima do MLW ({ctx.get('lw')} × {ctx.get('mlw')}). Pouso com sobrepeso exige inspeção. {insp}")
        out["level"] = "alerta"
    if t in ("duro", "severo"):
        out["chefia"] = ((f"Comandante, recebi o relatório da Manutenção sobre o {fn} ({v} fpm, {gs} G em {ades}). Preciso de você na sala "
                          "da chefia antes da próxima programação. Traga o OFP, o que lembrar do vento e a sua versão da aproximação. "
                          "Conversa séria, e sem desculpa de vento de través.")
                         if folk else
                         (f"Comandante, sobre o {fn} ({v} fpm, {gs} G): vamos conversar antes da próxima programação. "
                          "É uma conversa de aprendizado dentro da cultura justa, não um processo disciplinar."))
    return out
