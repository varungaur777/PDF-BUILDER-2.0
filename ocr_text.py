"""
OCR for SSC question/option images.

Turns a question image into lightweight markup that reportlab's Paragraph
understands: <b>bold</b>, <u>underlined</u>, "______" for blanks, and <br/>
between lines that were separate in the original. Images that aren't plain
text (figures, fractions, tables) are reported as not-text so the caller can
embed the picture instead.
"""

import io
import os
import re
from dataclasses import dataclass

import numpy as np
import pytesseract
from PIL import Image, ImageOps

def _find_tesseract():
    """Windows/Mac installers often don't add tesseract to PATH; look in the usual places."""
    import shutil
    if shutil.which("tesseract"):
        return
    env = os.environ.get("TESSERACT_CMD", "")
    candidates = [env] if env else []
    candidates += [r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                   r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                   os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
                   "/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"]
    for c in candidates:
        if c and os.path.exists(c):
            pytesseract.pytesseract.tesseract_cmd = c
            return


_find_tesseract()

PAD = 20
SCALE = 3
TESS_CFG = "--oem 1 -c load_system_dawg=0 -c load_freq_dawg=0 -c preserve_interword_spaces=0"


@dataclass
class OcrResult:
    ok: bool            # False -> embed the image instead
    markup: str         # reportlab Paragraph markup
    plain: str          # plain text (for comparing passages)
    conf: float
    reason: str = ""
    min_conf: float = 100.0


def _prep(blob, scale=None):
    scale = scale or SCALE
    im = Image.open(io.BytesIO(blob)).convert("L")
    im = ImageOps.expand(im, PAD, fill=255)
    im = im.resize((im.width * scale, im.height * scale), Image.LANCZOS)
    return im


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _horizontal_segments(dark, min_len, max_thick):
    """Long, thin horizontal strokes (blanks and underlines)."""
    h, w = dark.shape
    k = min_len
    # a pixel is 'line' if the k-wide window starting there is all dark
    c = np.cumsum(np.pad(dark.astype(np.int32), ((0, 0), (1, 0))), axis=1)
    full = (c[:, k:] - c[:, :-k]) == k          # shape (h, w-k+1)
    line = np.zeros_like(dark, dtype=bool)
    for dx in range(k):                         # dilate back to full width
        line[:, dx:dx + full.shape[1]] |= full
    segs = []
    rows = np.where(line.any(axis=1))[0]
    if rows.size == 0:
        return segs
    # group consecutive rows
    groups, start = [], rows[0]
    for a, b in zip(rows, rows[1:]):
        if b != a + 1:
            groups.append((start, a)); start = b
    groups.append((start, rows[-1]))
    for y0, y1 in groups:
        if y1 - y0 + 1 > max_thick:
            continue
        band = line[y0:y1 + 1].any(axis=0)
        xs = np.where(band)[0]
        # split into runs along x
        run_start = xs[0]
        for a, b in zip(xs, xs[1:]):
            if b != a + 1:
                segs.append((run_start, a, y0, y1)); run_start = b
        segs.append((run_start, xs[-1], y0, y1))
    return segs


def _stroke_width(dark_box):
    """Median horizontal run length of ink inside a word box ~ stroke width."""
    runs = []
    for row in dark_box:
        if not row.any():
            continue
        d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
        s, e = np.where(d == 1)[0], np.where(d == -1)[0]
        runs.extend((e - s).tolist())
    if not runs:
        return 0.0
    return float(np.median(runs))


def _letter_order(im):
    """Options like 'B – A – C – D' confuse normal OCR; read letters only."""
    free = pytesseract.image_to_string(im, config="--psm 7").strip()
    letters = re.findall(r"[A-Za-z]", free)
    if len(re.findall(r"[A-H]", free)) < 3 or len(re.findall(r"[A-H]", free)) < 0.6 * len(letters):
        return None                                   # not an order option (figure, Hindi, words…)
    t = pytesseract.image_to_string(im, config="--psm 7 -c tessedit_char_whitelist=ABCDEFGH").strip()
    if 3 <= len(t) <= 8 and len(set(t)) == len(t):
        return t
    return None


_HIN = None


def _hindi_available():
    global _HIN
    if _HIN is None:
        try:
            _HIN = "hin" in pytesseract.get_languages(config="")
        except Exception:
            _HIN = False
    return _HIN


def devanagari_score(blob):
    """(number of Hindi letters, share of Hindi among all letters) read in the image."""
    if not _hindi_available():
        return 0, 0.0
    t = pytesseract.image_to_string(_prep(blob, 2), lang="eng+hin", config="--psm 6")
    n = len(re.findall(r"[\u0900-\u097F]", t))
    return n, n / max(1, len(re.sub(r"\s", "", t)))


