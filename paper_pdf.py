"""
Paper-style PDF: two columns, question → options (a)–(d) → Answer box,
like the answer-key papers shared after exams.
"""

import io
import os
import re

from PIL import Image as PILImage

from img_tools import line_height, reflow, split_question, trim
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

_BUNDLED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fonts")
_FONT_CANDIDATES = [
    tuple(os.path.join(_BUNDLED, f"LiberationSerif-{w}.ttf") for w in ("Regular", "Bold", "Italic", "BoldItalic")),
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
    WM_ALPHA = float(S.get("watermark_opacity", 0.08) or 0.08)
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

    LANG = (S.get("language") or "en").lower()
    _trimmed = {}
    _scales = {}

    def text_scale(reason):
        if reason not in _scales:
            hs = sorted(h for h in (line_height(store.get(k)) for k, r in ocr.items()
                                    if r.reason == reason and store.get(k)) if h)
            band = FS * (1.38 if reason == "hindi" else 1.3)
            _scales[reason] = band / hs[len(hs) // 2] if hs else None
        return _scales[reason]

    def img_flow(key, max_w, max_h=110 * mm, match=None):
        blob = store.get(key)
        if not blob:
            return None
        r = ocr.get(key)
        if r is not None and r.reason == "hindi":
            # text kept as a picture: re-wrap it to the column at the size of the typed text,
            # one scale for the whole paper so every Hindi line comes out the same size
            got = reflow(blob, max_w, scale=text_scale(r.reason), lo_pt=FS * 1.2, hi_pt=FS * 1.6)
            if got:
                data, w, h = got
                if h <= max_h:
                    return Image(io.BytesIO(data), width=w, height=h, hAlign="LEFT")
        if key not in _trimmed:
            _trimmed[key] = trim(blob)
        blob = _trimmed[key]
        w, h = PILImage.open(io.BytesIO(blob)).size
        scale = min(0.62, max_w / w)
        if match:
            # option pictures next to a question figure: meet it halfway so both look alike in size
            scale = min(max_w / w, (scale * match) ** 0.5)
        if h * scale > max_h:
            scale = max_h / h
        return Image(io.BytesIO(blob), width=w * scale, height=h * scale, hAlign="LEFT")

    fig_scale = [None]            # pt per pixel of the current question's figure

    def question_picture(key):
        """Flowables for a question picture that isn't typed text. A wide 'text on top, figures
        below' picture is split: the text is re-wrapped to the column at text size and the figures
        get the full column width."""
        blob = store.get(key)
        r = ocr.get(key)
        if not blob or (r is not None and r.reason == "hindi"):
            im = img_flow(key, col_w)
            return [Spacer(1, 2), im] if im else []
        parts = split_question(blob)
        if parts:
            text_png, fig_png, text_h = parts
            got = reflow(text_png, col_w, scale=FS * 1.08 / max(1, text_h))
            fw, fh = PILImage.open(io.BytesIO(fig_png)).size
            fs = min(0.8, col_w / fw)
            if fh * fs > 95 * mm:
                fs = 95 * mm / fh
            if got:
                data, w, h = got
                fig_scale[0] = fs
                return [Spacer(1, 2), Image(io.BytesIO(data), width=w, height=h, hAlign="LEFT"), Spacer(1, 4),
                        Image(io.BytesIO(fig_png), width=fw * fs, height=fh * fs, hAlign="LEFT")]
        im = img_flow(key, col_w)
        if im:
            fig_scale[0] = im.drawWidth / max(1, PILImage.open(io.BytesIO(_trimmed.get(key) or blob)).size[0])
        return [Spacer(1, 2), im] if im else []

    def _pic_size(k):
        try:
            return PILImage.open(io.BytesIO(_trimmed.setdefault(k, trim(store.get(k))))).size
        except Exception:
            return None

    def one_language(keys):
        """Fallback when the image names don't say the language: drop the other-language copies."""
        keys = [k for k in keys or [] if k]
        if LANG == "both" or len(keys) < 2:
            return keys
        res = {k: ocr.get(k) for k in keys}
        hin = [k for k in keys if res[k] is not None and res[k].reason == "hindi"]
        other = [k for k in keys if k not in hin]
        if hin and other:
            if LANG == "en":
                keys = other
            else:                      # hi: keep Hindi + pictures, drop the typed English copy
                keys = [k for k in keys if k in hin or res[k] is None or not res[k].ok]
        # EN figure(s) followed by the same number of HI figure(s) of the same size -> keep one set
        pics = [k for k in keys if res.get(k) is None or not res[k].ok]
        if len(pics) == len(keys) and len(keys) % 2 == 0:
            n = len(keys) // 2
            a, b = keys[:n], keys[n:]
            sizes = [(_pic_size(x), _pic_size(y)) for x, y in zip(a, b)]
            if all(p and q and abs(p[0] - q[0]) <= 0.1 * max(p[0], q[0]) and abs(p[1] - q[1]) <= 0.15 * max(p[1], q[1])
                   for p, q in sizes):
                keys = b if LANG == "hi" else a
        return keys

    def parts_of(keys, fallback_text=""):
        """[(markup or None, key)] for every picture in a cell; None = keep the picture."""
        out, seen = [], set()
        for k in one_language(keys):
            r = ocr.get(k)
            txt = r.markup if (r and r.ok) else None
            if txt is not None:
                n = _norm(txt)
                if n in seen:          # same words twice (English + Hindi picture of a number)
                    continue
                seen.add(n)
            out.append((txt, k))
        if not out and fallback_text:
            out.append((_esc(fallback_text), None))
        return out

    # ---------------- page furniture
    def header_box(canvas):
        x, y, w, h = M, top_y - head_h + 3 * mm, page_w - 2 * M, head_h - 3 * mm
        canvas.setFillColor(ACC)
        canvas.roundRect(x, y, w, h, 6, stroke=0, fill=1)
        # darker right panel
        canvas.setFillColor(colors.Color(ACC.red * 0.7, ACC.green * 0.7, ACC.blue * 0.7))
        rx = x + w - 66 * mm
        canvas.roundRect(rx - 4 * mm, y + 3 * mm, 66 * mm, h - 6 * mm, 5, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont(FONT_B, 14)
        canvas.drawString(x + 6 * mm, y + h - 10 * mm, TITLE[:48])
        canvas.setFont(FONT, 9.5)
        canvas.drawString(x + 6 * mm, y + h - 15.5 * mm, (cand.exam or "SSC Examination")[:78])
        canvas.setFillColor(colors.Color(1, 1, 1, alpha=0.75)); canvas.setFont(FONT, 8)
        canvas.drawString(x + 6 * mm, y + h - 20.5 * mm, "Question paper with official answer key")
        if CH_NAME:
            canvas.setFillColor(colors.white); canvas.setFont(FONT_B, 8.5)
            label = f"Join {CH_NAME}  \u00b7  {CH_LINK.replace('https://', '')}" if CH_LINK else CH_NAME
            canvas.drawString(x + 6 * mm, y + h - 26.5 * mm, label)
            if CH_LINK:
                lw = pdfmetrics.stringWidth(label, FONT_B, 8.5)
                canvas.linkURL(CH_LINK, (x + 6 * mm, y + h - 27.5 * mm, x + 6 * mm + lw, y + h - 24 * mm), relative=0)
        canvas.setFillColor(colors.white); canvas.setFont(FONT_B, 10.5)
        canvas.drawString(rx, y + h - 9 * mm, "SSC Online Exam")
        date = " ".join(t.capitalize() if t.isalpha() else t for t in (cand.test_date or "").split())
        rows = [("Date", date), ("Shift", cand.shift)]
        if not hide_candidate:
            rows += [("Roll No", cand.roll_no), ("Name", cand.name)]
        yy = y + h - 14.5 * mm
        for k, v in rows:
            canvas.setFillColor(colors.Color(1, 1, 1, alpha=0.75)); canvas.setFont(FONT, 8)
            canvas.drawString(rx, yy, f"{k}")
            canvas.setFillColor(colors.white); canvas.setFont(FONT_B, 8.2)
            canvas.drawString(rx + 14 * mm, yy, (v or "")[:30])
            yy -= 4.2 * mm

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
        # thin divider between the two columns
        canvas.setStrokeColor(LINE); canvas.setLineWidth(0.5)
        col_top = (top_y - head_h - bar_h - 2 * mm) if first else (top_y - 2 * mm)
        canvas.line(page_w / 2, bottom, page_w / 2, col_top)
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

    def cell_flows(parts, style, max_w, max_h, match=None):
        out = []
        for txt, key in parts:
            if txt is not None:
                out.append(Paragraph(txt, style))
            else:
                im = img_flow(key, max_w, max_h, match)
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
            texts = [(lab, " ".join(t for t, _ in p)) for lab, p in items]
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
            flows = [(lab, cell_flows(p, OPT, half - 10 * mm, 38 * mm, fig_scale[0])) for lab, p in items]
            if all(getattr(f, "drawWidth", 0) <= half - 9 * mm for _, fl in flows for f in fl):
                rows = []
                for i in range(0, len(flows), 2):
                    row = []
                    for lab, fl in flows[i:i + 2]:
                        row += [Paragraph(f"<font name='{FONT_B}'>({lab})</font>", OPT), fl]
                    rows.append(row)
                t = Table(rows, colWidths=[8 * mm, half - 8 * mm] * 2, hAlign="LEFT")
                t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                       ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
                return [t]
        # at least one option is a picture (figure / Hindi): one row per option
        rows = []
        for lab, p in items:
            rows.append([Paragraph(f"<font name='{FONT_B}'>({lab})</font>", OPT),
                         cell_flows(p, OPT, col_w - 9 * mm, 45 * mm, fig_scale[0])])
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
            fig_scale[0] = None
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
                    block.append(Paragraph(label + re.sub(r"(<br/>\s*){2,}", "<br/>", first_txt), Q))
                rest = parts[1:]
            else:
                last_passage = None
                block.append(Paragraph(label, Q))
                rest = parts
            for txt, key in rest:          # more text, or the original picture (figure / Hindi)
                if txt is not None:
                    block.append(Paragraph(txt, Q))
                else:
                    block += question_picture(key)
            block.append(Spacer(1, 3))
            block += options_block(q)
            block.append(Spacer(1, 4))
            block.append(answer_box(q))
            block.append(Spacer(1, 10))
            story.append(KeepTogether(block))

    doc.build(story)
