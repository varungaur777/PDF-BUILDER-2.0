#!/usr/bin/env python3
"""
Telegram side of the SSC paper maker. Uses only the Python standard library,
so the quick "any new messages?" check runs before anything is installed.

  python telegram_bot.py check            # sets has_updates=true/false for GitHub Actions
  python telegram_bot.py poll             # read new messages, build PDFs, reply
  python telegram_bot.py send FILE...     # send files to TELEGRAM_CHAT_ID (after a normal run)

Environment
  TELEGRAM_BOT_TOKEN   bot token from @BotFather            (required)
  TELEGRAM_CHAT_ID     your chat id(s), comma separated     (who may use the bot / where PDFs go)
  WATERMARK_TEXT       e.g. @YourChannel                    (optional)

What the bot understands
  - .mhtml / .html files (send one part or all three together)
  - response sheet links pasted as text
  - words in the caption or message: "report" (score report instead of paper),
    "yours" (show your answer), "hide" (hide name and roll no), "nowm" (no watermark)
  - /start or /id  → replies with your chat id and how to use the bot
"""

import glob
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
WATERMARK = os.environ.get("WATERMARK_TEXT", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
HERE = os.path.dirname(os.path.abspath(__file__))
FILE_EXT = (".mhtml", ".mht", ".html", ".htm")
MAX_DOWNLOAD = 20 * 1024 * 1024   # Telegram bot API download limit

HELP = (
    "Send me your SSC response sheet page and I'll send back the question paper PDF "
    "(Q → options → official answer).\n\n"
    "How: open the response sheet in Chrome → ⋮ → ↓ download → share the .mhtml file here. "
    "Send PART-A, B and C together (or within 2 minutes) to get one combined PDF. Pasting the link also works if SSC lets GitHub open it.\n\n"
    "Add a word in the caption to change the PDF:\n"
    "• report – score report instead of paper\n"
    "• yours – show your answer too\n"
    "• hide – leave name and roll no. out\n"
    "• nowm – no watermark\n\n"
    "In a channel, name and roll no. are always left out.\n\n"
    "Replies usually come within 5–10 minutes."
)


# --------------------------------------------------------------------------
# Telegram API (stdlib only)
# --------------------------------------------------------------------------

def api(method, params=None, timeout=60):
    data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
                                   for k, v in (params or {}).items()}).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read().decode())
    if not out.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {out}")
    return out["result"]


def send_message(chat_id, text, reply_to=None):
    p = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": "true"}
    if reply_to:
        p["reply_to_message_id"] = reply_to
        p["allow_sending_without_reply"] = "true"
    try:
        return api("sendMessage", p)
    except Exception as e:
        print(f"sendMessage failed: {e}", file=sys.stderr)


def send_document(chat_id, path, caption="", reply_to=None):
    boundary = uuid.uuid4().hex
    fields = {"chat_id": str(chat_id), "caption": caption[:1000]}
    if reply_to:
        fields["reply_to_message_id"] = str(reply_to)
        fields["allow_sending_without_reply"] = "true"
    body = bytearray()
    for k, v in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    name = os.path.basename(path)
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{name}\"\r\n"
             f"Content-Type: {ctype}\r\n\r\n").encode()
    body += open(path, "rb").read() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{API}/sendDocument", data=bytes(body),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        out = json.loads(r.read().decode())
    if not out.get("ok"):
        raise RuntimeError(f"sendDocument failed: {out}")


def download(file_id, dest):
    info = api("getFile", {"file_id": file_id})
    url = f"https://api.telegram.org/file/bot{TOKEN}/{info['file_path']}"
    with urllib.request.urlopen(url, timeout=180) as r, open(dest, "wb") as f:
        f.write(r.read())


def get_updates(offset=None):
    p = {"timeout": 0, "allowed_updates": ["message", "channel_post"]}
    if offset is not None:
        p["offset"] = offset
    return api("getUpdates", p)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def need_token():
    if not TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN is not set.")


def cmd_check():
    """Cheap check so the heavy setup only runs when someone messaged the bot."""
    has = False
    if TOKEN:
        try:
            api("deleteWebhook")          # getUpdates only works without a webhook
            has = bool(get_updates())
        except Exception as e:
            print(f"check failed: {e}", file=sys.stderr)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"has_updates={'true' if has else 'false'}\n")
    print("has_updates:", has)


