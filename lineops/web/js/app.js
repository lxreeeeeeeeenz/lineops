"use strict";
/* LineOps — telas. Todo texto vindo do servidor entra como texto (nunca HTML). */
const {h, put, svg, icon, stagger, countUp, drawLine, expand, transition, toast, dialog, closeBtn, field, chip, note, fact, skeleton, fmt} = UI;
const {hhmm, hhmmz, dmy, dm, dt, hm, hmLong, kg, today, addDays, cap, localPair} = fmt;

let DISCLAIMER = "LineOps é um projeto independente de simulação aérea, sem afiliação, patrocínio ou endosso de companhias aéreas. Todo conteúdo é simulado e não é documentação operacional.";
/* tema da carreira: só as cores (o logo é um arquivo que o próprio piloto coloca) */
const hexRgb = x => { const v = x.replace("#", ""); return [0, 2, 4].map(i => parseInt(v.slice(i, i + 2), 16)); };
function applyTheme(p) {
  if (!p || !p.accent) return;
  const r = document.documentElement.style, [R, G, B] = hexRgb(p.accent);
  r.setProperty("--orange", p.accent); r.setProperty("--ember", p.accent2 || p.accent);
  [6, 14, 30].forEach(a => r.setProperty(`--orange-${a}`, `rgba(${R},${G},${B},.${String(a).padStart(2, "0")})`));
  document.documentElement.dataset.airline = p.code;
}
const brandMark = (size = 26) => svg("svg", {width: size, height: size, viewBox: "0 0 32 32", "aria-hidden": "true"},
  svg("rect", {width: 32, height: 32, rx: 7, fill: "#17191D", stroke: "#26282E"}), svg("path", {d: "M5 21.5l21-9.5-7.2 13.5-3.1-5.2z", style: "fill:var(--orange)"}));
function careerBadge(p, size = 30) {
  if (p.has_logo) return h("img", {class: "c-logo", src: `/api/careers/${p.code}/logo?v=${Date.now() % 1e6}`, alt: p.name, style: `height:${size}px`});
  return h("span", {class: "c-mono", style: `--c:${p.accent};height:${size}px;font-size:${Math.round(size * .42)}px`}, p.name);
}
const STATE = {RESERVADO: ["Reservado", "cool"], EM_VOO: ["Em voo", "or live"], PIREP_RECEBIDO: ["PIREP em revisão", "hold"],
  APROVADO: ["Voado", "go"], REJEITADO: ["Rejeitado", "stop"], CANCELADO: ["Cancelado", ""]};
const ORIGIN = {REAL: ["REAL", "go", "Voo existente no RPL publicado."],
  PROJETADO: ["PROJETADO", "hold", "Projeção do AeroScale: repete a semana mais recente do RPL em dias ainda sem publicação."],
  OBSERVADO: ["OBSERVADO", "cool", "Voo visto no ADS-B: partida observada, duração estimada."],
  SUGESTAO: ["SUGESTÃO", "hold", "Sugestão do AeroScale."], MANUAL: ["MANUAL", "", "Inserido manualmente no AeroScale."]};
const STAGE_ICON = {PRONTO: "ok", ATENCAO: "alerta", BLOQUEADO: "bloqueio", PENDENTE: "pendente", EM_ANDAMENTO: "live", FUTURO: "futuro", SEM_DADO: "futuro", FALTA: "alerta"};
const STAGE_WORD = {PRONTO: "pronto", ATENCAO: "atenção", BLOQUEADO: "bloqueado", PENDENTE: "a preparar", EM_ANDAMENTO: "em andamento", FUTURO: "versão futura", SEM_DADO: "sem dado", FALTA: "falta"};

/* ───────────────────────── API ───────────────────────── */
class ApiError extends Error { constructor(r, b) { super((b && b.message) || r.statusText || "Falha na requisição"); this.status = r.status; this.body = b; } }
async function api(method, path, body, raw) {
  const o = {method, headers: {"X-Requested-With": "lineops"}, credentials: "same-origin"};
  if (raw !== undefined) o.body = raw;
  else if (body !== undefined) { o.body = JSON.stringify(body); o.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, o);
  let b = null;
  try { b = await r.json(); } catch (e) { /* sem corpo */ }
  if (r.status === 401 && !path.includes("login")) { showAuth(false); throw new ApiError(r, b); }
  if (!r.ok) throw new ApiError(r, b);
  return b;
}
const get = p => api("GET", p), post = (p, b) => api("POST", p, b === undefined ? {} : b);
async function act(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; }
  catch (e) { if (e.status !== 401) toast(e.message, true); return undefined; }
}

/* ───────────────────────── peças ───────────────────────── */
function stateChip(s) { const [l, c] = STATE[s] || [s, ""]; return chip(l, c); }
function originChip(o) { const [l, c, t] = ORIGIN[o] || [o, "", ""]; return o ? chip(l, c, t) : null; }
function placeName(p) { return p && (p.city || p.name) ? `${p.city || p.name}` : (p && p.icao) || "—"; }

function routeArc(from, to, distNm, w = 240) {
  const s = svg("svg", {class: "arc", viewBox: `0 0 ${w} 74`, preserveAspectRatio: "xMidYMid meet", "aria-hidden": "true"});
  const d = `M14 54 Q ${w / 2} 4 ${w - 14} 54`;                       // o ápice fica em y=29
  s.append(svg("path", {class: "path", d}));
  const live = svg("path", {class: "live", d});
  s.append(live);
  s.append(svg("circle", {class: "node", cx: 14, cy: 54, r: 4.5}), svg("circle", {class: "node", cx: w - 14, cy: 54, r: 4.5}));
  s.append(svg("path", {class: "plane", transform: `translate(${w / 2} 29)`,
    d: "M10 0 L-4 5 L-1.5 0 L-4 -5 Z M-4 0 L-9 2 L-8 0 L-9 -2 Z"}));
  if (distNm) s.append(svg("text", {class: "dist", x: w / 2, y: 68}, `${distNm} nm`));
  requestAnimationFrame(() => drawLine(live));
  return s;
}

function boarding(f, opts = {}) {
  const st = f.flight_state || f.state;
  const reg = f.registration, when = f.dep_utc;
  const head = h("div", {class: "bd-top"},
    h("span", {class: "bd-flight"}, f.flight_number || "Sem número"),
    f.data_origin ? originChip(f.data_origin) : null,
    st ? stateChip(st) : null,
    f.activity === "RESERVA" ? chip("Acionamento de reserva", "hold") : null,
    h("div", {class: "bd-when"}, opts.whenLabel || (f.today === false ? cap(dmy(when).slice(0, 5)) : "Hoje"), h("b", {}, hhmmz(when))));
  const left = h("div", {class: "port"}, h("div", {class: "icao"}, f.adep), h("div", {class: "city"}, placeName(f.from)),
    h("div", {class: "hour"}, hhmm(f.dep_utc), f.dep_local ? h("small", {}, `${f.dep_local.hhmm} local`) : null));
  const right = h("div", {class: "port r"}, h("div", {class: "icao"}, f.ades), h("div", {class: "city"}, placeName(f.to)),
    h("div", {class: "hour"}, hhmm(f.arr_utc), f.arr_local ? h("small", {}, `${f.arr_local.hhmm} local`) : null));
  const foot = h("div", {class: "bd-foot"},
    fact("Aeronave", reg || "a definir", !!reg),
    fact("Tipo", f.aircraft_type || f.type_icao || "—"),
    fact("Bloco previsto", hmLong(f.block_min)),
    f.cruise_fl ? fact("Nível", "FL" + f.cruise_fl, true) : null,
    f.availability ? h("span", {class: "chip " + (f.availability.status === "DISPONIVEL" ? "go" : f.availability.status === "COM_RESTRICAO" ? "hold" : "stop")},
      f.availability.status === "DISPONIVEL" ? "Aeronave disponível" : f.availability.status === "COM_RESTRICAO" ? "Aeronave com restrição" : "Aeronave indisponível") : null,
    h("span", {class: "spacer"}), opts.actions || null);
  return h("article", {class: "boarding"}, head, h("div", {class: "bd-body"}, left, routeArc(f.from, f.to, f.distance_nm), right), foot);
}

function stripOf(l, side, href) {
  const st = l.flight_state || l.state;
  const kids = [
    h("div", {class: "no"}, l.flight_number || "—", h("small", {}, l.aircraft_type || l.type_icao || "")),
    h("div", {class: "rt"}, h("span", {class: "pair"}, h("span", {class: "code"}, l.adep), h("span", {class: "to"}, "→"), h("span", {class: "code"}, l.ades)),
      h("span", {class: "hrs"}, `${hhmm(l.dep_utc)}–${hhmm(l.arr_utc)}Z`), l.block_min ? h("span", {class: "hrs"}, hmLong(l.block_min)) : null),
    h("div", {class: "side"}, side),
    h("div", {class: "ext"}, [l.registration ? h("span", {}, l.registration) : null,
      l.cruise_fl ? h("span", {}, "FL" + l.cruise_fl) : null,
      l.route ? h("span", {title: l.route}, l.route.length > 52 ? l.route.slice(0, 52) + "…" : l.route) : null])];
  const attrs = {class: "strip" + (l.removed_batch ? " gone" : ""), "data-s": st || ""};
  if (href) { attrs.href = href; return h("a", attrs, kids); }
  return h("div", attrs, kids);
}

function gapLine(t) {
  const strong = t.overnight || t.below_min_turn || !t.same_airport;
  return h("div", {class: "gap" + (strong ? " long" : ""), title: t.kind},
    icon(t.overnight ? "pernoite" : t.below_min_turn ? "alerta" : "relogio", 14),
    `${t.overnight ? t.overnight_label : t.label} de ${hmLong(t.minutes)} em ${placeName(t.at)}`,
    t.below_min_turn ? chip(`abaixo do solo mínimo da escala (${t.min_turn} min)`, "hold") : null,
    !t.same_airport ? chip(`próxima perna parte de ${t.to ? t.to.icao : "outro aeroporto"}`, "hold") : null);
}

function journeyRail(day) {
  const items = [];
  const legs = day.legs.filter(l => l.is_flight);
  if (day.duty && day.duty.report_utc) {
    items.push(h("div", {class: "mark key"}, icon("relogio", 15), "Apresentação ", h("b", {}, localPair(day.duty.report_utc, day.duty.report_local)),
      day.base && legs[0] && legs[0].adep !== day.base.icao ? chip("fora da base", "hold") : null));
  } else if (legs.length) {
    items.push(h("div", {class: "mark"}, icon("info", 15), "Horário de apresentação não definido nos parâmetros da escala"));
  }
  legs.forEach((l, i) => {
    const flown = l.flight_state === "APROVADO";
    items.push(h("div", {class: "stop" + (flown ? " flown" : l.flight_id ? " on" : "")},
      stripOf(l, [swapChip((day.swaps || {})[l.id]), l.flight_state ? stateChip(l.flight_state) : chip("Não reservado", ""), icon("seta", 16)], l.flight_id ? `#/voo/${l.flight_id}` : null)));
    const t = day.turns[i];
    if (t) items.push(gapLine(t));
  });
  if (day.after_day && !day.overnight && day.after_day.same_airport && day.after_day.minutes > 0 && day.duty)
    items.push(h("div", {class: "gap"}, icon("pendente", 14), `${day.after_day.label.toLowerCase()} de ${hmLong(day.after_day.minutes)} até a próxima perna`));
  if (day.duty && day.duty.release_utc) items.push(h("div", {class: "mark key"}, icon("ok", 15), "Encerramento ", h("b", {}, localPair(day.duty.release_utc, day.duty.release_local)), " em ", placeName(day.ends_at)));
  if (day.overnight) items.push(h("div", {class: "mark key", title: day.overnight.source}, icon("pernoite", 15),
    day.overnight.label + " em ", h("b", {}, placeName(day.overnight.at)), h("span", {class: "muted"}, hmLong(day.overnight.minutes)),
    day.overnight.at_base ? chip("na base", "go") : chip("fora da base", "hold")));
  if (day.day_off && legs.length) items.push(h("div", {class: "mark"}, icon("pernoite", 15), "Folga programada começa neste dia"));
  return h("div", {class: "rail-journey"}, items);
}

/* ───────────────────────── navegação ───────────────────────── */
const VIEWS = {};
const NAV = [["painel", "Painel"], ["mensagens", "Mensagens"], ["escala", "Escala"], ["voos", "Voos"], ["rede", "Rede"], ["frota", "Frota"], ["pireps", "PIREPs"], ["diario", "Diário"], ["carreira", "Carreira"], ["estatisticas", "Estatísticas"], ["externos", "Voos Externos"], ["salarios", "Salários"], ["ajustes", "Ajustes"]];
let me = null, pending = 0, booted = false;
const view = (name, fn) => VIEWS[name] = fn;

function shell(cur) {
  const ind = h("span", {class: "ind", "aria-hidden": "true", style: "opacity:0"});
  const links = NAV.filter(([k]) => k !== "salarios" || payOn()).map(([k, l]) => h("a", {class: "nav", href: "#/" + k, "aria-current": k === cur ? "page" : null},
    icon(k, 19), h("span", {}, l), k === "pireps" && pending ? h("span", {class: "tag"}, pending) : null,
    k === "mensagens" ? h("span", {class: "tag nav-msg-tag", hidden: !inbox.unread}, inbox.unread) : null));
  const list = h("nav", {class: "navlist", "aria-label": "Seções"}, ind, links);
  requestAnimationFrame(() => {
    const a = list.querySelector("[aria-current=page]");
    if (a && innerWidth > 860) { ind.style.transform = `translateY(${a.offsetTop}px)`; ind.style.opacity = "1"; }
  });
  const logo = brandMark(26);
  const car = me.career || {};
  const netEl = h("a", {class: "netdot", href: "#/rede", title: "Gente voando agora na IVAO (whazzup público)"});
  railPulse(netEl);
  return h("div", {class: "rail"}, h("div", {class: "brand"}, logo, h("span", {}, "Line", h("b", {}, "Ops"))),
    h("a", {class: "c-switch", href: "#/modo", title: "Trocar de companhia"}, careerBadge({...car, has_logo: me.career_logo}, 24), h("span", {}, h("b", {}, `Modo ${car.name || ""}`), h("small", {}, "trocar"))),
    h("button", {class: "pal-btn", type: "button", onclick: () => openPalette(), title: "Paleta de comandos"}, icon("busca", 15), h("span", {}, "Buscar"), h("kbd", {}, "Ctrl K")),
    list, netEl,
    h("div", {class: "who" + (me.avatar ? " has-avatar" : "")}, me.avatar ? h("img", {class: "avatar", src: me.avatar, alt: "", width: 36, height: 36}) : null,
      h("div", {}, h("b", {}, me.display_name), me.pilot_code, " · ", h("a", {href: "#", onclick: async e => {
        e.preventDefault(); await act(() => post("/api/logout")); me = null; showAuth(false);
      }}, "sair"))));
}

async function render() {
  if (!me) return;
  const [name, arg] = (location.hash.replace(/^#\/?/, "") || "painel").split("?")[0].split("/");
  if (name === "modo") return modeScreen();
  const fn = VIEWS[name] || VIEWS.painel;
  const main = h("main", {}, skeleton("s", "h"));
  const paint = () => document.getElementById("app").replaceChildren(h("div", {class: "shell"}, shell(VIEWS[name] ? name : "painel"),
    h("div", {class: "stage"}, hudStrip(), main)));
  if (booted) await transition(paint); else paint();
  booted = true;
  try {
    await fn(main, arg);
    stagger(main);
    if (name !== "mensagens") refreshBadge();
  } catch (e) {
    if (e.status !== 401) put(main, h("div", {class: "head"}, h("h1", {}, "Algo falhou")), note("stop", "alerta", e.message));
  }
  if (location.hash !== lastHash) { lastHash = location.hash; scrollTo({top: 0}); }
}
let lastHash = "";
addEventListener("hashchange", render);

/* ───────────────────────── continuações (beta) ───────────────────────── */
const WDN = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"];
const wdText = w => !w || !w.length || w.length === 7 ? "todos os dias" : w.map(x => WDN[x]).join(", ");
const CONF_CLS = {alta: "go", "média": "cool", baixa: "", regra: "hold"};
function contLine(c, base) {
  if (!c) return null;
  if (c.source === "regra") return h("span", {}, chip("sua regra", "hold"), " ", c.to_airport ? `segue para ${c.to_airport}` : `segue no ${c.ident}`, c.note ? h("span", {class: "muted"}, ` · ${c.note}`) : null);
  if (!c.ident) return h("span", {class: "muted"}, `Sem continuação vista naquele dia: o avião costuma pernoitar em ${base} (${c.nights} de ${c.observed} vezes no ADS-B).`);
  return h("span", {}, chip(`confiança ${c.confidence}`, CONF_CLS[c.confidence]), " ", c.kind === "volta" ? "volta no " : "segue no ", h("b", {}, c.ident),
    ` ${c.adep}→${c.ades} · ${c.gap_min} min de solo`, h("span", {class: "muted"}, ` · ADS-B ${c.adsb}×${c.pair ? ` · par ${c.delta > 0 ? "+" : "−"}1${c.inverted ? " (invertido)" : ""}` : ""}`));
}
function contDayPanel(d) {
  const fl = (d.legs || []).filter(l => l.is_flight !== false && l.flight_number);
  if (!fl.length) return null;
  const box = h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Continuações do avião (beta)"), h("a", {href: "#/continuacoes", class: "muted", style: "font-size:13px"}, "ver todas")), skeleton("s"));
  get(`/api/continuacoes/dia/${d.date}`).then(m => {
    if (!box.isConnected) return;
    put(box, h("div", {class: "panel-head"}, h("h2", {}, "Continuações do avião (beta)"), h("a", {href: "#/continuacoes", class: "muted", style: "font-size:13px"}, "ver todas")),
      h("div", {class: "cont-list"}, fl.map(l => h("div", {class: "cont-row"}, h("b", {class: "mono"}, l.flight_number), h("span", {class: "muted mono"}, `${l.adep}→${l.ades}`),
        h("div", {}, contLine(m[String(l.id)], l.ades) || h("span", {class: "muted"}, "Sem dados de rodízio para este voo."))))),
      h("p", {class: "fine"}, "Depois deste voo, para onde o avião costuma seguir: o RPL (voos que saem dali até 4 h depois) cruzado com os rodízios vistos no ADS-B. É estimativa, não escala."));
  }).catch(() => box.remove());
  return box;
}
const cont = {q: ""};
view("continuacoes", async main => {
  const car = me.career || {};
  const q = h("input", {placeholder: `Voo (3376 ou ${car.code}3376) ou trecho (REC, GRU-REC)`, value: cont.q});
  const res = h("div", {});
  const rulesBox = h("div", {class: "panel"}, h("h2", {}, "Suas continuações"), skeleton("s"));
  const f = {from: h("input", {placeholder: "ex. 3376"}), to: h("input", {placeholder: "voo (3262) ou destino (PNZ)"}), note: h("input", {placeholder: "ex. o piloto disse"})};
  const wds = new Set();
  const wdBox = h("div", {class: "tools"});
  const paintWd = () => put(wdBox, WDN.map((n, k) => h("button", {type: "button", class: "chip" + (wds.has(k) ? " on" : ""), onclick: () => { wds.has(k) ? wds.delete(k) : wds.add(k); paintWd(); }}, n)));
  paintWd();
  const loadRules = async () => {
    const rs = await get("/api/continuacoes/regras");
    put(rulesBox, h("h2", {}, "Suas continuações"),
      h("p", {class: "fine", style: "margin-top:2px"}, "O gerador já usa as continuações identificadas sozinho, na força escolhida em Gerar escala (padrão 70). As que você cadastrar aqui (ou o piloto disse) valem sempre, por cima da estimativa: quando o voo entra na escala e a continuação existe naquele dia, ela vem em seguida. Destino (PNZ) aceita qualquer voo para lá."),
      rs.length ? h("div", {class: "cont-list"}, rs.map(r => h("div", {class: "cont-row"}, h("b", {class: "mono"}, r.from_ident),
        h("span", {}, "→ ", h("b", {}, r.to_ident.startsWith("@") ? `qualquer voo para ${r.to_ident.slice(1)}` : r.to_ident)),
        h("span", {class: "muted"}, `${wdText(r.weekdays)}${r.note ? " · " + r.note : ""}`),
        h("button", {class: "btn ghost sm", onclick: async () => { if (await act(() => api("DELETE", `/api/continuacoes/regras/${r.id}`), "Regra apagada")) loadRules(); }}, "Apagar"))))
        : h("p", {class: "muted"}, "Nenhuma ainda."),
      h("div", {class: "hr"}),
      h("div", {class: "row"}, field("Depois do voo", f.from), field("Segue para", f.to)),
      h("div", {class: "f"}, h("span", {}, "Dias de partida do voo (nenhum = todos)"), wdBox),
      field("Nota (opcional)", f.note),
      h("button", {class: "btn sm", onclick: async () => {
        const r = await act(() => post("/api/continuacoes/regras", {from: f.from.value, to: f.to.value, weekdays: [...wds], note: f.note.value}), "Continuação cadastrada");
        if (r) { f.from.value = f.to.value = f.note.value = ""; wds.clear(); paintWd(); loadRules(); }
      }}, "Cadastrar"));
  };
  const search = async () => {
    cont.q = q.value.trim();
    if (!cont.q) return put(res);
    put(res, skeleton("l"));
    try {
      const r = await get(`/api/continuacoes?q=${encodeURIComponent(cont.q)}`);
      put(res, r.items.length ? r.items.map(x => h("div", {class: "panel", style: "margin-top:14px"},
        h("div", {class: "panel-head"}, h("h2", {}, `${x.ident} · ${x.adep} → ${x.ades}`),
          h("span", {class: "muted", style: "font-size:12.5px"}, `voa ${wdText(x.weekdays)} · visto ${x.observed}× no ADS-B${x.nights ? ` · pernoitou ${x.nights}×` : ""}`)),
        x.candidates.length ? h("div", {class: "tbl"}, h("table", {}, h("thead", {}, h("tr", {}, ["Continuação", "Trecho", "Dias de partida do " + x.ident, "Solo", "ADS-B", "Pista", "Confiança"].map(t => h("th", {}, t)))),
          h("tbody", {}, x.candidates.map(c => h("tr", {}, h("td", {}, h("b", {}, c.ident)), h("td", {class: "t"}, `${c.adep}→${c.ades}${c.kind === "volta" ? " (volta)" : ""}`),
            h("td", {}, wdText(c.weekdays)), h("td", {class: "n"}, `${c.gap_min} min`), h("td", {class: "n"}, `${c.adsb}× · ${Math.round(c.rate * 100)}%`),
            h("td", {}, c.pair ? `par ${c.delta > 0 ? "+" : "−"}1${c.inverted ? " · invertido" : ""}` : "—"), h("td", {}, chip(c.confidence, CONF_CLS[c.confidence])))))))
          : h("p", {class: "muted"}, "Nenhuma continuação no RPL nem no ADS-B: o avião costuma pernoitar ou trocar de voo depois de mais de 4 h."),
        x.rules.length ? h("p", {}, chip("sua regra", "hold"), " ", x.rules.map(r => `${r.to_ident.startsWith("@") ? "qualquer voo para " + r.to_ident.slice(1) : r.to_ident} (${wdText(r.weekdays)})`).join("; ")) : null,
        h("button", {class: "btn ghost sm", onclick: () => { f.from.value = x.ident; f.to.focus(); f.to.scrollIntoView({behavior: "smooth", block: "center"}); }}, "Cadastrar continuação deste voo")))
        : h("p", {class: "muted", style: "margin-top:12px"}, "Nenhum voo encontrado na semana publicada do RPL."),
        h("p", {class: "fine", style: "margin-top:12px"}, `Semana de referência do RPL: ${dmy(r.reference_week[0])} a ${dmy(r.reference_week[1])} · ${r.adsb_days} dias de ADS-B · ${r.source}. ADS-B “2× · 27%”: visto 2 vezes, em 27% das vezes em que os dois voos existiam no período. Par ±1: numeração de ida e volta; invertido = o sentido menos comum na companhia.`));
    } catch (e) { put(res, note("stop", "alerta", e.message)); }
  };
  q.addEventListener("keydown", e => { if (e.key === "Enter") search(); });
  put(main, h("div", {class: "head"}, h("div", {}, h("h1", {}, "Continuações do avião"),
      h("div", {class: "sub"}, "Beta: depois de cada voo, para onde o avião (e a tripulação) costuma seguir. RPL do CGNA + rodízios vistos no ADS-B + o que você cadastrar."))),
    h("div", {class: "panel"}, h("div", {class: "row"}, field("Buscar", q), h("div", {style: "align-self:end;margin-bottom:13px"}, h("button", {class: "btn", onclick: search}, "Buscar")))),
    res, h("div", {style: "margin-top:18px"}, rulesBox));
  loadRules();
  if (cont.q) search();
});

/* ───────────────────────── estatísticas ───────────────────────── */
const stats = {src: "roster", period: "mes"};
view("estatisticas", async main => {
  const ym = today().slice(0, 7), [y, m] = ym.split("-").map(Number), last = new Date(Date.UTC(y, m, 0)).toISOString().slice(0, 10);
  const q = stats.period === "mes" ? `?inicio=${ym}-01&fim=${last}` : "";
  const d = await get("/api/estatisticas" + q);
  const s = stats.src === "roster" ? d.roster : d.flown;
  const seg = (opts, cur, set) => h("div", {class: "seg"}, opts.map(([v, l]) => h("button", {"aria-pressed": cur === v, onclick: () => { set(v); render(); }}, l)));
  const apName = a => a.iata ? `${a.iata}` : a.icao;
  const bars = (list, label) => {
    const max = Math.max(1, ...list.map(x => x.n));
    return list.length ? h("div", {class: "rank"}, list.map((x, k) => h("div", {class: "rank-row"},
      h("span", {class: "rank-k"}, String(k + 1)), h("span", {class: "rank-name"}, label(x)),
      h("span", {class: "rank-bar"}, h("i", {style: `width:${Math.round(100 * x.n / max)}%`})), h("b", {class: "rank-n"}, x.n))))
      : h("p", {class: "muted"}, "Nada no período.");
  };
  const hourMax = Math.max(1, ...s.by_hour);
  const fig = (v, l) => h("div", {class: "fig"}, h("b", {}, v), h("span", {}, l));
  put(main, h("div", {class: "head"}, h("div", {}, h("h1", {}, "Estatísticas"),
      h("div", {class: "sub"}, `${dmy(d.period.start)} a ${dmy(d.period.end)} · ${stats.src === "roster" ? "o que a escala programou" : "o que você voou (diário)"}`)),
      h("div", {class: "tools"}, seg([["mes", "Este mês"], ["tudo", "Tudo"]], stats.period, v => stats.period = v),
        seg([["roster", "Escala"], ["flown", "Diário"]], stats.src, v => stats.src = v))),
    h("div", {class: "figs"}, fig(s.legs, "pernas"), fig(`${Math.floor(s.block_min / 60)}h${String(s.block_min % 60).padStart(2, "0")}`, "de bloco"),
      fig(s.flight_days, "dias de voo"), fig(s.airports, "aeroportos"), fig(s.legs_per_day == null ? "—" : s.legs_per_day.toLocaleString("pt-BR"), "pernas por dia"),
      stats.src === "roster" && s.projected_pct != null ? fig(`${s.projected_pct}%`, "projetado do RPL") : fig(s.intl, "internacionais")),
    h("div", {class: "cols-2", style: "margin-top:18px"},
      h("div", {class: "panel"}, h("h2", {}, "Aeroportos mais usados"), h("p", {class: "fine", style: "margin-top:2px"}, "Pousos e decolagens somados."),
        bars(s.top_airports, x => h("span", {}, h("b", {}, apName(x)), " ", h("span", {class: "muted"}, x.city))),
        h("div", {class: "cols", style: "margin-top:14px;gap:12px"},
          h("div", {}, h("h3", {}, "Mais partidas"), bars(s.top_departures, x => h("b", {}, apName(x)))),
          h("div", {}, h("h3", {}, "Mais chegadas"), bars(s.top_arrivals, x => h("b", {}, apName(x)))))),
      h("div", {},
        h("div", {class: "panel"}, h("h2", {}, "Rotas"), bars(s.top_routes, x => h("b", {}, `${apName(x.a)} ⇄ ${apName(x.b)}`))),
        stats.src === "roster" ? h("div", {class: "panel", style: "margin-top:18px"}, h("h2", {}, "Onde você mais pernoita"),
          bars(d.overnights, x => h("span", {}, h("b", {}, apName(x)), " ", h("span", {class: "muted"}, x.city)))) : null)),
    h("div", {class: "cols-2", style: "margin-top:18px"},
      h("div", {class: "panel"}, h("h2", {}, "Partidas por hora (local)"),
        h("div", {class: "hours"}, s.by_hour.map((n, k) => h("div", {class: "hr-col", title: `${String(k).padStart(2, "0")}h: ${n}`},
          h("i", {style: `height:${Math.round(100 * n / hourMax)}%`}), h("span", {}, k % 3 === 0 ? String(k).padStart(2, "0") : ""))))),
      h("div", {class: "panel"}, h("h2", {}, "Destaques"), h("dl", {class: "kv"},
        h("dt", {}, "Etapa mais longa"), h("dd", {}, s.longest ? `${s.longest.flight_number || "voo"} ${s.longest.adep}–${s.longest.ades} · ${Math.floor(s.longest.min / 60)}h${String(s.longest.min % 60).padStart(2, "0")}` : "—"),
        h("dt", {}, "Dia mais cheio"), h("dd", {}, s.busiest_day ? `${dmy(s.busiest_day.date)} · ${s.busiest_day.legs} pernas` : "—"),
        h("dt", {}, "Voo que mais se repete"), h("dd", {}, s.top_flights && s.top_flights[0] ? `${s.top_flights[0].key} · ${s.top_flights[0].n}×` : "—"),
        s.extra ? [h("dt", {}, "Como extra"), h("dd", {}, `${s.extra} perna(s)`)] : null))),
    h("p", {class: "fine", style: "margin-top:14px"}, d.note));
});

/* ───────────────────────── faixa de instrumentos (3.0.1) ─────────────────────────
   Como a faixa de cima de um painel de voo: hora UTC e local correndo, a próxima apresentação com contagem regressiva e
   o próximo voo. Dados de /api/today (no máximo uma busca por minuto); a hora corre no navegador. */
const hud = {data: null, at: 0, busy: false};
function hudStrip() {
  const el = h("div", {class: "hud", role: "status", "aria-live": "off"});
  const cell = (label, ...v) => h("div", {class: "hud-cell"}, h("span", {class: "hud-l"}, label), h("span", {class: "hud-v"}, ...v));
  const pad = n => String(n).padStart(2, "0");
  const target = () => {
    const d = hud.data; if (!d) return null;
    const now = Date.now();
    const rep = d.duty && d.duty.report_utc && Date.parse(d.duty.report_utc) > now ? {utc: d.duty.report_utc, at: d.legs && d.legs[0] ? d.legs[0].adep : null}
      : d.next_duty && d.next_duty.report_utc ? {utc: d.next_duty.report_utc, at: d.next_duty.at && d.next_duty.at.icao} : null;
    return rep;
  };
  const tick = () => {
    if (!el.isConnected) return clearInterval(t);
    const n = new Date(), z = `${pad(n.getUTCHours())}:${pad(n.getUTCMinutes())}:${pad(n.getUTCSeconds())}`;
    const loc = `${pad(n.getHours())}:${pad(n.getMinutes())}`;
    const rep = target(), nf = hud.data && hud.data.next_flight;
    let cd = null, cls = "";
    if (rep) {
      const m = Math.round((Date.parse(rep.utc) - n.getTime()) / 60000);
      if (m >= 0) { cd = m >= 1440 ? `${Math.floor(m / 1440)}d ${pad(Math.floor(m % 1440 / 60))}h` : `${pad(Math.floor(m / 60))}:${pad(m % 60)}`; cls = m < 120 ? " warn" : ""; }
    }
    put(el,
      cell("UTC", h("b", {class: "hud-clock"}, z)),
      cell("Local", h("b", {}, loc)),
      cell("Próxima apresentação", rep ? h("b", {class: "hud-cd" + cls}, cd ? `T−${cd}` : "agora") : h("b", {class: "dim"}, "sem escala"),
        rep ? h("small", {}, `${dt(rep.utc)}${rep.at ? " · " + rep.at : ""}`) : null),
      nf ? cell("Próximo voo", h("b", {}, nf.flight_number || "voo"), h("small", {}, `${nf.adep} → ${nf.ades}${nf.registration ? " · " + nf.registration : ""}`)) : null,
      hud.data && hud.data.live ? cell("Em voo", h("b", {class: "hud-live"}, hud.data.live.flight_number || "ACARS"), h("small", {}, hud.data.live.phase || "")) : null);
  };
  const load = async () => {
    if (hud.busy || Date.now() - hud.at < 60000) return;
    hud.busy = true;
    try { hud.data = await get("/api/today"); hud.at = Date.now(); } catch (e) { /* a hora segue sem os dados */ } finally { hud.busy = false; }
    tick();
  };
  const t = setInterval(() => { tick(); if (Date.now() - hud.at >= 60000) load(); }, 1000);
  tick(); load();
  return el;
}

/* ───────────────────────── paleta de comandos (Ctrl+K) e atalhos ───────────────────────── */
const pal = {fleet: null};
function paletteItems(q) {
  const items = NAV.filter(([k]) => k !== "salarios" || payOn()).map(([k, l]) => ({label: l, hint: "tela", go: `#/${k}`}));
  items.push({label: "Gerar escala", hint: "ação", go: "#/gerar"}, {label: "Escala de hoje", hint: "ação", go: `#/escala/${today()}`},
             {label: "Trocar de companhia", hint: "ação", go: "#/modo"}, {label: "Continuações do avião (beta)", hint: "tela", go: "#/continuacoes"},
             {label: "Escala em texto / agenda", hint: "ação", fn: () => rosterTextDialog(roster.date, roster.bounds || {})});
  const m = q.match(/^(\d{1,2})[\/.-](\d{1,2})(?:[\/.-](\d{2,4}))?$/) || q.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (m) {
    let d;
    if (q.includes("-") && m[1].length === 4) d = `${m[1]}-${m[2]}-${m[3]}`;
    else { const y = m[3] ? (m[3].length === 2 ? "20" + m[3] : m[3]) : today().slice(0, 4); d = `${y}-${String(m[2]).padStart(2, "0")}-${String(m[1]).padStart(2, "0")}`; }
    if (!isNaN(Date.parse(d))) items.unshift({label: `Ir para o dia ${dmy(d)}`, hint: "escala", go: `#/escala/${d}`});
  }
  (pal.fleet || []).forEach(a => items.push({label: `${a.registration} · ${a.type_icao}${typeof cfgShort === "function" && cfgShort(a) ? " · " + cfgShort(a) : ""}`, hint: "frota", go: `#/frota/${a.id}`, quiet: true}));
  const n = s => s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  const qq = n(q.trim());
  return (qq ? items.filter(x => n(x.label).includes(qq) || n(x.hint).includes(qq)) : items.filter(x => !x.quiet)).slice(0, 12);
}
function openPalette() {
  if (!me || document.getElementById("pal")) return;
  if (!pal.fleet) get("/api/aircraft").then(l => { pal.fleet = l; paint(); }).catch(() => {});
  const inp = h("input", {placeholder: "Tela, ação, dia (12/11) ou matrícula…", autocomplete: "off", spellcheck: false});
  const list = h("div", {class: "pal-list", role: "listbox"});
  let sel = 0, items = [];
  const close = () => wrap.remove();
  const run = it => { close(); if (!it) return; if (it.fn) it.fn(); else location.hash = it.go; };
  const paint = () => {
    items = paletteItems(inp.value); sel = Math.min(sel, Math.max(0, items.length - 1));
    put(list, items.length ? items.map((it, k) => h("button", {class: "pal-item" + (k === sel ? " on" : ""), role: "option", onmousemove: () => { if (sel !== k) { sel = k; paint(); } },
      onclick: () => run(it)}, h("span", {}, it.label), h("small", {}, it.hint))) : h("p", {class: "muted", style: "padding:10px 14px;margin:0"}, "Nada encontrado."));
  };
  inp.addEventListener("input", () => { sel = 0; paint(); });
  inp.addEventListener("keydown", e => {
    if (e.key === "ArrowDown") { sel = Math.min(items.length - 1, sel + 1); paint(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { sel = Math.max(0, sel - 1); paint(); e.preventDefault(); }
    else if (e.key === "Enter") { run(items[sel]); e.preventDefault(); }
    else if (e.key === "Escape") close();
  });
  const wrap = h("div", {id: "pal", class: "pal-back", onclick: e => { if (e.target === wrap) close(); }},
    h("div", {class: "pal"}, inp, list, h("div", {class: "pal-foot"}, "↑↓ escolher · Enter abrir · Esc fechar · ? atalhos")));
  document.body.append(wrap); paint(); inp.focus();
}
function shortcutsHelp() {
  dialog("Atalhos de teclado", null, h("dl", {class: "kv"},
    [["Ctrl + K", "paleta: telas, ações, dia (12/11) e matrícula"], ["← →", "dia (ou semana, ou mês) anterior / seguinte, na Escala"], ["T", "hoje, na Escala"],
     ["D · S · M", "modo Dia, Semana e Mês, na Escala"], ["G", "Gerar escala"], ["?", "esta ajuda"]].map(([k, v]) => [h("dt", {class: "mono"}, k), h("dd", {}, v)])), [closeBtn]);
}
addEventListener("keydown", e => {
  if (!me) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); return; }
  const t = e.target, typing = t && (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName));
  if (typing || e.ctrlKey || e.metaKey || e.altKey || document.getElementById("pal") || document.getElementById("dlg").open) return;
  const onRoster = location.hash.startsWith("#/escala");
  if (e.key === "?") { shortcutsHelp(); return; }
  if (e.key === "g" || e.key === "G") { location.hash = "#/gerar"; return; }
  if (!onRoster) return;
  const k = e.key.toLowerCase();
  if (e.key === "ArrowLeft" || e.key === "ArrowRight") { const b = document.querySelector(`.stepper button[aria-label="${e.key === "ArrowLeft" ? "Anterior" : "Próximo"}"]`); if (b) { b.click(); e.preventDefault(); } }
  else if (k === "t") { roster.date = today(); location.hash = `#/escala/${today()}`; render(); }
  else if (k === "d" || k === "s" || k === "m") { roster.mode = {d: "dia", s: "semana", m: "mes"}[k]; render(); }
});

/* ───────────────────────── entrada ───────────────────────── */
/* senha esquecida: código no arquivo REDEFINIR-SENHA.txt da pasta de dados (só no PC da instalação) */
function forgotDialog(user0) {
  const u = h("input", {autocomplete: "username", value: user0 || "", placeholder: "seu usuário"});
  const code = h("input", {inputmode: "numeric", autocomplete: "one-time-code", maxlength: 6, placeholder: "6 dígitos", class: "mono"});
  const p1 = h("input", {type: "password", autocomplete: "new-password", minlength: 8});
  const p2 = h("input", {type: "password", autocomplete: "new-password", minlength: 8});
  const msg = h("div");
  const ask = async () => {
    try {
      const r = await post("/api/senha/codigo");
      put(msg, note("go", "ok", "Código gerado. Ele está no arquivo ", h("b", {}, "REDEFINIR-SENHA.txt"), " da pasta de dados da LineOps",
        h("div", {class: "mono", style: "font-size:12px;margin-top:4px;word-break:break-all"}, r.file),
        "No Windows o arquivo já abre sozinho. Vale 10 minutos."));
      code.focus();
    } catch (x) { put(msg, note("stop", "alerta", x.message)); }
  };
  dialog("Esqueci a senha", "A senha não fica guardada em lugar nenhum (só um hash), então ela não se recupera: você cria uma nova. Nada da sua carreira é apagado.",
    h("div", {}, h("button", {class: "btn ghost sm", type: "button", onclick: ask}, "Gerar código"), msg,
      field("Usuário", u), field("Código do arquivo", code), field("Senha nova (mín. 8)", p1), field("Repita a senha nova", p2)),
    [closeBtn, {label: "Trocar a senha", keep: true, fn: async () => {
      if (p1.value !== p2.value) { put(msg, note("stop", "alerta", "As senhas não conferem.")); return false; }
      try {
        await post("/api/senha/redefinir", {username: u.value.trim(), code: code.value.trim(), password: p1.value});
        document.getElementById("dlg").close();
        toast("Senha trocada. Entre com a senha nova.");
      } catch (x) { put(msg, note("stop", "alerta", x.message)); }
    }}]);
}
function showAuth(setup) {
  me = null; booted = false;
  const u = h("input", {autocomplete: "username", required: true, minlength: 3, placeholder: "comandante"});
  const p = h("input", {type: "password", autocomplete: setup ? "new-password" : "current-password", required: true, minlength: 8});
  const err = h("div", {hidden: true});
  const box = h("form", {class: "panel box", onsubmit: async e => {
    e.preventDefault();
    put(err).hidden = true;
    try { await post(setup ? "/api/setup" : "/api/login", {username: u.value.trim(), password: p.value, display_name: u.value.trim()}); await boot(); }
    catch (x) { put(err, note("stop", "alerta", x.message)).hidden = false; }
  }},
    h("h2", {}, setup ? "Primeiro acesso" : "Entrar"),
    h("p", {class: "muted", style: "font-size:13.5px"}, setup ? "Crie o usuário desta instalação. A senha precisa de pelo menos 8 caracteres." : "Use o usuário criado nesta instalação."),
    err, field("Usuário", u), field("Senha", p), h("button", {class: "btn wide", type: "submit"}, setup ? "Criar e entrar" : "Entrar"),
    setup ? null : h("p", {style: "margin:12px 0 0;text-align:center;font-size:13px"},
      h("a", {href: "#", onclick: e => { e.preventDefault(); forgotDialog(u.value.trim()); }}, "Esqueci a senha")));
  const logo = brandMark(30);
  document.getElementById("app").replaceChildren(h("div", {class: "auth"}, h("div", {class: "box"},
    h("div", {class: "brand"}, logo, h("span", {}, "Line", h("b", {}, "Ops"))),
    h("p", {class: "tag"}, "Centro de operações pessoal"), box, h("p", {class: "fine"}, DISCLAIMER))));
  setTimeout(() => u.focus(), 80);
}

/* ───────────────────────── painel ───────────────────────── */
view("painel", async main => {
  const d = await get("/api/today");
  pending = d.pending_pireps;
  const legs = d.legs.filter(l => l.is_flight);
  const nf = d.next_flight;
  const saud = [];
  if (d.kind === "VOO") saud.push("Sua programação de hoje está pronta.");
  else if (d.kind === "RESERVA" && d.reserve) {
    saud.push(h("span", {}, "Hoje você está de ", h("b", {}, "reserva"), " em ", placeName(d.reserve.at),
      `, de ${dmy(d.reserve.from)} a ${dmy(d.reserve.to)}.`));
    saud.push(d.reserve.activated_today ? " Reserva acionada para hoje."
      : d.reserve.act_start ? ` Acionamento previsto para ${dmy(d.reserve.act_start)}.` : ` Situação do acionamento: ${d.reserve.status || "sem informação"}.`);
  } else if (d.kind === "FOLGA") saud.push("Hoje é folga programada na sua escala.");
  else saud.push("Você não possui voos programados para hoje.");
  if (d.kind !== "VOO" && d.next_duty) saud.push(h("span", {}, " Sua próxima apresentação está prevista para ", h("b", {}, dmy(d.next_duty.date)),
    d.next_duty.report_utc ? ` às ${hhmmz(d.next_duty.report_utc)}` : "", " em ", placeName(d.next_duty.at), "."));
  if (d.kind !== "VOO" && !d.next_duty && d.roster_legs === 0) saud.push(" Nenhuma escala foi importada ainda.");

  const avisos = [];
  if (d.fleet_count === 0) avisos.push(note("hold", "frota", "Sua frota está vazia e o AeroScale não informa matrículas. ",
    h("a", {href: "#/frota"}, "Cadastre as aeronaves que você usa"), " para poder reservar voos e usar o ACARS."));
  if (d.roster_legs === 0) avisos.push(note("or", "importar", "Nenhuma escala importada. ", h("a", {href: "#/ajustes"}, "Importe o backup do AeroScale"), "."));
  if (d.live) avisos.push(note("or", "live", h("b", {}, "Voo em andamento: "), `${d.live.flight_number || "voo"} ${d.live.adep}→${d.live.ades}`,
    d.live.registration ? ` · ${d.live.registration}` : "", ` · ${d.live.phase || "—"}`,
    d.live.snapshot && d.live.snapshot.alt_ft != null ? ` · ${Math.round(d.live.snapshot.alt_ft).toLocaleString("pt-BR")} ft` : "",
    ` · atualizado ${hhmmz(d.live.last_seen_at)}`, " ", h("a", {href: `#/voo/${d.live.flight_id}`}, "acompanhar")));
  if ((d.tz_unknown || []).length) avisos.push(note("", "info", `Sem fuso conhecido para ${d.tz_unknown.join(", ")}: nestes aeroportos os horários aparecem só em UTC, como no AeroScale.`));
  (d.diagnostics || []).forEach(x => avisos.push(note(x.level === "warning" ? "hold" : "", x.level === "warning" ? "alerta" : "info",
    h("b", {}, "AeroScale: "), x.message)));
  if (pending) avisos.push(note("hold", "pireps", `${pending} PIREP ${pending > 1 ? "aguardam" : "aguarda"} sua revisão. `, h("a", {href: "#/pireps"}, "Revisar agora")));

  const resumo = h("div", {class: "panel"}, h("h2", {}, "Resumo operacional"), h("div", {class: "hr"}),
    h("dl", {class: "kv"},
      h("dt", {}, "Voos hoje"), h("dd", {}, legs.length || "nenhum"),
      h("dt", {}, "Tempo de bloco"), h("dd", {}, d.duty && d.duty.block_min ? hmLong(d.duty.block_min) : "—"),
      h("dt", {}, "Jornada"), h("dd", {}, d.duty && d.duty.duty_min ? hmLong(d.duty.duty_min) : "—"),
      h("dt", {}, "Aeronave"), h("dd", {}, nf && nf.registration ? nf.registration : "a definir"),
      h("dt", {}, "Situação técnica"), h("dd", {}, nf && nf.availability ? (nf.availability.status === "DISPONIVEL" ? "disponível" : nf.availability.status === "COM_RESTRICAO" ? "com restrição" : "indisponível") : "—"),
      h("dt", {}, "Encerramento"), h("dd", {}, d.ends_at ? d.ends_at.icao : "—"),
      h("dt", {}, "Pernoite"), h("dd", {}, d.overnight ? d.overnight.at.icao : "—"),
      h("dt", {}, "Próxima apresentação"), h("dd", {}, d.next_duty ? `${dm(d.next_duty.date)} ${d.next_duty.report_utc ? hhmmz(d.next_duty.report_utc) : ""} ${d.next_duty.at.icao}` : "—"),
      d.payroll ? [h("dt", {}, "Salário do mês"), h("dd", {}, d.payroll.needs_contract ? h("a", {href: "#/salarios"}, "cadastrar contrato")
        : h("a", {href: "#/salarios", title: "Estimativa pela escala do mês"}, `${brl(d.payroll.gross)} bruto`))] : null),
    d.duty && d.duty.source ? h("p", {class: "fine"}, cap(d.duty.source) + ".") : null);

  const repT = d.duty && d.duty.report_utc && Date.parse(d.duty.report_utc) > Date.now() ? {t: d.duty.report_utc, at: legs[0] && legs[0].adep}
    : d.next_duty && d.next_duty.report_utc ? {t: d.next_duty.report_utc, at: d.next_duty.at && d.next_duty.at.icao} : null;
  const limBox = h("div", {});
  put(main,
    h("header", {}, h("h1", {class: "welcome"}, `${greet()}, `, h("span", {class: "co"}, "comandante"), me.display_name ? ` ${me.display_name}` : "", "."),
      h("div", {class: "welcome-date"}, cap(d.label), d.base ? ` · base ${d.base.icao}` : "", " · ", liveClock()),
      h("p", {class: "lede"}, saud), repT ? countdown(repT.t, repT.at) : null),
    avisos.length ? h("div", {}, avisos) : null,
    validityNotes(),
    nf ? h("section", {}, boarding(nf, {whenLabel: nf.today ? (nf.starts_in_min > 0 ? `em ${hmLong(nf.starts_in_min)}` : "agora") : cap(dmy(nf.dep_utc).slice(0, 5)),
      actions: [(!nf.flight_id || nf.flight_state === "RESERVADO") ? h("button", {class: "btn ghost", onclick: () => manualDialog(legTarget(nf))}, icon("ok", 16), "Já fiz esse voo") : null,
        nf.flight_id ? h("a", {class: "btn", href: `#/voo/${nf.flight_id}`}, "Abrir Flight Center", icon("seta", 16))
        : h("button", {class: "btn", onclick: () => bookDialog(nf)}, icon("mais", 16), "Reservar este voo")]})) : null,
    h("div", {class: "cols-2"},
      h("div", {},
        h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Sua jornada de hoje"),
          h("a", {href: "#/escala", class: "muted", style: "font-size:13px"}, "ver escala")),
          legs.length ? journeyRail(d) : h("div", {class: "empty"}, icon("pernoite", 26), h("div", {}, d.kind === "FOLGA" ? "Folga programada." : d.kind === "RESERVA" ? "Dia de reserva." : "Sem voos programados para hoje."))),
        opsPanel(d)),
      h("div", {}, crewPanel(d.crew, d.stay_next), resumo, limBox, networkPanel([d.base && d.base.icao, nf && nf.adep, nf && nf.ades].filter((x, i, a) => x && a.indexOf(x) === i)))),
    h("p", {class: "fine"}, d.last_import ? `Escala importada do AeroScale em ${dt(d.last_import.imported_at)} (backup de ${d.last_import.source_created_at ? dt(d.last_import.source_created_at) : "data desconhecida"}). ` : "",
      DISCLAIMER));
  if (d.roster_legs) get(`/api/limites/${(d.date || today()).slice(0, 7)}`).then(li => { if (limBox.isConnected && li && li.available) put(limBox, limitsPanel(li)); }).catch(() => {});
});

function countdown(iso, at) {
  const el = h("div", {class: "countdown"});
  const tick = () => {
    const m = Math.round((Date.parse(iso) - Date.now()) / 60000);
    if (m <= 0) return put(el, icon("relogio", 14), h("b", {}, "Hora da apresentação"), at ? ` em ${at}` : "");
    const d = Math.floor(m / 1440), hh = Math.floor((m % 1440) / 60), mm = m % 60;
    put(el, icon("relogio", 14), "Apresentação em ", h("b", {}, d ? `${d}d ${hh}h` : hh ? `${hh} h ${String(mm).padStart(2, "0")} min` : `${mm} min`),
      at ? ` · ${at} · ${hhmmz(iso)}` : ` · ${hhmmz(iso)}`);
  };
  tick();
  const tm = setInterval(() => { if (!el.isConnected) clearInterval(tm); else tick(); }, 30000);
  return el;
}

/* ───────────────────────── escala em texto (exportador do AeroScale) ───────────────────────── */
function rosterTextDialog(date, bounds) {
  const ym = date.slice(0, 7), [y, m] = ym.split("-").map(Number), last = new Date(Date.UTC(y, m, 0)).toISOString().slice(0, 10);
  const MES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"];
  const st = {style: "companhia", times: false, period: "mes"};
  const per = h("select", {}, h("option", {value: "mes"}, `${cap(MES[m - 1])} de ${y}`),
    bounds && bounds.first ? h("option", {value: "tudo"}, `A escala inteira (${dmy(bounds.first)} a ${dmy(bounds.last)})`) : null,
    h("option", {value: "outro"}, "Outro período…"));
  const a = h("input", {type: "date", value: `${ym}-01`}), z = h("input", {type: "date", value: last});
  const custom = h("div", {class: "row", hidden: true}, field("De", a), field("Até", z));
  const out = h("textarea", {class: "mono", rows: 14, readonly: true, wrap: "off", style: "width:100%;resize:vertical;max-height:52vh"});
  const info = h("p", {class: "fine", style: "margin:6px 0 0"});
  const range = () => st.period === "tudo" ? [bounds.first, bounds.last] : st.period === "outro" ? [a.value, z.value] : [`${ym}-01`, last];
  const q = () => { const [i, f] = range(); return `inicio=${i}&fim=${f}&estilo=${st.style}&horarios=${st.times ? 1 : 0}`; };
  const refresh = async () => {
    try {
      const r = await get(`/api/escala/texto?${q()}`);
      out.value = r.text || "";
      put(info, r.empty ? r.message : `${dmy(r.start)} a ${dmy(r.end)} · ${r.legs} etapa(s). Dia que nenhuma escala cobre sai como SEM ESCALA.`);
    } catch (e) { out.value = ""; put(info, e.message || "Não deu para montar o texto."); }
  };
  const segS = h("div", {class: "seg"}, [["companhia", "Como a companhia"], ["classico", "Clássico"]].map(([v, l]) =>
    h("button", {type: "button", "aria-pressed": st.style === v, onclick: ev => { st.style = v; segS.querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", x === ev.currentTarget)); refresh(); }}, l)));
  const tm = h("input", {type: "checkbox"});
  tm.addEventListener("change", () => { st.times = tm.checked; refresh(); });
  per.addEventListener("change", () => { st.period = per.value; custom.hidden = per.value !== "outro"; refresh(); });
  [a, z].forEach(x => x.addEventListener("change", refresh));
  dialog("Escala em texto", "Como circula entre tripulantes, um dia por linha: o mesmo exportador do AeroScale, com a escala que está na sua carreira agora (trocas e cortes incluídos).",
    h("div", {}, field("Período", per), custom, h("div", {class: "tools", style: "margin:4px 0 10px;flex-wrap:wrap"}, segS,
      h("label", {class: "ck", style: "margin:0"}, tm, "Incluir horários (hora local)")), out, info),
    [closeBtn,
     {label: "Agenda (.ics)", cls: "ghost", keep: true, fn: () => { const [i0, f0] = range(); const l = h("a", {href: `/api/escala/agenda.ics?inicio=${i0}&fim=${f0}`, download: ""}); document.body.append(l); l.click(); l.remove();
       toast("Abra o arquivo .ics no Google Agenda, Outlook ou no calendário do celular"); }},
     {label: "Baixar .txt", cls: "ghost", keep: true, fn: () => { const l = h("a", {href: `/api/escala/texto.txt?${q()}`, download: ""}); document.body.append(l); l.click(); l.remove(); }},
     {label: "Copiar", keep: true, fn: async () => {
       try { await navigator.clipboard.writeText(out.value); } catch (e) { out.select(); document.execCommand("copy"); }
       toast("Escala copiada");
     }}]);
  refresh();
}

/* ───────────────────────── escala ───────────────────────── */
const roster = {mode: "dia", date: today(), bounds: null};
view("escala", async (main, arg) => {
  if (arg) roster.date = arg;
  if (!roster.bounds) roster.bounds = await get("/api/roster/bounds").catch(() => ({}));
  const b = roster.bounds || {};
  const addMonths = (d, n) => { const [y, m] = d.split("-").map(Number); const t = new Date(Date.UTC(y, m - 1 + n, 1)); return t.toISOString().slice(0, 10); };
  // a data vai no endereço (#/escala/AAAA-MM-DD): assim o seletor funciona mesmo quando você chegou por um link de dia
  const goDate = d => { roster.date = d; const want = `#/escala/${d}`; if (location.hash !== want) location.hash = want; else render(); };
  const jump = (n) => goDate(roster.mode === "mes" ? addMonths(roster.date, n) : addDays(roster.date, roster.mode === "dia" ? n : 7 * n));
  const MES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"];
  const lbl = roster.mode === "dia" ? dmy(roster.date) : roster.mode === "mes" ? `${cap(MES[+roster.date.slice(5, 7) - 1])} ${roster.date.slice(0, 4)}`
    : `${dm(roster.date)} – ${dm(addDays(roster.date, 6))}`;
  const stepper = h("div", {class: "stepper"},
    h("button", {onclick: () => jump(-1), "aria-label": "Anterior"}, icon("seta", 16, "flip")),
    h("span", {class: "lbl"}, lbl),
    h("button", {onclick: () => jump(1), "aria-label": "Próximo"}, icon("seta", 16)));
  stepper.querySelector(".flip").style.transform = "rotate(180deg)";
  const seg = h("div", {class: "seg"}, [["dia", "Dia"], ["semana", "Semana"], ["mes", "Mês"], ["tripulacao", "Tripulação"]].map(([m, l]) =>
    h("button", {"aria-pressed": roster.mode === m, onclick: () => { roster.mode = m; render(); }}, l)));
  const headEl = h("div", {class: "head"}, h("div", {}, h("h1", {}, "Escala"),
    h("div", {class: "sub"}, "Gerada pelo motor do AeroScale com o RPL do CGNA (ou importada). Aqui você reserva a perna e escolhe a aeronave.")),
    h("div", {class: "tools"}, h("a", {class: "btn sm", href: "#/gerar"}, icon("mais", 15), "Gerar escala"),
      h("button", {class: "btn ghost sm", onclick: () => rosterTextDialog(roster.date, b)}, "Texto"),
      h("button", {class: "btn ghost sm", onclick: () => goDate(today())}, "Hoje"), stepper, seg));
  put(main, headEl, skeleton("h"));
  if (roster.mode === "mes") {
    const ym = roster.date.slice(0, 7), [y, m] = ym.split("-").map(Number), last = new Date(Date.UTC(y, m, 0)).toISOString().slice(0, 10);
    const [mo, mp, li] = await Promise.all([get(`/api/month/${ym}`), get(`/api/mapa?inicio=${ym}-01&fim=${last}`), get(`/api/limites/${ym}`).catch(() => null)]);
    put(main, headEl, monthView(mo, mp, li));
    stagger(main);
    return;
  }
  const url = roster.mode === "dia" ? `/api/day/${roster.date}` : roster.mode === "semana" ? `/api/week/${roster.date}` : "/api/crew";
  const [data, fleet] = await Promise.all([get(url), get("/api/aircraft")]);
  const body = roster.mode === "dia" ? dayView(data, fleet) : roster.mode === "semana" ? weekView(data, fleet) : crewView(data);
  if (roster.mode === "tripulacao") headEl.querySelector(".stepper").style.display = "none";
  put(main, headEl, b.first ? h("p", {class: "fine", style: "margin-top:-12px"}, `Escala disponível de ${dmy(b.first)} a ${dmy(b.last)}.`) : null, body);
  stagger(main);
});

/* ───────────────────────── mês, mapa e limites ───────────────────────── */
const KIND_LABEL = {VOO: "voo", FOLGA: "folga", RESERVA: "reserva", SOBREAVISO: "sobreaviso", LIVRE: "livre"};
function monthView(mo, mp, li) {
  const t = mo.totals;
  const figs = h("div", {class: "figs"}, [
    ["Dias de voo", t.flight_days], ["Etapas", t.legs], ["Horas de voo", (t.block_min / 60).toFixed(1)], ["Folgas", t.off],
    ["Pernoites fora", t.nights_out], ["Aeroportos", t.airports]].map(([l, v]) => h("div", {class: "fig"}, h("b", {}, String(v)), h("span", {}, l))));
  const cal = h("div", {class: "cal"}, ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"].map(w => h("div", {class: "cal-wd"}, w)),
    Array.from({length: mo.first_weekday}, () => h("div", {class: "cal-pad"})),
    mo.days.map(d => h("a", {class: `cal-day k-${d.kind.toLowerCase()}${d.today ? " today" : ""}${d.past ? " past" : ""}`, href: `#/escala/${d.date}`,
      onclick: () => { roster.mode = "dia"; }, title: d.route.length ? d.route.join(" → ") : cap(KIND_LABEL[d.kind] || "")},
      h("div", {class: "cal-top"}, h("span", {class: "cal-n"}, d.day), d.swap ? h("span", {class: "cal-dot hold", title: "troca pedida"}) : null,
        d.flown && d.flown === d.legs ? h("span", {class: "cal-dot go", title: "voado"}) : null),
      d.kind === "VOO" ? [h("div", {class: "cal-mob"}, `${d.legs}×`), h("div", {class: "cal-route"}, d.route.length > 4 ? `${d.route[0]} … ${d.route[d.route.length - 1]}` : d.route.join("·")),
        h("div", {class: "cal-meta"}, `${d.legs} etapa${d.legs > 1 ? "s" : ""}${d.block_min ? " · " + hm(d.block_min) : ""}`),
        d.overnight_at ? h("div", {class: "cal-night"}, icon("pernoite", 12), d.overnight_at) : null]
        : h("div", {class: "cal-kind"}, d.kind === "RESERVA" && d.reserve_at ? `reserva ${d.reserve_at}` : (KIND_LABEL[d.kind] || "")))));
  return h("div", {},
    figs,
    h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Calendário"), h("div", {class: "tools cal-legend"},
      [["voo", "Voo"], ["folga", "Folga"], ["reserva", "Reserva"], ["sobreaviso", "Sobreaviso"]].map(([k, l]) => h("span", {class: "lg k-" + k}, l)))),
      cal, h("p", {class: "fine"}, "Toque num dia para abrir. Ponto verde: dia voado; amarelo: troca pedida.")),
    h("div", {class: "cols-2"},
      h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Mapa do mês"), h("span", {class: "muted", style: "font-size:12.5px"},
        `${mp.airports.length} aeroportos · ${mp.segments.length} trechos`)), routeMapSvg(mp),
        mp.missing && mp.missing.length ? h("p", {class: "fine"}, `Sem coordenadas no catálogo: ${mp.missing.join(", ")}.`) : null),
      limitsPanel(li)));
}
function routeMapSvg(mp, opt = {}) {
  const W = opt.w || 640, H = opt.h || 420, pad = 34;
  const aps = mp.airports || [];
  if (!aps.length) return h("div", {class: "empty"}, icon("vazio", 26), h("div", {}, "Sem trechos no período."));
  const lat = aps.map(a => a.lat), lon = aps.map(a => a.lon);
  let [la0, la1, lo0, lo1] = [Math.min(...lat), Math.max(...lat), Math.min(...lon), Math.max(...lon)];
  const span = Math.max(la1 - la0, (lo1 - lo0) * H / W, 4);
  const cla = (la0 + la1) / 2, clo = (lo0 + lo1) / 2, k = Math.cos(cla * Math.PI / 180);
  const sc = Math.min((H - 2 * pad) / span, (W - 2 * pad) / Math.max((lo1 - lo0) * k, span * W / H * k, 4));
  const X = lo => W / 2 + (lo - clo) * k * sc, Y = la => H / 2 - (la - cla) * sc;
  const pos = Object.fromEntries(aps.map(a => [a.icao, [X(a.lon), Y(a.lat)]]));
  const maxN = Math.max(...mp.segments.map(s => s.n), 1), maxV = Math.max(...aps.map(a => a.visits), 1);
  const g = svg("svg", {class: "rmap", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Mapa dos trechos"});
  for (let i = 1; i < 6; i++) { g.append(svg("line", {class: "grid", x1: 0, x2: W, y1: H * i / 6, y2: H * i / 6})); g.append(svg("line", {class: "grid", y1: 0, y2: H, x1: W * i / 6, x2: W * i / 6})); }
  mp.segments.forEach(sg => {
    const [x1, y1] = pos[sg.a], [x2, y2] = pos[sg.b], mx = (x1 + x2) / 2, my = (y1 + y2) / 2, dx = x2 - x1, dy = y2 - y1;
    const c = [mx - dy * 0.18, my + dx * 0.18];
    const p = svg("path", {class: "seg", d: `M${x1} ${y1} Q${c[0]} ${c[1]} ${x2} ${y2}`, "stroke-width": (1 + 3 * sg.n / maxN).toFixed(2)});
    p.append(svg("title", {}, `${sg.a} ↔ ${sg.b} · ${sg.n}×`));
    g.append(p);
  });
  const placed = [];
  [...aps].sort((a, b) => (b.base - a.base) || (b.visits - a.visits)).forEach(a => {
    const [x, y] = pos[a.icao], r = 3 + 5 * Math.sqrt(a.visits / maxV);
    const left = placed.some(([px, py]) => Math.abs(px - x) < 46 && Math.abs(py - y) < 12);   // rótulo colado em outro: vai para a esquerda
    placed.push([x, y]);
    const c = svg("circle", {class: "ap" + (a.base ? " base" : ""), cx: x, cy: y, r});
    c.append(svg("title", {}, `${a.icao}${a.city ? " · " + a.city : ""} · ${a.visits} pouso(s)/partida(s)`));
    g.append(c);
    const tx = svg("text", {class: "lbl" + (a.base ? " base" : ""), x: left ? x - r - 3 : x + r + 3, y: y + 4, "text-anchor": left ? "end" : "start"});
    tx.textContent = a.iata || a.icao;
    g.append(tx);
  });
  return h("div", {class: "rmap-wrap"}, g);
}
function gauge(label, v, limit, unit, rule, invert) {
  const pct = limit ? Math.min(100, Math.round(100 * v / limit)) : 0;
  const cls = invert ? (v >= limit ? "go" : "stop") : pct >= 95 ? "stop" : pct >= 80 ? "hold" : "go";
  return h("div", {class: "gauge", title: rule || ""}, h("div", {class: "gauge-top"}, h("span", {}, label),
    h("b", {class: "mono"}, `${typeof v === "number" ? (Number.isInteger(v) ? v : v.toFixed(1)) : "—"}${unit} / ${limit}${unit}`)),
    h("div", {class: "gauge-bar " + cls}, h("span", {style: `width:${invert ? Math.min(100, Math.round(100 * v / Math.max(limit, 1))) : pct}%`})));
}
function limitsPanel(li) {
  if (!li || !li.available) return h("div", {class: "panel"}, h("h2", {}, "Limites do mês"), h("p", {class: "muted"}, (li && li.reason) || "Sem escala no mês."));
  return h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Limites do mês"), li.errors.length ? chip(`${li.errors.length} violação(ões)`, "stop") : chip("dentro da lei", "go")),
    gauge("Horas de voo", li.block.h || 0, li.block.limit, " h", li.block.rule),
    gauge("Horas de jornada", li.duty.h || 0, li.duty.limit, " h", li.duty.rule),
    li.off.required != null ? gauge("Folgas (mínimo)", li.off.n || 0, li.off.required, "", li.off.rule, true) : null,
    h("dl", {class: "kv", style: "margin-top:12px"}, h("dt", {}, "Madrugadas"), h("dd", {}, li.madrugadas ?? "—"),
      h("dt", {}, "Pernoites fora"), h("dd", {}, li.nights_out ?? "—"), h("dt", {}, "Mais dias seguidos"), h("dd", {}, li.max_consecutive ?? "—"),
      h("dt", {}, "Folga de fim de semana"), h("dd", {}, li.off.weekend_pair ? "sim" : "não")),
    li.errors.map(e => note("stop", "alerta", e)),
    h("p", {class: "fine"}, `${li.source}: ${li.block.rule} (voo), ${li.duty.rule} (jornada), ${li.off.rule} (folgas).`));
}

function dayView(d, fleet) {
  const legs = d.legs.filter(l => l.is_flight);
  const chips = [d.day_off ? chip("Folga programada", "cool") : null,
    d.reserve ? chip(`Reserva em ${d.reserve.at.icao} (${dm(d.reserve.from)}–${dm(d.reserve.to)})`, "hold", d.reserve.note || "") : null,
    d.reserve && d.reserve.activated_today ? chip("Reserva acionada hoje", "or") : null];
  return h("div", {},
    (d.tz_unknown || []).length ? note("", "info", `Sem fuso conhecido para ${d.tz_unknown.join(", ")}: nestes aeroportos o LineOps mostra só UTC e usa UTC para datar a perna, igual ao AeroScale.`) : null,
    (d.diagnostics || []).map(x => note(x.level === "warning" ? "hold" : "", x.level === "warning" ? "alerta" : "info", h("b", {}, "AeroScale: "), x.message)),
    h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, cap(d.label)), h("div", {class: "tools"}, chips)),
      legs.length ? journeyRail(d) : h("div", {class: "empty"}, icon("vazio", 26), h("div", {},
        d.kind === "FOLGA" ? "Folga programada." : d.kind === "RESERVA" ? "Dia de reserva, sem voos atribuídos." : "Nenhuma perna neste dia.")),
      legs.filter(l => l.can_book || l.flight_state === "RESERVADO").length ? h("div", {style: "margin-top:16px;display:flex;gap:8px;flex-wrap:wrap"},
        legs.filter(l => l.can_book).map(l => h("button", {class: "btn ghost sm", onclick: () => bookDialog(l, fleet)}, icon("mais", 15), `Reservar ${l.flight_number || l.adep + "→" + l.ades}`)),
        legs.filter(l => l.can_book || l.flight_state === "RESERVADO").map(l => h("button", {class: "btn ghost sm", onclick: () => manualDialog(legTarget(l), fleet)},
          icon("ok", 15), `Já fiz o ${l.flight_number || l.adep + "→" + l.ades}`))) : null,
      swapRow(d, legs), overnightRow(d, legs)),
    swapDayPanel(d), contDayPanel(d),
    d.reserve ? h("div", {class: "panel"}, h("h2", {}, "Reserva"), h("p", {class: "muted"}, d.reserve.note || "Sem observações do AeroScale."),
      h("dl", {class: "kv"}, h("dt", {}, "Período"), h("dd", {}, `${dmy(d.reserve.from)} → ${dmy(d.reserve.to)} (${d.reserve.days} dias)`),
        h("dt", {}, "Local"), h("dd", {}, `${d.reserve.at.icao}${d.reserve.location_kind ? " · " + d.reserve.location_kind : ""}`),
        h("dt", {}, "Situação"), h("dd", {}, d.reserve.status || "—"),
        h("dt", {}, "Acionamento"), h("dd", {}, d.reserve.act_start ? `${dmy(d.reserve.act_start)}${d.reserve.act_end && d.reserve.act_end !== d.reserve.act_start ? " → " + dmy(d.reserve.act_end) : ""}` : "não acionada"))) : null,
    (d.schedule_notes || []).length ? h("details", {class: "panel"}, h("summary", {}, h("b", {}, "Observações do AeroScale sobre esta escala"), ` (${d.schedule_notes.length})`),
      h("div", {style: "margin-top:12px"}, d.schedule_notes.map(x => note(x.level === "warning" ? "hold" : "", x.level === "warning" ? "alerta" : "info", x.message)))) : null);
}

function weekView(w, fleet) {
  const t = today();
  return h("div", {class: "week"}, w.days.map(day => {
    const legs = day.legs.filter(l => l.is_flight);
    const el = h("article", {class: "wd" + (day.date === t ? " today" : "")});
    const inner = h("div", {class: "inner"});
    const btn = h("button", {"aria-expanded": "false", onclick: () => {
      const open = expand(el);
      if (open && !inner.childElementCount) {
        put(inner, legs.length ? legs.map(l => stripOf(l, [l.flight_state ? stateChip(l.flight_state) : h("button", {class: "btn ghost sm", onclick: e => { e.preventDefault(); bookDialog(l, fleet); }}, "Reservar"),
          l.flight_id ? icon("seta", 16) : null], l.flight_id ? `#/voo/${l.flight_id}` : null))
          : h("p", {class: "muted", style: "margin:4px 0 0"}, day.kind === "FOLGA" ? "Folga programada." : day.kind === "RESERVA" ? `Reserva em ${day.reserve_at ? day.reserve_at.icao : "—"}.` : "Sem pernas."));
      }
    }},
      h("div", {class: "dnum"}, day.date.slice(8, 10), h("small", {}, day.weekday.replace("-feira", ""))),
      h("div", {}, day.kind === "VOO" ? chip(`${day.flights} voo${day.flights > 1 ? "s" : ""}`, "or") :
        day.kind === "FOLGA" ? chip("Folga", "cool") : day.kind === "RESERVA" ? chip("Reserva", "hold") : chip("Livre", "")),
      h("div", {class: "route"}, day.kind === "VOO" ? [day.from.icao, h("span", {class: "sep"}, "→"), day.to.icao,
        h("span", {class: "muted", style: "font-size:13px;margin-left:10px"}, `${hhmm(legs[0].dep_utc)}–${hhmm(legs[legs.length - 1].arr_utc)}Z`)]
        : day.reserve_at ? h("span", {class: "muted"}, placeName(day.reserve_at)) : h("span", {class: "muted"}, "—")),
      h("div", {class: "tools"}, day.block_min ? h("span", {class: "muted t", style: "font-size:13px"}, hm(day.block_min)) : null,
        day.booked ? chip(`${day.booked} reservado${day.booked > 1 ? "s" : ""}`, "cool") : null, icon("seta", 16, "caret")));
    el.append(btn, h("div", {class: "body"}, h("div", {}, inner)));
    return el;
  }));
}

function crewView(r) {
  if (!r.available) return h("div", {class: "empty"}, icon("pireps", 26), h("div", {}, r.reason || "Sem dados de tripulação."));
  const s = r.summary, mes = Object.entries(s.months || {});
  const figs = h("div", {class: "figs"}, [
    ["Jornadas", s.duties, v => Math.round(v)],
    ["Horas de voo", s.block_h, v => v.toFixed(1)],
    ["Horas de jornada", s.duty_h, v => v.toFixed(1)],
    ["Madrugadas", s.madrugadas, v => Math.round(v)],
    ["Pernoites fora", s.nights_out, v => Math.round(v)],
    ["Dias seguidos", s.max_consecutive, v => Math.round(v)]].map(([l, v, f]) => {
      const b = h("b", {}, v == null ? "—" : f(v));
      if (typeof v === "number") countUp(b, v, f);
      return h("div", {class: "fig"}, b, h("span", {}, l));
    }));
  const viol = (r.violations || []).map(v => note(v.level === "error" ? "stop" : "hold", "alerta",
    h("b", {}, v.date ? dmy(v.date) + ": " : ""), v.message || v.code));
  const meses = mes.map(([m, x]) => h("tr", {}, h("td", {}, m), h("td", {class: "n"}, x.block_h.toFixed(1)), h("td", {class: "n"}, x.duty_h.toFixed(1)),
    h("td", {class: "n"}, `${x.off} / ${x.required_off}`), h("td", {}, x.weekend_pair ? chip("fim de semana", "go") : chip("sem par de fim de semana", "hold"))));
  return h("div", {},
    note("", "info", h("b", {}, "Regras do AeroScale: "), s.mode_label || "", r.base ? ` · base ${r.base.icao}` : "",
      r.profile && r.profile.label ? ` · perfil ${r.profile.label}` : ""),
    viol.length ? viol : note("go", "ok", "Nenhuma violação de jornada, repouso ou folga no período."),
    figs,
    mes.length ? h("div", {class: "panel"}, h("h2", {}, "Por mês"), h("div", {class: "hr"}), h("div", {class: "tbl"}, h("table", {},
      h("thead", {}, h("tr", {}, ["Mês", "Voo (h)", "Jornada (h)", "Folgas", "Fim de semana"].map((t, k) => h("th", {class: k > 0 && k < 4 ? "n" : ""}, t)))),
      h("tbody", {}, meses)))) : null,
    h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Jornadas"), h("span", {class: "muted", style: "font-size:13px"}, `${r.duties.length} no período`)),
      h("div", {class: "tbl"}, h("table", {},
        h("thead", {}, h("tr", {}, ["Dia", "Apresentação", "Encerra", "Trecho", "Pernas", "Jornada", "Voo", "Limite", "Repouso antes", "Pernoite"].map((t, k) =>
          h("th", {class: k > 3 ? "n" : ""}, t)))),
        h("tbody", {}, r.duties.map(d => h("tr", {title: d.limit_source || ""},
          h("td", {}, dmy(d.date)), h("td", {class: "t"}, d.report_local || hhmmz(d.report_utc)), h("td", {class: "t"}, d.release_local || hhmmz(d.release_utc)),
          h("td", {class: "t"}, `${d.start}→${d.end}`), h("td", {class: "n"}, d.legs),
          h("td", {class: "n" + (d.fdp_limit_h && d.duty_h > d.fdp_limit_h ? " over" : "")}, d.duty_h.toFixed(1)),
          h("td", {class: "n"}, d.block_h.toFixed(1)), h("td", {class: "n muted"}, d.fdp_limit_h ? d.fdp_limit_h.toFixed(1) : "—"),
          h("td", {class: "n"}, d.rest_before_h == null ? "—" : d.rest_before_h.toFixed(1) + (d.rest_required_h && d.rest_before_h < d.rest_required_h ? "!" : "")),
          h("td", {}, d.night_at ? chip(d.night_at, d.madrugada ? "hold" : "") : (d.madrugada ? chip("madrugada", "hold") : "—")))))))),
    h("p", {class: "fine"}, r.source, ". Período de ", dmy(r.period.start), " a ", dmy(r.period.end), "."));
}

/* ───────────────────────── backups ───────────────────────── */
function backupPanel(bi) {
  const items = bi.items || [];
  const kb = n => n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB";
  const tag = b => b.label ? (b.label.startsWith("antes-da-") ? chip("antes da " + b.label.slice(9), "cool", "Feito sozinho ao abrir uma versão nova")
    : b.label === "antes-de-restaurar" ? chip("antes de restaurar", "hold") : b.label === "enviado" ? chip("enviado", "") : chip(b.label, "")) : null;
  const file = h("input", {type: "file", accept: ".zip"});
  const restoreBtn = b => h("button", {class: "btn ghost sm", onclick: () => dialog("Restaurar este backup?",
    `${b.name}${b.created_at ? " · de " + dt(b.created_at) : ""}${b.version ? " · LineOps " + b.version : ""}`,
    h("div", {}, note("hold", "alerta", "A restauração acontece na próxima vez que a LineOps abrir, antes de qualquer banco ser aberto. ",
      "Antes, o estado de agora vira um backup “antes de restaurar”, então dá para voltar atrás."),
      h("p", {class: "muted"}, "Depois de confirmar, feche a LineOps (Sair na bandeja, ou Ctrl+C na janela do servidor) e abra de novo.")),
    [closeBtn, {label: "Agendar restauração", cls: "", fn: async () => { if (await act(() => post(`/api/backups/${encodeURIComponent(b.name)}/restaurar`), "Restauração agendada")) render(); }}])}, "Restaurar");
  return h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Backups da LineOps"), h("span", {class: "muted", style: "font-size:12.5px"}, `${items.length} guardado(s)`)),
    h("p", {class: "muted"}, "Cópia da LineOps inteira: piloto, todas as carreiras (escala, voos, diário, mensagens da equipe, pernoites, trocas, ",
      "acompanhamento da IVAO em andamento), motor de escalas, logos e RPL baixado. ",
      `Hoje são ${bi.count} arquivo(s). Um backup sai sozinho por dia e sempre que você abre uma versão nova, antes de mexer em qualquer coisa.`),
    bi.pending_restore ? note("hold", "alerta", h("b", {}, "Restauração agendada: "), bi.pending_restore, ". Feche e abra a LineOps para aplicar. ",
      h("button", {class: "btn ghost sm", onclick: async () => { if (await act(() => api("DELETE", "/api/backups/restaurar"), "Restauração cancelada")) render(); }}, "Cancelar")) : null,
    bi.last_restore ? (bi.last_restore.ok ? note("go", "ok", `Backup ${bi.last_restore.zip} restaurado ao abrir.`, bi.last_restore.safety ? ` O estado anterior ficou em ${bi.last_restore.safety}.` : "")
      : note("stop", "alerta", `A restauração de ${bi.last_restore.zip || "um backup"} não deu certo: ${bi.last_restore.error}. Nada foi trocado.`)) : null,
    h("div", {class: "tools", style: "flex-wrap:wrap;gap:8px"},
      h("button", {class: "btn", onclick: async () => { if (await act(() => post("/api/backup"), "Backup criado")) render(); }}, "Criar backup agora"),
      h("label", {class: "btn ghost", style: "cursor:pointer"}, file, "Enviar um backup (.zip)")),
    items.length ? h("div", {class: "tbl", style: "margin-top:12px"}, h("table", {}, h("tbody", {}, items.slice(0, 12).map(b =>
      h("tr", {}, h("td", {}, h("div", {class: "t", style: "font-size:12.5px"}, b.name), h("small", {class: "muted"},
          [b.created_at ? dt(b.created_at) : null, b.version ? "v" + b.version : null, b.messages != null ? `${b.messages} mensagens` : null,
           b.flights != null ? `${b.flights} voo(s) no diário` : null, b.ok ? null : "arquivo com problema"].filter(Boolean).join(" · "))),
        h("td", {}, tag(b)), h("td", {class: "n"}, kb(b.size)),
        h("td", {}, h("a", {href: "/api/backups/" + encodeURIComponent(b.name)}, "baixar")),
        h("td", {}, b.ok ? restoreBtn(b) : null)))))) : null,
    h("p", {class: "fine"}, `Pasta: ${bi.folder}. Guarde uma cópia fora deste computador (pen drive, nuvem). `,
      "Pelo .bat: restaurar.bat (arraste o .zip em cima, ou dois cliques para o mais recente)."),
    (() => { file.style.display = "none"; file.onchange = async () => {
      if (!file.files[0]) return;
      const r = await act(() => api("POST", "/api/backups/enviar", undefined, file.files[0]), "Backup conferido e guardado");
      file.value = "";
      if (r) render();
    }; return null; })());
}

/* ───────────────────────── troca de escala ───────────────────────── */
const SWAP_STATE = {PENDENTE: ["aguardando resposta", "hold"], APROVADO: ["aprovada", "go"], NEGADO: ["negada", ""],
                    CANCELADO: ["cancelada", ""], EXPIRADO: ["sem efeito", ""]};
const SWAP_DONE = ["EM_VOO", "PIREP_RECEBIDO", "APROVADO", "REJEITADO"];
function swapChip(sw) {
  if (!sw) return null;
  if (sw.came_in) return chip("entrou por troca", "go", sw.label);
  if (sw.state === "PENDENTE") return chip(`troca pedida · ${sw.chance}%`, "hold", sw.label);
  if (sw.state === "NEGADO") return chip("troca negada", "", sw.answer || "");
  return null;
}
function swapRow(d, legs) {
  const now = Date.now();
  const can = legs.filter(l => !SWAP_DONE.includes(l.flight_state) && Date.parse(l.dep_utc) > now
    && !((d.swaps || {})[l.id] && d.swaps[l.id].state === "PENDENTE"));
  if (!can.length) return null;
  return h("div", {class: "swap-row"}, h("span", {class: "muted", style: "font-size:13px"}, "Troca de escala:"),
    can.map(l => h("button", {class: "btn ghost sm", onclick: () => swapDialog(l)}, icon("troca", 15), `Pedir troca do ${l.flight_number || l.adep + "→" + l.ades}`)));
}
/* aviso de pernoite no Discord (só para o usuário cmdt079) */
function overnightRow(d, legs) {
  if (!(me && me.features && me.features.pernoite_discord)) return null;
  const fl = legs.filter(l => l.is_flight !== false && l.activity !== "SOBREAVISO");
  if (!fl.length) return null;
  const posts = d.overnight_posts || {}, lastId = fl[fl.length - 1].id;
  return h("div", {class: "swap-row pernoite-row"}, h("span", {class: "muted", style: "font-size:13px"}, "Discord:"),
    fl.map(l => {
      const st = posts[String(l.id)], cb = h("input", {type: "checkbox", checked: !!st && st.state === "ENVIADO"});
      const det = d.overnight && (d.overnight.kind === "marcado" ? d.overnight.detected : d.overnight);
      const auto = det && l.id === lastId;
      cb.addEventListener("change", async () => {
        cb.disabled = true;
        const r = await act(() => post(`/api/pernoite/${l.id}/${cb.checked ? "marcar" : "desmarcar"}`),
          cb.checked ? "Pernoite enviado para o Discord" : "Pernoite desmarcado");
        if (!r) cb.checked = !cb.checked;
        else if (r.note) toast(r.note, true);
        else if (cb.checked && !r.detected) toast("Enviado. Pela escala, esse voo não termina em pernoite: o pernoite do dia passa a ser no destino dele.");
        cb.disabled = false;
        if (r) render();                                   // o pernoite, o hotel e o resumo do dia seguem o voo marcado
      });
      return h("label", {class: "ck pernoite-ck" + (auto ? " auto" : ""), title: st && st.state === "ERRO" ? `Último envio falhou: ${st.error}` : (auto ? "Pela escala, este voo termina em pernoite" : "")},
        cb, h("b", {}, "ESSE VOO É PERNOITE"), ` ${l.flight_number || l.adep + "→" + l.ades}${auto ? " · pernoite na escala" : ""}${st && st.state === "ERRO" ? " · falhou" : ""}`);
    }));
}
function swapDayPanel(d) {
  const seen = new Set(), items = Object.values(d.swaps || {}).filter(x => !seen.has(x.id) && seen.add(x.id));
  if (!items.length) return null;
  return h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Pedidos de troca deste dia"), h("a", {href: "#/carreira", class: "muted", style: "font-size:13px"}, "todos os pedidos")),
    items.map(x => swapLine(x)));
}
function swapLine(x) {
  const [lab, cls] = SWAP_STATE[x.state] || [x.state, ""];
  return h("div", {class: "swap-line"},
    h("div", {}, h("b", {}, `#${x.id} · ${x.label}`),
      h("small", {}, x.state === "PENDENTE" ? `Chance estimada ${x.chance}% · resposta até ${dt(x.decide_at)}` : (x.answer || ""))),
    h("div", {class: "tools"}, chip(lab, cls), x.state === "PENDENTE" ? h("button", {class: "btn ghost sm", onclick: async () => {
      if (await act(() => post(`/api/troca/${x.id}/cancelar`), "Pedido cancelado")) render();
    }}, "Cancelar") : null));
}
function swapFactors(ch, factors) {
  return h("div", {class: "swap-fx"},
    h("div", {class: "swap-chance"}, h("b", {}, `${ch}%`), h("span", {}, "chance estimada de aprovação")),
    h("ul", {}, factors.map(f => h("li", {class: f.base ? "base" : f.delta > 0 ? "up" : f.delta < 0 ? "down" : ""},
      h("span", {}, f.label), h("b", {class: "mono"}, f.base ? String(f.delta) : (f.delta > 0 ? "+" : "") + f.delta)))));
}
async function swapDialog(leg) {
  const o = await act(() => get(`/api/troca/opcoes/${leg.id}`));
  if (!o) return;
  if (o.blocked) { dialog("Pedir troca de escala", o.leg_label, note("hold", "alerta", o.blocked), [closeBtn]); return; }
  let pick = null;
  const fx = h("div", {}, h("p", {class: "muted", style: "font-size:13px"}, "Escolha uma opção para ver a chance e o que pesa nela."));
  const reason = h("textarea", {rows: 2, maxlength: 300, placeholder: "Opcional. Vai junto no pedido (ex.: compromisso pessoal, preferência de horário)."});
  const opts = [];
  const choose = (el, p) => { opts.forEach(x => x.classList.toggle("sel", x === el)); pick = p; put(fx, swapFactors(p.chance, p.factors), p.rules_note ? note("", "info", p.rules_note) : null); };
  const legTxt = x => h("span", {class: "swap-leg" + (x.same ? " same" : "")}, h("b", {}, x.flight_number), ` ${x.adep}→${x.ades} · ${x.dep_label}`,
    x.same ? h("small", {}, " · mantém") : null, x.activity === "EXTRA" ? chip("EXTRA", "cool", "Vai como passageiro, sem voar") : null,
    x.origin === "PROJETADO" && !x.same ? chip("PROJETADO", "hold", "Sem RPL publicado para o dia: projeção da semana mais nova, como no AeroScale") : null);
  const optEl = (p, kind, title, sub) => {
    const el = h("button", {type: "button", class: "swap-opt" + (p.ok ? "" : " off"), disabled: !p.ok, onclick: () => choose(el, {...p, kind})},
      h("div", {class: "swap-opt-main"}, title, sub ? h("small", {}, sub) : null), p.ok ? h("span", {class: "swap-pct"}, `${p.chance}%`) : null,
      p.ok ? null : h("small", {class: "swap-why"}, p.why));
    if (p.ok) opts.push(el);
    return el;
  };
  const tr = o.troca.options, ok = tr.filter(x => x.ok), bad = tr.filter(x => !x.ok);
  const trocaEl = h("div", {class: "swap-group"}, h("h3", {}, "Trocar por outro voo no mesmo trecho"),
    ok.length ? ok.map(p => optEl(p, "TROCA", h("div", {}, p.block ? h("small", {class: "muted"}, `Troca o bloco ${p.replaces.map(x => x.flight_number).join(" + ")} (volta ao mesmo aeroporto):`) : null,
      p.legs.map(legTxt)))) : h("p", {class: "muted"}, o.troca.note || "Nenhuma troca cabe na sua escala neste dia."),
    bad.length ? h("details", {class: "swap-bad"}, h("summary", {}, `${bad.length} voo(s) do RPL que não cabem na sua escala`),
      bad.map(p => optEl(p, "TROCA", h("div", {}, p.legs.map(legTxt))))) : null);
  const destEl = h("div", {class: "swap-group"}, h("h3", {}, "Escolher o destino"), h("p", {class: "muted", style: "font-size:13px"}, "Procurando destinos no RPL…"));
  const loadDest = async () => {
    const d = await get(`/api/troca/destinos/${leg.id}`).catch(e => ({items: [], why: e.message}));
    if (d.blocked) { put(destEl, h("h3", {}, "Escolher o destino"), h("p", {class: "muted"}, d.blocked)); return; }
    d.items = d.items || [];
    const sel = h("select", {}, h("option", {value: ""}, `Escolha (${d.items.length} destino${d.items.length > 1 ? "s" : ""})`),
      d.items.map(x => h("option", {value: x.icao}, `${x.icao} · ${x.city || x.name || ""} · ${x.more ? x.options + "+" : x.options} opç${x.options > 1 || x.more ? "ões" : "ão"}${x.overnight ? " · com pernoite" : ""}`)));
    const list = h("div");
    const showDest = async icao => {
      opts.splice(0, opts.length, ...opts.filter(x => !list.contains(x)));
      if (pick && pick.dest) { pick = null; put(fx, h("p", {class: "muted", style: "font-size:13px"}, "Escolha uma opção para ver a chance e o que pesa nela.")); }
      if (!icao) return put(list);
      put(list, h("div", {class: "skel s"}));
      const r = await act(() => get(`/api/troca/opcoes/${leg.id}?destino=${encodeURIComponent(icao)}`));
      if (!r) return put(list);
      const good = r.troca.options.filter(x => x.ok), nope = r.troca.options.filter(x => !x.ok);
      const D = r.dest || icao;
      const row = p => h("div", {}, h("small", {class: "muted"}, `No lugar de ${p.replaces.map(x => x.flight_number).join(" + ")}`
        + (p.overnight_at ? ` · ${p.nights > 1 ? p.nights + " pernoites" : "pernoite"} em ${p.overnight_at}` : "") + ":"), p.legs.map(legTxt));
      const more = good.slice(8);
      put(list, good.length ? good.slice(0, 8).map(p => optEl({...p, dest: D}, "TROCA", row(p))) : h("p", {class: "muted"}, r.troca.note || "Nenhuma opção cabe."),
        more.length ? h("details", {class: "swap-bad"}, h("summary", {}, `mais ${more.length} opção(ões) para ${D}`), more.map(p => optEl({...p, dest: D}, "TROCA", row(p)))) : null,
        nope.length ? h("details", {class: "swap-bad"}, h("summary", {}, `${nope.length} opção(ões) que passam do limite ou não cabem`), nope.map(p => optEl(p, "TROCA", row(p)))) : null);
    };
    sel.onchange = () => showDest(sel.value);
    const typed = h("input", {placeholder: "ou digite: JPA, SBJP…", maxlength: 4, style: "text-transform:uppercase;max-width:180px"});
    const askTyped = async () => {
      const code = typed.value.trim();
      if (!code) return;
      sel.value = "";
      put(list, h("div", {class: "skel s"}));
      const e = await act(() => get(`/api/troca/destinos/${leg.id}?icao=${encodeURIComponent(code)}`));
      if (!e) return put(list);
      if (!e.ok) return put(list, note("hold", "alerta", e.why));
      const hit = [...sel.options].find(o => o.value === e.icao);
      if (hit) sel.value = e.icao;
      showDest(e.icao);
    };
    typed.onkeydown = ev => { if (ev.key === "Enter") { ev.preventDefault(); askTyped(); } };
    put(destEl, h("h3", {}, "Escolher o destino"),
      h("p", {class: "muted", style: "font-size:13px"}, d.rule || ""),
      d.items.length ? null : note("hold", "alerta", d.why || "Nenhum destino da lista cabe na sua escala; digite um para ver o motivo."),
      h("div", {class: "swap-dest-pick"}, field("Destino", sel), h("div", {class: "swap-typed"}, typed, h("button", {class: "btn ghost sm", type: "button", onclick: askTyped}, "Ver"))), list);
  };
  const lb = o.liberacao || {options: []};
  const lok = lb.options.filter(x => x.ok), lbad = lb.options.filter(x => !x.ok);
  const libRow = p => p.mode === "ciclo"
    ? h("div", {}, p.replaces.length > 1 ? h("small", {class: "muted"}, `Saem ${p.replaces.length} etapas juntas (voltam ao mesmo aeroporto):`) : null, p.replaces.map(legTxt))
    : h("div", {}, h("small", {class: "muted"}, `Sai do voo e vai de extra (passageiro, sem voar) até ${p.legs[0].ades}:`), p.legs.map(legTxt));
  const libEl = h("div", {class: "swap-group"}, h("h3", {}, "Sair da perna (a reserva cobre)"),
    lok.length ? lok.map(p => optEl(p, "LIBERACAO", libRow(p))) : h("p", {class: "muted"}, lb.why || "Não disponível."),
    lbad.length && lok.length ? h("details", {class: "swap-bad"}, h("summary", {}, `${lbad.length} opção(ões) que passam do limite`), lbad.map(p => optEl(p, "LIBERACAO", libRow(p)))) : null);
  dialog("Pedir troca de escala", o.leg_label,
    h("div", {class: "swap-dlg"}, trocaEl, destEl, libEl, fx, field("Motivo", reason),
      note("", "info", `Quem responde: ${o.answer_by}. A resposta chega como mensagem da equipe (e no celular, se estiver ligado). `,
        `Conexão mínima para trocar de aeronave: ${o.swap_min} min; jornada e repouso conferidos pelas regras do AeroScale.`,
        o.booked ? " Se for aprovada, a sua reserva deste voo é cancelada." : ""),
      h("p", {class: "fine"}, o.disclaimer)),
    [closeBtn, {label: "Enviar pedido", fn: async () => {
      if (!pick) { toast("Escolha uma opção."); return false; }
      const r = await act(() => post("/api/troca", {leg_id: leg.id, kind: pick.kind, ref: pick.kind === "TROCA" ? pick.ref : null,
        destino: pick.dest || null, reason: reason.value || null}), "Pedido enviado");
      if (!r) return false;
      render();
    }}]);
  loadDest();
}

const CFG_LABEL = {standby: "Standby", ddrmi: "DDRMI", dcdu: "DCDU", row_rops: "ROW/ROPS", drams: "DRAMS", fmc: "FMC",
                   thrust: "Empuxo (CFM56-7B)", sfp: "SFP", remark: "Observação da lista"};
// resumo da configuração (da lista de frota): "ISFD · SFP 2.0 · 27K · Collins" ou "ISIS · DDRMI · sem DCDU"
function cfgShort(a) {
  const c = a.config || {}, v = k => c[k] && c[k].value;
  const yn = (k, l) => v(k) === "SIM" ? l : v(k) === "NÃO" ? `sem ${l}` : null;
  return [v("standby"), v("sfp") && v("sfp") !== "não consta na lista" ? v("sfp") : null, v("thrust"), v("fmc"),
          yn("ddrmi", "DDRMI"), yn("dcdu", "DCDU"), yn("row_rops", "ROW/ROPS"), yn("drams", "DRAMS")].filter(Boolean).join(" · ");
}
function bookDialog(leg, fleet) {
  const run = async () => {
    const list = fleet || await get("/api/aircraft");
    const sel = h("select", {}, h("option", {value: ""}, "Decidir depois"),
      list.map(a => h("option", {value: a.id}, `${a.registration} · ${a.type_icao}${cfgShort(a) ? " · " + cfgShort(a) : ""}${a.availability.status !== "DISPONIVEL" ? " · " + (a.availability.status === "COM_RESTRICAO" ? "com restrição" : "indisponível") : ""}`)));
    const force = h("input", {type: "checkbox"});
    dialog(`Reservar ${leg.flight_number || "perna"}`, `${leg.adep} → ${leg.ades} · ${dt(leg.dep_utc)} · tipo planejado ${leg.type_icao || "—"}`,
      h("div", {}, list.length ? field("Aeronave da sua frota", sel) : note("hold", "frota", "Frota vazia: dá para reservar agora e atribuir a aeronave depois."),
        h("label", {class: "ck"}, force, "Aceitar aeronave de tipo diferente do planejado")),
      [closeBtn, {label: "Reservar", fn: async () => {
        const r = await act(() => post(`/api/roster/${leg.id || leg.leg_id}/book`, {aircraft_id: sel.value ? +sel.value : null, force_type: force.checked}), "Voo reservado");
        if (!r) return false;
        (r.warnings || []).forEach(w => toast(w));
        render();
      }}]);
  };
  run();
}

/* ───────────────────────── Flight Center ───────────────────────── */
view("voo", async (main, id) => {
  if (!id) { location.hash = "#/voos"; return; }
  const c = await get("/api/flight-center/" + id);
  const f = c.flight, leg = c.leg;
  const done = c.stages.filter(s => s.state === "PRONTO").length;
  const acts = [];
  if (f.state === "RESERVADO") acts.push(h("button", {class: "btn ghost", onclick: () => assignDialog(f)}, icon("frota", 16), f.aircraft_id ? "Trocar aeronave" : "Atribuir aeronave"));
  if (f.state === "RESERVADO" || f.state === "REJEITADO") acts.push(h("button", {class: "btn ghost", onclick: () => manualDialog({...flightTarget(f), aircraft_id: f.aircraft_id})}, icon("ok", 16), "Já fiz esse voo"));
  if (c.manual_entry) acts.push(h("button", {class: "btn stop", onclick: () => reasonDialog("Estornar o voo declarado", "O lançamento no diário e as horas da aeronave ficam marcados como estornados; o voo volta para reservado.",
    "Motivo", r => post(`/api/logbook/${c.manual_entry.id}/reverse`, {reason: r}), "Lançamento estornado")}, "Estornar lançamento"));
  if (f.state === "PIREP_RECEBIDO" && c.pirep) acts.push(h("a", {class: "btn", href: `#/pireps/${c.pirep.id}`}, "Revisar PIREP", icon("seta", 16)));
  if (f.state === "REJEITADO") acts.push(h("button", {class: "btn ghost", onclick: async () => { if (await act(() => post(`/api/flights/${id}/reactivate`), "Voo reaberto")) render(); }}, "Reabrir voo"));
  if (f.state === "RESERVADO") acts.push(h("button", {class: "btn stop", onclick: async () => { if (await act(() => post(`/api/flights/${id}/cancel`), "Voo cancelado")) location.hash = "#/voos"; }}, "Cancelar"));

  const stages = h("div", {class: "stages"}, c.stages.map(s => {
    const el = h("section", {class: "stage", "data-st": s.state});
    const inner = h("div", {class: "inner"}, s.note ? h("p", {class: "muted", style: "margin:0"}, s.note) : null, stageExtra(s, c));
    el.append(h("button", {"aria-expanded": "false", onclick: () => expand(el)},
      h("span", {class: "mk"}, icon(STAGE_ICON[s.state] || "pendente", 15)),
      h("span", {style: "min-width:0"}, h("span", {class: "ti"}, s.title), h("span", {class: "de", style: "display:block"}, s.detail)),
      h("span", {class: "tools"}, chip(STAGE_WORD[s.state] || s.state, s.state === "PRONTO" ? "go" : s.state === "ATENCAO" ? "hold" : s.state === "BLOQUEADO" ? "stop" : s.state === "EM_ANDAMENTO" ? "or live" : ""),
        icon("seta", 16, "caret"))),
      h("div", {class: "body"}, h("div", {}, inner)));
    return el;
  }));
  put(main,
    h("div", {class: "head"}, h("div", {}, h("a", {href: "#/voos", class: "muted", style: "font-size:13px"}, "← Voos"),
      h("h1", {}, `${f.flight_number || "Voo " + f.id}`), h("div", {class: "sub"}, `${placeName(leg.from)} → ${placeName(leg.to)} · ${dmy(f.dep_utc)}`,
        f.kind === "UNPLANNED" ? " · fora da escala" : "")), h("div", {class: "tools"}, acts)),
    boarding({...leg, state: f.state, registration: f.registration, aircraft_type: f.aircraft_type, availability: c.aircraft && c.aircraft.availability},
      {whenLabel: dmy(f.dep_utc).slice(0, 5)}),
    h("div", {class: "cols-2", style: "margin-top:18px"},
      h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Preparação do voo"), h("span", {class: "muted", style: "font-size:13px"}, `${done} de ${c.stages.length} prontas`)), stages),
      h("div", {},
        h("div", {class: "panel"}, h("h2", {}, "Jornada"), h("div", {class: "hr"}),
          h("dl", {class: "kv"},
            h("dt", {}, "Apresentação"), h("dd", {}, c.duty.report_utc ? localPair(c.duty.report_utc, c.duty.report_local) : "—"),
            h("dt", {}, "Partida"), h("dd", {}, localPair(f.dep_utc, leg.dep_local)),
            h("dt", {}, "Chegada"), h("dd", {}, localPair(f.arr_utc, leg.arr_local)),
            h("dt", {}, "Bloco previsto"), h("dd", {}, hmLong(leg.block_min)),
            h("dt", {}, "Distância"), h("dd", {}, leg.distance_nm ? leg.distance_nm + " nm" : "—")),
          c.duty.source ? h("p", {class: "fine"}, cap(c.duty.source) + ".") : null),
        lineCheckNote(c.line_check),
        delayPanel(c),
        acarsPanel(c),
        crewPanel(c.crew),
        stayCard(f),
        (c.messages || []).filter(m => m.channel !== "acars").length ? h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Conversa do voo"),
          h("a", {href: "#/mensagens", class: "muted", style: "font-size:13px"}, "caixa")),
          h("div", {class: "mmini"}, c.messages.filter(m => m.channel !== "acars").map(m => h("details", {class: "mfold"}, h("summary", {}, avatar(m, 30),
            h("div", {class: "mbody"}, h("div", {class: "mtop"}, h("b", {}, m.sender), h("span", {class: "msec"}, m.sector_label.split(" · ")[0]), h("small", {}, msgTime(m.ts))),
              h("div", {class: "msub"}, m.subject))), h("div", {class: "mtext", style: "margin:8px 0 4px 40px"}, m.body))))) : null,
        networkPanel([leg.adep, leg.ades], "Na frequência"),
        (c.timeline || []).length ? h("div", {class: "panel"}, h("h2", {}, "Linha do tempo"), h("div", {class: "hr"}),
          h("div", {class: "tl"}, c.timeline.slice().reverse().map(m => h("div", {class: "tl-i" + (m.kind === "WARN" ? " warn" : m.kind === "ESTADO" ? " st" : "")},
            h("span", {class: "t"}, dt(m.t)), h("span", {}, m.text))))) : null,
        c.other_legs_today.length ? h("div", {class: "panel"}, h("h2", {}, "Outras pernas do dia"), h("div", {class: "hr"}),
          c.other_legs_today.map(l => stripOf(l, [l.flight_state ? stateChip(l.flight_state) : chip("não reservada", "")], l.flight_id ? `#/voo/${l.flight_id}` : null))) : null)));
});

function stageExtra(s, c) {
  const f = c.flight;
  if (s.key === "despacho") {
    const sb = c.simbrief || {}, pr = sb.params || {}, alt = sb.alternate || {}, o = c.ofp;
    const acoes = h("div", {class: "tools"},
      sb.url ? h("a", {class: "btn ghost sm", href: sb.url, target: "_blank", rel: "noopener noreferrer"}, icon("decolar", 15), "Abrir SimBrief") : null,
      h("button", {class: "btn sm", onclick: () => importOfp(f.id)}, icon("importar", 15), o ? "Atualizar OFP" : "Importar OFP"),
      h("button", {class: "btn ghost sm", onclick: () => ofpFileDialog(f.id)}, "Enviar arquivo"),
      o ? h("button", {class: "btn ghost sm", onclick: async () => { if (await act(() => api("DELETE", "/api/ofp/" + o.id), "OFP removido")) render(); }}, "Remover") : null);
    if (!o) return [acoes,
      sb.url ? h("dl", {class: "kv"}, h("dt", {}, "Rota do RPL"), h("dd", {}, pr.route || "—"),
        h("dt", {}, "Alternado sugerido"), h("dd", {}, alt.icao ? `${alt.icao} (${alt.source})` : "o SimBrief escolhe")) : null,
      alt.note ? h("p", {class: "fine", style: "margin:0"}, cap(alt.note) + ".") : null,
      h("p", {class: "fine", style: "margin:0"}, "Gere o plano no SimBrief e clique em Importar OFP: o LineOps busca o seu último plano e guarda com o voo.")];
    return [releaseBlock(c), acoes, ofpBlock(o)];
  }
  if (s.key === "loadsheet") {
    const L = c.loadsheet || {}, prev = L.preview || {lines: [], warnings: []}, em = L.issued;
    const kgv = v => v == null ? "—" : Math.round(v).toLocaleString("pt-BR") + " kg";
    return [
      h("div", {class: "tools"},
        h("button", {class: "btn sm", onclick: () => loadsheetDialog(f.id, prev)}, icon("ok", 15), em ? "Emitir nova edição" : "Emitir loadsheet"),
        em ? h("a", {class: "btn ghost sm", href: `/api/loadsheet/${em.id}.pdf`, target: "_blank", rel: "noopener"}, icon("baixar", 15), `PDF da edição ${em.edition}`) : null),
      prev.warnings.map(w => note(w.level === "error" ? "stop" : w.level === "warn" ? "hold" : "", "alerta", w.message)),
      h("div", {class: "tbl", style: "width:100%"}, h("table", {},
        h("thead", {}, h("tr", {}, ["", "Peso", "Limite", "Margem", "Fonte"].map((t, k) => h("th", {class: k && k < 4 ? "n" : ""}, t)))),
        h("tbody", {}, prev.lines.map(l => h("tr", {style: l.label.startsWith("Peso de") || l.label.startsWith("Peso zero") ? "font-weight:600" : ""},
          h("td", {}, l.label), h("td", {class: "n" + (l.over ? " over" : "")}, kgv(l.value)),
          h("td", {class: "n muted"}, l.limit ? kgv(l.limit) : "—"),
          h("td", {class: "n" + (l.over ? " over" : "")}, l.margin == null ? "—" : kgv(l.margin)),
          h("td", {class: "muted", style: "font-size:12px;white-space:normal"}, l.source || "—")))))),
      em ? h("p", {class: "fine", style: "margin:0"}, `Edição ${em.edition} emitida em ${dt(em.issued_at)}`,
        em.notes ? ` · ${em.notes}` : "") : null,
      h("p", {class: "fine", style: "margin:0"}, (prev.cg && prev.cg.reason) || "")];
  }
  if (s.key === "meteo") {
    const w = c.weather || {};
    const tafs = Object.fromEntries((w.tafs || []).map(t => [t.icao, t]));
    const blocos = (w.metars || []).map(m => {
      const t = tafs[m.icao];
      return h("div", {style: "width:100%"},
        h("div", {class: "tools", style: "margin-bottom:6px"}, h("b", {class: "sign", style: "font-size:17px"}, m.icao),
          m.category ? chip(m.category, m.category === "VFR" ? "go" : m.category === "MVFR" ? "cool" : m.category === "IFR" ? "hold" : "stop") : null,
          m.freshness ? chip(m.freshness.label, m.freshness.status === "atual" ? "" : "hold") : null),
        m.raw ? h("p", {class: "mono", style: "margin:0 0 6px"}, m.raw) : note("hold", "alerta", m.error || "Sem METAR."),
        (m.alerts || []).map(a => note(a.level === "error" ? "stop" : a.level === "warning" ? "hold" : "", "alerta", a.text)),
        t && t.raw ? h("details", {}, h("summary", {class: "muted", style: "font-size:13px;cursor:pointer"}, "TAF"),
          (t.periods || [t.raw]).map(x => h("p", {class: "mono", style: "margin:4px 0"}, x))) : null);
    });
    return [h("button", {class: "btn ghost sm", onclick: async () => {
        const ic = [c.leg.adep, c.leg.ades, c.ofp && c.ofp.data.altn].filter(Boolean).join(",");
        if (await act(() => get(`/api/weather?icao=${ic}&force=true`), "Meteorologia atualizada")) render();
      }}, "Atualizar"),
      blocos.length ? blocos : note("hold", "alerta", w.error || "Nenhum METAR disponível. O computador do servidor tem internet?"),
      h("p", {class: "fine", style: "margin:0"}, "METAR e TAF do aviationweather.gov (NOAA), lidos pelo módulo do AeroScale. NOTAM não entra nesta versão.")];
  }
  if (s.key === "rota" && c.leg.route) return h("p", {class: "mono"}, c.leg.route);
  if (s.key === "mel" && c.aircraft) return h("a", {class: "btn ghost sm", href: `#/frota/${c.aircraft.id}`}, "Abrir ficha da aeronave");
  if (s.key === "acars") {
    const cmd = `python -m lineops_acars --server ${location.origin} --token SEU_TOKEN --sim xplane --flight ${f.id}`;
    const sess = c.session, live = sess && sess.status === "LIVE";
    const out = [];
    if (live && sess.sim === "ivao") out.push(ivaoLiveBox(c));
    else if (live) out.push(note("or", "live", `Sessão ao vivo · fase ${sess.phase || "—"} · atualizado ${hhmmz(sess.last_seen_at)}`));
    else if (f.state === "RESERVADO") {
      out.push(h("div", {class: "tools"},
        c.ivao && c.ivao.vid ? h("button", {class: "btn sm", onclick: () => ivaoStart(f.id)}, icon("live", 15), "Acompanhar pela IVAO")
          : h("a", {class: "btn ghost sm", href: "#/ajustes"}, "Informe o seu VID da IVAO em Ajustes"),
        h("button", {class: "btn ghost sm", onclick: () => manualDialog({...flightTarget(f), aircraft_id: f.aircraft_id})}, icon("ok", 15), "Já fiz esse voo")),
        h("p", {class: "fine", style: "margin:0"}, "Pela IVAO, o LineOps lê o whazzup público (o mesmo dado do Webeye) a cada 15 s e monta o PIREP sozinho quando você para no portão."));
    }
    out.push(h("details", {style: "width:100%"}, h("summary", {class: "muted", style: "cursor:pointer;font-size:13px"}, "ACARS do LineOps no PC do simulador"),
      h("p", {class: "muted", style: "margin:8px 0 0"}, "No PC do simulador, abra ", h("b", {}, "iniciar-acars.bat"), " e informe o voo ", h("b", {}, String(f.id)), ". Pela linha de comando:"),
      h("p", {class: "mono"}, cmd),
      h("button", {class: "btn ghost sm", onclick: async ev => {
        try { await navigator.clipboard.writeText(cmd); toast("Comando copiado"); } catch (e) { toast("Não foi possível copiar", true); }
        ev.currentTarget.classList.add("pulse");
      }}, "Copiar comando")));
    return out;
  }
  if (s.key === "pirep" && c.pirep) return h("a", {class: "btn ghost sm", href: `#/pireps/${c.pirep.id}`}, "Abrir PIREP");
  if (s.key === "briefing") {
    const l = c.leg, a = c.aircraft;
    return h("dl", {class: "kv"},
      h("dt", {}, "Trecho"), h("dd", {}, `${l.adep} → ${l.ades}`),
      h("dt", {}, "Origem do dado"), h("dd", {}, l.data_origin || "LineOps"),
      h("dt", {}, "Aeronave"), h("dd", {}, a ? `${a.registration} (${a.type_icao})` : "a definir"),
      h("dt", {}, "Rota"), h("dd", {}, l.route || "não informada"),
      h("dt", {}, "Nível"), h("dd", {}, l.cruise_fl ? "FL" + l.cruise_fl : "não informado"),
      h("dt", {}, "Restrições"), h("dd", {}, a && a.availability.restrictions.length ? a.availability.restrictions.map(r => r.title).join("; ") : "nenhuma"));
  }
  return null;
}

function ofpBlock(o) {
  const d = o.data, kg = v => v == null ? "—" : Math.round(v).toLocaleString("pt-BR") + " kg";
  const margem = (v, max) => v == null || max == null ? null : chip(`${Math.round(max - v).toLocaleString("pt-BR")} kg de margem`, max - v < 0 ? "stop" : "go");
  return h("div", {style: "width:100%"},
    (o.warnings || []).map(w => note(w.level === "warn" ? "hold" : "", w.level === "warn" ? "alerta" : "info", w.message)),
    h("div", {class: "cols", style: "gap:14px 22px;grid-template-columns:repeat(auto-fit,minmax(190px,1fr))"},
      h("div", {}, h("h3", {}, "Combustível"), h("dl", {class: "kv"},
        h("dt", {}, "Na partida"), h("dd", {}, kg(d.fuel_ramp)),
        h("dt", {}, "Táxi"), h("dd", {}, kg(d.fuel_taxi)),
        h("dt", {}, "Viagem"), h("dd", {}, kg(d.fuel_trip)),
        h("dt", {}, "Contingência"), h("dd", {}, kg(d.fuel_contingency)),
        h("dt", {}, "Alternado"), h("dd", {}, kg(d.fuel_alternate)),
        h("dt", {}, "Reserva"), h("dd", {}, kg(d.fuel_reserve)),
        h("dt", {}, "Extra"), h("dd", {}, kg(d.fuel_extra)),
        h("dt", {}, "No pouso"), h("dd", {}, kg(d.fuel_landing)))),
      h("div", {}, h("h3", {}, "Pesos"), h("dl", {class: "kv"},
        h("dt", {}, "Vazio operacional"), h("dd", {}, kg(d.oew)),
        h("dt", {}, "Passageiros"), h("dd", {}, d.pax_count == null ? "—" : `${d.pax_count}${d.pax_weight ? ` × ${Math.round(d.pax_weight)} kg` : ""}`),
        h("dt", {}, "Carga"), h("dd", {}, kg(d.cargo)),
        h("dt", {}, "Payload"), h("dd", {}, kg(d.payload)),
        h("dt", {}, "ZFW"), h("dd", {}, kg(d.est_zfw), " ", margem(d.est_zfw, d.max_zfw)),
        h("dt", {}, "Decolagem"), h("dd", {}, kg(d.est_tow), " ", margem(d.est_tow, d.max_tow)),
        h("dt", {}, "Pouso"), h("dd", {}, kg(d.est_ldw), " ", margem(d.est_ldw, d.max_ldw)))),
      h("div", {}, h("h3", {}, "Plano"), h("dl", {class: "kv"},
        h("dt", {}, "Trecho"), h("dd", {}, `${d.adep}→${d.ades}${d.altn ? "/" + d.altn : ""}`),
        h("dt", {}, "Pistas"), h("dd", {}, `${d.adep_rwy || "—"} / ${d.ades_rwy || "—"}`),
        h("dt", {}, "Nível inicial"), h("dd", {}, d.initial_altitude ? "FL" + Math.round(d.initial_altitude / 100) : "—"),
        h("dt", {}, "Cost index"), h("dd", {}, d.cost_index == null ? "—" : d.cost_index),
        h("dt", {}, "Tempo de voo"), h("dd", {}, hmLong(d.eet_min)),
        h("dt", {}, "Distância"), h("dd", {}, d.air_distance == null ? "—" : Math.round(d.air_distance) + " nm"),
        h("dt", {}, "Vento médio"), h("dd", {}, d.avg_wind_comp == null ? "—" : Math.round(d.avg_wind_comp) + " kt"),
        h("dt", {}, "AIRAC"), h("dd", {}, d.airac || "—"),
        h("dt", {}, "Aeronave"), h("dd", {}, `${d.type_icao || "—"}${d.registration ? " · " + d.registration : ""}`)))),
    d.route ? h("p", {class: "mono", style: "margin-top:10px"}, d.route) : null,
    h("p", {class: "fine", style: "margin:0"}, `Origem: ${o.source === "arquivo" ? "arquivo enviado por você" : "API do SimBrief"}`,
      o.generated_at ? ` · plano gerado ${dt(o.generated_at)}` : "", ` · importado ${dt(o.fetched_at)}`,
      d.converted_from ? " · pesos convertidos de libras" : "",
      (d.missing || []).length ? ` · ${d.missing.length} campo(s) não vieram no plano` : ""));
}

async function importOfp(flightId) {
  const cfg = await get("/api/settings").catch(() => ({}));
  const run = async (body) => {
    const r = await act(() => post(`/api/flights/${flightId}/ofp`, body), "OFP importado");
    if (!r) return false;
    (r.warnings || []).filter(w => w.level === "warn").forEach(w => toast(w.message));
    render();
  };
  if (cfg.simbrief_userid || cfg.simbrief_username) return run({});
  const id = h("input", {placeholder: "123456"}), lembrar = h("input", {type: "checkbox", checked: true});
  dialog("Importar OFP do SimBrief", "O LineOps busca o seu plano mais recente. O Pilot ID está em Account Settings no SimBrief.",
    h("div", {}, field("Pilot ID (ou nome de usuário)", id), h("label", {class: "ck"}, lembrar, "Guardar para as próximas vezes")),
    [closeBtn, {label: "Importar", fn: async () => {
      const v = id.value.trim();
      if (!v) { toast("Informe o Pilot ID.", true); return false; }
      return run(/^\d+$/.test(v) ? {userid: v, remember: lembrar.checked} : {username: v, remember: lembrar.checked});
    }}]);
}

function ofpFileDialog(flightId) {
  const f = h("input", {type: "file", accept: ".json,application/json"});
  dialog("Enviar o OFP em arquivo", "Para quando o computador do servidor não tem internet. Baixe o JSON em simbrief.com/api/xml.fetcher.php?userid=SEU_ID&json=1 e envie aqui.",
    h("div", {}, f),
    [closeBtn, {label: "Enviar", fn: async () => {
      if (!f.files[0]) { toast("Escolha o arquivo.", true); return false; }
      const r = await act(async () => api("POST", `/api/flights/${flightId}/ofp/file`, undefined, await f.files[0].text()), "OFP importado");
      if (!r) return false;
      render();
    }}]);
}

function loadsheetDialog(flightId, prev) {
  const atual = Object.fromEntries(prev.lines.map(l => [l.label, l.value]));
  const i = {pax_count: h("input", {type: "number", min: 0, placeholder: "do OFP"}),
             bag_weight: h("input", {type: "number", min: 0, placeholder: "do OFP"}),
             cargo: h("input", {type: "number", min: 0, placeholder: atual["Carga"] == null ? "do OFP" : String(Math.round(atual["Carga"]))}),
             fuel_ramp: h("input", {type: "number", min: 0, placeholder: atual["Combustível na partida"] == null ? "do OFP" : String(Math.round(atual["Combustível na partida"]))})};
  const notas = h("input", {placeholder: "obrigatória se algum limite estourar"});
  dialog("Emitir LOADSHEET", "Em branco, vale o que veio do OFP. O que você digitar aparece como informado por você.",
    h("div", {}, h("div", {class: "row"}, field("Passageiros", i.pax_count), field("Bagagem (kg)", i.bag_weight),
      field("Carga (kg)", i.cargo), field("Combustível na partida (kg)", i.fuel_ramp)), field("Justificativa", notas)),
    [closeBtn, {label: "Emitir", fn: async () => {
      const ov = {};
      for (const [k, el] of Object.entries(i)) if (el.value !== "") ov[k] = +el.value;
      const r = await act(() => post(`/api/flights/${flightId}/loadsheet`, {overrides: ov, notes: notas.value || null}), "LOADSHEET emitido");
      if (!r) return false;
      render();
    }}]);
}

/* ───────────────────────── voos ───────────────────────── */
view("voos", async main => {
  const [list, fleet] = await Promise.all([get("/api/flights"), get("/api/aircraft")]);
  put(main, h("div", {class: "head"}, h("div", {}, h("h1", {}, "Voos"), h("div", {class: "sub"}, "Pernas reservadas e voos já realizados.")),
      h("div", {class: "tools"}, h("button", {class: "btn", onclick: () => unplannedDialog(fleet)}, icon("mais", 16), "Voo fora da escala"))),
    list.length ? h("div", {}, list.map(f => stripOf(f, [f.kind === "UNPLANNED" ? chip("Fora da escala", "hold") : null, stateChip(f.state), icon("seta", 16)], `#/voo/${f.id}`)))
      : h("div", {class: "empty"}, icon("vazio", 26), h("div", {}, "Nenhum voo ainda. "), h("a", {href: "#/escala"}, "Escolher uma perna na escala")));
});

function assignDialog(f) {
  get("/api/aircraft").then(list => {
    if (!list.length) return toast("Cadastre uma aeronave na Frota antes.", true);
    const sel = h("select", {}, list.map(a => h("option", {value: a.id, selected: a.id === f.aircraft_id}, `${a.registration} · ${a.type_icao} · ${a.availability.status.toLowerCase().replace("_", " ")}`)));
    const force = h("input", {type: "checkbox"});
    dialog("Aeronave do voo", `${f.flight_number || "Voo " + f.id} · tipo planejado ${f.type_icao || "—"}`,
      h("div", {}, field("Aeronave", sel), h("label", {class: "ck"}, force, "Aceitar tipo diferente do planejado")),
      [closeBtn, {label: "Atribuir", fn: async () => {
        const r = await act(() => api("PUT", `/api/flights/${f.id}/aircraft`, {aircraft_id: +sel.value, force_type: force.checked}), "Aeronave atribuída");
        if (!r) return false;
        (r.warnings || []).forEach(w => toast(w));
        render();
      }}]);
  });
}
function unplannedDialog(fleet) {
  if (!fleet.length) return toast("Cadastre uma aeronave na Frota antes.", true);
  const i = {fn: h("input", {placeholder: "opcional"}), adep: h("input", {maxlength: 4, placeholder: "SBBR"}), ades: h("input", {maxlength: 4, placeholder: "SBGR"}),
    ac: h("select", {}, fleet.map(a => h("option", {value: a.id}, `${a.registration} · ${a.type_icao}`))), dep: h("input", {type: "datetime-local"}), notes: h("input")};
  dialog("Voo fora da escala", "Fica identificado como não planejado no diário e no histórico da aeronave.",
    h("div", {}, h("div", {class: "row"}, field("Número do voo", i.fn), field("Aeronave", i.ac), field("Origem (ICAO)", i.adep), field("Destino (ICAO)", i.ades)),
      field("Partida prevista (UTC)", i.dep), field("Observações", i.notes)),
    [closeBtn, {label: "Criar voo", fn: async () => {
      const r = await act(() => post("/api/flights", {flight_number: i.fn.value || null, adep: i.adep.value, ades: i.ades.value, aircraft_id: +i.ac.value,
        dep_utc: i.dep.value ? i.dep.value + ":00Z" : null, notes: i.notes.value || null}), "Voo criado");
      if (!r) return false;
      location.hash = "#/voo/" + r.id;
    }}]);
}

/* ───────────────────────── frota ───────────────────────── */
view("frota", async (main, arg) => {
  if (arg) return aircraftDetail(main, +arg);
  const list = await get("/api/aircraft?all=true");
  put(main, h("div", {class: "head"}, h("div", {}, h("h1", {}, "Frota"), h("div", {class: "sub"}, "Por matrícula. A disponibilidade é calculada, nunca digitada.")),
      h("div", {class: "tools"}, h("button", {class: "btn ghost", onclick: csvDialog}, icon("importar", 16), "Importar CSV"),
        h("button", {class: "btn", onclick: addAircraftDialog}, icon("mais", 16), "Nova aeronave"))),
    list.length ? h("div", {class: "panel"}, h("div", {class: "tbl"}, h("table", {},
      h("thead", {}, h("tr", {}, ["Matrícula", "Tipo", "Configuração", "Situação", "Posição", "Horas", "Ciclos"].map((t, k) => h("th", {class: k > 4 ? "n" : ""}, t)))),
      h("tbody", {}, list.map(a => h("tr", {class: "click", tabindex: 0, onclick: () => location.hash = "#/frota/" + a.id,
        onkeydown: e => e.key === "Enter" && (location.hash = "#/frota/" + a.id)},
        h("td", {}, h("b", {}, a.registration)), h("td", {class: "t"}, a.type_icao), h("td", {class: "muted", style: "font-size:13px"}, cfgShort(a) || "—"), h("td", {}, availChip(a)),
        h("td", {class: "t"}, a.location ? a.location.icao : "—"),
        h("td", {class: "n"}, a.totals.total_hours == null ? hm(a.totals.flown_air_min) : a.totals.total_hours.toFixed(1)),
        h("td", {class: "n"}, a.totals.total_cycles == null ? a.totals.flown_cycles : a.totals.total_cycles)))))))
      : h("div", {class: "empty"}, icon("frota", 26),
        h("p", {style: "margin:0 auto;max-width:46ch"}, "Sua frota está vazia e o AeroScale não informa matrículas. Cadastre as aeronaves que você usa; limites de peso e horas ficam em branco até você informá-los.")));
});
function availChip(a) {
  const s = a.availability.status;
  const txt = !a.active ? "Inativa" : s === "DISPONIVEL" ? "Disponível" : s === "COM_RESTRICAO" ? "Com restrição" : "Indisponível";
  const cls = !a.active ? "" : s === "DISPONIVEL" ? "go" : s === "COM_RESTRICAO" ? "hold" : "stop";
  return chip(txt, cls, [...a.availability.blocking, ...a.availability.restrictions.map(r => r.title)].join("; ") || null);
}

async function aircraftDetail(main, id) {
  const a = await get("/api/aircraft/" + id);
  const LIM = {mtow_kg: "MTOW", mlw_kg: "MLW", mzfw_kg: "MZFW", oew_kg: "OEW", max_fuel_kg: "Combustível máximo", pax_capacity: "Passageiros"};
  const figs = h("div", {class: "figs"},
    [["Horas totais", a.totals.total_hours, v => v == null ? "sem base" : v.toLocaleString("pt-BR", {minimumFractionDigits: 1, maximumFractionDigits: 1})],
     ["Ciclos totais", a.totals.total_cycles, v => v == null ? "sem base" : Math.round(v).toLocaleString("pt-BR")],
     ["Horas no LineOps", a.totals.flown_air_min / 60, v => v.toLocaleString("pt-BR", {minimumFractionDigits: 1, maximumFractionDigits: 1})],
     ["Ciclos no LineOps", a.totals.flown_cycles, v => Math.round(v)],
     ["Posição", null, () => a.location ? a.location.icao : "—"]].map(([l, v, f]) => {
      const b = h("b", {}, f(v));
      if (typeof v === "number") countUp(b, v, f);
      return h("div", {class: "fig"}, b, h("span", {}, l));
    }));
  put(main,
    h("div", {class: "head"}, h("div", {}, h("a", {href: "#/frota", class: "muted", style: "font-size:13px"}, "← Frota"), h("h1", {}, a.registration),
      h("div", {class: "sub"}, `${a.type_icao}${a.model_name ? " · " + a.model_name : ""} · ${a.airline} · cadastro ${a.source.toLowerCase()}`)),
      h("div", {class: "tools"}, availChip(a), h("button", {class: "btn ghost", onclick: () => act(() => api("PATCH", "/api/aircraft/" + id, {active: !a.active}), a.active ? "Aeronave desativada" : "Aeronave ativada").then(render)}, a.active ? "Desativar" : "Ativar"))),
    a.availability.blocking.map(b => note("stop", "bloqueio", b)),
    a.availability.restrictions.map(r => note("hold", "alerta", `${r.title} (${r.restriction.toLowerCase()})${r.text ? ": " + r.text : ""}`)),
    figs,
    h("div", {class: "cols"},
      h("div", {class: "panel"}, h("h2", {}, "Limites e dados técnicos"), h("p", {class: "muted", style: "font-size:13.5px"}, "Só existe o que você informar, com a fonte. Em branco significa desconhecido."),
        h("dl", {class: "kv"}, Object.entries(LIM).map(([k, l]) => [h("dt", {}, l), h("dd", {title: a.limits[k] ? a.limits[k].source : null}, a.limits[k] ? a.limits[k].value.toLocaleString("pt-BR") + (k.endsWith("_kg") ? " kg" : "") : "—")])),
        h("div", {class: "tools", style: "margin-top:14px"}, h("button", {class: "btn ghost sm", onclick: () => limitsDialog(a, LIM)}, "Editar limites"),
          h("button", {class: "btn ghost sm", onclick: () => baselineDialog(a)}, "Horas e ciclos base"))),
      h("div", {class: "panel"}, h("h2", {}, "Configuração"), Object.keys(a.config || {}).length
          ? h("dl", {class: "kv"}, Object.entries(a.config).map(([k, v]) => [h("dt", {}, CFG_LABEL[k] || k), h("dd", {title: v && v.source ? "Fonte: " + v.source : null}, v && v.value != null ? String(v.value) : "—")]))
          : h("p", {class: "muted"}, "Sem configuração informada."),
        a.notes ? h("p", {class: "fine", style: "margin-top:10px"}, a.notes) : null),
      h("div", {class: "panel"}, h("h2", {}, "Discrepâncias"), h("p", {class: "muted", style: "font-size:13.5px"}, "Controle seu, simulado. Não é MEL oficial."),
        a.discrepancies.length ? a.discrepancies.map(d => note(d.status === "CORRIGIDA" ? "go" : d.restriction === "BLOQUEIA" ? "stop" : "hold",
          d.status === "CORRIGIDA" ? "ok" : "alerta", h("b", {}, d.title), ` — ${d.status.toLowerCase()}, restrição ${d.restriction.toLowerCase()}${d.due_date ? `, prazo ${dmy(d.due_date)}` : ""}`,
          d.status !== "CORRIGIDA" ? h("div", {style: "margin-top:8px"}, h("button", {class: "btn ghost sm", onclick: () => act(() => post(`/api/discrepancies/${d.id}/close`), "Discrepância corrigida").then(render)}, "Marcar corrigida")) : null))
          : h("p", {class: "muted"}, "Nenhuma discrepância aberta."),
        h("button", {class: "btn ghost sm", onclick: () => discDialog(a)}, icon("mais", 15), "Registrar")),
      h("div", {class: "panel"}, h("h2", {}, "Indisponibilidades"),
        a.unavailability.length ? a.unavailability.map(u => note("", "relogio", `${dmy(u.from_date)} → ${dmy(u.to_date)}: ${u.reason} `,
          h("button", {class: "btn ghost sm", style: "margin-left:8px", onclick: () => act(() => api("DELETE", "/api/unavailability/" + u.id), "Período removido").then(render)}, "Remover")))
          : h("p", {class: "muted"}, "Nenhum período registrado."),
        h("button", {class: "btn ghost sm", onclick: () => unavDialog(a)}, icon("mais", 15), "Adicionar período"))),
    h("div", {class: "panel"}, h("h2", {}, "Histórico de voos"), h("div", {class: "hr"}),
      a.history.length ? h("div", {class: "tbl"}, h("table", {}, h("thead", {}, h("tr", {}, ["Data", "Voo", "Trecho", "Bloco", "Voo", "Toque"].map((t, k) => h("th", {class: k > 2 ? "n" : ""}, t)))),
        h("tbody", {}, a.history.map(l => h("tr", {}, h("td", {}, dmy(l.date)), h("td", {}, l.flight_number || "—"), h("td", {class: "t"}, `${l.adep}→${l.ades}`),
          h("td", {class: "n"}, hm(l.block_min)), h("td", {class: "n"}, hm(l.air_min)), h("td", {class: "n"}, l.landing_vs_fpm == null ? "—" : l.landing_vs_fpm + " fpm"))))))
        : h("p", {class: "muted"}, "Nenhum voo aprovado com esta aeronave.")),
    h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Horas e ciclos"), h("button", {class: "btn ghost sm", onclick: () => adjustDialog(a)}, "Ajuste manual")),
      h("p", {class: "muted", style: "font-size:13.5px"}, "Cada PIREP aprovado gera um lançamento. Estornos ficam riscados; nada é apagado."),
      a.ledger.length ? h("div", {class: "tbl"}, h("table", {}, h("thead", {}, h("tr", {}, ["Quando", "Tipo", "Bloco", "Voo", "Ciclos", "Nota"].map((t, k) => h("th", {class: k > 1 && k < 5 ? "n" : ""}, t)))),
        h("tbody", {}, a.ledger.map(l => h("tr", {style: l.reversed_at ? "opacity:.45;text-decoration:line-through" : ""}, h("td", {}, dt(l.created_at)),
          h("td", {}, l.kind === "FLIGHT" ? "Voo" : "Ajuste"), h("td", {class: "n"}, hm(l.block_min)), h("td", {class: "n"}, hm(l.air_min)),
          h("td", {class: "n"}, l.cycles), h("td", {class: "muted"}, l.note || ""))))))
        : h("p", {class: "muted"}, "Sem lançamentos.")));
}
function addAircraftDialog() {
  const car0 = (me && me.career) || {}, ft = (car0.fleet || [])[1] || "B738";   // GOL: B738 · LATAM: A320 · Azul: E195
  const MODEL = {B738: "Boeing 737-800", B38M: "Boeing 737 MAX 8", A319: "Airbus A319", A320: "Airbus A320", A20N: "Airbus A320neo",
                 A321: "Airbus A321", A21N: "Airbus A321neo", B789: "Boeing 787-9", B77W: "Boeing 777-300ER",
                 AT76: "ATR 72-600", E195: "Embraer E195", E295: "Embraer E195-E2", A332: "Airbus A330-200", A339: "Airbus A330-900neo"};
  const i = {reg: h("input", {placeholder: "PR-XYZ"}), air: h("input", {value: car0.code || "GLO"}), typ: h("input", {placeholder: ft, maxlength: 4}),
    model: h("input", {placeholder: MODEL[ft] || "Boeing 737-800"}), msn: h("input"), bh: h("input", {type: "number", min: 0, step: "0.1"}), bc: h("input", {type: "number", min: 0})};
  dialog("Nova aeronave", "Cadastre apenas matrículas reais que você usa. Nada é preenchido automaticamente.",
    h("div", {}, h("div", {class: "row"}, field("Matrícula", i.reg), field("Companhia", i.air), field("Tipo ICAO", i.typ), field("Modelo (opcional)", i.model), field("MSN (opcional)", i.msn)),
      h("p", {class: "muted", style: "font-size:13.5px"}, "Horas e ciclos que a aeronave já tinha antes do LineOps. Deixe em branco se não souber."),
      h("div", {class: "row"}, field("Horas totais", i.bh), field("Ciclos totais", i.bc))),
    [closeBtn, {label: "Cadastrar", fn: async () => {
      const r = await act(() => post("/api/aircraft", {registration: i.reg.value, airline: i.air.value, type_icao: i.typ.value, model_name: i.model.value || null,
        msn: i.msn.value || null, baseline_hours: i.bh.value === "" ? null : +i.bh.value, baseline_cycles: i.bc.value === "" ? null : +i.bc.value,
        baseline_note: (i.bh.value || i.bc.value) ? "informado no cadastro" : null}), "Aeronave cadastrada");
      if (!r) return false;
      render();
    }}]);
}
function csvDialog() {
  const f = h("input", {type: "file", accept: ".csv,text/csv"}), out = h("div");
  dialog("Importar frota por CSV", "Colunas: registration, airline, type_icao (obrigatórias). Opcionais: model_name, msn, mtow_kg, mlw_kg, mzfw_kg, oew_kg, max_fuel_kg, pax_capacity, baseline_hours, baseline_cycles.",
    h("div", {}, h("p", {class: "muted", style: "font-size:13.5px"}, "Célula vazia significa desconhecido. Matrículas já cadastradas são puladas."), f, out),
    [closeBtn, {label: "Importar", keep: true, fn: async () => {
      if (!f.files[0]) return toast("Escolha um arquivo.", true);
      const r = await act(async () => api("POST", "/api/aircraft/import-csv", undefined, await f.files[0].text()));
      if (!r) return;
      put(out, note(r.errors.length ? "hold" : "go", r.errors.length ? "alerta" : "ok",
        `Cadastradas ${r.added.length}, já existiam ${r.skipped_existing.length}, com erro ${r.errors.length}.`),
        r.errors.map(e => note("stop", "alerta", `Linha ${e.line} (${e.registration || "?"}): ${e.error}`)));
      render();
    }}]);
}
function limitsDialog(a, LIM) {
  const inp = {}, src = h("input", {placeholder: "ex.: ficha da aeronave, manual do fabricante"});
  Object.keys(LIM).forEach(k => inp[k] = h("input", {type: "number", min: 0, value: a.limits[k] ? a.limits[k].value : ""}));
  dialog("Limites de " + a.registration, "Em branco é desconhecido. O LineOps não estima pesos.",
    h("div", {}, h("div", {class: "row"}, Object.entries(LIM).map(([k, l]) => field(l + (k.endsWith("_kg") ? " (kg)" : ""), inp[k]))), field("Fonte dos dados", src)),
    [closeBtn, {label: "Salvar", fn: async () => {
      const lim = {};
      Object.keys(LIM).forEach(k => { if (inp[k].value !== "") lim[k] = {value: +inp[k].value, source: (a.limits[k] && +inp[k].value === a.limits[k].value ? a.limits[k].source : src.value) || "informado por você"}; });
      if (!await act(() => api("PATCH", "/api/aircraft/" + a.id, {limits: lim}), "Limites salvos")) return false;
      render();
    }}]);
}
function baselineDialog(a) {
  const bh = h("input", {type: "number", min: 0, step: "0.1", value: a.baseline_hours == null ? "" : a.baseline_hours});
  const bc = h("input", {type: "number", min: 0, value: a.baseline_cycles == null ? "" : a.baseline_cycles});
  const n = h("input", {value: a.baseline_note || ""});
  dialog("Horas e ciclos base", "Totais que a aeronave já tinha antes do LineOps.",
    h("div", {}, h("div", {class: "row"}, field("Horas", bh), field("Ciclos", bc)), field("Origem da informação", n)),
    [closeBtn, {label: "Salvar", fn: async () => {
      if (!await act(() => api("PATCH", "/api/aircraft/" + a.id, {baseline_hours: bh.value === "" ? null : +bh.value, baseline_cycles: bc.value === "" ? null : +bc.value, baseline_note: n.value || null}), "Salvo")) return false;
      render();
    }}]);
}
function discDialog(a) {
  const t = h("input"), d = h("input"), rt = h("input", {placeholder: "ex.: somente pista seca"}), mel = h("input", {placeholder: "referência sua"});
  const r = h("select", {}, [["NENHUMA", "Sem restrição"], ["INFORMA", "Apenas informa"], ["CONFIRMA", "Exige confirmação para voar"], ["BLOQUEIA", "Bloqueia a aeronave"]].map(([v, l]) => h("option", {value: v}, l)));
  const def = h("input", {type: "checkbox"}), due = h("input", {type: "date"});
  dialog("Registrar discrepância", `Aeronave ${a.registration}`,
    h("div", {}, field("Título", t), field("Descrição", d), field("Restrição", r), field("Texto da restrição", rt),
      h("label", {class: "ck"}, def, "Diferida"), h("div", {class: "row", style: "margin-top:12px"}, field("Prazo do diferimento", due), field("Referência MEL (sua)", mel))),
    [closeBtn, {label: "Registrar", fn: async () => {
      if (!await act(() => post(`/api/aircraft/${a.id}/discrepancies`, {title: t.value, description: d.value, restriction: r.value,
        restriction_text: rt.value || null, deferred: def.checked, due_date: due.value || null, mel_ref: mel.value || null}), "Discrepância registrada")) return false;
      render();
    }}]);
}
function unavDialog(a) {
  const f = h("input", {type: "date"}), t = h("input", {type: "date"}), r = h("input", {placeholder: "ex.: manutenção programada"});
  dialog("Período indisponível", `Aeronave ${a.registration}`,
    h("div", {}, h("div", {class: "row"}, field("De", f), field("Até", t)), field("Motivo", r)),
    [closeBtn, {label: "Salvar", fn: async () => {
      if (!await act(() => post(`/api/aircraft/${a.id}/unavailability`, {from_date: f.value, to_date: t.value, reason: r.value}), "Período salvo")) return false;
      render();
    }}]);
}
function adjustDialog(a) {
  const b = h("input", {type: "number", value: 0}), v = h("input", {type: "number", value: 0}), c = h("input", {type: "number", value: 0}), n = h("input");
  dialog("Ajuste manual", "Minutos, podendo ser negativos. Fica registrado com a justificativa.",
    h("div", {}, h("div", {class: "row"}, field("Bloco (min)", b), field("Voo (min)", v), field("Ciclos", c)), field("Justificativa", n)),
    [closeBtn, {label: "Lançar", fn: async () => {
      if (!await act(() => post(`/api/aircraft/${a.id}/adjust`, {block_min: +b.value, air_min: +v.value, cycles: +c.value, note: n.value}), "Ajuste lançado")) return false;
      render();
    }}]);
}

/* ───────────────────────── PIREPs ───────────────────────── */
let pirepFilter = "PENDING";
view("pireps", async (main, arg) => {
  if (arg) return pirepDetail(main, +arg);
  const list = await get("/api/pireps" + (pirepFilter ? "?state=" + pirepFilter : ""));
  put(main, h("div", {class: "head"}, h("div", {}, h("h1", {}, "PIREPs"), h("div", {class: "sub"}, "O que o ACARS mediu no simulador. Nada vira horas ou ciclos antes da sua aprovação.")),
      h("div", {class: "seg"}, [["PENDING", "Em revisão"], ["APPROVED", "Aprovados"], ["REJECTED", "Rejeitados"], ["", "Todos"]].map(([v, l]) =>
        h("button", {"aria-pressed": pirepFilter === v, onclick: () => { pirepFilter = v; render(); }}, l)))),
    list.length ? h("div", {class: "panel"}, h("div", {class: "tbl"}, h("table", {},
      h("thead", {}, h("tr", {}, ["Enviado", "Voo", "Trecho", "Aeronave", "Situação", "Avisos"].map(t => h("th", {}, t)))),
      h("tbody", {}, list.map(p => h("tr", {class: "click", tabindex: 0, onclick: () => location.hash = "#/pireps/" + p.id,
        onkeydown: e => e.key === "Enter" && (location.hash = "#/pireps/" + p.id)},
        h("td", {}, dt(p.submitted_at)), h("td", {}, p.flight_number || "—"), h("td", {class: "t"}, `${p.adep}→${p.ades}`), h("td", {class: "t"}, p.registration || "—"),
        h("td", {}, chip({PENDING: "Em revisão", APPROVED: "Aprovado", REJECTED: "Rejeitado"}[p.state], {PENDING: "hold", APPROVED: "go", REJECTED: "stop"}[p.state])),
        h("td", {}, p.warnings.length ? chip(String(p.warnings.length), p.warnings.some(w => w.level === "error") ? "stop" : "hold") : h("span", {class: "muted"}, "—"))))))))
      : h("div", {class: "empty"}, icon("pireps", 26), h("div", {}, "Nenhum PIREP nesta lista.")));
});

async function pirepDetail(main, id) {
  const p = await get("/api/pireps/" + id), d = p.data;
  const acts = [];
  if (p.state === "PENDING") acts.push(h("button", {class: "btn", onclick: () => approveDialog(p)}, icon("ok", 16), "Aprovar"),
    h("button", {class: "btn stop", onclick: () => reasonDialog("Rejeitar PIREP", "O voo volta para rejeitado e pode ser refeito.", "Motivo", r => post(`/api/pireps/${id}/reject`, {notes: r}), "PIREP rejeitado")}, "Rejeitar"));
  if (p.state === "APPROVED") acts.push(h("button", {class: "btn stop", onclick: () => reasonDialog("Estornar PIREP", "O diário e as horas são desfeitos; o histórico fica marcado.", "Motivo do estorno", r => post(`/api/pireps/${id}/reverse`, {reason: r}), "PIREP estornado")}, "Estornar"));
  const rows = [["Calços fora (OUT)", hhmmz(d.out_utc)], ["Decolagem (OFF)", hhmmz(d.off_utc)], ["Pouso (ON)", hhmmz(d.on_utc)], ["Calços (IN)", hhmmz(d.in_utc)],
    ["Tempo de bloco", hm(d.block_min)], ["Tempo de voo", hm(d.air_min)], ["Noturno", hm(d.night_min)],
    ["Combustível na saída", kg(d.fuel_out_kg)], ["Combustível na chegada", kg(d.fuel_in_kg)], ["Consumo", kg(d.fuel_used_kg)],
    ["Peso bruto na saída", kg(d.gw_out_kg)], ["Peso bruto no pouso", kg(d.gw_on_kg)],
    ["Razão no toque", d.landing_vs_fpm == null ? "—" : d.landing_vs_fpm + " fpm"], ["Força G no toque", d.landing_g == null ? "—" : d.landing_g.toFixed(2)],
    ["Quiques", d.bounces == null ? "—" : d.bounces], ["Altitude máxima", d.max_alt_ft == null ? "—" : d.max_alt_ft.toLocaleString("pt-BR") + " ft"],
    ["Distância voada", d.distance_nm == null ? "—" : d.distance_nm + " nm"], ["Origem detectada", d.detected_adep || "—"], ["Destino detectado", d.detected_ades || "—"]];
  put(main,
    h("div", {class: "head"}, h("div", {}, h("a", {href: "#/pireps", class: "muted", style: "font-size:13px"}, "← PIREPs"),
      h("h1", {}, `${p.flight_number || "Voo"} · ${p.adep} → ${p.ades}`),
      h("div", {class: "sub"}, `${p.registration || "sem aeronave"} · ${p.type_icao || "—"} · ${p.flight_kind === "UNPLANNED" ? "fora da escala" : "da escala"} · enviado ${dt(p.submitted_at)}`,
        d.webeye ? [" · ", h("a", {href: d.webeye, target: "_blank", rel: "noopener noreferrer"}, "ver no Webeye")] : null)),
      h("div", {class: "tools"}, acts)),
    p.review_notes ? note("", "info", p.review_notes) : null,
    p.warnings.map(w => note(w.level === "error" ? "stop" : w.level === "warn" ? "hold" : "", w.level === "error" ? "alerta" : "info", w.message)),
    p.comparison && p.comparison.length ? h("div", {class: "panel"}, h("h2", {}, "Planejado × medido"),
      h("p", {class: "muted", style: "font-size:13px"}, "OFP do SimBrief ao lado do que o ACARS mediu no simulador."), h("div", {class: "hr"}),
      h("div", {class: "tbl"}, h("table", {}, h("thead", {}, h("tr", {}, ["", "Planejado", "Medido", "Diferença"].map((t, k) => h("th", {class: k ? "n" : ""}, t)))),
        h("tbody", {}, p.comparison.map(x => h("tr", {}, h("td", {}, x.label), h("td", {class: "n"}, `${x.planned_text} ${x.unit}`),
          h("td", {class: "n"}, `${x.flown_text} ${x.unit}`), h("td", {class: "n"}, x.diff_text))))))) : null,
    h("div", {class: "cols-2"},
      h("div", {class: "panel"}, h("h2", {}, "Medições do ACARS"), h("p", {class: "muted", style: "font-size:13px"}, d.measured_by || ""), h("div", {class: "hr"}),
        h("dl", {class: "kv"}, rows.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]))),
      h("div", {}, h("div", {class: "panel"}, h("h2", {}, "Trilha"), h("div", {class: "hr"}), trackMap(p.track, p.adep, p.ades),
        h("p", {class: "fine"}, `${p.track.length} pontos registrados.`)),
        h("div", {class: "panel"}, h("h2", {}, "Mensagens"), h("div", {class: "hr"}),
          p.messages.length ? h("div", {class: "tbl"}, h("table", {}, h("tbody", {}, p.messages.map(m =>
            h("tr", {}, h("td", {class: "t"}, hhmmz(m.t)), h("td", {}, chip(m.kind, m.kind === "WARN" ? "hold" : "")), h("td", {class: "t wrap"}, m.text))))))
            : h("p", {class: "muted"}, "Nenhuma mensagem.")))));
}
function trackMap(track, adep, ades) {
  if (!track || track.length < 2) return h("div", {class: "empty"}, icon("vazio", 24), h("div", {}, "Sem trilha registrada."));
  const W = 680, H = 300, pad = 30;
  const lat0 = track.reduce((a, p) => a + p.lat, 0) / track.length, k = Math.cos(lat0 * Math.PI / 180);
  const xs = track.map(p => p.lon * k), ys = track.map(p => -p.lat);
  const mnx = Math.min(...xs), mxx = Math.max(...xs), mny = Math.min(...ys), mxy = Math.max(...ys);
  const sc = Math.min((W - 2 * pad) / ((mxx - mnx) || 1e-6), (H - 2 * pad) / ((mxy - mny) || 1e-6));
  const px = i => pad + (xs[i] - mnx) * sc + ((W - 2 * pad) - (mxx - mnx) * sc) / 2;
  const py = i => pad + (ys[i] - mny) * sc + ((H - 2 * pad) - (mxy - mny) * sc) / 2;
  const s = svg("svg", {class: "map", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `Trilha de ${adep} para ${ades}`});
  const line = svg("polyline", {class: "trk", points: track.map((_, i) => `${px(i).toFixed(1)},${py(i).toFixed(1)}`).join(" ")});
  s.append(line);
  [[0, adep], [track.length - 1, ades]].forEach(([i, n]) => {
    s.append(svg("circle", {class: "ap", cx: px(i), cy: py(i), r: 5}), svg("text", {x: px(i) + 10, y: py(i) - 9}, n));
  });
  requestAnimationFrame(() => drawLine(line));
  return s;
}
function approveDialog(p) {
  const d = p.data;
  const b = h("input", {type: "number", min: 0, value: d.block_min == null ? "" : d.block_min});
  const a = h("input", {type: "number", min: 0, value: d.air_min == null ? "" : d.air_min});
  const n = h("input", {type: "number", min: 0, value: d.night_min == null ? "" : d.night_min});
  const l = h("input", {type: "number", min: 1, value: 1}), rm = h("input"), notes = h("input", {placeholder: "obrigatória se houver erro"});
  const vs = h("input", {type: "number", step: 1, value: d.landing_vs_fpm == null ? "" : d.landing_vs_fpm, placeholder: "ex.: -180", required: true});
  const gg = h("input", {type: "number", step: "0.01", min: 0.5, max: 6, value: d.landing_g == null ? "" : d.landing_g, placeholder: "ex.: 1.25", required: true});
  const hard = h("input", {type: "checkbox"});
  dialog("Aprovar PIREP", "O voo entra no diário e a aeronave recebe horas e ciclos. Ajuste apenas se o ACARS errou; o ajuste fica registrado.",
    h("div", {}, h("div", {class: "row"}, field("Bloco (min)", b), field("Voo (min)", a), field("Noturno (min)", n), field("Pousos", l)),
      touchdownFields(vs, gg, hard, d.landing_vs_fpm == null ? "A IVAO não publica o toque: informe o que o seu simulador ou add-on mostrou." : "Medido pelo ACARS; corrija se o seu add-on mostrou outro valor.",
        d.type_icao || (p.flight && p.flight.type_icao)),
      field("Observação no diário", rm), field("Justificativa", notes)),
    [closeBtn, {label: "Aprovar", fn: async () => {
      const ov = {};
      if (b.value !== "" && +b.value !== d.block_min) ov.block_min = +b.value;
      if (a.value !== "" && +a.value !== d.air_min) ov.air_min = +a.value;
      if (n.value !== "" && +n.value !== d.night_min) ov.night_min = +n.value;
      if (+l.value !== 1) ov.landings = +l.value;
      if (rm.value) ov.remarks = rm.value;
      if (vs.value === "" || gg.value === "") { toast("Informe a razão (fpm) e o G do toque.", true); return false; }
      if (+vs.value !== d.landing_vs_fpm) ov.landing_vs_fpm = +vs.value;
      if (+gg.value !== d.landing_g) ov.landing_g = +gg.value;
      if (hard.checked) ov.hard_landing_reported = true;
      if (!await act(() => post(`/api/pireps/${p.id}/approve`, {overrides: ov, notes: notes.value || null}), "PIREP aprovado")) return false;
      render();
    }}]);
}
function reasonDialog(title, sub, label, fn, okMsg) {
  const r = h("input");
  dialog(title, sub, field(label, r), [closeBtn, {label: "Confirmar", cls: "stop", fn: async () => {
    if (!r.value.trim()) { toast("Escreva o motivo.", true); return false; }
    if (!await act(() => fn(r.value.trim()), okMsg)) return false;
    render();
  }}]);
}

/* ───────────────────────── diário ───────────────────────── */
view("diario", async main => {
  const d = await get("/api/logbook"), t = d.totals;
  const figs = h("div", {class: "figs"}, [["Voos", t.flights, v => Math.round(v)], ["Bloco", t.block_min, hm], ["Voo", t.air_min, hm],
    ["Noturno", t.night_min, hm], ["Pousos", t.landings, v => Math.round(v)], ["Toque médio", t.avg_vs_fpm, v => v == null ? "—" : Math.round(v) + " fpm"],
    ["Melhor toque", t.best_vs_fpm, v => v == null ? "—" : Math.round(v) + " fpm"], ["Pousos duros", t.hard_landings, v => Math.round(v || 0)]].map(([l, v, f]) => {
      const b = h("b", {}, f(v));
      if (v != null) countUp(b, v, f);
      return h("div", {class: "fig"}, b, h("span", {}, l));
    }));
  put(main, h("div", {class: "head"}, h("div", {}, h("h1", {}, "Diário de bordo"), h("div", {class: "sub"}, "Voos aprovados por você. Registro de simulação.")),
      h("div", {class: "tools"}, h("a", {class: "btn ghost sm", href: "/api/diario.csv", download: ""}, "Exportar CSV (Excel)"))), figs,
    d.entries.length ? h("div", {class: "panel"}, h("div", {class: "tbl"}, h("table", {},
      h("thead", {}, h("tr", {}, ["Data", "Voo", "Trecho", "Aeronave", "Tipo", "Bloco", "Voo", "Noite", "Toque", "Consumo", ""].map((x, k) => h("th", {class: k > 4 && k < 10 ? "n" : ""}, x)))),
      h("tbody", {}, d.entries.map(l => h("tr", {title: l.remarks || ""}, h("td", {}, dmy(l.date)),
        h("td", {}, l.flight_number || "—", l.source === "MANUAL" ? [" ", chip("declarado", "hold", "Lançado por você no “já fiz esse voo”")] : null,
          l.time_basis === "ESCALA" ? [" ", chip("horário da escala", "cool", "Voado em outro momento e registrado nos horários da escala")]
            : l.sched_out_utc ? [" ", chip("fora do horário", "", `Na escala: ${dm(l.sched_out_utc)} ${hhmmz(l.sched_out_utc)}`)] : null,
          l.network && l.network !== "OFFLINE" ? [" ", chip(l.network, "cool")] : null), h("td", {class: "t"}, `${l.adep}→${l.ades}`),
        h("td", {class: "t"}, l.registration || "—"), h("td", {class: "t"}, l.type_icao || "—"), h("td", {class: "n"}, hm(l.block_min)), h("td", {class: "n"}, hm(l.air_min)),
        h("td", {class: "n"}, hm(l.night_min)), h("td", {class: "n" + (l.landing_vs_fpm != null && (Math.abs(l.landing_vs_fpm) >= 600 || l.landing_g >= 2.2) ? " over" : "")},
          l.landing_vs_fpm == null ? "—" : `${l.landing_vs_fpm} fpm${l.landing_g ? " · " + l.landing_g.toFixed(2) + " G" : ""}`), h("td", {class: "n"}, kg(l.fuel_used_kg)),
        h("td", {}, l.source === "MANUAL" ? h("button", {class: "btn ghost sm", onclick: () => reasonDialog("Estornar o voo declarado",
          "O lançamento e as horas da aeronave ficam marcados como estornados.", "Motivo", r => post(`/api/logbook/${l.id}/reverse`, {reason: r}), "Lançamento estornado")}, "Estornar") : null)))))))
      : h("div", {class: "empty"}, icon("diario", 26), h("div", {}, "Ainda sem voos aprovados.")));
});

/* ───────────────────────── presença: relógio, CCO, IVAO e "já fiz esse voo" ───────────────────────── */
const greet = () => { const x = new Date().getHours(); return x < 5 ? "Boa noite" : x < 12 ? "Bom dia" : x < 18 ? "Boa tarde" : "Boa noite"; };
let clockTimer = null;
function liveClock() {
  const el = h("span", {class: "clock"});
  const tick = () => { const n = new Date(); el.textContent = `${n.toISOString().slice(11, 19)}Z · ${n.toLocaleTimeString("pt-BR", {hour: "2-digit", minute: "2-digit"})} aqui`; };
  tick();
  clearInterval(clockTimer);
  clockTimer = setInterval(() => { if (!el.isConnected) clearInterval(clockTimer); else tick(); }, 1000);
  return el;
}
const ago = t => { const x = (Date.now() - Date.parse(t)) / 1000; return x < 60 ? "agora" : x < 3600 ? `há ${Math.round(x / 60)} min` : x < 86400 ? `há ${Math.round(x / 3600)} h` : dmy(t); };
function ccoPanel() {
  const body = h("div", {class: "cco"}, h("div", {class: "skel s"}));
  get("/api/ops-feed").then(list => put(body, list.length ? list.map((m, i) => h("div", {class: "msg", style: `--i:${i}`},
      h("span", {class: "ic"}, icon(m.icon, 15)), h("div", {}, h("span", {class: "from"}, "CCO"), m.text), h("small", {title: dt(m.t)}, ago(m.t))))
    : h("p", {class: "muted", style: "margin:0"}, "Nada por aqui ainda. Reserve uma perna da escala e o CCO acompanha a sua operação daqui.")))
    .catch(() => put(body, note("hold", "alerta", "Não foi possível ler as mensagens do CCO.")));
  return h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Centro de operações"),
    h("span", {class: "muted", style: "font-size:13px"}, "o que aconteceu na sua operação")), body);
}
const pulseCache = {key: null, at: 0, data: null, err: null};
async function getPulse(airports) {
  const key = (airports || []).join(",");
  if (Date.now() - pulseCache.at < 60000 && (pulseCache.key === key || (pulseCache.err && !pulseCache.data))) {
    if (pulseCache.err && !pulseCache.data) throw new Error(pulseCache.err);
    return pulseCache.data;
  }
  try {
    const d = await get(`/api/ivao/pulse?airports=${encodeURIComponent(key)}`);
    Object.assign(pulseCache, {key, at: Date.now(), data: d, err: null});
    return d;
  } catch (e) { Object.assign(pulseCache, {key, at: Date.now(), err: e.message}); throw e; }
}
function railPulse(el) {
  const show = p => put(el, h("span", {class: "dot"}), h("span", {}, h("b", {}, p.airline_count), ` ${p.airline} na IVAO`), h("small", {}, `${p.totals.pilots.toLocaleString("pt-BR")} pilotos · ${p.totals.atcs} ATC`));
  if (pulseCache.data) return show(pulseCache.data);
  getPulse(pulseCache.key ? pulseCache.key.split(",").filter(Boolean) : []).then(show).catch(() => put(el, h("span", {class: "dot off"}), h("span", {}, "IVAO fora do ar")));
}
const fig = (l, v) => { const b = h("b", {}, v == null ? "—" : String(v)); if (typeof v === "number") countUp(b, v, x => Math.round(x).toLocaleString("pt-BR")); return h("div", {class: "fig"}, b, h("span", {}, l)); };
function networkPanel(airports, title) {
  const body = h("div", {}, h("div", {class: "skel s"}));
  getPulse(airports).then(p => put(body,
    p.me ? note("or", "live", h("b", {}, "Você está na IVAO: "), `${p.me.callsign} ${p.me.dep || "?"}→${p.me.arr || "?"}${p.me.state ? " · " + p.me.state : ""} `,
      h("a", {href: p.me.webeye, target: "_blank", rel: "noopener noreferrer"}, "ver no Webeye")) : null,
    h("div", {class: "figs mini"}, fig(`Voos ${p.airline} agora`, p.airline_count), fig("Pilotos na IVAO", p.totals.pilots), fig("Controladores", p.totals.atcs)),
    airports.length ? h("div", {class: "atc"}, h("h3", {}, "Controle nos seus aeroportos"),
      p.atc.length ? h("div", {class: "tools"}, p.atc.map(a => h("a", {class: "chip go", href: a.webeye, target: "_blank", rel: "noopener noreferrer", title: a.position || ""},
        h("span", {class: "dot"}), `${a.callsign}${a.frequency ? " · " + Number(a.frequency).toFixed(3) : ""}`)))
        : h("p", {class: "muted", style: "font-size:13.5px;margin:0"}, `Ninguém controlando ${airports.join(", ")} agora.`),
      Object.keys(p.traffic).length ? h("p", {class: "fine", style: "margin:6px 0 0"}, Object.entries(p.traffic).map(([k, v]) => `${k}: ${v.dep} saindo, ${v.arr} chegando`).join(" · ")) : null) : null,
    p.airline_flights.length ? h("div", {class: "tbl", style: "margin-top:10px"}, h("table", {}, h("tbody", {}, p.airline_flights.slice(0, 8).map(x => h("tr", {},
      h("td", {}, h("a", {href: x.webeye, target: "_blank", rel: "noopener noreferrer"}, x.callsign)), h("td", {class: "t"}, `${x.dep || "?"}→${x.arr || "?"}`),
      h("td", {class: "n"}, x.on_ground ? "no solo" : x.alt_ft != null ? "FL" + String(Math.round(x.alt_ft / 100)).padStart(3, "0") : "—"),
      h("td", {class: "muted"}, x.state || "")))))) : h("p", {class: "muted", style: "font-size:13.5px;margin-top:10px"}, `Nenhum voo ${p.airline} na IVAO neste momento.`),
    h("p", {class: "fine", style: "margin:8px 0 0"}, `Whazzup público da IVAO, lido pelo servidor${p.fetched_at ? " às " + hhmmz(p.fetched_at) : ""}${p.stale ? " (última leitura boa: a IVAO não respondeu agora)" : ""}.`)))
    .catch(e => put(body, note("hold", "alerta", e.message || "IVAO indisponível agora.")));
  return h("div", {class: "panel net-panel"}, h("div", {class: "panel-head"}, h("h2", {}, title || "Na rede agora"),
    h("span", {class: "tools"}, h("a", {href: "#/rede", class: "muted", style: "font-size:13px"}, "ver a Rede"), h("span", {class: "radar", "aria-hidden": "true"}))), body);
}
const legTarget = l => ({flight_id: l.flight_id || null, leg_id: l.id || l.leg_id, flight_number: l.flight_number, adep: l.adep, ades: l.ades,
  dep_utc: l.dep_utc, arr_utc: l.arr_utc, aircraft_id: l.aircraft_id || null});
const flightTarget = f => ({flight_id: f.id, flight_number: f.flight_number, adep: f.adep, ades: f.ades, dep_utc: f.dep_utc, arr_utc: f.arr_utc});
function manualDialog(t, fleet) {
  (fleet ? Promise.resolve(fleet) : get("/api/aircraft")).then(list => {
    const loc = x => x ? x.slice(0, 16) : "";
    const i = {out: h("input", {type: "datetime-local", value: loc(t.dep_utc)}), off: h("input", {type: "datetime-local"}),
      on: h("input", {type: "datetime-local"}), in_: h("input", {type: "datetime-local", value: loc(t.arr_utc)}),
      net: h("select", {}, [["OFFLINE", "Offline"], ["IVAO", "IVAO"], ["VATSIM", "VATSIM"], ["POSCON", "POSCON"], ["OUTRA", "Outra"]].map(([v, l]) => h("option", {value: v}, l))),
      land: h("input", {type: "number", min: 1, max: 10, value: 1}), fuel: h("input", {type: "number", min: 0, placeholder: "opcional"}),
      vs: h("input", {type: "number", placeholder: "ex.: -180", required: true}), gg: h("input", {type: "number", step: "0.01", placeholder: "ex.: 1.25", required: true}),
      hard: h("input", {type: "checkbox"}), rm: h("input", {placeholder: "opcional"})};
    const sel = h("select", {}, h("option", {value: ""}, "Sem aeronave da frota"),
      list.map(a => h("option", {value: a.id, selected: a.id === t.aircraft_id}, `${a.registration} · ${a.type_icao}`)));
    const force = h("input", {type: "checkbox"});
    // ignorar o horário da escala: voou antes (ou depois) da hora programada
    const future = !!t.dep_utc && Date.parse(t.dep_utc) > Date.now();
    const isoMin = ms => new Date(ms).toISOString().slice(0, 16);
    const blk = (Date.parse(t.arr_utc) - Date.parse(t.dep_utc)) || 7200e3;
    const fillNow = () => { const end = Math.floor(Date.now() / 60000) * 60000 - 5 * 60000; i.in_.value = isoMin(end); i.out.value = isoMin(end - blk); i.off.value = ""; i.on.value = ""; };
    const fillSched = () => { i.out.value = loc(t.dep_utc); i.in_.value = loc(t.arr_utc); i.off.value = ""; i.on.value = ""; };
    const ign = h("input", {type: "checkbox", checked: future});
    const mReal = h("input", {type: "radio", name: "tb-mode", value: "real", checked: true});
    const mEsc = h("input", {type: "radio", name: "tb-mode", value: "escala"});
    const modeBox = h("div", {class: "tb-box", hidden: !future},
      h("label", {class: "ck"}, mReal, h("span", {}, h("b", {}, "Usar o horário em que voei"), " · preenchido com agora; ajuste se voou em outro momento. A partida programada fica anotada no diário.")),
      h("label", {class: "ck"}, mEsc, h("span", {}, h("b", {}, "Registrar nos horários da escala"), " · conta como voado no dia programado (salário, escala), mesmo no futuro. O diário marca “no horário da escala”.")));
    ign.addEventListener("change", () => { modeBox.hidden = !ign.checked; ign.checked && mReal.checked ? fillNow() : fillSched(); });
    mReal.addEventListener("change", fillNow); mEsc.addEventListener("change", fillSched);
    if (future) fillNow();
    dialog(`Já fiz o ${t.flight_number || "voo"}`, `${t.adep} → ${t.ades}. Horários em UTC` + (future
        ? `. Na escala este voo é ${dm(t.dep_utc)} às ${hhmmz(t.dep_utc)}: como ainda não chegou a hora, o horário da escala está sendo ignorado.`
        : ", já preenchidos com os da escala: ajuste para o que você voou de verdade.") + " Entra no diário como voo declarado por você.",
      h("div", {}, field("Aeronave", sel),
        h("label", {class: "ck"}, ign, "Ignorar horário da escala (voei antes ou depois da hora programada)"), modeBox,
        h("div", {class: "row"}, field("Calços fora (OUT)", i.out), field("Calços (IN)", i.in_)),
        h("div", {class: "row"}, field("Decolagem (OFF) · opcional", i.off), field("Pouso (ON) · opcional", i.on)),
        h("div", {class: "row"}, field("Rede", i.net), field("Pousos", i.land), field("Combustível usado (kg)", i.fuel)),
        touchdownFields(i.vs, i.gg, i.hard, "Obrigatório em todo voo: o que o seu simulador ou add-on de pouso mostrou.",
          () => { const a = list.find(x => String(x.id) === sel.value); return (a && a.type_icao) || t.type_icao; }),
        field("Observação", i.rm), h("label", {class: "ck"}, force, "Aceitar aeronave de tipo diferente do planejado")),
      [closeBtn, {label: "Lançar no diário", fn: async () => {
        const body = {out_utc: i.out.value, in_utc: i.in_.value, off_utc: i.off.value || null, on_utc: i.on.value || null, network: i.net.value,
          landings: +i.land.value || 1, fuel_used_kg: i.fuel.value === "" ? null : +i.fuel.value, landing_vs_fpm: i.vs.value === "" ? null : +i.vs.value,
          landing_g: i.gg.value === "" ? null : +i.gg.value, hard_landing_reported: i.hard.checked,
          remarks: i.rm.value || null, force_type: force.checked, ignore_schedule: ign.checked ? (mEsc.checked ? "escala" : "real") : null};
        if (sel.value) body.aircraft_id = +sel.value;
        if (body.landing_vs_fpm == null || body.landing_g == null) { toast("Informe a razão (fpm) e o G do toque.", true); return false; }
        const r = await act(() => post(t.flight_id ? `/api/flights/${t.flight_id}/manual` : `/api/roster/${t.leg_id}/manual`, body), "No diário. Bom voo, comandante.");
        if (!r) return false;
        (r.warnings || []).forEach(w => toast(w));
        render();
      }}]);
  });
}
function stayPanel() {
  const body = h("div", {}, h("p", {class: "muted"}, "Carregando…"));
  const edit = (icao, cur) => {
    const i = {hotel: h("input", {value: (cur && cur.hotel) || "", placeholder: "nome do hotel de tripulação"}),
      tr: h("input", {value: (cur && cur.transport) || "", placeholder: "ex.: van no desembarque, 20 min até o hotel"}),
      nt: h("textarea", {rows: 3, placeholder: "opcional: restaurante perto, academia, o que der"}, (cur && cur.notes) || ""),
      src: h("input", {value: (cur && cur.source) || "", placeholder: "cadastro manual"})};
    dialog(`Pernoite em ${icao}`, "Vai nas mensagens de pernoite do Controle de Tripulantes. Deixe tudo em branco para apagar.",
      h("div", {}, h("div", {class: "tools", style: "margin-bottom:10px"},
          h("button", {class: "btn sm", type: "button", onclick: () => setTimeout(() => hotelPicker(icao, load), 0)}, icon("decolar", 15), "Buscar hotéis no OpenStreetMap")),
        field("Hotel", i.hotel), field("Combinado do transporte (opcional)", i.tr), field("Observações", i.nt), field("Fonte", i.src)),
      [closeBtn, {label: "Salvar", fn: async () => {
        const r = await act(() => api("PUT", `/api/airport-info/${icao}`, {hotel: i.hotel.value, transport: i.tr.value, notes: i.nt.value, source: i.src.value}), "Pernoite salvo");
        if (!r) return false; load();
      }}]);
  };
  const load = () => get("/api/airport-info").then(d => put(body,
    d.items.length ? h("div", {class: "tbl"}, h("table", {}, h("tbody", {}, d.items.map(x => h("tr", {},
      h("td", {class: "t"}, x.icao), h("td", {}, (x.place && x.place.city) || ""), h("td", {}, x.hotel || h("span", {class: "muted"}, "sem hotel")),
      h("td", {class: "muted"}, x.route_min != null ? `${x.route_min} min · ${String(x.route_km).replace(".", ",")} km` : x.hotel_lat != null ? "rota não calculada" : (x.transport || "")),
      h("td", {}, h("span", {class: "tools", style: "flex-wrap:nowrap"},
        h("button", {class: "btn ghost sm", onclick: () => hotelPicker(x.icao, load)}, "Buscar"),
        x.hotel_lat != null ? h("button", {class: "btn ghost sm", onclick: async () => { if (await act(() => post(`/api/stay/${x.icao}/route`), "Rota recalculada")) load(); }}, "Rota") : null,
        h("button", {class: "btn ghost sm", onclick: () => edit(x.icao, x)}, "Editar")))))))) : null,
    d.suggest.length ? h("div", {style: "margin-top:12px"}, h("div", {class: "fine", style: "margin-bottom:6px"}, "Destinos da sua escala sem cadastro:"),
      h("div", {class: "tools"}, d.suggest.slice(0, 16).map(x => h("button", {class: "chip", style: "cursor:pointer", title: (x.place && x.place.city) || "",
        onclick: () => edit(x.icao)}, "+ " + x.icao)))) : null,
    h("div", {class: "tools", style: "margin-top:12px"}, h("button", {class: "btn ghost sm", onclick: () => {
      const ic = h("input", {maxlength: 4, placeholder: "ex.: SBRF", style: "text-transform:uppercase"});
      dialog("Outro aeroporto", "ICAO de 4 letras.", field("ICAO", ic), [closeBtn, {label: "Continuar", fn: () => {
        const v = ic.value.trim().toUpperCase(); if (v.length !== 4) { toast("ICAO tem 4 letras.", true); return false; } setTimeout(() => edit(v), 0); }}]); }}, "Outro aeroporto")),
    h("p", {class: "fine"}, d.note + " Hotéis: © colaboradores do OpenStreetMap (ODbL). Rotas: OSRM."))).catch(e => put(body, note("hold", "alerta", e.message)));
  load();
  const cfgBox = h("div", {style: "margin-top:16px"});
  get("/api/settings").then(cfg => {
    const tr = h("select", {}, [["van", "Van da tripulação"], ["app", "Carro por aplicativo"]].map(([v, l]) => h("option", {value: v, selected: (cfg.stay_transport || "van") === v}, l)));
    const bf = h("input", {type: "number", min: 0, max: 90, value: cfg.stay_buffer_min || 15});
    const wk = h("input", {type: "number", min: 15, max: 180, value: cfg.stay_wakeup_min || 60});
    put(cfgBox, h("h3", {style: "margin:0 0 8px"}, "Transporte e despertar"),
      h("div", {class: "row"}, field("Transporte", tr), field("Folga antes da apresentação (min)", bf), field("Despertar antes do carro (min)", wk)),
      h("div", {class: "tools"}, h("button", {class: "btn sm", onclick: async () => {
        if (await act(() => api("PUT", "/api/settings", {stay_transport: tr.value, stay_buffer_min: String(bf.value), stay_wakeup_min: String(wk.value)}), "Salvo")) prefs = null; }}, "Salvar"),
        h("span", {class: "fine"}, "Saída do hotel = apresentação − rota − folga. Despertar = saída − antecedência. Parâmetros seus, não regra da lei.")));
  }).catch(() => {});
  return h("div", {class: "panel"}, h("h2", {}, "Pernoites"), h("p", {class: "muted"},
    "O hotel de cada aeroporto onde você pernoita, escolhido no OpenStreetMap, com a rota de carro calculada pelo OSRM. O LineOps usa isso para agendar o carro, o check-in e o despertar."), body, cfgBox);
}
/* critérios de pouso duro por fabricante (os mesmos do servidor: landing.py) */
const AIRBUS_NB = ["A318", "A319", "A320", "A321", "A19N", "A20N", "A21N"], BOEING_WB = ["B762", "B763", "B764", "B772", "B773", "B77L", "B77W", "B788", "B789", "B78X"];
function hardLimits(type) {
  const t = (type || "").toUpperCase();
  if (AIRBUS_NB.includes(t)) return {vs: 540, g: 2.6, sg: 3.0, label: "relatório LOAD<15> do A320: 540 fpm ou 2,6 G"};
  if (BOEING_WB.includes(t) || ["A332", "A333", "A338", "A339", "A359", "A35K"].includes(t)) return {vs: 600, g: 99, sg: 99, label: "limite de projeto do trem: 600 fpm"};
  if (["AT43", "AT45", "AT46", "AT72", "AT75", "AT76"].includes(t)) return {vs: 600, g: 99, sg: 99, label: "limite de energia do trem do ATR: 600 fpm"};
  if (["E170", "E175", "E190", "E195", "E290", "E295"].includes(t)) return {vs: 600, g: 99, sg: 99, label: "limite de projeto do trem: 600 fpm"};
  return {vs: 600, g: 2.2, sg: 2.6, label: "AMM 05-51 do 737: 600 fpm ou 2,2 G"};
}
function touchdownFields(vs, gg, hard, hint, type) {
  const tag = h("span", {class: "td-tag"});
  const getType = typeof type === "function" ? type : () => type;
  const upd = () => {
    const v = Math.abs(+vs.value), g = +gg.value, L = hardLimits(getType());
    if (vs.value === "" || gg.value === "") return put(tag);
    const [t, c] = v >= 900 || g >= L.sg ? ["pouso duro severo · inspeção", "stop"] : v >= L.vs || g > L.g ? [`pouso duro · inspeção (${L.label})`, "stop"]
      : v >= 360 || g >= 1.6 ? ["firme", "hold"] : v >= 240 || g >= 1.4 ? ["normal", ""] : v >= 100 ? ["suave", "go"] : ["manteiga (cuidado com flare longo)", "cool"];
    put(tag, chip(t, c));
  };
  vs.addEventListener("input", upd); gg.addEventListener("input", upd); upd();
  return h("div", {class: "td-box"}, h("h3", {}, "Toque"),
    h("div", {class: "row"}, field("Razão no toque (fpm)", vs), field("G no toque", gg)),
    h("div", {class: "tools", style: "margin:-4px 0 8px"}, tag, h("span", {class: "fine"}, hint)),
    h("label", {class: "ck"}, hard, "Reportei pouso duro (a Boeing considera o relato da tripulação o melhor critério: vai para inspeção)"));
}
async function ivaoStart(fid, confirm) {
  try {
    await post(`/api/flights/${fid}/ivao/start`, {confirm_restrictions: !!confirm});
    toast("Acompanhamento ligado. Conecte na IVAO e bom voo!");
    render();
  } catch (e) {
    if (e.body && e.body.error === "confirmacao_necessaria") {
      dialog("Restrições da aeronave", "Confirme que você leu antes de voar.",
        h("div", {}, ((e.body.details || {}).restrictions || []).map(r => note("hold", "alerta", r.title + (r.text ? ": " + r.text : "")))),
        [closeBtn, {label: "Li, iniciar", fn: () => ivaoStart(fid, true)}]);
    } else if (e.status !== 401) toast(e.message, true);
  }
}
function ivaoLiveBox(c) {
  const s = c.session, box = h("div", {style: "width:100%"});
  const refresh = async () => { const r = await act(() => post(`/api/acars/sessions/${s.id}/ivao/refresh`)); if (!r) return; if (r.status !== "LIVE") return render(); paint(r.snapshot || {}, r.last_seen_at); };
  const finish = async () => { if (await act(() => post(`/api/acars/sessions/${s.id}/ivao/finish`, {}), "PIREP enviado para revisão")) render(); };
  const abort = () => reasonDialog("Cancelar acompanhamento", "O voo volta para reservado; a trilha gravada fica guardada.", "Motivo",
    r => post(`/api/acars/sessions/${s.id}/abort`, {reason: r}), "Acompanhamento cancelado");
  const paint = (x, seen) => put(box,
    note("or", "live", h("b", {}, x.ivao_callsign ? `${x.ivao_callsign} na IVAO` : "Esperando você conectar na IVAO"),
      x.ivao_state ? ` · ${x.ivao_state}` : "", x.ivao_online && x.alt_ft != null ? ` · ${Math.round(x.alt_ft).toLocaleString("pt-BR")} ft · ${x.gs_kt ?? "—"} kt` : "",
      x.ivao_online === false ? " · sem sinal agora" : "", seen ? ` · lido ${hhmmz(seen)}` : ""),
    h("div", {class: "ooi"}, [["OUT", x.out_utc], ["OFF", x.off_utc], ["ON", x.on_utc], ["IN", x.in_utc]].map(([k, v]) =>
      h("div", {class: v ? "on" : ""}, h("span", {}, k), h("b", {}, v ? hhmmz(v) : "—")))),
    h("div", {class: "tools"},
      x.ivao_callsign ? h("a", {class: "btn ghost sm", href: `https://webeye.ivao.aero/?callsign=${encodeURIComponent(x.ivao_callsign)}`, target: "_blank", rel: "noopener noreferrer"},
        icon("decolar", 15), "Ver no Webeye") : null,
      h("button", {class: "btn ghost sm", onclick: refresh}, "Atualizar agora"),
      x.out_utc ? h("button", {class: "btn sm", onclick: finish}, "Encerrar e enviar PIREP") : null,
      h("button", {class: "btn stop sm", onclick: abort}, "Cancelar")));
  paint(s.snapshot || {}, s.last_seen_at);
  const timer = setInterval(async () => {
    if (!box.isConnected) return clearInterval(timer);
    try {
      const L = await get("/api/acars/live"), cur = L.find(x => x.id === s.id);
      if (!cur) { clearInterval(timer); render(); } else paint(cur.snapshot || {}, cur.last_seen_at);
    } catch (e) { /* tenta de novo no próximo ciclo */ }
  }, 15000);
  return box;
}

/* ───────────────────────── Rede: os outros pilotos da companhia na IVAO ───────────────────────── */
const net = {filter: "todos", data: null, timer: null, friendsOnline: null};
const HUBS = new Set(["SBGR", "SBSP", "SBRJ", "SBGL", "SBBR", "SBCF", "SBKP", "SBSV", "SBRF", "SBPA", "SBCT", "SBFZ", "SBEG", "SBBE", "SBFL", "SBCY", "SBGO", "SBVT", "SBNT", "SBMO"]);
const fl = ft => ft == null ? "—" : "FL" + String(Math.round(ft / 100)).padStart(3, "0");
const who = c => c.friend && c.friend.nickname ? c.friend.nickname : `VID ${c.vid}`;

view("rede", async main => {
  const filt = h("div", {class: "seg"}, [["todos", "Todos"], ["ar", "No ar"], ["solo", "No solo"], ["colegas", "Colegas"]].map(([k, l]) =>
    h("button", {"aria-pressed": net.filter === k, onclick: e => { net.filter = k; filt.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b === e.currentTarget))); paintNet(); }}, l)));
  const live = h("span", {class: "chip or live"}, "ao vivo");
  put(main, h("div", {class: "head"}, h("div", {}, h("h1", {}, "Rede"),
      h("div", {class: "sub"}, "Os outros pilotos da companhia voando agora na IVAO, e quem você mais encontra por lá.")),
    h("div", {class: "tools"}, live, filt)), h("div", {id: "net-body"}, skeleton("l", "h")));
  await loadNet();
  clearInterval(net.timer);
  net.timer = setInterval(() => { if (!document.getElementById("net-body")) return clearInterval(net.timer); if (document.visibilityState === "visible") loadNet(true); }, 60000);
});

async function loadNet(quiet) {
  try { net.data = await get("/api/network"); watchFriends(net.data); paintNet(); }
  catch (e) { if (!quiet) { const b = document.getElementById("net-body"); if (b) put(b, note("hold", "alerta", e.message || "IVAO indisponível agora.")); } }
}

function paintNet() {
  const body = document.getElementById("net-body"), d = net.data;
  if (!body || !d) return;
  const pass = c => net.filter === "ar" ? c.on_ground === false : net.filter === "solo" ? c.on_ground !== false : net.filter === "colegas" ? !!c.friend : true;
  const list = d.pilots.filter(c => !c.is_me && pass(c));
  const me = d.pilots.find(c => c.is_me) || d.me;
  put(body,
    !d.vid ? note("", "info", "Salve o seu VID em ", h("a", {href: "#/ajustes"}, "Ajustes → IVAO"), " para aparecer destacado e para o LineOps cruzar os seus voos com os dos outros.") : null,
    d.stale ? note("hold", "alerta", "A IVAO não respondeu agora; mostrando a última leitura boa.") : null,
    h("div", {class: "figs"}, fig(`${d.airline} online`, d.counts.online), fig("No ar", d.counts.airborne), fig("No solo", d.counts.ground),
      fig("Pilotos hoje", d.today.pilots), fig("Chegaram hoje", d.today.arrived)),
    h("div", {class: "cols-2", style: "margin-top:18px"},
      h("div", {},
        me ? h("div", {class: "panel me-card"}, h("div", {class: "panel-head"}, h("h2", {}, "Você na rede"), chip("online", "go")), pilotCard(me, true)) : null,
        h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Radar"), h("span", {class: "muted", style: "font-size:13px"}, `lido ${d.updated_at ? hhmmz(d.updated_at) : "—"}`)),
          radarMap(d)),
        list.length ? h("div", {class: "pilots"}, list.map(c => pilotCard(c)))
          : h("div", {class: "empty"}, icon("rede", 26), h("div", {}, net.filter === "colegas" ? "Nenhum colega online agora." : `Nenhum piloto ${d.airline} nesta lista agora.`))),
      h("div", {},
        friendsPanel(d), frequentPanel(d),
        h("div", {class: "panel"}, h("h2", {}, "Agora na rede"), h("div", {class: "hr"}),
          d.routes.length ? h("div", {}, h("h3", {style: "margin-bottom:8px"}, "Rotas"), h("div", {class: "tools"}, d.routes.map(([r, n]) => chip(`${r}${n > 1 ? " ×" + n : ""}`, "or")))) : h("p", {class: "muted"}, "Sem rotas no momento."),
          d.airports.length ? h("div", {style: "margin-top:14px"}, h("h3", {style: "margin-bottom:8px"}, `Aeroportos com mais ${(me.career || {}).name || "voos da companhia"}`),
            h("div", {class: "tools"}, d.airports.map(([a, n]) => chip(`${a} · ${n}`, "")))) : null,
          h("p", {class: "fine", style: "margin:12px 0 0"}, `${d.counts.network_pilots.toLocaleString("pt-BR")} pilotos na IVAO agora. ${d.source}.`)))));
}

function pilotCard(c, big, bare) {
  const dep = c.dep || {}, arr = c.arr || {};
  const fav = c.friend;
  const card = h("article", {class: "pcard" + (c.on_ground === false ? " air" : "") + (fav ? " fav" : "") + (big ? " big" : "")},
    h("div", {class: "pc-top"},
      h("a", {class: "pc-cs", href: c.webeye, target: "_blank", rel: "noopener noreferrer", title: "Abrir no Webeye"}, c.callsign),
      fav ? h("span", {class: "pc-nick"}, icon("estrela", 13), fav.nickname || `VID ${c.vid}`) : null,
      c.phase ? chip(c.phase, c.on_ground === false ? "or" : "") : null,
      h("span", {class: "pc-meta"}, [`VID ${c.vid}`, c.rating_label, c.aircraft, c.simulator].filter(Boolean).join(" · "))),
    h("div", {class: "pc-route"},
      h("div", {}, h("b", {}, dep.icao || "?"), h("small", {}, dep.city || "")),
      h("div", {class: "pc-bar", title: c.progress != null ? `${c.progress}% do caminho (estimativa pela distância)` : "progresso desconhecido"},
        h("i", {style: `width:${c.progress ?? 0}%`}), c.progress != null ? h("span", {class: "pc-plane", style: `left:${c.progress}%`}, icon("voos", 14)) : null),
      h("div", {class: "r"}, h("b", {}, arr.icao || "?"), h("small", {}, arr.city || ""))),
    h("div", {class: "pc-facts"},
      c.on_ground === false ? `${fl(c.alt_ft)} · ${c.gs_kt} kt` : `no solo${c.gs_kt > 2 ? ` · taxiando ${c.gs_kt} kt` : ""}`,
      c.arr_nm != null ? ` · ${c.arr_nm} nm para ${arr.icao || "o destino"}` : "",
      c.eta_utc ? ` · chega ~${hhmmz(c.eta_utc)}` : "",
      c.online_since ? ` · online desde ${hhmmz(c.online_since)}` : ""),
    c.relations && c.relations.length ? h("div", {class: "tools", style: "margin-top:8px"}, c.relations.map(r => chip(r, "cool"))) : null,
    c.seen_30d && !c.is_me ? h("div", {class: "pc-seen"}, `Visto em ${c.seen_30d.days} dia(s) nos últimos 30`) : null,
    !c.is_me && !bare ? h("div", {class: "tools pc-acts"},
      h("button", {class: "btn ghost sm", onclick: () => pilotDialog(c.vid, c)}, "Histórico"),
      h("button", {class: "btn ghost sm" + (fav ? " on" : ""), onclick: () => friendDialog(c.vid, fav, c.callsign)}, icon("estrela", 14), fav ? "Colega" : "Adicionar aos colegas")) : null);
  return card;
}

function radarMap(d) {
  const W = 720, H = 520, pad = 18;
  const pts = d.map_airports.map(a => [a[1], a[2]]).concat(d.pilots.filter(c => c.lat != null).map(c => [c.lat, c.lon]));
  if (!pts.length) return h("div", {class: "empty"}, "Sem posições.");
  const lat0 = pts.reduce((s, p) => s + p[0], 0) / pts.length, k = Math.cos(lat0 * Math.PI / 180);
  const xs = pts.map(p => p[1] * k), ys = pts.map(p => -p[0]);
  const mnx = Math.min(...xs), mxx = Math.max(...xs), mny = Math.min(...ys), mxy = Math.max(...ys);
  const sc = Math.min((W - 2 * pad) / ((mxx - mnx) || 1), (H - 2 * pad) / ((mxy - mny) || 1));
  const ox = pad + ((W - 2 * pad) - (mxx - mnx) * sc) / 2, oy = pad + ((H - 2 * pad) - (mxy - mny) * sc) / 2;
  const P = (lat, lon) => [ox + (lon * k - mnx) * sc, oy + (-lat - mny) * sc];
  const s = svg("svg", {class: "radar-map", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `Posição de ${d.pilots.length} voos ${d.airline} na IVAO`});
  const cx = W / 2, cy = H / 2;
  [90, 170, 250].forEach(r => s.append(svg("circle", {class: "ring", cx, cy, r})));
  s.append(svg("line", {class: "ring", x1: 0, y1: cy, x2: W, y2: cy}), svg("line", {class: "ring", x1: cx, y1: 0, x2: cx, y2: H}));
  const sweep = svg("g", {class: "sweep"}); sweep.append(svg("path", {d: `M${cx} ${cy} L${cx + 420} ${cy - 60} A 425 425 0 0 1 ${cx + 425} ${cy} Z`}));
  s.append(sweep);
  const used = new Set(d.pilots.flatMap(c => [(c.dep || {}).icao, (c.arr || {}).icao]));
  d.map_airports.forEach(([ic, la, lo]) => {
    const [x, y] = P(la, lo), hot = used.has(ic);
    s.append(svg("circle", {class: "ap" + (hot ? " hot" : ""), cx: x, cy: y, r: hot ? 3 : 1.6}));
    if (hot || HUBS.has(ic)) s.append(svg("text", {class: "apl" + (hot ? " hot" : ""), x: x + 5, y: y - 4}, ic));
  });
  d.pilots.filter(c => c.lat != null).forEach(c => {
    const [x, y] = P(c.lat, c.lon), arr = c.arr || {};
    const cls = "fl" + (c.is_me ? " me" : c.friend ? " fav" : c.on_ground === false ? " air" : " gnd");
    if (c.on_ground === false && arr.lat != null) { const [ax, ay] = P(arr.lat, arr.lon); s.append(svg("line", {class: "dest " + cls, x1: x, y1: y, x2: ax, y2: ay})); }
    const g = svg("g", {class: cls, transform: `translate(${x.toFixed(1)} ${y.toFixed(1)})`});
    g.append(svg("title", {}, `${c.callsign} ${(c.dep || {}).icao || "?"}→${arr.icao || "?"} · ${c.on_ground === false ? fl(c.alt_ft) : "no solo"}`));
    if (c.is_me || c.friend) g.append(svg("circle", {class: "halo", r: 9}));
    g.append(svg("path", {transform: `rotate(${(c.hdg || 0) - 90})`, d: "M7 0 L-3 3.6 L-1.2 0 L-3 -3.6 Z M-3 0 L-6.5 1.6 L-5.8 0 L-6.5 -1.6 Z"}));
    g.append(svg("text", {x: 9, y: 4}, c.callsign));
    const a = svg("a", {href: c.webeye || "#", target: "_blank"}); a.append(g); s.append(a);
  });
  return h("div", {class: "radar-wrap"}, s,
    h("div", {class: "glegend", style: "margin-top:8px"}, h("span", {}, h("b", {style: "background:var(--orange)"}), "no ar"), h("span", {}, h("b", {style: "background:var(--ash)"}), "no solo"),
      h("span", {}, h("b", {style: "background:#F2C94C"}), "colegas"), h("span", {}, h("b", {style: "background:var(--bone)"}), "você"),
      h("span", {class: "muted"}, "· pontos: aeroportos do AeroScale · tracejado: até o destino")));
}

function friendsPanel(d) {
  return h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Colegas"),
      h("button", {class: "btn ghost sm", onclick: () => friendDialog(null, null)}, icon("mais", 14), "Por VID")),
    d.friends.length ? h("div", {class: "flist"}, d.friends.map(f => h("div", {class: "frow" + (f.online ? " on" : "")},
      h("span", {class: "dot"}), h("div", {}, h("b", {}, f.nickname || `VID ${f.vid}`),
        h("small", {}, f.online && f.card ? `${f.card.callsign} ${(f.card.dep || {}).icao || "?"}→${(f.card.arr || {}).icao || "?"} · ${f.card.phase || ""}`
          : f.last ? `visto ${ago(f.last.last_seen)} como ${f.last.callsign}` : "ainda não visto pelo LineOps")),
      h("button", {class: "btn ghost sm", onclick: () => pilotDialog(f.vid, f.card)}, "ver"))))
      : h("p", {class: "muted", style: "font-size:13.5px;margin:0"}, "Marque pilotos com a estrela para acompanhar quando eles entram na IVAO. Você recebe um aviso aqui no LineOps."));
}

function frequentPanel(d) {
  const L = d.frequent.filter(x => !x.friend).slice(0, 8);
  return h("div", {class: "panel"}, h("h2", {}, "Quem você mais encontra"),
    h("p", {class: "muted", style: "font-size:13px"}, "Pilotos da companhia vistos na IVAO nos últimos 30 dias, pelas amostras que o seu servidor grava a cada 5 min."),
    L.length ? h("div", {class: "flist"}, L.map(x => h("div", {class: "frow" + (x.online ? " on" : "")}, h("span", {class: "dot"}),
      h("div", {}, h("b", {}, x.callsign || `VID ${x.vid}`), h("small", {}, `VID ${x.vid} · ${x.days} dia(s) · ${x.flights} voo(s) · visto ${ago(x.last)}`)),
      h("button", {class: "btn ghost sm", onclick: () => pilotDialog(x.vid)}, "ver"))))
      : h("p", {class: "muted", style: "font-size:13.5px;margin:0"}, "Ainda sem histórico. Com o servidor ligado e o VID salvo, a lista se forma sozinha."));
}

async function pilotDialog(vid, card) {
  const r = await act(() => get(`/api/network/pilots/${vid}`));
  if (!r) return;
  dialog(r.friend && r.friend.nickname ? `${r.friend.nickname} · VID ${vid}` : `VID ${vid}`,
    `${r.days} dia(s) e ${r.flights} voo(s) vistos pelo LineOps · ${r.arrived} chegada(s) confirmadas pela posição.`,
    h("div", {},
      card ? pilotCard(card, false, true) : null,
      r.routes.length ? h("div", {style: "margin:12px 0"}, h("h3", {style: "margin-bottom:8px"}, "Rotas"), h("div", {class: "tools"}, r.routes.map(([x, n]) => chip(`${x} ×${n}`, "or")))) : null,
      r.together.length ? h("div", {style: "margin:12px 0"}, h("h3", {style: "margin-bottom:8px"}, "Na rede junto com você"),
        r.together.map(t => h("p", {style: "font-size:13.5px;margin:0 0 4px"}, `${dmy(t.day)}: ele ${t.callsign} ${t.dep || "?"}→${t.arr || "?"} · você ${t.my_callsign} ${t.my_dep || "?"}→${t.my_arr || "?"}`))) : null,
      r.history.length ? h("div", {class: "tbl"}, h("table", {}, h("thead", {}, h("tr", {}, ["Dia", "Callsign", "Rota", "Avião", "Máx.", ""].map(x => h("th", {}, x)))),
        h("tbody", {}, r.history.slice(0, 15).map(x => h("tr", {}, h("td", {}, dmy(x.day)), h("td", {class: "t"}, x.callsign), h("td", {class: "t"}, `${x.dep || "?"}→${x.arr || "?"}`),
          h("td", {}, x.aircraft || "—"), h("td", {class: "t"}, x.max_alt_ft ? fl(x.max_alt_ft) : "—"), h("td", {}, x.arrived ? chip("chegou", "go") : x.airborne ? chip("voou", "or") : chip("solo", "")))))))
        : h("p", {class: "muted"}, "Sem histórico gravado ainda.")),
    [closeBtn, {label: r.friend ? "Editar colega" : "Adicionar aos colegas", fn: () => { setTimeout(() => friendDialog(vid, r.friend, card && card.callsign), 50); }}]);
}

function friendDialog(vid, fav, callsign) {
  const v = h("input", {value: vid || "", inputmode: "numeric", placeholder: "ex.: 123456", disabled: !!vid});
  const n = h("input", {value: (fav && fav.nickname) || "", placeholder: "como você chama esse piloto"});
  const o = h("input", {value: (fav && fav.note) || "", placeholder: "opcional, ex.: voa sempre GRU–SDU"});
  const btns = [closeBtn];
  if (fav) btns.push({label: "Remover", cls: "stop", fn: async () => { if (!await act(() => api("DELETE", `/api/network/friends/${vid}`), "Removido dos colegas")) return false; refreshNet(); }});
  btns.push({label: "Salvar", fn: async () => {
    if (!await act(() => post("/api/network/friends", {vid: v.value, nickname: n.value || null, note: o.value || null}), "Colega salvo")) return false;
    refreshNet();
  }});
  dialog(fav ? "Editar colega" : "Adicionar aos colegas", (callsign ? `${callsign}. ` : "") + "A IVAO não publica nomes: o apelido fica só no seu LineOps.",
    h("div", {}, field("VID", v), field("Apelido", n), field("Nota", o)), btns);
}
const refreshNet = () => { if (document.getElementById("net-body")) loadNet(true); else render(); };

function watchFriends(d) {
  const now = new Set(d.friends.filter(f => f.online).map(f => f.vid));
  if (net.friendsOnline) d.friends.filter(f => f.online && !net.friendsOnline.has(f.vid)).forEach(f =>
    toast(`★ ${f.nickname || "VID " + f.vid} entrou na IVAO${f.card ? `: ${f.card.callsign} ${(f.card.dep || {}).icao || "?"}→${(f.card.arr || {}).icao || "?"}` : ""}`));
  net.friendsOnline = now;
}
setInterval(() => { if (me && document.visibilityState === "visible" && !document.getElementById("net-body") && net.data && net.data.friends.length)
  get("/api/network").then(d => { net.data = d; watchFriends(d); }).catch(() => {}); }, 120000);

/* ───────────────────────── Mensagens: a equipe da operação ───────────────────────── */
const SECTOR_HUE = {manutencao: "#E25A4A", escala: "#6FA8DC", tripulantes: "#46C08B", despacho: "#FF6A13", carregamento: "#B48CF2", mcc: "#E9B44C", chefia: "#EDEAE4", folha: "#5FC4C4", cabine: "#F28DB2", transporte: "#9AA5B1", hotelaria: "#C9A27E", tripulacao: "#7FD1AE", cco: "#8FA8FF"};
const inbox = {unread: 0, sel: null, sector: "", pend: false, lastTop: null};
const initials = n => (n || "?").replace(/^(Cmte\.|Eng\.|DOV)\s*/i, "").split(/\s+/).filter(Boolean).slice(0, 2).map(x => x[0]).join("").toUpperCase();
const avatar = (m, size = 36) => h("span", {class: "avatar", style: `--c:${SECTOR_HUE[m.sector] || "#8A9099"};width:${size}px;height:${size}px;font-size:${Math.round(size * .38)}px`,
  title: m.sector_label || ""}, initials(m.sender));
const msgTime = t => { const d = Date.parse(t); return Date.now() - d < 86400e3 ? hhmmz(t) : dm(t); };

function msgLine(m, onClick, active) {
  return h("button", {class: "mline" + (m.read_at ? "" : " unread") + (active ? " sel" : ""), onclick: onClick},
    avatar(m), h("div", {class: "mbody"},
      h("div", {class: "mtop"}, h("b", {}, m.sender), h("span", {class: "msec"}, m.sector_label), h("small", {}, msgTime(m.ts))),
      h("div", {class: "msub"}, m.channel === "acars" ? [h("span", {class: "acars-tag"}, "ACARS"), " "] : null, m.subject),
      h("div", {class: "mprev"}, m.body.split("\n").find(x => x.trim()) || "")),
    m.ack_required && !m.acked_at ? h("span", {class: "chip hold"}, "ciente") : null);
}

function msgDetail(m) {
  const box = h("div", {class: "mdetail"});
  const paint = () => put(box,
    h("div", {class: "mhead"}, avatar(m, 46), h("div", {}, h("b", {}, m.sender), h("div", {class: "muted", style: "font-size:13px"}, `${m.sender_title || ""} · ${m.sector_label}`)),
      h("small", {class: "muted"}, dt(m.ts))),
    h("h2", {style: "margin:16px 0 10px"}, m.subject),
    m.channel === "acars" ? acarsSlip(m) : h("div", {class: "mtext"}, m.body),
    h("div", {class: "tools", style: "margin-top:16px"},
      (m.links || []).map(l => h("a", {class: "btn ghost sm", href: l.href, target: l.external ? "_blank" : null, rel: l.external ? "noopener noreferrer" : null}, l.label)),
      m.ack_required ? (m.acked_at ? chip(`ciente dado ${dt(m.acked_at)}`, "go")
        : h("button", {class: "btn sm", onclick: async () => { const r = await act(() => post(`/api/messages/${m.id}/ack`), "Ciente registrado"); if (r) { Object.assign(m, r); paint(); refreshBadge(); } }}, icon("ok", 15), "Dar ciente")) : null),
    h("p", {class: "fine", style: "margin-top:18px"}, "Personagens fictícios; o conteúdo vem da sua escala, do OFP, do MEL e da IVAO."));
  paint();
  return box;
}

view("mensagens", async main => {
  const d = await get("/api/messages" + (inbox.sector ? `?sector=${inbox.sector}` : ""));
  let list = d.messages.filter(m => !inbox.pend || (m.ack_required && !m.acked_at));
  inbox.unread = d.counts.unread;
  const pane = h("div", {class: "mpane"});
  const open = async (m, el) => {
    inbox.sel = m.id;
    main.querySelectorAll(".mline.sel").forEach(x => x.classList.remove("sel"));
    if (el) { el.classList.add("sel"); el.classList.remove("unread"); }
    put(pane, msgDetail(m));
    if (innerWidth < 980) pane.scrollIntoView({behavior: "smooth", block: "start"});
    if (!m.read_at) { const r = await api("POST", `/api/messages/${m.id}/read`).catch(() => null); if (r) { m.read_at = r.read_at; refreshBadge(); } }
  };
  const lines = list.map(m => { const el = msgLine(m, () => open(m, el), m.id === inbox.sel); return el; });
  const chips = h("div", {class: "tools"}, [["", "Todos"], ...Object.entries(d.sectors)].map(([k, l]) =>
    h("button", {class: "chip " + (inbox.sector === k ? "or" : ""), style: "cursor:pointer", onclick: () => { inbox.sector = k; render(); }}, l.split(" · ")[0])),
    h("button", {class: "chip " + (inbox.pend ? "hold" : ""), style: "cursor:pointer", onclick: () => { inbox.pend = !inbox.pend; render(); }}, `Pedem ciente (${d.counts.pending_ack})`));
  put(main, h("div", {class: "head"}, h("div", {}, h("h1", {}, "Mensagens"),
      h("div", {class: "sub"}, "A operação falando com você: escala, controle de tripulantes, DOV, carregamento, manutenção e chefia.")),
    h("div", {class: "tools"}, d.counts.unread ? h("button", {class: "btn ghost sm", onclick: async () => { if (await act(() => post("/api/messages/read-all"))) render(); }}, "Marcar todas como lidas") : null,
      h("a", {class: "btn ghost sm", href: "#/ajustes"}, "Equipe"))),
    chips,
    h("div", {class: "inbox", style: "margin-top:14px"},
      h("div", {class: "mlist panel"}, lines.length ? lines : h("div", {class: "empty"}, icon("mensagens", 26), h("div", {}, "Nenhuma mensagem aqui."))),
      pane));
  const first = list.find(m => m.id === inbox.sel) || list[0];
  if (first) put(pane, msgDetail(first)); else put(pane, h("div", {class: "empty"}, "Selecione uma mensagem."));
  if (first && !first.read_at && innerWidth >= 980) open(first, lines[list.indexOf(first)]);
});

function opsPanel(d) {
  const L = d.messages || [];
  return h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Mensagens da operação"),
      h("a", {href: "#/mensagens", class: "muted", style: "font-size:13px"}, `abrir${d.message_counts && d.message_counts.unread ? ` (${d.message_counts.unread} não lidas)` : ""}`)),
    L.length ? h("div", {class: "mmini"}, L.map(m => h("a", {class: "mline" + (m.read_at ? "" : " unread"), href: "#/mensagens", onclick: () => { inbox.sel = m.id; }},
      avatar(m, 32), h("div", {class: "mbody"}, h("div", {class: "mtop"}, h("b", {}, m.sender), h("span", {class: "msec"}, m.sector_label.split(" · ")[0]),
        h("small", {}, msgTime(m.ts))), h("div", {class: "msub"}, m.subject)),
      m.ack_required && !m.acked_at ? h("span", {class: "chip hold"}, "ciente") : null)))
      : h("p", {class: "muted", style: "margin:0"}, "Sem mensagens ainda."));
}

function refreshBadge() {
  get("/api/messages/counts").then(c => { inbox.unread = c.unread; document.querySelectorAll(".nav-msg-tag").forEach(t => { t.textContent = c.unread; t.hidden = !c.unread; }); }).catch(() => {});
}
setInterval(async () => {
  if (!me || document.visibilityState !== "visible") return;
  try {
    const d = await get("/api/messages?unread=true");
    const top = d.messages[0];
    const fresh = inbox.lastTop === null ? [] : d.messages.filter(m => m.id > inbox.lastTop);
    const up = fresh.filter(m => m.channel === "acars" || m.channel === "alarme");
    if (up.length) {
      if (!prefs) prefs = await get("/api/settings").catch(() => ({}));
      if ((prefs.acars_chime || "on") !== "off") chime();
      toast(`${up[0].channel === "alarme" ? "⏰" : "ACARS ·"} ${up[0].subject}`);
      if (up.some(m => location.hash === `#/voo/${m.flight_id}`)) render();
    } else if (top && fresh.length) toast(`${top.sender} (${top.sector_label.split(" · ")[0]}): ${top.subject}`);
    if (top) inbox.lastTop = Math.max(inbox.lastTop || 0, top.id); else if (inbox.lastTop === null) inbox.lastTop = 0;
    inbox.unread = d.counts.unread;
    document.querySelectorAll(".nav-msg-tag").forEach(t => { t.textContent = d.counts.unread; t.hidden = !d.counts.unread; });
  } catch (e) { /* tenta de novo */ }
}, 20000);

/* ───────────────────────── ACARS: o que chega no avião durante o voo ───────────────────────── */
let prefs = null;
function chime() {          // o "ding-dong" da cabine, sintetizado (nenhum arquivo de som)
  try {
    const A = window.AudioContext || window.webkitAudioContext;
    const ctx = chime.ctx || (chime.ctx = new A());
    const t0 = ctx.currentTime + .02;
    [[1046.5, 0], [784, .32]].forEach(([f, d]) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "sine"; o.frequency.value = f;
      g.gain.setValueAtTime(.0001, t0 + d); g.gain.exponentialRampToValueAtTime(.16, t0 + d + .02); g.gain.exponentialRampToValueAtTime(.0001, t0 + d + .9);
      o.connect(g).connect(ctx.destination); o.start(t0 + d); o.stop(t0 + d + .95);
    });
  } catch (e) { /* sem áudio: só o aviso visual */ }
}
function acarsSlip(m, fresh) {
  return h("div", {class: "slip" + (fresh ? " fresh" : "") + (m.level === "alerta" ? " alert" : "")},
    h("div", {class: "slip-head"}, h("span", {}, "UPLINK"), h("span", {}, hhmmz(m.ts)), h("span", {}, m.sector_label.split(" · ")[0])),
    h("div", {class: "slip-from"}, `DE ${m.sender}${m.sender_title ? " · " + m.sender_title : ""}`),
    h("div", {class: "slip-sub"}, m.subject),
    h("div", {class: "slip-body"}, m.body),
    (m.links || []).length ? h("div", {class: "slip-links"}, m.links.map(l => h("a", {href: l.href, target: l.external ? "_blank" : null}, l.label))) : null);
}
function acarsPanel(c) {
  const L = (c.messages || []).filter(m => m.channel === "acars");
  const live = c.session && c.session.status === "LIVE";
  if (!L.length && !live) return null;
  const fresh = m => Date.now() - Date.parse(m.ts) < 10 * 60e3 && !m.read_at;
  return h("div", {class: "panel acars-panel"}, h("div", {class: "panel-head"}, h("h2", {}, "ACARS"),
      h("span", {class: "tools"}, h("span", {class: "muted", style: "font-size:12.5px"}, live ? "impressora ligada" : `${L.length} mensagem(ns) do voo`),
        live ? h("span", {class: "radar", "aria-hidden": "true"}) : null)),
    L.length ? h("div", {class: "slips"}, L.slice(0, 2).map(m => acarsSlip(m, fresh(m))),
      L.length > 2 ? h("details", {class: "slip-more"}, h("summary", {}, `mais ${L.length - 2} impressão(ões)`), L.slice(2).map(m => acarsSlip(m))) : null)
      : h("p", {class: "muted", style: "margin:0;font-size:13.5px"}, "Durante o voo chegam aqui a cabine, o pernoite ou a próxima perna, o tempo no destino e o MCC, conforme o avião avança."));
}

/* ───────────────────────── tripulação, atraso, cheque de rota ───────────────────────── */
const person = (name, role, sector) => h("div", {class: "crew-p"}, avatar({sender: name, sector, sector_label: role}, 34),
  h("div", {}, h("b", {}, name), h("small", {}, role)));
function crewPanel(c, stayNext) {
  if (!c) return null;
  const st = (stayNext || []).filter(e => /stay_(wake|pickup|car|checkin)/.test(e.key));
  const lab = {stay_car: "Carro no desembarque", stay_checkin: "Check-in", stay_wake: "Despertar", stay_pickup: "Carro na portaria"};
  return h("div", {class: "panel crew-panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Sua tripulação"),
      h("span", {class: "muted", style: "font-size:12.5px"}, c.trip_day > 1 ? `dia ${c.trip_day} da viagem` : "início da viagem")),
    h("div", {class: "crew-grid"}, person(c.fo.name, "Copiloto", "tripulacao"), c.cabin.map(x => person(x.name, x.role, "cabine"))),
    st.length ? h("div", {class: "crew-next"}, st.map(e => h("div", {}, h("span", {class: "t"}, dt(e.due_utc).slice(0, 5) + " " + hhmmz(e.due_utc)),
      h("span", {}, lab[e.key.split(":")[0]] || e.subject)))) : null,
    h("p", {class: "fine", style: "margin:10px 0 0"}, `${c.disclaimer} Comissários pela regra do ${c.cabin_rule.split(":")[0]}.`));
}
function lineCheckNote(lc) {
  if (!lc) return null;
  if (lc.status === "AGENDADO") return note("or", "chefia", h("b", {}, "Cheque de rota neste voo: "), `${lc.checker} no jumpseat. `,
    "Ele olha despacho assinado, pontualidade, toque e combustível × OFP. Acompanhe pela IVAO ou pelo ACARS.");
  const lab = {APROVADO: ["aprovado", "go"], APROVADO_OBS: ["aprovado com observações", "hold"], REPROVADO: ["não aprovado", "stop"], NAO_REALIZADO: ["não realizado", ""]}[lc.status] || [lc.status, ""];
  return h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Cheque de rota"), chip(lab[0], lab[1])),
    h("div", {class: "lc-items"}, (lc.items || []).map(i => h("div", {class: "lc-i"}, h("span", {class: "lc-m " + (i.ok ? "ok" : i.ok === false ? "no" : "")}, i.ok ? "✔" : i.ok === false ? "✘" : "–"),
      h("b", {}, i.item), h("span", {class: "muted"}, i.note)))),
    h("p", {class: "fine", style: "margin:8px 0 0"}, `Checador: ${lc.checker} (fictício).`));
}
function delayPanel(c) {
  const d = c.delay;
  if (!d || !d.late) return null;
  if (d.code) return note("", "info", h("b", {}, `Atraso de ${d.out_delay_min} min: `), `código ${d.code}, ${(d.code_label || "").toLowerCase()}`, d.remark ? ` (${d.remark})` : "", ".");
  const sel = h("select", {}, Object.entries(c.delay_codes || {}).map(([k, v]) => h("option", {value: k, selected: k === "93"}, `${k} · ${v}`)));
  const rm = h("input", {placeholder: "opcional: o que aconteceu"});
  return h("div", {class: "panel delay-panel"}, h("div", {class: "panel-head"}, h("h2", {}, `Atraso de ${d.out_delay_min} min`), chip("código pendente", "hold")),
    h("p", {class: "muted", style: "margin-top:0"}, "O CCO precisa do código de atraso (IATA) para fechar o relatório. Acima de 15 min do programado conta como atraso (OTP15)."),
    h("div", {class: "row"}, field("Código", sel), field("Observação", rm)),
    h("button", {class: "btn sm", onclick: async () => { if (await act(() => post(`/api/flights/${c.flight.id}/delay`, {code: sel.value, remark: rm.value}), "Código registrado")) render(); }}, "Enviar ao CCO"));
}

/* ───────────────────────── carreira ───────────────────────── */
const MS_ICON = {aeroporto: "📍", aeronave: "🛩️", horas: "⏱️", pousos: "🛬", sequencia: "🧈", noturno: "🌙"};
view("carreira", async main => {
  const d = await get("/api/carreira");
  const ck = d.check, om = d.otp_month, oa = d.otp_all;
  const ckLab = ck.scheduled ? `agendado no ${ck.scheduled.flight_number || "voo"} (${dm(ck.scheduled.dep_utc)})` : ck.due ? "vencido: a chefia vai escalar" : `válido até ${dmy(ck.valid_until)}`;
  const figs = h("div", {class: "figs"}, [
    ["Partidas no horário (mês)", om.dep_otp == null ? "—" : om.dep_otp + "%"], ["Partidas no horário (total)", oa.dep_otp == null ? "—" : oa.dep_otp + "%"],
    ["Chegadas no horário (total)", oa.arr_otp == null ? "—" : oa.arr_otp + "%"], ["Marcos", d.milestones.length], ["Aeroportos", d.airports.length],
    ["Atrasos sem código", oa.pending_codes]].map(([l, v]) => h("div", {class: "fig"}, h("b", {}, String(v)), h("span", {}, l))));
  put(main, h("div", {class: "head"}, h("div", {}, h("h1", {}, "Carreira"), h("div", {class: "sub"}, "Pontualidade, marcos, cheque de rota e as pessoas com quem você voou. Tudo do seu diário."))),
    figs,
    h("div", {class: "cols-2"},
      h("div", {},
        h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Marcos"), h("span", {class: "muted", style: "font-size:12.5px"}, `${d.milestones.length} conquistados`)),
          d.milestones.length ? h("div", {class: "ms-grid"}, d.milestones.map(m => h("div", {class: "ms"}, h("span", {class: "ms-i", "aria-hidden": "true"}, MS_ICON[m.kind] || "★"),
            h("div", {}, h("b", {}, m.title), h("small", {}, [m.detail, dmy(m.achieved_at)].filter(Boolean).join(" · "))))))
            : h("div", {class: "empty"}, icon("diario", 26), h("div", {}, "O primeiro voo no diário já traz os primeiros marcos."))),
        h("div", {class: "panel"}, h("h2", {}, "Pontualidade"), h("div", {class: "hr"}),
          oa.flights ? h("div", {}, h("dl", {class: "kv"}, h("dt", {}, "Voos medidos"), h("dd", {}, oa.flights), h("dt", {}, "Partidas atrasadas"), h("dd", {}, oa.late),
            h("dt", {}, "Pendentes de código"), h("dd", {}, oa.pending_codes)),
            oa.codes.length ? h("div", {class: "wrap-list", style: "margin-top:10px"}, oa.codes.map(x => h("div", {class: "wl"}, h("span", {class: "mono wl-k"}, x.code),
              h("span", {}, x.label), h("b", {class: "mono"}, `${x.n}×`)))) : null)
            : h("p", {class: "muted"}, "Ainda sem voos no horário da escala para medir."),
          h("p", {class: "fine"}, oa.rule + ". Voo lançado com “ignorar horário” não entra.")),
      ),
      h("div", {},
        h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Cheque de rota"), chip(ck.due && !ck.scheduled ? "vencido" : ck.scheduled ? "agendado" : "em dia", ck.due && !ck.scheduled ? "hold" : ck.scheduled ? "cool" : "go")),
          h("p", {style: "margin:0 0 8px"}, cap(ckLab) + "."),
          ck.history.length ? h("div", {class: "wrap-list"}, ck.history.map(x => h("div", {class: "wl"}, h("span", {class: "mono wl-k"}, dm(x.done_at || x.announced_at)),
            h("span", {}, `${x.flight_number || "—"} ${x.adep || ""}→${x.ades || ""} · ${x.checker}`),
            h("b", {}, ({AGENDADO: "agendado", APROVADO: "aprovado", APROVADO_OBS: "aprovado c/ obs.", REPROVADO: "não aprovado", NAO_REALIZADO: "não realizado"})[x.status] || x.status)))) : null,
          h("p", {class: "fine"}, ck.rule + ". Checadores fictícios; critérios do LineOps.")),
        h("div", {class: "panel"}, h("h2", {}, "Com quem você mais voou"), h("div", {class: "hr"}),
          d.colleagues.length ? h("div", {class: "crew-list"}, d.colleagues.map(x => h("div", {class: "crew-row"}, avatar({sender: x.name, sector: x.role === "Copiloto" ? "tripulacao" : "cabine", sector_label: x.role}, 32),
            h("div", {}, h("b", {}, x.name), h("small", {}, `${x.role} · ${x.days} dia${x.days > 1 ? "s" : ""} juntos · último ${dmy(x.last)}`)))))
            : h("p", {class: "muted"}, "Depois do primeiro dia de voo aprovado, a sua tripulação aparece aqui."),
          h("p", {class: "fine"}, "Tripulação fictícia do LineOps.")))));
  const pp = h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Passaporte")), skeleton("l", "l"));
  const sp = h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Pedidos de troca de escala")), skeleton("l", "l"));
  const cq = h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Conquistas")), skeleton("l"));
  main.append(cq, pp, sp);
  get("/api/conquistas").then(c => {
    if (!cq.isConnected) return;
    const fmtN = (v, u) => `${Number.isInteger(v) ? v : v.toLocaleString("pt-BR", {maximumFractionDigits: 1})}`;
    const items = [...c.items].sort((a, b) => (b.done - a.done) || (b.pct - a.pct));
    put(cq, h("div", {class: "panel-head"}, h("h2", {}, "Conquistas"), h("span", {class: "muted", style: "font-size:12.5px"}, `${c.done} de ${c.total}`)),
      h("div", {class: "badges"}, items.map(b => h("div", {class: "badge" + (b.done ? " got" : ""), title: `Fonte: ${b.source}`},
        h("span", {class: "ic"}, icon(b.done ? "trofeu" : "relogio", 18)),
        h("div", {style: "min-width:0;flex:1"}, h("b", {}, b.title), h("small", {}, b.desc),
          b.done ? h("small", {style: "color:var(--ember);margin-top:4px"}, "Conquistada")
            : h("div", {}, h("div", {class: "bar"}, h("i", {style: `width:${b.pct}%`})), h("small", {style: "margin-top:3px"}, `${fmtN(b.current)} de ${b.goal} ${b.unit}`)))))),
      h("p", {class: "fine"}, c.note));
  }).catch(() => cq.remove());
  Promise.all([get("/api/passaporte"), get("/api/mapa?fonte=diario")]).then(([ps, mp]) => {
    if (!pp.isConnected) return;
    const types = ps.types.filter(t => t.type !== "—");
    put(pp, h("div", {class: "panel-head"}, h("h2", {}, "Passaporte"), h("span", {class: "muted", style: "font-size:12.5px"},
        `${ps.airports.length} aeroporto(s) · ${ps.countries.length} país(es) · ${ps.flights} voo(s)`)),
      ps.airports.length ? h("div", {class: "cols-2"},
        h("div", {}, h("div", {class: "pass-grid"}, ps.airports.map(a => h("div", {class: "stamp" + (a.country && a.country !== "BR" ? " intl" : ""),
          title: `primeira vez ${dmy(a.first)} · última ${dmy(a.last)}`},
          h("span", {class: "n"}, `${a.landings}×`), h("b", {}, a.iata || a.icao), h("small", {}, a.city || a.icao), h("small", {}, `desde ${dmy(a.first)}`))))),
        h("div", {}, routeMapSvg(mp, {w: 560, h: 380}),
          types.length ? h("div", {class: "tbl", style: "margin-top:12px"}, h("table", {}, h("thead", {}, h("tr", {}, ["Tipo", "Voos", "Horas", "Noturnas"].map((x, k) => h("th", {class: k ? "n" : ""}, x)))),
            h("tbody", {}, types.map(t => h("tr", {}, h("td", {}, `${t.type}${t.name ? " · " + t.name : ""}`), h("td", {class: "n"}, t.flights),
              h("td", {class: "n"}, (t.block_min / 60).toFixed(1)), h("td", {class: "n"}, (t.night_min / 60).toFixed(1))))))) : null))
        : h("p", {class: "muted"}, "Cada aeroporto onde você pousar entra aqui com a data da primeira vez. Começa no primeiro voo aprovado no diário."),
      h("p", {class: "fine"}, "Do seu diário de bordo desta carreira. Mapa com as coordenadas do catálogo do AeroScale."));
  }).catch(() => put(pp));
  const sw = await get("/api/troca").catch(() => null);
  if (sw) put(sp, h("div", {class: "panel-head"}, h("h2", {}, "Pedidos de troca de escala"), h("span", {class: "muted", style: "font-size:12.5px"}, `${sw.items.length} pedido(s)`)),
    sw.items.length ? sw.items.map(swapLine) : h("p", {class: "muted"}, "Nenhum pedido ainda. Na Escala, abra um dia e use “Pedir troca” numa perna."),
    h("p", {class: "fine"}, sw.disclaimer));
});

/* ───────────────────────── modo (companhia) ───────────────────────── */
async function modeScreen() {
  const app = document.getElementById("app");
  const c = await get("/api/careers");
  const enter = async code => {
    const r = await act(() => post(`/api/careers/${code}/select`));
    if (!r) return;
    me = await get("/api/me"); applyTheme(me.career); DISCLAIMER = me.disclaimer || DISCLAIMER;
    me.career_logo = !!(c.items.find(x => x.code === code) || {}).has_logo;
    roster.bounds = null; location.hash = "#/painel";
  };
  const upload = code => {
    const f = h("input", {type: "file", accept: ".png,.jpg,.jpeg,.webp,.svg", style: "display:none"});
    f.addEventListener("change", async () => {
      const file = f.files[0]; if (!file) return;
      const res = await fetch(`/api/careers/${code}/logo?name=${encodeURIComponent(file.name)}`, {method: "PUT", body: file, credentials: "same-origin", headers: {"X-Requested-With": "lineops"}});
      const j = await res.json().catch(() => ({}));
      if (!res.ok) return toast(j.message || "Não deu para salvar o logo.", true);
      toast("Logo salvo"); modeScreen();
    });
    document.body.append(f); f.click(); setTimeout(() => f.remove(), 60000);
  };
  const card = p => {
    const on = p.status === "ativo";
    return h("div", {class: "mode-card" + (on ? "" : " soon") + (p.code === c.current && c.chosen ? " cur" : ""), style: `--c:${p.accent}`},
      h("div", {class: "mode-top"}, careerBadge(p, 34), on ? null : chip("em breve", "")),
      h("h2", {}, p.name), h("p", {class: "muted", style: "margin:2px 0 8px;font-size:13px"}, p.full),
      h("p", {style: "margin:0 0 10px;font-size:13.5px"}, p.blurb),
      p.fleet.length ? h("div", {class: "mode-fleet"}, p.fleet.map(t => h("span", {}, t))) : null,
      p.stats ? h("p", {class: "fine", style: "margin:10px 0 0"}, `${p.stats.flights} voo(s) no diário · ${hmLong(p.stats.block_min)} de bloco · ${p.stats.legs} perna(s) na escala`) : null,
      h("div", {class: "tools", style: "margin-top:auto;padding-top:14px"},
        on ? h("button", {class: "btn", onclick: () => enter(p.code)}, `Entrar no modo ${p.name}`) : h("button", {class: "btn ghost", disabled: true}, "Em breve"),
        on ? h("button", {class: "btn ghost sm", onclick: () => upload(p.code), title: "Coloque o arquivo de logo que quiser (PNG, JPG, WEBP ou SVG)"}, p.has_logo ? "Trocar logo" : "Pôr logo") : null,
        on && p.has_logo ? h("button", {class: "btn ghost sm", onclick: async () => { if (await act(() => api("DELETE", `/api/careers/${p.code}/logo`))) modeScreen(); }}, "Tirar logo") : null));
  };
  app.replaceChildren(h("div", {class: "mode-screen"},
    h("div", {class: "brand", style: "justify-content:center"}, brandMark(30), h("span", {}, "Line", h("b", {}, "Ops"))),
    h("h1", {class: "mode-h"}, `${greet()}, comandante${me && me.display_name ? " " + me.display_name : ""}.`),
    h("p", {class: "muted", style: "text-align:center;margin:-6px auto 22px;max-width:640px"}, "Em qual companhia você voa hoje? Cada uma tem a sua escala, frota, diário, salário e equipe."),
    h("div", {class: "mode-grid"}, c.items.map(card)),
    h("p", {class: "fine", style: "text-align:center;margin-top:20px"}, "Os Voos Externos, o celular e o seu login valem para todas. Logotipos são arquivos que você mesmo coloca; a LineOps não traz marca de companhia nenhuma.",
      c.chosen ? [" ", h("a", {href: "#/painel"}, "Voltar")] : null)));
}

/* ───────────────────────── gerar escala (motor do AeroScale) ───────────────────────── */
const gen = {job: null, timer: null, offs: []};
view("gerar", async main => {
  const d = await get("/api/motor");
  const o = d.options, st = d.status, df = o.defaults, S = o.settings || {};
  const car = me.career || {};
  // o formulário inteiro do gerador do AeroScale (mesmos campos, mesmos padrões); lembra o que você usou por companhia
  const LS = `lineops.gerar.${car.code || "x"}`;
  let prev = {};
  try { prev = JSON.parse(localStorage.getItem(LS) || "{}") || {}; } catch (e) { prev = {}; }
  const pv = (k, dflt) => (prev[k] !== undefined && prev[k] !== null ? prev[k] : dflt);
  const num = (k, dflt, attrs = {}) => h("input", {type: "number", value: pv(k, dflt ?? ""), ...attrs});
  const ck = (k, dflt) => h("input", {type: "checkbox", checked: !!pv(k, dflt)});
  const sel = (k, opts, dflt) => { const e = h("select", {}, opts.map(([v, l]) => h("option", {value: v}, l))); e.value = String(pv(k, dflt) ?? ""); return e; };
  const i = {kind: sel("kind", o.kinds, df.kind), name: h("input", {placeholder: "Gerado automaticamente"}),
    month: h("input", {type: "month", value: df.month}), start: h("input", {type: "date", value: df.start_date}), end: h("input", {type: "date"}),
    base: h("select", {}, h("option", {value: ""}, "Automática (a com mais partidas do tipo)"),
      o.bases.map(b => h("option", {value: b.icao}, `${b.icao}${b.iata ? " · " + b.iata : ""}${b.city ? " · " + b.city : ""} · ${b.n} partidas`))),
    type: h("select", {}, o.types.map(t => h("option", {value: t.type}, `${t.type} · ${t.n} voos no RPL`))),
    swap: num("swap_min", 50, {min: 20, max: 180}), deadhead: ck("allow_deadhead", true),
    realism: ck("profile_realism", true), sob: num("sobreaviso_per_month", "", {min: 0, max: 8, placeholder: "padrão do perfil"}),
    soba: num("sobreaviso_activation_pct", "", {min: 0, max: 100, placeholder: "35"}),
    longoff: ck("long_off", true), inactive: ck("inactive_days", true),
    rtb: sel("return_to_base", [["none", "Livre"], ["daily", "Pernoitar na base todo dia"], ["end", "Terminar na base"]], "none"),
    prefa: h("input", {placeholder: "ICAO ou IATA: REC, FOR…", value: (pv("preferred_airports", []) || []).join(", ")}),
    prefs: sel("pref_strength", [["leve", "Leve (de vez em quando)"], ["normal", "Normal"], ["forte", "Forte (sempre que der)"]], "normal"),
    avoida: h("input", {placeholder: "ex.: SBRB", value: (pv("avoid_airports", []) || []).join(", ")}),
    lmin: num("leg_min_minutes", 0, {min: 0}), lmax: num("leg_max_minutes", 960, {min: 1}),
    legsmin: num("legs_min", S.legs_min ?? 2, {min: 0, max: 12}), legsmax: num("legs_max", S.legs_max ?? 5, {min: 1, max: 12}),
    turnmode: sel("turn_mode", [["auto", "Automático (medido no RPL)"], ["fixed", "Valor fixo"]], S.turn_mode || "auto"),
    minturn: num("min_turn", S.min_turn_min ?? 40, {min: 10}), maxturn: num("max_turn", S.max_turn_min ?? 240, {min: 10}),
    minrest: num("min_rest", S.min_rest_min ?? 600, {min: 0}),
    dstart: h("input", {type: "time", value: pv("day_start", S.day_start || "05:00")}), dend: h("input", {type: "time", value: pv("day_end", S.day_end || "23:59")}),
    chcustom: h("input", {type: "checkbox", checked: prev.legs2_pct != null}),
    l2: h("input", {type: "range", min: 0, max: 100, step: 5, value: pv("legs2_pct", (o.chaves_profile || [30, 38])[0])}),
    l3: h("input", {type: "range", min: 0, max: 100, step: 5, value: pv("legs3_pct", (o.chaves_profile || [30, 38])[1])}),
    tmatch: sel("type_match", [["family", "Mesma família (recomendado: como na operação real)"], ["exact", "Exata (mesmo designador do RPL)"]], S.type_match || "family"),
    proj: ck("projection", S.projection_enabled ?? true), used: ck("avoid_used", S.avoid_used_flights ?? true),
    intl: ck("international", S.intl_enabled ?? true), intlc: h("input", {type: "checkbox", checked: prev.intl_target_pct != null}),
    intlp: h("input", {type: "range", min: 1, max: 100, step: 1, value: pv("intl_target_pct", 10)}),
    model: ck("model_realism", S.model_realism ?? true), posn: ck("allow_positioning", false),
    anom: num("anomaly_repeat_pct", S.anomaly_repeat_pct ?? 5, {min: 0, max: 100}),
    crew: sel("crew_mode", [["rbac117", "RBAC 117 – jornada conforme horário de apresentação e etapas (GOL, LATAM, Azul)"],
      ["lei", "Lei 13.475 estrita – 9 h de jornada, 8 h de voo, 4 pousos"], ["off", "Desligado – só os tempos acima"]], S.crew_mode || "rbac117"),
    offmin: num("days_off_min", S.days_off_min ?? 10, {min: 0, max: 20}), maxcons: num("max_consecutive_days", S.max_consecutive_days ?? 6, {min: 1, max: 21}),
    cdo: ck("crew_days_off", S.crew_days_off ?? true), cfb: ck("crew_folga_base", S.crew_folga_base ?? true),
    cmad: ck("crew_madrugada", S.crew_madrugada ?? true), cmon: ck("crew_monthly", S.crew_monthly ?? true),
    res: sel("reserve_blocks", [["", "Padrão (semanal 1, mensal 1, avulsa 0; 6 meses e anual: por mês)"], ["0", "Sem reserva"], ["1", "1 bloco"], ["2", "2 blocos"], ["3", "3 blocos"]], ""),
    rmin: num("reserve_min_days", S.reserve_min_days ?? 3, {min: 3, max: 6}), rmax: num("reserve_max_days", S.reserve_max_days ?? 6, {min: 3, max: 6}),
    rbase: num("reserve_base_pct", S.reserve_base_pct ?? 80, {min: 0, max: 100}), ract: num("reserve_activation_pct", S.reserve_activation_pct ?? 50, {min: 0, max: 100}),
    ractd: num("reserve_activation_max_days", S.reserve_activation_max_days ?? 3, {min: 1, max: 6}),
    madr: ck("avoid_madrugada", false), pref: sel("time_pref", [["", "Sem preferência"], ["manha", "Prefiro manhã"], ["tarde", "Prefiro tarde"]], ""),
    seed: h("input", {type: "number", placeholder: "sorteada"}), off: h("input", {type: "date"}), frompos: h("input", {type: "checkbox"}),
    fromday: h("input", {type: "date"}), replace: ck("replace_from_day", true), cargo: ck("include_cargo", false),
    follow: h("input", {type: "range", min: 0, max: 100, step: 5, value: pv("follow_strength", 70)}),
    ntDate: h("input", {type: "date"}), ntAp: h("input", {placeholder: "ICAO ou IATA: FOR, SBRF…", style: "text-transform:uppercase"})};
  const nights = [];
  const nightBox = h("div", {class: "tools"});
  const paintNights = () => put(nightBox, nights.map((x, k) => h("button", {class: "chip", type: "button", title: "Tirar",
    onclick: () => { nights.splice(k, 1); paintNights(); }}, `${dm(x.date)} · ${x.airport} ×`)));
  if (prev.icao_type && o.types.some(t => t.type === prev.icao_type)) i.type.value = prev.icao_type;
  const hub = (car.hubs || [])[0];
  i.base.value = prev.base !== undefined && (prev.base === "" || o.bases.some(b => b.icao === prev.base)) ? prev.base : (hub && o.bases.some(b => b.icao === hub) ? hub : "");
  const resTouched = {v: prev.reserve_min_days != null};
  [i.rmin, i.rmax].forEach(x => x.addEventListener("input", () => { resTouched.v = true; }));
  // dias da semana: "Padrão", "Folga" ou "até N" pernas
  const wm = {...((pv("weekday_max", null)) || S.weekday_max || {})};
  const DAYN = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"];
  const wkGrid = h("div", {class: "wk-grid"}, DAYN.map((dn, k) => {
    const e = h("select", {}, h("option", {value: ""}, "Padrão"), h("option", {value: "0"}, "Folga"), [1, 2, 3, 4, 5, 6, 7, 8].map(n => h("option", {value: n}, `até ${n}`)));
    e.value = wm[String(k)] === undefined ? "" : String(wm[String(k)]);
    const cell = h("label", {class: "wk-cell" + (wm[String(k)] === 0 ? " off" : "")}, h("span", {}, dn), e);
    e.addEventListener("change", () => { if (e.value === "") delete wm[String(k)]; else wm[String(k)] = +e.value; cell.classList.toggle("off", e.value === "0"); });
    return cell;
  }));
  const chOut = h("div", {class: "fine"}), intlOut = h("div", {class: "fine"}), folOut = h("div", {class: "fine"});
  const syncFol = () => {
    const v = +i.follow.value;
    const which = v === 0 ? "Desligado: o avião não é seguido (nem as suas regras)."
      : `Força ${v}: as suas continuações sempre; as identificadas no RPL e no ADS-B entram sozinhas — `
        + [["alta", 1, 1], ["média", 30, 0.85], ["baixa", 80, 0.6]].filter(([, m]) => v >= m).map(([c, , w]) => `${c} em ~${Math.round(v * w)}% das vezes`).join(", ")
        + (v < 30 ? " (média a partir de 30)" : v < 80 ? " (baixa a partir de 80)" : "") + ".";
    put(folOut, which);
  };
  i.follow.addEventListener("input", syncFol);
  const syncCh = moved => {
    let a = +i.l2.value, b = +i.l3.value;
    if (a + b > 100) { if (moved === "l3") { a = 100 - b; i.l2.value = a; } else { b = 100 - a; i.l3.value = b; } }
    chBox.hidden = !i.chcustom.checked;
    put(chOut, i.chcustom.checked ? `2 etapas: ${a}% dos dias · 3 etapas: ${b}% · restante ${100 - a - b}%: chaves de 1, 4 ou 5 etapas, na proporção do perfil.`
      : `Sem definir, segue o perfil da companhia (~${(o.chaves_profile || [30, 38])[0]}% com 2 etapas e ~${(o.chaves_profile || [30, 38])[1]}% com 3).`);
  };
  const syncIntl = () => {
    intlBox.hidden = !i.intl.checked; intlSl.hidden = !(i.intl.checked && i.intlc.checked);
    const v = +i.intlp.value;
    put(intlOut, i.intl.checked && i.intlc.checked ? `${v}% das pernas · cerca de 1 a cada ${Math.max(1, Math.round(100 / v))}.` + (car.code === "AZU" ? " A Azul tem poucos internacionais em narrowbody: valores altos podem não ser atingidos." : "")
      : "Sem definir, segue a proporção real vista no ADS-B (LATAM ~5%, GOL ~4%, Azul ~1% das pernas nos narrowbodies).");
  };
  const chBox = h("div", {class: "row"}, field("2 etapas", i.l2), field("3 etapas", i.l3));
  const intlSl = h("div", {}, field("Internacionais (% das pernas)", i.intlp));
  const intlBox = h("div", {style: "margin-left:26px"}, h("label", {class: "ck"}, i.intlc, "Definir a quantidade de voos internacionais"), intlSl, intlOut);
  i.chcustom.addEventListener("change", () => syncCh()); i.l2.addEventListener("input", () => syncCh("l2")); i.l3.addEventListener("input", () => syncCh("l3"));
  [i.intl, i.intlc].forEach(x => x.addEventListener("change", syncIntl)); i.intlp.addEventListener("input", syncIntl);
  const mtWrap = field("Solo mínimo fixo (min)", i.minturn);
  const syncTurn = () => { mtWrap.hidden = i.turnmode.value !== "fixed"; };
  i.turnmode.addEventListener("change", syncTurn);
  const profHint = h("p", {class: "fine", style: "margin:4px 0 8px"});
  const syncProf = () => put(profHint, i.realism.checked ? `${o.profile_text || ""} O gerador mira nas horas de voo e na quantidade de pernas das escalas reais, em vez de encher o dia de pernas curtas.`
    : "Desligado: o gerador enche o dia com o máximo de pernas permitido.");
  i.realism.addEventListener("change", syncProf);
  syncCh(); syncIntl(); syncTurn(); syncProf(); syncFol();
  const fromPosLabel = h("span", {}, "Começar de onde o avião está");
  const posBox = h("div", {class: "panel"}, h("h2", {}, "Mudar de base / recomeçar daqui"), skeleton("l"));
  const loadPos = async () => {
    const v = await get("/api/posicao").catch(() => null);
    if (!v) return put(posBox);
    const pos = v.memorized || v.position || {}, rm = v.remove || {};
    const pname = pos.icao ? `${pos.icao}${pos.place && pos.place.city ? " · " + pos.place.city : ""}` : "";
    i.frompos.checked = !!v.memorized && !rm.legs;
    put(fromPosLabel, `Começar de onde o avião está${pos.icao ? ` (${pos.icao}${pos.at_local ? ", depois de " + pos.at_local + " local" : ""})` : ""}: a escala nova começa no dia seguinte, na base escolhida acima`);
    const doCut = () => dialog("Apagar a escala daqui pra frente?", `Depois do ${pos.flight_number || "último voo de hoje"} em ${pos.icao}`,
      h("div", {}, note("hold", "alerta", `${rm.legs} etapa(s) saem da escala, de ${dm(rm.from)} a ${dm(rm.to)} (com histórico, como uma alteração de escala).`),
        h("p", {class: "muted"}, "O que já foi voado não muda. Dá para desfazer enquanto você não gerar a escala nova.")),
      [closeBtn, {label: "Apagar daqui pra frente", fn: async () => { if (await act(() => post("/api/posicao/cortar"), "Escala apagada · posição memorizada")) render(); }}]);
    put(posBox, h("div", {class: "panel-head"}, h("h2", {}, "Mudar de base / recomeçar daqui"), v.memorized ? chip("posição memorizada", "cool") : null),
      h("p", {style: "margin:0 0 8px"}, pos.icao ? ["Seu avião ", v.memorized ? "ficou" : "fica", " em ", h("b", {}, pname),
        pos.flight_number ? ` depois do ${pos.flight_number}` : "", pos.at_local ? ` (${pos.at_local} local)` : "", "."] : "Sem posição: sem escala e sem base."),
      rm.legs ? h("p", {class: "muted", style: "margin:0 0 10px"}, `Apagar daqui pra frente tira ${rm.legs} etapa(s), de ${dm(rm.from)} a ${dm(rm.to)}`
          + (rm.first ? ` (a próxima seria o ${rm.first.flight_number} ${rm.first.adep}→${rm.first.ades}, ${rm.first.dep_local})` : "")
          + (rm.booked ? `, cancela ${rm.booked} reserva(s) de voo` : "") + (rm.reserves ? `, ${rm.reserves} bloco(s) de reserva` : "")
          + (rm.swaps ? ` e ${rm.swaps} pedido(s) de troca aberto(s)` : "") + ". Depois, escolha a base nova e gere com “Começar de onde o avião está”.")
        : h("p", {class: "muted", style: "margin:0 0 10px"}, v.memorized ? "A escala depois daqui já foi apagada. Escolha a base nova abaixo e gere: ela começa onde o avião ficou."
          : "Não há escala depois do último voo de hoje: dá para gerar direto a partir daqui."),
      v.blocked ? note("hold", "alerta", v.blocked) : null,
      h("div", {class: "tools", style: "flex-wrap:wrap;gap:8px"},
        rm.legs && !v.blocked ? h("button", {class: "btn", onclick: doCut}, "Apagar daqui pra frente") : null,
        v.can_undo ? h("button", {class: "btn ghost", onclick: async () => { if (await act(() => post("/api/posicao/desfazer"), "A escala voltou")) render(); }}, "Desfazer o corte") : null));
    if (!rm.legs && pos.icao) i.frompos.checked = i.frompos.checked || !!v.memorized;
  };
  const offBox = h("div", {class: "tools"});
  const offReq = h("div", {});   // "Folga pedida" do passo 7 (some com "Sem folgas")
  // folgas: automáticas (regras do AeroScale), nenhuma, ou só as marcadas no calendário
  i.offmode = sel("days_off_mode", [["auto", "Automáticas (regras de folga do AeroScale)"], ["none", "Sem folgas"], ["manual", "Só as que eu escolher"]], "auto");
  const offCal = h("div", {class: "offcal-wrap"});
  const offNote = h("p", {class: "fine", style: "margin:6px 0 0"});
  const periodDays = () => {
    const k = i.kind.value, iso = x => x.toISOString().slice(0, 10), D = s => new Date(s + "T00:00:00Z");
    let a, b;
    if (["mensal", "semestral", "anual"].includes(k)) {
      const [y, m] = (i.month.value || df.month).split("-").map(Number), n = k === "mensal" ? 1 : k === "semestral" ? 6 : 12;
      a = i.fromday.value ? D(i.fromday.value) : new Date(Date.UTC(y, m - 1, 1)); b = new Date(Date.UTC(y, m - 1 + n, 0));
    } else if (k === "semanal") { a = D(i.start.value || df.start_date); b = new Date(a.getTime() + 6 * 864e5); }
    else { a = D(i.start.value || df.start_date); b = D(i.end.value || i.start.value || df.start_date); }
    const out = [];
    for (let t = a.getTime(); t <= b.getTime() && out.length < 400; t += 864e5) out.push(iso(new Date(t)));
    return out;
  };
  const paintCal = () => {
    const mode = i.offmode.value;
    offCal.hidden = mode !== "manual"; offReq.hidden = mode === "none";
    [i.cdo, i.longoff].forEach(x => { x.disabled = mode !== "auto"; if (mode !== "auto") x.checked = false; });
    put(offNote, mode === "none" ? "Nenhuma folga: as folgas regulamentares (Lei 13.475, arts. 50 e 51) ficam desligadas e o painel não cobra folga. Jornada, repouso e horas do mês continuam valendo, então pode sobrar dia sem voo quando eles apertam."
      : mode === "manual" ? "Toque nos dias de folga. Só esses viram folga; as regras automáticas de folga ficam desligadas (jornada, repouso e horas do mês continuam valendo)."
      : "O motor distribui as folgas pelas regras (mínimo do mês, máximo de dias seguidos, fim de semana do mês). Folgas pedidas entram no passo 7.");
    if (mode !== "manual") return;
    const days = periodDays();
    if (days.length > 92) { put(offCal, h("p", {class: "fine"}, `Período longo (${days.length} dias): marque as folgas no campo “Folga pedida” do passo 7.`)); return; }
    const pick = new Set(gen.offs), W = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"];
    const first = (new Date(days[0] + "T00:00:00Z").getUTCDay() + 6) % 7;
    put(offCal, h("div", {class: "offcal"}, W.map(w => h("span", {class: "cal-wd"}, w)), Array.from({length: first}, () => h("span", {})),
      days.map((ds, k) => h("button", {type: "button", class: "offday" + (pick.has(ds) ? " on" : "") + (ds.slice(8) === "01" && k ? " m1" : ""), title: dmy(ds),
        onclick: () => { gen.offs = pick.has(ds) ? gen.offs.filter(x => x !== ds) : [...gen.offs, ds].sort(); paintOff(); paintCal(); }},
        ds.slice(8) === "01" && k ? `1/${ds.slice(5, 7)}` : String(+ds.slice(8))))),
      h("div", {class: "tools", style: "margin-top:6px"}, h("span", {class: "fine"}, `${gen.offs.filter(x => days.includes(x)).length} folga(s) marcada(s)`),
        gen.offs.length ? h("button", {class: "btn ghost sm", type: "button", onclick: () => { gen.offs = []; paintOff(); paintCal(); }}, "Limpar") : null));
  };
  const paintOff = () => put(offBox, gen.offs.map(x => h("button", {class: "chip", type: "button", title: "Tirar", onclick: () => { gen.offs = gen.offs.filter(y => y !== x); paintOff(); if (i.offmode.value === "manual") paintCal(); }}, `${dm(x)} ×`)));
  paintOff();
  const periodBox = h("div", {class: "row"});
  const monthly = () => ["mensal", "semestral", "anual"].includes(i.kind.value);
  const paintPeriod = () => put(periodBox, field(monthly() ? (i.kind.value === "mensal" ? "Mês" : "Mês inicial") : i.kind.value === "semanal" ? "Início da semana" : "Data inicial", monthly() ? i.month : i.start),
    monthly() ? field("A partir do dia (opcional)", i.fromday) : null, i.kind.value === "avulsa" ? field("Data final", i.end) : null);
  i.kind.addEventListener("change", paintPeriod); paintPeriod();
  [i.kind, i.month, i.start, i.end, i.fromday].forEach(x => x.addEventListener("change", () => { if (i.offmode.value === "manual") paintCal(); }));
  i.offmode.addEventListener("change", paintCal);
  put(offReq, h("div", {class: "row"}, field("Folga pedida", i.off), h("div", {style: "align-self:end;margin-bottom:13px"},
    h("button", {class: "btn ghost sm", type: "button", onclick: () => { if (i.off.value && !gen.offs.includes(i.off.value)) { gen.offs.push(i.off.value); gen.offs.sort(); paintOff(); if (i.offmode.value === "manual") paintCal(); } }}, "Adicionar"))),
    offBox);
  paintCal();
  const out = h("div", {});
  const csv = v => v.split(/[ ,;]+/).map(x => x.trim().toUpperCase()).filter(Boolean);
  const body = () => {
    const b = {kind: i.kind.value, name: i.name.value.trim() || null, base: i.base.value || null, icao_type: i.type.value, mode: "tripulante",
      swap_min: +i.swap.value || null, allow_deadhead: i.deadhead.checked, profile_realism: i.realism.checked,
      sobreaviso_per_month: i.sob.value === "" ? null : +i.sob.value, sobreaviso_activation_pct: i.soba.value === "" ? null : +i.soba.value,
      long_off: i.longoff.checked, inactive_days: i.inactive.checked, return_to_base: i.rtb.value,
      preferred_airports: csv(i.prefa.value), pref_strength: i.prefs.value, avoid_airports: csv(i.avoida.value),
      leg_min_minutes: +i.lmin.value, leg_max_minutes: +i.lmax.value, legs_min: +i.legsmin.value, legs_max: +i.legsmax.value,
      turn_mode: i.turnmode.value, min_turn: +i.minturn.value, max_turn: +i.maxturn.value, min_rest: +i.minrest.value,
      day_start: i.dstart.value, day_end: i.dend.value, weekday_max: {...wm},
      legs2_pct: i.chcustom.checked ? +i.l2.value : null, legs3_pct: i.chcustom.checked ? +i.l3.value : null,
      type_match: i.tmatch.value, projection: i.proj.checked, avoid_used: i.used.checked, international: i.intl.checked,
      intl_target_pct: i.intl.checked && i.intlc.checked ? +i.intlp.value : null, model_realism: i.model.checked,
      allow_positioning: i.posn.checked, anomaly_repeat_pct: +i.anom.value,
      crew_mode: i.crew.value, days_off_min: +i.offmin.value, max_consecutive_days: +i.maxcons.value,
      crew_days_off: i.cdo.checked, crew_folga_base: i.cfb.checked, crew_madrugada: i.cmad.checked, crew_monthly: i.cmon.checked,
      reserve_blocks: i.res.value === "" ? null : +i.res.value,
      reserve_min_days: resTouched.v ? +i.rmin.value : null, reserve_max_days: resTouched.v ? +i.rmax.value : null,
      reserve_base_pct: +i.rbase.value, reserve_activation_pct: +i.ract.value, reserve_activation_max_days: +i.ractd.value,
      avoid_madrugada: i.madr.checked, time_pref: i.pref.value || null, seed: i.seed.value === "" ? null : +i.seed.value, requested_off: gen.offs,
      overnights: nights.map(x => ({date: x.date, airport: x.airport})), days_off_mode: i.offmode.value};
    if (b.days_off_mode === "none") b.requested_off = [];
    if (car.code === "GLO") b.include_cargo = i.cargo.checked;
    b.follow_strength = +i.follow.value;
    if (["mensal", "semestral", "anual"].includes(b.kind)) b.month = i.month.value; else { b.start_date = i.start.value; if (b.kind === "avulsa") b.end_date = i.end.value || i.start.value; }
    if (i.frompos.checked) b.from_position = true;
    if (monthly() && i.fromday.value) b.start_day = i.fromday.value;
    else if (i.replace.checked) b.start_day = monthly() ? `${b.month}-01` : b.start_date;   // substitui o que já existe nesses dias
    if (b.start_day) b.replace_from_day = i.replace.checked;
    return b;
  };
  const remember = b => { try { localStorage.setItem(LS, JSON.stringify({...b, name: undefined, seed: undefined, requested_off: undefined, start_day: undefined, overnights: undefined,
    from_position: undefined, month: undefined, start_date: undefined, end_date: undefined})); } catch (e) { /* só conveniência */ } };
  const showJob = j => {
    if (j.state === "running") return put(out, h("div", {class: "panel"}, h("h2", {}, "Gerando a escala…"),
      h("div", {class: "gen-bar"}, h("span", {style: `width:${Math.round((j.progress || 0) * 100)}%`})), h("p", {class: "muted", style: "margin:8px 0"}, `${cap(j.stage || "")} · ${j.elapsed_s}s`),
      h("button", {class: "btn ghost sm", onclick: () => post(`/api/motor/tarefas/${j.id}/cancelar`)}, "Cancelar")));
    clearInterval(gen.timer);
    if (j.state === "error") return put(out, note("stop", "alerta", j.error || "A geração falhou."));
    if (j.state === "cancelled") return put(out, note("", "info", "Geração cancelada."));
    const r = j.result, im = r.import || {};
    put(out, h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, r.name || "Escala gerada"), chip(`${r.legs} pernas`, "go")),
      h("p", {style: "margin:0 0 10px"}, `De ${dmy(r.start_date)} a ${dmy(r.end_date)} · na sua escala: ${im.legs_new || 0} novas, ${im.legs_changed || 0} alteradas, ${im.legs_removed || 0} removidas. Gerada em ${j.elapsed_s}s.`),
      h("div", {class: "gen-diags"}, (r.diagnostics || []).map(x => h("div", {class: "gd " + (x.level || "")}, h("b", {}, x.level === "error" ? "✘" : x.level === "warning" ? "!" : "·"), h("span", {}, x.message)))),
      (im.warnings || []).map(w => note("hold", "alerta", w)),
      h("div", {class: "tools", style: "margin-top:12px"}, h("a", {class: "btn", href: `#/escala/${r.start_date}`}, "Ver a escala"),
        h("button", {class: "btn ghost", onclick: () => { i.seed.value = ""; go(); }}, "Gerar de novo (outra sorte)"))));
  };
  const go = async () => {
    const b = body();
    if ([0, 1, 2, 3, 4, 5, 6].every(k => wm[String(k)] === 0)) return toast("Todos os dias estão como folga.", true);
    remember(b);
    const j = await act(() => post("/api/motor/gerar", b));
    if (!j) return;
    gen.job = j.id; showJob(j); clearInterval(gen.timer);
    gen.timer = setInterval(async () => { if (!location.hash.startsWith("#/gerar")) return clearInterval(gen.timer);
      try { showJob(await get(`/api/motor/tarefas/${gen.job}`)); } catch (e) { clearInterval(gen.timer); put(out, note("stop", "alerta", e.message)); } }, 500);
  };
  const cov = st.coverage || {};
  const real = {txt: h("textarea", {rows: 8, class: "mono", placeholder: "(01) GRU-FOR-GRU (1554,1555)\n(02) FR\n(03) RESERVA GRU\n(04) SOBREAVISO"}), month: h("input", {type: "month", value: df.month}), base: h("input", {placeholder: "base (opcional)"})};
  const step = (n, t) => h("h2", {class: "gen-step"}, h("span", {class: "step-n"}, n), t);
  const saveDefault = async () => {
    const r = await act(() => post("/api/motor/padrao", {weekday_max: {...wm}, crew_mode: i.crew.value, crew_days_off: i.cdo.checked, crew_folga_base: i.cfb.checked,
      crew_madrugada: i.cmad.checked, crew_monthly: i.cmon.checked, days_off_min: +i.offmin.value, max_consecutive_days: +i.maxcons.value, turn_mode: i.turnmode.value}),
      "Padrão salvo: vale para as próximas escalas");
    return r;
  };
  const wipeBox = h("div", {class: "panel danger-zone"}, h("h2", {}, "Apagar todas as escalas"),
    h("p", {class: "muted"}, "Tira todas as pernas, folgas, reservas e sobreavisos desta carreira, para começar do zero. O diário de bordo, os voos já feitos, os salários e as mensagens ficam. Antes, sai um backup completo."),
    h("button", {class: "btn stop sm", onclick: async () => {
      const w = await act(() => get("/api/escala/apagar-tudo"));
      if (!w) return;
      const conf = h("input", {placeholder: "APAGAR", autocomplete: "off", style: "text-transform:uppercase"});
      dialog("Apagar todas as escalas?", `${car.name || ""}: ${w.legs} etapa(s) na escala${w.booked ? `, ${w.booked} reservada(s)` : ""}, ${w.reserves} bloco(s) de reserva.`,
        h("div", {}, note("stop", "alerta", "A escala inteira desta carreira sai. O diário de bordo (", String(w.logbook), " voo(s)) e o resto ficam. Um backup completo é feito antes."),
          field("Digite APAGAR para confirmar", conf)),
        [closeBtn, {label: "Apagar todas", cls: "stop", fn: async () => {
          const r = await act(() => post("/api/escala/apagar-tudo", {confirmar: conf.value.trim().toUpperCase()}), "Todas as escalas apagadas");
          if (!r) return false;
          roster.bounds = null; render();
        }}]);
    }}, "Apagar todas as escalas…"));
  put(main, h("div", {class: "head"}, h("div", {}, h("h1", {}, `Gerar escala · ${car.name || ""}`),
      h("div", {class: "sub"}, `Motor do ${st.engine} dentro da LineOps, com todos os ajustes do gerador do AeroScale: escala do tripulante com o RPL do CGNA, calibrada com escalas reais de comandante.`)),
      h("div", {class: "tools"}, h("a", {class: "btn ghost sm", href: "#/escala"}, "Voltar à escala"))),
    h("div", {class: "cols-2 gen-cols"},
      h("div", {},
        posBox,
        h("div", {class: "panel"}, step(1, "Período"), h("div", {class: "hr"}),
          field("Tipo de escala", i.kind), periodBox, field("Nome da escala (opcional)", i.name),
          h("label", {class: "ck"}, i.replace, "Substituir a escala que já existe nesses dias (o que vem antes fica; continua de onde o avião estava)"),
          h("label", {class: "ck"}, i.frompos, fromPosLabel),
          h("div", {class: "subpanel"}, field("Folgas", i.offmode), offNote, offCal)),
        h("div", {class: "panel"}, step(2, "Tripulante"), h("div", {class: "hr"}),
          h("p", {class: "fine", style: "margin-top:0"}, "Como na vida real: o piloto segue a habilitação no tipo, fica com a aeronave enquanto ela segue o rodízio real e troca de aeronave nos hubs."),
          h("div", {class: "row"}, field("Habilitação (tipo)", i.type), field("Conexão mínima com troca de aeronave (min)", i.swap)),
          h("label", {class: "ck"}, i.deadhead, "Voltar como extra (passageiro) em voo real quando precisar chegar à base ou à reserva"),
          h("div", {class: "subpanel"}, h("label", {class: "ck"}, i.realism, h("b", {}, "Realismo das escalas reais")), profHint,
            h("div", {class: "row"}, field("Sobreaviso por mês", i.sob), field("Chance de acionar o sobreaviso (%)", i.soba)),
            h("p", {class: "fine", style: "margin:0 0 6px"}, "Sobreaviso: em casa, 3 a 12 h; no máximo 8 por mês (Lei 13.475, art. 43)."),
            h("label", {class: "ck"}, i.longoff, "Um bloco longo de folga no mês (5 a 7 dias) e no máximo uma folga isolada"),
            h("label", {class: "ck"}, i.inactive, "Dia INATIVO fora da base (depois de 2 madrugadas ou no meio da viagem)"))),
        h("div", {class: "panel"}, step(3, "Aeroportos"), h("div", {class: "hr"}),
          h("div", {class: "row"}, field("Base / início", i.base), field("Retorno à base", i.rtb)),
          field("Aeroportos de preferência (escala direcionada)", i.prefa),
          h("p", {class: "fine", style: "margin:-6px 0 10px"}, "O gerador puxa a escala para esses aeroportos e, quando não há voo direto, para os que têm voo até eles."),
          h("div", {class: "row"}, field("Força da preferência", i.prefs), field("Evitar como destino", i.avoida))),
        h("div", {class: "panel"}, step(4, "Pernas e tempos"), h("div", {class: "hr"}),
          h("div", {class: "row"}, field("Duração mín. da perna (min)", i.lmin), field("Duração máx. da perna (min)", i.lmax)),
          h("div", {class: "row"}, field("Pernas por dia (mín.)", i.legsmin), field("Pernas por dia (máx.)", i.legsmax)),
          h("div", {class: "row"}, field("Solo mínimo", i.turnmode), mtWrap, field("Solo máximo (min)", i.maxturn)),
          h("div", {class: "row"}, field("Descanso noturno mín. (min)", i.minrest), field("Partidas a partir de (local)", i.dstart), field("até", i.dend)),
          h("div", {class: "f"}, h("span", {}, "Máximo de pernas por dia da semana"), wkGrid,
            h("p", {class: "fine", style: "margin:6px 0 0"}, "“Padrão” usa o máximo de pernas por dia acima; o limite do dia tem prioridade. Ex.: terça “até 1”, quinta “Folga”.")),
          h("div", {class: "subpanel"}, h("label", {class: "ck"}, i.chcustom, "Definir a chance de chaves com 2 e 3 etapas"), chBox, chOut),
          field("Compatibilidade de tipo", i.tmatch),
          h("label", {class: "ck"}, i.proj, "Projetar o padrão semanal do RPL fora da validade (pernas marcadas como projetadas)"),
          h("label", {class: "ck"}, i.used, "Não reutilizar voos já atribuídos em outras escalas (as suas escalas antigas do mesmo período não contam: são substituídas)"),
          h("label", {class: "ck"}, i.intl, "Incluir voos internacionais reais observados no ADS-B (os RPL do CGNA só têm voos domésticos)"), intlBox,
          h("label", {class: "ck"}, i.model, "Priorizar sequências prováveis (modelo de rodízio calibrado com rodízios reais)"),
          h("label", {class: "ck"}, i.posn, "Permitir pernas de posicionamento sugeridas quando ficar sem voos (sem número de voo, marcadas como sugestão)"),
          h("div", {class: "row"}, field("Força das continuações do avião (beta)", i.follow), h("div", {style: "align-self:end;margin-bottom:13px"},
            h("a", {href: "#/continuacoes", class: "btn ghost sm"}, "Ver e cadastrar"))), folOut,
          car.code === "GLO" ? h("label", {class: "ck"}, i.cargo, "Incluir voos de carga (GOL 9xxx, normalmente GOLLOG). Desligado, eles ficam fora da escala e das trocas") : null,
          h("div", {class: "row"}, field("Chance de anormalidade: repetição de trecho (% dos dias)", i.anom))),
        h("div", {class: "panel"}, step(5, "Regras de tripulação"), h("div", {class: "hr"}),
          field("Perfil", i.crew), h("div", {class: "row"}, field("Folgas mínimas/mês", i.offmin), field("Máx. dias seguidos", i.maxcons)),
          h("label", {class: "ck"}, i.cdo, "Folgas regulamentares: mínimo mensal proporcional, folga após no máximo 6 dias e um sábado+domingo no mês; retorno da folga a partir das 10:00"),
          h("label", {class: "ck"}, i.cfb, "Folga começa na base (o dia anterior à folga termina na base)"),
          h("label", {class: "ck"}, i.cmad, "Madrugadas: no máximo 2 seguidas e 4 em 7 dias (00:00–06:00)"),
          h("label", {class: "ck"}, i.cmon, "Limites de 80 h de voo/mês (jato) ou 85 h (turbo-hélice), 44 h semanais e 176 h mensais de jornada"),
          h("p", {class: "fine"}, "Sempre aplicados com o perfil ligado: repouso de 12/16/24 h conforme a jornada (Lei 13.475, art. 48). Regras internas de cada empresa (acordos coletivos) não são públicas; ajuste os limites acima se quiser algo mais restrito."),
          h("button", {class: "btn ghost sm", type: "button", onclick: saveDefault}, "Salvar dias da semana e regras como padrão")),
        h("div", {class: "panel"}, step(6, "Reserva automática"), h("div", {class: "hr"}),
          h("div", {class: "row"}, field("Blocos de reserva", i.res), field("Duração mínima (dias)", i.rmin), field("máxima", i.rmax)),
          h("div", {class: "row"}, field("Chance de ser na base (%)", i.rbase), field("Chance de acionamento (%)", i.ract), field("Acionamento dura até (dias)", i.ractd)),
          h("p", {class: "fine"}, "Sorteada entre 3 e 6 dias seguidos. Acionada, recebe voos reais do RPL que saem do local da reserva e voltam a ele; sem sequência compatível, o acionamento fica registrado como inviável, sem inventar voos.")),
        h("div", {class: "panel"}, step(7, "Seu pedido"), h("div", {class: "hr"}),
          offReq, h("div", {class: "row", style: "margin-top:8px"}, field("Período do dia", i.pref), field("Semente (repetir uma escala)", i.seed)),
          h("label", {class: "ck"}, i.madr, "Evitar madrugada"),
          h("div", {class: "subpanel"}, h("b", {}, "Pernoite pedido"),
            h("p", {class: "fine", style: "margin:4px 0 8px"}, "Escolha a noite e o aeroporto onde quer dormir. O motor termina o dia ali com voos reais do RPL, dentro das regras; folga regulamentar no caminho troca de dia. Se não der, a escala sai mesmo assim e o diagnóstico diz por quê."),
            h("div", {class: "row"}, field("Noite de", i.ntDate), field("Aeroporto", i.ntAp), h("div", {style: "align-self:end;margin-bottom:13px"},
              h("button", {class: "btn ghost sm", type: "button", onclick: () => {
                const ap = i.ntAp.value.trim().toUpperCase();
                if (!i.ntDate.value || !ap) return toast("Escolha a noite e o aeroporto.", true);
                if (nights.some(x => x.date === i.ntDate.value)) return toast("Já tem pernoite pedido nessa noite.", true);
                if (nights.length >= 8) return toast("No máximo 8 pernoites pedidos.", true);
                nights.push({date: i.ntDate.value, airport: ap}); nights.sort((a, b) => a.date < b.date ? -1 : 1); i.ntAp.value = ""; paintNights();
              }}, "Adicionar"))), nightBox)),
        h("div", {class: "tools gen-go"}, h("button", {class: "btn", onclick: go}, "Gerar escala")), out),
      h("div", {},
        h("div", {class: "panel"}, h("h2", {}, "RPL do CGNA"), h("div", {class: "hr"}),
          h("dl", {class: "kv"}, h("dt", {}, "Companhia"), h("dd", {}, car.full || car.name), h("dt", {}, "Semana publicada"), h("dd", {}, cov.from ? `${dmy(cov.from)} a ${dmy(cov.to)}` : "—"),
            h("dt", {}, "Semanas guardadas"), h("dd", {}, (st.rpl_weeks || []).length), h("dt", {}, "Tipos no RPL"), h("dd", {}, o.types.map(t => t.type).join(", ") || "—"),
            h("dt", {}, "Última verificação"), h("dd", {}, st.update && st.update.state && st.update.state.checked_at ? dt(st.update.state.checked_at) : "—")),
          h("p", {class: "fine"}, "Dias fora da semana publicada repetem a semana mais nova (ou a mesma semana do ano anterior) e aparecem como PROJETADO. O RPL novo vem sozinho ao abrir (no máximo a cada 12 h), ou pelo botão."),
          h("button", {class: "btn ghost sm", onclick: async () => { const r = await act(() => post("/api/motor/rpl/atualizar")); if (r) { toast((r.results || []).map(x => `${x.airline}: ${x.message}`).join(" · ") || "Verificado"); render(); } }}, "Atualizar RPL agora")),
        (st.generated || []).length ? h("div", {class: "panel"}, h("h2", {}, "Já geradas"), h("div", {class: "wrap-list"}, st.generated.map(x => h("div", {class: "wl"},
          h("span", {class: "mono wl-k"}, dm(x.created_at)), h("span", {}, x.name), h("a", {href: `#/escala/${x.start_date}`, class: "muted"}, "ver"))))) : null,
        h("details", {class: "panel"}, h("summary", {}, h("b", {}, "Importar escala real (texto)")),
          h("p", {class: "fine"}, "Cole a escala como circula entre tripulantes, um dia por linha. Cada voo é procurado no RPL pelo número, trecho e data."),
          real.txt, h("div", {class: "row", style: "margin-top:8px"}, field("Mês da primeira linha", real.month), field("Base", real.base)),
          h("button", {class: "btn sm", onclick: async () => { const r = await act(() => post("/api/motor/escala-real", {text: real.txt.value, month: real.month.value, base: real.base.value || null}), "Escala real importada"); if (r) location.hash = "#/escala"; }}, "Importar")),
        wipeBox)));
  loadPos();
  if (gen.job) get(`/api/motor/tarefas/${gen.job}`).then(j => { if (j.state === "running") { showJob(j); clearInterval(gen.timer);
    gen.timer = setInterval(async () => { if (!location.hash.startsWith("#/gerar")) return clearInterval(gen.timer); showJob(await get(`/api/motor/tarefas/${gen.job}`)); }, 500); } }).catch(() => {});
});

/* ───────────────────────── Voos Externos (fora das OPS) ───────────────────────── */
const ext = {f: {family: "", origin: "", dest: "", min_h: "", max_h: ""}, timer: null};
const extQs = () => "?" + Object.entries(ext.f).filter(([, v]) => v !== "").map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
const nextSat = () => { const d = new Date(); d.setUTCDate(d.getUTCDate() + ((6 - d.getUTCDay() + 7) % 7 || 7)); d.setUTCHours(22, 0, 0, 0); return d.toISOString().slice(0, 16); };
function copyText(txt, label = "Copiado") {
  const ok = () => toast(label);
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(txt).then(ok).catch(() => fallback());
  fallback();
  function fallback() { const t = h("textarea", {style: "position:fixed;opacity:0"}); t.value = txt; document.body.append(t); t.select();
    try { document.execCommand("copy"); ok(); } catch (e) { toast("Selecione e copie o texto.", true); } t.remove(); }
}
function routeBig(o, d, on, dn) {
  return h("div", {class: "ext-route"}, h("div", {}, h("b", {}, o), h("small", {}, on || "")), h("span", {class: "ext-arc", "aria-hidden": "true"}, "· · ✈ · ·"),
    h("div", {style: "text-align:right"}, h("b", {}, d), h("small", {}, dn || "")));
}
function extGroupDialog(c) {
  const rowsBox = h("div", {class: "ext-members"});
  const addRow = (v = {}) => {
    const r = {name: h("input", {value: v.name || "", placeholder: "nome"}), net: h("select", {}, ["IVAO", "VATSIM", "OFFLINE", "OUTRA"].map(n => h("option", {value: n, selected: (v.network || "IVAO") === n}, n))),
      cs: h("input", {value: v.callsign || "", placeholder: "auto", maxlength: 10, style: "text-transform:uppercase"}),
      ty: h("input", {value: v.type_icao || "", placeholder: "mesmo", maxlength: 4, style: "text-transform:uppercase"})};
    const el = h("div", {class: "ext-mrow"}, r.name, r.net, r.cs, r.ty, h("button", {class: "btn ghost sm", type: "button", title: "Tirar", onclick: () => { el.remove(); rows.splice(rows.indexOf(r), 1); }}, "×"));
    rows.push(r); rowsBox.append(el);
  };
  const rows = [];
  addRow({name: me.display_name || "Eu"}); addRow();
  const i = {when: h("input", {type: "datetime-local", value: nextSat()}), st: h("input", {type: "number", min: 0, max: 30, value: 3}),
    nt: h("input", {placeholder: "opcional: ex. Discord às 21:30Z, nível de cruzeiro combinado"}),
    o: h("input", {placeholder: "SBGR", maxlength: 4, style: "text-transform:uppercase"}), d: h("input", {placeholder: "LPPT", maxlength: 4, style: "text-transform:uppercase"}),
    ty: h("input", {placeholder: "B77W", maxlength: 4, style: "text-transform:uppercase", value: c ? c.type_icao : ""}), cs: h("input", {placeholder: "TAP88 (opcional)", maxlength: 10, style: "text-transform:uppercase"})};
  dialog(c ? `Voo em grupo · ${c.callsign}` : "Voo em grupo · rota manual",
    c ? `${c.adep} ${c.adep_name || ""} → ${c.ades} ${c.ades_name || ""} · ${c.type_name} (voo real da ${c.airline_name}, visto no ADS-B). Horários em UTC.` : "Rota sua, sem voo real por trás. Horários em UTC.",
    h("div", {}, c ? null : h("div", {class: "row"}, field("Origem (ICAO)", i.o), field("Destino (ICAO)", i.d), field("Tipo", i.ty), field("Callsign", i.cs)),
      c ? field("Tipo de vocês (o real é " + c.type_icao + ")", i.ty) : null,
      h("div", {class: "row"}, field("Saída do grupo (UTC)", i.when), field("Intervalo entre saídas (min)", i.st)),
      h("h3", {style: "margin:6px 0 8px"}, "Quem voa"), h("div", {class: "ext-mhead"}, h("span", {}, "Nome"), h("span", {}, "Rede"), h("span", {}, "Callsign"), h("span", {}, "Tipo"), h("span", {})),
      rowsBox, h("button", {class: "btn ghost sm", type: "button", onclick: () => addRow()}, icon("mais", 14), "Adicionar amigo"),
      field("Observação", i.nt)),
    [closeBtn, {label: "Montar o voo", fn: async () => {
      const body = {dep_utc: i.when.value, stagger_min: +i.st.value || 0, notes: i.nt.value || null, type_icao: i.ty.value.trim().toUpperCase() || null,
        members: rows.map(r => ({name: r.name.value.trim(), network: r.net.value, callsign: r.cs.value.trim().toUpperCase() || null, type_icao: r.ty.value.trim().toUpperCase() || null})).filter(m => m.name)};
      if (c) body.callsign = c.callsign; else Object.assign(body, {adep: i.o.value.trim().toUpperCase(), ades: i.d.value.trim().toUpperCase(), callsign: i.cs.value.trim().toUpperCase() || null});
      const r = await act(() => post("/api/externos", body), "Voo montado. Agora é mandar o convite!");
      if (!r) return false;
      render();
      setTimeout(() => inviteDialog(r), 50);
    }}]);
}
function inviteDialog(f) {
  const ta = h("textarea", {rows: 12, readonly: true, class: "mono", style: "font-size:13px"}, f.invite);
  dialog("Convite para o grupo", "Cole no WhatsApp ou no Discord.", ta,
    [closeBtn, {label: "Copiar", keep: true, fn: () => copyText(f.invite, "Convite copiado")}]);
}
function extRadar(f) {
  const box = h("div", {}, h("p", {class: "muted"}, "Procurando o grupo na IVAO e na VATSIM…"));
  const paint = r => put(box,
    h("div", {class: "ext-radar"}, r.members.map(m => h("div", {class: "ext-rr" + (m.online ? " on" : "")},
      h("div", {}, h("b", {}, m.name), h("small", {}, `${m.network} · ${m.callsign || "sem callsign"}`)),
      m.online ? h("div", {class: "ext-bar"}, h("span", {style: `width:${m.progress ?? 0}%`})) : h("span", {class: "muted", style: "font-size:13px"}, "fora da rede"),
      m.online ? h("small", {class: "mono"}, m.on_ground ? "no solo" : `${Math.round(m.alt_ft || 0).toLocaleString("pt-BR")} ft · ${m.gs_kt ?? "—"} kt`,
        m.remaining_nm != null ? ` · faltam ${m.remaining_nm.toLocaleString("pt-BR")} nm` : "", m.plan_ok === false ? ` · plano ${m.plan}` : "") : null))),
    Object.entries(r.errors || {}).map(([k, v]) => note("hold", "alerta", `${k}: ${v}`)),
    h("p", {class: "fine"}, `${r.note} Lido às ${hhmmz(r.at)}; atualiza a cada 30 s.`));
  const load = () => get(`/api/externos/${f.id}/radar`).then(paint).catch(e => put(box, note("hold", "alerta", e.message)));
  dialog(`Radar · ${f.callsign || f.adep + "→" + f.ades}`, `${f.adep} → ${f.ades}`, box, [{label: "Fechar", cls: "ghost", fn: () => { clearInterval(ext.timer); }}]);
  load(); clearInterval(ext.timer); ext.timer = setInterval(() => { if (!document.getElementById("dlg").open) return clearInterval(ext.timer); load(); }, 30000);
}
function extResults(f) {
  const ins = f.members.map(m => ({m, b: h("input", {value: m.block_min != null ? `${Math.floor(m.block_min / 60)}:${String(m.block_min % 60).padStart(2, "0")}` : "", placeholder: "14:35"}),
    v: h("input", {type: "number", value: m.landing_fpm ?? "", placeholder: "fpm"}), g: h("input", {type: "number", step: "0.01", value: m.landing_g ?? "", placeholder: "G"})}));
  dialog("Resultados do voo", "Cada um informa o bloco e o toque. O pódio sai daqui.",
    h("div", {class: "ext-res"}, h("div", {class: "ext-rhead"}, h("span", {}, ""), h("span", {}, "Bloco"), h("span", {}, "Razão (fpm)"), h("span", {}, "G")),
      ins.map(x => h("div", {class: "ext-rrow"}, h("b", {}, x.m.name), x.b, x.v, x.g))),
    [closeBtn, {label: "Salvar", fn: async () => {
      for (const x of ins) {
        if (x.b.value === "" && x.v.value === "" && x.g.value === "") continue;
        const r = await act(() => api("PUT", `/api/externos/membro/${x.m.id}/resultado`, {block: x.b.value || null, landing_fpm: x.v.value === "" ? null : +x.v.value, landing_g: x.g.value === "" ? null : +x.g.value}));
        if (!r) return false;
      }
      toast("Resultados salvos"); render();
    }}]);
}
function extWx(f) {
  const box = h("div", {}, h("p", {class: "muted"}, "Lendo METAR e TAF…"));
  dialog(`Tempo · ${f.adep} e ${f.ades}`, "aviationweather.gov, lido agora pelo servidor do LineOps.", box, [closeBtn]);
  get(`/api/externos/${f.id}/tempo`).then(w => put(box, [...(w.metars || []), ...(w.tafs || [])].length ? h("div", {class: "mono", style: "font-size:13px;white-space:pre-wrap"},
    (w.metars || []).map(m => (m.raw || `METAR ${m.icao}: sem leitura`) + "\n"), "\n", (w.tafs || []).map(t => (t.raw || "") + "\n\n")) : note("hold", "alerta", w.error || "Sem METAR agora (o servidor está sem internet?).")))
    .catch(e => put(box, note("hold", "alerta", e.message)));
}
function extCard(f) {
  const past = f.status === "VOADO";
  return h("div", {class: "panel ext-card" + (past ? " past" : "")},
    h("div", {class: "panel-head"}, h("div", {}, h("h2", {style: "margin:0"}, f.callsign || "Voo em grupo"), h("small", {class: "muted"}, [f.airline_name, f.type_name, f.source === "ADSB" ? "voo real (ADS-B)" : "rota manual"].filter(Boolean).join(" · "))),
      chip(past ? "voado" : "planejado", past ? "go" : "cool")),
    routeBig(f.adep, f.ades, f.adep_name, f.ades_name),
    h("div", {class: "ext-facts"}, h("span", {}, h("b", {}, `${dm(f.dep_utc)} ${hhmmz(f.dep_utc)}`), f.dep_local ? ` (${f.dep_local.hhmm} local)` : ""),
      f.km ? h("span", {}, `${Math.round(f.km).toLocaleString("pt-BR")} km`) : null, f.est_block_min ? h("span", {}, `~${hmLong(f.est_block_min)} (estimado)`) : null,
      f.arr_utc ? h("span", {}, `chegada ~${hhmmz(f.arr_utc)}${f.arr_local ? ` (${f.arr_local.hhmm} local)` : ""}`) : null),
    h("div", {class: "ext-mlist"}, f.members.map(m => h("div", {}, h("span", {class: "mono"}, hhmmz(m.dep_utc)), h("b", {}, m.name), h("span", {class: "muted ext-net"}, `${m.network}${m.callsign ? " · " + m.callsign : ""}${m.type_icao && m.type_icao !== f.type_icao ? " · " + m.type_icao : ""}`),
      h("a", {href: m.simbrief, target: "_blank", rel: "noopener noreferrer", class: "muted ext-sb", style: "font-size:12.5px"}, "SimBrief")))),
    f.podium.length ? h("div", {class: "ext-podium"}, f.podium.slice(0, 3).map(p => h("div", {class: "pod p" + p.pos}, h("span", {}, ["🥇", "🥈", "🥉"][p.pos - 1]), h("b", {}, p.name), h("small", {}, `${p.fpm} fpm${p.g ? " · " + p.g.toFixed(2) + " G" : ""}`)))) : null,
    f.notes ? h("p", {class: "fine", style: "margin:8px 0 0"}, f.notes) : null,
    h("div", {class: "tools", style: "margin-top:12px"},
      h("button", {class: "btn sm", onclick: () => inviteDialog(f)}, "Convite"),
      h("button", {class: "btn ghost sm", onclick: () => extRadar(f)}, h("span", {class: "radar", "aria-hidden": "true", style: "width:14px;height:14px"}), "Radar do grupo"),
      h("button", {class: "btn ghost sm", onclick: () => extWx(f)}, "Tempo"),
      h("button", {class: "btn ghost sm", onclick: () => extResults(f)}, "Resultados"),
      !past ? h("button", {class: "btn ghost sm", onclick: async () => { if (await act(() => post(`/api/externos/${f.id}/cancelar`), "Voo cancelado")) render(); }}, "Cancelar") : null));
}
view("externos", async main => {
  const [lst, cat] = await Promise.all([get("/api/externos"), get("/api/externos/catalogo" + extQs())]);
  const busy = h("span", {class: "muted", style: "font-size:13px"});
  const fetchNow = async ev => {
    const b = ev.currentTarget; b.disabled = true; put(busy, "Perguntando ao ADS-B quem está no ar… (até 1 min)");
    const r = await act(() => post("/api/externos/buscar"));
    b.disabled = false; put(busy);
    if (r) { toast(`${r.kept} long haul${r.kept === 1 ? "" : "s"} com rota real${r.cached ? " (busca de minutos atrás)" : ""}`); render(); }
  };
  const fi = {family: h("select", {}, h("option", {value: ""}, "Qualquer aeronave"), cat.families.map(x => h("option", {value: x, selected: ext.f.family === x}, x))),
    origin: h("input", {value: ext.f.origin, placeholder: "origem: SBGR, BR, K…", style: "text-transform:uppercase"}),
    dest: h("input", {value: ext.f.dest, placeholder: "destino", style: "text-transform:uppercase"}),
    min_h: h("input", {type: "number", min: 0, max: 20, step: "0.5", value: ext.f.min_h, placeholder: "mín. h"}),
    max_h: h("input", {type: "number", min: 0, max: 20, step: "0.5", value: ext.f.max_h, placeholder: "máx. h"})};
  const apply = () => { Object.keys(fi).forEach(k => ext.f[k] = fi[k].value.trim()); render(); };
  Object.values(fi).forEach(el => el.addEventListener("change", apply));
  const draw = async () => { try { extGroupDialog(await get("/api/externos/sortear" + extQs())); } catch (e) { toast(e.message, true); } };
  put(main,
    h("div", {class: "head ext-head"}, h("div", {}, h("h1", {}, "Voos Externos"),
      h("div", {class: "sub"}, "Fora das OPS: long haul de verdade, visto no ADS-B, para voar com os amigos. Não mexe na escala, no diário nem nos salários das suas carreiras.")),
      h("div", {class: "tools"}, h("button", {class: "btn", onclick: fetchNow}, "Buscar voos no ar agora"), h("button", {class: "btn ghost", onclick: () => extGroupDialog(null)}, "Rota manual"))),
    busy,
    lst.upcoming.length ? h("section", {}, h("h2", {class: "ext-sec"}, "Próximos voos do grupo"), h("div", {class: "ext-cards"}, lst.upcoming.map(extCard))) : null,
    h("section", {}, h("h2", {class: "ext-sec"}, "Voos reais para escolher"),
      h("div", {class: "ext-filters"}, fi.family, fi.origin, fi.dest, fi.min_h, fi.max_h, h("button", {class: "btn sm", onclick: draw}, "🎲 Sortear")),
      cat.items.length ? h("div", {class: "ext-cat"}, cat.items.map(c => h("div", {class: "ext-ci"},
        h("div", {class: "ext-cs"}, h("b", {class: "mono"}, c.callsign), h("small", {}, c.airline_name)),
        h("div", {}, h("b", {}, `${c.adep} → ${c.ades}`), h("small", {}, `${c.adep_name || ""} → ${c.ades_name || ""}`)),
        h("div", {}, h("b", {}, c.type_icao), h("small", {}, c.registration || "")),
        h("div", {}, h("b", {}, `~${hmLong(c.est_block_min)}`), h("small", {}, `${Math.round(c.km).toLocaleString("pt-BR")} km`)),
        h("small", {class: "muted"}, `visto ${dm(c.last_seen)} ${hhmmz(c.last_seen)}`),
        h("button", {class: "btn ghost sm", onclick: () => extGroupDialog(c)}, "Voar com o grupo"))))
        : h("div", {class: "empty"}, icon("rede", 26), h("div", {}, cat.total ? "Nenhum voo com esses filtros." : "O catálogo está vazio. Clique em “Buscar voos no ar agora”: o LineOps pega os long-haulers que estão voando e a rota real de cada um.")),
      h("p", {class: "fine"}, `${cat.attribution} Long haul a partir de ${cat.min_km.toLocaleString("pt-BR")} km. Duração estimada pelo cruzeiro típico do tipo, sem vento (o SimBrief calcula o real). Catálogo: ${cat.total} voo(s) vistos.`)),
    lst.leaderboard.length ? h("section", {}, h("h2", {class: "ext-sec"}, "Quem pousa melhor"), h("div", {class: "panel"}, h("div", {class: "wrap-list"},
      lst.leaderboard.map((x, k) => h("div", {class: "wl"}, h("span", {class: "mono wl-k"}, `${k + 1}º`), h("span", {}, h("b", {}, x.name), ` · média ${x.avg_fpm} fpm · melhor ${x.best_fpm} fpm`),
        h("b", {}, `${x.flights} voo${x.flights > 1 ? "s" : ""}${x.hard ? ` · ${x.hard} duro` : ""}`)))))) : null,
    lst.past.length ? h("section", {}, h("h2", {class: "ext-sec"}, "Já voados"), h("div", {class: "ext-cards"}, lst.past.slice(0, 8).map(extCard))) : null);
});

/* ───────────────────────── pernoite: hotel, carro, despertar ───────────────────────── */
function stayCard(f) {
  if (!f || ["CANCELADO"].includes(f.state)) return null;
  const box = h("div", {class: "panel stay-card", hidden: true});
  get(`/api/stay/flight/${f.id}`).then(d => {
    const t = d.timeline;
    if (!t) return;
    const loc = iso => { if (!iso) return "—"; try { return new Intl.DateTimeFormat("pt-BR", {hour: "2-digit", minute: "2-digit", timeZone: d.tz || "UTC"}).format(new Date(iso)) + (d.tz ? "" : "Z"); } catch (e) { return hhmmz(iso); } };
    const day = iso => { try { return new Intl.DateTimeFormat("pt-BR", {day: "2-digit", month: "2-digit", timeZone: d.tz || "UTC"}).format(new Date(iso)); } catch (e) { return dm(iso); } };
    const st = Object.fromEntries((d.events || []).map(e => [e.key.split(":")[0], e.status]));
    const tag = k => st[k] === "ENVIADO" ? chip("entregue", "go") : st[k] === "CANCELADO" ? chip("cancelado", "") : st[k] === "VENCIDO" ? chip("passou", "") : st[k] ? chip("agendado", "cool") : null;
    const r = d.ride || {}, hotel = d.hotel || {};
    const rowsT = [["Carro no desembarque", t.car_utc, "stay_car", `${r.vehicle || ""}${r.plate ? " · " + r.plate : ""}`],
      ["Check-in", t.checkin_utc, "stay_checkin", hotel.hotel || "hotel não escolhido"],
      ["Despertar", t.wake_utc, "stay_wake", `${t.wakeup_min} min antes do carro`],
      ["Carro na portaria", t.pickup_utc, "stay_pickup", `${t.drive.min} min até o aeroporto + ${t.buffer_min} de folga`],
      ["Apresentação", t.report_utc, null, ""]];
    put(box, h("div", {class: "panel-head"}, h("h2", {}, `Pernoite em ${f.ades}`), h("a", {href: "#/ajustes", class: "muted", style: "font-size:13px"}, "pernoites")),
      h("div", {class: "stay-hotel"}, h("b", {}, hotel.hotel || "Hotel não escolhido"), h("span", {class: "muted"}, hotel.hotel_address || (hotel.hotel ? "" : "escolha em Ajustes › Pernoites")),
        h("span", {class: "fine"}, t.drive.label)),
      h("div", {class: "stay-tl"}, rowsT.filter(x => x[1]).map(([l, iso, k, sub]) => h("div", {class: "stay-row"},
        h("span", {class: "stay-t"}, loc(iso), h("small", {}, day(iso))), h("div", {}, h("b", {}, l), sub ? h("div", {class: "muted", style: "font-size:12.5px"}, sub) : null), tag(k)))),
      t.short ? note("stop", "alerta", "O despertar cai antes do check-in: o repouso não cabe. Fale com o Planejamento de Escala.") : null,
      h("p", {class: "fine", style: "margin:10px 0 0"}, `Horários ${t.basis}${d.tz ? ", hora local de " + f.ades : ", em UTC"}. Motorista e placa fictícios. ${d.attribution}`));
    box.hidden = false;
  }).catch(() => {});
  return box;
}
function hotelPicker(icao, done) {
  const body = h("div", {class: "hp"}, h("p", {class: "muted"}, `Procurando hotéis perto de ${icao} no OpenStreetMap…`));
  dialog(`Hotéis perto de ${icao}`, "Escolha um. O LineOps calcula a rota de carro pelo OSRM e usa nas mensagens do pernoite.", body, [closeBtn]);
  get(`/api/stay/${icao}/hotels`).then(d => put(body,
    d.hotels.length ? h("div", {class: "hp-list"}, d.hotels.map(x => h("div", {class: "hp-item"},
      h("div", {}, h("b", {}, x.name), x.stars ? h("span", {class: "muted"}, ` · ${"★".repeat(Math.min(5, +x.stars || 0)) || x.stars}`) : null,
        h("div", {class: "muted", style: "font-size:12.5px"}, [x.address, `${String(x.km).replace(".", ",")} km em linha reta`, x.kind === "motel" ? "motel" : null].filter(Boolean).join(" · "))),
      h("button", {class: "btn sm", onclick: async ev => {
        const btn = ev.currentTarget, dl = btn.closest("dialog");
        btn.disabled = true;
        const r = await act(() => post(`/api/stay/${icao}/hotel`, x), "Hotel escolhido");
        if (!r) { btn.disabled = false; return; }
        if (r.warning) toast(r.warning, true); else toast(`${r.route_min} min de carro até o hotel`);
        if (dl) dl.close();
        done && done();
      }}, "Escolher"))))
      : h("p", {class: "muted"}, `Nenhum hotel com nome no OpenStreetMap num raio de ${d.radius_km} km.`),
    h("p", {class: "fine", style: "margin-top:10px"}, d.note + " " + d.attribution)))
    .catch(e => put(body, note("hold", "alerta", e.message || "OpenStreetMap indisponível agora.")));
}

function releaseBlock(c) {
  const r = c.release, f = c.flight;
  if (!r) return c.ofp ? note("", "info", "O DOV está conferindo o OFP; o despacho chega em instantes.") : null;
  return h("div", {class: "release" + (r.status === "RETIDO" ? " held" : r.pic_signed_at ? " ok" : "")},
    h("div", {class: "rel-row"}, h("span", {}, "DOV"), h("b", {}, r.dov_name), h("small", {}, `${r.status === "RETIDO" ? "reteve" : "assinou"} ${dt(r.dov_signed_at)}`)),
    h("div", {class: "rel-row"}, h("span", {}, "Comandante"), h("b", {}, r.pic_signed_at ? me.display_name : "—"),
      r.pic_signed_at ? h("small", {}, `assinou ${dt(r.pic_signed_at)}`) : r.status === "LIBERADO" && ["RESERVADO", "EM_VOO"].includes(f.state)
        ? h("button", {class: "btn sm", onclick: async () => { if (await act(() => post(`/api/flights/${f.id}/dispatch/sign`), "Despacho assinado")) render(); }}, icon("ok", 15), "Assinar despacho") : null),
    r.reasons.length ? note("stop", "bloqueio", h("b", {}, "Retido: "), r.reasons.join("; ")) : null);
}

function teamPanel() {
  const body = h("div", {}, h("div", {class: "skel s"}));
  get("/api/team").then(t => {
    const inputs = t.staff.map(r => ({r, el: h("input", {value: r.name})}));
    const label = {todos: "", manha: " · manhã", tarde: " · tarde", noite: " · noite"};
    put(body, h("div", {class: "row"}, inputs.map(({r, el}) => field(`${r.sector_label.split(" · ")[0]}${label[r.shift] || ""}`, el))),
      h("button", {class: "btn", onclick: async () => { if (await act(() => api("PUT", "/api/team", {staff: inputs.map(({r, el}) => ({sector: r.sector, shift: r.shift, name: el.value}))}), "Equipe salva")) render(); }}, "Salvar"),
      h("p", {class: "fine"}, t.disclaimer, " Os turnos seguem a hora local da sua base (manhã 06–14, tarde 14–22, noite 22–06)."));
  }).catch(e => put(body, note("hold", "alerta", e.message)));
  return h("div", {class: "panel"}, h("h2", {}, "Equipe da operação"),
    h("p", {class: "muted"}, "Quem fala com você em Mensagens. Mude os nomes como quiser: cada setor faz o papel que tem numa companhia de verdade."), body);
}

/* ───────────────────────── Boas-vindas (primeira execução) ───────────────────────── */
const wiz = {step: 0};
const WIZ_STEPS = ["Bem-vindo", "Escala", "Frota", "Integrações", "Pronto"];
view("boas-vindas", async main => {
  const [ob, cfg] = await Promise.all([get("/api/onboarding"), get("/api/settings")]);
  const s = ob.steps;
  const go = n => { wiz.step = Math.max(0, Math.min(WIZ_STEPS.length - 1, n)); render(); };
  const finish = async () => { if (await act(() => post("/api/onboarding/done"), "Tudo pronto. Bons voos, comandante!")) location.hash = "#/painel"; };
  const bar = h("ol", {class: "wiz-bar"}, WIZ_STEPS.map((t, i) => h("li", {class: i === wiz.step ? "on" : i < wiz.step ? "ok" : ""},
    h("button", {onclick: () => go(i)}, h("span", {}, i < wiz.step ? "✓" : i + 1), t))));
  const nav = (next, label = "Continuar") => h("div", {class: "tools wiz-nav"},
    wiz.step ? h("button", {class: "btn ghost", onclick: () => go(wiz.step - 1)}, "Voltar") : null,
    h("span", {style: "flex:1"}),
    wiz.step < WIZ_STEPS.length - 1 ? h("button", {class: "btn ghost", onclick: () => go(wiz.step + 1)}, "Pular este passo") : null,
    h("button", {class: "btn", onclick: next}, label, icon("seta", 16)));
  let body;
  if (wiz.step === 0) {
    body = [h("h1", {class: "welcome", style: "font-size:clamp(30px,4vw,46px)"}, "Bem-vindo à operação, ", h("span", {class: "co"}, "comandante"), "."),
      h("p", {class: "lede"}, "Em quatro passos o LineOps fica pronto: a sua escala do AeroScale, a frota que você voa e as integrações. Dá para pular qualquer passo e voltar depois em Ajustes."),
      h("div", {class: "wiz-flow"}, [["escala", "AeroScale manda a escala", "o Planejamento publica e o Controle de Tripulantes cuida dela"],
        ["despacho", "O DOV prepara o despacho", "SimBrief, meteorologia, MEL; vocês dois assinam"],
        ["live", "Você voa", "pela IVAO, pelo ACARS do LineOps ou declarando depois"],
        ["diario", "Diário, horas e salário", "com o toque registrado e a Manutenção de olho"]].map(([ic, t, d]) =>
        h("div", {class: "wiz-card"}, h("span", {class: "ic"}, icon(ic, 20)), h("b", {}, t), h("small", {}, d)))),
      h("p", {class: "fine"}, DISCLAIMER), nav(() => go(1), "Começar")];
  } else if (wiz.step === 1) {
    const file = h("input", {type: "file", accept: ".zip,.db"}), out = h("div");
    const imp = async dry => {
      if (!file.files[0]) return toast("Escolha o backup do AeroScale.", true);
      put(out, h("div", {class: "skel s"}));
      const r = await act(() => api("POST", "/api/import/aeroscale?dry_run=" + dry, undefined, file.files[0]));
      if (!r) return put(out);
      put(out, note(dry ? "" : "go", dry ? "info" : "ok", h("b", {}, dry ? "Prévia (nada gravado): " : "Importado: "),
        `${r.legs_total} pernas, ${r.legs_new} novas. Origens: ${Object.entries(r.origins).map(([k, v]) => `${k} ${v}`).join(", ")}.`), r.warnings.map(w => note("hold", "alerta", w)));
      if (!dry) { roster.bounds = null; s.escala = true; }
    };
    body = [h("h2", {}, "A sua escala"), h("p", {class: "muted"}, "A LineOps gera a escala sozinha, com o motor do AeroScale e o RPL do CGNA. Escolha o mês, a base e o tipo, e pronto."),
      s.escala ? note("go", "ok", "Você já tem uma escala. Pode gerar ou importar outra quando quiser.") : null,
      h("div", {class: "tools", style: "margin-bottom:14px"}, h("a", {class: "btn", href: "#/gerar"}, icon("mais", 16), "Gerar a minha escala")),
      h("p", {class: "fine"}, "Ou, se você gera no app do AeroScale, importe o backup dele (.zip). Reimportar depois é seguro."),
      field("Backup do AeroScale (.zip ou .db)", file), h("div", {class: "tools"}, h("button", {class: "btn ghost", onclick: () => imp(true)}, "Ver prévia"),
        h("button", {class: "btn", onclick: () => imp(false)}, icon("importar", 16), "Importar")), out, nav(() => go(2))];
  } else if (wiz.step === 2) {
    const f = h("input", {type: "file", accept: ".csv,text/csv"}), out = h("div");
    body = [h("h2", {}, "A frota que você voa"), h("p", {class: "muted"}, "Cadastre só matrículas reais que você usa. O jeito rápido é um CSV com registration, airline, type_icao (e model_name, se quiser). Limites de peso ficam em branco até você informar."),
      s.frota ? note("go", "ok", `${s.frota} aeronave(s) na frota.`) : null,
      field("CSV da frota", f), h("div", {class: "tools"}, h("button", {class: "btn", onclick: async () => {
        if (!f.files[0]) return toast("Escolha um arquivo.", true);
        const r = await act(async () => api("POST", "/api/aircraft/import-csv", undefined, await f.files[0].text()));
        if (r) { s.frota += r.added.length; put(out, note(r.errors.length ? "hold" : "go", "ok", `Cadastradas ${r.added.length}, já existiam ${r.skipped_existing.length}, com erro ${r.errors.length}.`)); }
      }}, icon("importar", 16), "Importar CSV"), h("a", {class: "btn ghost", href: "#/frota"}, "Cadastrar uma por uma")), out, nav(() => go(3))];
  } else if (wiz.step === 3) {
    const vid = h("input", {value: cfg.ivao_vid || "", inputmode: "numeric", placeholder: "ex.: 123456"});
    const sb = h("input", {value: cfg.simbrief_userid || "", inputmode: "numeric", placeholder: "Pilot ID do SimBrief"});
    const pay = h("input", {type: "checkbox", checked: s.salarios});
    const style = h("select", {}, [["folclorico", "Folclórico: a bronca de hangar"], ["just_culture", "Just culture: o tom que a Boeing recomenda"]].map(([v, l]) =>
      h("option", {value: v, selected: (cfg.landing_style || "folclorico") === v}, l)));
    body = [h("h2", {}, "Integrações"),
      h("div", {class: "row"}, field("VID da IVAO (acompanhamento do voo e Rede)", vid), field("Pilot ID do SimBrief (o DOV busca o seu plano)", sb)),
      h("div", {class: "row"}, field("Rigor da Chefia de Manutenção", style)),
      h("label", {class: "ck", style: "margin-bottom:16px"}, pay, "Ligar os salários (contracheque simulado pela CCT dos aeronautas)"),
      nav(async () => {
        if (vid.value && !/^\d+$/.test(vid.value.trim())) return toast("O VID tem só números.", true);
        const ok = await act(() => api("PUT", "/api/settings", {ivao_vid: vid.value.trim(), simbrief_userid: sb.value.trim(), landing_style: style.value}));
        if (!ok) return;
        if (pay.checked !== s.salarios) { const r = await act(() => api("PUT", "/api/payroll/enabled", {enabled: pay.checked})); if (r) me.features = {...(me.features || {}), payroll: r.enabled}; }
        go(4);
      })];
  } else {
    const item = (okv, t, d) => h("div", {class: "wiz-check" + (okv ? " ok" : "")}, icon(okv ? "ok" : "pendente", 18), h("div", {}, h("b", {}, t), h("small", {}, d)));
    body = [h("h2", {}, "Tudo certo"), h("p", {class: "muted"}, "O que ficou faltando dá para fazer depois em Ajustes."),
      item(s.escala, "Escala do AeroScale", s.escala ? "importada" : "ainda não importada"),
      item(s.frota > 0, "Frota", s.frota ? `${s.frota} aeronave(s)` : "vazia: sem aeronave não dá para reservar com matrícula"),
      item(s.ivao, "IVAO", s.ivao ? "VID salvo" : "sem VID: a Rede funciona, o acompanhamento não"),
      item(s.simbrief, "SimBrief", s.simbrief ? "o DOV busca o seu plano sozinho" : "você importa o OFP à mão"),
      h("p", {class: "fine"}, "Dica: no ícone do LineOps perto do relógio, “Acesso pela rede” mostra o endereço para o PC do simulador e o celular."),
      nav(finish, "Ir para o painel")];
  }
  put(main, h("div", {class: "wiz"}, h("div", {class: "tools", style: "justify-content:space-between;margin-bottom:18px"}, bar,
    h("button", {class: "btn ghost sm", onclick: finish}, "Pular tudo, configuro depois")), h("div", {class: "panel wiz-body"}, body)));
});

function phonePanel() {
  const box = h("div", {}, h("p", {class: "muted"}, "Carregando…"));
  const save = async (body, ok) => { const r = await act(() => api("PUT", "/api/notify", body), ok || "Salvo"); if (r) paint(r); return r; };
  const paint = c => {
    const t = c.telegram, n = c.ntfy;
    const tok = h("input", {placeholder: "123456789:AAH...", autocomplete: "off"});
    const url = h("input", {value: c.public_url || (/^(localhost|127\.)/.test(location.hostname) ? "" : location.origin), placeholder: "http://192.168.0.10:8787"});
    const lvl = h("select", {}, [["importantes", "Só o importante (recomendado)"], ["tudo", "Todas as mensagens"], ["desligado", "Desligado"]].map(([v, l]) => h("option", {value: v, selected: c.level === v}, l)));
    const qt = h("input", {value: c.quiet || "", placeholder: "23:00-06:00 (vazio = sem silêncio)"});
    put(box,
      h("p", {class: "muted", style: "margin-top:0"}, "A operação no seu bolso: escala, despacho, ACARS em voo, pernoite, despertar e a sua tripulação. ",
        "Notificação de navegador não funciona aqui (exige HTTPS e o LineOps roda na sua rede em http://), por isso o LineOps usa o Telegram ou o ntfy."),
      h("div", {class: "ph-grid"},
        h("div", {class: "ph-card"}, h("h3", {}, "Telegram ", t.paired ? chip("conectado", "go") : t.configured ? chip("falta parear", "hold") : chip("recomendado", "cool")),
          h("p", {class: "fine"}, "De mão dupla: dá ciente, assina o despacho e informa o código de atraso pelos botões. Comandos /hoje, /proximo, /pernoite e /mensagens."),
          !t.configured ? h("div", {},
            h("ol", {class: "steps"}, h("li", {}, "No Telegram, abra o ", h("b", {}, "@BotFather"), " e mande ", h("span", {class: "mono"}, "/newbot"), "."),
              h("li", {}, "Escolha um nome (ex.: LineOps do Triple) e um usuário terminado em bot."), h("li", {}, "Cole aqui o token que ele mandar.")),
            field("Token do bot", tok), h("button", {class: "btn sm", onclick: () => save({tg_token: tok.value.trim()}, "Bot reconhecido")}, "Conectar o bot"))
          : !t.paired ? h("div", {}, h("p", {}, "Bot ", h("b", {}, "@" + t.bot), " reconhecido. Agora abra o bot no Telegram e mande:"),
              h("div", {class: "pair"}, `/start ${t.pair_code}`),
              h("div", {class: "tools"}, h("a", {class: "btn sm", href: `https://t.me/${t.bot}?start=${t.pair_code}`, target: "_blank", rel: "noopener noreferrer"}, "Abrir o bot"),
                h("button", {class: "btn ghost sm", onclick: async () => paint(await get("/api/notify"))}, "Já mandei"),
                h("button", {class: "btn ghost sm", onclick: () => save({tg_off: true}, "Telegram desligado")}, "Trocar o bot")))
          : h("div", {}, h("p", {}, "Conectado ao ", h("b", {}, "@" + t.bot), "."), h("div", {class: "tools"},
              h("button", {class: "btn ghost sm", onclick: () => save({tg_unpair: true}, "Desconectado")}, "Desparear"),
              h("button", {class: "btn ghost sm", onclick: () => save({tg_off: true}, "Telegram desligado")}, "Desligar")))),
        h("div", {class: "ph-card"}, h("h3", {}, "ntfy ", n.topic ? chip("ligado", "go") : chip("sem cadastro", "")),
          h("p", {class: "fine"}, "Só de ida e sem conta: instale o app ntfy (Android ou iPhone) e assine o tópico. O nome do tópico funciona como senha."),
          n.topic ? h("div", {}, h("div", {class: "pair"}, n.topic), h("p", {class: "fine"}, `No app: + › assinar tópico › ${n.topic} (servidor ${n.server}).`),
              h("div", {class: "tools"}, h("button", {class: "btn ghost sm", onclick: () => save({ntfy_new_topic: true}, "Tópico novo gerado")}, "Gerar outro tópico"),
                h("button", {class: "btn ghost sm", onclick: () => save({ntfy_off: true}, "ntfy desligado")}, "Desligar")))
            : h("button", {class: "btn sm", onclick: () => save({ntfy_new_topic: true}, "Tópico criado")}, "Criar meu tópico"))),
      h("div", {class: "row", style: "margin-top:14px"}, field("O que vai para o celular", lvl), field("Horário de silêncio (hora da base; o despertar sempre toca)", qt)),
      field("Endereço do LineOps para os links (o do PC na sua rede)", url),
      h("div", {class: "tools"}, h("button", {class: "btn sm", onclick: () => save({level: lvl.value, quiet: qt.value.trim(), public_url: url.value.trim()})}, "Salvar"),
        c.channels.length ? h("button", {class: "btn ghost sm", onclick: async () => {
          const r = await act(() => post("/api/notify/test")); if (r) toast(Object.entries(r).map(([k, v]) => `${k}: ${v === "ok" ? "enviado" : v}`).join(" · ")); }}, "Mandar um teste") : null),
      (c.recent || []).length ? h("div", {class: "tbl", style: "margin-top:12px"}, h("table", {}, h("tbody", {}, c.recent.map(r => h("tr", {},
        h("td", {}, r.channel), h("td", {}, r.subject), h("td", {}, r.status === "ENVIADO" ? chip("enviado", "go") : r.status === "ERRO" ? chip("erro", "stop") : r.status === "IGNORADO" ? chip("não vai", "") : chip(r.status.toLowerCase(), "hold")),
        h("td", {class: "muted", title: r.error || ""}, r.sent_at ? hhmmz(r.sent_at) : (r.error || "").slice(0, 40))))))) : null,
      h("p", {class: "fine"}, "Fora de casa, os links só abrem se o celular alcançar o PC (mesma rede, ou uma VPN como o Tailscale). As notificações chegam de qualquer lugar."));
  };
  get("/api/notify").then(paint).catch(e => put(box, note("hold", "alerta", e.message)));
  return h("div", {class: "panel"}, h("h2", {}, "Celular"), box);
}
function aboutPanel() {
  const body = h("div", {}, h("div", {class: "skel s"}));
  get("/api/about").then(a => put(body,
    h("dl", {class: "kv"}, h("dt", {}, "Versão"), h("dd", {}, a.version + (a.desktop ? " · app do Windows" : "")),
      h("dt", {}, "Dados"), h("dd", {style: "white-space:normal"}, a.data_dir), h("dt", {}, "Banco"), h("dd", {}, (a.db_size / 1048576).toFixed(1) + " MB"),
      h("dt", {}, "Log"), h("dd", {style: "white-space:normal"}, a.log_path), h("dt", {}, "Regras do AeroScale"), h("dd", {}, a.aeroscale_modules),
      h("dt", {}, "Sistema"), h("dd", {style: "white-space:normal"}, `${a.platform} · Python ${a.python}`)),
    h("h3", {style: "margin:16px 0 8px"}, "Dados com prazo de validade"),
    a.validity.map(v => note(v.status === "ok" ? "go" : v.status === "vencendo" ? "hold" : "stop", v.status === "ok" ? "ok" : "alerta",
      h("b", {}, v.label), v.until ? ` · até ${dmy(v.until)}` : "", ` · ${v.status}`, !v.relevant ? " (não usado agora)" : "",
      v.status !== "ok" ? h("div", {class: "fine", style: "margin-top:4px"}, v.action) : null)),
    h("div", {class: "tools", style: "margin-top:10px"},
      h("button", {class: "btn ghost sm", onclick: async () => {
        const r = await act(() => get("/api/logs?lines=300"));
        if (r) dialog("Log do LineOps", r.path, h("pre", {class: "mono logbox"}, r.lines.join("\n") || "(vazio)"), [closeBtn]);
      }}, "Ver o log"),
      h("a", {class: "btn ghost sm", href: "/api/logs/download"}, icon("baixar", 15), "Baixar o log"),
      h("a", {class: "btn ghost sm", href: "#/boas-vindas", onclick: () => { wiz.step = 0; }}, "Rever as boas-vindas")),
    h("p", {class: "fine"}, a.disclaimer))).catch(e => put(body, note("hold", "alerta", e.message)));
  return h("div", {class: "panel"}, h("h2", {}, "Sobre o LineOps"), body);
}

function validityNotes() {
  const box = h("div");
  get("/api/validity").then(L => put(box, L.map(v => note(v.status === "vencido" ? "stop" : "hold", "alerta", h("b", {}, v.label),
    v.until ? ` (${v.status === "vencido" ? "venceu" : "vence"} em ${dmy(v.until)})` : "", ". ", v.action)))).catch(() => {});
  return box;
}

/* ───────────────────────── salários (opcional) ───────────────────────── */
const brl = v => v == null ? "—" : Number(v).toLocaleString("pt-BR", {style: "currency", currency: "BRL"});
const money = (v, c) => { try { return Number(v).toLocaleString("pt-BR", {style: "currency", currency: c}); } catch (e) { return `${c} ${Number(v).toFixed(2)}`; } };
const MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"];
const monthLabel = m => `${MESES[+m.slice(5, 7) - 1]} de ${m.slice(0, 4)}`;
const addMonths = (m, n) => new Date(Date.UTC(+m.slice(0, 4), +m.slice(5, 7) - 1 + n, 1)).toISOString().slice(0, 7);
const pay = {month: today().slice(0, 7), basis: "programado"};
const payOn = () => !!(me && me.features && me.features.payroll);

view("salarios", async (main, arg) => {
  if (arg && /^\d{4}-\d{2}$/.test(arg)) pay.month = arg;
  const cfg = await get("/api/payroll/config");
  const title = h("div", {}, h("h1", {}, "Salários"),
    h("div", {class: "sub"}, "Contracheque simulado pela CCT dos aeronautas, com as horas da sua escala do AeroScale e do seu diário."));
  if (!cfg.enabled) {
    put(main, h("div", {class: "head"}, title),
      h("div", {class: "panel"}, h("h2", {}, "Módulo desligado"),
        h("p", {class: "muted"}, "Os salários são opcionais. Ligados, aparecem no menu e no painel; desligados, nada é calculado."),
        h("button", {class: "btn", onclick: () => togglePayroll(true)}, icon("salarios", 16), "Ligar salários")));
    return;
  }
  const stepper = h("div", {class: "stepper"},
    h("button", {onclick: () => { pay.month = addMonths(pay.month, -1); render(); }, "aria-label": "Mês anterior"}, icon("seta", 16, "flip")),
    h("span", {class: "lbl"}, cap(monthLabel(pay.month))),
    h("button", {onclick: () => { pay.month = addMonths(pay.month, 1); render(); }, "aria-label": "Próximo mês"}, icon("seta", 16)));
  stepper.querySelector(".flip").style.transform = "rotate(180deg)";
  const seg = h("div", {class: "seg"}, [["programado", "Programado"], ["realizado", "Realizado"]].map(([k, l]) =>
    h("button", {"aria-pressed": pay.basis === k, onclick: () => { pay.basis = k; render(); },
      title: k === "programado" ? "Horas da escala importada do AeroScale" : "Horas dos voos aprovados no diário"}, l)));
  const tools = h("div", {class: "tools"}, h("button", {class: "btn ghost sm", onclick: () => { pay.month = today().slice(0, 7); render(); }}, "Este mês"), stepper, seg);
  const head = h("div", {class: "head"}, title, tools);
  put(main, head, skeleton("h"));
  const d = await get(`/api/payroll/${pay.month}?basis=${pay.basis}`);
  if (d.needs_contract) {
    const form = contractForm(cfg, null, d.suggestion);
    put(main, head, h("div", {class: "panel"}, h("h2", {}, "Seu contrato"),
      h("p", {class: "muted"}, "Antes do primeiro cálculo, confira a função e a aeronave. Os valores vêm da CCT 2025/2026 com a cláusula de cada um; mude o que o seu acordo pagar diferente."),
      form.el, h("div", {class: "tools", style: "margin-top:8px"}, h("button", {class: "btn", onclick: async () => {
        if (await act(() => post("/api/payroll/contracts", form.collect()), "Contrato salvo")) render();
      }}, icon("ok", 16), "Salvar contrato"))));
    return;
  }
  const yr = await get(`/api/payroll/year/${pay.month.slice(0, 4)}`).catch(() => null);
  const t = d.totals, hr = d.hours;
  const acts = [];
  if (d.closing) {
    acts.push(chip(`Fechado em ${dmy(d.closing.closed_at)}`, "go"),
      h("a", {class: "btn ghost sm", href: `/api/payroll/${d.month}/pdf?closing=${d.closing.id}`, target: "_blank", rel: "noopener"}, icon("baixar", 15), "PDF do fechamento"),
      h("button", {class: "btn stop sm", onclick: () => reasonDialog("Anular fechamento", "O contracheque fechado fica guardado como anulado; você pode fechar o mês de novo.", "Motivo",
        r => post(`/api/payroll/closings/${d.closing.id}/void`, {reason: r}), "Fechamento anulado")}, "Anular"));
  }
  acts.push(h("a", {class: "btn ghost sm", href: `/api/payroll/${d.month}/pdf?basis=${d.basis}`, target: "_blank", rel: "noopener"}, icon("baixar", 15), "PDF"));
  if (d.basis === "realizado" && d.month_ended && !d.closing)
    acts.push(h("button", {class: "btn sm", onclick: () => closeMonthDialog(d)}, icon("ok", 15), "Fechar o mês"));

  const figs = h("div", {class: "figs money"}, [["Bruto", t.gross, brl], ["Descontos", t.deductions, brl], ["Líquido", t.net, brl],
    ["Horas de voo", hr.flight_min, hm], ["Acima da garantia", hr.excess_min, hm], ["Diárias", d.meals.domestic.value, brl]].map(([l, v, f]) => {
      const b = h("b", {}, f(v));
      countUp(b, v, f);
      return h("div", {class: "fig"}, b, h("span", {}, l));
    }));
  put(main, head,
    h("div", {class: "tools", style: "margin:-6px 0 16px"}, chip(d.basis === "programado" ? "Pela escala" : "Pelo diário", "or"),
      chip(`${d.contract.role} · ${d.contract.category_label}`, ""), d.base.icao ? chip(`base ${d.base.icao}`, "cool") : null,
      h("span", {class: "spacer", style: "flex:1"}), acts),
    d.warnings.map(w => note(w.level === "warn" ? "hold" : "", w.level === "warn" ? "alerta" : "info", w.message)),
    figs,
    h("div", {class: "cols-2", style: "margin-top:18px"},
      h("div", {}, payslip(d), mealsPanel(d)),
      h("div", {}, guaranteePanel(d), contractPanel(cfg, d))),
    yr ? yearPanel(yr) : null,
    h("p", {class: "fine"}, d.source.basis_note, " Valores padrão: ", h("a", {href: d.source.cct.url, target: "_blank", rel: "noopener noreferrer"}, d.source.cct.label),
      `, vigente de ${dmy(d.source.cct.valid_from)} a ${dmy(d.source.cct.valid_to)}. Estimativa de simulação: cada empresa tem acordo próprio e o RH pode calcular diferente.`));
});

const shortSrc = s => s.replace(/^Portaria Interministerial MPS\/MF nº 13.*/, "Portaria MPS/MF nº 13/2026")
  .replace(/^Tabela mensal do IRRF de 2026.*/, "IRRF 2026 · Lei 15.270/2025").split(" (")[0];
function payslip(d) {
  const L = d.lines.filter(l => l.kind !== "info");
  return h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Contracheque"), h("span", {class: "muted", style: "font-size:13px"}, cap(monthLabel(d.month)))),
    h("div", {class: "tbl"}, h("table", {class: "payslip"},
      h("thead", {}, h("tr", {}, ["Cód.", "Descrição", "Ref.", "Proventos", "Descontos"].map((x, k) => h("th", {class: k > 2 ? "n" : ""}, x)))),
      h("tbody", {}, L.map(l => h("tr", {title: l.detail || ""},
        h("td", {class: "t muted"}, l.code),
        h("td", {class: "wrap"}, l.label, !l.taxable && l.kind === "provento" ? h("span", {class: "muted"}, " · isento") : null, l.source ? h("span", {class: "lsrc", title: l.source}, shortSrc(l.source)) : null),
        h("td", {class: "t muted ref"}, l.ref || ""),
        h("td", {class: "n"}, l.kind === "provento" ? brl(l.amount) : ""), h("td", {class: "n"}, l.kind === "desconto" ? brl(l.amount) : ""))),
        h("tr", {class: "tot"}, h("td", {}), h("td", {}, "Totais"), h("td", {}), h("td", {class: "n"}, brl(d.totals.gross)), h("td", {class: "n"}, brl(d.totals.deductions))),
        h("tr", {class: "net"}, h("td", {}), h("td", {}, "Líquido"), h("td", {}), h("td", {class: "n"}, brl(d.totals.net)), h("td", {}))))),
    (d.totals.foreign_meals || []).length ? note("", "info", "Diárias internacionais, pagas na moeda da CCT e fora do total: ",
      d.totals.foreign_meals.map(x => money(x.value, x.currency)).join(" + "), ".") : null,
    d.totals.food_voucher ? h("p", {class: "fine", style: "margin:10px 0 0"}, `Fora do contracheque: vale-alimentação de ${brl(d.totals.food_voucher)} (CCT cl. 2.4).`) : null,
    h("details", {style: "margin-top:12px"}, h("summary", {class: "muted", style: "cursor:pointer;font-size:13.5px"}, "Como cada linha foi calculada"),
      h("div", {style: "margin-top:10px"}, L.filter(l => l.detail).map(l => h("p", {style: "font-size:13.5px"}, h("b", {}, `${l.code} ${l.label}: `), l.detail)))));
}

function guaranteePanel(d) {
  const x = d.hours, G = x.guarantee_min, F = x.flight_min, R = x.reserve_min;
  const S = Math.max(0, x.equiv_min - F - R), E = x.equiv_min;
  const max = Math.max(E, G, 1) * 1.08, pc = v => `${(100 * v / max).toFixed(2)}%`;
  const bar = h("div", {class: "gbar", role: "img", "aria-label": `${hm(E)} equivalentes para uma garantia de ${hm(G)}`},
    F ? h("i", {class: "v", style: `width:${pc(F)}`}) : null, R ? h("i", {class: "r", style: `width:${pc(R)}`}) : null,
    S ? h("i", {class: "s", style: `width:${pc(S)}`}) : null,
    h("div", {class: "gmark" + (G / max > 0.8 ? " left" : ""), style: `left:${pc(G)}`}, h("span", {}, `${hm(G)}`)));
  const falta = G - E;
  return h("div", {class: "panel"}, h("h2", {}, `Garantia de ${Math.round(G / 60)} h`),
    h("p", {class: "muted", style: "font-size:13.5px"}, "A parte fixa paga as primeiras horas. Reserva conta como hora de voo e sobreaviso vale 1/3 (CCT cl. 3.2.7)."),
    bar,
    h("div", {class: "glegend"},
      h("span", {}, h("b", {style: "background:var(--orange)"}), `Voo ${hm(F)}`),
      h("span", {}, h("b", {style: "background:var(--cool)"}), `Reserva ${hm(R)}${x.reserve_days.length ? ` (${x.reserve_days.length} d)` : ""}`),
      h("span", {}, h("b", {style: "background:var(--hold)"}), `Sobreaviso ${hm(x.standby_min)} → ${hm(S)}`)),
    h("p", {style: "margin:12px 0 0;font-size:14px"}, falta > 0 ? `Faltam ${hm(falta)} para passar da garantia: nada de variável ainda.`
      : h("span", {}, h("b", {}, hm(x.excess_min)), ` acima da garantia, a ${brl(x.hour_rate)} a hora.`)),
    h("div", {class: "hr"}),
    h("dl", {class: "kv"},
      h("dt", {}, "Pernas voadas"), h("dd", {}, `${x.legs} em ${x.days_flown} dia(s)`),
      h("dt", {}, "Domingos e feriados"), h("dd", {}, x.sunday_holiday_min ? `${hm(x.sunday_holiday_min)} · ${x.sunday_holiday_days.map(dm).join(", ")}` : "—"),
      h("dt", {}, "Noturno (22h–5h)"), h("dd", {}, hm(x.night_min)),
      h("dt", {}, "Como extra"), h("dd", {}, x.deadhead_min ? hm(x.deadhead_min) : "—"),
      h("dt", {}, "Reserva acionada"), h("dd", {}, x.reserve_activated.length ? x.reserve_activated.map(dm).join(", ") : "—"),
      h("dt", {}, "Feriados do mês"), h("dd", {style: "white-space:normal"}, Object.keys(d.holidays).length ? Object.entries(d.holidays).map(([k, v]) => `${dm(k)} ${v}`).join(" · ") : "—")));
}

const collapsible = (on, label, el) => on ? h("details", {}, h("summary", {class: "muted", style: "cursor:pointer;font-size:13.5px;margin-bottom:8px"}, label), el) : el;
function mealsPanel(d) {
  const m = d.meals, dom = m.domestic;
  const duties = m.duties.filter(x => x.meals.length);
  return h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Diárias de alimentação"),
      h("span", {class: "muted", style: "font-size:13px"}, `${dom.main} refeição(ões) · ${dom.breakfast} café(s)${dom.hotel_skipped ? ` · ${dom.hotel_skipped} café(s) no hotel` : ""}`)),
    h("p", {class: "muted", style: "font-size:13.5px"}, `Devidas nas janelas da CCT cl. 2.3 (café 05–08, almoço 11–13, jantar 19–20, ceia 00–01, no fuso da base) em que você estava em jornada: `,
      `da apresentação ao encerramento, calculados pelo crew.analyze do AeroScale. Refeição ${brl(m.main_value)}, café ${brl(m.breakfast_value)}.`),
    duties.length ? collapsible(duties.length > 6, `Ver as ${duties.length} jornadas`, h("div", {class: "tbl"}, h("table", {},
      h("thead", {}, h("tr", {}, ["Dia", "Jornada", "Trecho", "Refeições"].map(x => h("th", {}, x)))),
      h("tbody", {}, duties.map(x => h("tr", {},
        h("td", {}, dm(x.date)), h("td", {class: "t"}, `${hhmmz(x.report_utc)}–${hhmmz(x.release_utc)}`), h("td", {class: "t"}, `${x.start}→${x.end}`),
        h("td", {class: "wrap"}, x.meals.map(ml => chip(`${ml.label}${ml.value ? " " + money(ml.value, ml.currency) : ""}`,
          !ml.value ? "" : ml.currency === "BRL" ? "go" : "cool", `${ml.local} em ${ml.where}${ml.note ? " · " + ml.note : ""}`))))))))) 
      : h("p", {class: "muted"}, "Nenhuma jornada com janela de refeição neste mês."));
}

function contractPanel(cfg, d) {
  const c = d.contract.data, src = c.sources || {};
  const row = (k, l, v) => [h("dt", {}, l), h("dd", {title: src[k] || ""}, v, src[k] === "informado por você" ? h("span", {class: "muted"}, " · seu") : null)];
  return h("div", {class: "panel"}, h("div", {class: "panel-head"}, h("h2", {}, "Contrato"),
      h("button", {class: "btn ghost sm", onclick: () => editContract(cfg, c)}, "Editar")),
    h("dl", {class: "kv"},
      h("dt", {}, "Vale desde"), h("dd", {}, cap(monthLabel(d.contract.valid_from))),
      h("dt", {}, "Função"), h("dd", {}, `${c.role} · ${cfg.categories[c.category]}`),
      row("fixed_total", "Parte fixa", brl(c.fixed_total)),
      row("hour_rate", "Hora acima da garantia", brl(c.hour_rate)),
      row("guarantee_h", "Garantia", `${c.guarantee_h} h`),
      row("reserve_hour_value", "Hora de reserva", brl(c.reserve_hour_value)),
      row("standby_hour_value", "Hora de sobreaviso", brl(c.standby_hour_value)),
      row("reserve_hours_per_day", "Reserva por dia", `${c.reserve_hours_per_day} h`),
      row("meal_value", "Refeição", brl(c.meal_value)),
      h("dt", {}, "Adicionais"), h("dd", {style: "white-space:normal"}, [c.sunday_holiday_double ? "domingo/feriado em dobro" : null, c.dsr_enabled ? `DSR (${c.dsr_rest_days} folgas)` : null,
        c.night_pct ? `noturno ${c.night_pct}%` : null, c.hazard_pct ? `periculosidade ${c.hazard_pct}%` : null].filter(Boolean).join(" · ") || "—"),
      h("dt", {}, "Impostos"), h("dd", {}, c.taxes_enabled ? `INSS e IRRF${c.dependents ? ` · ${c.dependents} dependente(s)` : ""}` : "não calculados")),
    cfg.contracts.length > 1 ? h("details", {style: "margin-top:12px"}, h("summary", {class: "muted", style: "cursor:pointer;font-size:13px"}, `Versões (${cfg.contracts.length})`),
      h("div", {class: "tbl", style: "margin-top:8px"}, h("table", {}, h("tbody", {}, cfg.contracts.map(v => h("tr", {},
        h("td", {}, v.valid_from), h("td", {}, `${v.data.role} · ${cfg.categories[v.data.category]}`), h("td", {class: "n"}, brl(v.data.fixed_total)),
        h("td", {class: "muted"}, dt(v.created_at)))))))) : null,
    h("p", {class: "fine", style: "margin:12px 0 0"}, "Passe o mouse sobre um valor para ver a fonte. GOL, LATAM e Azul têm acordos próprios acima da CCT: se o seu contracheque é diferente, edite e os valores ficam marcados como seus."));
}

function yearPanel(y) {
  if (!y.months.length) return null;
  return h("div", {class: "panel", style: "margin-top:18px"}, h("div", {class: "panel-head"}, h("h2", {}, `Ano de ${y.year}`),
      h("span", {class: "muted", style: "font-size:13px"}, "realizado; meses fechados usam a foto do fechamento")),
    h("div", {class: "tbl"}, h("table", {},
      h("thead", {}, h("tr", {}, ["Mês", "Situação", "Voo", "Bruto", "INSS", "IRRF", "Líquido", ""].map((x, k) => h("th", {class: k > 1 && k < 7 ? "n" : ""}, x)))),
      h("tbody", {}, y.months.map(m => h("tr", {class: "click", tabindex: 0, onclick: e => { if (e.target.closest("a")) return; pay.month = m.month; pay.basis = "realizado"; render(); }},
        h("td", {}, cap(monthLabel(m.month))), h("td", {}, m.closed ? chip("fechado", "go") : chip("em aberto", "")),
        h("td", {class: "n"}, hm(m.flight_min)), h("td", {class: "n"}, brl(m.gross)), h("td", {class: "n"}, brl(m.inss)), h("td", {class: "n"}, brl(m.irrf)),
        h("td", {class: "n"}, brl(m.net)),
        h("td", {}, m.closed ? h("a", {href: `/api/payroll/${m.month}/pdf?closing=${m.closing_id}`, target: "_blank", rel: "noopener"}, "PDF") : ""))),
        h("tr", {class: "tot"}, h("td", {}, "Total"), h("td", {}), h("td", {class: "n"}, hm(y.totals.flight_min)), h("td", {class: "n"}, brl(y.totals.gross)),
          h("td", {class: "n"}, brl(y.totals.inss)), h("td", {class: "n"}, brl(y.totals.irrf)), h("td", {class: "n"}, brl(y.totals.net)), h("td", {}))))));
}

function closeMonthDialog(d) {
  const n = h("input", {placeholder: "opcional"});
  dialog(`Fechar ${monthLabel(d.month)}`, `Guarda a foto deste contracheque realizado: líquido de ${brl(d.totals.net)}. Mudanças posteriores no diário ou no contrato não alteram o fechamento.`,
    field("Observação", n), [closeBtn, {label: "Fechar o mês", fn: async () => {
      if (!await act(() => post(`/api/payroll/${d.month}/close`, {notes: n.value || null}), "Mês fechado")) return false;
      render();
    }}]);
}

function editContract(cfg, c) {
  const form = contractForm(cfg, c, {});
  dialog("Editar contrato", "Salvar cria uma nova versão a partir do mês escolhido; os meses anteriores continuam com a versão antiga.", form.el,
    [closeBtn, {label: "Salvar", fn: async () => {
      if (!await act(() => post("/api/payroll/contracts", form.collect()), "Contrato salvo")) return false;
      render();
    }}]);
}

function contractForm(cfg, base, sugg) {
  const pre = (r, c) => (cfg.presets.find(p => p.role === r && p.category === c) || {}).values;
  const start = base || pre(sugg.role || "Comandante", sugg.category || "jato_nb");
  const role = h("select", {}, cfg.roles.map(r => h("option", {value: r}, r)));
  const cat = h("select", {}, Object.entries(cfg.categories).map(([k, l]) => h("option", {value: k}, l)));
  role.value = start.role; cat.value = start.category;
  const valid = h("input", {type: "month", value: pay.month});
  const num = (step) => h("input", {type: "number", step: step || "0.01", min: 0, inputmode: "decimal"});
  const f = {fixed_total: num(), organic_pct: num("0.1"), hour_rate: num(), guarantee_h: num("0.5"), reserve_hour_value: num(), standby_hour_value: num(),
    reserve_hours_per_day: num("0.5"), dsr_rest_days: num("1"), night_pct: num("0.1"), hazard_pct: num("0.1"), meal_value: num(), breakfast_pct: num("1"),
    food_voucher: num(), dependents: num("1")};
  const ck = {sunday_holiday_double: h("input", {type: "checkbox"}), dsr_enabled: h("input", {type: "checkbox"}),
    breakfast_in_hotel: h("input", {type: "checkbox"}), taxes_enabled: h("input", {type: "checkbox"})};
  const hol = h("input", {placeholder: "ex.: 01-25, 07-09 (MM-DD)"});
  const items = h("div");
  const addItem = (it = {label: "", value: "", taxable: true}) => {
    const r = h("div", {class: "items-row"}, h("input", {placeholder: "ex.: gratificação de habilitação", value: it.label}),
      h("input", {type: "number", step: "0.01", value: it.value, placeholder: "R$"}),
      h("label", {class: "ck"}, h("input", {type: "checkbox", checked: it.taxable !== false}), "tributável"),
      h("button", {class: "btn ghost sm", type: "button", onclick: () => r.remove()}, "Remover"));
    items.append(r);
  };
  const fill = v => {
    for (const [k, el] of Object.entries(f)) el.value = v[k] == null ? "" : v[k];
    for (const [k, el] of Object.entries(ck)) el.checked = !!v[k];
  };
  fill(start);
  hol.value = (start.extra_holidays || []).join(", ");
  (start.other_items || []).forEach(addItem);
  const reset = () => { const v = pre(role.value, cat.value); if (v) { fill(v); toast(`Valores da CCT para ${role.value} · ${cfg.categories[cat.value]}`); } };
  role.addEventListener("change", reset); cat.addEventListener("change", reset);
  const sg = sugg && (sugg.role || sugg.category) ? note("or", "info", h("b", {}, "Pelo AeroScale: "),
    [sugg.role ? `${sugg.role} (${sugg.role_source})` : null, sugg.category ? `${cfg.categories[sugg.category]} — ${sugg.category_source}` : null].filter(Boolean).join(" · "), ".") : null;
  const sec = t => h("h3", {style: "margin:14px 0 8px"}, t);
  const el = h("div", {}, sg,
    h("div", {class: "row"}, field("Vale a partir de", valid), field("Função", role), field("Aeronave (categoria da CCT)", cat)),
    h("div", {class: "tools", style: "margin:-4px 0 4px"}, h("button", {class: "btn ghost sm", type: "button", onclick: reset}, "Restaurar valores da CCT")),
    sec("Parte fixa e horas"),
    h("div", {class: "row"}, field("Parte fixa mensal (R$)", f.fixed_total), field("Compensação orgânica (%)", f.organic_pct),
      field("Hora acima da garantia (R$)", f.hour_rate), field("Garantia (h)", f.guarantee_h)),
    sec("Reserva e sobreaviso"),
    h("div", {class: "row"}, field("Hora de reserva (R$)", f.reserve_hour_value), field("Hora de sobreaviso (R$)", f.standby_hour_value),
      field("Horas por dia de reserva", f.reserve_hours_per_day)),
    sec("Adicionais"),
    h("label", {class: "ck", style: "margin-bottom:10px"}, ck.sunday_holiday_double, "Horas em domingos e feriados em dobro (cl. 3.2.6)"),
    field("Feriados da sua base, além dos nacionais", hol),
    h("label", {class: "ck", style: "margin-bottom:10px"}, ck.dsr_enabled, "DSR sobre a parte variável"),
    h("div", {class: "row"}, field("Folgas do DSR", f.dsr_rest_days), field("Adicional noturno (%)", f.night_pct), field("Periculosidade (%)", f.hazard_pct)),
    h("p", {class: "fine", style: "margin-top:-4px"}, "A CCT 2025/26 não traz percentual de adicional noturno nem de periculosidade: deixe 0 se o seu acordo não paga."),
    sec("Diárias e benefícios"),
    h("div", {class: "row"}, field("Refeição (R$)", f.meal_value), field("Café (% da refeição)", f.breakfast_pct), field("Vale-alimentação (R$)", f.food_voucher)),
    h("label", {class: "ck", style: "margin-bottom:10px"}, ck.breakfast_in_hotel, "Café incluso no hotel depois de pernoite fora da base"),
    sec("Outros proventos fixos"), items,
    h("button", {class: "btn ghost sm", type: "button", onclick: () => addItem()}, icon("mais", 15), "Adicionar provento"),
    sec("Impostos (estimativa)"),
    h("div", {class: "row"}, h("label", {class: "ck", style: "align-self:center;margin-bottom:13px"}, ck.taxes_enabled, "Calcular INSS e IRRF de 2026"),
      field("Dependentes no IR", f.dependents)));
  const collect = () => {
    const data = {role: role.value, category: cat.value, preset: (base && base.preset) || "cct-2025-2026",
      meal_intl: (base && base.meal_intl) || undefined,
      extra_holidays: hol.value.split(/[,;\s]+/).map(x => x.trim()).filter(Boolean),
      other_items: [...items.children].map(r => { const [l, v] = r.querySelectorAll("input:not([type=checkbox])"); return {label: l.value.trim(), value: v.value === "" ? 0 : +v.value, taxable: r.querySelector("input[type=checkbox]").checked}; }).filter(i => i.label)};
    for (const [k, el] of Object.entries(f)) data[k] = el.value === "" ? (k === "food_voucher" ? null : el.value) : +el.value;
    for (const [k, el] of Object.entries(ck)) data[k] = el.checked;
    return {valid_from: valid.value, data};
  };
  return {el, collect};
}

async function togglePayroll(on) {
  const r = await act(() => api("PUT", "/api/payroll/enabled", {enabled: on}), on ? "Salários ligado" : "Salários desligado");
  if (!r) return;
  me.features = {...(me.features || {}), payroll: r.enabled};
  render();
}

/* ───────────────────────── ajustes ───────────────────────── */
function statsSharePanel() {
  const box = h("div", {class: "panel"}, h("h2", {}, "Estatísticas do projeto"), skeleton("s"));
  get("/api/estatisticas/envio").then(s => {
    const cb = h("input", {type: "checkbox", checked: s.enabled});
    cb.addEventListener("change", async () => { const r = await act(() => api("PUT", "/api/estatisticas/envio", {enabled: cb.checked}), cb.checked ? "Envio ligado" : "Envio desligado"); if (!r) cb.checked = !cb.checked; });
    put(box, h("h2", {}, "Estatísticas do projeto"),
      h("label", {class: "ck"}, cb, "Enviar o resumo de cada escala gerada para as estatísticas da LineOps"),
      h("p", {class: "fine"}, "Vai só o resumo da escala: companhia, tipo, base, período, número de pernas, horas, aeroportos e rotas mais usados, a versão e um código aleatório desta instalação (",
        h("span", {class: "mono"}, s.install), "). Não vai nome de usuário, senha, e-mail nem IP. Desligado, nada sai."));
  }).catch(() => box.remove());
  return box;
}
function profilePanel() {
  const name = h("input", {value: me.display_name || "", maxlength: 40});
  const file = h("input", {type: "file", accept: "image/jpeg,image/png,image/webp", hidden: true});
  const pic = h("div", {class: "pf-pic"});
  const paintPic = () => put(pic, me.avatar ? h("img", {src: `${me.avatar}?t=${Date.now()}`, alt: ""}) : h("span", {}, (me.display_name || "?").slice(0, 1).toUpperCase()));
  paintPic();
  file.addEventListener("change", async () => {
    const f = file.files[0]; file.value = "";
    if (!f) return;
    const r = await act(() => api("PUT", "/api/me/avatar", undefined, f), "Foto atualizada");
    if (r) { me.avatar = r.avatar; paintPic(); render(); }
  });
  return h("div", {class: "panel"}, h("h2", {}, "Perfil"),
    h("div", {class: "pf"}, pic, h("div", {class: "pf-body"},
      h("div", {class: "row"}, field("Nome de exibição", name), field("Código de piloto", h("input", {value: me.pilot_code || "", disabled: true}))),
      h("div", {class: "tools"},
        h("button", {class: "btn sm", onclick: async () => { const r = await act(() => api("PUT", "/api/me", {display_name: name.value}), "Nome salvo"); if (r) { me.display_name = r.display_name; render(); } }}, "Salvar nome"),
        h("button", {class: "btn ghost sm", onclick: () => file.click()}, "Trocar foto"),
        me.avatar ? h("button", {class: "btn ghost sm", onclick: async () => { const r = await act(() => api("DELETE", "/api/me/avatar"), "Foto removida"); if (r) { me.avatar = r.avatar; paintPic(); render(); } }}, "Tirar foto") : null, file),
      h("p", {class: "fine"}, "Foto em JPEG, PNG ou WebP, até 2 MB. Fica na pasta de dados deste computador; atualizar a LineOps não apaga. O código de piloto segue a companhia escolhida."))));
}
function discordPanel() {
  const box = h("div", {class: "panel"}, h("h2", {}, "Discord · pernoites"), skeleton("s"));
  const paint = info => {
    const url = h("input", {type: "url", placeholder: "https://discord.com/api/webhooks/…", autocomplete: "off"});
    put(box, h("h2", {}, "Discord · pernoites"),
      h("p", {class: "muted"}, "Na escala, cada voo ganha a caixa “ESSE VOO É PERNOITE”. Marcada, vai para esta webhook: voo, trecho, data, chegada (hora local), cidade do pernoite, próxima apresentação e hotel (o de Ajustes › Pernoites). Desmarcar apaga a mensagem."),
      info.configured ? note("go", "ok", info.default ? "Usando a webhook padrão: " : "Webhook cadastrada: ", h("span", {class: "mono"}, info.masked)) : note("hold", "alerta", "Nenhuma webhook cadastrada ainda."),
      field("Trocar por outra webhook (Discord › Editar canal › Integrações › Webhooks › Copiar URL)", url),
      h("div", {class: "tools"},
        h("button", {class: "btn sm", onclick: async () => { const r = await act(() => api("PUT", "/api/pernoite/discord", {url: url.value}), "Webhook salva"); if (r) paint(r); }}, "Salvar"),
        info.configured && !info.default ? h("button", {class: "btn ghost sm", onclick: async () => { const r = await act(() => api("PUT", "/api/pernoite/discord", {url: ""}), "Voltou para a webhook padrão"); if (r) paint(r); }}, "Voltar para a padrão") : null));
  };
  get("/api/pernoite/discord").then(paint).catch(() => box.remove());
  return box;
}
view("ajustes", async main => {
  const [batches, tokens, backups, rules, cfg] = await Promise.all([get("/api/import/batches"), get("/api/tokens"), get("/api/backups-info"), get("/api/rules"), get("/api/settings")]);
  const file = h("input", {type: "file", accept: ".zip,.db"}), out = h("div");
  const doImport = async dry => {
    if (!file.files[0]) return toast("Escolha o backup do AeroScale.", true);
    put(out, h("div", {class: "skel s"}));
    const r = await act(() => api("POST", "/api/import/aeroscale?dry_run=" + dry, undefined, file.files[0]));
    if (!r) return put(out);
    put(out, note(dry ? "" : "go", dry ? "info" : "ok",
      h("b", {}, dry ? "Prévia, nada foi gravado: " : "Importado: "),
      `${r.legs_total} pernas — ${r.legs_new} novas, ${r.legs_changed} alteradas, ${r.legs_unchanged} iguais, ${r.legs_removed} removidas, ${r.legs_restored} restauradas. `,
      `Origens: ${Object.entries(r.origins).map(([k, v]) => `${k} ${v}`).join(", ")}. Aeronaves novas: ${r.aircraft_new}.`),
      r.warnings.map(w => note("hold", "alerta", w)));
    if (!dry) { roster.bounds = null; setTimeout(render, 2200); }
  };
  const tname = h("input", {placeholder: "ex.: PC do simulador"}), tout = h("div");
  put(main, h("div", {class: "head"}, h("div", {}, h("h1", {}, "Ajustes"), h("div", {class: "sub"}, "Importação, acesso do ACARS e backups."))),
    h("div", {class: "panel"}, h("h2", {}, "Escala do AeroScale"),
      h("p", {class: "muted"}, "Mão única: o LineOps lê o backup e nunca altera o AeroScale. Reimportar é seguro, e reservas e voos já feitos são preservados."),
      field("Arquivo de backup (.zip ou .db)", file),
      h("div", {class: "tools"}, h("button", {class: "btn ghost", onclick: () => doImport(true)}, "Ver prévia"),
        h("button", {class: "btn", onclick: () => doImport(false)}, icon("importar", 16), "Importar")), out,
      batches.length ? h("details", {style: "margin-top:14px"}, h("summary", {class: "muted"}, `Importações anteriores (${batches.length})`),
        h("div", {class: "tbl", style: "margin-top:8px"}, h("table", {}, h("tbody", {}, batches.map(b => h("tr", {}, h("td", {}, dt(b.imported_at)),
          h("td", {class: "muted"}, b.source_name || "—"), h("td", {class: "t"}, "v" + (b.source_version || "?")),
          h("td", {class: "n"}, `${b.stats.legs_new || 0} novas`), h("td", {class: "n"}, `${b.stats.legs_changed || 0} alteradas`))))))) : null),
    profilePanel(),
    statsSharePanel(),
    me && me.features && me.features.pernoite_discord ? discordPanel() : null,
    h("div", {class: "panel"}, h("h2", {}, "Acesso do ACARS"),
      h("p", {class: "muted"}, "O cliente ACARS usa um token próprio: envia dados de voo e não administra nada. Revogue se perder o computador."),
      tokens.map(t => note("", "live", h("b", {}, t.name), ` · criado ${dt(t.created_at)} · último uso ${t.last_used_at ? dt(t.last_used_at) : "nunca"} `,
        h("button", {class: "btn stop sm", style: "margin-left:8px", onclick: async () => { if (await act(() => api("DELETE", "/api/tokens/" + t.id), "Token revogado")) render(); }}, "Revogar"))),
      h("div", {class: "row"}, field("Nome do novo token", tname),
        h("div", {style: "align-self:end;margin-bottom:13px"}, h("button", {class: "btn", onclick: async () => {
          if (!tname.value.trim()) return toast("Dê um nome ao token.", true);
          const r = await act(() => post("/api/tokens", {name: tname.value.trim()}));
          if (r) { put(tout, note("hold", "alerta", h("b", {}, "Copie agora, não aparece de novo: "), h("span", {class: "mono"}, r.token))); tname.value = ""; }
        }}, "Gerar token"))), tout),
    backupPanel(backups),
    h("div", {class: "panel"}, h("h2", {}, "SimBrief"),
      h("p", {class: "muted"}, "Com o Pilot ID guardado, o botão Importar OFP no Flight Center busca o seu plano mais recente sem perguntar nada."),
      (() => {
        const id = h("input", {value: cfg.simbrief_userid || "", placeholder: "123456"});
        const nm = h("input", {value: cfg.simbrief_username || "", placeholder: "opcional"});
        return h("div", {}, h("div", {class: "row"}, field("Pilot ID", id), field("Nome de usuário", nm)),
          h("button", {class: "btn", onclick: async () => { if (await act(() => api("PUT", "/api/settings", {simbrief_userid: id.value.trim(), simbrief_username: nm.value.trim()}), "SimBrief salvo")) render(); }}, "Salvar"));
      })()),
    phonePanel(),
    aboutPanel(),
    teamPanel(),
    stayPanel(),
    h("div", {class: "panel"}, h("h2", {}, "ACARS em voo"),
      h("p", {class: "muted"}, "Durante o voo acompanhado (IVAO ou cliente ACARS), a operação manda mensagens conforme o avião avança: cabine, pernoite ou próxima perna, tempo no destino e MCC. Elas aparecem no Flight Center como impressão do ACARS."),
      (() => {
        const ck = h("input", {type: "checkbox", checked: (cfg.acars_chime || "on") !== "off"});
        return h("div", {class: "tools"}, h("label", {class: "ck", style: "margin:0"}, ck, "Tocar o chime quando chegar mensagem"),
          h("button", {class: "btn ghost sm", onclick: chime}, "Ouvir"),
          h("button", {class: "btn sm", onclick: async () => { if (await act(() => api("PUT", "/api/settings", {acars_chime: ck.checked ? "on" : "off"}), "Salvo")) { prefs = null; render(); } }}, "Salvar"));
      })()),
    h("div", {class: "panel"}, h("h2", {}, "Chefia de Manutenção"),
      h("p", {class: "muted"}, "Todo voo termina com a razão e o G do toque. Pouso duro, pelo critério do fabricante do avião (737: 600 fpm ou 2,2 G; A320: 540 fpm ou 2,6 G; ATR, E-Jets, A330, 777 e 787: 600 fpm), ou reportado por você, bloqueia a aeronave para a inspeção até você fechar o item na Frota."),
      (() => {
        const sel = h("select", {}, [["folclorico", "Folclórico: a bronca que todo mundo conta no hangar"], ["just_culture", "Just culture: o tom que a Boeing recomenda"]].map(([v, l]) =>
          h("option", {value: v, selected: (cfg.landing_style || "folclorico") === v}, l)));
        return h("div", {class: "row"}, field("Rigor da chefia", sel), h("div", {style: "align-self:end;margin-bottom:13px"},
          h("button", {class: "btn", onclick: async () => { if (await act(() => api("PUT", "/api/settings", {landing_style: sel.value}), "Salvo")) render(); }}, "Salvar")));
      })()),
    h("div", {class: "panel"}, h("h2", {}, "IVAO"),
      h("p", {class: "muted"}, "Com o seu VID, o Flight Center acompanha o voo pela rede IVAO (o mesmo dado do Webeye) e monta o PIREP sozinho. O painel mostra quem da companhia está voando agora e o controle online nos seus aeroportos."),
      (() => {
        const vid = h("input", {value: cfg.ivao_vid || "", placeholder: "ex.: 123456", inputmode: "numeric"});
        return h("div", {}, h("div", {class: "row"}, field("VID da IVAO", vid)),
          h("button", {class: "btn", onclick: async () => { if (await act(() => api("PUT", "/api/settings", {ivao_vid: vid.value.trim()}), "VID salvo")) render(); }}, "Salvar"));
      })(),
      h("p", {class: "fine"}, "Só leitura de dados públicos: o LineOps não conecta você na IVAO nem guarda senha. Precisa de internet no computador do servidor.")),
    h("div", {class: "panel"}, h("h2", {}, "Salários (opcional)"),
      h("p", {class: "muted"}, "Contracheque simulado pela CCT dos aeronautas: parte fixa, horas acima da garantia, reserva, sobreaviso, domingos e feriados, DSR, diárias e a estimativa de INSS e IRRF, com as horas da sua escala do AeroScale e do seu diário. Desligado, não aparece em lugar nenhum."),
      h("div", {class: "tools"}, payOn() ? chip("Ligado", "go") : chip("Desligado", ""),
        h("button", {class: "btn" + (payOn() ? " ghost" : ""), onclick: () => togglePayroll(!payOn())}, payOn() ? "Desligar" : "Ligar"),
        payOn() ? h("a", {class: "btn ghost", href: "#/salarios"}, icon("salarios", 16), "Abrir Salários") : null)),
    h("div", {class: "panel"}, h("h2", {}, "Regras do AeroScale"),
      h("p", {class: "muted"}, "As regras de escala, tripulação, pernoite, aeroportos e despacho rodam com os módulos do próprio AeroScale, copiados sem alteração. Quem gera a escala continua sendo o AeroScale."),
      h("dl", {class: "kv"},
        h("dt", {}, "Versão dos módulos"), h("dd", {}, rules.version),
        h("dt", {}, "Perfil da companhia"), h("dd", {}, rules.profile ? `${rules.profile.label} · apresentação ${rules.profile.report_min} min` : "—"),
        h("dt", {}, "Cópia do banco"), h("dd", {}, rules.has_source_db ? `${(rules.size / 1048576).toFixed(1)} MB (da última importação)` : "ainda não importada")),
      rules.has_source_db ? null : note("hold", "alerta", "Sem a cópia do banco do AeroScale, o que depende do RPL (rota, alternado do SimBrief, rodízio) fica indisponível. Importe um backup."),
      h("p", {class: "fine"}, "Módulos: ", rules.modules.join(", "), ".")),
    h("p", {class: "fine"}, DISCLAIMER));
});

/* ───────────────────────── início ───────────────────────── */
/* o que há de novo: uma vez por versão, depois de atualizar */
const NEWS = {"3.0.2b4": [
  ["Continuações automáticas na geração", "O gerador agora segue sozinho para onde o avião costuma ir depois de cada voo (RPL + ADS-B), sem você cadastrar nada. Ex.: TAM3376 → Petrolina nos dias em que isso acontece."],
  ["Força das continuações", "Slider em Gerar escala: 0 desliga; quanto mais alto, mais vezes a continuação é obrigatória e mais incertas as que entram (alta a partir de 1, média a partir de 30, baixa a partir de 80). As que você cadastra valem sempre. O diagnóstico da escala diz quantas foram seguidas."],
  ["Pernoite marcado manda no dia", "Marcou “ESSE VOO É PERNOITE”? O pernoite do dia (resumo, hotel, carro e calendário do mês) passa a ser no destino desse voo, na hora."],
  ["Bogotá e aeroportos de fora", "Códigos IATA de fora do Brasil (BOG) são reconhecidos no pernoite e nas trocas. Sem voo da companhia para lá nos dados, a escala avisa em vez de inventar."]],
  "3.0.1": [
  ["Cabine noturna", "Visual novo: base azul-noite de painel de voo, as cores de aviônica com o significado da cabine (ciano = dado vivo, verde = ok, âmbar = atenção, vermelho = alerta) e a faixa de instrumentos no topo de toda tela, com hora UTC e local, a próxima apresentação em contagem regressiva e o próximo voo."],
  ["Estatísticas", "Tela nova: aeroportos mais usados, rotas, onde você mais pernoita, partidas por hora, etapa mais longa e dia mais cheio. Da escala ou do diário, do mês ou de tudo."],
  ["Aviso de pernoite para todos", "A caixa “ESSE VOO É PERNOITE” e o envio para o Discord, que eram só do cmdt079, agora valem para qualquer usuário."],
  ["Escalas geradas nas estatísticas do projeto", "Cada escala gerada manda um resumo anônimo (companhia, tipo, base, período, pernas, horas, aeroportos e rotas mais usados) para as estatísticas da LineOps. Sem nome de usuário, e-mail nem IP. Dá para desligar em Ajustes."],
  ["Muito mais tripulação", "Mais de mil nomes fictícios de copilotos e comissários."]],
  "3.0.0": [
  ["Escala mais rápida e mais certa", "Uma leitura só da escala para todas as telas, índices novos no banco e o app compactado (gzip): as telas abrem mais rápido e o painel de limites, as trocas, o mês e a exportação veem exatamente as mesmas pernas."],
  ["Paleta de comandos", "Ctrl + K (ou Buscar, na barra lateral): telas, ações, um dia (12/11) ou uma matrícula. Na Escala, ← → mudam o dia, T volta para hoje, D/S/M trocam o modo. ? mostra os atalhos."],
  ["Escala na sua agenda", "Escala → Texto → Agenda (.ics): Google Agenda, Outlook ou o calendário do celular, com folga, reserva e sobreaviso como dias inteiros."],
  ["Conquistas", "Na Carreira: marcos tirados do seu diário (voos, horas, pousos manteiga, regiões do Brasil, exterior, rede)."],
  ["Perfil", "Ajustes › Perfil: foto (qualquer usuário) e nome de exibição."],
  ["Folgas do seu jeito e senha esquecida", "No gerador: folgas automáticas, nenhuma, ou só as que você marcar no calendário. Na entrada: Esqueci a senha."]]};
function whatsNew(ver) {
  let seen = null;
  try { seen = localStorage.getItem("lineops.versao_vista"); localStorage.setItem("lineops.versao_vista", ver); } catch (e) { return; }
  const list = NEWS[ver];
  if (!list || seen === ver) return;
  dialog(`Novidades da ${ver}`, "O que mudou nesta versão. Seus dados ficaram como estavam (backup automático antes de atualizar).",
    h("ul", {class: "whatsnew"}, list.map(([t, d]) => h("li", {}, h("b", {}, t), ": ", d))), [{label: "Bora voar", fn: () => {}}]);
}
async function boot() {
  try {
    const hl = await fetch("/api/health", {credentials: "same-origin"}).then(r => r.json());
    if (hl.needs_setup) return showAuth(true);
    if (!hl.authenticated) return showAuth(false);
    me = await get("/api/me");
    applyTheme(me.career); DISCLAIMER = me.disclaimer || DISCLAIMER;
    try {
      const cs = await get("/api/careers");
      me.career_logo = !!(cs.items.find(x => x.code === me.career.code) || {}).has_logo;
      if (!cs.chosen && me.token_kind === "session") { location.hash = "#/modo"; return render(); }
    } catch (e) { /* segue na carreira padrão */ }
    try { pending = (await get("/api/today")).pending_pireps; } catch (e) { /* segue sem o contador */ }
    try {
      const ob = await get("/api/onboarding");
      if (!ob.done && (!ob.steps.escala || !ob.steps.frota) && ["", "#", "#/", "#/painel"].includes(location.hash)) { wiz.step = 0; location.hash = "#/boas-vindas"; }
    } catch (e) { /* sem assistente */ }
    render();
    whatsNew(hl.version);
  } catch (e) {
    if (e.status === 401) return showAuth(false);
    document.getElementById("app").replaceChildren(h("div", {class: "auth"}, h("div", {class: "panel box"},
      h("h2", {}, "Sem conexão com o servidor"), h("p", {class: "muted"}, "A LineOps não respondeu. Confira se a janela do servidor continua aberta."),
      h("button", {class: "btn wide", onclick: boot}, "Tentar de novo"))));
  }
}
boot();
