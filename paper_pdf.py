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


def _hex(c, default):
    try:
        return colors.HexColor(c)
    except Exception:
        return default


def build_paper(path, cand, questions, store, ocr, *, show_yours=False, hide_candidate=False,
                settings=None, watermark=None, footer_text="", pos=1.0, neg=0.25):
    """
    questions: list of Question (from ssc_report)
    ocr: dict image_key -> OcrResult (text, or ok=False to keep the original picture)
    settings: dict from settings.py (colours, watermark, channel name/link, heading)
    """
    FONT, FONT_B = _register_fonts()
    S = dict(settings or {})
    if watermark is not None:            # command-line override
        S["watermark"] = watermark
    Q_COL = _hex(S.get("question_color"), RED)
    O_COL = _hex(S.get("option_color"), INK)
    A_COL = _hex(S.get("answer_color"), BLUE)
    ACC = _hex(S.get("accent_color"), BLUE)
    WM_COL = _hex(S.get("watermark_color"), colors.HexColor("#DB3333"))
    WM_ALPHA = float(S.get("watermark_opacity", 0.13) or 0.13)
    WM = (S.get("watermark") or "").strip()
    FS = float(S.get("font_size", 9.4) or 9.4)
    TITLE = S.get("header_title") or "Staff Selection Commission"
    CH_NAME = (S.get("channel_name") or "").strip()
    CH_LINK = (S.get("channel_link") or "").strip()

    page_w, page_h = A4
    M = 13 * mm
    gutter = 7 * mm
    col_w = (page_w - 2 * M - gutter) / 2
    head_h = 36 * mm
    bar_h = 12 * mm
    bottom = M + 6 * mm
    top_y = page_h - M

    Q = ParagraphStyle("Q", fontName=FONT, fontSize=FS, leading=FS * 1.32, textColor=Q_COL)
    PASS = ParagraphStyle("P", fontName=FONT, fontSize=FS - 0.6, leading=(FS - 0.6) * 1.32, textColor=INK)
    OPT = ParagraphStyle("O", fontName=FONT, fontSize=FS - 0.4, leading=(FS - 0.4) * 1.33, textColor=O_COL)
    ANS = ParagraphStyle("A", fontName=FONT_B, fontSize=FS - 0.4, leading=FS * 1.17, textColor=A_COL)
    BAR = ParagraphStyle("BAR", fontName=FONT_B, fontSize=10.5, leading=13, textColor=INK)

    def img_flow(key, max_w, max_h=110 * mm):
        blob = store.get(key)
        if not blob:
            return None
        w, h = PILImage.open(io.BytesIO(blob)).size
        scale = min(0.62, max_w / w)
        if h * scale > max_h:
            scale = max_h / h
        return Image(io.BytesIO(blob), width=w * scale, height=h * scale, hAlign="LEFT")

    def parts_of(keys, fallback_text=""):
        """[(markup or None, key)] for every picture in a cell; None = keep the picture."""
        out = []
        for k in keys or []:
            r = ocr.get(k)
            out.append((r.markup if (r and r.ok) else None, k))
        if not out and fallback_text:
            out.append((_esc(fallback_text), None))
        return out

    # ---------------- page furniture
    def header_box(canvas):
        x, y, w, h = M, top_y - head_h + 3 * mm, page_w - 2 * M, head_h - 3 * mm
        canvas.setStrokeColor(ACC); canvas.setLineWidth(1.4)
        canvas.roundRect(x, y, w, h, 4, stroke=1, fill=0)
        canvas.setFillColor(INK)
        canvas.setFont(FONT_B, 13)
        canvas.drawString(x + 6 * mm, y + h - 10 * mm, TITLE[:48])
        canvas.setFont(FONT, 9)
        canvas.drawString(x + 6 * mm, y + h - 15.5 * mm, (cand.exam or "SSC Examination")[:80])
        canvas.setFont(FONT, 8)
        canvas.setFillColor(GREY)
        canvas.drawString(x + 6 * mm, y + h - 20.5 * mm, "Question paper with official answer key")
        if CH_NAME:
            canvas.setFillColor(ACC); canvas.setFont(FONT_B, 8)
            label = f"{CH_NAME}  ·  {CH_LINK.replace('https://', '')}" if CH_LINK else CH_NAME
            canvas.drawString(x + 6 * mm, y + h - 25.5 * mm, label)
            if CH_LINK:
                lw = pdfmetrics.stringWidth(label, FONT_B, 8)
                canvas.linkURL(CH_LINK, (x + 6 * mm, y + h - 26.5 * mm, x + 6 * mm + lw, y + h - 23 * mm), relative=0)
        rx = x + w - 62 * mm
        canvas.setFillColor(ACC); canvas.setFont(FONT_B, 11)
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
        if WM:
            # large diagonal channel handle, twice per page (like shared answer-key papers)
            canvas.setFillColor(colors.Color(WM_COL.red, WM_COL.green, WM_COL.blue, alpha=WM_ALPHA))
            size = 60
            tw = pdfmetrics.stringWidth(WM, FONT_B, size)
            size = max(24, min(72, size * (page_w * 0.78) / max(tw, 1)))
            canvas.setFont(FONT_B, size)
            for cx, cy in ((page_w * 0.46, page_h * 0.66), (page_w * 0.56, page_h * 0.24)):
                canvas.saveState()
                canvas.translate(cx, cy)
                canvas.rotate(42)
                canvas.drawCentredString(0, -size / 3, WM)
                canvas.restoreState()
        # footer: channel name + clickable link
        foot = footer_text
        if not foot and CH_NAME:
            foot = f"Join {CH_NAME}" + (f"  ·  {CH_LINK.replace('https://', '')}" if CH_LINK else "")
        if foot:
            canvas.setFillColor(ACC); canvas.setFont(FONT_B, 8)
            canvas.drawString(M, M, foot)
            if CH_LINK:
                fw = pdfmetrics.stringWidth(foot, FONT_B, 8)
                canvas.linkURL(CH_LINK, (M, M - 2, M + fw, M + 8), relative=0)
        canvas.setFillColor(GREY); canvas.setFont(FONT, 7.5)
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
                          author=CH_NAME or "ssc_report.py")

    def section_bar(name):
        t = Table([[Paragraph(_esc(name.upper()), BAR)]], colWidths=[page_w - 2 * M], rowHeights=[9 * mm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), BAR_BG),
            ("LINEBEFORE", (0, 0), (0, 0), 3.5, ACC),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        return t

    def cell_flows(parts, style, max_w, max_h):
        out = []
        for txt, key in parts:
            if txt is not None:
                out.append(Paragraph(txt, style))
            else:
                im = img_flow(key, max_w, max_h)
                if im:
                    out.append(im)
        return out or [Paragraph("", style)]

    def options_block(q):
        items = []   # (label, [(markup|None, key)])
        for o in q.options:
            lab = LETTERS[o.number - 1] if o.number <= len(LETTERS) else str(o.number)
            items.append((lab, parts_of(o.imgs or ([o.img] if o.img else []), o.text)))
        all_text = all(p and all(t is not None for t, _ in p) for _, p in items)
        if all_text:
            texts = [(lab, " / ".join(t for t, _ in p)) for lab, p in items]
            plain_len = max(len(_norm(t)) for _, t in texts)
            parts = [f"<font name='{FONT_B}'>({lab})</font> {t}" for lab, t in texts]
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
        # all options are small pictures (figures): 2 x 2 grid saves a lot of space
        half = col_w / 2
        if len(items) in (2, 4) and all(p and all(t is None for t, _ in p) for _, p in items):
            flows = [(lab, cell_flows(p, OPT, half - 10 * mm, 38 * mm)) for lab, p in items]
            if all(getattr(f, "drawWidth", 0) <= half - 9 * mm for _, fl in flows for f in fl):
                rows = []
                for i in range(0, len(flows), 2):
                    row = []
                    for lab, fl in flows[i:i + 2]:
                        row += [Paragraph(f"<font name='{FONT_B}'>({lab})</font>", OPT), fl]
                    rows.append(row)
                t = Table(rows, colWidths=[8 * mm, half - 8 * mm] * 2, hAlign="LEFT")
                t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                       ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
                return [t]
        # at least one option is a picture (figure / Hindi): one row per option
        rows = []
        for lab, p in items:
            rows.append([Paragraph(f"<font name='{FONT_B}'>({lab})</font>", OPT),
                         cell_flows(p, OPT, col_w - 9 * mm, 45 * mm)])
        t = Table(rows, colWidths=[8 * mm, col_w - 8 * mm], hAlign="LEFT")
        t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                               ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]))
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
            ("LINEBEFORE", (0, 0), (0, 0), 2.2, ACC),
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
            parts = parts_of(q.q_imgs or ([q.q_img] if q.q_img else []), q.q_text)
            block = []
            first_txt = parts[0][0] if parts else None
            if first_txt is not None:
                passage, ask = split_passage(first_txt)
                if passage:
                    if _norm(passage) != last_passage:
                        story.append(CondPageBreak(40 * mm))
                        story.append(Paragraph(f"<font name='{FONT_B}' color='{Q_COL.hexval().replace('0x', '#')}'>Passage (Q.{q.qno} onwards)</font>", PASS))
                        story.append(Spacer(1, 2))
                        story.append(Paragraph(passage, PASS))
                        story.append(Spacer(1, 6))
                        last_passage = _norm(passage)
                    block.append(Paragraph(label + ask, Q))
                else:
                    last_passage = None
                    block.append(Paragraph(label + first_txt, Q))
                rest = parts[1:]
            else:
                last_passage = None
                block.append(Paragraph(label, Q))
                rest = parts
            for txt, key in rest:          # more text, or the original picture (figure / Hindi)
                if txt is not None:
                    block.append(Paragraph(txt, Q))
                else:
                    im = img_flow(key, col_w)
                    if im:
                        block += [Spacer(1, 2), im]
            block.append(Spacer(1, 3))
            block += options_block(q)
            block.append(Spacer(1, 4))
            block.append(answer_box(q))
            block.append(Spacer(1, 10))
            story.append(KeepTogether(block))

    doc.build(story)