def parse_flags(text):
    t = (text or "").lower()
    return {
        "report": bool(re.search(r"\breport\b", t)),
        "yours": bool(re.search(r"\byours?\b", t)),
        "hide": bool(re.search(r"\bhide\b", t)),
        "nowm": bool(re.search(r"\bno ?wm\b|\bnowatermark\b", t)),
    }


def build(chat_id, job, workdir):
    """Download files, run ssc_report.py, send the PDF back."""
    files, links, flags, reply_to = job["files"], job["links"], job["flags"], job["reply_to"]
    local = []
    for i, (file_id, name, size) in enumerate(files):
        if size and size > MAX_DOWNLOAD:
            send_message(chat_id, f"⚠️ {name} is over 20 MB — Telegram won't let bots download it. "
                                  "Try saving just one part per file.", reply_to)
            continue
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name) or f"part{i}.mhtml"
        dest = os.path.join(workdir, f"{i:02d}_{safe}")
        if not dest.lower().endswith(FILE_EXT):
            dest += ".mhtml"
        download(file_id, dest)
        local.append(dest)
    if not local and not links:
        return

    out_dir = os.path.join(workdir, "out")
    cmd = [sys.executable, os.path.join(HERE, "ssc_report.py"), *local, "--out-dir", out_dir,
           "--summary", os.path.join(workdir, "summary.md")]
    if links:
        cmd += ["--links", " ".join(links)]
    cmd += ["--style", "report" if flags["report"] else "paper"]
    if flags["yours"]:
        cmd.append("--show-yours")
    if flags["hide"] or chat_id.startswith("-100"):   # channels never get name / roll no
        cmd.append("--hide-candidate")
    if WATERMARK and not flags["nowm"]:
        cmd += ["--watermark", WATERMARK]

    # output is captured, not printed, so public Action logs don't show names or roll numbers
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE)
    pdfs = sorted(glob.glob(os.path.join(out_dir, "*.pdf")))
    if p.returncode != 0 or not pdfs:
        err = (p.stderr or p.stdout or "").strip().splitlines()
        msg = "\n".join(err[-8:]) or "unknown error"
        send_message(chat_id, f"❌ Couldn't make the PDF.\n\n{msg}", reply_to)
        print(f"build failed for chat (exit {p.returncode})", file=sys.stderr)
        return

    summary = open(os.path.join(workdir, "summary.md"), encoding="utf-8").read() \
        if os.path.exists(os.path.join(workdir, "summary.md")) else ""
    total = re.search(r"\*\*Total: ([^*]+)\*\*", summary)
    rows = re.findall(r"^\| PART-[^|]*\| (\d+) \| (\d+) \| (\d+) \|", summary, re.M)
    nq = sum(int(a) + int(b) + int(c) for a, b, c in rows) or \
        sum(int(n) for n in re.findall(r": (\d+) questions", p.stdout))
    caption = f"✅ {nq} questions" + (f" · score {total.group(1)}" if total and flags["report"] else "")
    ocr = re.search(r"OCR: .*", p.stdout)
    if ocr and "0 kept as pictures" not in ocr.group(0):
        caption += "\n(some figures/maths kept as pictures)"
    secs = re.findall(r"^\| (PART-[A-Z])", summary, re.M)
    if secs:
        caption += " · " + ", ".join(secs)
    for pdf in pdfs:
        send_document(chat_id, pdf, caption, reply_to)
    probs = summary.split("**Problems:**", 1)[1].strip() if "**Problems:**" in summary else ""
    if probs:
        # file names only (00_a.txt → a.txt); no personal details are in these lines
        probs = re.sub(r"\b\d\d_", "", probs)
        send_message(chat_id, "⚠️ Some files were skipped:\n" + probs[:1500], reply_to)
    print(f"sent {len(pdfs)} PDF(s)")


QUIET_SECONDS = 120     # wait this long after the last file so all parts go into one PDF
MAX_WAIT = 600


