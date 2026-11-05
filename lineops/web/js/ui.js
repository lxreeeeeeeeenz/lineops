"use strict";
/* LineOps — base da interface: DOM, ícones, formatação e animação. Sem bibliotecas externas:
   o LineOps roda numa rede local que pode não ter internet, então nada é carregado de CDN. */
const UI = (() => {
const SVG = "http://www.w3.org/2000/svg";
const reduce = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ---------- DOM ---------- */
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || (v === false && !k.startsWith("aria-"))) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;
    else if (k === "value") el.value = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k.startsWith("aria-")) el.setAttribute(k, String(v));   // aria-pressed="true", não vazio
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  add(el, kids);
  return el;
}
function add(el, kids) {
  for (const c of kids.flat(Infinity)) {
    if (c == null || c === false || c === "") continue;
    el.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
}
function put(el, ...kids) { el.replaceChildren(); add(el, kids); return el; }
function svg(tag, attrs, ...kids) {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null && v !== false) el.setAttribute(k, v);
  for (const c of kids.flat(Infinity)) if (c != null && c !== false) el.append(c.nodeType ? c : document.createTextNode(String(c)));
  return el;
}

/* ---------- ícones (traço 1.6, grade 24) ---------- */
const PATHS = {
  painel: "M3 13h5l2-7 3 14 2-7h6",
  escala: "M4 6h16M4 6v13a1 1 0 001 1h14a1 1 0 001-1V6M8 3v5M16 3v5M7 12h4M7 16h8",
  voos: "M4 15l16-6-5 10-2-4z M9 17l-1 3",
  frota: "M12 3v10M7 21l5-5 5 5M4 13h16l-2-3H6z",
  pireps: "M8 4h8a1 1 0 011 1v15l-5-3-5 3V5a1 1 0 011-1zM9.5 9h5",
  diario: "M5 4h12a2 2 0 012 2v14H7a2 2 0 01-2-2zM9 8h6M9 12h6",
  ajustes: "M5 7h14M5 12h14M5 17h14M9 5v4M15 10v4M11 15v4",
  voo: "M4 15l16-6-5 10-2-4z M9 17l-1 3",
  despacho: "M9 4h6v3H9z M7 5.5H6v15h12v-15h-1 M9 11h6 M9 15h4",
  rede: "M8 10.5a3 3 0 100-6 3 3 0 000 6z M16.5 10.5a2.6 2.6 0 100-5.2 2.6 2.6 0 000 5.2z M2.5 19.5c.4-3.2 2.6-5 5.5-5s5.1 1.8 5.5 5 M14 14.6c.8-.3 1.6-.5 2.5-.5 2.5 0 4.4 1.6 4.9 4.6",
  estrela: "M12 3.5l2.6 5.3 5.8.8-4.2 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.2-4.1 5.8-.8z",
  carreira: "M12 3.5l2.6 5.3 5.8.8-4.2 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.2-4.1 5.8-.8z",
  externos: "M12 21a9 9 0 100-18 9 9 0 000 18z M3.5 9h17 M3.5 15h17 M12 3c2.4 2.5 3.6 5.5 3.6 9s-1.2 6.5-3.6 9 M12 3C9.6 5.5 8.4 8.5 8.4 12s1.2 6.5 3.6 9",
  mensagens: "M4 6.5A1.5 1.5 0 015.5 5h13A1.5 1.5 0 0120 6.5v9a1.5 1.5 0 01-1.5 1.5H10l-4 3v-3h-.5A1.5 1.5 0 014 15.5z M8 10h8 M8 13h5",
  salarios: "M3 7h18v10H3z M12 9.5a2.5 2.5 0 100 5 2.5 2.5 0 000-5z M6.5 10v4 M17.5 10v4",
  decolar: "M3 19h18M4 13l3 1 10-6-3-4 2-1 5 4-13 9-5-1z",
  pousar: "M3 19h18M20 14l-3-1-2-11-2 1v9l-6-2-1-3-2 1 1 6z",
  pernoite: "M20 14a7.5 7.5 0 01-9.8-9.7A7.5 7.5 0 1020 14z",
  reserva: "M12 7v5M12 3a9 9 0 100 18 9 9 0 000-18z",
  relogio: "M12 3a9 9 0 100 18 9 9 0 000-18zM12 7v5l3 2",
  ok: "M4 12.5l5 5L20 7",
  alerta: "M12 4l9 16H3zM12 10v4M12 17.5v.5",
  bloqueio: "M12 3a9 9 0 100 18 9 9 0 000-18zM7 7l10 10",
  pendente: "M12 3a9 9 0 100 18 9 9 0 000-18zM12 8v4l3 2",
  futuro: "M12 3a9 9 0 100 18 9 9 0 000-18zM8 12h8",
  seta: "M9 5l7 7-7 7",
  troca: "M4 8h13l-3-3 M17 8l-3 3 M20 16H7l3 3 M7 16l3-3",
  busca: "M11 4a7 7 0 100 14 7 7 0 000-14z M20 20l-4-4",
  trofeu: "M8 4h8v5a4 4 0 01-8 0V4z M8 6H5a3 3 0 003 3 M16 6h3a3 3 0 01-3 3 M12 13v4 M9 20h6 M10 17h4v3h-4z",
  mais: "M12 5v14M5 12h14",
  importar: "M12 3v11M8 10l4 4 4-4M4 19h16",
  baixar: "M12 3v11M8 10l4 4 4-4M4 19h16",
  vazio: "M4 17l5-9 4 5 3-3 4 7zM4 4h16v16H4z",
  info: "M12 3a9 9 0 100 18 9 9 0 000-18zM12 11v6M12 7.5v.5",
  live: "M12 9a3 3 0 100 6 3 3 0 000-6M6.5 6.5a7.5 7.5 0 000 11M17.5 6.5a7.5 7.5 0 010 11",
};
function icon(name, size = 18, cls) {
  const s = svg("svg", {width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": 1.6, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", class: cls || ""});
  for (const d of (PATHS[name] || "").split(" M").map((p, i) => (i ? "M" + p : p))) if (d) s.append(svg("path", {d}));
  return s;
}

