"""
Picture helpers for bilingual papers.

- lang_of(src)          'en' / 'hi' / None from the SSC image name (..._EN.jpg, ..._HI.jpg)
- pick_images(...)      one language per cell + drop duplicate copies of the same picture
- trim(blob)            cut the empty white border off a picture
- reflow(blob, ...)     re-wrap a wide text picture (Hindi) into the column so it stays readable
"""

import io
import re

import numpy as np
from PIL import Image

_LANG_RE = re.compile(r"[_\-.](EN|ENG|ENGLISH|HI|HN|HIN|HINDI)(?:[_\-.]\d+)?\.(?:jpe?g|png|gif|bmp|webp)$", re.I)


def lang_of(src):
    if not src:
        return None
    path = re.split(r"[?#]", src)[0]
    m = _LANG_RE.search(path)
    if not m:
        return None
    return "en" if m.group(1).upper().startswith("E") else "hi"


def _gray(blob):
    try:
        im = Image.open(io.BytesIO(blob))
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, "white")
            im = Image.alpha_composite(bg, im)
        return np.array(im.convert("L"))
    except Exception:
        return None


def _bbox(a, thr=170):
    ink = a < thr
    rows, cols = np.where(ink.any(axis=1))[0], np.where(ink.any(axis=0))[0]
    if not len(rows):
        return None
    return rows[0], rows[-1] + 1, cols[0], cols[-1] + 1


def _sig(blob):
    a = _gray(blob) if blob else None
    if a is None:
        return None
    b = _bbox(a)
    if b is None:
        return None
    y0, y1, x0, x1 = b
    c = a[y0:y1, x0:x1]
    thumb = np.array(Image.fromarray(c).resize((24, 24), Image.BILINEAR), dtype=float)
    return (x1 - x0) / max(1, y1 - y0), thumb


def _same(s1, s2):
    if s1 is None or s2 is None:
        return False
    r1, t1 = s1
    r2, t2 = s2
    if abs(r1 - r2) > 0.08 * max(r1, r2):
        return False
    return float(np.mean(np.abs(t1 - t2))) < 10.0


def pick_images(srcs, store, lang="en"):
    """Keep one language of a bilingual cell and drop repeated copies of the same picture."""
    srcs = [s for s in (srcs or []) if s]
    if lang in ("en", "hi"):
        tags = [lang_of(s) for s in srcs]
        if any(t == lang for t in tags):
            srcs = [s for s, t in zip(srcs, tags) if t in (lang, None)]
        # only the other language exists -> keep it (e.g. a Hindi-only section)
    out, sigs = [], []
    for s in srcs:
        sig = _sig(store.get(s))
        if any(_same(sig, t) for t in sigs):
            continue
        out.append(s)
        sigs.append(sig)
    return out


def trim(blob, pad=4):
    """PNG bytes of the picture without its empty border (or the original if nothing to cut)."""
    a = _gray(blob)
    if a is None:
        return blob
    b = _bbox(a)
    if b is None:
        return blob
    y0, y1, x0, x1 = b
    h, w = a.shape
    y0, x0 = max(0, y0 - pad), max(0, x0 - pad)
    y1, x1 = min(h, y1 + pad), min(w, x1 + pad)
    if (y1 - y0) * (x1 - x0) > 0.92 * h * w:
        return blob
    buf = io.BytesIO()
    Image.open(io.BytesIO(blob)).convert("RGB").crop((x0, y0, x1, y1)).save(buf, "PNG")
    return buf.getvalue()


def _runs(mask, min_gap):
    """[(start, end)] of True runs, joining runs separated by fewer than min_gap False cells."""
    idx = np.where(mask)[0]
    if not len(idx):
        return []
    runs, s, prev = [], idx[0], idx[0]
    for i in idx[1:]:
        if i - prev > min_gap:
            runs.append((s, prev + 1))
            s = i
        prev = i
    runs.append((s, prev + 1))
    return runs


