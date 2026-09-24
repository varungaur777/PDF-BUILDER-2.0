# SSC Question Paper Bot

Send your SSC response sheet page to a Telegram bot and get back a clean two-column PDF:

**Q.1** → options **(a) (b) (c) (d)** → **Answer: (x)** (SSC's official key), watermarked **@NotesHubX**.

SSC shows every question as a picture, so the script reads them with OCR (Tesseract). Blanks (`______`), underlined words and bold words are kept. Figures, fractions and tables are kept as the original picture. Cloze passages print once, followed by their questions.

This repo is **public**, and it is safe to keep it that way: your files never enter the repo. The bot downloads them into a temporary folder on GitHub's machine, makes the PDF, sends it back on Telegram, and prints no names, roll numbers or links in the logs.

---

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
3. The bot checks every 5 minutes (GitHub can start it a few minutes late). For an instant check: **Actions → Telegram bot → Run workflow**.

Caption words:

| Word | Effect |
|---|---|
| `yours` | also show your chosen answer |
| `hide` | leave name and roll no. out (always on in channels) |
| `nowm` | no watermark |
| `report` | score report instead of the paper (+1 / −0.25 by section, wrong answers in red) |

## Settings

- **Watermark:** `@NotesHubX` by default. To change it: **Settings → Secrets and variables → Actions → Variables → New repository variable**, name `WATERMARK_TEXT`.
- **Check interval:** the `cron` line in `.github/workflows/telegram.yml`.
- GitHub pauses scheduled workflows after 60 days with no commits — re-enable from the Actions tab.

## Don't

- Don't upload response sheet files or paste links into the repo, issues or workflow inputs — in a public repo anyone can see them.

## On a computer

```
sudo apt install tesseract-ocr fonts-liberation
pip install -r requirements.txt
python ssc_report.py part-a.mhtml part-b.mhtml part-c.mhtml --watermark "@NotesHubX"
```