/* ---------- formatação ---------- */
const pad2 = n => String(n).padStart(2, "0");
const hhmm = s => s ? s.slice(11, 16) : "—";
const hhmmz = s => s ? s.slice(11, 16) + "Z" : "—";
const dmy = s => s ? `${s.slice(8, 10)}/${s.slice(5, 7)}/${s.slice(0, 4)}` : "—";
const dm = s => s ? `${s.slice(8, 10)}/${s.slice(5, 7)}` : "—";
const dt = s => s ? `${dmy(s)} ${hhmmz(s)}` : "—";
const hm = m => m == null ? "—" : `${Math.floor(Math.abs(m) / 60)}:${pad2(Math.abs(m) % 60)}`;
const hmLong = m => m == null ? "—" : (m >= 60 ? `${Math.floor(m / 60)}h${m % 60 ? pad2(m % 60) : ""}` : `${m} min`);
const kg = v => v == null ? "—" : Math.round(v).toLocaleString("pt-BR") + " kg";
const today = () => new Date().toISOString().slice(0, 10);
const addDays = (d, n) => new Date(Date.parse(d + "T00:00:00Z") + n * 864e5).toISOString().slice(0, 10);
const cap = s => s ? s[0].toUpperCase() + s.slice(1) : s;
function localPair(utc, local) {                       // "23:55Z · 20:55 em Brasília" sem inventar fuso
  if (!utc) return "—";
  return local ? `${hhmmz(utc)} · ${local.hhmm} local` : hhmmz(utc);
}

/* ---------- animação ---------- */
function stagger(root, sel = ":scope > *") {
  if (reduce()) return;
  [...root.querySelectorAll(sel)].forEach((el, i) => { el.classList.add("rise"); el.style.setProperty("--i", Math.min(i, 9)); });
}
function countUp(el, to, fmt = String, dur = 620) {
  if (reduce() || typeof to !== "number" || !isFinite(to)) { el.textContent = fmt(to); return; }
  const t0 = performance.now();
  const step = now => {
    const p = Math.min((now - t0) / dur, 1), e = 1 - Math.pow(1 - p, 3);
    el.textContent = fmt(to * e);
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
function drawLine(path) {                              // prepara o traço para a animação de desenho
  try { path.style.setProperty("--len", path.getTotalLength().toFixed(1)); } catch (e) { /* fora do DOM */ }
}
function expand(el, open) {
  if (open === undefined) open = !el.hasAttribute("open-x");
  el.toggleAttribute("open-x", open);
  const btn = el.querySelector("button[aria-expanded]");
  if (btn) btn.setAttribute("aria-expanded", String(open));
  return open;
}
async function transition(fn) {                        // troca de tela suave quando o navegador suportar
  if (reduce() || !document.startViewTransition) return fn();
  return document.startViewTransition(fn).finished.catch(() => {});
}

/* ---------- avisos flutuantes ---------- */
let toastTimer;
function toast(msg, err) {
  const t = document.getElementById("toast");
  put(t, icon(err ? "alerta" : "ok", 17), String(msg));
  t.className = "show" + (err ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.className = "", err ? 6500 : 2800);
}

/* ---------- diálogo ---------- */
const dlg = () => document.getElementById("dlg");
function dialog(title, subtitle, body, buttons) {
  const d = dlg();
  put(d, h("h2", {}, title), subtitle ? h("p", {class: "muted", style: "font-size:13.5px"}, subtitle) : null, body,
    h("div", {class: "btns"}, buttons.map(b => h("button", {class: "btn " + (b.cls || ""), type: "button", onclick: async ev => {
      const btn = ev.currentTarget;
      btn.disabled = true;
      try { const r = await b.fn?.(); if (r !== false && !b.keep) d.close(); } finally { btn.disabled = false; }
    }}, b.label))));
  if (!d.open) d.showModal();
  const first = d.querySelector("input,select,textarea");
  if (first) setTimeout(() => first.focus(), 60);
}
const closeBtn = {label: "Fechar", cls: "ghost", fn: () => {}};
const field = (label, input) => h("label", {class: "f"}, h("span", {}, label), input);
const chip = (text, cls, title) => h("span", {class: "chip " + (cls || ""), title: title || null},
  cls && cls.includes("live") ? h("span", {class: "dot"}) : null, text);
const note = (kind, iconName, ...body) => h("div", {class: "note " + (kind || "")}, icon(iconName || "info", 17), h("div", {}, body));
const fact = (label, value, mono) => h("div", {class: "fact"}, h("span", {}, label), h("b", {class: mono ? "mono" : ""}, value));
const skeleton = (...kinds) => h("div", {class: "cols", style: "gap:14px"}, kinds.map(k => h("div", {class: "skel " + k})));

return {h, put, add, svg, icon, stagger, countUp, drawLine, expand, transition, toast, dialog, closeBtn, field, chip, note, fact,
        skeleton, reduce, fmt: {hhmm, hhmmz, dmy, dm, dt, hm, hmLong, kg, today, addDays, cap, localPair, pad2}};
})();
