"""LOADSHEET em PDF (reportlab). Sai com a marca de simulação no topo e no rodapé de cada página."""
from __future__ import annotations

import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

LARANJA = colors.HexColor("#C2410C")
CINZA = colors.HexColor("#4B5563")
LINHA = colors.HexColor("#D1D5DB")
VERMELHO = colors.HexColor("#B91C1C")


def _kg(v):
    return "—" if v is None else f"{v:,.0f}".replace(",", ".") + " kg"


def _hdr(canvas, doc, titulo: str):
    canvas.saveState()
    canvas.setFont("Helvetica-Bold", 8)
    canvas.setFillColor(LARANJA)
    canvas.drawString(18 * mm, A4[1] - 12 * mm, "SIMULAÇÃO — NÃO É DOCUMENTO OPERACIONAL")
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(CINZA)
    canvas.drawRightString(A4[0] - 18 * mm, A4[1] - 12 * mm, titulo)
    canvas.setStrokeColor(LINHA)
    canvas.line(18 * mm, A4[1] - 14 * mm, A4[0] - 18 * mm, A4[1] - 14 * mm)
    canvas.line(18 * mm, 16 * mm, A4[0] - 18 * mm, 16 * mm)
    canvas.setFont("Helvetica", 7)
    canvas.drawString(18 * mm, 11 * mm, "Gerado pela LineOps — projeto pessoal de simulação, sem afiliação com companhias aéreas.")
    canvas.drawRightString(A4[0] - 18 * mm, 11 * mm, f"página {doc.page}")
    canvas.restoreState()


def build(ls: dict) -> bytes:
    """Recebe a linha do loadsheet (com data) e devolve o PDF em bytes."""
    d = ls["data"] if "data" in ls else ls
    f = d["flight"]
    buf = io.BytesIO()
    titulo = f"{f.get('flight_number') or 'Voo'} {f['adep']}–{f['ades']} · edição {d.get('edition', 1)}"
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=20 * mm, bottomMargin=20 * mm, title=f"LOADSHEET {titulo}", author="LineOps")
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=17, spaceAfter=2, alignment=0, textColor=colors.black)
    sub = ParagraphStyle("sub", parent=ss["Normal"], fontSize=9.5, textColor=CINZA, spaceAfter=10)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=11, spaceBefore=10, spaceAfter=4, textColor=colors.black)
    pequeno = ParagraphStyle("p", parent=ss["Normal"], fontSize=8, textColor=CINZA, leading=11)

    story = [Paragraph("LOADSHEET", h1),
             Paragraph(f"{f.get('flight_number') or 'Voo sem número'} &nbsp;·&nbsp; {f['adep']} → {f['ades']} &nbsp;·&nbsp; "
                       f"{(f.get('dep_utc') or '')[:10]} &nbsp;·&nbsp; {f.get('registration') or 'sem matrícula'} "
                       f"({f.get('type_icao') or '—'})", sub)]

    linhas = [["", "Peso", "Limite", "Margem", "Fonte"]]
    estilo = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
              ("TEXTCOLOR", (0, 0), (-1, 0), CINZA), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
              ("FONT", (0, 1), (-1, -1), "Helvetica", 9), ("ALIGN", (1, 0), (3, -1), "RIGHT"),
              ("TEXTCOLOR", (4, 1), (4, -1), CINZA), ("FONT", (4, 1), (4, -1), "Helvetica", 7),
              ("LEFTPADDING", (4, 0), (4, -1), 4), ("RIGHTPADDING", (4, 0), (4, -1), 3),
              ("GRID", (0, 0), (-1, -1), 0.4, LINHA), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
              ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
    for i, l in enumerate(d["lines"], start=1):
        destaque = l["label"].startswith(("Peso zero", "Peso de"))
        linhas.append([l["label"], _kg(l.get("value")), _kg(l.get("limit")) if l.get("limit") else "—",
                       _kg(l.get("margin")) if l.get("margin") is not None else "—", l.get("source") or "—"])
        if destaque:
            estilo += [("FONT", (0, i), (3, i), "Helvetica-Bold", 9.5), ("BACKGROUND", (0, i), (-1, i), colors.HexColor("#FAFAFA"))]
        if l.get("over"):
            estilo += [("TEXTCOLOR", (0, i), (3, i), VERMELHO), ("FONT", (0, i), (3, i), "Helvetica-Bold", 9.5)]
    t = Table(linhas, colWidths=[58 * mm, 26 * mm, 25 * mm, 25 * mm, 40 * mm], repeatRows=1)
    t.setStyle(TableStyle(estilo))
    story.append(t)

    cg = d.get("cg") or {}
    story.append(Paragraph("Centro de gravidade", h2))
    story.append(Paragraph(cg.get("reason") or "—" if not cg.get("available") else str(cg), pequeno))

    if d.get("warnings"):
        story.append(Paragraph("Observações", h2))
        for w in d["warnings"]:
            cor = VERMELHO if w["level"] == "error" else CINZA
            story.append(Paragraph(f'<font color="{cor.hexval()[2:]}">• {w["message"]}</font>', pequeno))
    if ls.get("notes"):
        story.append(Paragraph("Justificativa", h2))
        story.append(Paragraph(ls["notes"], pequeno))

    story.append(Spacer(1, 8))
    emitido = ls.get("issued_at") or d.get("computed_at") or datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    story.append(Paragraph(f"Emitido em {emitido[:16].replace('T', ' ')}Z"
                           + (f" · a partir do OFP do SimBrief gerado em {d['ofp_generated_at'][:16].replace('T', ' ')}Z"
                              if d.get("ofp_generated_at") else " · sem OFP importado")
                           + ". Pesos em quilos. Cada número traz a sua fonte; o que não existe aparece como travessão.", pequeno))
    doc.build(story, onFirstPage=lambda c, dd: _hdr(c, dd, titulo), onLaterPages=lambda c, dd: _hdr(c, dd, titulo))
    return buf.getvalue()
