"""Biblioteca técnica: os documentos (PDF) que VOCÊ coloca na LineOps, indexados para busca, e a MEL lida item a item.

Nada vem junto com a LineOps: as MEL/CDL/QRH são documentos da companhia, protegidos. Você importa o seu PDF (Biblioteca ›
Importar) e ele fica só no seu banco da carreira (dados/carreiras/XXX.db), página a página, com busca de texto. Os PDF de uma
companhia não aparecem nas outras.

MEL (formato AMOS/GOL, "Minimum Equipment List"): cada item vira um registro com código ATA (21-10-02-03), título, categoria
de reparo (A/B/C/D), instalados, exigidos para despacho, observações/condições e os procedimentos (M)/(O) com o placard.
Os prazos por categoria seguem a definição 22 da própria MEL (B: 3 dias; C: 10; D: 120; A: o que a observação disser), contados
a partir do dia seguinte ao registro. O que não é identificado com segurança fica fora (e o PDF continua pesquisável por página).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, timedelta

from .db import jloads, now_iso, one, rows, tx
from .errors import FGVError, NotFound

log = logging.getLogger("lineops.biblioteca")
KINDS = ("MEL", "CDL", "QRH", "FCOM", "OUTRO")
CAT_DAYS = {"B": 3, "C": 10, "D": 120}                 # definição 22 da MEL (A: prazo na observação do item)
MAX_PDF_MB = 80

_ROW = re.compile(r"^(?P<ind>\s{0,10})(?P<seq>\d{2}(?:-(?:\d{2}|[A-Z]))*|[A-Z])\s{2,}(?P<rest>\S.*)$")
_COLS = re.compile(r"\s(?P<cat>[ABCD])\s+(?P<inst>\d{1,3}|-)\s+(?P<req>\d{1,3}|-)(?:\s+(?P<rem>.*))?$")
_ATA = re.compile(r"\bATA\s+(\d{2})\b")
_PROC = re.compile(r"^(?P<code>\d{2}-\d{2}(?:-(?:\d{2}|[A-Z])){0,3})\s+(?P<title>\S.*)?$")
_SECTION = re.compile(r"\(Continued\)\s*([A-Z][A-Z &/,\-()]+)\s*$")
_SECTION2 = re.compile(r"^\s*([A-Z][A-Z &/,\-()]+?)\s{2,}\(Continued\)Page \d+")
_NOISE = re.compile(r"Minimum Equipment List|Configuration Deviation List|^\s*Page \d+|\(Continued\)|^\s*(System &|Sequence|numbers)\s*$"
                    r"|Item \(1\)|Number Installed|Number Required for Dispatch|Remarks or Exceptions|^\s*\d{1,2}\.\w+ \d{4}\s*$")
_FOOT = re.compile(r"\s{3,}\d{1,2}\.\w+ \d{4}\s*$")
_REMARKISH = re.compile(r"^(\(P\)|\(M\)|\(O\)|May |One |Two |Any |NOTE|a\)|b\)|c\)|[a-z]\. )")


# ------------------------------------------------------------------ leitura do PDF
def _reader(path):
    try:
        import pypdf
    except ImportError as e:                                   # pragma: no cover
        raise FGVError("Para importar PDF é preciso o pacote pypdf (pip install pypdf).", code="dependencia") from e
    try:
        return pypdf.PdfReader(str(path))
    except Exception as e:                                     # noqa: BLE001
        raise FGVError(f"Não consegui abrir o PDF: {e}", code="pdf_invalido") from e


def page_texts(path, progress=None):
    """Texto de cada página, preservando colunas (modo layout)."""
    r = _reader(path)
    n = len(r.pages)
    out = []
    for i, p in enumerate(r.pages):
        try:
            t = p.extract_text(extraction_mode="layout") or ""
        except Exception:                                      # noqa: BLE001 — página sem texto (imagem) ou corrompida
            t = ""
        out.append(t.replace("\xa0", " "))
        if progress and (i % 10 == 0 or i == n - 1):
            progress(i + 1, n)
    return out


def sniff(texts: list[str], file_name: str) -> dict:
    """Título, tipo, frota e revisão lidos da capa (o que não está lá fica em branco)."""
    head = "\n".join(t for t in texts[:8] if "Minimum Equipment List" in t or "Configuration Deviation" in t or "MEL" in t or "CDL" in t or "QRH" in t)[:6000] or "\n".join(texts[:3])
    kind = "OUTRO"
    if re.search(r"MINIMUM EQUIPMENT LIST|\bMEL\b", head, re.I) and not re.search(r"CONFIGURATION DEVIATION", head, re.I):
        kind = "MEL"
    elif re.search(r"CONFIGURATION DEVIATION LIST|\bCDL\b", head, re.I):
        kind = "CDL"
    elif re.search(r"QUICK REFERENCE HANDBOOK|\bQRH\b", head, re.I):
        kind = "QRH"
    elif re.search(r"FCOM|FLIGHT CREW OPERATIONS MANUAL", head, re.I):
        kind = "FCOM"
    title = None
    for ln in head.splitlines():
        s = ln.strip()
        if re.match(r"^(MEL|CDL|QRH|FCOM)\b.*\d", s, re.I) or re.search(r"B\s?737|A3[12]\d|E1[79]\d|ATR", s):
            title = s
            break
    rev = re.search(r"Revision\s+(\d+[a-z]?)\s*\(([^)]+)\)", head, re.I)
    fleet = re.search(r"(737-?\s?(?:MAX|NG|700|800|8)[^\n,]*|A3(?:19|20|21)\s?(?:neo|ceo)?[^\n,]*|E\s?19[05][^\n,]*)", head, re.I)
    return {"kind": kind, "title": title or re.sub(r"\.pdf$", "", file_name, flags=re.I),
            "revision": rev.group(1) if rev else None, "rev_date": rev.group(2) if rev else None,
            "fleet": fleet.group(1).strip()[:40] if fleet else None}


# ------------------------------------------------------------------ MEL: item a item
def _level(ind: str) -> int:
    n = len(ind)
    return 1 if n <= 1 else 2 if n <= 3 else 3


def parse_mel(texts: list[str]) -> list[dict]:
    """Itens da MEL a partir do texto em layout. Devolve [{code, ata, level, parent, title, category, installed, required,
    remarks, page, maint, oper, placard, section}] na ordem do documento."""
    items: dict[str, dict] = {}
    order: list[str] = []
    ata, section = None, None
    stack: dict[int, str] = {}
    cur = None                 # item sendo lido (linhas de continuação do título/observações)
    rem_col = None             # coluna absoluta onde começam as observações, medida na linha do item
    proc_code, proc_kind = None, None

    def new(code, lvl, parent, title, pno):
        it = {"code": code, "ata": ata, "level": lvl, "parent": parent, "title": title, "category": None, "installed": None,
              "required": None, "remarks": "", "page": pno, "maint": "", "oper": "", "placard": "", "section": section}
        items[code] = it
        order.append(code)
        return it

    for pno, text in enumerate(texts, 1):
        lines = text.replace("\xa0", " ").splitlines()
        head = "\n".join(lines[:3])
        m = _ATA.search(head)
        if m:
            ata = m.group(1)
        for ln in lines[:3]:
            ms = _SECTION.search(ln) or _SECTION2.match(ln)
            if ms:
                section = ms.group(1).strip()
        if "Number Required for Dispatch" in text:
            proc_code = None                               # a tabela recomeça nesta página
        if not ata or ata == "00":
            continue
        for ln in lines:
            ln = _FOOT.sub("", ln.rstrip())
            if not ln.strip() or _NOISE.search(ln):
                continue
            mp = _PROC.match(ln)
            if mp and mp.group("code").startswith(ata + "-"):
                code = mp.group("code")
                proc_code, proc_kind = code, None
                cur = None
                if code not in items:
                    parts = code.split("-")
                    new(code, len(parts) - 1, "-".join(parts[:-1]) if len(parts) > 2 else None, (mp.group("title") or "").strip(), pno)
                continue
            if proc_code:
                s = ln.strip()
                if s.startswith("Maintenance Procedures"):
                    proc_kind = "maint"
                    continue
                if s.startswith("Operating Procedures") or s.startswith("Operations Procedures"):
                    proc_kind = "oper"
                    continue
                if re.match(r"^\s*PLACARD\s{2,}", ln) and "LOCATION" in ln:
                    proc_kind = proc_kind or "maint"
                    continue
                mr = _ROW.match(ln)
                if mr and _COLS.search(mr.group("rest")) and len(mr.group("ind")) <= 8:
                    proc_code = None                       # voltou a tabela
                else:
                    it = items[proc_code]
                    if proc_kind in ("maint", "oper"):
                        if re.match(r"^(INOP|NOT APPLICABLE|N/A)\b", s) and not it["placard"]:
                            it["placard"] = re.sub(r"\s{2,}", " · ", s)
                        it[proc_kind] += re.sub(r"\s{2,}", "  ", s) + "\n"
                    continue
            mr = _ROW.match(ln)
            if mr:
                ind, seq, rest = mr.group("ind"), mr.group("seq"), mr.group("rest")
                mc = _COLS.search(rest)
                if len(ind) <= 1 and seq == ata and not mc:
                    section = rest.strip()                 # "21  AIR CONDITIONING": cabeçalho do capítulo
                    continue
                if "-" in seq:                             # formato "45-01": o código inteiro vem na linha (MEL do MAX)
                    code = f"{ata}-{seq}"
                    parts = code.split("-")
                    lvl, parent = len(parts) - 1, "-".join(parts[:-1])
                    stack = {k: "-".join(parts[:k + 1]) for k in range(1, lvl + 1)}
                else:
                    lvl = _level(ind)
                    if seq.isalpha():                      # sub-item com letra (21-45-03-A): um nível abaixo do último número
                        nums = [k for k, v in stack.items() if v.rsplit("-", 1)[-1].isdigit()]
                        lvl = max(nums) + 1 if nums else 1
                    parent = stack.get(lvl - 1) if lvl > 1 else None
                    if lvl > 1 and not parent:
                        continue
                    code = f"{ata}-{seq}" if lvl == 1 else f"{parent}-{seq}"
                    stack[lvl] = code
                    for k in list(stack):
                        if k > lvl:
                            del stack[k]
                title = rest[:mc.start()].strip() if mc else rest.strip()
                it = items.get(code) or new(code, lvl, parent, title, pno)
                if title and not it["title"]:
                    it["title"] = title
                if mc:
                    it["category"] = mc.group("cat")
                    it["installed"] = None if mc.group("inst") == "-" else int(mc.group("inst"))
                    it["required"] = None if mc.group("req") == "-" else int(mc.group("req"))
                    rem = (mc.group("rem") or "").strip()
                    if rem:
                        it["remarks"] += rem + "\n"
                    rem_col = mr.start("rest") + (mc.start("rem") if mc.group("rem") else mc.end("req") + 1)
                else:
                    rem_col = None
                cur = it
                continue
            if cur:
                # a mesma linha pode ter a continuação do título (esquerda) e da observação (direita): corta na coluna
                cut = rem_col - 6 if rem_col is not None else 60
                left, right = ln[:cut].strip(), ln[cut:].strip()
                if left and _REMARKISH.match(left) and not right:
                    right, left = left, ""
                if right:
                    cur["remarks"] += right + "\n"
                if left and len(cur["title"]) < 160:
                    cur["title"] = (cur["title"] + " " + left).strip()
    out = []
    for code in order:
        it = items[code]
        for k in ("remarks", "maint", "oper"):
            it[k] = it[k].strip()
        it["title"] = re.sub(r"\s+", " ", it["title"]).strip()
        out.append(it)
    return out


# ------------------------------------------------------------------ banco
def _fts(conn) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS library_fts USING fts5(text, content='library_page', content_rowid='id', tokenize='unicode61')")
        return True
    except Exception:                                          # noqa: BLE001 — SQLite sem FTS5: busca por LIKE
        return False


def docs(conn, airline: str) -> list[dict]:
    out = []
    for d in rows(conn.execute("SELECT * FROM library_doc WHERE airline=? ORDER BY kind, title", (airline,))):
        d["items"] = one(conn.execute("SELECT COUNT(*) AS n FROM mel_item WHERE doc_id=?", (d["id"],)))["n"]
        out.append(d)
    return out


def import_pdf(conn, airline: str, path, file_name: str, *, kind: str | None = None, title: str | None = None, progress=None,
               user_id=None) -> dict:
    """Lê o PDF, guarda as páginas (busca) e, se for MEL, os itens. O arquivo não é copiado: só o texto."""
    from pathlib import Path
    p = Path(path)
    if p.stat().st_size > MAX_PDF_MB * 1024 * 1024:
        raise FGVError(f"PDF maior que {MAX_PDF_MB} MB.", code="pdf_grande")
    sha = hashlib.sha1(p.read_bytes()).hexdigest()
    dup = one(conn.execute("SELECT id, title FROM library_doc WHERE airline=? AND sha1=?", (airline, sha)))
    if dup:
        raise FGVError(f"Este PDF já está na biblioteca: {dup['title']}.", code="duplicado")
    texts = page_texts(p, progress)
    meta = sniff(texts, file_name)
    kind = (kind or meta["kind"]).upper()
    if kind not in KINDS:
        raise FGVError("Tipo: MEL, CDL, QRH, FCOM ou OUTRO.", code="campo_invalido")
    items = parse_mel(texts) if kind in ("MEL", "CDL") else []
    blank = sum(1 for t in texts if len(t.strip()) < 20)
    note = None
    if blank > len(texts) // 2:
        note = f"{blank} de {len(texts)} páginas sem texto (PDF digitalizado): a busca não acha nada nelas."
    if kind == "MEL" and len(items) < 20:
        log.warning("MEL %s: só %d itens reconhecidos", file_name, len(items))
    fts = _fts(conn)
    with tx(conn):
        did = conn.execute("""INSERT INTO library_doc(airline, kind, title, fleet, revision, rev_date, file_name, pages, sha1, imported_at, meta)
                              VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                           (airline, kind, (title or meta["title"]).strip()[:160], meta["fleet"], meta["revision"], meta["rev_date"], file_name,
                            len(texts), sha, now_iso(), json.dumps({"items": len(items)}))).lastrowid
        for i, t in enumerate(texts, 1):
            pid = conn.execute("INSERT INTO library_page(doc_id, page, text) VALUES (?,?,?)", (did, i, t)).lastrowid
            if fts:
                conn.execute("INSERT INTO library_fts(rowid, text) VALUES (?,?)", (pid, t))
        for it in items:
            conn.execute("""INSERT INTO mel_item(doc_id, code, ata, level, parent, section, title, category, installed, required, remarks, maint, oper, placard, page)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (did, it["code"], it["ata"], it["level"], it["parent"], it["section"], it["title"], it["category"], it["installed"],
                          it["required"], it["remarks"], it["maint"], it["oper"], it["placard"], it["page"]))
    return {"id": did, "kind": kind, "title": title or meta["title"], "pages": len(texts), "items": len(items), "revision": meta["revision"],
            "rev_date": meta["rev_date"], "fleet": meta["fleet"], "fts": fts, "note": note}


def delete_doc(conn, airline: str, doc_id: int, user_id=None) -> dict:
    d = one(conn.execute("SELECT * FROM library_doc WHERE id=? AND airline=?", (doc_id, airline)))
    if not d:
        raise NotFound("Documento não encontrado.")
    with tx(conn):
        if _fts(conn):
            conn.execute("DELETE FROM library_fts WHERE rowid IN (SELECT id FROM library_page WHERE doc_id=?)", (doc_id,))
        conn.execute("DELETE FROM library_page WHERE doc_id=?", (doc_id,))
        # a discrepância guarda o código e a categoria; só o vínculo com o item some
        conn.execute("UPDATE discrepancy SET mel_item_id=NULL WHERE mel_item_id IN (SELECT id FROM mel_item WHERE doc_id=?)", (doc_id,))
        conn.execute("DELETE FROM mel_item WHERE doc_id=?", (doc_id,))
        conn.execute("DELETE FROM library_doc WHERE id=?", (doc_id,))
    return {"deleted": d["title"]}


def _snippet(text: str, q: str, width: int = 220) -> str:
    low, ql = text.lower(), q.lower().split()
    pos = min((low.find(w) for w in ql if low.find(w) >= 0), default=-1)
    if pos < 0:
        return re.sub(r"\s+", " ", text[:width]).strip()
    a = max(0, pos - width // 3)
    return ("…" if a else "") + re.sub(r"\s+", " ", text[a:a + width]).strip() + "…"


def search(conn, airline: str, q: str, doc_id: int | None = None, limit: int = 30) -> dict:
    """Busca nas páginas dos documentos da companhia (FTS5; sem FTS5, LIKE)."""
    q = (q or "").strip()
    if len(q) < 2:
        raise FGVError("Digite pelo menos 2 letras.", code="busca_curta")
    out = []
    ids = {d["id"]: d for d in rows(conn.execute("SELECT id, kind, title FROM library_doc WHERE airline=?", (airline,)))}
    if not ids:
        return {"items": [], "note": "Nenhum documento importado nesta companhia."}
    want = [doc_id] if doc_id else list(ids)
    if _fts(conn):
        fq = " ".join(f'"{w}"' for w in re.findall(r"[\w\-/]+", q))
        try:
            rs = rows(conn.execute(f"""SELECT p.id, p.doc_id, p.page, p.text FROM library_fts f JOIN library_page p ON p.id=f.rowid
                                       WHERE library_fts MATCH ? AND p.doc_id IN ({','.join('?' * len(want))}) ORDER BY rank LIMIT ?""",
                                   (fq, *want, limit)))
        except Exception:                                      # noqa: BLE001 — sintaxe que o FTS não aceita
            rs = []
    else:
        rs = rows(conn.execute(f"""SELECT id, doc_id, page, text FROM library_page WHERE doc_id IN ({','.join('?' * len(want))})
                                   AND lower(text) LIKE ? LIMIT ?""", (*want, f"%{q.lower()}%", limit)))
    for r in rs:
        d = ids.get(r["doc_id"])
        if d:
            out.append({"doc_id": r["doc_id"], "doc": d["title"], "kind": d["kind"], "page": r["page"], "snippet": _snippet(r["text"], q)})
    return {"items": out}


def page(conn, airline: str, doc_id: int, pno: int) -> dict:
    d = one(conn.execute("SELECT id, title, kind, pages FROM library_doc WHERE id=? AND airline=?", (doc_id, airline)))
    if not d:
        raise NotFound("Documento não encontrado.")
    p = one(conn.execute("SELECT text FROM library_page WHERE doc_id=? AND page=?", (doc_id, pno)))
    if not p:
        raise NotFound("Página não encontrada.")
    return {"doc": d, "page": pno, "text": p["text"]}


# ------------------------------------------------------------------ MEL
def mel_docs(conn, airline: str) -> list[dict]:
    return rows(conn.execute("SELECT id, kind, title, fleet, revision, rev_date FROM library_doc WHERE airline=? AND kind IN ('MEL','CDL') ORDER BY kind, title", (airline,)))


def mel_search(conn, airline: str, q: str = "", doc_id: int | None = None, ata: str | None = None, limit: int = 60) -> dict:
    q = (q or "").strip()
    sql = """SELECT i.id, i.doc_id, d.title AS doc, d.kind, d.fleet, i.code, i.ata, i.level, i.section, i.title, i.category, i.installed, i.required,
                    substr(i.remarks, 1, 240) AS remarks, i.page, (i.maint<>'') AS has_m, (i.oper<>'') AS has_o
             FROM mel_item i JOIN library_doc d ON d.id=i.doc_id WHERE d.airline=?"""
    args: list = [airline]
    if doc_id:
        sql += " AND i.doc_id=?"
        args.append(doc_id)
    if ata:
        sql += " AND i.ata=?"
        args.append(ata[:2])
    if q:
        if re.match(r"^\d{2}(-\d{2}){0,3}$", q):
            sql += " AND i.code LIKE ?"
            args.append(q + "%")
        else:
            sql += " AND (lower(i.title) LIKE ? OR lower(i.remarks) LIKE ? OR lower(i.section) LIKE ?)"
            args += [f"%{q.lower()}%"] * 3
    sql += " ORDER BY i.doc_id, i.id LIMIT ?"
    args.append(limit)
    return {"items": rows(conn.execute(sql, args)), "atas": rows(conn.execute(
        "SELECT i.ata, MIN(i.section) AS section, COUNT(*) AS n FROM mel_item i JOIN library_doc d ON d.id=i.doc_id WHERE d.airline=? GROUP BY i.ata ORDER BY i.ata", (airline,)))}


def mel_item(conn, airline: str, item_id: int) -> dict:
    it = one(conn.execute("""SELECT i.*, d.title AS doc, d.kind, d.fleet, d.revision, d.rev_date FROM mel_item i JOIN library_doc d ON d.id=i.doc_id
                             WHERE i.id=? AND d.airline=?""", (item_id, airline)))
    if not it:
        raise NotFound("Item da MEL não encontrado.")
    it["children"] = rows(conn.execute("SELECT id, code, title, category, installed, required FROM mel_item WHERE doc_id=? AND parent=? ORDER BY id",
                                       (it["doc_id"], it["code"])))
    it["deadline_days"] = CAT_DAYS.get(it["category"] or "")
    return it


def mel_by_code(conn, airline: str, code: str) -> dict | None:
    return one(conn.execute("""SELECT i.* FROM mel_item i JOIN library_doc d ON d.id=i.doc_id WHERE d.airline=? AND d.kind='MEL' AND i.code=?
                               ORDER BY i.id LIMIT 1""", (airline, code.strip())))


def due_date_for(category: str | None, opened: str) -> str | None:
    """Prazo de reparo pela categoria (definição 22 da MEL): dias corridos a partir da meia-noite do dia do registro."""
    n = CAT_DAYS.get((category or "").upper())
    if not n:
        return None
    return (date.fromisoformat(opened[:10]) + timedelta(days=n)).isoformat()