def has_devanagari(blob, strict=False):
    """True if the image contains Hindi (Devanagari) text. strict: for pictures that may be figures."""
    n, share = devanagari_score(blob)
    return (n >= 4 and share >= 0.7) if strict else n >= 3


def ocr_image(blob, single_line=False):
    """single_line=True for option images (enables the letter-order fallback)."""
    r = _ocr_image(blob, allow_bold=not single_line)
    if not r.ok or r.conf < 80:          # retry once at another resolution
        r2 = _ocr_image(blob, allow_bold=not single_line, scale=2 if SCALE != 2 else 4)
        if r2.ok and (not r.ok or r2.conf > r.conf):
            r = r2
    # Hindi / bilingual text can't be typed back correctly -> keep the original picture
    if r.ok and (r.conf < 93 or r.min_conf < 60) and has_devanagari(blob):
        return OcrResult(False, "", "", r.conf, "hindi")
    if not r.ok and re.match(r"(low confidence|no text)", r.reason or "") and has_devanagari(blob):
        return OcrResult(False, "", "", r.conf, "hindi")
    if not r.ok and (r.reason or "").startswith("non-text ink") and r.conf < 60 and has_devanagari(blob, strict=True):
        return OcrResult(False, "", "", r.conf, "hindi")
    # number series / figures read with doubt: a wrong digit is worse than the picture
    if r.ok:
        shaky = (r.conf < 85 or r.min_conf < 50) if single_line else (r.conf < 93 or r.min_conf < 70)
        numeric = False
        for line in re.split(r"<br/>", r.markup or ""):
            alnum = re.sub(r"[^0-9A-Za-z]", "", re.sub(r"<[^>]+>", "", line))
            digits = sum(c.isdigit() for c in alnum)
            garbled = re.search(r"\d[,.;:]{2,}", line)
            if digits >= 4 and (digits >= 0.5 * len(alnum) or garbled):
                numeric = numeric or bool(garbled) or shaky
        if numeric:
            return OcrResult(False, "", "", r.conf, "numbers")
    if single_line and (not r.ok or r.conf < 88 or re.fullmatch(r"[A-H\s\-–—=]{5,}", r.plain or "")):
        letters = _letter_order(_prep(blob))
        if letters:
            dashed = (not r.ok) or bool(re.search(r"[-–—=]", r.plain or ""))
            txt = " – ".join(letters) if dashed else letters
            return OcrResult(True, txt, txt, 90.0, "letter order")
    return r


