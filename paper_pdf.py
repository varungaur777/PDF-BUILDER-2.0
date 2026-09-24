"""
Paper-style PDF: two columns, question → options (a)–(d) → Answer box,
like the answer-key papers shared after exams.
"""

import io
import os
import re

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer, Table,
                                TableStyle, Image, KeepTogether, FrameBreak, NextPageTemplate,
                                PageBreak, CondPageBreak)
from reportlab.lib.fonts import addMapping

RED = colors.HexColor("#B3261E")
BLUE = colors.HexColor("#1F4E9E")
INK = colors.HexColor("#1B1B1B")
GREY = colors.HexColor("#6B6B6B")
BAR_BG = colors.HexColor("#F0F1F3")
LINE = colors.HexColor("#D5D8DD")
GREEN = colors.HexColor("#1E7D34")

LETTERS = "abcdefgh"

_FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSerif-BoldItalic.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSerifCondensed.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSerifCondensed-Bold.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSerifCondensed-Italic.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSerifCondensed-BoldItalic.ttf"),
]


def _register_fonts():
    for reg, bold, ital, bi in _FONT_CANDIDATES:
        if all(os.path.exists(p) for p in (reg, bold, ital, bi)):
            pdfmetrics.registerFont(TTFont("PSerif", reg))
            pdfmetrics.registerFont(TTFont("PSerif-Bold", bold))
            pdfmetrics.registerFont(TTFont("PSerif-Italic", ital))
            pdfmetrics.registerFont(TTFont("PSerif-BoldItalic", bi))
            addMapping("PSerif", 0, 0, "PSerif")
            addMapping("PSerif", 1, 0, "PSerif-Bold")
            addMapping("PSerif", 0, 1, "PSerif-Italic")
            addMapping("PSerif", 1, 1, "PSerif-BoldItalic")
            return "PSerif", "PSerif-Bold"
    return "Times-Roman", "Times-Bold"


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _norm(markup):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", markup or "")).strip().lower()


def split_passage(markup):
    """Return (passage, question) if the text looks like passage + final question."""
    paras = [p for p in (markup or "").split("<br/><br/>") if p.strip()]
    if len(paras) >= 3 and len(_norm(markup)) > 500:
        ask = re.sub(r"^((?:<[^>]+>)*)([a-z])", lambda m: m.group(1) + m.group(2).upper(), paras[-1].strip())
        return "<br/><br/>".join(paras[:-1]), ask
    return None, markup


