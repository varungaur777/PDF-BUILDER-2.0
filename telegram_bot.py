#!/usr/bin/env python3
"""
Telegram side of the SSC paper maker (public bot). Standard library only, so the quick
"any new messages?" check runs before anything is installed.

  python telegram_bot.py check        # GitHub Actions: sets has_updates / hook_url outputs
  python telegram_bot.py poll         # read new messages, build outputs, reply (repeats while new ones arrive)
  python telegram_bot.py serve        # run forever on your own server
  python telegram_bot.py arm          # reconnect the Vercel webhook (HOOK_URL) after a run
  python telegram_bot.py send FILE…   # send files to the first admin

Environment
  TELEGRAM_BOT_TOKEN   bot token from @BotFather                               (required)
  TELEGRAM_CHAT_ID     team/admin ids, comma separated: user ids, team groups,  (admins: /set, groups)
                       channels (ADMIN_IDS works too)
  LOG_CHAT_ID          channel/group that gets a copy of every request          (optional)
  PUBLIC_BOT           true (default) = anyone can use the bot in a private chat
  MAX_PARALLEL         how many people are served at the same time (default 2); the rest wait in a queue
  (users must join the channel in settings "force_join" first; /set force_join off to disable)

What users can send (DM)
  - the saved response sheet page(s): .mhtml / .txt / .html, any name, 1 or all parts
  - a command as caption or as a separate message:
      (nothing) → section PDFs + full paper + marks photo
      /sections → one PDF per section      /full → full paper only
      /marks    → marks photo only         /report → score analysis PDF
    words: yours (show your answer), hide (no name/roll no), nowm (no watermark)
Admins: /settings, /set <key> <value>, /reset
"""

import html
import json
import queue
import threading
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
ADMINS = [c.strip() for c in (os.environ.get("ADMIN_IDS") or os.environ.get("TELEGRAM_CHAT_ID", "")).split(",") if c.strip()]
ALLOWED = ADMINS                      # kept for older callers
LOG_CHAT = os.environ.get("LOG_CHAT_ID", "").strip()
PUBLIC = os.environ.get("PUBLIC_BOT", "true").strip().lower() not in ("0", "false", "no")
TG_BASE = os.environ.get("TELEGRAM_API", "https://api.telegram.org").rstrip("/")
API = f"{TG_BASE}/bot{TOKEN}"
HERE = os.path.dirname(os.path.abspath(__file__))
FILE_EXT = (".mhtml", ".mht", ".html", ".htm", ".txt")
MAX_DOWNLOAD = 20 * 1024 * 1024       # Telegram bot API download limit
MAX_FILES = 8                         # per person per request
QUIET_SECONDS = int(os.environ.get("QUIET_SECONDS", "120"))   # wait after the last file so all parts go together
MAX_WAIT = 600
RUN_BUDGET = int(os.environ.get("RUN_BUDGET_SECONDS", str(45 * 60)))  # keep taking new requests for this long
MAX_PARALLEL = max(1, int(os.environ.get("MAX_PARALLEL", "2")))
MAX_QUEUED_PER_USER = 2
UPDATES = ["message", "channel_post", "callback_query"]

MODES = ("sections", "full", "marks", "report", "all")

HELP = (
    "👋 SSC Answer Key Bot\n\n"
    "Apni SSC response sheet bhejo, bot wapas dega:\n"
    "📄 har section ki alag PDF (Question → options → official answer)\n"
    "📘 full paper ki ek PDF\n"
    "📊 marks calculation ki photo\n\n"
    "Do tareeke:\n"
    "1️⃣ Response sheet ka link yahan paste karo, ya\n"
    "2️⃣ Chrome mein response sheet kholo → ⋮ → ↓ (download) → jo file bani woh yahan bhejo.\n"
    "Saare parts ek saath bhej sakte ho.\n\n"
    "Sirf ek cheez chahiye? Caption/message mein likho:\n"
    "/marks – sirf marks photo\n"
    "/full – sirf full paper\n"
    "/sections – sirf section-wise PDFs\n"
    "/report – galat answers ke saath analysis\n"
    "'yours' likhoge to aapka answer bhi dikhega.\n\n"
    "/queue – line mein aapka number\n\n"
    "ℹ️ Aapki bheji file/link aur bot ka jawab team ke record ke liye save hota hai."
)


# --------------------------------------------------------------------------
# Telegram API (stdlib only)
# --------------------------------------------------------------------------

