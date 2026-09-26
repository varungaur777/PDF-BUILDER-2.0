"""
Bot settings: colours, watermark, channel name/link, heading.

Stored in settings.json next to this file. Admins change them from Telegram with
    /set watermark @NotesHubX
    /set question_color #B3261E
    /settings        (show all)
    /reset           (back to defaults)
On GitHub Actions the change is committed to the repo so the next run uses it.
"""

import json
import os
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.environ.get("SETTINGS_FILE", os.path.join(HERE, "settings.json"))

# key: (default, kind, help)
SCHEMA = {
    "bot_name":          ("SSC Answer Key Bot", "text", "bot name shown in /start"),
    "force_join":        ("@NotesHubX", "channels", "channels users must join first, comma separated (off = anyone). "
                          "Private channel: -100id followed by its invite link"),
    "channel_name":      ("NotesHubX", "text", "channel name in the footer and on the marks photo"),
    "channel_link":      ("https://t.me/NotesHubX", "url", "channel link in the footer (clickable)"),
    "header_title":      ("Staff Selection Commission", "text", "big title in the PDF header"),
    "watermark":         ("@NotesHubX", "text", "diagonal watermark text (off = no watermark)"),
    "watermark_color":   ("#DB3333", "color", "watermark colour"),
    "watermark_opacity": (0.08, "number:0.02-0.6", "watermark strength, 0.02 (faint) to 0.6 (dark)"),
    "question_color":    ("#B3261E", "color", "question text colour"),
    "option_color":      ("#1B1B1B", "color", "options text colour"),
    "answer_color":      ("#1F4E9E", "color", "'Answer: (b)' colour"),
    "accent_color":      ("#1F4E9E", "color", "header border, section bar and answer box line"),
    "font_size":         (9.4, "number:7-13", "question font size"),
    "language":          ("en", "choice:en,hi,both", "bilingual papers: en = English only, hi = Hindi only, both = both"),
}

NAMED_COLORS = {
    "red": "#C62828", "blue": "#1F4E9E", "green": "#1E7D34", "black": "#1B1B1B", "orange": "#E65100",
    "purple": "#6A1B9A", "pink": "#C2185B", "grey": "#6B6B6B", "gray": "#6B6B6B", "brown": "#5D4037",
    "navy": "#0D2A6B", "teal": "#00695C", "maroon": "#7B1F1F",
}


def defaults():
    return {k: v[0] for k, v in SCHEMA.items()}


def load():
    s = defaults()
    try:
        with open(PATH, encoding="utf-8") as f:
            stored = json.load(f)
        for k, v in stored.items():
            if k in SCHEMA:
                s[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"settings.json unreadable, using defaults: {e}")
    return s


def _one_channel(item):
    """'@name' / 't.me/name' / '-100123 https://t.me/+invite' -> normalised text, or an error."""
    m = re.fullmatch(r"(?:https?://)?(?:t\.me/|telegram\.me/)?@?([A-Za-z][A-Za-z0-9_]{3,31})/?", item)
    if m:
        return "@" + m.group(1), None
    m = re.fullmatch(r"(-100\d{6,})(?:\s+((?:https?://)?t\.me/\S+))?", item)
    if m:
        link = m.group(2) or ""
        if link and not link.startswith("http"):
            link = "https://" + link
        return (m.group(1) + (" " + link if link else "")), None
    return None, (f"'{item}' is not a channel. Use @username (e.g. @NotesHubX), its t.me link, or for a private "
                  "channel its -100… id followed by the invite link. Separate several channels with commas. "
                  "off = no join check.")


def join_channels(value):
    """force_join text -> [(chat_id_for_api, join_link)]"""
    out = []
    for item in [x.strip() for x in (value or "").split(",") if x.strip()]:
        parts = item.split()
        ref = parts[0]
        link = parts[1] if len(parts) > 1 else (f"https://t.me/{ref.lstrip('@')}" if ref.startswith("@") else "")
        out.append((ref, link))
    return out


def validate(key, raw):
    """Return (value, error)."""
    key = key.strip().lower()
    if key not in SCHEMA:
        return None, f"Unknown setting '{key}'. Send /settings to see the list."
    kind = SCHEMA[key][1]
    raw = raw.strip()
    if kind == "color":
        v = NAMED_COLORS.get(raw.lower(), raw)
        if re.fullmatch(r"#?[0-9A-Fa-f]{6}", v):
            return ("#" + v.lstrip("#")).upper(), None
        return None, f"'{raw}' is not a colour. Use a code like #B3261E or a name: {', '.join(sorted(NAMED_COLORS))}."
    if kind.startswith("number"):
        lo, hi = (float(x) for x in kind.split(":")[1].split("-"))
        try:
            v = float(raw)
        except ValueError:
            return None, f"'{raw}' is not a number."
        if not lo <= v <= hi:
            return None, f"{key} must be between {lo:g} and {hi:g}."
        return v, None
    if kind.startswith("choice"):
        opts = kind.split(":")[1].split(",")
        v = {"english": "en", "hindi": "hi", "eng": "en", "hin": "hi"}.get(raw.lower(), raw.lower())
        if v in opts:
            return v, None
        return None, f"{key} must be one of: {', '.join(opts)}."
    if kind == "channels":
        if raw.lower() in ("off", "none", "-"):
            return "", None
        out = []
        for item in [x.strip() for x in re.split(r"[,\n]+", raw) if x.strip()]:
            v, err = _one_channel(item)
            if err:
                return None, err
            out.append(v)
        if len(out) > 5:
            return None, "At most 5 channels."
        return ", ".join(out), None
    if kind == "url":
        if raw.lower() in ("off", "none", "-"):
            return "", None
        v = raw if re.match(r"https?://", raw) else "https://" + raw.lstrip("@/")
        if raw.startswith("@"):
            v = "https://t.me/" + raw[1:]
        if not re.fullmatch(r"https?://[^\s<>\"]+", v):
            return None, f"'{raw}' is not a link."
        return v, None
    if len(raw) > 60:
        return None, "Too long (max 60 characters)."
    return ("" if raw.lower() in ("off", "none", "-") and key == "watermark" else raw), None


def save(s, note="settings"):
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump({k: s[k] for k in SCHEMA}, f, indent=2, ensure_ascii=False)
        f.write("\n")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        _commit(note)


def _commit(note):
    """Keep the change: commit settings.json back to the repo (needs contents: write)."""
    def git(*a):
        return subprocess.run(["git", *a], cwd=HERE, capture_output=True, text=True)
    git("config", "user.name", "ssc-bot")
    git("config", "user.email", "ssc-bot@users.noreply.github.com")
    git("add", os.path.relpath(PATH, HERE))
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("commit", "-m", f"bot: {note}")
    for _ in range(3):
        if git("push").returncode == 0:
            print("settings saved to the repo")
            return
        git("pull", "--rebase")
    print("could not push settings.json (check workflow permissions: contents: write)")


def describe(s):
    lines = []
    for k, (d, kind, help_) in SCHEMA.items():
        v = s.get(k, d)
        shown = v if v != "" else "off"
        mark = "" if v == d else "  ✏️"
        lines.append(f"• {k} = {shown}{mark}\n   {help_}")
    return "\n".join(lines)
