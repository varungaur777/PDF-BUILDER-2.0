# SSC Answer Key Bot (public)

Anyone can DM the bot their SSC response sheet page(s) and get back:
- 📄 one PDF per section (Q → options (a)–(d) → official answer), any number of sections
- 📘 the full paper in one PDF
- 📊 a marks calculation photo (+1 / −0.25, section-wise)

File names: `12 Sep 2026 (English Language and Comprehension) (Shift-1).pdf`, full paper `12 Sep 2026 (<Exam name>) (Shift-1).pdf`.
English questions are typed out with OCR; figures, maths layouts and Hindi (or bilingual) questions/options are kept as the original picture.
Every request is copied to the team's log channel (user name, @username, id, the files and the outputs). The bot tells users this in /start.

## Commands

| Who | Send | Gets |
|---|---|---|
| Anyone (DM) | the file(s), no caption | section PDFs + full paper + marks photo |
| | caption `/marks` | marks photo only |
| | caption `/full` | full paper only |
| | caption `/sections` | section PDFs only |
| | caption `/report` | analysis PDF (your wrong answers in red) |
| | words `yours` / `hide` / `nowm` | show your answer / no name & roll no. / no watermark |
| Team (ids in `TELEGRAM_CHAT_ID`) | `/settings` | all settings |
| | `/set <name> <value>` | e.g. `/set question_color #1E7D34`, `/set watermark off`, `/set channel_link t.me/NotesHubX` |
| | `/reset` | back to defaults |

Settings: `channel_name`, `channel_link` (footer + marks photo), `header_title`, `watermark`, `watermark_color`, `watermark_opacity`, `question_color`, `option_color`, `answer_color`, `accent_color`, `font_size`. They are saved in `settings.json` (committed back to the repo by the bot).

## Secrets (repo → Settings → Secrets and variables → Actions)

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | team member user ids, comma separated (send /start to the bot to see your id) |
| `LOG_CHAT_ID` | id of the log channel (add the bot there as admin; post /id in it to get the id) |

## 1. Put the code on GitHub (phone is fine)

1. github.com → **+** → **New repository** → name `ssc-paper` → **Public** → **Create**.
2. Upload everything from the zip, keeping the folders:
   `ssc_report.py`, `ocr_text.py`, `paper_pdf.py`, `telegram_bot.py`, `requirements.txt`, `.github/workflows/telegram.yml`
   - If folders won't upload: **Add file → Create new file**, type `.github/workflows/telegram.yml` as the name and paste its contents.
3. **Actions** tab → enable workflows if asked.

## 2. Connect the Telegram bot

1. Telegram → **@BotFather** → `/newbot` → pick a name and a username → copy the **token**.
2. GitHub repo → **Settings → Secrets and variables → Actions → Secrets → New repository secret**
   Name `TELEGRAM_BOT_TOKEN`, value the token. (Secrets stay hidden even in a public repo.)
3. Open your bot in Telegram and send `/start`.
4. GitHub → **Actions → Telegram bot → Run workflow**. In a minute the bot replies with **your chat id**.
5. Add another secret: name `TELEGRAM_CHAT_ID`, value that id.
   - Also want it in your channel? Add the bot as an admin of the channel, post `/id` in the channel, run the workflow again, and add that id too, comma separated: `123456789,-1001234567890`.

The bot only answers the chat ids in `TELEGRAM_CHAT_ID`; everyone else is refused.

## 3. Use it

1. Chrome → open the response sheet → ⋮ → ↓ download. Do it for each part. The file name doesn't matter (`a.txt`, `b.txt`, `ViewCandResponse3.mhtml`…) — the bot reads which part it is from inside the file.
2. Send the file(s) to the bot. Send PART-A, PART-B and PART-C together (or one after another within 2 minutes) and you get **one PDF** with all sections in order A → B → C, each starting on a new page. The bot waits 2 minutes after your last file before starting, so it doesn't split them. You can also paste the response sheet link, but SSC may block GitHub's servers, so the file is more reliable.
3. With the Vercel hook (below) the PDF arrives in 2–3 minutes. Without it, an hourly run picks files up; for an instant check use **Actions → Telegram bot → Run workflow**.

Caption words:

| Word | Effect |
|---|---|
| `yours` | also show your chosen answer |
| `hide` | leave name and roll no. out (always on in channels) |
| `nowm` | no watermark |
| `report` | score report instead of the paper (+1 / −0.25 by section, wrong answers in red) |

## Instant replies with Vercel (recommended)

GitHub's own schedule can start hours late. With a tiny free Vercel function, Telegram calls Vercel the moment you send a file, Vercel starts the GitHub run immediately, and the PDF arrives in about 2–3 minutes. The hourly schedule stays as a safety net.

1. Make a new GitHub repo `ssc-bot-hook` with `api/telegram.js` and `package.json` (from the zip).
2. GitHub token: Settings → Developer settings → Fine-grained tokens → only `PDF-BUILDER-2.0` → **Actions: Read and write**.
3. vercel.com → Add New → Project → import `ssc-bot-hook` → Environment Variables `TELEGRAM_BOT_TOKEN`, `GITHUB_TOKEN`, `GITHUB_REPO` (= `varungaur777/PDF-BUILDER-2.0`) → Deploy.
4. Open `https://<project>.vercel.app/api/telegram` once. It connects the bot and shows ✅.

## Faster: run it on your own server (replies in ~1 minute)

GitHub often starts scheduled runs late (sometimes by hours). On a free Oracle Cloud server the bot runs 24x7 and answers within about a minute of your last file.

1. Create an Ubuntu server (Oracle Cloud → Compute → Instances → Create → **Canonical Ubuntu**, shape **Ampere A1.Flex** or **E2.1.Micro**).
2. SSH in and run:
   ```
   git clone https://github.com/varungaur777/PDF-BUILDER-2.0.git
   cd PDF-BUILDER-2.0
   bash server-setup.sh
   ```
   Enter the same bot token and chat id(s) as the GitHub secrets.
3. On GitHub: **Actions → Telegram bot → ⋯ → Disable workflow** — only one copy of the bot may run.

Update later: `cd PDF-BUILDER-2.0 && bash server-setup.sh` · Logs: `sudo docker compose logs -f --tail 50`

## Settings

- **Watermark:** `@NotesHubX` by default. To change it: **Settings → Secrets and variables → Actions → Variables → New repository variable**, name `WATERMARK_TEXT`.
- **Safety-net interval:** the `cron` line in `.github/workflows/telegram.yml` (hourly).
- GitHub pauses scheduled workflows after 60 days with no commits — re-enable from the Actions tab.

## Don't

- Don't upload response sheet files or paste links into the repo, issues or workflow inputs — in a public repo anyone can see them.

## On a computer

```
sudo apt install tesseract-ocr fonts-liberation
pip install -r requirements.txt
python ssc_report.py part-a.mhtml part-b.mhtml part-c.mhtml --watermark "@NotesHubX"
```