def api(method, params=None, timeout=60):
    data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
                                   for k, v in (params or {}).items()}).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            out = json.loads(body)
        except Exception:
            raise RuntimeError(f"Telegram {method} HTTP {e.code}: {body[:200]}")
        if e.code == 429:
            wait = int(out.get("parameters", {}).get("retry_after", 5))
            time.sleep(wait + 1)
            return api(method, params, timeout)
    if not out.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {out.get('description', out)}")
    return out["result"]


def _multipart(method, fields, files, timeout=180):
    """files: {field_name: path}"""
    boundary = uuid.uuid4().hex
    body = bytearray()
    for k, v in fields.items():
        if v is None:
            continue
        v = json.dumps(v) if isinstance(v, (list, dict)) else str(v)
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for field, path in files.items():
        name = os.path.basename(path)
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
                 f"filename=\"{name}\"; filename*=UTF-8''{urllib.parse.quote(name)}\r\n"
                 f"Content-Type: {ctype}\r\n\r\n").encode()
        body += open(path, "rb").read() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{API}/{method}", data=bytes(body),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        out = json.loads(e.read().decode() or "{}")
    if not out.get("ok"):
        raise RuntimeError(f"{method} failed: {out.get('description', out)}")
    return out["result"]


def _reply(fields, reply_to):
    if reply_to:
        fields["reply_to_message_id"] = reply_to
        fields["allow_sending_without_reply"] = "true"
    return fields


def send_message(chat_id, text, reply_to=None, buttons=None):
    p = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": "true"}
    if buttons:
        p["reply_markup"] = {"inline_keyboard": buttons}
    try:
        return api("sendMessage", _reply(p, reply_to))
    except Exception as e:
        print(f"sendMessage failed: {e}", file=sys.stderr)


def send_document(chat_id, path, caption="", reply_to=None):
    return _multipart("sendDocument", _reply({"chat_id": chat_id, "caption": caption[:1000]}, reply_to),
                      {"document": path})


def send_documents(chat_id, paths, caption="", reply_to=None):
    """Up to 10 files as one album; caption goes on the last one. Returns the sent messages."""
    if len(paths) == 1:
        return [send_document(chat_id, paths[0], caption, reply_to)]
    sent = []
    for i in range(0, len(paths), 10):
        chunk = paths[i:i + 10]
        media = [{"type": "document", "media": f"attach://f{j}"} for j in range(len(chunk))]
        if caption and i + 10 >= len(paths):
            media[-1]["caption"] = caption[:1000]
        sent += _multipart("sendMediaGroup", _reply({"chat_id": chat_id, "media": media}, reply_to),
                           {f"f{j}": p for j, p in enumerate(chunk)})
    return sent


def send_photo(chat_id, path, caption="", reply_to=None):
    return _multipart("sendPhoto", _reply({"chat_id": chat_id, "caption": caption[:1000]}, reply_to),
                      {"photo": path})


def download(file_id, dest):
    info = api("getFile", {"file_id": file_id})
    url = f"{TG_BASE}/file/bot{TOKEN}/{info['file_path']}"
    with urllib.request.urlopen(url, timeout=180) as r, open(dest, "wb") as f:
        f.write(r.read())


def get_updates(offset=None, wait=0):
    """wait > 0 = long polling: Telegram holds the request until a message arrives."""
    p = {"timeout": wait, "allowed_updates": UPDATES}
    if offset is not None:
        p["offset"] = offset
    return api("getUpdates", p, timeout=wait + 30)


# --------------------------------------------------------------------------
# Log channel (never allowed to break a user's request)
# --------------------------------------------------------------------------

def log_safe(fn, *a, **kw):
    if not LOG_CHAT:
        return None
    try:
        return fn(*a, **kw)
    except Exception as e:
        print(f"log channel: {e}", file=sys.stderr)


def who(m):
    u = m.get("from") or {}
    if not u and m.get("sender_chat"):
        c = m["sender_chat"]
        return f"{c.get('title', 'channel')} · id {c.get('id')}"
    name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or "?"
    un = f" (@{u['username']})" if u.get("username") else ""
    return f"{name}{un} · id {u.get('id')}"


def log_request(job):
    lines = ["🧾 New request",
             f"👤 {job['who']}",
             f"💬 chat {job['chat_id']} ({job['chat_type']})",
             f"⚙️ {', '.join(sorted(job['modes'])) or 'all'}"
             + (" · " + ", ".join(k for k, v in job["flags"].items() if v) if any(job["flags"].values()) else ""),
             f"📎 {len(job['files'])} file(s)" + (f", {len(job['links'])} link(s)" if job["links"] else ""),
             "🕐 " + time.strftime("%d %b %Y, %I:%M %p", time.gmtime(time.time() + 5.5 * 3600)) + " IST"]
    log_safe(api, "sendMessage", {"chat_id": LOG_CHAT, "text": "\n".join(lines)})
    for mid in job["message_ids"]:
        log_safe(api, "forwardMessage", {"chat_id": LOG_CHAT, "from_chat_id": job["chat_id"], "message_id": mid})
    for link in job["links"]:
        log_safe(api, "sendMessage", {"chat_id": LOG_CHAT, "text": f"🔗 {link}", "disable_web_page_preview": "true"})


