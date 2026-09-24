#!/usr/bin/env python3
"""
SSC response sheet analyser.

Reads SSC "Candidate Response Sheet" pages (saved .mhtml files or live links),
marks every question Correct / Wrong / Not attempted, scores it section by
section, and writes a PDF report with each question and its options marked.

Usage
  python ssc_report.py                       # every file in input/
  python ssc_report.py part-a.mhtml part-c.mhtml
  python ssc_report.py --links "URL1 URL2 URL3"
  python ssc_report.py --cards wrong         # only wrong/unattempted questions in the PDF

How the SSC page marks options (legend on the page itself):
  green  = correct option, and you chose it
  red    = option you chose, and it is wrong
  yellow = correct option, you did not choose it
  none of green/red in a question = not answered
"""

import argparse
import csv
import email
import glob
import io
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from email import policy
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from PIL import Image as PILImage

# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Option:
    number: int
    colour: str            # green / red / yellow / ""
    img: str | None = None  # image key in the image store
    text: str = ""


@dataclass
class Question:
    qno: int
    section: str
    question_id: str
    challenge_url: str
    q_img: str | None
    q_text: str
    options: list = field(default_factory=list)

    @property
    def correct(self):
        return [o.number for o in self.options if o.colour in ("green", "yellow")]

    @property
    def chosen(self):
        return [o.number for o in self.options if o.colour in ("green", "red")]

    @property
    def status(self):
        cols = [o.colour for o in self.options]
        if "green" in cols:
            return "Correct"
        if "red" in cols:
            return "Wrong"
        return "Not attempted"


@dataclass
class Candidate:
    roll_no: str = ""
    name: str = ""
    exam: str = ""
    test_date: str = ""
    shift: str = ""
    centre: str = ""


# --------------------------------------------------------------------------
# Loading pages
# --------------------------------------------------------------------------

class ImageStore:
    """Holds image bytes keyed by URL; looks up with or without query string."""

    def __init__(self):
        self.data = {}

    @staticmethod
    def _strip(url):
        p = urlsplit(url)
        return urlunsplit((p.scheme, p.netloc, p.path, "", ""))

    def add(self, url, blob):
        self.data[url] = blob
        self.data.setdefault(self._strip(url), blob)

    def get(self, url):
        if url is None:
            return None
        return self.data.get(url) or self.data.get(self._strip(url))


def load_mhtml(path, store):
    raw = open(path, "rb").read()
    msg = email.message_from_bytes(raw, policy=policy.default)
    html = None
    base = None
    for part in msg.walk():
        ctype = part.get_content_type()
        loc = part.get("Content-Location")
        if ctype == "text/html" and html is None:
            payload = part.get_payload(decode=True)
            charset = part.get_content_charset() or "utf-8"
            html = payload.decode(charset, errors="replace")
            base = loc
        elif ctype.startswith("image/"):
            blob = part.get_payload(decode=True)
            if loc:
                store.add(loc, blob)
            cid = part.get("Content-ID")
            if cid:
                store.add("cid:" + cid.strip("<>"), blob)
    if html is None:
        raise ValueError(f"{path}: no HTML page found inside. Is this a Chrome 'page' (.mhtml) download?")
    return html, base


def load_html_file(path, store):
    html = open(path, encoding="utf-8", errors="replace").read()
    folder = os.path.dirname(os.path.abspath(path))
    for src in re.findall(r'<img[^>]+src="([^"]+)"', html):
        if src.startswith("http") or src.startswith("data:"):
            continue
        local = os.path.join(folder, src.split("?")[0])
        if os.path.exists(local):
            store.add(src, open(local, "rb").read())
    return html, None


