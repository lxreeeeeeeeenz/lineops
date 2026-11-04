"""Cartas de aeródromo pelo AISWEB (DECEA) – v2.2.1.

Funciona sem configurar nada: quando o usuário abre um aeródromo, o AeroScale lê a lista de cartas na página
pública do aeródromo no AISWEB (a mesma que ele abriria no navegador) e guarda o resultado. Não é um robô: não
segue links nem varre o site; é uma leitura por aeródromo, por ciclo AIRAC, sempre a pedido do usuário. Os PDFs
são os links públicos ("uso ostensivo") dessa página.

Opcional (avançado): com a chave gratuita da API oficial do AISWEB (Publicações → API-AISWEB), a lista vem da
API (area=cartas) em vez da página.

Comportamento conservador:
* a lista de cartas de um aeródromo é guardada por ciclo AIRAC (28 dias) e relida no máximo 1 vez por semana;
* o PDF é baixado só quando a carta é aberta (ou quando o usuário pede "baixar para usar offline") e fica em
  data/cartas/<ICAO>/; depois disso abre sem internet;
* downloads em lote com pausa entre arquivos.

Formato da resposta (documentação oficial da API AISWEB, Postman): <aisweb><cartas emenda=".." total="..">
<item><id/><especie/><tipo/><tipo_descr/><nome/><IcaoCode/><dt/><link/><arquivo/><icp/><pe/><notam/>
<tabcode/></item>...</cartas></aisweb>.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

from .db import get_settings, now_iso, row

DEFAULT_URL = "https://api.decea.mil.br/aisweb/"
PAGE_URL = "https://aisweb.decea.gov.br/?i=aerodromos&codigo={icao}"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AeroScale/2.2 (simulacao de voo)"
CODES = {"ADC", "PDC", "GMC", "AGMC", "AOC", "SID", "STAR", "IAC", "VAC", "ARC", "MVA", "ENRC", "PATC", "VFR"}
KEY_URL = "https://aisweb.decea.mil.br/?i=publicacoes&p=api"
ALLOWED_HOSTS = ("decea.gov.br", "decea.mil.br", "aer.mil.br")
UA = "AeroScale (simulacao de voo; cartas sob demanda)"
TIMEOUT = 25
IFR_TIPOS = ["ADC", "PDC", "AOC", "GMC", "SID", "STAR", "IAC", "VAC"]
ORDER = ["ADC", "PDC", "GMC", "AGMC", "AOC", "SID", "STAR", "IAC", "VAC", "ARC", "MVA", "ENRC"]
TIPO_LABEL = {
    "ADC": "Carta de aeródromo (ADC)", "PDC": "Estacionamento e docagem (PDC)", "GMC": "Movimentação no solo (GMC)",
    "AGMC": "Movimentação no solo de aeródromo (AGMC)", "AOC": "Obstáculos de aeródromo (AOC)",
    "SID": "Saídas por instrumentos (SID)", "STAR": "Chegadas padrão (STAR)", "IAC": "Aproximação por instrumentos (IAC)",
    "VAC": "Aproximação visual (VAC)", "ARC": "Carta de área (ARC)", "MVA": "Altitude mínima de vetoração (MVA)",
    "ENRC": "Rota (ENRC)",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS chart_cache (
    icao TEXT PRIMARY KEY, airac TEXT, emenda TEXT, fetched_at TEXT NOT NULL, items TEXT NOT NULL
);
"""


class ChartsError(Exception):
    pass


class NoKey(ChartsError):
    pass


