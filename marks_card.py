"""
Marks calculation photo (PNG scorecard) for Telegram.
"""

import os
import re

from PIL import Image, ImageDraw, ImageFont

_FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/truetype/liberation",
              "/usr/share/fonts/truetype/noto"]


def _font(bold, size):
    names = (["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "NotoSans-Bold.ttf"] if bold
             else ["DejaVuSans.ttf", "LiberationSans-Regular.ttf", "NotoSans-Regular.ttf"])
    for d in _FONT_DIRS:
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _rgb(h, default):
    h = (h or "").lstrip("#")
    if re.fullmatch(r"[0-9A-Fa-f]{6}", h):
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    return default


def _section_label(name):
    m = re.match(r"\s*(PART-[A-Z])\s*\((.*)\)\s*$", name, re.I)
    if m:
        return m.group(1).upper(), m.group(2)
    return "", name


def _fit(draw, text, font_bold, size, max_w, min_size=18):
    while size > min_size:
        f = _font(font_bold, size)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 2
    return _font(font_bold, min_size)


def make_marks_card(path, cand, sections, tot, *, settings=None, show_candidate=True, pos=1.0, neg=0.25):
    S = settings or {}
    ACC = _rgb(S.get("accent_color"), (31, 78, 158))
    GREEN, RED, GREY, INK = (30, 125, 52), (198, 40, 40), (110, 110, 110), (27, 27, 27)
    BG, CARD, LINE = (243, 245, 248), (255, 255, 255), (222, 226, 232)

    W = 1080
    rows = list(sections.items())
    H = 560 + 118 * len(rows) + (70 if show_candidate else 0) + 150
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    pad = 48

    # header band
    d.rectangle([0, 0, W, 210], fill=ACC)
    title = S.get("header_title") or "Staff Selection Commission"
    d.text((pad, 36), title, font=_fit(d, title, True, 44, W - 2 * pad), fill=(255, 255, 255))
    exam = cand.exam or "SSC Examination"
    d.text((pad, 98), exam, font=_fit(d, exam, False, 32, W - 2 * pad), fill=(230, 236, 250))
    when = "  ·  ".join(x for x in (cand.test_date.title() if cand.test_date else "", cand.shift) if x)
    d.text((pad, 146), when, font=_font(False, 28), fill=(210, 220, 245))

    y = 240
    if show_candidate and (cand.name or cand.roll_no):
        d.text((pad, y), cand.name or "", font=_font(True, 34), fill=INK)
        roll = f"Roll No. {cand.roll_no}" if cand.roll_no else ""
        f = _font(False, 28)
        d.text((W - pad - d.textlength(roll, font=f), y + 4), roll, font=f, fill=GREY)
        y += 70

    # total card
    d.rounded_rectangle([pad, y, W - pad, y + 200], radius=26, fill=CARD, outline=LINE, width=2)
    d.text((pad + 36, y + 26), "TOTAL SCORE", font=_font(True, 26), fill=GREY)
    big = f"{tot['marks']:.2f}"
    fb = _font(True, 96)
    d.text((pad + 36, y + 64), big, font=fb, fill=ACC)
    bw = d.textlength(big, font=fb)
    d.text((pad + 48 + bw, y + 112), f"/ {tot['max']:.0f}", font=_font(False, 40), fill=GREY)
    stats = [("Correct", tot["Correct"], GREEN), ("Wrong", tot["Wrong"], RED), ("Not attempted", tot["Not attempted"], GREY)]
    sx = W - pad - 36
    for label, val, col in reversed(stats):
        fv, fl = _font(True, 44), _font(False, 22)
        w = max(d.textlength(str(val), font=fv), d.textlength(label, font=fl))
        d.text((sx - w, y + 56), str(val), font=fv, fill=col)
        d.text((sx - w, y + 116), label, font=fl, fill=GREY)
        sx -= w + 44
    y += 236

    # section table
    d.text((pad, y), "Section-wise marks", font=_font(True, 30), fill=INK)
    y += 54
    cols = [("Section", pad + 24), ("Att.", 560), ("✓", 670), ("✗", 760), ("—", 850), ("Marks", 930)]
    for label, x in cols:
        col = GREEN if label == "✓" else RED if label == "✗" else GREY
        d.text((x, y), label, font=_font(True, 24), fill=col)
    y += 44
    for name, s in rows:
        d.rounded_rectangle([pad, y, W - pad, y + 102], radius=18, fill=CARD, outline=LINE, width=2)
        part, title_ = _section_label(name)
        if part:
            d.text((pad + 24, y + 14), part, font=_font(True, 24), fill=ACC)
        d.text((pad + 24, y + (48 if part else 30)), title_, font=_fit(d, title_, False, 26, 560 - pad - 40, 16), fill=INK)
        cy = y + 34
        for val, x, col in ((s["attempted"], 560, INK), (s["Correct"], 670, GREEN), (s["Wrong"], 760, RED),
                            (s["Not attempted"], 850, GREY)):
            d.text((x, cy), str(val), font=_font(True, 30), fill=col)
        d.text((930, cy - 10), f"{s['marks']:.2f}", font=_font(True, 32), fill=INK)
        d.text((930, cy + 30), f"/ {s['max']:.0f}", font=_font(False, 20), fill=GREY)
        y += 118

    # footer
    y += 10
    note = f"+{pos:g} for correct  ·  −{neg:g} for wrong  ·  raw score before normalisation"
    d.text((pad, y), note, font=_font(False, 22), fill=GREY)
    y += 44
    ch = S.get("channel_name") or ""
    link = (S.get("channel_link") or "").replace("https://", "")
    if ch or link:
        d.rounded_rectangle([pad, y, W - pad, y + 64], radius=32, fill=ACC)
        txt = f"Join {ch}  ·  {link}" if ch and link else (ch or link)
        f = _fit(d, txt, True, 28, W - 2 * pad - 40)
        d.text(((W - d.textlength(txt, font=f)) / 2, y + 16), txt, font=f, fill=(255, 255, 255))
        y += 64
    im = im.crop((0, 0, W, min(H, y + 40)))
    im.save(path, optimize=True)
    return path