def _is_input(m):
    txt = m.get("text") or m.get("caption") or ""
    return bool(m.get("document")) or bool(re.search(r"https?://", txt))


def wait_until_quiet():
    """If a file just arrived, wait for the others (parts are often sent one by one)."""
    start = time.time()
    while True:
        ups = get_updates()
        newest = max(((u.get("message") or u.get("channel_post") or {}).get("date", 0)
                      for u in ups if _is_input(u.get("message") or u.get("channel_post") or {})), default=0)
        wait = newest + QUIET_SECONDS - time.time()
        if not newest or wait <= 0 or time.time() - start > MAX_WAIT:
            return ups
        print(f"waiting {int(wait)}s for more files")
        time.sleep(min(wait, 60) + 1)


def cmd_poll():
    need_token()
    try:
        api("deleteWebhook")
    except Exception:
        pass
    updates = wait_until_quiet()
    if not updates:
        print("no new messages")
        return
    # confirm right away so a crash can't make the bot process the same file forever
    get_updates(offset=updates[-1]["update_id"] + 1)

    jobs = {}   # chat_id -> job
    for u in updates:
        m = u.get("message") or u.get("channel_post")
        if not m:
            continue
        chat_id = str(m["chat"]["id"])
        text = (m.get("text") or m.get("caption") or "").strip()
        if re.match(r"^/(start|help|id)\b", text):
            note = "" if chat_id in ALLOWED else \
                "\n\n⚠️ Setup step: add the id above as the TELEGRAM_CHAT_ID secret in GitHub, then send your file."
            send_message(chat_id, f"Your chat id: {chat_id}\n\n{HELP}{note}", m.get("message_id"))
            continue
        if chat_id not in ALLOWED:
            send_message(chat_id, f"Sorry, this bot is private. (chat id {chat_id})", m.get("message_id"))
            continue
        job = jobs.setdefault(chat_id, {"files": [], "links": [], "flags": parse_flags(""), "reply_to": None})
        doc = m.get("document")
        if doc:
            name = doc.get("file_name") or "page.mhtml"
            mime = (doc.get("mime_type") or "").lower()
            ext = os.path.splitext(name.lower())[1]
            looks_ok = (ext in FILE_EXT or ext in (".txt", "") or "mhtml" in mime or "multipart" in mime
                        or mime in ("text/plain", "text/html", "application/octet-stream", "message/rfc822"))
            if looks_ok:
                job["files"].append((doc["file_id"], name, doc.get("file_size")))
                job["reply_to"] = job["reply_to"] or m.get("message_id")
            else:
                send_message(chat_id, f"I can't read {name}. Send the .mhtml page file from Chrome (⋮ → ↓).",
                             m.get("message_id"))
        found = re.findall(r"https?://\S+", text)
        if found:
            job["links"] += found
            job["reply_to"] = job["reply_to"] or m.get("message_id")
        for k, v in parse_flags(text).items():
            job["flags"][k] = job["flags"][k] or v

    for chat_id, job in jobs.items():
        if not job["files"] and not job["links"]:
            continue
        n = len(job["files"]) + len(job["links"])
        send_message(chat_id, f"⏳ Got {n} item(s). Making your PDF… (about a minute per part)", job["reply_to"])
        with tempfile.TemporaryDirectory() as wd:
            try:
                build(chat_id, job, wd)
            except Exception as e:
                send_message(chat_id, f"❌ Something went wrong: {e}", job["reply_to"])
                print(f"error: {type(e).__name__}", file=sys.stderr)


def cmd_send(paths):
    if not TOKEN or not ALLOWED:
        print("Telegram not set up (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) — skipping.")
        return
    files = [p for p in paths if os.path.isfile(p)]
    if not files:
        print("nothing to send")
        return
    caption = os.environ.get("TG_CAPTION", "✅ Your SSC PDF")
    target = ALLOWED[0]
    for f in files:
        send_document(target, f, caption)
    print(f"sent {len(files)} file(s) to Telegram")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "poll"
    if what == "check":
        cmd_check()
    elif what == "poll":
        cmd_poll()
    elif what == "send":
        cmd_send(sys.argv[2:])
    else:
        sys.exit(__doc__)