def airac_start(d: date | None = None) -> date:
    """Início do ciclo AIRAC vigente (ciclos de 28 dias; AIRAC 2001 começou em 02/01/2020)."""
    d = d or datetime.now(timezone.utc).date()
    anchor = date(2020, 1, 2)
    return anchor + timedelta(days=(d - anchor).days // 28 * 28)


def configured(s: dict) -> bool:
    return bool((s.get("aisweb_api_key") or "").strip() and (s.get("aisweb_api_pass") or "").strip())


def _base(s: dict) -> str:
    return (s.get("aisweb_url") or DEFAULT_URL).strip() or DEFAULT_URL


def _request(s: dict, params: dict) -> bytes:
    q = {"apiKey": s["aisweb_api_key"].strip(), "apiPass": s["aisweb_api_pass"].strip(), "area": "cartas", **params}
    url = _base(s) + ("&" if "?" in _base(s) else "?") + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/xml,text/xml"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise ChartsError(f"AISWEB respondeu HTTP {e.code}. Confira a chave em Configurações → Cartas (AISWEB).")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ChartsError(f"Sem conexão com o AISWEB ({getattr(e, 'reason', e)}).")


def parse_xml(data: bytes) -> dict:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        txt = data[:200].decode("utf-8", "replace").strip()
        raise ChartsError("Resposta do AISWEB não é XML" + (f": {txt}" if txt else "."))
    cartas = root if root.tag == "cartas" else root.find(".//cartas")
    if cartas is None:
        msg = " ".join(t.strip() for t in root.itertext() if t.strip())[:200]
        raise ChartsError(f"AISWEB: {msg or 'resposta sem cartas'} (chave e senha da API corretas?)")
    items = []
    for it in cartas.findall("item"):
        d = {c.tag: (c.text or "").strip() for c in it}
        items.append({
            "id": d.get("id") or d.get("arquivo") or d.get("nome"),
            "especie": d.get("especie", ""), "tipo": (d.get("tipo") or "").upper(), "tipo_descr": d.get("tipo_descr", ""),
            "nome": d.get("nome", ""), "icao": (d.get("IcaoCode") or d.get("icaocode") or d.get("icaoCode") or "").upper(),
            "dt": d.get("dt", ""), "link": d.get("link", ""), "icp": d.get("icp", ""), "tabcode": d.get("tabcode", ""),
        })
    return {"emenda": cartas.get("emenda", ""), "total": cartas.get("total", ""), "items": items}


def _safe_id(cid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(cid))[:80]


def chart_dir(data_dir: str, icao: str) -> str:
    return os.path.join(data_dir, "cartas", re.sub(r"[^A-Z0-9]", "", icao.upper())[:4])


def pdf_path(data_dir: str, icao: str, cid: str) -> str:
    return os.path.join(chart_dir(data_dir, icao), f"{_safe_id(cid)}.pdf")


def _valid_icao(icao: str) -> str:
    icao = (icao or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{4}", icao):
        raise ValueError("Informe o código ICAO do aeródromo (4 letras, ex.: SBGR).")
    return icao


# ------------------------------------------------------------------ página pública do aeródromo (sem chave)
from html.parser import HTMLParser  # noqa: E402


class _PageParser(HTMLParser):
    """Lê os links de download de cartas e o título de categoria (ADC, SID, IAC…) mais próximo antes de cada um."""

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base, self.cat, self.items = base, None, []
        self._a = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if "arquivo=" in href and "download" in href:
                self._a = {"href": urllib.parse.urljoin(self.base, href.replace("&amp;", "&")),
                           "title": dict(attrs).get("title") or "", "text": []}

    def handle_endtag(self, tag):
        if tag == "a" and self._a is not None:
            a, self._a = self._a, None
            name = re.sub(r"\s+", " ", " ".join(a["text"])).strip() or a["title"]
            q = urllib.parse.parse_qs(urllib.parse.urlparse(a["href"]).query)
            cid = (q.get("arquivo") or [name])[0]
            self.items.append({"id": cid, "especie": "IFR", "tipo": self.cat or _guess_tipo(name), "tipo_descr": "",
                               "nome": name, "icao": "", "dt": "", "link": a["href"], "icp": "", "tabcode": ""})

    def handle_data(self, data):
        if self._a is not None:
            self._a["text"].append(data)
            return
        t = data.strip().upper()
        m = re.match(r"^([A-Z]{3,4})(?:\s*\(\d+\))?$", t)
        if m and m.group(1) in CODES:
            self.cat = m.group(1)


def _guess_tipo(name: str) -> str:
    n = name.upper()
    for k in ("ADC", "PDC", "AGMC", "GMC", "AOC", "VAC", "STAR", "SID", "ARC"):
        if re.search(rf"\b{k}\b", n):
            return k
    if re.match(r"^(ILS|RNP|RNAV \(GNSS\)|VOR|NDB|LOC|GLS)", n):
        return "IAC"
    return "OUTRAS"


def fetch_page(s: dict, icao: str) -> tuple[list[dict], str]:
    """Lista de cartas lida da página pública do aeródromo no AISWEB (sem chave)."""
    url = (s.get("aisweb_page_url") or PAGE_URL).format(icao=icao)
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "text/html",
                                               "Accept-Language": "pt-BR,pt;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            html_ = r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise ChartsError(f"O AISWEB respondeu HTTP {e.code} ao abrir a página de {icao}.")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ChartsError(f"Sem conexão com o AISWEB ({getattr(e, 'reason', e)}).")
    p = _PageParser(url)
    p.feed(html_)
    m = re.search(r"emenda[^0-9]{0,40}(\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})", html_, re.I)
    if not p.items and icao not in html_.upper():
        raise ChartsError("A página do AISWEB mudou ou não carregou; abra o aeródromo no AISWEB pelo navegador.")
    seen, out = set(), []
    for i in sorted(p.items, key=_sort_key):
        if i["id"] not in seen:
            seen.add(i["id"])
            out.append(i)
    return out, (m.group(1) if m else "")


def _sort_key(it):
    t = it["tipo"]
    return (ORDER.index(t) if t in ORDER else len(ORDER), it["nome"])


def list_charts(db, data_dir: str, icao: str, refresh: bool = False) -> dict:
    """Lista de cartas do aeródromo (cache por ciclo AIRAC) + PDFs que o usuário pôs na pasta data/cartas/ICAO."""
    icao = _valid_icao(icao)
    s = get_settings(db)
    cur = airac_start().isoformat()
    c = row(db.execute("SELECT * FROM chart_cache WHERE icao=?", (icao,)))
    fresh = c and c["airac"] == cur and \
        datetime.now(timezone.utc) - datetime.fromisoformat(c["fetched_at"].replace("Z", "+00:00")) < timedelta(days=7)
    warning = None
    items, emenda, fetched = (json.loads(c["items"]), c["emenda"], c["fetched_at"]) if c else ([], "", None)
    source = "api" if configured(s) else "pagina"
    if refresh or not fresh:
        if True:
            try:
                items, emenda = fetch_remote(s, icao) if source == "api" else fetch_page(s, icao)
                fetched = now_iso()
                db.execute("INSERT OR REPLACE INTO chart_cache(icao, airac, emenda, fetched_at, items) VALUES (?,?,?,?,?)",
                           (icao, cur, emenda, fetched, json.dumps(items, ensure_ascii=False)))
                db.commit()
            except ChartsError as e:
                if not c:
                    raise
                warning = f"{e} Mostrando a lista guardada de {c['fetched_at'][:10]}."
    return _result(icao, items, data_dir, emenda, fetched, configured=True, warning=warning, source=source)


def fetch_remote(s: dict, icao: str) -> tuple[list[dict], str]:
    r = parse_xml(_request(s, {"IcaoCode": icao}))
    items = [i for i in r["items"] if i["icao"] in ("", icao) and i["especie"].upper() != "ROTA"]
    emenda = r["emenda"]
    if not items:
        # algumas versões da API exigem espécie e tipo: pergunta tipo a tipo (poucas chamadas, com pausa)
        for t in IFR_TIPOS:
            rr = parse_xml(_request(s, {"IcaoCode": icao, "especie": "IFR", "tipo": t}))
            items += [i for i in rr["items"] if i["icao"] in ("", icao)]
            emenda = emenda or rr["emenda"]
            time.sleep(0.3)
    seen, out = set(), []
    for i in sorted(items, key=_sort_key):
        if i["id"] and i["id"] not in seen and i["link"]:
            seen.add(i["id"])
            out.append(i)
    return out, emenda


def _result(icao, items, data_dir, emenda, fetched, configured: bool, warning: str | None = None,
            source: str = "pagina") -> dict:
    d = chart_dir(data_dir, icao)
    known = set()
    out = []
    for i in items:
        p = pdf_path(data_dir, icao, i["id"])
        known.add(os.path.basename(p))
        out.append({**{k: i[k] for k in ("id", "tipo", "tipo_descr", "nome", "dt", "icp")},
                    "offline": os.path.exists(p), "source": "aisweb"})
    if os.path.isdir(d):   # PDFs que o usuário colocou na pasta (Navigraph, cartas antigas…)
        for fn in sorted(os.listdir(d)):
            if fn.lower().endswith(".pdf") and fn not in known:
                out.append({"id": "local-" + fn[:-4], "tipo": "LOCAL", "tipo_descr": "Arquivo local", "nome": fn[:-4],
                            "dt": "", "icp": "", "offline": True, "source": "local"})
    groups = []
    for it in out:
        if not groups or groups[-1]["tipo"] != it["tipo"]:
            groups.append({"tipo": it["tipo"], "label": TIPO_LABEL.get(it["tipo"], it["tipo_descr"] or it["tipo"]), "items": []})
        groups[-1]["items"].append(it)
    return {"icao": icao, "configured": configured, "emenda": emenda, "fetched_at": fetched, "airac": airac_start().isoformat(),
            "total": len(out), "offline": sum(1 for x in out if x["offline"]), "groups": groups, "warning": warning,
            "source": source,
            "aisweb_page": PAGE_URL.format(icao=icao), "key_url": KEY_URL}


def _host_ok(link: str, s: dict) -> bool:
    u = urllib.parse.urlparse(link)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    base_host = urllib.parse.urlparse(_base(s)).hostname or ""
    page_host = urllib.parse.urlparse((s.get("aisweb_page_url") or PAGE_URL).format(icao="SBGR")).hostname or ""
    return u.hostname in (base_host, page_host) or any(u.hostname == h or u.hostname.endswith("." + h) for h in ALLOWED_HOSTS)


def get_pdf(db, data_dir: str, icao: str, cid: str) -> str:
    """Caminho do PDF da carta; baixa do AISWEB na primeira vez."""
    icao = _valid_icao(icao)
    if cid.startswith("local-"):
        p = os.path.join(chart_dir(data_dir, icao), cid[6:] + ".pdf")
        if os.path.exists(p) and os.path.dirname(os.path.abspath(p)) == os.path.abspath(chart_dir(data_dir, icao)):
            return p
        raise LookupError("Arquivo local não encontrado.")
    p = pdf_path(data_dir, icao, cid)
    if os.path.exists(p):
        return p
    c = row(db.execute("SELECT items FROM chart_cache WHERE icao=?", (icao,)))
    it = next((i for i in json.loads(c["items"]) if str(i["id"]) == str(cid)), None) if c else None
    if not it:
        raise LookupError("Carta não encontrada na lista. Atualize a lista do aeródromo.")
    s = get_settings(db)
    if not _host_ok(it["link"], s):
        raise ChartsError("Link da carta fora do AISWEB; download recusado.")
    req = urllib.request.Request(it["link"], headers={"User-Agent": BROWSER_UA,
                                                      "Referer": (s.get("aisweb_page_url") or PAGE_URL).format(icao=icao)})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ChartsError(f"Não foi possível baixar a carta do AISWEB ({getattr(e, 'reason', e)}).")
    if not data.startswith(b"%PDF"):
        raise ChartsError("O AISWEB não devolveu um PDF para esta carta (link expirado? atualize a lista).")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, p)
    return p


def download_all(db, data_dir: str, icaos: list[str], progress=None, pause: float = 0.5) -> dict:
    """Baixa para uso offline todas as cartas dos aeródromos (tarefa em segundo plano)."""
    icaos = [_valid_icao(x) for x in dict.fromkeys(icaos)]
    done = fail = skip = 0
    errors = []
    lists = []
    for k, icao in enumerate(icaos):
        if progress:
            progress(0.05 + 0.15 * k / max(1, len(icaos)), f"lista de cartas de {icao}")
        try:
            lists.append(list_charts(db, data_dir, icao))
        except ChartsError as e:
            errors.append(f"{icao}: {e}")
    todo = [(l["icao"], it["id"]) for l in lists for g in l["groups"] for it in g["items"] if it["source"] == "aisweb"]
    for k, (icao, cid) in enumerate(todo):
        if progress:
            progress(0.2 + 0.8 * k / max(1, len(todo)), f"{icao}: carta {k + 1} de {len(todo)}")
        if os.path.exists(pdf_path(data_dir, icao, cid)):
            skip += 1
            continue
        try:
            get_pdf(db, data_dir, icao, cid)
            done += 1
            time.sleep(pause)
        except (ChartsError, LookupError) as e:
            fail += 1
            if len(errors) < 10:
                errors.append(f"{icao}: {e}")
    return {"airports": len(icaos), "downloaded": done, "already": skip, "failed": fail, "errors": errors}


def test_credentials(db) -> dict:
    s = get_settings(db)
    if not configured(s):
        raise NoKey("Informe a chave (apiKey) e a senha (apiPass) da API AISWEB.")
    items, emenda = fetch_remote(s, "SBGR")
    return {"ok": True, "message": f"Chave aceita: {len(items)} carta(s) de SBGR (emenda {emenda or '—'})."}