def load_url(url, store):
    import requests
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/128.0 Mobile Safari/537.36"),
        "Accept-Language": "en-IN,en;q=0.9",
    })
    try:
        r = s.get(url, timeout=60)
        r.raise_for_status()
    except Exception as e:
        raise RuntimeError(
            f"Could not open the link ({e}).\n"
            "The SSC site may block servers outside India or the link may have expired.\n"
            "Fix: save the page as .mhtml in Chrome and put it in the input/ folder instead."
        )
    html = r.text
    if "Q.No" not in html:
        raise RuntimeError(
            "The link opened, but it doesn't look like a response sheet (no questions found).\n"
            "It may need a login. Save the page as .mhtml in Chrome and use the input/ folder instead."
        )
    srcs = set(re.findall(r'<img[^>]+src="([^"]+)"', html))
    for src in srcs:
        full = urljoin(r.url, src)
        try:
            ir = s.get(full, timeout=60, headers={"Referer": r.url})
            if ir.ok:
                store.add(src, ir.content)
                store.add(full, ir.content)
        except Exception:
            pass
    return html, r.url


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def clean(s):
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def parse_candidate(soup):
    c = Candidate()
    labels = {
        "roll no": "roll_no", "candidate name": "name", "exam level": "exam",
        "test date": "test_date", "test time and shift": "shift", "centre name": "centre",
    }
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) != 2:
            continue
        key = clean(tds[0].get_text()).rstrip(".").lower()
        if key in labels:
            sel = tds[1].find("option", selected=True)
            val = clean(sel.get_text()) if sel else clean(tds[1].get_text())
            setattr(c, labels[key], val.lstrip(":").strip())
    return c


def cell_colour(td):
    if td is None:
        return ""
    col = (td.get("bgcolor") or "").lower()
    if not col:
        m = re.search(r"background-color:\s*([a-z]+)", td.get("style", "") or "", re.I)
        col = m.group(1).lower() if m else ""
    return col if col in ("green", "red", "yellow") else ""


