#!/usr/bin/env python3
"""
Telegram Leave Bot for LJIET ARS Leave Portal
=============================================
Converted from the WhatsApp bot in:
  https://github.com/milanparivar-code/Leave.git
  (AUTO LEAVE POSTER ARENA / whatsapp_bot.js)

Features
--------
- /balance  -> live reconciled ARS portal vs actual balances
- /leave    -> dead-simple flow: type -> today/other dates -> load -> done.
              Today's date is the default (one tap). Quick one-line command
              also supported.
- /history  -> recent generated leave reports
- /start, /help, /cancel
- Legacy WhatsApp syntax also works: `!leave ...` and `!balance`
- Works in private chat AND groups
- Auto-applies leave on http://ars.ljinstitutes.org:81
- Generates identical LJIET PDF report and sends it as a document

Modes
-----
1. LOCAL mode (default): bot imports portal_api.py + pdf_generator.py
   directly. No separate server needed. Just run this file.
2. REMOTE mode: set USE_BACKEND_API=true and BACKEND_BASE_URL to an
   already-running Flask app (app.py). The bot then calls
   /api/get_balances, /api/create_leave, /download_pdf/... over HTTP
   (same as the original whatsapp_bot.js did).

Setup
-----
1. Create a bot with @BotFather on Telegram and copy the token.
2. pip install -r requirements.txt
3. cp .env.example .env  -> fill TELEGRAM_BOT_TOKEN etc.
4. python telegram_bot.py
"""

import os
import re
import json
import logging
import asyncio
from datetime import datetime
from timezone_utils import get_ist_now, get_ist_today_str
from functools import wraps

import requests
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

load_dotenv()

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PORTAL_USERNAME = os.getenv("PORTAL_USERNAME", "00000365")
EMP_NAME = os.getenv("EMP_NAME", "MILAN PATEL")
DEPARTMENT = os.getenv("DEPARTMENT", "FY1")
POSITION = os.getenv("POSITION", "AP")

USE_BACKEND_API = os.getenv("USE_BACKEND_API", "false").lower() in ("1", "true", "yes")
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://127.0.0.1:15834").rstrip("/")

# Optional access control: comma-separated telegram user/chat ids allowed to use bot.
# Empty = everyone allowed (use carefully in public groups).
_allowed_raw = os.getenv("ALLOWED_USER_IDS", "").strip() or os.getenv("ALLOWED_CHAT_IDS", "").strip()
ALLOWED_IDS = set()
if _allowed_raw:
    for part in re.split(r"[,\s]+", _allowed_raw):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ALLOWED_IDS.add(int(part))

# Local-mode imports (lazy so REMOTE mode works even without reportlab installed)
LeavePortalAPI = None
LeavePDFGenerator = None
if not USE_BACKEND_API:
    try:
        from portal_api import LeavePortalAPI  # type: ignore
        from pdf_generator import LeavePDFGenerator  # type: ignore
    except Exception as e:
        print(f"WARNING: local imports failed ({e}). Falling back to REMOTE API mode.")
        USE_BACKEND_API = True

DATA_FILE = os.getenv("DATA_FILE", "leaves_database.json")
PDF_FOLDER = os.getenv("PDF_FOLDER", "generated_pdfs")
os.makedirs(PDF_FOLDER, exist_ok=True)
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w") as f:
        json.dump([], f)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("leave-telegram-bot")

# Conversation states
(LEAVE_TYPE, WHEN, PORTION, FROM_DATE, TO_DATE, TOTAL_DAYS,
 LOAD_STATUS, LOAD_DETAILS_ASK, LOAD_SUBJECT, LOAD_SEM, LOAD_TIME,
 LOAD_ENGAGER, CONFIRM) = range(13)

# NOTE: every entry here MUST be a company-PDF balance column
# (pdf_generator.BAL_COLS) so the red highlight + after-balance deduction
# always land in the right column. Portal Medical (Ml) is applied when the
# user picks SL; there is intentionally no separate ML choice.
LEAVE_TYPES = ["CL", "SL", "VL", "EL", "RH", "DL", "LWP"]
LEAVE_DESCRIPTIONS = {
    "CL": "Casual Leave",
    "SL": "Sick / Medical Leave",
    "VL": "Vacation Leave",
    "EL": "Earned Leave",
    "RH": "Restricted Holiday",
    "DL": "Duty Leave",
    "LWP": "Leave Without Pay",
}
LOAD_OPTIONS = ["Load Adjusted", "Load Taken By Self", "No Load"]
DAY_OPTIONS = ["0.25", "0.5", "1", "2", "3", "5", "10"]
LOAD_DEFAULTS = {
    "load_subject": "JAVA-II",
    "load_sem": "II",
    "load_time": "11:30 AM TO 1:30 PM",
    "load_engager": "DJU (MATHS-II)",
}
# (days, half_type, short_type or None, button label)
PORTION_OPTIONS = [
    ("0.5", "First Half", None, "☀️ First Half (0.5)"),
    ("0.5", "Second Half", None, "🌤️ Second Half (0.5)"),
    ("0.25", "First Half", "Morning Short", "🌅 Morning Short (0.25)"),
    ("0.25", "First Half", "Evening Short", "🌇 Evening Short (0.25)"),
]


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def is_allowed(update: Update) -> bool:
    """Access control check."""
    if not ALLOWED_IDS:
        return True
    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None
    return (user_id in ALLOWED_IDS) or (chat_id in ALLOWED_IDS)


