"""Parser de arquivos CGNA/RPL ("Planos de Voo Repetitivos").

Formato observado nos arquivos AZU/GLO/TAM (validade 22out.2026):

    CIA: GLO        INÍCIO DE VALIDADE: 22out.2026        PAG.: 1
    ---------------------------------------------------------------
       VALIDO VALIDO DIAS OP  IDENT  TIPO   ADEP     VEL   FL  ROTA      DEST         OBSERVACOES
       DESDE   ATE   STQQSSD   ANV     TURB     EOBT                         EET
    ---------------------------------------------------------------
       221026 231026 0004500 GLO1000 B738/M SBSP0925 N0367 270 NIBRU ... SBRJ0043 EQPT/...
                                                                (rota cont.)        (obs. cont.)

* Largura fixa. As colunas ROTA / DEST / OBSERVACOES são localizadas a partir
  do cabeçalho de cada arquivo (adaptável); se o cabeçalho não existir, usamos
  as posições observadas (59 / 95 / 104).
* Linhas de continuação trazem o resto da rota (coluna ROTA) e/ou das
  observações (coluna OBSERVACOES).
* DIAS OP: 7 posições Seg..Dom; cada posição vale 0 ou o número do dia (1..7).
* EOBT e EET em HHMM. EOBT é UTC (padrão ICAO para planos de voo).

O parser não descarta nada: cada registro guarda o texto bruto original e uma
lista de problemas (erro/aviso). Registros com erro continuam visíveis, mas não
entram na geração de escalas.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta

DEFAULT_COLS = {"route": 59, "dest": 95, "obs": 104}

MONTHS_PT = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}

MAIN_RE = re.compile(
    r"^\s{0,6}(?P<vf>\d{6})\s+(?P<vt>\d{6})\s+(?P<days>[0-9]{7})\s+"
    r"(?P<ident>[A-Z0-9]{3,8})\s+(?P<type>[A-Z0-9]{2,4})/(?P<wake>[LMHJ])\s+"
    r"(?P<adep>[A-Z]{4})(?P<eobt>\d{4})\s+(?P<speed>[NKM]\d{3,4})\s+(?P<fl>[A-Z]?\d{2,4}|VFR)\s"
)
LOOSE_MAIN_RE = re.compile(r"^\s{0,6}\d{6}\s+\d{6}\s")
DEST_RE = re.compile(r"^(?P<ades>[A-Z]{4})(?P<eet>\d{4})$")
DEST_SEARCH_RE = re.compile(r"\s(?P<ades>[A-Z]{4})(?P<eet>\d{4})(?:\s|$)")
HEADER_MARKERS = ("PLANOS DE VOO", "CLASSIFICA", "CIA:", "VALIDO", "DESDE", "-----")
REMARK_KEY_RE = re.compile(r"^([A-Z]{2,5})/(.*)$")


@dataclass
class Issue:
    severity: str  # error | warning | info
    code: str
    message: str


@dataclass
class RplRecord:
    line_no: int
    raw_lines: list[str]
    airline: str = ""
    ident: str = ""
    flight_number: str = ""
    valid_from: str | None = None
    valid_to: str | None = None
    days: str = ""
    type_icao: str = ""
    wake: str = ""
    adep: str = ""
    eobt: str = ""
    speed: str = ""
    fl: str = ""
    route: str = ""
    ades: str = ""
    eet: str = ""
    eet_min: int | None = None
    remarks: str = ""
    remarks_dict: dict = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    @property
    def status(self) -> str:
        sev = {i.severity for i in self.issues}
        return "error" if "error" in sev else "warning" if "warning" in sev else "ok"

    def add(self, severity, code, message):
        self.issues.append(Issue(severity, code, message))

    def to_dict(self):
        d = asdict(self)
        d["status"] = self.status
        d["raw_text"] = "\n".join(self.raw_lines)
        return d


@dataclass
class RplParseResult:
    airline: str | None
    header_validity: str | None
    header_validity_date: str | None
    encoding: str
    sha256: str
    layout: dict
    records: list[RplRecord]
    file_issues: list[Issue]

    @property
    def valid_from(self):
        ds = [r.valid_from for r in self.records if r.valid_from]
        return min(ds) if ds else None

    @property
    def valid_to(self):
        ds = [r.valid_to for r in self.records if r.valid_to]
        return max(ds) if ds else None

    def counts(self):
        w = sum(1 for r in self.records if r.status == "warning")
        e = sum(1 for r in self.records if r.status == "error")
        return {"records": len(self.records), "warnings": w, "errors": e}


def decode_bytes(data: bytes) -> tuple[str, str]:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace"), "latin-1"


def parse_ddmmyy(s: str) -> date | None:
    try:
        d, m, y = int(s[0:2]), int(s[2:4]), int(s[4:6])
        return date(2000 + y, m, d)
    except (ValueError, IndexError):
        return None


def hhmm_to_min(s: str) -> int | None:
    if not re.fullmatch(r"\d{4}", s or ""):
        return None
    h, m = int(s[:2]), int(s[2:])
    if m >= 60:
        return None
    return h * 60 + m


def parse_remarks(text: str) -> dict:
    """'EQPT/SDFG PBN/B1C1 RMK/TCAS EQUIPPED' -> {'EQPT': 'SDFG', 'PBN': 'B1C1', 'RMK': 'TCAS EQUIPPED'}"""
    out: dict[str, str] = {}
    key = None
    for tok in text.split():
        m = REMARK_KEY_RE.match(tok)
        if m:
            key = m.group(1)
            out[key] = (out[key] + " " + m.group(2)).strip() if key in out else m.group(2)
        elif key:
            out[key] = (out[key] + " " + tok).strip()
        else:
            out.setdefault("_SEM_CHAVE", "")
            out["_SEM_CHAVE"] = (out["_SEM_CHAVE"] + " " + tok).strip()
    return out


def _detect_layout(lines: list[str]) -> tuple[dict, list[Issue]]:
    issues = []
    for ln in lines[:200]:
        if "ROTA" in ln and "DEST" in ln and "ADEP" in ln:
            # O rótulo OBSERVACOES não fica alinhado aos dados; o campo DEST+EET
            # tem sempre 8 caracteres, então OBS começa em DEST + 9.
            cols = {"route": ln.find("ROTA"), "dest": ln.find("DEST")}
            cols["obs"] = cols["dest"] + 9
            if not (0 < cols["route"] < cols["dest"] < cols["obs"]):
                break
            return cols, issues
    issues.append(Issue("warning", "layout_padrao",
                        "Cabeçalho de colunas não encontrado; usando posições padrão do formato RPL."))
    return dict(DEFAULT_COLS), issues


def parse_rpl(data: bytes, filename: str = "") -> RplParseResult:
    text, encoding = decode_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    file_issues: list[Issue] = []
    if encoding != "utf-8-sig":
        file_issues.append(Issue("info", "encoding", f"Arquivo decodificado como {encoding} (não UTF-8)."))

    layout, lay_issues = _detect_layout(lines)
    file_issues += lay_issues

    airline = None
    header_validity = None
    header_validity_date = None
    for ln in lines[:60]:
        m = re.search(r"CIA:\s*([A-Z0-9]{2,3})", ln)
        if m and not airline:
            airline = m.group(1)
        m = re.search(r"VALIDADE:\s*(\d{1,2})([a-zç]{3})\.?(\d{4})", ln, re.I)
        if m and not header_validity:
            header_validity = m.group(0).split(":", 1)[1].strip()
            mon = MONTHS_PT.get(m.group(2).lower())
            if mon:
                try:
                    header_validity_date = date(int(m.group(3)), mon, int(m.group(1))).isoformat()
                except ValueError:
                    pass

    records: list[RplRecord] = []
    current: RplRecord | None = None
    rc, dc, oc = layout["route"], layout["dest"], layout["obs"]

    for idx, ln in enumerate(lines, start=1):
        if not ln.strip():
            continue
        up = ln.strip().upper()
        if any(up.startswith(h) for h in HEADER_MARKERS):
            continue
        m = MAIN_RE.match(ln)
        if m:
            current = RplRecord(line_no=idx, raw_lines=[ln.rstrip()])
            records.append(current)
            _fill_main(current, m, ln, rc, dc, oc)
            continue
        if LOOSE_MAIN_RE.match(ln):
            # Parece início de registro, mas não bate com o formato: preserva e sinaliza.
            current = RplRecord(line_no=idx, raw_lines=[ln.rstrip()])
            current.add("error", "formato", "Linha de registro fora do formato esperado; não interpretada.")
            records.append(current)
            continue
        if current is not None and ln.startswith(" " * min(rc, 20)):
            current.raw_lines.append(ln.rstrip())
            route_part = ln[rc:dc].strip() if len(ln) > rc else ""
            obs_part = ln[oc:].strip() if len(ln) > oc else ""
            mid = ln[dc:oc].strip() if len(ln) > dc else ""
            if route_part:
                current.route = (current.route + " " + route_part).strip()
            if obs_part:
                current.remarks = (current.remarks + " " + obs_part).strip()
            if mid:
                current.add("warning", "continuacao", f"Texto inesperado na coluna DEST em continuação: '{mid}'.")
            continue
        file_issues.append(Issue("warning", "linha_ignorada", f"Linha {idx} não reconhecida: '{ln.strip()[:80]}'"))

    for r in records:
        if r.remarks:
            r.remarks_dict = parse_remarks(r.remarks)

    if not airline:
        prefixes = [r.ident[:3] for r in records if r.ident]
        if prefixes:
            airline = max(set(prefixes), key=prefixes.count)
            file_issues.append(Issue("warning", "cia_inferida",
                                     f"Linha 'CIA:' não encontrada; companhia inferida pelos identificativos: {airline}."))
        else:
            file_issues.append(Issue("error", "cia_ausente", "Não foi possível identificar a companhia."))

    _cross_validate(records, airline, header_validity_date)
    if not records:
        file_issues.append(Issue("error", "vazio", "Nenhum registro de voo encontrado no arquivo."))

    return RplParseResult(airline, header_validity, header_validity_date, encoding, sha, layout,
                          records, file_issues)


def _fill_main(r: RplRecord, m: re.Match, ln: str, rc: int, dc: int, oc: int):
    g = m.groupdict()
    r.ident = g["ident"]
    r.airline = g["ident"][:3]
    r.flight_number = g["ident"][3:]
    vf, vt = parse_ddmmyy(g["vf"]), parse_ddmmyy(g["vt"])
    r.valid_from = vf.isoformat() if vf else None
    r.valid_to = vt.isoformat() if vt else None
    if not vf:
        r.add("error", "data_inicio", f"Data 'válido desde' inválida: {g['vf']}")
    if not vt:
        r.add("error", "data_fim", f"Data 'válido até' inválida: {g['vt']}")
    if vf and vt and vt < vf:
        r.add("error", "periodo", "Período de validade invertido (fim antes do início).")
    r.days = g["days"]
    bad = [i for i, ch in enumerate(r.days) if ch not in ("0", str(i + 1))]
    if bad:
        r.add("error", "dias", f"Campo DIAS OP inconsistente ('{r.days}'): posição de dia com valor inesperado.")
    if r.days == "0000000":
        r.add("error", "sem_dias", "Nenhum dia de operação informado.")
    if vf and vt and not bad and r.days != "0000000" and vt >= vf:
        ops = [d for d in _daterange(vf, vt) if r.days[d.weekday()] != "0"]
        if not ops:
            r.add("warning", "sem_ocorrencia", "Nenhum dia de operação cai dentro do período de validade.")
    r.type_icao, r.wake = g["type"], g["wake"]
    r.adep, r.eobt = g["adep"], g["eobt"]
    if hhmm_to_min(r.eobt) is None or int(r.eobt[:2]) > 23:
        r.add("error", "eobt", f"EOBT inválido: {r.eobt}")
    r.speed, r.fl = g["speed"], g["fl"]

    r.route = ln[rc:dc].strip() if len(ln) > rc else ""
    dest_tok = ln[dc:oc].strip() if len(ln) > dc else ""
    dm = DEST_RE.match(dest_tok)
    if not dm:
        # Coluna deslocada? Procura o primeiro token AAAA9999 depois da rota.
        sm = DEST_SEARCH_RE.search(ln, m.end())
        if sm:
            dm = sm
            r.add("warning", "coluna_deslocada", "DEST/EET fora da coluna esperada; localizado por busca.")
            r.route = ln[m.end():sm.start()].strip()
    if dm:
        r.ades, r.eet = dm.group("ades"), dm.group("eet")
        r.eet_min = hhmm_to_min(r.eet)
        if r.eet_min is None or r.eet_min == 0:
            r.add("error", "eet", f"EET inválido: {r.eet}")
            r.eet_min = None
        elif r.eet_min > 16 * 60:
            r.add("warning", "eet_longo", f"EET muito longo ({r.eet}); confira.")
    else:
        r.add("error", "dest", "Aeródromo de destino/EET não encontrado.")
    r.remarks = ln[oc:].strip() if len(ln) > oc else ""
    if r.adep and r.ades and r.adep == r.ades:
        r.add("error", "mesmo_aerodromo", "Origem e destino iguais.")


def _daterange(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def _cross_validate(records: list[RplRecord], airline: str | None, header_date: str | None):
    seen: dict[tuple, int] = {}
    by_key: dict[tuple, list[RplRecord]] = {}
    for r in records:
        if not r.ident:
            continue
        if airline and r.airline != airline:
            r.add("warning", "cia_divergente",
                  f"Identificativo {r.ident} não pertence à companhia do arquivo ({airline}).")
        if header_date and r.valid_to and r.valid_to < header_date:
            r.add("warning", "vencido", "Validade termina antes do início de validade do arquivo.")
        k = (r.ident, r.adep, r.ades, r.valid_from, r.valid_to, r.days, r.eobt, r.type_icao)
        if k in seen:
            r.add("warning", "duplicado", f"Registro duplicado (igual ao da linha {seen[k]}).")
        else:
            seen[k] = r.line_no
        by_key.setdefault((r.ident, r.adep, r.ades), []).append(r)

    # Ambiguidade: mesmo voo e mesmo trecho no mesmo dia com horários/tipos diferentes.
    # (mesmo número com origens diferentes no dia é normal: voo com escalas.)
    for _key, lst in by_key.items():
        if len(lst) < 2:
            continue
        occ: dict[str, list[RplRecord]] = {}
        for r in lst:
            if r.status == "error" or not r.valid_from or not r.valid_to:
                continue
            for d in _daterange(date.fromisoformat(r.valid_from), date.fromisoformat(r.valid_to)):
                if r.days[d.weekday()] != "0":
                    occ.setdefault(d.isoformat(), []).append(r)
        for d, rs in occ.items():
            uniq = {(x.eobt, x.type_icao) for x in rs}
            if len(rs) > 1 and len(uniq) > 1:
                for x in rs:
                    msg = f"Ambiguidade em {d}: {len(rs)} registros do mesmo voo e trecho com horário/tipo diferentes."
                    if not any(i.code == "ambiguo" and i.message == msg for i in x.issues):
                        x.add("warning", "ambiguo", msg)
