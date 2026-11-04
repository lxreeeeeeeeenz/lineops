"""Contracheque simulado em PDF (reportlab), com a marca de simulação em todas as páginas."""
from __future__ import annotations

import io
from xml.sax.saxutils import escape as E

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .payroll import brl, hhmm

LARANJA = colors.HexColor("#C2410C")
CINZA = colors.HexColor("#4B5563")
LINHA = colors.HexColor("#D1D5DB")
FUNDO = colors.HexColor("#F3F4F6")
MES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]


def _hdr(canvas, doc, titulo: str):
    canvas.saveState()
    canvas.setFont("Helvetica-Bold", 8)
    canvas.setFillColor(LARANJA)
    canvas.drawString(18 * mm, A4[1] - 12 * mm, "SIMULAÇÃO — NÃO É CONTRACHEQUE REAL")
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


def _v(x):
    return brl(x).replace("R$ ", "")


def build(d: dict, closing: dict | None = None, me_name: str | None = None) -> bytes:
    y, m = d["month"].split("-")
    comp = f"{MES[int(m) - 1]} de {y}"
    ct = d["contract"]
    titulo = f"Contracheque · {comp} · {'fechado' if closing else d['basis']}"
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=20 * mm, bottomMargin=20 * mm,
                            title=titulo, author="LineOps")
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=17, spaceAfter=2, alignment=0)
    sub = ParagraphStyle("sub", parent=ss["Normal"], fontSize=9.5, textColor=CINZA, spaceAfter=10)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=11, spaceBefore=10, spaceAfter=4)
    small = ParagraphStyle("p", parent=ss["Normal"], fontSize=8, textColor=CINZA, leading=11)
    cell = ParagraphStyle("c", parent=ss["Normal"], fontSize=8.5, leading=10.5)

    base = (d.get("base") or {}).get("icao")
    story = [Paragraph("Contracheque simulado", h1),
             Paragraph(f"{E(me_name or '')} &nbsp;·&nbsp; {ct['role']} &nbsp;·&nbsp; {ct['category_label']}"
                       f"{' &nbsp;·&nbsp; base ' + base if base else ''} &nbsp;·&nbsp; competência {comp} &nbsp;·&nbsp; "
                       f"{'fechado em ' + closing['closed_at'][:10] if closing else 'base ' + d['basis']}", sub)]

    linhas = [["Cód.", "Descrição", "Referência", "Proventos", "Descontos"]]
    for l in d["lines"]:
        if l["kind"] == "info":
            continue
        desc = l["label"] + ("" if l["taxable"] or l["kind"] == "desconto" else " (não tributável)")
        linhas.append([l["code"], Paragraph(E(desc), cell), l["ref"] or "",
                       _v(l["amount"]) if l["kind"] == "provento" else "", _v(l["amount"]) if l["kind"] == "desconto" else ""])
    t = d["totals"]
    linhas.append(["", "Totais", "", _v(t["gross"]), _v(t["deductions"])])
    linhas.append(["", "Líquido", "", _v(t["net"]), ""])
    n = len(linhas)
    tb = Table(linhas, colWidths=[13 * mm, 78 * mm, 27 * mm, 28 * mm, 28 * mm], repeatRows=1)
    tb.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), FUNDO), ("TEXTCOLOR", (0, 0), (-1, 0), CINZA), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 9), ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.4, LINHA), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONT", (0, n - 2), (-1, -1), "Helvetica-Bold", 9.5), ("BACKGROUND", (0, n - 2), (-1, -1), colors.HexColor("#FAFAFA")),
        ("TEXTCOLOR", (3, n - 1), (3, n - 1), LARANJA),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story.append(tb)

    h = d["hours"]
    story.append(Paragraph("Horas do mês", h2))
    hl = [["Voo (calço a calço)", hhmm(h["flight_min"]), "Reserva", f"{len(h['reserve_days'])} dia(s) · {hhmm(h['reserve_min'])}"],
          ["Sobreaviso", hhmm(h["standby_min"]), "Equivalentes", hhmm(h["equiv_min"])],
          ["Garantia", hhmm(h["guarantee_min"]), "Excedente", hhmm(h["excess_min"])],
          ["Domingos e feriados", hhmm(h["sunday_holiday_min"]), "Noturno (22–05)", hhmm(h["night_min"])]]
    th = Table(hl, colWidths=[42 * mm, 45 * mm, 42 * mm, 45 * mm])
    th.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 8.5), ("TEXTCOLOR", (0, 0), (0, -1), CINZA),
                            ("TEXTCOLOR", (2, 0), (2, -1), CINZA), ("GRID", (0, 0), (-1, -1), 0.4, LINHA),
                            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story.append(th)
    story.append(Spacer(1, 6))

    if t.get("foreign_meals"):
        story.append(Paragraph("Diárias internacionais (moeda da CCT, sem conversão): "
                               + ", ".join(f"{x['currency']} {_v(x['value'])}" for x in t["foreign_meals"]), small))
    if t.get("food_voucher"):
        story.append(Paragraph(f"Benefício fora do contracheque: vale-alimentação {brl(t['food_voucher'])}.", small))

    story.append(Paragraph("Como cada linha foi calculada", h2))
    for l in d["lines"]:
        if l.get("detail") or l.get("source"):
            story.append(Paragraph(f"<b>{l['code']} {E(l['label'])}</b>: {E(l.get('detail') or '')}"
                                   f"{' Fonte: ' + E(l['source']) + '.' if l.get('source') else ''}", small))
    if d.get("warnings"):
        story.append(Paragraph("Observações", h2))
        for w in d["warnings"]:
            story.append(Paragraph("• " + E(w["message"]), small))
    story.append(Spacer(1, 8))
    story.append(Paragraph(f"{d['source']['basis_note']} Valores padrão: {d['source']['cct']['label']} "
                           f"(vigência {d['source']['cct']['valid_from'][8:]}/{d['source']['cct']['valid_from'][5:7]}/{d['source']['cct']['valid_from'][:4]} "
                           f"a {d['source']['cct']['valid_to'][8:]}/{d['source']['cct']['valid_to'][5:7]}/{d['source']['cct']['valid_to'][:4]}). "
                           "Estimativa de simulação: o seu acordo coletivo e o RH da empresa podem calcular diferente.", small))
    if closing and closing.get("notes"):
        story.append(Paragraph("Observação do fechamento: " + E(closing["notes"]), small))
    doc.build(story, onFirstPage=lambda c, dd: _hdr(c, dd, titulo), onLaterPages=lambda c, dd: _hdr(c, dd, titulo))
    return buf.getvalue()
