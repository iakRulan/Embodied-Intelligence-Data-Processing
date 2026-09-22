"""Author the reviewer PDF from Markdown. Requires reportlab (documentation only)."""
from __future__ import annotations

import argparse
import html
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--font-regular", default="C:/Windows/Fonts/simsun.ttc")
    ap.add_argument("--font-bold", default="C:/Windows/Fonts/simhei.ttf")
    a = ap.parse_args()
    output = Path(a.output).resolve()
    if output.exists():
        raise ValueError("PDF already exists; use a new output path")
    output.parent.mkdir(parents=True, exist_ok=True)
    pdfmetrics.registerFont(TTFont("CJK", a.font_regular, subfontIndex=0))
    pdfmetrics.registerFont(TTFont("CJK-Bold", a.font_bold))
    navy, teal = colors.HexColor("#102A43"), colors.HexColor("#087F8C")
    body = ParagraphStyle("Body", fontName="CJK", fontSize=10.2, leading=15.8,
                          textColor=navy, wordWrap="CJK", spaceAfter=8, splitLongWords=True)
    h1 = ParagraphStyle("Title", parent=body, fontName="CJK-Bold", fontSize=25, leading=31, spaceAfter=13)
    h2 = ParagraphStyle("Section", parent=body, fontName="CJK-Bold", fontSize=18, leading=25, spaceAfter=15)
    h3 = ParagraphStyle("Subsection", parent=body, fontName="CJK-Bold", fontSize=12, leading=18,
                        spaceBefore=6, spaceAfter=8, textColor=teal, keepWithNext=True)
    cell = ParagraphStyle("Cell", parent=body, fontSize=9.2, leading=13.2, spaceAfter=0)
    head = ParagraphStyle("Head", parent=cell, fontName="CJK-Bold", textColor=colors.white)
    code = ParagraphStyle("Code", parent=body, fontName="Courier", fontSize=8.3, leading=12.5,
                          backColor=colors.HexColor("#EFF6F8"), borderPadding=8, spaceAfter=8)
    width = A4[0] - 96

    def text(s):
        s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
        s = s.replace("`", "").replace("**", "")
        return html.escape(s)

    def p(s, style=body):
        return Paragraph(text(s), style)

    lines = Path(a.input).read_text(encoding="utf-8-sig").splitlines()
    story, i = [], 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line == "<!-- pagebreak -->":
            story.append(PageBreak())
        elif line.startswith("# "):
            story.append(p(line[2:], h1))
        elif line.startswith("## "):
            story.append(p(line[3:], h2))
        elif line.startswith("### "):
            story.append(p(line[4:], h3))
        elif line.startswith("```"):
            block = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(html.escape(lines[i]))
                i += 1
            story.append(Paragraph("<br/>".join(block), code))
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [s.strip() for s in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r"[-: ]+", s) for s in cells):
                    rows.append(cells)
                i += 1
            cols = len(rows[0])
            widths = ([width * .29, width * .71] if cols == 2 else
                      [width * .21, width * .45, width * .34])
            data = [[p(s, head if ri == 0 else cell) for s in row] for ri, row in enumerate(rows)]
            table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), navy),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#F0F6F8"), colors.white]),
                ("LINEBELOW", (0, 0), (-1, 0), 1, teal),
                ("LINEBELOW", (0, 1), (-1, -1), .3, colors.HexColor("#DCE7EB")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            story += [table, Spacer(1, 10)]
            continue
        else:
            story.append(p(line))
        i += 1

    def decorate(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(teal)
        canvas.setLineWidth(1.2)
        canvas.line(48, A4[1] - 35, A4[0] - 48, A4[1] - 35)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(navy)
        canvas.drawString(48, A4[1] - 27, "MoMo_Lab  /  RefSync-QA v3.2  /  Algorithm Description")
        canvas.drawString(48, 25, "2026-09-22  |  Local validation; platform acceptance pending")
        canvas.drawRightString(A4[0] - 48, 25, f"{doc.page:02d}")
        canvas.restoreState()

    doc = SimpleDocTemplate(str(output), pagesize=A4, leftMargin=48, rightMargin=48,
                            topMargin=54, bottomMargin=45,
                            title="RefSync-QA v3.2 - Algorithm Description", author="MoMo_Lab")
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    print(output)


if __name__ == "__main__":
    main()