def restricted(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not is_allowed(update):
            logger.warning(f"Blocked unauthorized access from user={update.effective_user}")
            if update.message:
                await update.message.reply_text(
                    "⛔ <b>Access denied.</b>\nYour Telegram ID is not authorized for this bot.",
                    parse_mode=ParseMode.HTML,
                )
            elif update.callback_query:
                await update.callback_query.answer("⛔ Not authorized", show_alert=True)
            return ConversationHandler.END if "CONFIRM" in func.__name__ else None
        return await func(update, context, *args, **kwargs)
    return wrapper


def validate_date(text: str) -> bool:
    try:
        datetime.strptime(text.strip(), "%d/%m/%Y")
        return True
    except ValueError:
        return False


def esc(text) -> str:
    """Escape text for Telegram HTML parse mode."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def today_str() -> str:
    return get_ist_today_str("%d/%m/%Y")


def get_all_leaves():
    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_leave_record(record: dict):
    records = get_all_leaves()
    records.insert(0, record)
    with open(DATA_FILE, "w") as f:
        json.dump(records, f, indent=4)


def portion_label(payload: dict) -> str:
    """Human label for the sub-day portion, e.g. 'Second Half' / 'Morning Short'."""
    try:
        days = float(payload.get("total_days", 1))
    except (TypeError, ValueError):
        return ""
    if days >= 1:
        return ""
    if days == 0.25:
        return payload.get("short_type", "Morning Short")
    return payload.get("half_type", "First Half")


def portal_half_for(payload: dict) -> str:
    """Map the chosen portion to the ARS portal's 1st/2nd half radio."""
    if payload.get("half_type") == "Second Half":
        return "Second"
    if payload.get("short_type") == "Evening Short":
        return "Second"
    return "First"


# --- Balances ---------------------------------------------------------------
def fetch_balances_local() -> dict:
    api = LeavePortalAPI()
    api.login()
    return api.get_reconciled_balances()


def fetch_balances_remote() -> dict:
    r = requests.get(f"{BACKEND_BASE_URL}/api/get_balances", timeout=60)
    r.raise_for_status()
    return r.json()


def fetch_balances() -> dict:
    if USE_BACKEND_API:
        return fetch_balances_remote()
    return fetch_balances_local()


def format_balances(data: dict) -> str:
    lines = [
        "📊 <b>LJIET FACULTY LEAVE RECONCILIATION</b>",
        f"<b>Faculty:</b> {esc(EMP_NAME)} ({esc(PORTAL_USERNAME)})",
        "",
        "<b>Code │ Portal │ Actual │ Pending</b>",
        "──────────────────────────",
    ]
    for code in ["CL", "VL", "EL", "SL", "RH", "DL", "LWP"]:
        if code not in data:
            continue
        item = data[code]
        portal = item.get("portal", 0)
        actual = item.get("actual", 0)
        pending = item.get("pending", 0)
        flag = " ⚠️" if actual < 0 else ""
        lines.append(
            f"<b>{esc(code)}</b> │ {portal} │ <b>{actual}</b>{flag} │ {pending}"
        )
    lines += [
        "",
        "💡 <i>Portal balances update days later after HOD/HR approval. "
        "Actual = Portal minus your pending requests.</i>",
    ]
    return "\n".join(lines)


# --- Leave creation ---------------------------------------------------------
def create_leave_local(payload: dict) -> dict:
    """Apply leave on portal + generate PDF locally. Returns record dict."""
    leave_type = payload.get("leave_type", "CL").upper()
    from_date = payload.get("from_date")
    to_date = payload.get("to_date", from_date)
    total_days = float(payload.get("total_days", 1))
    load_status = payload.get("load_status", "Load Adjusted")
    half_type = payload.get("half_type", "First Half")
    short_type = payload.get("short_type", "Morning Short")

    portal_success = False
    portal_msg = "Local mode: portal sync skipped."
    try:
        portal = LeavePortalAPI()
        ok, login_msg = portal.login()
        if ok:
            portal_success, portal_msg = portal.apply_leave(
                leave_code=leave_type,
                frm_dt=from_date,
                to_dt=to_date,
                reason=f"Applying for {leave_type} — {load_status}",
                day_mode="Half" if total_days <= 0.5 else "Full",
                half_type=portal_half_for(payload),
            )
        else:
            portal_msg = f"Portal login failed: {login_msg}"
    except Exception as e:
        portal_msg = f"Portal exception: {e}"
        logger.exception("Portal apply failed")

    pdf_gen = LeavePDFGenerator(output_dir=PDF_FOLDER)
    leave_data = {
        "empid": PORTAL_USERNAME,
        "emp_name": EMP_NAME,
        "department": DEPARTMENT,
        "position": POSITION,
        "leave_type": leave_type,
        "from_date": from_date,
        "to_date": to_date,
        "total_days": total_days,
        "half_type": half_type,
        "short_type": short_type,
        "load_subject": payload.get("load_subject", ""),
        "load_sem": payload.get("load_sem", ""),
        "load_time": payload.get("load_time", ""),
        "load_engager": payload.get("load_engager", ""),
        "load_status": load_status if load_status.startswith("_") else f"_{load_status}",
    }
    pdf_path, pdf_fname = pdf_gen.generate_pdf(leave_data)

    record = {
        "leave_type": leave_type,
        "from_date": from_date,
        "to_date": to_date,
        "total_days": total_days,
        "half_type": half_type,
        "short_type": short_type,
        "load_status": load_status,
        "portal_submitted": portal_success,
        "portal_msg": portal_msg,
        "pdf_filename": pdf_fname,
        "pdf_path": pdf_path,
        "timestamp": get_ist_now().strftime("%d-%m-%Y %H:%M:%S"),
    }
    save_leave_record({k: v for k, v in record.items() if k != "pdf_path"})
    return record


def create_leave_remote(payload: dict) -> dict:
    """Call Flask backend /api/create_leave, download the PDF locally."""
    r = requests.post(f"{BACKEND_BASE_URL}/api/create_leave", json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(data.get("error", "Backend failed to create leave"))
    record = data["record"]
    # Download PDF bytes
    pdf_url = f"{BACKEND_BASE_URL}/download_pdf/{requests.utils.quote(record['pdf_filename'])}"
    pr = requests.get(pdf_url, timeout=60)
    pr.raise_for_status()
    local_path = os.path.join(PDF_FOLDER, record["pdf_filename"])
    with open(local_path, "wb") as f:
        f.write(pr.content)
    record["pdf_path"] = local_path
    return record


def create_leave(payload: dict) -> dict:
    if USE_BACKEND_API:
        return create_leave_remote(payload)
    return create_leave_local(payload)


def format_leave_caption(record: dict) -> str:
    sync = "✅ Verified Sync" if record.get("portal_submitted") else "📝 Manual Entry"
    portion = portion_label(record)
    portion_txt = f" ({esc(portion)})" if portion else ""
    return (
        "📑 <b>L. J. INSTITUTE OF ENGINEERING &amp; TECHNOLOGY</b>\n"
        "<b>OFFICIAL LEAVE APPLICATION &amp; LOAD REPORT</b>\n\n"
        f"<b>Faculty:</b> {esc(EMP_NAME)} ({esc(PORTAL_USERNAME)})\n"
        f"<b>Dept:</b> {esc(DEPARTMENT)} │ <b>Position:</b> {esc(POSITION)}\n"
        f"<b>Category:</b> {esc(record.get('leave_type'))}\n"
        f"<b>Duration:</b> {esc(record.get('from_date'))} → {esc(record.get('to_date'))} "
        f"(<b>{esc(record.get('total_days'))} day</b>){portion_txt}\n"
        f"<b>Load:</b> {esc(record.get('load_status'))}\n\n"
        f"<b>🌐 Portal Sync:</b> {sync}\n"
        f"<i>{esc(record.get('portal_msg', ''))}</i>\n\n"
        f"<i>📎 {esc(record.get('pdf_filename'))}</i>"
    )


async def execute_and_send_leave(status_msg, context, payload: dict):
    """Blocking portal+PDF work runs in a thread; then sends PDF to chat."""
    loop = asyncio.get_running_loop()
    try:
        record = await loop.run_in_executor(None, lambda: create_leave(payload))
    except Exception as e:
        logger.exception("Leave creation failed")
        await status_msg.edit_text(
            f"❌ <b>Failed to create leave:</b>\n{esc(str(e))}",
            parse_mode=ParseMode.HTML,
        )
        return None

    caption = format_leave_caption(record)
    pdf_path = record.get("pdf_path")
    try:
        if pdf_path and os.path.exists(pdf_path):
            with open(pdf_path, "rb") as doc:
                await context.bot.send_document(
                    chat_id=status_msg.chat_id,
                    document=doc,
                    filename=record["pdf_filename"],
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
            await status_msg.edit_text("✅ <b>Leave compiled &amp; PDF sent above!</b>", parse_mode=ParseMode.HTML)
        else:
            await status_msg.edit_text(
                f"✅ <b>Leave logged!</b> (File: {esc(record['pdf_filename'])})\n"
                "⚠️ PDF file not found to attach.",
                parse_mode=ParseMode.HTML,
            )
    except Exception as e:
        logger.exception("Failed to send PDF")
        await status_msg.edit_text(
            f"✅ Leave logged (<i>{esc(record['pdf_filename'])}</i>) but Telegram upload failed:\n{esc(e)}",
            parse_mode=ParseMode.HTML,
        )
    return record


def parse_quick_args(text: str):
    """
    Parse: [Type] [FromDate] [ToDate] [Days] [LoadStatus...] [second|evening?]
    Returns (payload_dict, error_message).
    """
    parts = text.strip().split()
    if len(parts) < 5:
        return None, (
            "❌ <b>Invalid syntax.</b>\n\n"
            "Use:\n<code>/leave [Type] [FromDate] [ToDate] [Days] [LoadStatus]</code>\n\n"
            "<b>Examples:</b>\n"
            "<code>/leave CL 22/04/2026 22/04/2026 1 Load Adjusted</code>\n"
            "<code>/leave VL 17/05/2026 26/05/2026 10 Load Taken By Self</code>\n"
            "<code>/leave SL 05/09/2026 05/09/2026 0.5 No Load second</code>\n\n"
            "Or just send <code>/leave</code> for the simple button flow (today is default). 💬"
        )
    leave_type = parts[0].upper()
    from_date, to_date = parts[1], parts[2]
    if leave_type not in LEAVE_TYPES:
        return None, f"❌ Unknown leave type <b>{esc(leave_type)}</b>. Choose from: {', '.join(LEAVE_TYPES)}"
    if not validate_date(from_date) or not validate_date(to_date):
        return None, "❌ Dates must be in <b>DD/MM/YYYY</b> format. Example: <code>22/04/2026</code>"
    try:
        total_days = float(parts[3])
        if total_days <= 0 or total_days > 365:
            raise ValueError()
    except ValueError:
        return None, "❌ Total days must be a number like <b>0.25, 0.5, 1, 10</b>."
    tail = " ".join(parts[4:])
    low = tail.lower()
    # Optional trailing portion hint: "... second" / "... evening"
    half_type, short_type = "First Half", "Morning Short"
    if "second" in low or "evening" in low or "2nd" in low or "afternoon" in low:
        half_type, short_type = "Second Half", "Evening Short"
    # Normalize common load short forms
    if "adjust" in low:
        load_status = "Load Adjusted"
    elif "self" in low:
        load_status = "Load Taken By Self"
    elif "no" in low and "load" in low:
        load_status = "No Load"
    else:
        load_status = tail  # keep as typed

    if total_days <= 0.5 or load_status == "No Load":
        details = {"load_subject": "", "load_sem": "", "load_time": "", "load_engager": ""}
    else:
        details = dict(LOAD_DEFAULTS)
    payload = {
        "leave_type": leave_type,
        "from_date": from_date,
        "to_date": to_date,
        "total_days": total_days,
        "half_type": half_type,
        "short_type": short_type,
        "load_status": load_status,
        **details,
    }
    return payload, None


def load_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⚙️ {o}", callback_data=f"load:{o}") for o in LOAD_OPTIONS[:2]],
        [InlineKeyboardButton(f"⚙️ {LOAD_OPTIONS[2]}", callback_data=f"load:{LOAD_OPTIONS[2]}")],
    ])


async def ask_load_status(obj, context, edit):
    """obj is a CallbackQuery (edit=True) or a Message update (edit=False)."""
    d = context.user_data["leave"]
    portion = portion_label(d)
    portion_txt = f" ({portion})" if portion else ""
    text = (
        f"✅ <b>{esc(d['leave_type'])}</b> │ {esc(d['from_date'])} → {esc(d['to_date'])} "
        f"│ <b>{esc(d['total_days'])} day{portion_txt}</b>\n\n"
        "⚙️ Select <b>Load Status</b>:"
    )
    if edit:
        await obj.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=load_buttons())
    else:
        await obj.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=load_buttons())
    return LOAD_STATUS