def _file_id(msg):
    if not msg:
        return None
    if msg.get("document"):
        return msg["document"]["file_id"]
    if msg.get("photo"):
        return msg["photo"][-1]["file_id"]
    return None


def log_outputs(sent_docs, sent_photo, caption):
    ids = [x for x in (_file_id(m) for m in sent_docs or []) if x]
    if ids:
        if len(ids) == 1:
            log_safe(api, "sendDocument", {"chat_id": LOG_CHAT, "document": ids[0], "caption": caption[:1000]})
        else:
            for i in range(0, len(ids), 10):
                media = [{"type": "document", "media": fid} for fid in ids[i:i + 10]]
                if i + 10 >= len(ids):
                    media[-1]["caption"] = caption[:1000]
                log_safe(api, "sendMediaGroup", {"chat_id": LOG_CHAT, "media": media})
    pid = _file_id(sent_photo)
    if pid:
        log_safe(api, "sendPhoto", {"chat_id": LOG_CHAT, "photo": pid, "caption": caption[:1000]})


# --------------------------------------------------------------------------
# GitHub Actions / webhook helpers
# --------------------------------------------------------------------------

def need_token():
    if not TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN is not set.")


def webhook_secret():
    """Same formula as the Vercel function (sha256 of the bot token), so nothing extra to configure."""
    import hashlib
    return hashlib.sha256(TOKEN.encode()).hexdigest()[:40]


def cmd_check():
    """Cheap check so the heavy setup only runs when someone messaged the bot.
    Also remembers the Vercel webhook (if any) so `arm` can reconnect it after the run."""
    has = False
    hook = os.environ.get("HOOK_URL", "").strip()
    if TOKEN:
        try:
            if not hook:
                hook = (api("getWebhookInfo") or {}).get("url", "") or ""
            api("deleteWebhook", {"drop_pending_updates": "false"})   # getUpdates only works without a webhook
            has = bool(get_updates())
        except Exception as e:
            print(f"check failed: {e}", file=sys.stderr)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"has_updates={'true' if has else 'false'}\n")
            f.write(f"hook_url={hook}\n")
    print("has_updates:", has, "| webhook:", "yes" if hook else "no")


def cmd_arm():
    """After a run: hand new messages back to the Vercel webhook (instant replies)."""
    hook = os.environ.get("HOOK_URL", "").strip()
    if not TOKEN or not hook.startswith("https://"):
        print("no webhook to reconnect (polling mode)")
        return
    try:
        api("setWebhook", {"url": hook, "secret_token": webhook_secret(), "max_connections": 1,
                           "allowed_updates": UPDATES})
        print("webhook reconnected")
    except Exception as e:
        print(f"could not reconnect webhook: {e}", file=sys.stderr)
        sys.exit(1)


# --------------------------------------------------------------------------
# Understanding messages
# --------------------------------------------------------------------------

def parse_flags(text):
    t = (text or "").lower()
    return {
        "yours": bool(re.search(r"\byours?\b", t)),
        "hide": bool(re.search(r"\bhide\b", t)),
        "nowm": bool(re.search(r"\bno ?wm\b|\bnowatermark\b", t)),
    }


def parse_modes(text):
    t = (text or "").lower()
    found = set(re.findall(r"/(sections?|full|marks|report|all)\b", t))
    found |= {w for w in ("report",) if re.search(rf"(?<![/\w]){w}\b", t)}   # old caption word
    return {"sections" if m.startswith("section") else m for m in found}


def is_admin(m, chat_id):
    uid = str((m.get("from") or {}).get("id", ""))
    return chat_id in ADMINS or (uid and uid in ADMINS)


def allowed_chat(m, chat_id, chat_type):
    if chat_id in ADMINS:
        return True
    if chat_type == "private":
        return PUBLIC or str((m.get("from") or {}).get("id")) in ADMINS
    return False                      # random groups/channels: only the team's own