def question_id_from(img_src, fallback):
    if img_src:
        m = re.search(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", img_src, re.I)
        if m:
            return m.group(1)
    return fallback


def parse_questions(soup, default_section):
    """Walk the page in order, tracking the current section heading."""
    questions = []
    section = default_section
    challenge_re = re.compile(r"Challenge Question No", re.I)
    for el in soup.find_all(["span", "table"]):
        if el.name == "span" and el.get("id", "").startswith("lblsubject"):
            section = clean(el.get_text()) or section
            continue
        if el.name != "table":
            continue
        link = el.find("a", string=challenge_re)
        if link is None or link.find_parent("table") is not el:
            continue
        m = re.search(r"(\d+)", link.get_text())
        qno = int(m.group(1)) if m else len(questions) + 1
        q_img, q_text, opts = None, "", []
        for tr in el.find_all("tr"):
            if tr.find_parent("table") is not el:
                continue
            tds = tr.find_all("td", recursive=False)
            if len(tds) < 2 or tr.find("a", string=challenge_re):
                continue
            content = tds[-1]
            img = content.find("img")
            src = img.get("src") if img else None
            text = clean(content.get_text())
            if "Q.No" in tds[0].get_text():
                q_img, q_text = src, text
            elif src or text:
                opts.append(Option(number=len(opts) + 1, colour=cell_colour(tds[0]), img=src, text=text))
        qid = question_id_from(q_img, f"{section[:6]}-Q{qno}")
        questions.append(Question(qno=qno, section=section, question_id=qid,
                                  challenge_url=link.get("href", ""), q_img=q_img,
                                  q_text=q_text, options=opts))
    return questions


def section_short(name):
    m = re.match(r"(PART-[A-Z])", name, re.I)
    return m.group(1).upper() if m else name[:12]


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def score(questions, pos, neg):
    sections = {}
    for q in questions:
        s = sections.setdefault(q.section, {"total": 0, "Correct": 0, "Wrong": 0, "Not attempted": 0})
        s["total"] += 1
        s[q.status] += 1
    for s in sections.values():
        s["attempted"] = s["Correct"] + s["Wrong"]
        s["marks"] = s["Correct"] * pos - s["Wrong"] * neg
        s["max"] = s["total"] * pos
    tot = {k: sum(s[k] for s in sections.values())
           for k in ("total", "Correct", "Wrong", "Not attempted", "attempted", "marks", "max")}
    return sections, tot


def q_marks(q, pos, neg):
    return {"Correct": pos, "Wrong": -neg}.get(q.status, 0.0)


def fmt(x):
    return f"{x:+.2f}" if x else "0.00"


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

def build_pdf(path, cand, questions, sections, tot, store, pos, neg, cards):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                    TableStyle, Image, PageBreak, KeepTogether, CondPageBreak)

    GREEN = colors.HexColor("#1E8E3E")
    GREEN_BG = colors.HexColor("#E6F4EA")
    RED = colors.HexColor("#D93025")
    RED_BG = colors.HexColor("#FCE8E6")
    GREY = colors.HexColor("#5F6368")
    GREY_BG = colors.HexColor("#F1F3F4")
    INK = colors.HexColor("#202124")
    LINE = colors.HexColor("#DADCE0")
    STATUS_COL = {"Correct": GREEN, "Wrong": RED, "Not attempted": GREY}
    STATUS_BG = {"Correct": GREEN_BG, "Wrong": RED_BG, "Not attempted": GREY_BG}

    ss = getSampleStyleSheet()
    H1 = ParagraphStyle("H1", parent=ss["Title"], fontSize=18, leading=22, textColor=INK, spaceAfter=2)
    H2 = ParagraphStyle("H2", parent=ss["Heading2"], fontSize=13, leading=16, textColor=INK, spaceBefore=10, spaceAfter=6)
    BODY = ParagraphStyle("B", parent=ss["BodyText"], fontSize=9, leading=12, textColor=INK)
    SMALL = ParagraphStyle("S", parent=BODY, fontSize=7.5, leading=9.5, textColor=GREY)
    CELL = ParagraphStyle("C", parent=BODY, fontSize=8, leading=10)
    CELLB = ParagraphStyle("CB", parent=CELL, fontName="Helvetica-Bold")

    page_w, page_h = A4
    margin = 14 * mm
    avail_w = page_w - 2 * margin
    avail_h = page_h - 2 * margin - 20

    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def img_flow(key, max_w, max_h=None):
        blob = store.get(key)
        if not blob:
            return None
        try:
            im = PILImage.open(io.BytesIO(blob))
            w, h = im.size
        except Exception:
            return None
        scale = min(0.75, max_w / w)
        if max_h and h * scale > max_h:
            scale = max_h / h
        return Image(io.BytesIO(blob), width=w * scale, height=h * scale)

    story = []

    # ---- Header
    story.append(Paragraph("SSC Response Sheet Analysis", H1))
    story.append(Paragraph(esc(cand.exam or "SSC Examination"), SMALL))
    story.append(Spacer(1, 6))
    info = [
        ["Candidate", cand.name, "Roll No.", cand.roll_no],
        ["Test date", cand.test_date, "Shift", cand.shift],
        ["Centre", cand.centre, "Sections", ", ".join(section_short(s) for s in sections)],
    ]
    t = Table([[Paragraph(f"<b>{esc(a)}</b>", CELL), Paragraph(esc(b), CELL),
                Paragraph(f"<b>{esc(c)}</b>", CELL), Paragraph(esc(d), CELL)] for a, b, c, d in info],
              colWidths=[22 * mm, 68 * mm, 22 * mm, avail_w - 112 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), GREY_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t)

    # ---- Big total
    story.append(Spacer(1, 10))
    big = ParagraphStyle("big", parent=BODY, fontSize=26, leading=30, fontName="Helvetica-Bold", textColor=INK)
    story.append(Paragraph(f"{tot['marks']:.2f} <font size=13 color='#5F6368'>/ {tot['max']:.0f}</font>", big))
    story.append(Paragraph(
        f"Raw score, +{pos:g} per correct, −{neg:g} per wrong, before SSC normalisation. "
        f"{tot['Correct']} correct · {tot['Wrong']} wrong · {tot['Not attempted']} not attempted.", SMALL))

    # ---- Score table
    story.append(Paragraph("Score by section", H2))
    rows = [["Section", "Qs", "Attempted", "Correct", "Wrong", "Not att.", "Marks"]]
    for name, s in sections.items():
        rows.append([Paragraph(esc(name), CELL), s["total"], s["attempted"], s["Correct"], s["Wrong"],
                     s["Not attempted"], f"{s['marks']:.2f} / {s['max']:.0f}"])
    rows.append([Paragraph("<b>Total</b>", CELL), tot["total"], tot["attempted"], tot["Correct"], tot["Wrong"],
                 tot["Not attempted"], f"{tot['marks']:.2f} / {tot['max']:.0f}"])
    t = Table(rows, colWidths=[avail_w - 128 * mm, 12 * mm, 20 * mm, 18 * mm, 16 * mm, 18 * mm, 44 * mm],
              repeatRows=1)
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 8.5),
        ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 8.5),
        ("BACKGROUND", (0, 0), (-1, 0), GREY_BG),
        ("BACKGROUND", (0, -1), (-1, -1), GREY_BG),
        ("TEXTCOLOR", (3, 1), (3, -1), GREEN),
        ("TEXTCOLOR", (4, 1), (4, -1), RED),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
    ]))
    story.append(t)

    # ---- Wrong / unattempted index for challenges
    flagged = [q for q in questions if q.status != "Correct"]
    if flagged:
        story.append(Paragraph("Questions to review before challenging", H2))
        story.append(Paragraph(
            "Wrong and unattempted questions with their Question IDs. Open a question's page in this PDF "
            "to see the options; tap its <i>Challenge</i> link to open SSC's challenge page (you must be logged in).",
            SMALL))
        story.append(Spacer(1, 4))
        rows = [["Q.No", "Section", "Yours", "Key", "Result", "Question ID"]]
        for q in flagged:
            rows.append([q.qno, section_short(q.section),
                         ",".join(map(str, q.chosen)) or "–", ",".join(map(str, q.correct)) or "–",
                         q.status, Paragraph(f"<font face='Courier' size=7>{esc(q.question_id)}</font>", CELL)])
        t = Table(rows, colWidths=[13 * mm, 18 * mm, 14 * mm, 14 * mm, 24 * mm, avail_w - 83 * mm], repeatRows=1)
        st = [
            ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
            ("FONT", (0, 1), (-1, -1), "Helvetica", 8),
            ("BACKGROUND", (0, 0), (-1, 0), GREY_BG),
            ("ALIGN", (0, 0), (4, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.3, LINE),
            ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]
        for i, q in enumerate(flagged, 1):
            st.append(("TEXTCOLOR", (4, i), (4, i), STATUS_COL[q.status]))
        t.setStyle(TableStyle(st))
        story.append(t)

    # ---- Full answer list, 3 columns side by side
    story.append(CondPageBreak(90 * mm))
    story.append(Paragraph("Answer list", H2))
    story.append(Paragraph("Yours = option you chose, Key = SSC's correct option.", SMALL))
    story.append(Spacer(1, 4))
    items = [[q.qno, ",".join(map(str, q.chosen)) or "–", ",".join(map(str, q.correct)) or "–",
              {"Correct": "✓", "Wrong": "✗", "Not attempted": "–"}[q.status], fmt(q_marks(q, pos, neg)), q.status]
             for q in questions]
    ncol = 3
    per = -(-len(items) // ncol)
    header = ["Q", "Yours", "Key", "", "Marks"]
    grid = [header * ncol]
    style = [("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5),
             ("FONT", (0, 1), (-1, -1), "Helvetica", 7.5),
             ("ALIGN", (0, 0), (-1, -1), "CENTER"),
             ("TOPPADDING", (0, 0), (-1, -1), 1.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.2),
             ("LINEBELOW", (0, 0), (-1, 0), 0.5, LINE)]
    for r in range(per):
        row = []
        for c in range(ncol):
            idx = c * per + r
            if idx < len(items):
                it = items[idx]
                row += it[:5]
                style.append(("TEXTCOLOR", (c * 5 + 3, r + 1), (c * 5 + 4, r + 1), STATUS_COL[it[5]]))
                style.append(("BACKGROUND", (c * 5, r + 1), (c * 5 + 4, r + 1), STATUS_BG[it[5]]))
            else:
                row += [""] * 5
        grid.append(row)
    for c in range(1, ncol):
        style.append(("LINEBEFORE", (c * 5, 0), (c * 5, -1), 1.5, colors.white))
    cw = avail_w / ncol
    t = Table(grid, colWidths=[cw * f for f in (0.16, 0.2, 0.2, 0.14, 0.3)] * ncol, repeatRows=1)
    t.setStyle(TableStyle(style))
    story.append(t)

    # ---- Question pages
    shown = questions if cards == "all" else [q for q in questions if q.status != "Correct"]
    if shown:
        story.append(PageBreak())
        story.append(Paragraph("Questions", H2))
        story.append(Paragraph(
            "<font color='#1E8E3E'><b>Green</b></font> = SSC's correct option. "
            "<font color='#D93025'><b>Red</b></font> = your wrong choice.", SMALL))
        story.append(Spacer(1, 6))

    marker_w = 22 * mm
    inner_w = avail_w - 12
    for q in shown:
        col = STATUS_COL[q.status]
        m = q_marks(q, pos, neg)
        link = (f" · <a href='{esc(q.challenge_url)}' color='#1A73E8'>Challenge</a>"
                if q.challenge_url.startswith("http") else "")
        head = Paragraph(
            f"<b>Q.{q.qno}</b> <font color='#5F6368'>· {esc(section_short(q.section))}</font> · "
            f"<font color='{col.hexval().replace('0x', '#')}'><b>{q.status}</b> ({fmt(m)})</font>"
            f"<font size=7 color='#5F6368'> · ID {esc(q.question_id)}</font>{link}", CELL)
        rows = [[head, ""]]
        qflow = img_flow(q.q_img, inner_w, avail_h * 0.62) if q.q_img else None
        rows.append([qflow or Paragraph(esc(q.q_text) or "(question image missing)", BODY), ""])
        st = [("SPAN", (0, 0), (1, 0)), ("SPAN", (0, 1), (1, 1)),
              ("BACKGROUND", (0, 0), (1, 0), GREY_BG),
              ("BOX", (0, 0), (-1, -1), 0.6, LINE),
              ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
              ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6)]
        for o in q.options:
            is_key = o.number in q.correct
            is_mine = o.number in q.chosen
            if is_key and is_mine:
                label, fg, bg = f"{o.number}  ✓ Yours", GREEN, GREEN_BG
            elif is_key:
                label, fg, bg = f"{o.number}  Key", GREEN, GREEN_BG
            elif is_mine:
                label, fg, bg = f"{o.number}  ✗ Yours", RED, RED_BG
            else:
                label, fg, bg = str(o.number), GREY, None
            oflow = img_flow(o.img, inner_w - marker_w, avail_h * 0.3) if o.img else None
            r = len(rows)
            rows.append([Paragraph(f"<font color='{fg.hexval().replace('0x', '#')}'><b>{esc(label)}</b></font>", CELL),
                         oflow or Paragraph(esc(o.text), BODY)])
            st.append(("LINEABOVE", (0, r), (-1, r), 0.3, LINE))
            if bg is not None:
                st.append(("BACKGROUND", (0, r), (-1, r), bg))
                st.append(("LINEBEFORE", (0, r), (0, r), 3, fg))
        t = Table(rows, colWidths=[marker_w, avail_w - marker_w])
        t.setStyle(TableStyle(st))
        story.append(KeepTogether([t, Spacer(1, 8)]))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(GREY)
        canvas.drawString(margin, 8 * mm, f"{cand.name} · Roll {cand.roll_no} · For personal self-analysis only")
        canvas.drawRightString(page_w - margin, 8 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=margin, rightMargin=margin,
                            topMargin=margin, bottomMargin=margin + 4,
                            title=f"SSC report - {cand.name} {cand.roll_no}", author="ssc_report.py")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


# --------------------------------------------------------------------------
# OCR (paper style)
# --------------------------------------------------------------------------

def _ocr_job(job):
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    from ocr_text import ocr_image
    key, blob, single = job
    try:
        return key, ocr_image(blob, single_line=single)
    except Exception as e:  # never let one bad image stop the paper
        from ocr_text import OcrResult
        return key, OcrResult(False, "", "", 0, f"error {e}")


def run_ocr(questions, store, jobs=0):
    from multiprocessing import Pool
    todo, seen = [], set()
    for q in questions:
        for key, single in [(q.q_img, False)] + [(o.img, True) for o in q.options]:
            if key and key not in seen and store.get(key):
                seen.add(key)
                todo.append((key, store.get(key), single))
    n = jobs or os.cpu_count() or 2
    with Pool(n) as pool:
        return dict(pool.map(_ocr_job, todo, chunksize=8))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def gather_inputs(args):
    files = list(args.files)
    links = [u for u in re.split(r"[\s,]+", args.links or "") if u.startswith("http")]
    if not files and not links:
        for pat in ("*.mhtml", "*.mht", "*.html", "*.htm", "*.txt"):
            files += glob.glob(os.path.join(args.input_dir, pat))
    return sorted(set(files)), links


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help=".mhtml / .html response sheet files")
    ap.add_argument("--links", default="", help="response sheet link(s), separated by spaces or commas")
    ap.add_argument("--input-dir", default="input")
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--pos", type=float, default=1.0, help="marks per correct answer (default 1)")
    ap.add_argument("--neg", type=float, default=0.25, help="marks deducted per wrong answer (default 0.25)")
    ap.add_argument("--cards", choices=["all", "wrong"], default="all",
                    help="question pages in the PDF: all, or only wrong/unattempted")
    ap.add_argument("--style", choices=["paper", "report"], default="paper",
                    help="paper = two-column question paper with answers (default); report = score analysis")
    ap.add_argument("--show-yours", action="store_true", help="paper: also show your chosen option")
    ap.add_argument("--hide-candidate", action="store_true", help="paper: leave name and roll no. out of the header")
    ap.add_argument("--watermark", default="", help="paper: faint diagonal text on every page")
    ap.add_argument("--footer", default="", help="paper: small text at the bottom-left of every page")
    ap.add_argument("--jobs", type=int, default=0, help="parallel OCR workers (default: all CPUs)")
    ap.add_argument("--summary", default="", help="append a Markdown summary to this file (GitHub step summary)")
    args = ap.parse_args()

    files, links = gather_inputs(args)
    if not files and not links:
        sys.exit("No input. Put .mhtml files in the input/ folder or pass --links.")

    store = ImageStore()
    cand = Candidate()
    by_key = {}
    errors = []

    sources = [("file", f) for f in files] + [("link", u) for u in links]
    for kind, src in sources:
        try:
            if kind == "link":
                html, base = load_url(src, store)
            else:
                # decide by what's inside, not the name: a.txt, page.mhtml, x.html all work
                head = open(src, "rb").read(4096).lstrip()
                is_mhtml = head.startswith(b"From:") or b"MIME-Version" in head or b"multipart/related" in head
                if is_mhtml:
                    html, base = load_mhtml(src, store)
                else:
                    html, base = load_html_file(src, store)
        except Exception as e:
            errors.append(f"{src}: {e}")
            print(f"!! {src}: {e}", file=sys.stderr)
            continue
        soup = BeautifulSoup(html, "html.parser")
        c = parse_candidate(soup)
        for k, v in asdict(c).items():
            if v and not getattr(cand, k):
                setattr(cand, k, v)
        if cand.roll_no and c.roll_no and c.roll_no != cand.roll_no:
            errors.append(f"{src}: roll number {c.roll_no} differs from {cand.roll_no} — skipped")
            continue
        qs = parse_questions(soup, default_section=os.path.basename(src))
        keys = [k for q in qs for k in [q.q_img] + [o.img for o in q.options] if k]
        missing = sum(1 for k in keys if not store.get(k))
        if not qs:
            errors.append(f"{os.path.basename(src)}: no questions found — is this an SSC response sheet page?")
            continue
        if keys and missing > len(keys) / 2:
            errors.append(f"{os.path.basename(src)}: the question pictures aren't inside this file. "
                          "Save it again in Chrome with ⋮ → ↓ (download), which keeps the pictures.")
            continue
        print(f"{os.path.basename(src)[:60]}: {len(qs)} questions"
              + (f", {missing} images missing" if missing else ""))
        for q in qs:
            by_key[(q.section, q.qno)] = q  # later file wins on duplicates

    questions = sorted(by_key.values(), key=lambda q: (section_short(q.section), q.qno))
    if not questions:
        msg = "No questions found.\n\n" + "\n\n".join(errors)
        if args.summary:
            with open(args.summary, "a", encoding="utf-8") as f:
                f.write("## ❌ Report failed\n\n```\n" + msg + "\n```\n")
        sys.exit(msg)

    sections_order = []
    for q in questions:
        if q.section not in sections_order:
            sections_order.append(q.section)
    sections, tot = score(questions, args.pos, args.neg)
    sections = {k: sections[k] for k in sections_order}

    os.makedirs(args.out_dir, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9]+", "_", f"SSC_{cand.roll_no}_{cand.name}").strip("_") or "SSC_report"
    pdf_path = os.path.join(args.out_dir, stem + ".pdf")
    csv_path = os.path.join(args.out_dir, stem + ".csv")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Section", "Q.No", "Question ID", "Your option", "Correct option", "Result", "Marks", "Challenge link"])
        for q in questions:
            w.writerow([q.section, q.qno, q.question_id, ",".join(map(str, q.chosen)),
                        ",".join(map(str, q.correct)), q.status, q_marks(q, args.pos, args.neg), q.challenge_url])

    if args.style == "report":
        build_pdf(pdf_path, cand, questions, sections, tot, store, args.pos, args.neg, args.cards)
    else:
        from paper_pdf import build_paper
        ocr = run_ocr(questions, store, args.jobs)
        fails = [k for k, r in ocr.items() if not r.ok]
        print(f"OCR: {len(ocr) - len(fails)} of {len(ocr)} images read as text, {len(fails)} kept as pictures")
        pdf_path = os.path.join(args.out_dir, stem + "_paper.pdf")
        build_paper(pdf_path, cand, questions, store, ocr, show_yours=args.show_yours,
                    hide_candidate=args.hide_candidate, watermark=args.watermark,
                    footer_text=args.footer, pos=args.pos, neg=args.neg)

    lines = [f"## {cand.name} · Roll {cand.roll_no}", "",
             f"**Total: {tot['marks']:.2f} / {tot['max']:.0f}** (raw, +{args.pos:g} / −{args.neg:g})", "",
             "| Section | Correct | Wrong | Not attempted | Marks |", "|---|---|---|---|---|"]
    for name, s in sections.items():
        lines.append(f"| {name} | {s['Correct']} | {s['Wrong']} | {s['Not attempted']} | {s['marks']:.2f} / {s['max']:.0f} |")
    wrong = [str(q.qno) for q in questions if q.status == "Wrong"]
    lines += ["", f"Wrong: {', '.join(wrong) or 'none'}", "",
              "Download the PDF from **Artifacts** at the bottom of this page."]
    if errors:
        lines += ["", "**Problems:**"] + [f"- {e}" for e in errors]
    summary = "\n".join(lines)
    print("\n" + summary)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as f:
            f.write(summary + "\n")
    print(f"\nPDF: {pdf_path}\nCSV: {csv_path}")


if __name__ == "__main__":
    main()