# ----------------------------------------------------------------------------
# Command handlers
# ----------------------------------------------------------------------------
@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Apply Leave", callback_data="menu:leave"),
         InlineKeyboardButton("📊 Check Balance", callback_data="menu:balance")],
        [InlineKeyboardButton("🗂️ History", callback_data="menu:history"),
         InlineKeyboardButton("❓ Help", callback_data="menu:help")],
    ])
    await update.message.reply_text(
        f"👋 <b>Welcome, {esc(EMP_NAME)}!</b>\n\n"
        "🤖 <b>LJIET Leave Bot (Telegram)</b> is ready.\n"
        "I apply leaves on the ARS portal and generate your official PDF report.\n\n"
        "Project: <i>AUTO LEAVE POSTER ARENA</i> (converted from WhatsApp → Telegram)\n\n"
        "Choose an option or type /help:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


@restricted
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>BOT COMMANDS</b>\n\n"
        "📝 <b>Apply leave (simple):</b> just send <code>/leave</code> and tap the buttons — "
        f"today (<b>{today_str()}</b>) is the default date.\n\n"
        "🚀 <b>Quick apply (one line):</b>\n"
        "<code>/leave [Type] [From] [To] [Days] [Load]</code>\n"
        "Ex: <code>/leave CL 22/04/2026 22/04/2026 1 Load Adjusted</code>\n\n"
        "📊 <b>Balances:</b> <code>/balance</code>\n"
        "🗂️ <b>Recent reports:</b> <code>/history</code>\n"
        "❌ <b>Cancel flow:</b> <code>/cancel</code>\n\n"
        f"<b>Leave types:</b> {', '.join(LEAVE_TYPES)}\n"
        f"<b>Load:</b> {', '.join(LOAD_OPTIONS)}\n\n"
        "<i>Tip: old WhatsApp syntax (!leave / !balance) also works here.</i>",
        parse_mode=ParseMode.HTML,
    )