def line_height(blob, thr=160):
    """Median height (px) of the text lines in a picture, or None."""
    a = _gray(blob)
    if a is None:
        return None
    lines = _runs((a < thr).sum(axis=1) > 0, 1)
    hs = sorted(e - s for s, e in lines if e - s >= 4)
    return hs[len(hs) // 2] if hs else None


def reflow(blob, max_w_pt, band_pt=11.0, scale=None, lo_pt=None, hi_pt=None, thr=160):
    """
    Re-wrap a text picture so every line fits max_w_pt. scale = pt per pixel (use the same for the
    whole paper so all Hindi text comes out the same size); default: lines become band_pt tall.
    Returns (png_bytes, width_pt, height_pt), or None when the picture doesn't look like plain text lines.
    """
    a = _gray(blob)
    if a is None:
        return None
    b = _bbox(a, thr)
    if b is None:
        return None
    y0, y1, x0, x1 = b
    a = a[y0:y1, x0:x1]
    ink = a < thr
    H, W = ink.shape

    rows = ink.sum(axis=1) > 0
    lines = _runs(rows, 1)
    if not lines:
        return None
    hs = sorted(e - s for s, e in lines)
    med = hs[len(hs) // 2]
    # join matra / dot fragments that sit just above or below a line
    merged = []
    for s, e in lines:
        if merged and (e - s < 0.45 * med or merged[-1][1] - merged[-1][0] < 0.45 * med) and s - merged[-1][1] <= max(3, 0.3 * med):
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    lines = merged
    hs = sorted(e - s for s, e in lines)
    med = max(4, hs[len(hs) // 2])
    if max(hs) > 2.4 * med or med < 6:
        return None                 # figure / table / stacked maths -> leave the picture alone

    if scale is None:
        scale = band_pt / med           # pt per pixel
    elif lo_pt or hi_pt:                # the paper-wide scale, kept within sane line heights
        scale = min(max(scale, (lo_pt or 0) / med), (hi_pt or 1e9) / med)
    avail = int(max_w_pt / scale)
    if W <= avail:                  # fits already: just draw it bigger
        buf = io.BytesIO()
        Image.fromarray(a).save(buf, "PNG")
        return buf.getvalue(), W * scale, H * scale

    word_gap = max(2, int(0.26 * med))
    space = int(0.35 * med)
    right = max(int(np.where(ink[s:e].any(axis=0))[0][-1]) for s, e in lines)

    # words of every source line, with the line's anchor row (heaviest row = Hindi headline)
    src = []
    prev_end = None
    for s, e in lines:
        band = ink[s:e]
        cols = band.any(axis=0)
        words = _runs(cols, word_gap)
        prof = band.sum(axis=1)
        top_part = prof[: max(1, int(len(prof) * 0.7))]
        anchor = int(np.argmax(top_part))
        para = prev_end is not None and (s - prev_end) > 1.1 * med
        src.append(dict(s=s, e=e, anchor=anchor, words=words, para=para,
                        full=bool(words) and words[-1][1] >= 0.85 * right))
        prev_end = e
    # a line only "runs on" into the next one when the picture clearly holds a wrapped paragraph
    if sum(L["full"] for L in src) < 2:
        for L in src:
            L["full"] = False

    def gap_between(p, q):
        """keep the original spacing between pieces of the same line (a false split stays invisible)"""
        return q[1] - p[2] if p[0] is q[0] and q[1] > p[2] else space

    out_lines, cur, cur_w = [], [], 0
    for li, L in enumerate(src):
        if L["para"] and cur:
            out_lines.append((cur, True))
            cur, cur_w = [], 0
        for wx0, wx1 in L["words"]:
            w = wx1 - wx0
            need = w + (gap_between(cur[-1], (L, wx0, wx1)) if cur else 0)
            if cur and cur_w + need > avail:
                out_lines.append((cur, False))
                cur, cur_w = [], 0
                need = w
            cur.append((L, wx0, wx1))
            cur_w += need
        if not L["full"] and cur:     # short source line = the author broke the line here
            out_lines.append((cur, False))
            cur, cur_w = [], 0
    if cur:
        out_lines.append((cur, False))

    lead = int(0.28 * med)
    placed, total_h, out_w = [], 0, 0
    for ws, _ in out_lines:
        up = max(L["anchor"] for L, _, _ in ws)
        down = max((L["e"] - L["s"]) - L["anchor"] for L, _, _ in ws)
        x = 0
        items = []
        prev = None
        for piece in ws:
            L, wx0, wx1 = piece
            if prev is not None:
                x += gap_between(prev, piece)
            items.append((x, up - L["anchor"], L, wx0, wx1))
            x += wx1 - wx0
            prev = piece
        out_w = max(out_w, x)
        placed.append((total_h, items))
        total_h += up + down + lead
    total_h -= lead
    canvas = Image.new("L", (max(1, out_w), max(1, total_h)), 255)
    for top, items in placed:
        for x, dy, L, wx0, wx1 in items:
            crop = Image.fromarray(a[L["s"]:L["e"], wx0:wx1])
            canvas.paste(crop, (x, top + dy))
    # safety: nothing may get lost
    if (np.array(canvas) < thr).sum() < 0.97 * ink.sum():
        return None
    buf = io.BytesIO()
    canvas.save(buf, "PNG")
    return buf.getvalue(), out_w * scale, total_h * scale
