"""
Marks calculation photo (PNG) — answer-key style table card:
title, candidate details, section table (Total | NA | Right | Wrong | Marks), yellow total row.
"""

import os
import re

from PIL import Image, ImageDraw, ImageFont

_HERE = os.path.dirname(os.path.abspath(__file__))
_FONT_DIRS = [os.path.join(_HERE, "..", "fonts"), os.path.join(_HERE, "fonts"),
              "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/truetype/liberation",
              "/usr/share/fonts/truetype/noto", r"C:\Windows\Fonts", "/Library/Fonts",
              "/System/Library/Fonts/Supplemental"]
_cache = {}
S = 2                                   # draw at 2x, shrink at the end = crisp text


def _font(bold, size):
    key = (bold, size)
    if key not in _cache:
        names = (["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"] if bold
                 else ["DejaVuSans.ttf", "LiberationSans-Regular.ttf", "arial.ttf", "Arial.ttf"])
        f = None
        for d in _FONT_DIRS:
            for n in names:
                p = os.path.join(d, n)
                if os.path.exists(p):
                    f = ImageFont.truetype(p, int(size * S))
                    break
            if f:
                break
        _cache[key] = f or ImageFont.load_default()
    return _cache[key]


def _num(x):
    s = f"{x:.2f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _mask(v, keep=4):
    v = (v or "").strip()
    return ("X" * max(0, len(v) - keep) + v[-keep:]) if len(v) > keep else v


def _section_name(name):
    m = re.match(r"\s*PART-[A-Z]+\s*\((.*)\)\s*$", name or "", re.I)
    return (m.group(1) if m else name).strip()


def make_marks_card(path, cand, sections, tot, *, settings=None, show_candidate=True, pos=1.0, neg=0.25):
    St = settings or {}
    W = 1100
    RED, DARK = (198, 40, 40), (160, 20, 20)
    YELLOW, GRID, INK, GREY = (255, 238, 0), (120, 120, 120), (20, 20, 20), (90, 90, 90)
    ch = (St.get("channel_name") or "").strip()
    link = (St.get("channel_link") or "").replace("https://", "")

    im = Image.new("RGB", (W * S, 2400 * S), "white")
    d = ImageDraw.Draw(im)

    def T(x, y, t, f, fill=INK, anchor="la"):
        d.text((x * S, y * S), t, font=f, fill=fill, anchor=anchor)

    def R(x0, y0, x1, y1, fill=None, outline=GRID, width=1):
        d.rectangle([x0 * S, y0 * S, x1 * S, y1 * S], fill=fill, outline=outline, width=width * S)

    def wrap(t, f, max_w):
        words, lines, cur = t.split(), [], ""
        for w in words:
            nxt = (cur + " " + w).strip()
            if d.textlength(nxt, font=f) <= max_w * S or not cur:
                cur = nxt
            else:
                lines.append(cur)
                cur = w
        return lines + [cur] if cur else lines

    # ---- title: "<Exam>:" on one line, "Answer Key and Analysis by <channel>" on the next
    exam = cand.exam or "SSC Examination"
    ft = _font(True, 30)
    y = 26
    for line in wrap(exam + ":", ft, W - 120) + ["Answer Key and Analysis" + (f" by {ch}" if ch else "")]:
        T(W / 2, y, line, ft, INK, "ma")
        y += 40
    y += 14

    # ---- card
    x0, x1 = 90, W - 90
    top = y
    # header: board name only (exam is in the Subject row)
    hdr = St.get("header_title") or "Staff Selection Commission"
    T(W / 2, y + 26, hdr.upper(), _font(True, 28), INK, "ma")
    y += 80

    # details
    date = " ".join(w.capitalize() if w.isalpha() else w for w in (cand.test_date or "").split())
    rows = []
    if show_candidate:
        if cand.roll_no:
            rows.append(("Roll Number", _mask(cand.roll_no)))
        if cand.name:
            rows.append(("Candidate Name", cand.name))
    rows += [("Exam Date", date), ("Exam Time", cand.shift), ("Subject", exam)]
    rows = [(k, v) for k, v in rows if v]
    split = x0 + 300
    fl, fv = _font(True, 23), _font(False, 23)
    for k, v in rows:
        lines = wrap(v, fv, x1 - split - 30)
        h = max(62, 20 + 32 * len(lines))
        R(x0, y, split, y + h)
        R(split, y, x1, y + h)
        T(x0 + 16, y + h / 2, k, fl, INK, "lm")
        for i, line in enumerate(lines):
            T(split + 16, y + h / 2 + (i - (len(lines) - 1) / 2) * 32, line, fv, INK, "lm")
        y += h

    # section table
    cols = [("Section", 350), ("Total", 112), ("NA", 86), ("Right", 112), ("Wrong", 120)]
    used = sum(w for _, w in cols)
    cols.append(("Marks", (x1 - x0) - used))
    fh = _font(True, 24)
    h = 64
    x = x0
    for name, w in cols:
        R(x, y, x + w, y + h, fill=RED, outline=DARK)
        T(x + w / 2, y + h / 2, name, fh, (255, 255, 255), "mm")
        x += w
    y += h
    fc = _font(False, 23)
    for name, s in sections.items():
        lines = wrap(_section_name(name), fc, cols[0][1] - 28)
        h = max(64, 22 + 32 * len(lines))
        vals = [None, str(s["total"]), str(s["Not attempted"]), str(s["Correct"]), str(s["Wrong"]), _num(s["marks"])]
        x = x0
        for (cname, w), v in zip(cols, vals):
            R(x, y, x + w, y + h)
            if v is None:
                for i, line in enumerate(lines):
                    T(x + 16, y + h / 2 + (i - (len(lines) - 1) / 2) * 32, line, fc, INK, "lm")
            else:
                T(x + w / 2, y + h / 2, v, fc, INK, "mm")
            x += w
        y += h
    # total row
    h = 66
    vals = ["Total", str(tot["total"]), str(tot["Not attempted"]), str(tot["Correct"]), str(tot["Wrong"]), _num(tot["marks"])]
    x = x0
    fb = _font(True, 25)
    for (cname, w), v in zip(cols, vals):
        R(x, y, x + w, y + h, fill=YELLOW)
        T(x + w / 2, y + h / 2, v, fb, INK, "mm")
        x += w
    y += h
    # outer red border around the whole card
    d.rectangle([x0 * S, top * S, x1 * S, y * S], outline=DARK, width=4 * S)

    # watermark across the card
    if ch:
        wm = Image.new("RGBA", im.size, (0, 0, 0, 0))
        wd = ImageDraw.Draw(wm)
        fw = _font(True, 110)
        wd.text(((W / 2) * S, ((top + y) / 2) * S), ch, font=fw, fill=(220, 60, 60, 26), anchor="mm")
        wm = wm.rotate(28, center=((W / 2) * S, ((top + y) / 2) * S))
        im = Image.alpha_composite(im.convert("RGBA"), wm).convert("RGB")
        d = ImageDraw.Draw(im)

    # footer
    y += 22
    note = f"Marking Scheme: +{_num(pos)} for Correct, −{_num(neg)} for Incorrect"
    T(W / 2, y, note, _font(False, 21), GREY, "ma")
    y += 34
    if ch or link:
        T(W / 2, y, f"Join {ch}  •  {link}" if ch and link else (ch or link), _font(True, 22), DARK, "ma")
        y += 34
    y += 18
    im = im.crop((0, 0, W * S, y * S)).resize((W, y), Image.LANCZOS)
    im.save(path, optimize=True)
    return path