@restricted
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = await update.message.reply_text(
        "⏳ <i>Scraping ARS portal &amp; reconciling balances…</i>",
        parse_mode=ParseMode.HTML,
    )
    try:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, fetch_balances)
        await status.edit_text(format_balances(data), parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.exception("Balance fetch failed")
        await status.edit_text(
            f"❌ <b>Failed to fetch balances:</b>\n{esc(str(e))}",
            parse_mode=ParseMode.HTML,
        )


@restricted
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    records = get_all_leaves()[:5]
    if not records:
        await update.message.reply_text("🗂️ No leave reports yet. Use /leave to create one!")
        return
    lines = ["🗂️ <b>Recent Leave Reports</b>\n"]
    for i, r in enumerate(records, 1):
        sync = "✅" if r.get("portal_submitted") else "📝"
        lines.append(
            f"{i}. {sync} <b>{esc(r.get('leave_type'))}</b> "
            f"{esc(r.get('from_date'))} → {esc(r.get('to_date'))} "
            f"({esc(r.get('total_days'))}d)\n"
            f"   📎 <i>{esc(r.get('pdf_filename'))}</i>\n"
            f"   🕒 {esc(r.get('timestamp', ''))}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@restricted
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Leave flow cancelled. Send /leave to start again.")
    return ConversationHandler.END


# ----------------------------------------------------------------------------
# /leave conversation (dead-simple guided flow, today = default)
# ----------------------------------------------------------------------------
@restricted
async def leave_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry: quick args -> execute immediately, else start guided flow."""
    arg_text = " ".join(context.args) if context.args else ""
    if arg_text.strip():
        payload, err = parse_quick_args(arg_text)
        if err:
            await update.message.reply_text(err, parse_mode=ParseMode.HTML)
            return ConversationHandler.END
        status = await update.message.reply_text(
            f"⏳ <i>Applying {esc(payload['leave_type'])} on ARS portal &amp; compiling PDF…</i>",
            parse_mode=ParseMode.HTML,
        )
        await execute_and_send_leave(status, context, payload)
        return ConversationHandler.END

    # Guided flow -> ask leave type
    buttons = [
        InlineKeyboardButton(f"{t} ({LEAVE_DESCRIPTIONS.get(t, '')})", callback_data=f"lt:{t}")
        for t in LEAVE_TYPES
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    kb = InlineKeyboardMarkup(rows)
    await update.message.reply_text(
        "📝 <b>Apply leave — Step 1: category?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    context.user_data["leave"] = {}
    return LEAVE_TYPE


async def lt_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        await query.answer("⛔ Not authorized", show_alert=True)
        return ConversationHandler.END
    leave_type = query.data.split(":", 1)[1]
    context.user_data["leave"]["leave_type"] = leave_type
    today = today_str()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Today {today} (Full Day)", callback_data="when:today_full")],
        [InlineKeyboardButton("🕐 Today — Half / Short leave", callback_data="when:today_part")],
        [InlineKeyboardButton("📅 Other dates…", callback_data="when:other")],
    ])
    await query.edit_message_text(
        f"✅ Category: <b>{esc(leave_type)}</b> ({esc(LEAVE_DESCRIPTIONS.get(leave_type, ''))})\n\n"
        "📅 <b>Step 2: when?</b> (today is default 👇)",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    return WHEN


async def when_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return ConversationHandler.END
    choice = query.data.split(":", 1)[1]
    d = context.user_data["leave"]
    if choice == "today_full":
        d["from_date"] = d["to_date"] = today_str()
        d["total_days"] = 1
        return await ask_load_status(query, context, edit=True)
    if choice == "today_part":
        d["from_date"] = d["to_date"] = today_str()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data=f"portion:{days}:{half}:{short or '-'}")]
            for days, half, short, label in PORTION_OPTIONS
        ])
        await query.edit_message_text(
            f"✅ <b>{esc(d['leave_type'])}</b> │ Today {esc(d['from_date'])}\n\n"
            "🕐 Which portion of the day?",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return PORTION
    # other dates
    await query.edit_message_text(
        f"✅ Category: <b>{esc(d['leave_type'])}</b>\n\n"
        f"📅 Enter <b>From Date</b> (DD/MM/YYYY)\n<i>Example: {today_str()}</i>",
        parse_mode=ParseMode.HTML,
    )
    return FROM_DATE


async def portion_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return ConversationHandler.END
    _, days, half, short = query.data.split(":", 3)
    d = context.user_data["leave"]
    d["total_days"] = float(days)
    d["half_type"] = half
    d["short_type"] = short if short != "-" else "Morning Short"
    return await ask_load_status(query, context, edit=True)


async def from_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END
    text = update.message.text.strip()
    if not validate_date(text):
        await update.message.reply_text(
            "❌ Please enter date as <b>DD/MM/YYYY</b>. Example: <code>22/04/2026</code>",
            parse_mode=ParseMode.HTML,
        )
        return FROM_DATE
    context.user_data["leave"]["from_date"] = text
    await update.message.reply_text(
        f"✅ From: <b>{esc(text)}</b>\n\n📅 Enter <b>To Date</b> (DD/MM/YYYY)",
        parse_mode=ParseMode.HTML,
    )
    return TO_DATE


async def to_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END
    text = update.message.text.strip()
    if not validate_date(text):
        await update.message.reply_text(
            "❌ Please enter date as <b>DD/MM/YYYY</b>. Example: <code>22/04/2026</code>",
            parse_mode=ParseMode.HTML,
        )
        return TO_DATE
    # sanity: to >= from
    try:
        d_from = datetime.strptime(context.user_data["leave"]["from_date"], "%d/%m/%Y")
        d_to = datetime.strptime(text, "%d/%m/%Y")
        if d_to < d_from:
            await update.message.reply_text("❌ <b>To Date</b> cannot be before <b>From Date</b>. Try again:", parse_mode=ParseMode.HTML)
            return TO_DATE
    except ValueError:
        pass
    context.user_data["leave"]["to_date"] = text
    row1 = [InlineKeyboardButton(x, callback_data=f"days:{x}") for x in DAY_OPTIONS[:4]]
    row2 = [InlineKeyboardButton(x, callback_data=f"days:{x}") for x in DAY_OPTIONS[4:]]
    kb = InlineKeyboardMarkup([row1, row2])
    await update.message.reply_text(
        f"✅ {esc(context.user_data['leave']['from_date'])} → <b>{esc(text)}</b>\n\n"
        "🔢 Select <b>Total Days</b> (or type a number like 0.25 / 1.5):",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    return TOTAL_DAYS


async def _after_days_set(update, context, edit_obj):
    """Route after total_days is known: sub-day -> portion, else load status."""
    d = context.user_data["leave"]
    days = float(d["total_days"])
    if days == 0.5:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("☀️ First Half", callback_data="portion:0.5:First Half:-")],
            [InlineKeyboardButton("🌤️ Second Half", callback_data="portion:0.5:Second Half:-")],
        ])
        text = f"✅ <b>{esc(d['total_days'])} day</b>\n\n🕐 First or second half?"
        if edit_obj is not None:
            await edit_obj.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        else:
            await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return PORTION
    if days == 0.25:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌅 Morning Short", callback_data="portion:0.25:First Half:Morning Short")],
            [InlineKeyboardButton("🌇 Evening Short", callback_data="portion:0.25:First Half:Evening Short")],
        ])
        text = f"✅ <b>{esc(d['total_days'])} day</b>\n\n🕐 Morning or evening short?"
        if edit_obj is not None:
            await edit_obj.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        else:
            await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return PORTION
    if edit_obj is not None:
        return await ask_load_status(edit_obj, context, edit=True)
    return await ask_load_status(update, context, edit=False)


async def days_chosen_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return ConversationHandler.END
    context.user_data["leave"]["total_days"] = float(query.data.split(":", 1)[1])
    return await _after_days_set(update, context, query)


async def days_typed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END
    try:
        days = float(update.message.text.strip())
        if days <= 0 or days > 365:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("❌ Enter a valid number (e.g. <b>0.25, 0.5, 1, 2</b>).", parse_mode=ParseMode.HTML)
        return TOTAL_DAYS
    context.user_data["leave"]["total_days"] = days
    return await _after_days_set(update, context, None)


async def load_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return ConversationHandler.END
    load = query.data.split(":", 1)[1]
    context.user_data["leave"]["load_status"] = load
    days = float(context.user_data["leave"].get("total_days", 1))
    # Skip load details for short/no-load leaves
    if days <= 0.5 or load == "No Load":
        context.user_data["leave"].update(
            {"load_subject": "", "load_sem": "", "load_time": "", "load_engager": ""}
        )
        return await ask_confirm(query, context, edit=True)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Use usual details", callback_data="loaddet:usual")],
        [InlineKeyboardButton("✏️ Edit details…", callback_data="loaddet:edit")],
    ])
    await query.edit_message_text(
        f"✅ Load: <b>{esc(load)}</b>\n\n"
        "📚 <b>Alternate load arrangement?</b>\n"
        f"<i>Usual: {esc(LOAD_DEFAULTS['load_subject'])}, Sem {esc(LOAD_DEFAULTS['load_sem'])}, "
        f"{esc(LOAD_DEFAULTS['load_time'])}, {esc(LOAD_DEFAULTS['load_engager'])}</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    return LOAD_DETAILS_ASK


async def load_details_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return ConversationHandler.END
    if query.data == "loaddet:usual":
        context.user_data["leave"].update(dict(LOAD_DEFAULTS))
        return await ask_confirm(query, context, edit=True)
    await query.edit_message_text(
        "✏️ Enter <b>Subject</b>:",
        parse_mode=ParseMode.HTML,
    )
    return LOAD_SUBJECT


async def load_subject_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END
    text = update.message.text.strip()
    if text == "/skip":
        text = LOAD_DEFAULTS["load_subject"]
    context.user_data["leave"]["load_subject"] = text
    await update.message.reply_text(
        f"✅ Subject: <b>{esc(text)}</b>\nEnter <b>Sem</b> (or /skip for <i>II</i>):",
        parse_mode=ParseMode.HTML,
    )
    return LOAD_SEM


async def load_sem_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END
    text = update.message.text.strip()
    if text == "/skip":
        text = LOAD_DEFAULTS["load_sem"]
    context.user_data["leave"]["load_sem"] = text
    await update.message.reply_text(
        f"✅ Sem: <b>{esc(text)}</b>\nEnter <b>Time</b> (or /skip for <i>11:30 AM TO 1:30 PM</i>):",
        parse_mode=ParseMode.HTML,
    )
    return LOAD_TIME


async def load_time_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END
    text = update.message.text.strip()
    if text == "/skip":
        text = LOAD_DEFAULTS["load_time"]
    context.user_data["leave"]["load_time"] = text
    await update.message.reply_text(
        f"✅ Time: <b>{esc(text)}</b>\nEnter <b>Staff member who will engage</b> (or /skip for <i>DJU (MATHS-II)</i>):",
        parse_mode=ParseMode.HTML,
    )
    return LOAD_ENGAGER


async def load_engager_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END
    text = update.message.text.strip()
    if text == "/skip":
        text = LOAD_DEFAULTS["load_engager"]
    context.user_data["leave"]["load_engager"] = text
    return await ask_confirm(update, context, edit=False)


async def ask_confirm(obj, context, edit=False):
    d = context.user_data["leave"]
    portion = portion_label(d)
    portion_txt = f" ({portion})" if portion else ""
    summary = (
        "🧾 <b>Please confirm your leave:</b>\n\n"
        f"<b>Type:</b> {esc(d.get('leave_type'))} ({esc(LEAVE_DESCRIPTIONS.get(d.get('leave_type'), ''))})\n"
        f"<b>From:</b> {esc(d.get('from_date'))}\n"
        f"<b>To:</b> {esc(d.get('to_date'))}\n"
        f"<b>Days:</b> {esc(d.get('total_days'))}{esc(portion_txt)}\n"
        f"<b>Load:</b> {esc(d.get('load_status'))}\n"
    )
    if d.get("load_subject"):
        summary += (
            f"<b>Subject:</b> {esc(d.get('load_subject'))} │ <b>Sem:</b> {esc(d.get('load_sem'))}\n"
            f"<b>Time:</b> {esc(d.get('load_time'))}\n"
            f"<b>Engager:</b> {esc(d.get('load_engager'))}\n"
        )
    summary += "\nShall I apply this on the ARS portal &amp; generate the PDF?"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm & Apply", callback_data="confirm:yes"),
        InlineKeyboardButton("❌ Cancel", callback_data="confirm:no"),
    ]])
    if edit:
        await obj.edit_message_text(summary, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await obj.message.reply_text(summary, parse_mode=ParseMode.HTML, reply_markup=kb)
    return CONFIRM


async def confirm_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return ConversationHandler.END
    if query.data == "confirm:no":
        context.user_data.clear()
        await query.edit_message_text("❌ Leave cancelled. Send /leave to start again.")
        return ConversationHandler.END
    payload = dict(context.user_data.get("leave", {}))
    context.user_data.clear()
    await query.edit_message_text(
        f"⏳ <i>Applying {esc(payload.get('leave_type'))} on ARS portal &amp; compiling PDF…</i>",
        parse_mode=ParseMode.HTML,
    )
    # query.message works as status_msg (has chat_id + edit_text)
    await execute_and_send_leave(query.message, context, payload)
    return ConversationHandler.END


# ----------------------------------------------------------------------------
# Legacy WhatsApp-style handlers (!leave / !balance) + menu buttons
# ----------------------------------------------------------------------------
@restricted
async def legacy_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    low = text.lower()
    if low in ("!balance", "!balances"):
        await cmd_balance(update, context)
        return
    if low.startswith("!leave"):
        arg_text = text[6:].strip()
        if not arg_text:
            await update.message.reply_text(
                "Use: <code>!leave [Type] [From] [To] [Days] [Load]</code>\n"
                "Ex: <code>!leave CL 22/04/2026 22/04/2026 1 Load Adjusted</code>\n"
                "Or /leave for the simple button flow (today is default).",
                parse_mode=ParseMode.HTML,
            )
            return
        payload, err = parse_quick_args(arg_text)
        if err:
            await update.message.reply_text(err, parse_mode=ParseMode.HTML)
            return
        status = await update.message.reply_text(
            "⏳ <i>Applying on ARS portal &amp; compiling PDF…</i>",
            parse_mode=ParseMode.HTML,
        )
        await execute_and_send_leave(status, context, payload)


async def menu_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        await query.answer("⛔ Not authorized", show_alert=True)
        return
    action = query.data.split(":", 1)[1]
    await query.message.reply_text(f"Type /{action} to continue 👇" if action != "leave" else "Type /leave to start 👇")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error: {context.error}")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit(
            "❌ TELEGRAM_BOT_TOKEN is missing.\n"
            "1. Talk to @BotFather on Telegram -> /newbot -> copy token.\n"
            "2. cp .env.example .env and paste it as TELEGRAM_BOT_TOKEN=...\n"
            "3. Run again: python telegram_bot.py"
        )

    mode = f"REMOTE ({BACKEND_BASE_URL})" if USE_BACKEND_API else "LOCAL (direct portal+PDF)"
    print("=" * 60)
    print("🤖 LJIET Telegram Leave Bot")
    print(f"   Mode     : {mode}")
    print(f"   Faculty  : {EMP_NAME} ({PORTAL_USERNAME})")
    print(f"   Access   : {'restricted to ' + str(sorted(ALLOWED_IDS)) if ALLOWED_IDS else 'open (anyone with bot link)'}")
    print("=" * 60)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("leave", leave_entry),
        ],
        states={
            LEAVE_TYPE: [CallbackQueryHandler(lt_chosen, pattern=r"^lt:")],
            WHEN: [CallbackQueryHandler(when_chosen, pattern=r"^when:")],
            PORTION: [CallbackQueryHandler(portion_chosen, pattern=r"^portion:")],
            FROM_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, from_date_received)],
            TO_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, to_date_received)],
            TOTAL_DAYS: [
                CallbackQueryHandler(days_chosen_callback, pattern=r"^days:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, days_typed),
            ],
            LOAD_STATUS: [CallbackQueryHandler(load_chosen, pattern=r"^load:")],
            LOAD_DETAILS_ASK: [CallbackQueryHandler(load_details_chosen, pattern=r"^loaddet:")],
            LOAD_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, load_subject_received),
                           CommandHandler("skip", load_subject_received)],
            LOAD_SEM: [MessageHandler(filters.TEXT & ~filters.COMMAND, load_sem_received),
                       CommandHandler("skip", load_sem_received)],
            LOAD_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, load_time_received),
                        CommandHandler("skip", load_time_received)],
            LOAD_ENGAGER: [MessageHandler(filters.TEXT & ~filters.COMMAND, load_engager_received),
                           CommandHandler("skip", load_engager_received)],
            CONFIRM: [CallbackQueryHandler(confirm_chosen, pattern=r"^confirm:")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("balances", cmd_balance))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(menu_button_handler, pattern=r"^menu:"))
    app.add_handler(MessageHandler(filters.Regex(r"^!leave|^!balance"), legacy_text_handler))
    app.add_error_handler(error_handler)

    print("✅ Bot is polling. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