def settings_command(chat_id, text, m):
    import settings as S
    if not is_admin(m, chat_id):
        send_message(chat_id, "🔒 Yeh command sirf team ke liye hai.", m.get("message_id"))
        return
    st = S.load()
    cmd = text.split(None, 1)[0].lower().split("@")[0]
    if cmd == "/settings":
        send_message(chat_id, "⚙️ Current settings (✏️ = changed)\n\n" + S.describe(st)
                     + "\n\nChange: /set <name> <value>\ne.g. /set question_color #1E7D34\n/set watermark off\n/reset = defaults",
                     m.get("message_id"))
        return
    if cmd == "/reset":
        S.save(S.defaults(), "reset settings")
        send_message(chat_id, "♻️ Settings reset to defaults.", m.get("message_id"))
        return
    parts = text.split(None, 2)
    if len(parts) < 3:
        send_message(chat_id, "Use: /set <name> <value>\ne.g. /set channel_link t.me/NotesHubX\nSend /settings for all names.",
                     m.get("message_id"))
        return
    key, raw = parts[1], parts[2]
    val, err = S.validate(key, raw)
    if err:
        send_message(chat_id, f"❌ {err}", m.get("message_id"))
        return
    st[key.lower()] = val
    S.save(st, f"set {key.lower()}")
    send_message(chat_id, f"✅ {key.lower()} = {val if val != '' else 'off'}\nNext PDFs will use it.", m.get("message_id"))
    log_safe(api, "sendMessage", {"chat_id": LOG_CHAT, "text": f"⚙️ {who(m)} changed {key.lower()} → {val or 'off'}"})


# --------------------------------------------------------------------------
# Building and sending
# --------------------------------------------------------------------------

