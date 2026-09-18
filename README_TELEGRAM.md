# 🤖 LJIET Telegram Leave Bot

Converted from the WhatsApp bot in [`milanparivar-code/Leave`](https://github.com/milanparivar-code/Leave.git)
(`AUTO LEAVE POSTER ARENA / whatsapp_bot.js` → `telegram_bot.py`).

Same brain (ARS portal automation + identical LJIET PDF reports), new Telegram face — with **buttons, guided flow, and group support**.

---

## ✨ What it does

| Command | Description |
|---|---|
| `/start` | Welcome + button menu |
| `/leave` | **Dead-simple button flow** (type → today/other dates → load → confirm; today is default) |
| `/leave CL 22/04/2026 22/04/2026 1 Load Adjusted` | **Quick apply** in one line (same syntax as WhatsApp `!leave`) |
| `/balance` | Live reconciled **Portal vs Actual** balances |
| `/history` | Last 5 generated reports |
| `/cancel` | Cancel the guided flow |
| `!leave ...` / `!balance` | Old WhatsApp syntax still works (for muscle memory) |

Flow per leave:
1. ✅ Validates input
2. 🌐 Auto-applies on `http://ars.ljinstitutes.org:81`
3. 📄 Generates the 100% identical LJIET PDF (`pdf_generator.py`)
4. 📎 Sends the PDF back in Telegram with a formatted caption
5. 💾 Logs to `leaves_database.json` (shared with Flask dashboard)

Works in **private chat and groups**.

---

## 🚀 Quick start (5 minutes)

### 1. Create your Telegram bot
1. Open Telegram → talk to **@BotFather**
2. Send `/newbot` → pick a name + username (must end in `bot`, e.g. `LjietLeaveBot`)
3. Copy the token, e.g. `123456:AA...`

### 2. Install & configure
```bash
cd telegram-leave-bot
pip install -r requirements.txt
cp .env.example .env
# edit .env -> paste TELEGRAM_BOT_TOKEN, check portal credentials
```

### 3. Run
```bash
# Option A: bot only (recommended, LOCAL mode - no Flask needed)
python telegram_bot.py
# or
bash run_bot.sh

# Option B: Flask dashboard + bot together (REMOTE mode)
bash run_all.sh
# Dashboard: http://127.0.0.1:15834
```

### 4. Talk to your bot
- Open your bot in Telegram → `/start`
- Try `/balance`, then `/leave`
- For groups: add the bot to the group, make it admin (optional), and either:
  - use `/leave@YourBotName ...`, or
  - disable *Group Privacy* via @BotFather → `/mybots` → your bot → *Bot Settings* → *Group Privacy* → *Turn off*, so it sees `!leave` too.

---

## ⚙️ Configuration (`.env`)

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Token from @BotFather |
| `PORTAL_USERNAME` | ✅ | `00000365` | ARS portal user ID |
| `PORTAL_PASSWORD` | ✅ | — | ARS portal password |
| `LOGIN_YEAR` | — | `01/07/2026LJIET` | Portal login year = "LJIET 07/2026-06/2027" |
| `EMP_NAME` / `DEPARTMENT` / `POSITION` | — | `MILAN PATEL/FY1/AP` | Shown on PDF + messages |
| `USE_BACKEND_API` | — | `false` | `true` = call Flask API instead of local portal+PDF |
| `BACKEND_BASE_URL` | — | `http://127.0.0.1:15834` | Flask URL (REMOTE mode) |
| `ALLOWED_USER_IDS` | — | empty (open) | e.g. `12345,67890` — only these users can use the bot. Get IDs from @userinfobot |

---

## 📁 Files (what came from the original repo)

```
telegram-leave-bot/
├── telegram_bot.py      # ⭐ NEW: Telegram bot (replaces whatsapp_bot.js)
├── portal_api.py        # ← original: ARS login, balance scrape, apply leave
├── pdf_generator.py     # ← original: identical LJIET PDF builder
├── app.py               # ← original: Flask dashboard (optional, for run_all.sh)
├── requirements.txt     # ← extended: + python-telegram-bot
├── .env.example         # ⭐ NEW: Telegram config template
├── run_bot.sh           # ⭐ NEW: run bot only
├── run_all.sh           # ⭐ NEW: run Flask + bot together
├── leaves_database.json # shared leave log
└── generated_pdfs/      # output PDFs
```

The original `whatsapp_bot.js` logic is preserved 1:1:
- `!leave [Type] [From] [To] [Days] [Load]` → now `/leave ...` (plus `!leave` still works)
- `!balance` → now `/balance` (plus `!balance` still works)
- Same default load details (`JAVA-II / II / 11:30 AM TO 1:30 PM / DJU (MATHS-II)`)
- Same `apply_on_portal: true` behaviour

---

## ☁️ Deploy 24/7 (free options)

- **Any VPS / old laptop / Raspberry Pi:** `nohup python telegram_bot.py &` or use `systemd` / `pm2`.
- **Render / Railway / Fly.io (free tier):** push this folder, set env vars, start command `python telegram_bot.py`.
- Keep `ALLOWED_USER_IDS` set if the bot username is public.

---

## 🧪 Test without Telegram

```bash
python test_telegram_bot.py
```
Checks: quick-arg parsing, date validation, PDF generation, balance formatting, DB write.

## ❓ Troubleshooting

- **`TELEGRAM_BOT_TOKEN is missing`** → you forgot `cp .env.example .env` + paste token.
- **Bot doesn't reply in group** → use `/leave@YourBotUsername`, or turn off Group Privacy in @BotFather.
- **Portal timeout** → ARS portal (`:81`) is intranet/slow; the bot still generates the PDF and marks sync status in the caption. Balances fall back to last-known defaults.
- **`Conflict: terminated by other getUpdates`** → bot is running twice; kill the other instance.