def _ocr_image(blob, single_line=False, allow_bold=True, scale=None):
    im = _prep(blob, scale)
    arr = np.array(im)
    dark = arr < 140
    total_ink = int(dark.sum())
    if total_ink == 0:
        return OcrResult(False, "", "", 0, "blank image")

    psm = 7 if single_line else 6
    d = pytesseract.image_to_data(im, config=f"--psm {psm} {TESS_CFG}", output_type=pytesseract.Output.DICT)
    words = []
    for i, txt in enumerate(d["text"]):
        t = txt.strip()
        if not t:
            continue
        words.append(dict(t=t, x=d["left"][i], y=d["top"][i], w=d["width"][i], h=d["height"][i],
                          conf=float(d["conf"][i]), key=(d["block_num"][i], d["par_num"][i], d["line_num"][i])))
    if not words:
        return OcrResult(False, "", "", 0, "no text")

    heights = sorted(w["h"] for w in words)
    xh = heights[len(heights) // 2] or 30                    # typical word height

    # drop OCR tokens that are just blank/underline strokes
    words = [w for w in words if not re.fullmatch(r"[_\-—–=~.]{3,}", w["t"])]

    segs = _horizontal_segments(dark, min_len=max(int(xh * 1.6), 40), max_thick=max(int(xh * 0.25), 4))

    # how much ink is explained by words + strokes? (figures/maths leave a lot unexplained)
    covered = np.zeros_like(dark)
    for w in words:
        covered[max(0, w["y"] - 4):w["y"] + w["h"] + 4, max(0, w["x"] - 4):w["x"] + w["w"] + 4] = True
    for x0, x1, y0, y1 in segs:
        covered[y0 - 2:y1 + 3, x0:x1 + 1] = True
    unexplained = (dark & ~covered).sum() / total_ink
    conf = float(np.mean([w["conf"] for w in words])) if words else 0.0
    tall_words = sum(1 for w in words if w["h"] > xh * 2.2)

    if unexplained > 0.08:
        return OcrResult(False, "", "", conf, f"non-text ink {unexplained:.0%}")
    if conf < 75:
        return OcrResult(False, "", "", conf, f"low confidence {conf:.0f}")
    if tall_words >= 2:
        return OcrResult(False, "", "", conf, "stacked/maths layout")

    # classify strokes: underline (ink just above it) vs blank (empty above)
    underlines, blanks = [], []
    for x0, x1, y0, y1 in segs:
        above = dark[max(0, y0 - int(xh * 0.9)):max(0, y0 - 3), x0:x1 + 1]
        fill = above.mean() if above.size else 0
        (underlines if fill > 0.06 else blanks).append((x0, x1, y0, y1))

    for w in words:
        cx0, cx1 = w["x"], w["x"] + w["w"]
        bottom = w["y"] + w["h"]
        w["u"] = any(min(cx1, x1) - max(cx0, x0) > 0.5 * w["w"] and -xh * 0.3 < y0 - bottom < xh * 0.6
                     for x0, x1, y0, y1 in underlines)
        w["sw"] = _stroke_width(dark[w["y"]:bottom, cx0:cx1])

    # group into lines (tesseract's own line ids)
    lines = {}
    for w in words:
        lines.setdefault(w["key"], []).append(w)
    line_list = sorted(lines.values(), key=lambda ws: (min(w["y"] for w in ws), min(w["x"] for w in ws)))

    # insert blanks into the line whose vertical band contains them
    for x0, x1, y0, y1 in blanks:
        best, bestd = None, None
        for ws in line_list:
            top = min(w["y"] for w in ws); bot = max(w["y"] + w["h"] for w in ws)
            if top - xh * 0.5 <= y0 <= bot + xh * 0.6:
                dd = abs(y0 - bot)
                if bestd is None or dd < bestd:
                    best, bestd = ws, dd
        tok = dict(t="______", x=x0, y=y0 - xh, w=x1 - x0, h=xh, conf=100, u=False, sw=0, blank=True)
        if best is None:
            line_list.append([tok])
        else:
            best.append(tok)
    line_list = sorted(line_list, key=lambda ws: min(w["y"] for w in ws))

    # bold: stroke width well above the image's typical stroke width
    sws = sorted(w["sw"] for ws in line_list for w in ws if w["sw"] > 0 and len(w["t"]) >= 3)
    base_sw = sws[len(sws) // 3] if (sws and allow_bold and len(sws) >= 10) else 0
    for ws in line_list:
        for w in ws:
            w["b"] = bool(base_sw) and w["sw"] >= base_sw * 1.5 and not w.get("blank")
        real = [w for w in ws if not w.get("blank")]
        if real and len(real) >= 4 and sum(len(w["t"]) for w in real if w["b"]) >= 0.34 * sum(len(w["t"]) for w in real):
            for w in real:            # a mostly-bold line is a bold line
                w["b"] = True

    right_edge = max(max(w["x"] + w["w"] for w in ws) for ws in line_list)
    left_edge = min(min(w["x"] for w in ws) for ws in line_list)
    out, plain = [], []
    prev_bottom = None
    for i, ws in enumerate(line_list):
        ws.sort(key=lambda w: w["x"])
        top = min(w["y"] for w in ws)
        if prev_bottom is not None:
            gap = top - prev_bottom
            wrapped = out and out[-1][1]
            if gap > xh * 1.1:
                out.append(("<br/><br/>", False))
            elif not wrapped:
                out.append(("<br/>", False))
            else:
                out.append((" ", False))
        starts_item = bool(re.match(r"^(\(?[A-Ha-h1-9ivx]{1,3}[\).:]|Q\.?\s*\d)", ws[0]["t"]))
        if starts_item and out and out[-1][0] == " ":
            out[-1] = ("<br/>", False)
        prev_bottom = max(w["y"] + w["h"] for w in ws)
        parts = []
        for w in ws:
            t = _esc(w["t"])
            if w.get("blank"):
                t = "______"
            if w["u"]:
                t = f"<u>{t}</u>"
            if w["b"]:
                t = f"<b>{t}</b>"
            parts.append(t)
            plain.append(w["t"])
        text = " ".join(parts)
        text = re.sub(r"</b> <b>", " ", text)
        text = re.sub(r"</u> <u>", " ", text)
        text = re.sub(r"______ \(", "______(", text)
        line_right = max(w["x"] + w["w"] for w in ws)
        is_wrapped = (line_right - left_edge) > 0.86 * (right_edge - left_edge) and len(line_list) > 1
        out.append((text, is_wrapped))
    markup = "".join(t for t, _ in out)
    markup = re.sub(r"(<br/>)+$", "", markup)
    markup = re.sub(r"\s+[‘’'`|]$", "", markup)   # stray tick after a trailing blank
    markup = re.sub(r"^([a-z])", lambda m: m.group(1).upper(), markup)
    min_conf = min((w["conf"] for w in words), default=0.0)
    return OcrResult(True, markup, " ".join(plain), conf, "", min_conf)