def build(chat_id, job, workdir):
    """Download files, run ssc_report.py, send outputs back (and to the log channel)."""
    files, links, flags, reply_to = job["files"], job["links"], job["flags"], job["reply_to"]
    local = []
    for i, (file_id, name, size) in enumerate(files[:MAX_FILES]):
        if size and size > MAX_DOWNLOAD:
            send_message(chat_id, f"⚠️ {name} 20 MB se badi hai, Telegram bot use download nahi karne deta. "
                                  "Har part alag file mein bhejo.", reply_to)
            continue
        # the file type is detected from its content, so the name doesn't matter (.mhtml → .txt etc.)
        dest = os.path.join(workdir, f"part{i + 1}.txt")
        download(file_id, dest)
        local.append(dest)
    if len(files) > MAX_FILES:
        send_message(chat_id, f"⚠️ Ek baar mein {MAX_FILES} files tak. Pehli {MAX_FILES} le li hain.", reply_to)
    if not local and not links:
        return

    modes = set(job["modes"]) or {"all"}
    if "all" in modes:
        modes = {"sections", "full", "marks"}
    out_dir = os.path.join(workdir, "out")
    cmd = [sys.executable, os.path.join(HERE, "ssc_report.py"), *local, "--out-dir", out_dir,
           "--summary", os.path.join(workdir, "summary.md")]
    if links:
        cmd += ["--links", " ".join(links)]
    if "report" in modes:
        cmd += ["--style", "report"]
    else:
        cmd += ["--style", "paper", "--make", ",".join(sorted(modes))]
    if flags["yours"]:
        cmd.append("--show-yours")
    if flags["hide"] or job["chat_type"] != "private":     # groups/channels never get name / roll no
        cmd.append("--hide-candidate")
    if flags["nowm"]:
        cmd += ["--watermark", "off"]
    cmd += ["--jobs", str(max(1, (os.cpu_count() or 2) // MAX_PARALLEL))]

    # output is captured, not printed, so public Action logs don't show names or roll numbers
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE)
    try:
        man = json.load(open(os.path.join(out_dir, "manifest.json"), encoding="utf-8"))
    except Exception:
        man = {"outputs": []}
    outs = [o for o in man.get("outputs", []) if os.path.exists(o["path"])]
    if p.returncode != 0 or not outs:
        err = (p.stderr or p.stdout or "").strip().splitlines()
        msg = "\n".join(err[-6:]) or "unknown error"
        msg = re.sub(r"part\d+\.txt: ", "", msg)
        send_message(chat_id, f"❌ PDF nahi ban payi.\n\n{msg}\n\nChrome mein ⋮ → ↓ se save ki hui file bhejo.", reply_to)
        log_safe(api, "sendMessage", {"chat_id": LOG_CHAT, "text": f"❌ Failed for {job['who']}\n{msg[:500]}"})
        return

    tot = man.get("total", {})
    secs = man.get("sections", {})
    nq = tot.get("total", 0)
    score_line = f"{tot.get('marks', 0):.2f} / {tot.get('max', 0):.0f}" if tot else ""
    import settings as S
    st = S.load()
    ch = st.get("channel_name") or ""
    link = (st.get("channel_link") or "").replace("https://", "")
    join = f"\n\n📢 {ch} · {link}" if (ch or link) else ""
    ocr = re.search(r"kept as pictures( \(\d+ Hindi\))?", p.stdout)
    pics = re.search(r"(\d+) kept as pictures", p.stdout)
    note = "\n🖼 Figure/Hindi wale question original photo mein hain." if pics and pics.group(1) != "0" else ""

    docs = [o["path"] for o in outs if o["kind"] in ("section", "full", "report")]
    photo = next((o["path"] for o in outs if o["kind"] == "marks"), None)
    sent_docs, sent_photo = [], None
    if docs:
        cap = f"✅ {nq} questions · {len(secs)} section(s){note}{join}"
        sent_docs = send_documents(chat_id, docs, cap, reply_to)
    if photo:
        lines = [f"📊 Score: {score_line}", f"✅ {tot.get('Correct', 0)}  ❌ {tot.get('Wrong', 0)}  ➖ {tot.get('Not attempted', 0)}"]
        for name, s in secs.items():
            short = re.match(r"(PART-[A-Z])", name)
            lines.append(f"• {short.group(1) if short else name[:30]}: {s['marks']:.2f} / {s['max']:.0f}")
        sent_photo = send_photo(chat_id, photo, "\n".join(lines) + join, reply_to)
    log_outputs(sent_docs, sent_photo, f"{job['who']} · {nq} Qs · score {score_line}")
    probs = open(os.path.join(workdir, "summary.md"), encoding="utf-8").read() if os.path.exists(
        os.path.join(workdir, "summary.md")) else ""
    if "**Problems:**" in probs:
        probs = re.sub(r"part(\d+)\.txt", r"file \1", probs.split("**Problems:**", 1)[1].strip())
        send_message(chat_id, "⚠️ Kuch files chhod di gayi:\n" + probs[:1500], reply_to)
    print(f"sent {len(docs)} file(s){' + marks photo' if photo else ''}")


def _msg_of(u):
    return u.get("message") or u.get("channel_post")


def _is_input(m):
    txt = m.get("text") or m.get("caption") or ""
    return bool(m.get("document")) or bool(re.search(r"https?://", txt))


# --------------------------------------------------------------------------
# Channel join gate
# --------------------------------------------------------------------------

_member_cache = {}
_gate_warned = [False]


def join_channel():
    import settings as S
    return (S.load().get("force_join") or "").strip()


def join_link(ch):
    return f"https://t.me/{ch.lstrip('@')}" if ch.startswith("@") else ""


def is_member(user_id):
    """True if the user has joined the force_join channel (or no channel is set)."""
    ch = join_channel()
    if not ch or str(user_id) in ADMINS:
        return True
    now = time.time()
    hit = _member_cache.get(user_id)
    if hit and now < hit[1]:
        return hit[0]
    try:
        r = api("getChatMember", {"chat_id": ch, "user_id": user_id})
        ok = r.get("status") in ("creator", "administrator", "member") or \
            (r.get("status") == "restricted" and r.get("is_member"))
    except Exception as e:
        # bot isn't admin in the channel or the channel is wrong: don't lock everyone out
        if not _gate_warned[0]:
            _gate_warned[0] = True
            print(f"join check failed ({e}); letting users through", file=sys.stderr)
            log_safe(api, "sendMessage", {"chat_id": LOG_CHAT, "text":
                     f"⚠️ Channel join check isn't working for {ch}: {e}\n"
                     "Make the bot an admin of that channel, or /set force_join off."})
        return True
    _member_cache[user_id] = (ok, now + (600 if ok else 15))
    return ok


def join_prompt(chat_id, reply_to=None, held=False):
    ch = join_channel()
    import settings as S
    name = S.load().get("channel_name") or ch
    text = (f"🔒 Bot use karne ke liye pehle hamara channel join karo: {ch}\n\n"
            "Join karke neeche ✅ button dabao."
            + ("\n\n📎 Aapki file sambhal kar rakhi hai — join karte hi PDF banni shuru ho jayegi." if held else ""))
    buttons = []
    if join_link(ch):
        buttons.append([{"text": f"📢 Join {name}", "url": join_link(ch)}])
    buttons.append([{"text": "✅ Maine join kar liya", "callback_data": "joined"}])
    send_message(chat_id, text, reply_to, buttons)


# --------------------------------------------------------------------------
# Dispatcher: collects files per person, queue, N workers
# --------------------------------------------------------------------------

def _new_job(m, chat_id, chat_type):
    return {"files": [], "links": [], "flags": parse_flags(""), "modes": set(), "reply_to": None,
            "message_ids": [], "who": who(m), "user_id": (m.get("from") or {}).get("id"),
            "chat_id": chat_id, "chat_type": chat_type, "last": time.time()}


class Dispatcher:
    def __init__(self, parallel=MAX_PARALLEL, persistent=False):
        self.parallel = parallel
        self.persistent = persistent          # server mode: keeps waiting jobs across time
        self.pending = {}                     # chat_id -> job still collecting files
        self.held = {}                        # chat_id -> job waiting for channel join
        self.waiting = []                     # queued jobs, in order
        self.active = {}                      # chat_id -> job being built
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.workers = []

    # ---- queue bookkeeping
    def position(self, chat_id):
        with self.lock:
            if chat_id in self.active:
                return 0
            for i, j in enumerate(self.waiting, 1):
                if j["chat_id"] == chat_id:
                    return max(1, i - max(0, self.parallel - len(self.active)))
        return None

    def queued_count(self, chat_id):
        with self.lock:
            return sum(1 for j in self.waiting if j["chat_id"] == chat_id) + (1 if chat_id in self.active else 0)

    def enqueue(self, job):
        with self.lock:
            # same person still waiting in line: merge instead of a second place
            for j in self.waiting:
                if j["chat_id"] == job["chat_id"]:
                    j["files"] += job["files"]
                    j["links"] += job["links"]
                    j["modes"] |= job["modes"]
                    j["message_ids"] += job["message_ids"]
                    return
            self.waiting.append(job)
            free = max(0, self.parallel - len(self.active))
            ahead = len(self.waiting) - 1 - free            # people really in front of this one
            job["had_to_wait"] = ahead >= 0
            self.cv.notify()
        n = len(job["files"]) + len(job["links"])
        what = f"{n} file" if job["files"] else f"{n} link"
        if job["had_to_wait"]:
            send_message(job["chat_id"], f"🕐 {what} mil gayi. Abhi bheed hai — queue mein aapka number: {ahead + 1}.\n"
                                         "Baari aate hi PDF banni shuru hogi. /queue se number dekh sakte ho.", job["reply_to"])
        else:
            send_message(job["chat_id"], f"⏳ {what} mil gayi. PDF ban rahi hai… (lagbhag 1 minute har part)", job["reply_to"])

    def promote(self, force=False):
        """Move people who stopped sending files into the queue."""
        now = time.time()
        ready = []
        with self.lock:
            for cid, j in list(self.pending.items()):
                if force or now - j["last"] >= QUIET_SECONDS:
                    ready.append(self.pending.pop(cid))
        for j in ready:
            if j["files"] or j["links"]:
                self.enqueue(j)
            elif j["modes"]:
                send_message(j["chat_id"], "📎 Ab apni response sheet ki file ya link bhejo "
                                           "(command caption mein bhi likh sakte ho).", j["reply_to"])

    # ---- workers
    def start(self):
        for i in range(self.parallel):
            t = threading.Thread(target=self._work, name=f"worker{i + 1}", daemon=True)
            t.start()
            self.workers.append(t)

    def _next(self, block=True):
        with self.cv:
            while not self.waiting:
                if not block:
                    return None
                self.cv.wait(5)
            job = self.waiting.pop(0)
            self.active[job["chat_id"]] = job
            return job

    def _work(self, block=True):
        while True:
            job = self._next(block)
            if job is None:
                return
            try:
                if job.get("had_to_wait"):
                    send_message(job["chat_id"], "🚀 Aapki baari aa gayi! PDF ban rahi hai…", job["reply_to"])
                log_request(job)
                with tempfile.TemporaryDirectory() as wd:
                    build(job["chat_id"], job, wd)
            except Exception as e:
                send_message(job["chat_id"], f"❌ Kuch gadbad ho gayi: {e}", job["reply_to"])
                log_safe(api, "sendMessage", {"chat_id": LOG_CHAT, "text": f"❌ Error for {job['who']}: {e}"[:1000]})
                print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
            finally:
                with self.lock:
                    self.active.pop(job["chat_id"], None)

    def drain(self):
        """GitHub mode: serve everyone in line with N parallel workers, then return."""
        threads = [threading.Thread(target=self._work, args=(False,)) for _ in range(self.parallel)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    # ---- incoming updates
    def feed(self, u):
        if u.get("callback_query"):
            return self.on_button(u["callback_query"])
        m = _msg_of(u)
        if not m:
            return
        chat_id = str(m["chat"]["id"])
        chat_type = m["chat"].get("type", "private")
        text = (m.get("text") or m.get("caption") or "").strip()
        first = text.split(None, 1)[0].lower().split("@")[0] if text else ""
        uid = (m.get("from") or {}).get("id")
        gate = chat_type == "private" and uid and not is_admin(m, chat_id)

        if first in ("/start", "/help", "/id"):
            extra = f"\n\n🆔 Chat id: {chat_id}" + (f" · your id: {uid}" if uid else "")
            send_message(chat_id, HELP + extra, m.get("message_id"))
            if gate and not is_member(uid):
                join_prompt(chat_id)
            return
        if first in ("/set", "/settings", "/reset"):
            return settings_command(chat_id, text, m)
        if first == "/queue":
            pos = self.position(chat_id)
            msg = ("🚀 Aapki PDF abhi ban rahi hai." if pos == 0 else
                   f"🕐 Queue mein aapka number: {pos}." if pos else
                   "✅ Aap queue mein nahi ho. File ya link bhejo.")
            return send_message(chat_id, msg, m.get("message_id"))
        if not allowed_chat(m, chat_id, chat_type):
            if chat_type == "private":
                send_message(chat_id, "🔒 Yeh bot abhi private hai.", m.get("message_id"))
            return

        # links: only SSC exam sites
        links, bad = [], []
        for url in re.findall(r"https?://\S+", text):
            url = url.rstrip(").,>]'\"")
            (links if self._link_ok(url) else bad).append(url)
        if bad and not links and not m.get("document"):
            send_message(chat_id, "❌ Yeh SSC response sheet ka link nahi lagta. "
                                  "ssc.gov.in / cbexams.com wala link bhejo, ya saved page file.", m.get("message_id"))

        doc = m.get("document")
        file_ok = False
        if doc:
            name = doc.get("file_name") or "page.mhtml"
            mime = (doc.get("mime_type") or "").lower()
            ext = os.path.splitext(name.lower())[1]
            file_ok = (ext in FILE_EXT or ext == "" or "mhtml" in mime or "multipart" in mime
                       or mime in ("text/plain", "text/html", "application/octet-stream", "message/rfc822"))
            if not file_ok:
                send_message(chat_id, f"❌ {name} nahi padh sakta. Response sheet ka link bhejo, "
                                      "ya Chrome mein ⋮ → ↓ se save ki hui page file.", m.get("message_id"))
        if m.get("photo"):
            send_message(chat_id, "📷 Photo/screenshot se nahi ban sakta. Response sheet ka link bhejo, "
                                  "ya Chrome mein ⋮ → ↓ se save ki hui page file.", m.get("message_id"))

        modes = parse_modes(text)
        if not (file_ok or links or modes):
            return
        if self.queued_count(chat_id) >= MAX_QUEUED_PER_USER and (file_ok or links):
            return send_message(chat_id, "✋ Aapki pichhli request abhi line mein hai. Woh poori hone do.", m.get("message_id"))

        member = (not gate) or not (file_ok or links) or is_member(uid)
        with self.lock:
            if member and chat_id in self.held:          # joined meanwhile: release the held files
                self.pending[chat_id] = self.held.pop(chat_id)
            store = self.held if (chat_id in self.held) else self.pending
            job = store.setdefault(chat_id, _new_job(m, chat_id, chat_type))
        if file_ok:
            job["files"].append((doc["file_id"], doc.get("file_name") or "page", doc.get("file_size")))
            job["message_ids"].append(m["message_id"])
        if links:
            job["links"] += links
            job["message_ids"].append(m["message_id"])
        job["reply_to"] = job["reply_to"] or m.get("message_id")
        job["modes"] |= modes
        for k, v in parse_flags(text).items():
            job["flags"][k] = job["flags"][k] or v
        job["last"] = time.time()

        if not member:
            with self.lock:
                if self.pending.get(chat_id) is job:
                    self.pending.pop(chat_id)
                first_hold = chat_id not in self.held
                self.held[chat_id] = job
            if first_hold:
                if self.persistent:
                    join_prompt(chat_id, m.get("message_id"), held=True)
                else:
                    join_prompt(chat_id, m.get("message_id"))
                    send_message(chat_id, "Join karne ke baad file/link dobara bhejna.")
            return
        if (file_ok or links) and len(job["files"]) + len(job["links"]) == 1 and self.persistent:
            send_message(chat_id, f"📥 Mil gaya. Baaki parts ho to bhej do — {QUIET_SECONDS} sec baad shuru karunga.",
                         m.get("message_id"))

    def _link_ok(self, url):
        try:
            import ssc_report
            return ssc_report.link_allowed(url)
        except Exception:
            return False

    def on_button(self, cq):
        uid = (cq.get("from") or {}).get("id")
        msg = cq.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", uid))
        if cq.get("data") != "joined":
            return log_safe_ok(api, "answerCallbackQuery", {"callback_query_id": cq["id"]})
        _member_cache.pop(uid, None)
        if is_member(uid):
            log_safe_ok(api, "answerCallbackQuery", {"callback_query_id": cq["id"], "text": "✅ Welcome!"})
            log_safe_ok(api, "editMessageText", {"chat_id": chat_id, "message_id": msg.get("message_id"),
                                                 "text": "✅ Channel join ho gaya. Shukriya!"})
            with self.lock:
                job = self.held.pop(chat_id, None)
            if job and (job["files"] or job["links"]):
                job["last"] = 0
                with self.lock:
                    self.pending[chat_id] = job
                self.promote()
            else:
                send_message(chat_id, "Ab apni response sheet ka link ya file bhejo 📎")
        else:
            log_safe_ok(api, "answerCallbackQuery", {"callback_query_id": cq["id"], "show_alert": "true",
                                                     "text": "❌ Abhi join nahi dikh raha. Pehle channel join karo, phir dobara dabao."})


def log_safe_ok(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:
        print(f"telegram: {e}", file=sys.stderr)


def wait_until_quiet():
    """If a file just arrived, wait for the others (parts are often sent one by one)."""
    start = time.time()
    while True:
        ups = get_updates()
        newest = max(((_msg_of(u) or {}).get("date", 0)
                      for u in ups if _is_input(_msg_of(u) or {})), default=0)
        wait = newest + QUIET_SECONDS - time.time()
        if not newest or wait <= 0 or time.time() - start > MAX_WAIT:
            return ups
        print(f"waiting {int(wait)}s for more files")
        time.sleep(min(wait, 60) + 1)


def handle(updates, dispatcher=None):
    """GitHub mode: take a batch of updates, serve everyone, return."""
    get_updates(offset=updates[-1]["update_id"] + 1)     # confirm first: a crash can't loop on the same file
    d = dispatcher or Dispatcher(persistent=False)
    for u in updates:
        try:
            d.feed(u)
        except Exception as e:
            print(f"update failed: {e}", file=sys.stderr)
    d.promote(force=True)
    d.drain()


def cmd_poll():
    need_token()
    try:
        api("deleteWebhook")
    except Exception:
        pass
    started = time.time()
    rounds = 0
    while True:
        updates = wait_until_quiet()
        if not updates:
            if rounds == 0:
                print("no new messages")
            return
        handle(updates)
        rounds += 1
        if time.time() - started > RUN_BUDGET:
            print("time budget used; leaving the rest for the next run")
            return


def cmd_serve():
    """Run forever on your own server: instant replies, queue with MAX_PARALLEL workers."""
    need_token()
    try:
        api("deleteWebhook")
    except Exception:
        pass
    d = Dispatcher(persistent=True)
    d.start()
    print(f"bot running · public={PUBLIC} · {MAX_PARALLEL} at a time · waits {QUIET_SECONDS}s after the last file · "
          f"join: {join_channel() or 'off'}", flush=True)
    offset, errors = None, 0
    while True:
        try:
            ups = get_updates(offset=offset, wait=10)
            for u in ups:
                offset = u["update_id"] + 1
                try:
                    d.feed(u)
                except Exception as e:
                    print(f"update failed: {e}", file=sys.stderr, flush=True)
            d.promote()
            errors = 0
        except KeyboardInterrupt:
            raise
        except Exception as e:
            errors += 1
            msg = str(e)
            if "409" in msg or "Conflict" in msg:
                msg += " — another copy of the bot is running (GitHub workflow or webhook)."
            print(f"error: {msg}", file=sys.stderr, flush=True)
            time.sleep(min(60, 5 * errors))


def cmd_send(paths):
    if not TOKEN or not ADMINS:
        print("Telegram not set up (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) — skipping.")
        return
    files = [p for p in paths if os.path.isfile(p)]
    if not files:
        print("nothing to send")
        return
    send_documents(ADMINS[0], files, os.environ.get("TG_CAPTION", "✅ Your SSC PDF"))
    print(f"sent {len(files)} file(s) to Telegram")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "poll"
    if what == "check":
        cmd_check()
    elif what == "poll":
        cmd_poll()
    elif what == "send":
        cmd_send(sys.argv[2:])
    elif what == "serve":
        cmd_serve()
    elif what == "arm":
        cmd_arm()
    else:
        sys.exit(__doc__)