def build_paper(path, cand, questions, store, ocr, *, show_yours=False, hide_candidate=False,
                watermark="", footer_text="", pos=1.0, neg=0.25):
    """
    questions: list of Question (from ssc_report)
    ocr: dict image_key -> OcrResult
    """
    FONT, FONT_B = _register_fonts()

    page_w, page_h = A4
    M = 13 * mm
    gutter = 7 * mm
    col_w = (page_w - 2 * M - gutter) / 2
    head_h = 36 * mm
    bar_h = 12 * mm
    bottom = M + 6 * mm
    top_y = page_h - M

    Q = ParagraphStyle("Q", fontName=FONT, fontSize=9.4, leading=12.4, textColor=RED)
    PASS = ParagraphStyle("P", fontName=FONT, fontSize=8.8, leading=11.6, textColor=INK)
    OPT = ParagraphStyle("O", fontName=FONT, fontSize=9, leading=12, textColor=INK)
    ANS = ParagraphStyle("A", fontName=FONT_B, fontSize=9, leading=11, textColor=BLUE)
    BAR = ParagraphStyle("BAR", fontName=FONT_B, fontSize=10.5, leading=13, textColor=INK)
    SMALL = ParagraphStyle("S", fontName=FONT, fontSize=8, leading=10, textColor=GREY)

    def img_flow(key, max_w, max_h=110 * mm):
        blob = store.get(key)
        if not blob:
            return None
        w, h = PILImage.open(io.BytesIO(blob)).size
        scale = min(0.62, max_w / w)
        if h * scale > max_h:
            scale = max_h / h
        return Image(io.BytesIO(blob), width=w * scale, height=h * scale, hAlign="LEFT")

    def text_of(key):
        r = ocr.get(key)
        return r.markup if (r and r.ok) else None

    # ---------------- page furniture
    def header_box(canvas):
        x, y, w, h = M, top_y - head_h + 3 * mm, page_w - 2 * M, head_h - 3 * mm
        canvas.setStrokeColor(BLUE); canvas.setLineWidth(1.4)
        canvas.roundRect(x, y, w, h, 4, stroke=1, fill=0)
        canvas.setFillColor(INK)
        canvas.setFont(FONT_B, 13)
        canvas.drawString(x + 6 * mm, y + h - 10 * mm, "Staff Selection Commission")
        canvas.setFont(FONT, 9)
        canvas.drawString(x + 6 * mm, y + h - 15.5 * mm, (cand.exam or "SSC Examination")[:80])
        canvas.setFont(FONT, 8)
        canvas.setFillColor(GREY)
        canvas.drawString(x + 6 * mm, y + h - 20.5 * mm, "Question paper with official answer key")
        rx = x + w - 62 * mm
        canvas.setFillColor(BLUE); canvas.setFont(FONT_B, 11)
        canvas.drawString(rx, y + h - 8.5 * mm, "SSC Online Exam")
        canvas.setFillColor(INK); canvas.setFont(FONT, 8.2)
        rows = [("Date", cand.test_date), ("Shift", cand.shift)]
        if not hide_candidate:
            rows += [("Roll No", cand.roll_no), ("Name", cand.name)]
        yy = y + h - 14 * mm
        for k, v in rows:
            canvas.setFont(FONT, 8.2); canvas.drawString(rx, yy, f"{k}:")
            canvas.setFont(FONT_B, 8.2); canvas.drawString(rx + 14 * mm, yy, (v or "")[:34])
            yy -= 4.3 * mm

    def furniture(canvas, doc, first=False):
        canvas.saveState()
        if first:
            header_box(canvas)
        if watermark:
            # large diagonal channel handle, twice per page (like shared answer-key papers)
            canvas.setFillColor(colors.Color(0.86, 0.2, 0.2, alpha=0.13))
            size = 60
            tw = pdfmetrics.stringWidth(watermark, FONT_B, size)
            size = max(24, min(72, size * (page_w * 0.78) / max(tw, 1)))
            canvas.setFont(FONT_B, size)
            for cx, cy in ((page_w * 0.46, page_h * 0.66), (page_w * 0.56, page_h * 0.24)):
                canvas.saveState()
                canvas.translate(cx, cy)
                canvas.rotate(42)
                canvas.drawCentredString(0, -size / 3, watermark)
                canvas.restoreState()
        canvas.setFillColor(GREY); canvas.setFont(FONT, 7.5)
        foot = footer_text or watermark
        if foot:
            canvas.drawString(M, M, foot)
        canvas.drawRightString(page_w - M, M, f"Page {doc.page}")
        canvas.restoreState()

    def cols(top):
        h = top - bottom
        return [Frame(M, bottom, col_w, h, id="c1", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0),
                Frame(M + col_w + gutter, bottom, col_w, h, id="c2", leftPadding=0, rightPadding=0,
                      topPadding=0, bottomPadding=0)]

    def barframe(top):
        return Frame(M, top - bar_h, page_w - 2 * M, bar_h, id="bar", leftPadding=0, rightPadding=0,
                     topPadding=0, bottomPadding=0)

    first_top = top_y - head_h
    templates = [
        PageTemplate("first", [barframe(first_top)] + cols(first_top - bar_h - 2 * mm),
                     onPage=lambda c, d: furniture(c, d, first=True)),
        PageTemplate("sec", [barframe(top_y)] + cols(top_y - bar_h - 2 * mm), onPage=furniture),
        PageTemplate("body", cols(top_y), onPage=furniture),
    ]
    doc = BaseDocTemplate(path, pagesize=A4, pageTemplates=templates,
                          leftMargin=M, rightMargin=M, topMargin=M, bottomMargin=bottom,
                          title=f"{cand.exam or 'SSC'} – {cand.test_date} {cand.shift}".strip(),
                          author="ssc_report.py")

    def section_bar(name):
        t = Table([[Paragraph(_esc(name.upper()), BAR)]], colWidths=[page_w - 2 * M], rowHeights=[9 * mm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), BAR_BG),
            ("LINEBEFORE", (0, 0), (0, 0), 3.5, BLUE),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        return t

    def options_block(q):
        items = []   # (label, markup or None, img key)
        for o in q.options:
            lab = LETTERS[o.number - 1] if o.number <= len(LETTERS) else str(o.number)
            items.append((lab, text_of(o.img) if o.img else _esc(o.text), o.img))
        if all(t is not None for _, t, _ in items):
            plain_len = max(len(_norm(t)) for _, t, _ in items)
            parts = [f"<font name='{FONT_B}'>({lab})</font> {t}" for lab, t, _ in items]
            if plain_len <= 12 and len(items) <= 4:
                return [Paragraph("&nbsp;&nbsp;&nbsp;".join(parts), OPT)]
            if plain_len <= 22:
                rows, cw = [], col_w / 2
                for i in range(0, len(parts), 2):
                    rows.append([Paragraph(p, OPT) for p in parts[i:i + 2]] + [""] * (2 - len(parts[i:i + 2])))
                t = Table(rows, colWidths=[cw, cw], hAlign="LEFT")
                t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                                       ("TOPPADDING", (0, 0), (-1, -1), 0.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 0.5),
                                       ("VALIGN", (0, 0), (-1, -1), "TOP")]))
                return [t]
            return [Paragraph(p, OPT) for p in parts]
        # at least one option is a picture: one row per option
        rows = []
        for lab, t, key in items:
            cell = Paragraph(t, OPT) if t is not None else (img_flow(key, col_w - 9 * mm, 45 * mm) or Paragraph("", OPT))
            rows.append([Paragraph(f"<font name='{FONT_B}'>({lab})</font>", OPT), cell])
        t = Table(rows, colWidths=[8 * mm, col_w - 8 * mm], hAlign="LEFT")
        t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                               ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
        return [t]

    def answer_box(q):
        key = ", ".join(f"({LETTERS[n - 1]})" for n in q.correct) or "—"
        txt = f"Answer: {key}"
        if show_yours:
            yours = ", ".join(f"({LETTERS[n - 1]})" for n in q.chosen) or "not attempted"
            col = {"Correct": GREEN, "Wrong": RED}.get(q.status, GREY).hexval().replace("0x", "#")
            mark = {"Correct": " right", "Wrong": " wrong"}.get(q.status, "")
            txt += f"<font name='{FONT}' color='#6B6B6B'>&nbsp;&nbsp;·&nbsp;&nbsp;Your answer: </font><font color='{col}'>{yours}{mark}</font>"
        t = Table([[Paragraph(txt, ANS)]], colWidths=[col_w])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), BAR_BG),
            ("LINEBEFORE", (0, 0), (0, 0), 2.2, BLUE),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        return t

    story = []
    sections = []
    for q in questions:
        if not sections or sections[-1][0] != q.section:
            sections.append((q.section, []))
        sections[-1][1].append(q)

    for si, (name, qs) in enumerate(sections):
        if si > 0:
            story += [NextPageTemplate("sec"), PageBreak()]
        story += [section_bar(name), FrameBreak(), NextPageTemplate("body")]
        last_passage = None
        for q in qs:
            label = f"<font name='{FONT_B}'>Q.{q.qno}.</font> "
            qtext = text_of(q.q_img) if q.q_img else _esc(q.q_text)
            block = []
            if qtext is not None:
                passage, ask = split_passage(qtext)
                if passage:
                    if _norm(passage) != last_passage:
                        story.append(CondPageBreak(40 * mm))
                        story.append(Paragraph(f"<font name='{FONT_B}' color='#B3261E'>Passage (Q.{q.qno} onwards)</font>", PASS))
                        story.append(Spacer(1, 2))
                        story.append(Paragraph(passage, PASS))
                        story.append(Spacer(1, 6))
                        last_passage = _norm(passage)
                    block.append(Paragraph(label + ask, Q))
                else:
                    last_passage = None
                    block.append(Paragraph(label + qtext, Q))
            else:
                last_passage = None
                block.append(Paragraph(label, Q))
                im = img_flow(q.q_img, col_w)
                if im:
                    block.append(im)
            block.append(Spacer(1, 3))
            block += options_block(q)
            block.append(Spacer(1, 4))
            block.append(answer_box(q))
            block.append(Spacer(1, 10))
            story.append(KeepTogether(block))

    doc.build(story)
