import os
import asyncio
import re
import json
import threading
from datetime import datetime, timedelta, time, date
from timezone_utils import get_ist_now, get_ist_today, get_ist_today_str, get_ist_time
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from flask import Flask, request, jsonify
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    KeepTogether,
)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()
from portal_api import LeavePortalAPI
from pdf_generator import LeavePDFGenerator, get_leave_filename, get_faculty_shortname
from shift_pdf_generator import (
    generate_shift_change_pdf,
    get_shift_change_filename,
    get_shift_display,
    SHIFT_CATALOG,
)

# ==========================================
# 1. CONFIGURATION & CONSTANTS
# ==========================================
BASE_PORTAL_URL = os.getenv("BASE_PORTAL_URL", "http://ars.ljinstitutes.org:81").rstrip("/")
FACULTY_DB_FILE = "faculty_store.json"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
APP_PORT = int(os.getenv("PORT", "5000"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "MYTELEBOT")
AUTHENTICATED_ADMINS = set()

(
    REG_EMP,
    REG_PASS,
    REG_NAME,
    REG_DEPT,
    REG_POS,
    REG_SHORT_NAME,
    REG_BAL_CHOICE,
    REG_BAL_VERIFY,
    REG_BAL_MANUAL,
) = range(10, 19)

(
    LEAVE_TYPE,
    DAY_TYPE,
    DATE_PICK,
    CUSTOM_DATE,
    LOAD_CHOICE,
    LOAD_COUNT,
    LOAD_INPUT,
    LOAD_NOT_ADJ_STATUS,
    LOAD_AUTO_MAX_DIV,
    LOAD_AUTO_MERGED,
    LOAD_AUTO_OPTIONS,
    LOAD_AUTO_SLOT_PICK,
    SUBMIT_TIMING,
    REASON_CHOICE,
    CUSTOM_REASON,
    CREDIT_CONFIRM,
    CONFIRMATION,
) = range(20, 37)

(
    PROF_MENU,
    PROF_INPUT_NAME,
    PROF_INPUT_DEPT,
    PROF_INPUT_POS,
    PROF_INPUT_PASS,
    PROF_INPUT_INITIALS,
) = range(40, 46)

ATT_CUSTOM_DATE = 50
(
    SHIFT_DATE_PICK,
    SHIFT_CUSTOM_DATE,
    SHIFT_NEW_PICK,
    SHIFT_REASON_PICK,
    SHIFT_CUSTOM_REASON,
    SHIFT_CONFIRM,
    SHIFT_SUBMIT,
) = range(60, 67)

(
    ADMIN_PASS_INPUT,
    ADMIN_MENU,
    ADMIN_WAIT_UPLOAD,
) = range(70, 73)

(
    CHECK_LOAD_FACULTY,
    CHECK_LOAD_DATE,
    CHECK_LOAD_MAX_DIV,
    CHECK_LOAD_MERGED,
    CHECK_LOAD_CASCADE,
    CHECK_LOAD_VIEW,
) = range(80, 86)


# ==========================================
# 2. LOCAL MULTI-FACULTY DATABASE (THREAD-SAFE)
# ==========================================
_DB_LOCK = threading.Lock()

def load_faculties() -> dict:
    with _DB_LOCK:
        if os.path.exists(FACULTY_DB_FILE):
            try:
                with open(FACULTY_DB_FILE, "r") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

def save_faculty(user_id: int, profile: dict):
    with _DB_LOCK:
        data = {}
        if os.path.exists(FACULTY_DB_FILE):
            try:
                with open(FACULTY_DB_FILE, "r") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[str(user_id)] = profile
        with open(FACULTY_DB_FILE, "w") as f:
            json.dump(data, f, indent=2)


def load_faculty_store() -> dict:
    return load_faculties()


def save_faculty_store(store: dict):
    with _DB_LOCK:
        with open(FACULTY_DB_FILE, "w") as f:
            json.dump(store, f, indent=2)


def get_faculty(user_id: int) -> dict:
    fac = load_faculties().get(str(user_id))
    if fac and "balances" in fac and isinstance(fac["balances"], dict):
        b = fac["balances"]
        if "EXL" not in b and "Ex.L" not in b and "ExL" not in b:
            b["EXL"] = "1.00" if fac.get("emp_code") == "00000365" else "0.00"
        if "WML" not in b:
            b["WML"] = "0.00"
    return fac

def get_faculty_by_emp_code(emp_code: str) -> dict:
    if not emp_code:
        return {}
    target_code = str(emp_code).strip()
    all_facs = load_faculties()
    for uid, fac in all_facs.items():
        if str(fac.get("emp_code", "")).strip() == target_code:
            return fac
    return {}


# ==========================================
# 3. DATE & LEAVE RULE VALIDATOR ENGINE
# ==========================================
def parse_and_validate_dates(leave_type: str, day_type: str, date_str: str) -> dict:
    """
    Parses dates, calculates duration, and strictly validates against 2024 circular.
    """
    date_str = date_str.strip()
    if "to" in date_str.lower():
        parts = date_str.lower().split("to")
        from_str = parts[0].strip()
        to_str = parts[1].strip()
    else:
        from_str = date_str
        to_str = date_str

    try:
        d1 = datetime.strptime(from_str, "%d/%m/%Y").date()
        d2 = datetime.strptime(to_str, "%d/%m/%Y").date()
    except ValueError:
        return {"valid": False, "error": "Invalid date format. Use `DD/MM/YYYY` or `DD/MM/YYYY to DD/MM/YYYY`."}

    if d2 < d1:
        return {"valid": False, "error": "End date cannot be prior to start date."}

    day_count = (d2 - d1).days + 1

    # Rule: Short Day & Half Day can only be taken on a SINGLE calendar date
    if ("Short" in day_type or "Half" in day_type) and day_count > 1:
        return {"valid": False, "error": f"{day_type} cannot span multiple dates. Please select a single day."}

    # Rule: Restricted Holiday (RH) cannot exceed 1 day and cannot be half day
    if leave_type == "RH" and day_count > 1:
        return {"valid": False, "error": "Restricted Holiday (RH) must be applied as single full day."}

    # Rule: Vacation Leave (VL) min 7 consecutive days for teaching faculty
    if leave_type == "VL" and day_count < 7:
        return {
            "valid": False,
            "error": f"Rule Violation: Vacation Leave must be at least 7 consecutive days per split (entered: {day_count} days)."
        }

    # Unit computation
    if "Short" in day_type:
        units = 0.25
    elif "Half" in day_type:
        units = 0.5
    else:
        # Full day duration calculation
        units = float(day_count)
        # Check Vacation Sunday inclusion if > 5 days
        if leave_type == "VL" and day_count > 5:
            pass # Sunday rule tracked in circular

    return {
        "valid": True,
        "from_date": d1.strftime("%d/%m/%Y"),
        "to_date": d2.strftime("%d/%m/%Y"),
        "d1": d1,
        "d2": d2,
        "day_count": day_count,
        "units": units
    }


def evaluate_submission_deadline(day_type: str, leave_date: date) -> dict:
    """
    Validates WhatsApp group submission cutoffs:
    - Morning / Full Day: 8:30 AM
    - Afternoon: 11:30 AM
    """
    now = get_ist_now()
    today = now.date()

    is_today = (today == leave_date)
    is_prior = (today < leave_date)

    if "2nd Half" in day_type or "Afternoon Short" in day_type:
        cutoff = time(11, 30)
        label = "11:30 AM"
    else:
        cutoff = time(8, 30)
        label = "8:30 AM"

    if is_prior:
        return {"status": f"Prior Application (Before {label})", "on_time": True, "deadline": label}
    elif is_today and now.time() <= cutoff:
        return {"status": f"On Time (Submitted before {label})", "on_time": True, "deadline": label}
    else:
        return {"status": f"EXCEEDED DEADLINE (Submitted after {label})", "on_time": False, "deadline": label}


# ==========================================
def normalize_login_year(login_year: str) -> str:
    ly = str(login_year or "").strip()
    if not ly or "07/2026-06/2027" in ly or "2026LJIET" in ly:
        return "01/07/2026LJIET"
    if "07/2025-06/2026" in ly or "2025LJIET" in ly:
        return "01/07/2025LJIET"
    return ly


# ==========================================
# 4. INSTITUTIONAL PORTAL ENGINE (ARS)
# ==========================================
class PortalSession:
    def __init__(self, username, password, login_year=None):
        self.username = str(username or "").strip()
        self.password = str(password or "").strip()
        self.login_year = normalize_login_year(login_year)
        self.api = LeavePortalAPI(
            username=self.username,
            password=self.password,
            login_year=self.login_year
        )
        self.faculty_name = "Faculty Member"
        self.department = "Civil Engineering"

    def login(self) -> tuple:
        succ, msg = self.api.login()
        if succ:
            self.faculty_name = getattr(self.api, "faculty_name", "Faculty Member")
            self.department = getattr(self.api, "department", "")
            return True, msg
        print(f"⚠️ ARS Portal login failed for '{self.username}': {msg}")
        return False, msg

    def fetch_balances(self) -> dict:
        b = self.api.get_all_balances()
        return {
            "CL": f"{float(b.get('CL', 0.0)):.2f}",
            "SD": f"{float(b.get('SD', 0.0)):.2f}",
            "EL": f"{float(b.get('EL', 0.0)):.2f}",
            "SL": f"{float(b.get('SL', 0.0)):.2f}",
            "RH": f"{float(b.get('RH', 0.0)):.2f}",
            "LWP": "0.00",
            "VL": f"{float(b.get('VL', 0.0)):.2f}",
            "DL": f"{float(b.get('DL', 0.0)):.2f}",
            "EXL": f"{float(b.get('EXL', 0.0)):.2f}",
            "WML": f"{float(b.get('WML', 0.0)):.2f}",
        }

    def get_all_balances(self) -> dict:
        """Alias ensuring portal.get_all_balances() never raises AttributeError."""
        return self.fetch_balances()

    def get_portal_balances(self) -> dict:
        """Direct raw portal closing balances."""
        return self.api.get_portal_balances()

    def fetch_attendance(self, target_date_str=None) -> dict:
        return self.api.get_attendance(target_date_str)

    def submit_leave(self, data: dict, dry_run=False) -> tuple:
        leave_code = data.get("leave_type", "CL")
        from_date = data.get("from_date", "")
        to_date = data.get("to_date", "")
        reason = data.get("reason", "Personal Work")
        day_type = data.get("day_type", "Full Day")

        day_mode = "Half" if "Half" in day_type else "Short" if "Short" in day_type else "Full"
        half_type = "Second" if "2nd" in day_type or "Second" in day_type else "First"

        return self.api.apply_leave(
            leave_code=leave_code,
            frm_dt=from_date,
            to_dt=to_date,
            reason=reason,
            day_mode=day_mode,
            half_type=half_type,
            dry_run=dry_run
        )


# ==========================================
# 5. REPORTLAB OFFICIAL PDF GENERATOR
# ==========================================
def build_leave_selector(target="SD"):
    categories = ["CL", "SL", "SD", "VL", "RH", "LWP", "DL", "EXL"]
    badges = []
    t_clean = target.upper()
# ==========================================
# 5. REPORTLAB OFFICIAL PDF GENERATOR
# ==========================================
def generate_leave_pdf(leave_data: dict, output_path: str = None) -> str:
    pdf_gen = LeavePDFGenerator(output_dir="generated_pdfs")

    load_adjustments = leave_data.get("load_adjustments", [])
    if load_adjustments and len(load_adjustments) > 0:
        adj = load_adjustments[0]
        load_subject = adj.get("subject", "")
        load_sem = adj.get("class_div", "")
        load_time = adj.get("slot", "")
        load_engager = adj.get("substitute", "")
        load_status = "Load Adjusted"
    else:
        load_subject = ""
        load_sem = ""
        load_time = ""
        load_engager = ""
        load_status = leave_data.get("load_status", "No Load")

    pdf_input = {
        "emp_name": leave_data.get("emp_name", "MILAN PATEL"),
        "department": leave_data.get("department", "FY1"),
        "position": leave_data.get("position", "AP"),
        "leave_type": leave_data.get("leave_type", "CL"),
        "from_date": leave_data.get("from_date", ""),
        "to_date": leave_data.get("to_date", ""),
        "total_days": leave_data.get("units", leave_data.get("total_days", 1)),
        "half_type": leave_data.get("shift_type", "First Half"),
        "short_type": leave_data.get("shift_type", "Morning Short"),
        "load_status": load_status,
        "load_subject": load_subject,
        "load_sem": load_sem,
        "load_time": load_time,
        "load_engager": load_engager,
        "load_adjustments": load_adjustments,
        "score_val": leave_data.get("score_val"),
        "credit_penalty": leave_data.get("credit_penalty"),
        "submission_status_desc": leave_data.get("submission_status_desc", ""),
    }

    balances_before = leave_data.get("balances_before") or leave_data.get("balances")
    generated_path, _ = pdf_gen.generate_pdf(pdf_input, balances_before=balances_before)

    if output_path and output_path != generated_path:
        import shutil
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        shutil.copy2(generated_path, output_path)
        return output_path

    return generated_path


DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head><title>LJIET Leave Bot Dashboard</title></head>
<body>
<select id="half_type"><option value="1st Half">1st Half</option></select>
<select id="short_type"><option value="Morning Short">Morning Short</option></select>
<option value="DL">DL</option>
</body>
</html>
"""

# ==========================================
# 6. FLASK WEB BACKEND ROUTES
# ==========================================
app = Flask(__name__)

@app.route("/")
def index():
    return jsonify({"status": "running", "service": "LJIET Leave Automation Engine"})

@app.route("/api/verify_login", methods=["POST"])
def api_verify_login():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    login_year = data.get("login_year", "LJIET 07/2026-06/2027")

    if not username or not password:
        return jsonify({"status": "error", "message": "Missing credentials"}), 400

    portal = PortalSession(username=username, password=password, login_year=login_year)
    login_ok, login_msg = portal.login()
    if login_ok:
        return jsonify({
            "status": "success",
            "name": portal.faculty_name,
            "dept": portal.department
        }), 200
    return jsonify({"status": "error", "message": login_msg or "Invalid ARS credentials"}), 401

@app.route("/api/balances", methods=["POST"])
@app.route("/api/get_balances", methods=["GET", "POST"])
def api_balances():
    data = request.get_json(silent=True) or request.args.to_dict() or {}
    username = data.get("username") or data.get("emp_code")
    password = data.get("password")
    if not username or not password:
        return jsonify({"status": "error", "message": "Missing credentials. Please link your ARS User ID and Password in /profile."}), 400

    portal = PortalSession(
        username=username,
        password=password,
        login_year=data.get("login_year", "01/07/2026LJIET")
    )
    login_ok, _ = portal.login()
    if login_ok:
        return jsonify(portal.fetch_balances()), 200
    return jsonify({"status": "error", "message": "Authentication failed"}), 401

@app.route("/api/attendance", methods=["POST"])
def api_attendance():
    data = request.get_json(silent=True) or request.args.to_dict() or {}
    username = data.get("username") or data.get("emp_code")
    password = data.get("password")
    if not username or not password:
        return jsonify({"status": "error", "message": "Missing credentials"}), 400

    portal = PortalSession(
        username=username,
        password=password,
        login_year=data.get("login_year", "01/07/2026LJIET")
    )
    login_ok, _ = portal.login()
    if login_ok:
        return jsonify(portal.fetch_attendance(data.get("date"))), 200
    return jsonify({"status": "error", "message": "Authentication failed"}), 401

@app.route("/api/apply_leave", methods=["POST"])
@app.route("/api/create_leave", methods=["POST"])
def api_apply_leave():
    data = request.get_json() or {}
    username = data.get("username") or data.get("emp_code")
    password = data.get("password")
    if not username or not password:
        return jsonify({"status": "error", "message": "Missing credentials. Please check your registered profile."}), 400

    portal = PortalSession(
        username=username,
        password=password,
        login_year=data.get("login_year", "01/07/2026LJIET")
    )
    login_ok, _ = portal.login()
    if login_ok:
        dry_run = data.get("dry_run", False)
        success, msg = portal.submit_leave(data, dry_run=dry_run)
        if success:
            return jsonify({"status": "success", "message": msg}), 200
        return jsonify({"status": "error", "message": msg}), 400
    return jsonify({"status": "error", "message": "Portal authentication failed. Please verify your password in /profile."}), 401

@app.route("/api/get_leaves", methods=["GET"])
def api_get_leaves():
    return jsonify({"status": "success", "leaves": []}), 200

@app.route("/download_pdf/<filename>", methods=["GET"])
def download_pdf(filename):
    from flask import send_from_directory
    folder = os.path.join(os.getcwd(), "generated_pdfs")
    return send_from_directory(folder, filename, as_attachment=True)


@app.route("/api/shift_change", methods=["POST"])
def api_shift_change():
    """Generates official shift change PDF and optionally submits to ARS portal."""
    data = request.json or {}
    try:
        out_path, filename = generate_shift_change_pdf(data)
        pdf_url = f"{BACKEND_BASE_URL}/download_pdf/{filename}"

        apply_portal = data.get("apply_portal", False)
        portal_result = None
        if apply_portal:
            username = data.get("emp_code") or data.get("username")
            password = data.get("password")
            portal = LeavePortalAPI(username=username, password=password)
            succ, _ = portal.login()
            if succ:
                shift_cd = data.get("new_shift")
                frm_dt = data.get("from_date")
                to_dt = data.get("to_date") or frm_dt
                rsn = data.get("reason", "Shift Change Request")
                p_succ, p_msg = portal.apply_shift_change(shift_cd, frm_dt, to_dt, rsn)
                portal_result = {"success": p_succ, "message": p_msg}
            else:
                portal_result = {"success": False, "message": "Portal login failed"}

        return jsonify({
            "status": "success",
            "filename": filename,
            "pdf_url": pdf_url,
            "portal_submission": portal_result
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


# ==========================================
# 7. TELEGRAM BOT CONVERSATION HANDLERS
# ==========================================
async def safe_edit_text(query, text, reply_markup=None, parse_mode="Markdown"):
    """Safely edit message text or reply if edit fails."""
    is_callback = isinstance(query, CallbackQuery) or (
        hasattr(query, "answer") and getattr(query, "text", None) is None
    )
    if is_callback:
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
            return
        except Exception:
            pass
        try:
            if hasattr(query, "message") and query.message:
                await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
                return
        except Exception:
            pass
    else:
        try:
            if hasattr(query, "reply_text"):
                await query.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
                return
            elif hasattr(query, "message") and query.message:
                await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
                return
        except Exception:
            pass


def format_welcome_screen(faculty: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Generates the full welcome screen showing all faculty info and options."""
    b = faculty.get("balances", {})
    pos = faculty.get("position", "Assistant Professor")
    pos_code = faculty.get("position_code") or ("AP" if "Assistant" in pos else "ASP" if "Associate" in pos else "PROF" if "Prof" in pos else "LA" if "Lab" in pos else "AP")
    session = faculty.get("login_year", "01/07/2026LJIET")
    b_mode = "Portal Verified" if faculty.get("balance_mode") == "portal" else "Custom / Manual"

    short_disp = faculty.get("short_name") or faculty.get("initials") or "Not set"

    text = (
        f"🎉 **Welcome to LJIET Leave Management System!**\n\n"
        f"👤 **Faculty Profile:**\n"
        f"• **Name:** {faculty.get('name', 'Faculty Member')}\n"
        f"• **Timetable Short Name:** `{short_disp}`\n"
        f"• **Employee Code:** `{faculty.get('emp_code', '')}`\n"
        f"• **Department:** {faculty.get('dept', 'Civil Engineering')}\n"
        f"• **Designation:** {pos} (`{pos_code}`)\n"
        f"• **Session Year:** `{session}`\n"
        f"• **Balances Mode:** {b_mode}\n\n"
        f"📊 **Current Leave Balances:**\n"
        f"• Casual Leave (CL): **{b.get('CL', '0')}**\n"
        f"• Short Day (SD): **{b.get('SD', '0')}**\n"
        f"• Earned Leave (EL): **{b.get('EL', '0')}**\n"
        f"• Sick Leave (SL): **{b.get('SL', '0')}**\n"
        f"• Restricted Holiday (RH): **{b.get('RH', '0')}**\n"
        f"• Vacation Leave (VL): **{b.get('VL', '0')}**\n"
        f"• Duty Leave (DL): **{b.get('DL', '0')}**\n"
        f"• Exchanged Leave (Ex.L): **{b.get('EXL', '0')}**\n"
        f"• Women Medical Leave (WML): **{b.get('WML', '0')}**\n"
        f"• Leave Without Pay (LWP): **{b.get('LWP', '0')}**\n\n"
        f"Select an action below:"
    )
    kb = [
        [InlineKeyboardButton("📝 Apply for Leave", callback_data="CMD_APPLY")],
        [InlineKeyboardButton("🔄 Shift Change Application", callback_data="CMD_SHIFT_CHANGE")],
        [
            InlineKeyboardButton("📊 Check Balances", callback_data="CMD_BALANCE"),
            InlineKeyboardButton("⏱️ Attendance / Punch", callback_data="CMD_ATTENDANCE"),
        ],
        [
            InlineKeyboardButton("👤 Update / Edit Profile", callback_data="CMD_EDIT_PROFILE"),
            InlineKeyboardButton("🔍 Check Lecture Adjustments", callback_data="CMD_CHECK_LOAD"),
        ],
        [
            InlineKeyboardButton("⚙️ Re-login / Switch User", callback_data="START_REG"),
            InlineKeyboardButton("🛠️ Admin Panel", callback_data="CMD_ADMIN"),
        ],
        [
            InlineKeyboardButton("📋 Application Status", callback_data="CMD_STATUS"),
            InlineKeyboardButton("🚪 Logout", callback_data="CMD_LOGOUT"),
        ],
    ]
    return text, InlineKeyboardMarkup(kb)


def log_application_to_db(record: dict):
    """Persists a leave or shift change application record to leaves_database.json."""
    try:
        db_file = "leaves_database.json"
        records = []
        if os.path.exists(db_file):
            with open(db_file, "r", encoding="utf-8") as f:
                records = json.load(f)
        records.append(record)
        with open(db_file, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2)
    except Exception as e:
        logger.error(f"Error logging application to database: {e}")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    faculty = get_faculty(user_id)

    if not faculty:
        text = (
            "🏛️ **LJIET Leave & Load Management System**\n\n"
            "Welcome to the official LJIET Faculty Automation Assistant.\n\n"
            "✨ **Features:**\n"
            "• 📝 1-Click Leave Applications with Official PDFs\n"
            "• 🔄 Shift Change Requests & Official Forms\n"
            "• 📊 Live ARS Leave Balances & Status Tracking\n"
            "• ⏱️ Biometric Punch History & Attendance Logs\n"
            "• 🤖 100% Accurate Timetable Load Adjustments\n\n"
            "👉 *Please sign in with your official ARS Portal credentials:*"
        )
        kb = [[InlineKeyboardButton("🔑 Login with ARS Credentials", callback_data="START_REG")]]
        if update.callback_query:
            await safe_edit_text(update.callback_query, text, reply_markup=InlineKeyboardMarkup(kb))
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else:
        text, markup = format_welcome_screen(faculty)
        if update.callback_query:
            await safe_edit_text(update.callback_query, text, reply_markup=markup)
        else:
            await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")


async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Logs out the active user, clearing local session and credentials."""
    user_id = update.effective_user.id
    try:
        store = load_faculty_store()
        str_id = str(user_id)
        if str_id in store:
            del store[str_id]
            save_faculty_store(store)
    except Exception as e:
        logger.error(f"Error during logout: {e}")

    context.user_data.clear()

    text = (
        "🔒 **Logged Out Successfully**\n\n"
        "You have been logged out of the LJIET Leave Management System.\n"
        "Your active session and saved credentials have been cleared from this chat.\n\n"
        "👉 *To sign in again at any time, click below or type `/login`:*"
    )
    kb = [
        [InlineKeyboardButton("🔑 Login to ARS Portal", callback_data="START_REG")],
    ]
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await safe_edit_text(query, text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetches live leave and shift change status directly from ARS portal and database."""
    user_id = update.effective_user.id
    faculty = get_faculty(user_id)
    if not faculty:
        msg = "⚠️ Please log in to ARS first via /login."
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        else:
            await update.message.reply_text(msg)
        return

    query = update.callback_query
    msg_status = None
    if query:
        await query.answer()
        msg_status = await safe_edit_text(query, "⏳ Fetching live application status from ARS Portal...")
    else:
        msg_status = await update.message.reply_text("⏳ Fetching live application status from ARS Portal...")

    emp_code = faculty.get("emp_code") or faculty.get("username")
    portal = LeavePortalAPI(username=emp_code, password=faculty.get("password"))
    status_data = await asyncio.to_thread(portal.get_application_status)

    leaves = status_data.get("leaves", [])
    shifts = status_data.get("shifts", [])

    lines = [
        "📋 **ARS Portal Application Status**",
        f"👤 Faculty: **{faculty.get('name', 'Faculty')}** (`{emp_code}`)\n"
    ]

    lines.append("📝 **Leave Applications:**")
    if not leaves:
        lines.append("  • *No recent leave applications found.*")
    else:
        for l in leaves[:5]:
            status_str = l.get("status", "Applied")
            lines.append(f"  • **{l.get('leave_type')}** ({l.get('days')} day) on `{l.get('from_date')}`")
            lines.append(f"    ↳ Status: {status_str}")
            if l.get("reason"):
                lines.append(f"    ↳ Reason: *{l.get('reason')}*")

    lines.append("\n🔄 **Shift Change Applications:**")
    if not shifts:
        lines.append("  • *No recent shift change applications found.*")
    else:
        for s in shifts[:5]:
            status_str = s.get("status", "Applied")
            lines.append(f"  • **{s.get('new_shift')}** on `{s.get('from_date')}`")
            lines.append(f"    ↳ Status: {status_str}")
            if s.get("reason"):
                lines.append(f"    ↳ Reason: *{s.get('reason')}*")

    lines.append("\n💡 *Leaves and shifts are officially recorded on ARS attendance.*")

    kb = [
        [InlineKeyboardButton("🔄 Refresh Status", callback_data="REFRESH_STATUS")],
        [InlineKeyboardButton("🏠 Back to Main Menu", callback_data="CMD_HOME")],
    ]
    if query:
        await safe_edit_text(query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))
    else:
        await msg_status.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")


async def return_to_home_screen(target, context: ContextTypes.DEFAULT_TYPE = None, prefix_msg: str = ""):
    """
    Directly navigates the user back to the Home Screen (format_welcome_screen)
    with their profile, leave balances, and main menu options.
    Handles both CallbackQuery objects and Message/Update objects cleanly.
    """
    user_id = None
    query = None
    msg_obj = None

    if isinstance(target, Update):
        if target.callback_query:
            query = target.callback_query
            user_id = target.effective_user.id if target.effective_user else None
        else:
            msg_obj = target.effective_message or target.message
            user_id = target.effective_user.id if target.effective_user else None
    elif isinstance(target, CallbackQuery) or hasattr(target, "edit_message_text") or hasattr(target, "data"):
        query = target
        user_id = query.from_user.id if query.from_user else None
    elif hasattr(target, "reply_text"):
        msg_obj = target
        user_id = getattr(target, "chat_id", None)
    elif hasattr(target, "message") and hasattr(target.message, "reply_text"):
        msg_obj = target.message
        user_id = getattr(msg_obj, "chat_id", None)

    faculty = get_faculty(user_id) if user_id else None

    # Returning home ends only the current conversation flow.  Do not remove
    # the saved faculty profile here; that is reserved for the explicit
    # Logout action.
    if context is not None:
        context.user_data.clear()

    if faculty:
        welcome_text, welcome_markup = format_welcome_screen(faculty)
        full_text = f"{prefix_msg.strip()}\n\n{welcome_text}" if prefix_msg else welcome_text
        if query:
            await safe_edit_text(query, full_text, reply_markup=welcome_markup)
        elif msg_obj:
            await msg_obj.reply_text(full_text, reply_markup=welcome_markup, parse_mode="Markdown")
    else:
        fallback = prefix_msg or "👋 Welcome! Please /register with your ARS User ID & Password to link your account."
        reg_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Register with ARS Credentials", callback_data="START_REG")]])
        if query:
            await safe_edit_text(query, fallback, reply_markup=reg_kb)
        elif msg_obj:
            await msg_obj.reply_text(fallback, reply_markup=reg_kb, parse_mode="Markdown")

    return ConversationHandler.END


# ---- REGISTRATION FLOW ----
async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    context.user_data.clear()
    msg = "Enter your **ARS User ID / Employee Code** (e.g. `00000365`):"
    kb = [[InlineKeyboardButton("❌ Cancel Registration", callback_data="CANCEL_REG")]]
    if query:
        await safe_edit_text(query, msg, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return REG_EMP


async def reg_emp_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == "CANCEL_REG":
            return await return_to_home_screen(query, context, prefix_msg="❌ **Registration canceled.**")
        return await handle_universal_callback(update, context)

    if not update.message or not update.message.text:
        return REG_EMP

    context.user_data["reg_emp"] = update.message.text.strip()
    await update.message.reply_text("Enter your **ARS Portal Password**:")
    return REG_PASS


async def reg_pass_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        cb_data = query.data
        if cb_data == "RETRY_PASS":
            emp_code = context.user_data.get("reg_emp", "")
            await safe_edit_text(query, f"Enter your **ARS Portal Password** for `{emp_code}`:")
            return REG_PASS
        elif cb_data == "RETRY_EMP":
            await safe_edit_text(query, "Enter your **ARS User ID / Employee Code** (e.g. `00000365`):")
            return REG_EMP
        elif cb_data == "CANCEL_REG":
            return await return_to_home_screen(query, context, prefix_msg="❌ **Registration canceled.**")
        return await handle_universal_callback(update, context)

    if not update.message or not update.message.text:
        return REG_PASS

    emp_code = context.user_data.get("reg_emp", "").strip()
    password = update.message.text.strip()
    login_year = os.getenv("LOGIN_YEAR", "01/07/2026LJIET")

    msg_status = await update.message.reply_text("⏳ Verifying ARS User ID and Password...")

    try:
        portal = PortalSession(username=emp_code, password=password, login_year=login_year)
        login_ok, login_msg = await asyncio.to_thread(portal.login)
    except Exception as e:
        login_ok = False
        login_msg = str(e)

    if not login_ok:
        kb = [
            [InlineKeyboardButton("🔄 Re-enter Password", callback_data="RETRY_PASS")],
            [InlineKeyboardButton("👤 Re-enter User ID", callback_data="RETRY_EMP")],
            [InlineKeyboardButton("❌ Cancel Registration", callback_data="CANCEL_REG")],
        ]
        await msg_status.edit_text(
            f"❌ **Invalid ARS Credentials!**\n\n"
            f"Authentication failed for Employee Code: `{emp_code}`.\n"
            f"Error: `{login_msg or 'Invalid User ID or Password'}`\n\n"
            f"Please re-enter your **ARS Portal Password** (or choose an option below):",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return REG_PASS

    context.user_data["reg_pass"] = password
    context.user_data["reg_portal_name"] = portal.faculty_name
    context.user_data["reg_login_year"] = login_year
    if portal.department:
        context.user_data["reg_portal_dept"] = portal.department

    # Auto-resolve initials and details
    from timetable_engine import get_timetable_engine
    tt_engine = get_timetable_engine()
    fac_name = portal.faculty_name or "Faculty Member"
    detected_initials = tt_engine.resolve_faculty_initials(fac_name, emp_code=emp_code) or get_faculty_shortname(fac_name)
    detected_dept = portal.department or "FY1 (Civil Engineering)"
    detected_pos = "Assistant Professor"
    detected_pos_code = "AP"

    context.user_data["reg_name"] = fac_name
    context.user_data["reg_initials"] = detected_initials
    context.user_data["reg_dept_val"] = detected_dept
    context.user_data["reg_pos_val"] = detected_pos
    context.user_data["reg_pos_code"] = detected_pos_code
    context.user_data["reg_department_pending"] = True

    # Fetch live balances immediately using the already-authenticated session
    portal_balances = await asyncio.to_thread(portal.fetch_balances)
    context.user_data["reg_portal_balances"] = portal_balances
    context.user_data["fetched_balances"] = portal_balances

    name_hint = f" (detected: `{fac_name}`)" if fac_name and fac_name != "Faculty Member" else ""
    kb = [
        [InlineKeyboardButton(f"✅ Confirm '{fac_name}' & Select Department", callback_data="REG_CONFIRM_AUTO")],
        [InlineKeyboardButton("✍️ Type Custom Name", callback_data="REG_TYPE_CUSTOM_NAME")],
        [InlineKeyboardButton("❌ Cancel Registration", callback_data="CANCEL_REG")],
    ]
    prompt = (
        f"✅ **ARS Credentials Verified!**\n\n"
        f"👤 **Detected Faculty Profile:**\n"
        f"• **Full Name:** {fac_name}\n"
        f"• **Timetable Short Name:** `{detected_initials}`\n"
        f"• **Employee Code:** `{emp_code}`\n"
        f"• **Department:** {detected_dept}\n"
        f"• **Designation:** {detected_pos} (`{detected_pos_code}`)\n\n"
        f"📊 **Leave Balances Linked:**\n"
        f"• CL: **{portal_balances.get('CL', '0.00')}** | SL: **{portal_balances.get('SL', '0.00')}** | VL: **{portal_balances.get('VL', '0.00')}** | RH: **{portal_balances.get('RH', '0.00')}**\n\n"
        f"Tap **Confirm & Select Department** to choose your department, or type a custom name below:"
    )
    await msg_status.edit_text(prompt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return REG_NAME


async def reg_confirm_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return REG_NAME
    await query.answer()
    data = query.data
    u = context.user_data
    user_id = update.effective_user.id

    if data == "REG_CONFIRM_AUTO":
        if u.get("reg_department_pending"):
            u["reg_department_pending"] = False
            detected_dept = u.get("reg_portal_dept") or u.get("reg_dept_val") or "FY1"
            prompt = (
                "✅ **Faculty name confirmed.**\n\n"
                f"Detected department: **{detected_dept}**\n\n"
                "Select your department/section. If it is not listed, tap **Type Custom Department**."
            )
            await safe_edit_text(query, prompt, reply_markup=InlineKeyboardMarkup(build_dept_keyboard()))
            return REG_DEPT
        await safe_edit_text(query, "⏳ Finalizing your faculty registration...")
        emp_code = u.get("reg_emp", "")
        password = u.get("reg_pass", "")
        login_year = u.get("reg_login_year", "01/07/2026LJIET")

        portal_balances = u.get("reg_portal_balances") or u.get("fetched_balances")
        if not portal_balances or all(float(v or 0) == 0.0 for v in portal_balances.values()):
            portal = PortalSession(username=emp_code, password=password, login_year=login_year)
            await asyncio.to_thread(portal.login)
            portal_balances = await asyncio.to_thread(portal.fetch_balances)

        is_admin = (emp_code in ("00000365", "00000366"))
        faculty = {
            "name": u.get("reg_name", "Faculty Member"),
            "emp_code": emp_code,
            "password": password,
            "dept": u.get("reg_dept_val", "FY1 (Civil Engineering)"),
            "position": u.get("reg_pos_val", "Assistant Professor"),
            "position_code": u.get("reg_pos_code", "AP"),
            "initials": u.get("reg_initials", "FACULTY"),
            "short_name": u.get("reg_initials", "FACULTY"),
            "login_year": login_year,
            "balances": portal_balances,
            "balance_mode": "portal",
            "is_admin": is_admin
        }
        save_faculty(user_id, faculty)

        text, markup = format_welcome_screen(faculty)
        welcome_msg = (
            f"🎉 **Registration Completed Successfully!**\n\n"
            f"Welcome, **{faculty['name']}** (`{faculty['short_name']}`)!\n"
            f"Your ARS portal balances and timetable duties are now linked.\n\n"
            f"{text}"
        )
        await safe_edit_text(query, welcome_msg, reply_markup=markup)
        return ConversationHandler.END

    elif data == "REG_TYPE_CUSTOM_NAME":
        await safe_edit_text(query, "Type your **Full Faculty Name**:")
        return REG_NAME

    return await handle_universal_callback(update, context)


def build_dept_keyboard():
    return [
        [
            InlineKeyboardButton("FY1", callback_data="DEPT:FY1"),
            InlineKeyboardButton("FY2", callback_data="DEPT:FY2"),
            InlineKeyboardButton("FY3", callback_data="DEPT:FY3"),
            InlineKeyboardButton("FY4", callback_data="DEPT:FY4"),
            InlineKeyboardButton("FY5", callback_data="DEPT:FY5"),
        ],
        [
            InlineKeyboardButton("SY1", callback_data="DEPT:SY1"),
            InlineKeyboardButton("SY2", callback_data="DEPT:SY2"),
            InlineKeyboardButton("SY3", callback_data="DEPT:SY3"),
            InlineKeyboardButton("SY4", callback_data="DEPT:SY4"),
            InlineKeyboardButton("SY5", callback_data="DEPT:SY5"),
        ],
        [
            InlineKeyboardButton("✍️ Type Custom Department", callback_data="DEPT:CUSTOM"),
        ],
    ]


async def reg_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        return await handle_universal_callback(update, context)
    if not update.message or not update.message.text:
        return REG_NAME

    context.user_data["reg_name"] = update.message.text.strip()
    kb = build_dept_keyboard()
    await update.message.reply_text("Select or type your **Department**:", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return REG_DEPT


async def reg_dept_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = context.user_data

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        cb_data = query.data
        if cb_data == "DEPT:CUSTOM":
            await safe_edit_text(query, "Type your **Department Name** (e.g. `Civil Engineering`):")
            return REG_DEPT
        elif cb_data.startswith("DEPT:"):
            dept = cb_data.replace("DEPT:", "")
            msg_target = query.message
        else:
            return await handle_universal_callback(update, context)
    else:
        if not update.message or not update.message.text:
            return REG_DEPT
        dept = update.message.text.strip()
        msg_target = update.message

    u["reg_dept_val"] = dept

    # Ask for Position / Designation
    kb = [
        [InlineKeyboardButton("Assistant Professor (AP)", callback_data="POS:Assistant Professor:AP")],
        [InlineKeyboardButton("Associate Professor (ASP)", callback_data="POS:Associate Professor:ASP")],
        [InlineKeyboardButton("Professor (PROF)", callback_data="POS:Professor:PROF")],
        [InlineKeyboardButton("Lab Assistant (LA)", callback_data="POS:Lab Assistant:LA")],
        [InlineKeyboardButton("✍️ Type Custom Designation", callback_data="POS:CUSTOM")],
        [InlineKeyboardButton("🔙 Back to Department", callback_data="BACK_TO_DEPT")],
    ]
    prompt = f"Selected Department: **{dept}**\n\n🎖️ Select your **Position / Designation**:"
    if update.callback_query:
        await safe_edit_text(update.callback_query, prompt, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await msg_target.reply_text(prompt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return REG_POS


async def reg_pos_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    u = context.user_data
    emp_code = u.get("reg_emp", "")
    password = u.get("reg_pass", "")
    input_name = u.get("reg_name", "Faculty Member")
    dept = u.get("reg_dept_val", "Civil Engineering")

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        cb_data = query.data

        if cb_data == "BACK_TO_DEPT":
            await safe_edit_text(
                query,
                "Select or type your **Department**:",
                reply_markup=InlineKeyboardMarkup(build_dept_keyboard()),
            )
            return REG_DEPT

        if cb_data == "POS:CUSTOM":
            await safe_edit_text(query, "Type your **Designation / Position** (e.g. `Assistant Professor`):")
            return REG_POS

        if not cb_data.startswith("POS:"):
            return await handle_universal_callback(update, context)

        parts = cb_data.split(":")
        pos_title = parts[1]
        pos_code = parts[2] if len(parts) > 2 else clean_position(pos_title)
        msg_target = query.message
    else:
        if not update.message or not update.message.text:
            return REG_POS
        pos_title = update.message.text.strip()
        pos_code = clean_position(pos_title)
        msg_target = update.message

    u["reg_pos_title"] = pos_title
    u["reg_pos_code"] = pos_code

    login_year = u.get("reg_login_year", os.getenv("LOGIN_YEAR", "01/07/2026LJIET"))
    portal_name = u.get("reg_portal_name")
    final_name = input_name if (input_name and input_name != "Faculty Member") else (portal_name or input_name)
    u["reg_final_name"] = final_name
    u["reg_login_year"] = login_year

    # Try resolving initials from timetable engine
    from timetable_engine import get_timetable_engine
    engine = get_timetable_engine()
    detected_shortname = engine.resolve_faculty_initials(final_name, emp_code=emp_code) or ""
    if not detected_shortname:
        detected_shortname = get_faculty_shortname(final_name)
    if detected_shortname in ["FACULTY", "EMP"]:
        detected_shortname = ""
    u["detected_shortname"] = detected_shortname

    if detected_shortname:
        prompt = (
            f"🏷️ **Timetable Short Name / Initials**\n\n"
            f"• Faculty Name: **{final_name}**\n"
            f"• Detected Initials: `{detected_shortname}`\n\n"
            f"ℹ️ _This short name is used to search your timetable lectures for load adjustments and in generated PDF filenames._\n\n"
            f"Please confirm `{detected_shortname}` or enter your timetable initials (e.g. `IRS`, `MDP`, `PDB`, `DR`):"
        )
        kb = [
            [InlineKeyboardButton(f"✅ Confirm '{detected_shortname}'", callback_data=f"SHORT_CONFIRM:{detected_shortname}")],
            [InlineKeyboardButton("✍️ Enter Custom Short Name", callback_data="SHORT_CUSTOM")],
            [InlineKeyboardButton("🔙 Back to Designation", callback_data="BACK_TO_POS")],
        ]
    else:
        prompt = (
            f"🏷️ **Timetable Short Name / Initials**\n\n"
            f"• Faculty Name: **{final_name}**\n\n"
            f"Please enter your **Timetable Short Name / Initials** (e.g. `IRS`, `MDP`, `PDB`, `DR`):\n\n"
            f"ℹ️ _Used for timetable lecture search, load adjustments, and PDF filenames._"
        )
        kb = [
            [InlineKeyboardButton("🔙 Back to Designation", callback_data="BACK_TO_POS")],
        ]

    if update.callback_query:
        await safe_edit_text(update.callback_query, prompt, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await msg_target.reply_text(prompt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return REG_SHORT_NAME


async def reg_short_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = context.user_data
    user_id = update.effective_user.id

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        cb_data = query.data

        if cb_data == "BACK_TO_POS":
            kb = [
                [InlineKeyboardButton("Assistant Professor (AP)", callback_data="POS:Assistant Professor:AP")],
                [InlineKeyboardButton("Associate Professor (ASP)", callback_data="POS:Associate Professor:ASP")],
                [InlineKeyboardButton("Professor (PROF)", callback_data="POS:Professor:PROF")],
                [InlineKeyboardButton("Lab Assistant (LA)", callback_data="POS:Lab Assistant:LA")],
                [InlineKeyboardButton("✍️ Type Custom Designation", callback_data="POS:CUSTOM")],
                [InlineKeyboardButton("🔙 Back to Department", callback_data="BACK_TO_DEPT")],
            ]
            await safe_edit_text(query, "Select your **Position / Designation**:", reply_markup=InlineKeyboardMarkup(kb))
            return REG_POS

        if cb_data == "SHORT_CUSTOM":
            prompt = (
                "✍️ **Enter Timetable Short Name / Initials**\n\n"
                "Please type your timetable initials (e.g. `IRS`, `MDP`, `PDB`, `DR`):"
            )
            kb = [[InlineKeyboardButton("🔙 Back", callback_data="BACK_TO_SHORT_PICK")]]
            await safe_edit_text(query, prompt, reply_markup=InlineKeyboardMarkup(kb))
            return REG_SHORT_NAME

        if cb_data == "BACK_TO_SHORT_PICK":
            det = u.get("detected_shortname", "")
            final_name = u.get("reg_final_name", "Faculty")
            if det:
                prompt = (
                    f"🏷️ **Timetable Short Name / Initials**\n\n"
                    f"• Faculty Name: **{final_name}**\n"
                    f"• Detected Initials: `{det}`\n\n"
                    f"Please confirm `{det}` or enter custom initials:"
                )
                kb = [
                    [InlineKeyboardButton(f"✅ Confirm '{det}'", callback_data=f"SHORT_CONFIRM:{det}")],
                    [InlineKeyboardButton("✍️ Enter Custom Short Name", callback_data="SHORT_CUSTOM")],
                    [InlineKeyboardButton("🔙 Back to Designation", callback_data="BACK_TO_POS")],
                ]
            else:
                prompt = (
                    f"🏷️ **Timetable Short Name / Initials**\n\n"
                    f"• Faculty Name: **{final_name}**\n\n"
                    f"Please enter your **Timetable Short Name / Initials** (e.g. `IRS`, `MDP`, `PDB`, `DR`):"
                )
                kb = [[InlineKeyboardButton("🔙 Back to Designation", callback_data="BACK_TO_POS")]]
            await safe_edit_text(query, prompt, reply_markup=InlineKeyboardMarkup(kb))
            return REG_SHORT_NAME

        if cb_data.startswith("SHORT_CONFIRM:"):
            short_name = cb_data.split(":", 1)[1].strip().upper()
            msg_target = query.message
        else:
            return await handle_universal_callback(update, context)
    else:
        if not update.message or not update.message.text:
            return REG_SHORT_NAME
        raw = update.message.text.strip().upper()
        short_name = re.sub(r"[^A-Za-z0-9_]", "", raw)
        if not short_name:
            await update.message.reply_text("⚠️ Please enter a valid short name (e.g. `IRS`, `MDP`, `PDB`):")
            return REG_SHORT_NAME
        msg_target = update.message

    u["reg_short_name"] = short_name
    u["reg_initials"] = short_name

    final_name = u.get("reg_final_name", "Faculty")
    emp_code = u.get("reg_emp", "")
    dept = u.get("reg_dept_val", "Civil Engineering")
    pos_title = u.get("reg_pos_title", "Assistant Professor")
    pos_code = u.get("reg_pos_code", "AP")

    kb = [
        [InlineKeyboardButton("🔄 Fetch Balances from Portal", callback_data="BAL_MODE:PORTAL")],
        [InlineKeyboardButton("✍️ Enter Balances Manually", callback_data="BAL_MODE:MANUAL")],
        [InlineKeyboardButton("🔙 Back to Short Name", callback_data="BACK_TO_SHORT_PICK")],
    ]
    prompt = (
        f"✅ **Registration Profile Ready!**\n\n"
        f"• Faculty Name: **{final_name}**\n"
        f"• Timetable Short Name: `{short_name}`\n"
        f"• Employee Code: `{emp_code}`\n"
        f"• Department: **{dept}**\n"
        f"• Designation: **{pos_title}** (`{pos_code}`)\n\n"
        "**Leave Balances Setup:**\n"
        "How would you like to set your leave balances?"
    )
    if update.callback_query:
        await safe_edit_text(update.callback_query, prompt, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await msg_target.reply_text(prompt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return REG_BAL_CHOICE


async def reg_bal_choice_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cb_data = query.data
    u = context.user_data

    if cb_data in ["BACK_TO_POS", "BACK_TO_SHORT_PICK"]:
        det = u.get("reg_short_name") or u.get("detected_shortname", "")
        final_name = u.get("reg_final_name", "Faculty")
        if det:
            prompt = (
                f"🏷️ **Timetable Short Name / Initials**\n\n"
                f"• Faculty Name: **{final_name}**\n"
                f"• Current Initials: `{det}`\n\n"
                f"Please confirm `{det}` or enter custom initials:"
            )
            kb = [
                [InlineKeyboardButton(f"✅ Confirm '{det}'", callback_data=f"SHORT_CONFIRM:{det}")],
                [InlineKeyboardButton("✍️ Enter Custom Short Name", callback_data="SHORT_CUSTOM")],
                [InlineKeyboardButton("🔙 Back to Designation", callback_data="BACK_TO_POS")],
            ]
        else:
            prompt = (
                f"🏷️ **Timetable Short Name / Initials**\n\n"
                f"• Faculty Name: **{final_name}**\n\n"
                f"Please enter your **Timetable Short Name / Initials** (e.g. `IRS`, `MDP`, `PDB`, `DR`):"
            )
            kb = [[InlineKeyboardButton("🔙 Back to Designation", callback_data="BACK_TO_POS")]]
        await safe_edit_text(query, prompt, reply_markup=InlineKeyboardMarkup(kb))
        return REG_SHORT_NAME

    if cb_data == "BAL_MODE:PORTAL":
        await safe_edit_text(query, "⏳ Fetching live leave balances from ARS portal...")
        try:
            portal = PortalSession(
                username=u.get("reg_emp"),
                password=u.get("reg_pass"),
                login_year=u.get("reg_login_year", "01/07/2026LJIET")
            )
            await asyncio.to_thread(portal.login)
            b = await asyncio.to_thread(portal.fetch_balances)

            u["fetched_balances"] = b
            fb = u["fetched_balances"]
            text = (
                f"📊 **Fetched Leave Balances from Portal for {u.get('reg_final_name', u.get('reg_name', 'Faculty'))}**:\n\n"
                f"• Casual Leave (CL): **{fb.get('CL', '0.00')}**\n"
                f"• Short Day (SD): **{fb.get('SD', '0.00')}**\n"
                f"• Earned Leave (EL): **{fb.get('EL', '0.00')}**\n"
                f"• Sick Leave (SL): **{fb.get('SL', '0.00')}**\n"
                f"• Restricted Holiday (RH): **{fb.get('RH', '0.00')}**\n"
                f"• Vacation Leave (VL): **{fb.get('VL', '0.00')}**\n"
                f"• Duty Leave (DL): **{fb.get('DL', '0.00')}**\n"
                f"• Exchanged Leave (Ex.L): **{fb.get('EXL', '0.00')}**\n"
                f"• Women Medical Leave (WML): **{fb.get('WML', '0.00')}**\n"
                f"• Leave Without Pay (LWP): **{fb.get('LWP', '0.00')}**\n\n"
                "Please verify these balances. Would you like to confirm them or edit any balance?"
            )
            kb = [
                [InlineKeyboardButton("✅ Confirm & Save Balances", callback_data="BAL_CONFIRM:SAVE")],
                [InlineKeyboardButton("✏️ Edit Individual Balance", callback_data="BAL_CONFIRM:EDIT_MENU")],
                [InlineKeyboardButton("✍️ Enter Balances Manually", callback_data="BAL_CONFIRM:EDIT_BULK")],
                [InlineKeyboardButton("🔙 Back to Balances Setup", callback_data="BACK_TO_BAL_CHOICE")],
            ]
            await safe_edit_text(query, text, reply_markup=InlineKeyboardMarkup(kb))
            return REG_BAL_VERIFY
        except Exception as e:
            await safe_edit_text(query, f"⚠️ Error fetching balances: {e}\nPlease enter balances manually:")
            return await prompt_manual_balances(query.message, context)
    elif cb_data == "BAL_MODE:MANUAL":
        return await prompt_manual_balances(query.message, context)
    else:
        return await handle_universal_callback(update, context)


def format_balance_editor_view(name: str, balances: dict, back_callback: str = "BACK_TO_BAL_CHOICE", back_label: str = "🔙 Back to Balances Setup"):
    b = balances or {}
    text = (
        f"📊 **Leave Balances for {name}**:\n\n"
        f"• Casual Leave (CL): **{b.get('CL', '0')}**\n"
        f"• Short Day (SD): **{b.get('SD', '0')}**\n"
        f"• Earned Leave (EL): **{b.get('EL', '0')}**\n"
        f"• Sick Leave (SL): **{b.get('SL', '0')}**\n"
        f"• Restricted Holiday (RH): **{b.get('RH', '0')}**\n"
        f"• Vacation Leave (VL): **{b.get('VL', '0')}**\n"
        f"• Duty Leave (DL): **{b.get('DL', '0')}**\n"
        f"• Exchanged Leave (Ex.L): **{b.get('EXL', '0')}**\n"
        f"• Women Medical Leave (WML): **{b.get('WML', '0')}**\n"
        f"• Leave Without Pay (LWP): **{b.get('LWP', '0')}**\n\n"
        "👉 **Tap any leave type below to edit its balance individually**, or save:"
    )
    kb = [
        [
            InlineKeyboardButton(f"✏️ CL: {b.get('CL', '0')}", callback_data="BAL_EDIT:CL"),
            InlineKeyboardButton(f"✏️ SD: {b.get('SD', '0')}", callback_data="BAL_EDIT:SD"),
        ],
        [
            InlineKeyboardButton(f"✏️ EL: {b.get('EL', '0')}", callback_data="BAL_EDIT:EL"),
            InlineKeyboardButton(f"✏️ SL: {b.get('SL', '0')}", callback_data="BAL_EDIT:SL"),
        ],
        [
            InlineKeyboardButton(f"✏️ RH: {b.get('RH', '0')}", callback_data="BAL_EDIT:RH"),
            InlineKeyboardButton(f"✏️ VL: {b.get('VL', '0')}", callback_data="BAL_EDIT:VL"),
        ],
        [
            InlineKeyboardButton(f"✏️ DL: {b.get('DL', '0')}", callback_data="BAL_EDIT:DL"),
            InlineKeyboardButton(f"✏️ Ex.L: {b.get('EXL', '0')}", callback_data="BAL_EDIT:EXL"),
        ],
        [
            InlineKeyboardButton(f"✏️ WML: {b.get('WML', '0')}", callback_data="BAL_EDIT:WML"),
            InlineKeyboardButton(f"✏️ LWP: {b.get('LWP', '0')}", callback_data="BAL_EDIT:LWP"),
        ],
        [
            InlineKeyboardButton("✅ Confirm & Save Balances", callback_data="BAL_CONFIRM:SAVE"),
        ],
        [
            InlineKeyboardButton("✍️ Type Multiple at Once", callback_data="BAL_CONFIRM:EDIT_BULK"),
            InlineKeyboardButton(back_label, callback_data=back_callback),
        ],
    ]
    return text, InlineKeyboardMarkup(kb)


async def prompt_manual_balances(msg_obj, context: ContextTypes.DEFAULT_TYPE = None):
    if context:
        u = context.user_data
        if "fetched_balances" not in u:
            u["fetched_balances"] = {
                "CL": "0", "SD": "0", "EL": "0", "SL": "0", "RH": "0",
                "VL": "0", "DL": "0", "EXL": "0", "WML": "0", "LWP": "0"
            }
        text, markup = format_balance_editor_view(u.get("reg_final_name", u.get("reg_name", "Faculty")), u["fetched_balances"])
        await msg_obj.reply_text(text, reply_markup=markup, parse_mode="Markdown")
        return REG_BAL_MANUAL

    text = (
        "✍️ **Enter Leave Balances Manually**\n\n"
        "Please type your leave balances in key-value format.\n\n"
        "**Example Format:**\n"
        "`CL=12, SL=10, RH=2, VL=0, SD=0, EL=0, DL=0, LWP=0`\n"
    )
    kb = [[InlineKeyboardButton("🔙 Back to Balances Setup", callback_data="BACK_TO_BAL_CHOICE")]]
    await msg_obj.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return REG_BAL_MANUAL


async def prompt_bulk_manual_input(msg_obj, is_profile: bool = False):
    text = (
        "✍️ **Enter Multiple Balances at Once**\n\n"
        "Type any leave balances you want to set in `KEY=VALUE` format.\n"
        "**Example:** `CL=12, SL=10, RH=2`\n\n"
        "Any category not mentioned will keep its current balance."
    )
    back_cb = "BACK_TO_PROFILE" if is_profile else "BACK_TO_BAL_MENU"
    back_lbl = "🔙 Back to Profile" if is_profile else "🔙 Back to Balances Menu"
    kb = [[InlineKeyboardButton(back_lbl, callback_data=back_cb)]]
    await msg_obj.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return REG_BAL_MANUAL


async def reg_bal_verify_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cb_data = query.data
    user_id = update.effective_user.id
    u = context.user_data

    if cb_data == "BACK_TO_BAL_CHOICE":
        kb = [
            [InlineKeyboardButton("🔄 Fetch Balances from Portal", callback_data="BAL_MODE:PORTAL")],
            [InlineKeyboardButton("✍️ Enter Balances Manually", callback_data="BAL_MODE:MANUAL")],
            [InlineKeyboardButton("🔙 Back to Short Name", callback_data="BACK_TO_SHORT_PICK")],
        ]
        await safe_edit_text(query, "How would you like to set your leave balances?", reply_markup=InlineKeyboardMarkup(kb))
        return REG_BAL_CHOICE

    if cb_data == "BAL_CONFIRM:SAVE":
        balances = u.get("fetched_balances", {})
        short_name = u.get("reg_short_name") or u.get("reg_initials") or ""
        profile = {
            "emp_code": u.get("reg_emp"),
            "name": u.get("reg_final_name", u.get("reg_name", "Faculty Member")),
            "short_name": short_name,
            "initials": short_name,
            "dept": u.get("reg_dept_val", "Civil Engineering"),
            "position": u.get("reg_pos_title", "Assistant Professor"),
            "position_code": u.get("reg_pos_code", "AP"),
            "password": u.get("reg_pass"),
            "login_year": u.get("reg_login_year", "01/07/2026LJIET"),
            "balance_mode": "portal",
            "balances": balances,
        }
        save_faculty(user_id, profile)
        welcome_text, welcome_markup = format_welcome_screen(profile)
        await safe_edit_text(query, welcome_text, reply_markup=welcome_markup)
        return ConversationHandler.END

    elif cb_data in ["BAL_CONFIRM:EDIT", "BAL_CONFIRM:EDIT_MENU"]:
        text, markup = format_balance_editor_view(u.get("reg_final_name", u.get("reg_name", "Faculty")), u.get("fetched_balances", {}))
        await safe_edit_text(query, text, reply_markup=markup)
        return REG_BAL_MANUAL

    elif cb_data == "BAL_CONFIRM:EDIT_BULK":
        return await prompt_bulk_manual_input(query.message)
    else:
        return await handle_universal_callback(update, context)


async def reg_bal_manual_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    u = context.user_data

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        cb_data = query.data

        if cb_data == "BACK_TO_BAL_CHOICE":
            kb = [
                [InlineKeyboardButton("🔄 Fetch Balances from Portal", callback_data="BAL_MODE:PORTAL")],
                [InlineKeyboardButton("✍️ Enter Balances Manually", callback_data="BAL_MODE:MANUAL")],
                [InlineKeyboardButton("🔙 Back to Short Name", callback_data="BACK_TO_SHORT_PICK")],
            ]
            await safe_edit_text(query, "How would you like to set your leave balances?", reply_markup=InlineKeyboardMarkup(kb))
            return REG_BAL_CHOICE

        elif cb_data in ["BACK_TO_BAL_MENU", "BAL_EDIT_CANCEL"]:
            u.pop("editing_cat", None)
            is_prof = u.get("is_profile_bal_edit", False)
            back_cb = "BACK_TO_PROFILE" if is_prof else "BACK_TO_BAL_CHOICE"
            back_lbl = "🔙 Back to Profile" if is_prof else "🔙 Back to Balances Setup"
            faculty_rec = get_faculty(user_id) if is_prof else None
            name_val = faculty_rec.get("name") if faculty_rec else u.get("reg_final_name", u.get("reg_name", "Faculty"))
            text, markup = format_balance_editor_view(name_val, u.get("fetched_balances", {}), back_callback=back_cb, back_label=back_lbl)
            await safe_edit_text(query, text, reply_markup=markup)
            return REG_BAL_MANUAL

        elif cb_data == "BACK_TO_PROFILE":
            u.pop("is_profile_bal_edit", None)
            faculty = get_faculty(user_id)
            if faculty:
                p_text, p_markup = format_profile_menu(faculty)
                await safe_edit_text(query, p_text, reply_markup=p_markup)
                return PROF_MENU
            return await profile_start(update, context)

        elif cb_data.startswith("BAL_EDIT:"):
            cat = cb_data.split(":")[1]
            u["editing_cat"] = cat
            curr_val = u.get("fetched_balances", {}).get(cat, "0")
            prompt = (
                f"✏️ **Editing {cat} Balance**\n"
                f"Current Value: **{curr_val}**\n\n"
                f"Please enter the new balance number (e.g. `12` or `10.5`):"
            )
            kb = [[InlineKeyboardButton("🔙 Cancel / Back to Balances", callback_data="BAL_EDIT_CANCEL")]]
            await safe_edit_text(query, prompt, reply_markup=InlineKeyboardMarkup(kb))
            return REG_BAL_MANUAL

        elif cb_data == "BAL_CONFIRM:EDIT_BULK":
            return await prompt_bulk_manual_input(query.message, is_profile=bool(u.get("is_profile_bal_edit")))

        elif cb_data == "BAL_CONFIRM:SAVE":
            balances = u.get("fetched_balances", {})
            if u.get("is_profile_bal_edit"):
                faculty = get_faculty(user_id)
                if faculty:
                    faculty["balances"] = balances
                    faculty["balance_mode"] = "manual"
                    save_faculty(user_id, faculty)
                    u.pop("is_profile_bal_edit", None)
                    p_text, p_markup = format_profile_menu(faculty)
                    await safe_edit_text(query, "✅ **Leave balances updated successfully!**\n\n" + p_text, reply_markup=p_markup)
                    return PROF_MENU
            short_name = u.get("reg_short_name") or u.get("reg_initials") or ""
            profile = {
                "emp_code": u.get("reg_emp"),
                "name": u.get("reg_final_name", u.get("reg_name", "Faculty Member")),
                "short_name": short_name,
                "initials": short_name,
                "dept": u.get("reg_dept_val", "Civil Engineering"),
                "position": u.get("reg_pos_title", "Assistant Professor"),
                "position_code": u.get("reg_pos_code", "AP"),
                "password": u.get("reg_pass"),
                "login_year": u.get("reg_login_year", "01/07/2026LJIET"),
                "balance_mode": "manual",
                "balances": balances,
            }
            save_faculty(user_id, profile)
            welcome_text, welcome_markup = format_welcome_screen(profile)
            await safe_edit_text(query, welcome_text, reply_markup=welcome_markup)
            return ConversationHandler.END

        return await handle_universal_callback(update, context)

    if not update.message or not update.message.text:
        return REG_BAL_MANUAL

    text = update.message.text.strip()

    # Case A: User was editing a single specific category (e.g. CL)
    if u.get("editing_cat"):
        cat = u.pop("editing_cat")
        m = re.search(r"[\d.]+", text)
        if m:
            val = m.group(0)
            u.setdefault("fetched_balances", {})[cat] = val
            is_prof = u.get("is_profile_bal_edit", False)
            if is_prof:
                faculty_rec = get_faculty(user_id)
                if faculty_rec:
                    faculty_rec.setdefault("balances", {})[cat] = val
                    faculty_rec["balance_mode"] = "manual"
                    save_faculty(user_id, faculty_rec)
            await update.message.reply_text(f"✅ Updated **{cat}** balance to **{val}**.")
            back_cb = "BACK_TO_PROFILE" if is_prof else "BACK_TO_BAL_CHOICE"
            back_lbl = "🔙 Back to Profile" if is_prof else "🔙 Back to Balances Setup"
            faculty_rec = get_faculty(user_id) if is_prof else None
            name_val = faculty_rec.get("name") if faculty_rec else u.get("reg_final_name", u.get("reg_name", "Faculty"))
            text_menu, markup = format_balance_editor_view(name_val, u["fetched_balances"], back_callback=back_cb, back_label=back_lbl)
            await update.message.reply_text(text_menu, reply_markup=markup, parse_mode="Markdown")
            return REG_BAL_MANUAL
        else:
            u["editing_cat"] = cat
            await update.message.reply_text(f"⚠️ Could not parse a valid number for {cat}. Please enter a number:")
            return REG_BAL_MANUAL

    # Case B: User entered multiple key-values (e.g. "CL=12, SL=10...")
    categories = ["CL", "SL", "RH", "SD", "VL", "EL", "DL", "LWP", "EXL", "WML"]
    parsed = {}
    for cat in categories:
        m = re.search(rf"\b{cat}\b\s*[:=]\s*([\d.]+)", text, re.IGNORECASE)
        if m:
            parsed[cat] = m.group(1)

    if parsed:
        u.setdefault("fetched_balances", {}).update(parsed)

    # If full manual list entered (>= 3 categories), finalize and save
    if len(parsed) >= 3:
        for cat in categories:
            if cat not in u["fetched_balances"]:
                u["fetched_balances"][cat] = "0.0"
        if u.get("is_profile_bal_edit"):
            faculty = get_faculty(user_id)
            if faculty:
                faculty["balances"] = u["fetched_balances"]
                faculty["balance_mode"] = "manual"
                save_faculty(user_id, faculty)
                u.pop("is_profile_bal_edit", None)
                p_text, p_markup = format_profile_menu(faculty)
                await update.message.reply_text("✅ **Leave balances updated successfully!**\n\n" + p_text, reply_markup=p_markup, parse_mode="Markdown")
                return PROF_MENU
        short_name = u.get("reg_short_name") or u.get("reg_initials") or ""
        profile = {
            "emp_code": u.get("reg_emp"),
            "name": u.get("reg_final_name", u.get("reg_name", "Faculty Member")),
            "short_name": short_name,
            "initials": short_name,
            "dept": u.get("reg_dept_val", "Civil Engineering"),
            "position": u.get("reg_pos_title", "Assistant Professor"),
            "position_code": u.get("reg_pos_code", "AP"),
            "password": u.get("reg_pass"),
            "login_year": u.get("reg_login_year", "01/07/2026LJIET"),
            "balance_mode": "manual",
            "balances": u["fetched_balances"],
        }
        save_faculty(user_id, profile)
        welcome_text, welcome_markup = format_welcome_screen(profile)
        await update.message.reply_text(welcome_text, reply_markup=welcome_markup, parse_mode="Markdown")
        return ConversationHandler.END
    elif parsed:
        is_prof = u.get("is_profile_bal_edit", False)
        back_cb = "BACK_TO_PROFILE" if is_prof else "BACK_TO_BAL_CHOICE"
        back_lbl = "🔙 Back to Profile" if is_prof else "🔙 Back to Balances Setup"
        faculty_rec = get_faculty(user_id) if is_prof else None
        name_val = faculty_rec.get("name") if faculty_rec else u.get("reg_final_name", u.get("reg_name", "Faculty"))
        text_menu, markup = format_balance_editor_view(name_val, u["fetched_balances"], back_callback=back_cb, back_label=back_lbl)
        await update.message.reply_text(f"✅ Updated {len(parsed)} balance(s).", reply_markup=markup, parse_mode="Markdown")
        return REG_BAL_MANUAL
    else:
        await update.message.reply_text("⚠️ Please tap one of the category buttons to edit its balance, or type `CL=12, SL=10`.")
        return REG_BAL_MANUAL


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass

    user_id = update.effective_user.id
    faculty = get_faculty(user_id)
    if not faculty:
        await update.effective_message.reply_text("Please /register first.")
        return

    b = faculty.get("balances", {})
    kb = [
        [
            InlineKeyboardButton("🔄 Sync from ARS Portal", callback_data="PROF_EDIT:PORTAL_SYNC"),
            InlineKeyboardButton("✏️ Edit Balances", callback_data="PROF_EDIT:BAL"),
        ],
        [
            InlineKeyboardButton("🔙 Back to Main Menu", callback_data="CMD_WELCOME"),
        ],
    ]
    text = (
        f"📊 **Complete Leave Balances for {faculty.get('name', 'Faculty Member')}** (`{faculty.get('emp_code', '')}`):\n\n"
        f"• Casual Leave (CL): **{b.get('CL', '0')}**\n"
        f"• Short Day (SD): **{b.get('SD', '0')}**\n"
        f"• Earned Leave (EL): **{b.get('EL', '0')}**\n"
        f"• Sick Leave (SL): **{b.get('SL', '0')}**\n"
        f"• Restricted Holiday (RH): **{b.get('RH', '0')}**\n"
        f"• Leave Without Pay (LWP): **{b.get('LWP', '0')}**\n"
        f"• Vacation Leave (VL): **{b.get('VL', '0')}**\n"
        f"• Duty Leave (DL): **{b.get('DL', '0')}**\n"
        f"• Exchanged Leave (Ex.L): **{b.get('EXL', '0')}**\n"
        f"• Women Medical Leave (WML): **{b.get('WML', '0')}**\n\n"
        f"_(Shows your current updated balance in the bot. Tap 'Sync from ARS Portal' if you wish to re-pull from portal)_"
    )
    if update.callback_query:
        await safe_edit_text(update.callback_query, text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")


# ==========================================
# PROFILE MANAGEMENT (VIEW / EDIT / PORTAL SYNC)
# ==========================================
def format_profile_menu(faculty: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Generates the profile view and interactive editing options."""
    b = faculty.get("balances", {})
    pos = faculty.get("position", "Assistant Professor")
    pos_code = faculty.get("position_code") or ("AP" if "Assistant" in pos else "ASP" if "Associate" in pos else "PROF" if "Prof" in pos else "LA" if "Lab" in pos else "AP")
    session = faculty.get("login_year", "01/07/2026LJIET")
    b_mode = "Portal Verified" if faculty.get("balance_mode") == "portal" else "Custom / Manual"
    pwd = faculty.get("password", "")
    masked_pwd = ("•" * len(pwd)) if pwd else "Not set"
    short_disp = faculty.get("short_name") or faculty.get("initials") or "Not set"

    text = (
        f"👤 **Faculty Profile Management**\n\n"
        f"• **Name:** {faculty.get('name', 'Faculty Member')}\n"
        f"• **Timetable Short Name:** `{short_disp}`\n"
        f"• **Employee Code:** `{faculty.get('emp_code', '')}`\n"
        f"• **Department:** {faculty.get('dept', 'Civil Engineering')}\n"
        f"• **Designation:** {pos} (`{pos_code}`)\n"
        f"• **ARS Password:** `{masked_pwd}`\n"
        f"• **Session Year:** `{session}`\n"
        f"• **Balances Mode:** {b_mode}\n\n"
        f"📊 **Current Balances:**\n"
        f"CL: **{b.get('CL', '0')}** | SD: **{b.get('SD', '0')}** | EL: **{b.get('EL', '0')}** | SL: **{b.get('SL', '0')}** | RH: **{b.get('RH', '0')}**\n"
        f"VL: **{b.get('VL', '0')}** | DL: **{b.get('DL', '0')}** | Ex.L: **{b.get('EXL', '0')}** | WML: **{b.get('WML', '0')}** | LWP: **{b.get('LWP', '0')}**\n\n"
        f"Select an option to update or edit:"
    )
    kb = [
        [
            InlineKeyboardButton("✏️ Edit Name", callback_data="PROF_EDIT:NAME"),
            InlineKeyboardButton("🏷️ Edit Short Name", callback_data="PROF_EDIT:INITIALS"),
        ],
        [
            InlineKeyboardButton("🏢 Edit Department", callback_data="PROF_EDIT:DEPT"),
            InlineKeyboardButton("🎖️ Edit Designation", callback_data="PROF_EDIT:POS"),
        ],
        [
            InlineKeyboardButton("🔑 Edit Password", callback_data="PROF_EDIT:PASS"),
            InlineKeyboardButton("📊 Edit Leave Balances", callback_data="PROF_EDIT:BAL"),
        ],
        [
            InlineKeyboardButton("🔄 Sync from Portal", callback_data="PROF_EDIT:PORTAL_SYNC"),
            InlineKeyboardButton("🔙 Back to Main Menu", callback_data="CMD_WELCOME"),
        ],
    ]
    return text, InlineKeyboardMarkup(kb)


async def profile_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    faculty = get_faculty(user_id)

    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass

    if not faculty:
        text = (
            "👋 **No faculty profile found.**\n\n"
            "Please register with your ARS User ID & Password first."
        )
        kb = [[InlineKeyboardButton("🔐 Register with ARS Credentials", callback_data="START_REG")]]
        if update.callback_query:
            await safe_edit_text(update.callback_query, text, reply_markup=InlineKeyboardMarkup(kb))
        else:
            await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return ConversationHandler.END

    text, markup = format_profile_menu(faculty)
    if update.callback_query:
        await safe_edit_text(update.callback_query, text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    return PROF_MENU


async def profile_menu_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return PROF_MENU
    await query.answer()
    data = query.data or ""
    user_id = update.effective_user.id
    faculty = get_faculty(user_id)
    u = context.user_data

    if not faculty:
        return await profile_start(update, context)

    if data in ["BACK_TO_PROFILE", "PROF_CANCEL"]:
        u.pop("is_profile_bal_edit", None)
        text, markup = format_profile_menu(faculty)
        await safe_edit_text(query, text, reply_markup=markup)
        return PROF_MENU

    if data == "CMD_WELCOME":
        text, markup = format_welcome_screen(faculty)
        await safe_edit_text(query, text, reply_markup=markup)
        return ConversationHandler.END

    if data == "PROF_EDIT:NAME":
        curr_name = faculty.get("name", "Faculty Member")
        prompt = (
            f"✏️ **Update Faculty Name**\n\n"
            f"Current Name: **{curr_name}**\n\n"
            f"Please enter your new full name:"
        )
        kb = [[InlineKeyboardButton("🔙 Cancel / Back to Profile", callback_data="BACK_TO_PROFILE")]]
        await safe_edit_text(query, prompt, reply_markup=InlineKeyboardMarkup(kb))
        return PROF_INPUT_NAME

    if data == "PROF_EDIT:INITIALS":
        curr_short = faculty.get("short_name") or faculty.get("initials") or "Not set"
        prompt = (
            f"🏷️ **Update Timetable Short Name / Initials**\n\n"
            f"Current Short Name: **{curr_short}**\n\n"
            f"Please enter your new timetable short name / initials (e.g. `IRS`, `MDP`, `PDB`, `DR`):\n\n"
            f"ℹ️ _This short name is used to search your timetable lectures for load adjustments and in PDF filenames._"
        )
        kb = [[InlineKeyboardButton("🔙 Cancel / Back to Profile", callback_data="BACK_TO_PROFILE")]]
        await safe_edit_text(query, prompt, reply_markup=InlineKeyboardMarkup(kb))
        return PROF_INPUT_INITIALS

    if data == "PROF_EDIT:DEPT":
        curr_dept = faculty.get("dept", "Civil Engineering")
        prompt = (
            f"🏢 **Update Department**\n\n"
            f"Current Department: **{curr_dept}**\n\n"
            f"Select your Department or type a custom one:"
        )
        kb = build_dept_keyboard() + [[InlineKeyboardButton("🔙 Cancel / Back to Profile", callback_data="BACK_TO_PROFILE")]]
        await safe_edit_text(query, prompt, reply_markup=InlineKeyboardMarkup(kb))
        return PROF_INPUT_DEPT

    if data == "PROF_EDIT:POS":
        curr_pos = faculty.get("position", "Assistant Professor")
        curr_code = faculty.get("position_code", "AP")
        prompt = (
            f"🎖️ **Update Designation / Position**\n\n"
            f"Current: **{curr_pos}** (`{curr_code}`)\n\n"
            f"Select your new Position / Designation:"
        )
        kb = [
            [InlineKeyboardButton("Assistant Professor (AP)", callback_data="POS:Assistant Professor:AP")],
            [InlineKeyboardButton("Associate Professor (ASP)", callback_data="POS:Associate Professor:ASP")],
            [InlineKeyboardButton("Professor (PROF)", callback_data="POS:Professor:PROF")],
            [InlineKeyboardButton("Lab Assistant (LA)", callback_data="POS:Lab Assistant:LA")],
            [InlineKeyboardButton("✍️ Type Custom Designation", callback_data="POS:CUSTOM")],
            [InlineKeyboardButton("🔙 Cancel / Back to Profile", callback_data="BACK_TO_PROFILE")],
        ]
        await safe_edit_text(query, prompt, reply_markup=InlineKeyboardMarkup(kb))
        return PROF_INPUT_POS

    if data == "PROF_EDIT:PASS":
        prompt = (
            f"🔑 **Update ARS Portal Password**\n\n"
            f"Please enter your new ARS portal password:"
        )
        kb = [[InlineKeyboardButton("🔙 Cancel / Back to Profile", callback_data="BACK_TO_PROFILE")]]
        await safe_edit_text(query, prompt, reply_markup=InlineKeyboardMarkup(kb))
        return PROF_INPUT_PASS

    if data == "PROF_EDIT:BAL":
        u["fetched_balances"] = dict(faculty.get("balances", {}))
        u["is_profile_bal_edit"] = True
        text, markup = format_balance_editor_view(
            faculty.get("name", "Faculty"),
            u["fetched_balances"],
            back_callback="BACK_TO_PROFILE",
            back_label="🔙 Back to Profile"
        )
        await safe_edit_text(query, text, reply_markup=markup)
        return REG_BAL_MANUAL

    if data == "PROF_EDIT:PORTAL_SYNC":
        await safe_edit_text(query, "⏳ Fetching live leave balances from ARS portal...")
        try:
            api = LeavePortalAPI(
                username=faculty.get("emp_code"),
                password=faculty.get("password"),
                login_year=faculty.get("login_year", "01/07/2026LJIET")
            )
            succ, msg = await asyncio.to_thread(api.login)
            if succ:
                b = await asyncio.to_thread(api.get_all_balances)
                faculty["balances"] = {
                    "CL": str(b.get("CL", "0")),
                    "SD": str(b.get("SD", "0")),
                    "EL": str(b.get("EL", "0")),
                    "SL": str(b.get("SL", "0")),
                    "RH": str(b.get("RH", "0")),
                    "LWP": str(b.get("LWP", "0")),
                    "VL": str(b.get("VL", "0")),
                    "DL": str(b.get("DL", "0")),
                    "EXL": str(b.get("EXL", "0")),
                    "WML": str(b.get("WML", "0")),
                }
                faculty["balance_mode"] = "portal"
                save_faculty(user_id, faculty)
                p_text, p_markup = format_profile_menu(faculty)
                await safe_edit_text(query, "✅ **Leave balances synced successfully from ARS Portal!**\n\n" + p_text, reply_markup=p_markup)
            else:
                p_text, p_markup = format_profile_menu(faculty)
                await safe_edit_text(query, f"⚠️ Portal Sync Failed: {msg}. Please check your ARS password.\n\n" + p_text, reply_markup=p_markup)
        except Exception as e:
            p_text, p_markup = format_profile_menu(faculty)
            await safe_edit_text(query, f"⚠️ Error syncing balances: {e}\n\n" + p_text, reply_markup=p_markup)
        return PROF_MENU

    return await handle_universal_callback(update, context)


async def profile_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    faculty = get_faculty(user_id)
    if not faculty:
        return await profile_start(update, context)

    if update.callback_query:
        return await profile_menu_picked(update, context)

    if not update.message or not update.message.text:
        return PROF_INPUT_NAME

    new_name = update.message.text.strip()
    faculty["name"] = new_name
    save_faculty(user_id, faculty)

    p_text, p_markup = format_profile_menu(faculty)
    await update.message.reply_text(f"✅ Faculty name updated to **{new_name}**!\n\n" + p_text, reply_markup=p_markup, parse_mode="Markdown")
    return PROF_MENU


async def profile_initials_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    faculty = get_faculty(user_id)
    if not faculty:
        return await profile_start(update, context)

    if update.callback_query:
        return await profile_menu_picked(update, context)

    if not update.message or not update.message.text:
        return PROF_INPUT_INITIALS

    raw = update.message.text.strip().upper()
    new_initials = re.sub(r"[^A-Za-z0-9_]", "", raw)
    if not new_initials:
        await update.message.reply_text("⚠️ Please enter a valid short name (e.g. `IRS`, `MDP`, `PDB`):")
        return PROF_INPUT_INITIALS

    faculty["short_name"] = new_initials
    faculty["initials"] = new_initials
    save_faculty(user_id, faculty)

    p_text, p_markup = format_profile_menu(faculty)
    await update.message.reply_text(
        f"✅ Timetable short name updated to **{new_initials}**!\n\n" + p_text,
        reply_markup=p_markup,
        parse_mode="Markdown"
    )
    return PROF_MENU


async def profile_dept_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    faculty = get_faculty(user_id)
    if not faculty:
        return await profile_start(update, context)

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        cb_data = query.data or ""
        if cb_data in ["BACK_TO_PROFILE", "PROF_CANCEL"]:
            return await profile_menu_picked(update, context)
        if cb_data == "DEPT:CUSTOM":
            await safe_edit_text(query, "Type your **Department Name** (e.g. `Civil Engineering`):")
            return PROF_INPUT_DEPT
        elif cb_data.startswith("DEPT:"):
            dept = cb_data.replace("DEPT:", "")
            faculty["dept"] = dept
            save_faculty(user_id, faculty)
            p_text, p_markup = format_profile_menu(faculty)
            await safe_edit_text(query, f"✅ Department updated to **{dept}**!\n\n" + p_text, reply_markup=p_markup)
            return PROF_MENU
        return await handle_universal_callback(update, context)

    if not update.message or not update.message.text:
        return PROF_INPUT_DEPT

    dept = update.message.text.strip()
    faculty["dept"] = dept
    save_faculty(user_id, faculty)
    p_text, p_markup = format_profile_menu(faculty)
    await update.message.reply_text(f"✅ Department updated to **{dept}**!\n\n" + p_text, reply_markup=p_markup, parse_mode="Markdown")
    return PROF_MENU


async def profile_pos_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    faculty = get_faculty(user_id)
    if not faculty:
        return await profile_start(update, context)

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        cb_data = query.data or ""
        if cb_data in ["BACK_TO_PROFILE", "PROF_CANCEL"]:
            return await profile_menu_picked(update, context)
        if cb_data == "POS:CUSTOM":
            await safe_edit_text(query, "Type your **Designation / Position** (e.g. `Assistant Professor`):")
            return PROF_INPUT_POS
        if cb_data.startswith("POS:"):
            parts = cb_data.split(":")
            pos_title = parts[1]
            pos_code = parts[2] if len(parts) > 2 else clean_position(pos_title)
            faculty["position"] = pos_title
            faculty["position_code"] = pos_code
            save_faculty(user_id, faculty)
            p_text, p_markup = format_profile_menu(faculty)
            await safe_edit_text(query, f"✅ Designation updated to **{pos_title}** (`{pos_code}`)!\n\n" + p_text, reply_markup=p_markup)
            return PROF_MENU
        return await handle_universal_callback(update, context)

    if not update.message or not update.message.text:
        return PROF_INPUT_POS

    pos_title = update.message.text.strip()
    pos_code = clean_position(pos_title)
    faculty["position"] = pos_title
    faculty["position_code"] = pos_code
    save_faculty(user_id, faculty)
    p_text, p_markup = format_profile_menu(faculty)
    await update.message.reply_text(f"✅ Designation updated to **{pos_title}** (`{pos_code}`)!\n\n" + p_text, reply_markup=p_markup, parse_mode="Markdown")
    return PROF_MENU


async def profile_pass_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    faculty = get_faculty(user_id)
    if not faculty:
        return await profile_start(update, context)

    if update.callback_query:
        return await profile_menu_picked(update, context)

    if not update.message or not update.message.text:
        return PROF_INPUT_PASS

    new_pass = update.message.text.strip()
    faculty["password"] = new_pass
    save_faculty(user_id, faculty)
    p_text, p_markup = format_profile_menu(faculty)
    await update.message.reply_text("✅ ARS Portal password updated successfully!\n\n" + p_text, reply_markup=p_markup, parse_mode="Markdown")
    return PROF_MENU


# ==========================================
# ATTENDANCE & PUNCH DETAILS (ARS PORTAL)
# ==========================================
def format_punch_card(record: dict, faculty: dict, target_date_str: str) -> tuple[str, InlineKeyboardMarkup]:
    """Formats the interactive Telegram punch card for a specific date."""
    pos = faculty.get("position", "Faculty")
    pos_code = faculty.get("position_code", "AP")
    emp_code = faculty.get("emp_code", "")
    dept = faculty.get("dept", "Civil Engineering")

    try:
        dt_obj = datetime.strptime(target_date_str, "%d/%m/%Y")
        date_formatted = dt_obj.strftime("%A, %d/%m/%Y")
        is_today = (dt_obj.date() == get_ist_today())
        is_yesterday = (dt_obj.date() == (get_ist_today() - timedelta(days=1)))
    except Exception:
        date_formatted = target_date_str
        is_today = False
        is_yesterday = False

    date_badge = " (Today)" if is_today else " (Yesterday)" if is_yesterday else ""

    if not record:
        text = (
            f"⏱️ **Attendance & Punch Details**\n"
            f"📅 **Date:** {date_formatted}{date_badge}\n"
            f"👤 **Faculty:** {faculty.get('name', 'Faculty')} (`{emp_code}`)\n"
            f"🏢 **Department:** {dept}\n\n"
            f"ℹ️ **No attendance / punch record found on ARS portal for this date.**\n"
            f"(It may be a Sunday, unassigned date, or yet to be uploaded by ARS system.)"
        )
    else:
        shift = record.get("shift") or "T2"
        in_t = record.get("in_time") or ""
        out_t = record.get("out_time") or ""
        late = record.get("late") or ""
        early = record.get("early") or ""
        wrk_hr = record.get("wrk_hr") or ""
        leave = record.get("leave") or ""
        week_off = record.get("week_off") or ""
        holiday = record.get("holiday") or ""
        absent = record.get("absent") or ""
        extra_less = record.get("extra_less") or ""

        in_disp = f"🟢 **{in_t}**" if in_t else "⚪ **Not Punched**"

        if out_t:
            out_disp = f"🔴 **{out_t}**"
        elif in_t and is_today:
            out_disp = "⏳ **Pending (Day in Progress)**"
        elif in_t:
            out_disp = "⚠️ **Missing / Not Punched**"
        else:
            out_disp = "⚪ **Not Punched**"

        wrk_disp = f"`{wrk_hr} hrs`" if wrk_hr else "`00:00 hrs`" if (in_t and is_today) else "`N/A`"

        remarks = []
        if week_off:
            remarks.append(f"🏖️ Week Off ({week_off})")
        if holiday:
            remarks.append(f"🎉 Holiday ({holiday})")
        if leave:
            remarks.append(f"📝 Leave ({leave})")
        if absent:
            remarks.append(f"❌ {absent}")
        if late:
            remarks.append(f"⚠️ Late by `{late}`")
        if early:
            remarks.append(f"⚠️ Left early by `{early}`")
        if extra_less and extra_less.strip() != "00:00":
            remarks.append(f"⚖️ Extra/Less: `{extra_less}`")

        if not remarks:
            if in_t and out_t:
                remarks.append("✅ Full Day Completed (On Time)")
            elif in_t:
                remarks.append("🟢 Present & On Duty")
            else:
                remarks.append("⚪ No Punches Recorded")

        remark_disp = "\n• ".join(remarks)

        text = (
            f"⏱️ **Attendance & Punch Details**\n"
            f"📅 **Date:** {date_formatted}{date_badge}\n"
            f"👤 **Faculty:** {faculty.get('name', 'Faculty')} (`{emp_code}`)\n"
            f"🏢 **Department:** {dept} | **Shift:** `{shift}`\n\n"
            f"🕒 **Punch Timings:**\n"
            f"• **In Punch:** {in_disp}\n"
            f"• **Out Punch:** {out_disp}\n"
            f"• **Net Working Hours:** {wrk_disp}\n\n"
            f"📌 **Status / Remarks:**\n"
            f"• {remark_disp}"
        )

    today_str = get_ist_today_str("%d/%m/%Y")
    yest_str = (get_ist_today() - timedelta(days=1)).strftime("%d/%m/%Y")

    kb = [
        [
            InlineKeyboardButton("🔄 Refresh Today", callback_data=f"ATT_DATE:{today_str}"),
            InlineKeyboardButton("📅 Yesterday", callback_data=f"ATT_DATE:{yest_str}"),
        ],
        [
            InlineKeyboardButton("📆 Pick Date", callback_data="ATT_PICK_DATE"),
            InlineKeyboardButton("📋 Last 7 Days", callback_data="ATT_RECENT_7"),
        ],
        [
            InlineKeyboardButton("🔙 Back to Main Menu", callback_data="CMD_WELCOME"),
        ],
    ]
    return text, InlineKeyboardMarkup(kb)


def format_recent_punches(records: dict, faculty: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Renders a chronological 7-day digest of recent punches."""
    emp_code = faculty.get("emp_code", "")
    text = (
        f"📋 **Recent Attendance Summary (Last 7 Days)**\n"
        f"👤 Faculty: **{faculty.get('name', 'Faculty')}** (`{emp_code}`)\n"
        f"🏢 Dept: {faculty.get('dept', 'Civil Engineering')}\n\n"
    )
    now = get_ist_now()
    lines = []
    for i in range(7):
        d = now - timedelta(days=i)
        d_str = d.strftime("%d/%m/%Y")
        d_short = d.strftime("%d/%m (%a)")
        rec = records.get(d_str, {})
        in_t = rec.get("in_time", "")
        out_t = rec.get("out_time", "")
        wrk = rec.get("wrk_hr", "")
        wo = rec.get("week_off", "")
        lv = rec.get("leave", "")
        ab = rec.get("absent", "")

        if wo:
            status = f"🏖️ Week Off ({wo})"
        elif lv:
            status = f"📝 Leave ({lv})"
        elif ab and not in_t:
            status = f"❌ {ab}"
        elif in_t and out_t:
            status = f"In: `{in_t}` | Out: `{out_t}` ({wrk}h)"
        elif in_t:
            status = f"In: `{in_t}` | Out: `Pending`"
        else:
            status = "⚪ No punch"

        prefix = "👉 " if i == 0 else "• "
        lines.append(f"{prefix}**{d_short}:** {status}")

    text += "\n".join(lines)
    today_str = now.strftime("%d/%m/%Y")
    kb = [
        [
            InlineKeyboardButton("⏱️ View Today's Punch", callback_data=f"ATT_DATE:{today_str}"),
            InlineKeyboardButton("📆 Pick Date", callback_data="ATT_PICK_DATE"),
        ],
        [
            InlineKeyboardButton("🔙 Back to Main Menu", callback_data="CMD_WELCOME"),
        ],
    ]
    return text, InlineKeyboardMarkup(kb)


def format_attendance_date_picker(faculty: dict) -> tuple[str, InlineKeyboardMarkup]:
    now = get_ist_now()
    text = (
        "📆 **Select a Date to View Attendance & Punch Details**:\n\n"
        "Tap one of the recent dates below, or enter any date manually in `DD/MM/YYYY` format:"
    )
    kb = []
    row = []
    for i in range(6):
        d = now - timedelta(days=i)
        d_str = d.strftime("%d/%m/%Y")
        d_label = ("Today" if i == 0 else "Yesterday" if i == 1 else d.strftime("%d %b (%a)"))
        row.append(InlineKeyboardButton(f"📅 {d_label}", callback_data=f"ATT_DATE:{d_str}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)

    kb.append([InlineKeyboardButton("✍️ Type Custom Date (DD/MM/YYYY)", callback_data="ATT_CUSTOM_PROMPT")])
    today_str = now.strftime("%d/%m/%Y")
    kb.append([InlineKeyboardButton("🔙 Back to Today's Punch", callback_data=f"ATT_DATE:{today_str}")])
    return text, InlineKeyboardMarkup(kb)


async def fetch_and_display_attendance(update: Update, context: ContextTypes.DEFAULT_TYPE, faculty: dict, target_date_str: str):
    """Fetches attendance from portal and edits/replies with the formatted punch card."""
    user_id = update.effective_user.id
    status_msg = None
    if update.callback_query:
        try:
            await safe_edit_text(update.callback_query, f"⏳ Fetching live punch details for **{target_date_str}** from ARS portal...")
        except Exception:
            pass
    elif update.effective_message:
        status_msg = await update.effective_message.reply_text(f"⏳ Fetching live punch details for **{target_date_str}** from ARS portal...")

    try:
        api = LeavePortalAPI(
            username=faculty.get("emp_code"),
            password=faculty.get("password"),
            login_year=faculty.get("login_year", "01/07/2026LJIET")
        )
        succ, login_msg = await asyncio.to_thread(api.login)
        if not succ:
            err_text = (
                f"❌ **ARS Portal Login Failed:** {login_msg}\n\n"
                f"Please update your password via /profile or tap below:"
            )
            kb = [
                [InlineKeyboardButton("👤 Update Profile / Password", callback_data="CMD_EDIT_PROFILE")],
                [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="CMD_WELCOME")]
            ]
            if update.callback_query:
                await safe_edit_text(update.callback_query, err_text, reply_markup=InlineKeyboardMarkup(kb))
            elif status_msg:
                await status_msg.edit_text(err_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
            return

        res = await asyncio.to_thread(api.get_attendance, target_date_str)
        if not res.get("success", False):
            err_text = f"⚠️ Could not read attendance details: {res.get('error', 'Unknown portal error')}."
            kb = [[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="CMD_WELCOME")]]
            if update.callback_query:
                await safe_edit_text(update.callback_query, err_text, reply_markup=InlineKeyboardMarkup(kb))
            elif status_msg:
                await status_msg.edit_text(err_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
            return

        records = res.get("records", {})
        context.user_data["cached_attendance"] = records
        rec = records.get(target_date_str)

        card_text, card_markup = format_punch_card(rec, faculty, target_date_str)
        if update.callback_query:
            await safe_edit_text(update.callback_query, card_text, reply_markup=card_markup)
        elif status_msg:
            await status_msg.edit_text(card_text, reply_markup=card_markup, parse_mode="Markdown")
        else:
            await update.effective_message.reply_text(card_text, reply_markup=card_markup, parse_mode="Markdown")
    except Exception as e:
        err_text = f"⚠️ Error fetching attendance: {e}"
        kb = [[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="CMD_WELCOME")]]
        if update.callback_query:
            await safe_edit_text(update.callback_query, err_text, reply_markup=InlineKeyboardMarkup(kb))
        elif status_msg:
            await status_msg.edit_text(err_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        else:
            await update.effective_message.reply_text(err_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")


async def attendance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for /attendance or /punch or CMD_ATTENDANCE."""
    user_id = update.effective_user.id
    faculty = get_faculty(user_id)

    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass

    if not faculty:
        text = "⚠️ Please /register with your ARS credentials first to view attendance."
        kb = [[InlineKeyboardButton("🔐 Register Account", callback_data="START_REG")]]
        if update.callback_query:
            await safe_edit_text(update.callback_query, text, reply_markup=InlineKeyboardMarkup(kb))
        else:
            await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return ConversationHandler.END

    target_date = get_ist_today_str("%d/%m/%Y")
    if context.args and len(context.args) > 0:
        arg = context.args[0].strip()
        m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", arg)
        if m:
            d, mo, y = m.groups()
            target_date = f"{d.zfill(2)}/{mo.zfill(2)}/{y}"

    await fetch_and_display_attendance(update, context, faculty, target_date)
    return ATT_CUSTOM_DATE


async def attendance_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    user_id = update.effective_user.id
    faculty = get_faculty(user_id)

    if not faculty:
        return await attendance_cmd(update, context)

    if data.startswith("ATT_DATE:"):
        date_str = data.replace("ATT_DATE:", "")
        await fetch_and_display_attendance(update, context, faculty, date_str)
        return ATT_CUSTOM_DATE

    elif data == "ATT_RECENT_7":
        records = context.user_data.get("cached_attendance")
        if records:
            text, markup = format_recent_punches(records, faculty)
            await safe_edit_text(query, text, reply_markup=markup)
        else:
            today_str = get_ist_today_str("%d/%m/%Y")
            await fetch_and_display_attendance(update, context, faculty, today_str)
            records = context.user_data.get("cached_attendance", {})
            text, markup = format_recent_punches(records, faculty)
            await safe_edit_text(query, text, reply_markup=markup)
        return ATT_CUSTOM_DATE

    elif data == "ATT_PICK_DATE":
        text, markup = format_attendance_date_picker(faculty)
        await safe_edit_text(query, text, reply_markup=markup)
        return ATT_CUSTOM_DATE

    elif data == "ATT_CUSTOM_PROMPT":
        prompt = (
            "✍️ **Enter Date for Attendance / Punch Details**\n\n"
            "Please type the date in `DD/MM/YYYY` format (e.g. `05/09/2026`):"
        )
        today_str = get_ist_today_str("%d/%m/%Y")
        kb = [[InlineKeyboardButton("🔙 Back to Today's Punch", callback_data=f"ATT_DATE:{today_str}")]]
        await safe_edit_text(query, prompt, reply_markup=InlineKeyboardMarkup(kb))
        return ATT_CUSTOM_DATE

    elif data == "CMD_WELCOME":
        text, markup = format_welcome_screen(faculty)
        await safe_edit_text(query, text, reply_markup=markup)
        return ConversationHandler.END

    return await handle_universal_callback(update, context)


async def attendance_custom_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    faculty = get_faculty(user_id)
    if not faculty:
        return await attendance_cmd(update, context)

    if update.callback_query:
        return await attendance_callback_handler(update, context)

    if not update.message or not update.message.text:
        return ATT_CUSTOM_DATE

    raw = update.message.text.strip()
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", raw)
    if not m:
        today_str = get_ist_today_str("%d/%m/%Y")
        kb = [
            [InlineKeyboardButton("📆 Quick Pick Date", callback_data="ATT_PICK_DATE")],
            [InlineKeyboardButton("🔙 Back to Today's Punch", callback_data=f"ATT_DATE:{today_str}")],
        ]
        await update.message.reply_text(
            "⚠️ Invalid date format. Please enter date as `DD/MM/YYYY` (e.g. `08/09/2026`):",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return ATT_CUSTOM_DATE

    d, mo, y = m.groups()
    target_date = f"{d.zfill(2)}/{mo.zfill(2)}/{y}"
    await fetch_and_display_attendance(update, context, faculty, target_date)
    return ATT_CUSTOM_DATE


# ==========================================
# LEAVE APPLICATION FLOW (WITH LOAD & CREDIT RULES)
# ==========================================
async def apply_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    faculty = get_faculty(user_id)
    if not faculty:
        await update.effective_message.reply_text("Please /register first.")
        return ConversationHandler.END

    context.user_data.clear()
    short_name = faculty.get("short_name") or faculty.get("initials") or ""
    context.user_data["user_profile"] = faculty
    context.user_data["faculty"] = faculty
    context.user_data["emp_name"] = faculty.get("name", "")
    context.user_data["emp_code"] = faculty.get("emp_code", "")
    context.user_data["faculty_initials"] = short_name
    context.user_data["short_name"] = short_name
    kb = [
        [InlineKeyboardButton("Casual Leave (CL)", callback_data="CL"), InlineKeyboardButton("Short Day (0.25 SD)", callback_data="SD")],
        [InlineKeyboardButton("Sick Leave (SL)", callback_data="SL"), InlineKeyboardButton("Earned Leave (EL)", callback_data="EL")],
        [InlineKeyboardButton("Restricted Holiday (RH)", callback_data="RH"), InlineKeyboardButton("Vacation Leave (VL)", callback_data="VL")],
        [InlineKeyboardButton("Exchanged Leave (Ex.L)", callback_data="ExL"), InlineKeyboardButton("Duty Leave (DL)", callback_data="DL")],
        [InlineKeyboardButton("Leave Without Pay (LWP)", callback_data="LWP")],
        [InlineKeyboardButton("❌ Cancel Application", callback_data="CANCEL_APPLY")],
    ]
    prompt = f"📝 **Leave Application for {faculty['name']}**\nSelect Leave Category:"
    if update.callback_query:
        await update.callback_query.answer()
        await safe_edit_text(update.callback_query, prompt, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(prompt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return LEAVE_TYPE


async def proceed_with_leave_type(update: Update, context: ContextTypes.DEFAULT_TYPE, chosen: str):
    query = update.callback_query
    context.user_data["leave_type"] = chosen

    if chosen == "RH":
        context.user_data["day_type"] = "Full Day"
        context.user_data["shift_type"] = ""
        context.user_data["punch_timing"] = "8:15 AM - 3:00 PM"
        return await show_date_picker(query, "RH is Full Day only (1.0 Unit). Select Date:", back_callback="BACK_TO_LEAVE_TYPE")
    elif chosen == "SD":
        kb = [
            [InlineKeyboardButton("🌅 Morning Short • In 9:45", callback_data="Morning Short")],
            [InlineKeyboardButton("🌇 Afternoon Short • Out 1:30", callback_data="Afternoon Short")],
            [InlineKeyboardButton("🔙 Back to Leave Category", callback_data="BACK_TO_LEAVE_TYPE")],
        ]
        await safe_edit_text(query, "Select Short Day Division:", reply_markup=InlineKeyboardMarkup(kb))
        return DAY_TYPE
    elif chosen == "VL":
        context.user_data["day_type"] = "Full Day"
        context.user_data["shift_type"] = ""
        context.user_data["punch_timing"] = "Non-Teaching Phase"
        return await show_date_picker(query, "Vacation Leave (Min 7 days per split). Select Date / Range:", back_callback="BACK_TO_LEAVE_TYPE")
    else:
        kb = [
            [InlineKeyboardButton("Full Day (8:15 AM - 3:00 PM)", callback_data="Full Day")],
            [InlineKeyboardButton("1st Half Day (In: 11:38 AM)", callback_data="1st Half")],
            [InlineKeyboardButton("2nd Half Day (Out: 11:38 AM)", callback_data="2nd Half")],
            [InlineKeyboardButton("🔙 Back to Leave Category", callback_data="BACK_TO_LEAVE_TYPE")],
        ]
        await safe_edit_text(query, f"Selected: **{chosen}**\nSelect Duration:", reply_markup=InlineKeyboardMarkup(kb))
        return DAY_TYPE


async def leave_type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chosen = query.data

    if chosen == "CANCEL_APPLY":
        return await return_to_home_screen(query, context, prefix_msg="❌ **Leave application canceled.**")

    if chosen.startswith("FORCE_APPLY:"):
        actual_lt = chosen.split(":", 1)[1]
        context.user_data["allow_negative_balance"] = True
        return await proceed_with_leave_type(update, context, actual_lt)

    if chosen not in ["CL", "SD", "SL", "EL", "RH", "VL", "LWP", "ExL", "DL"]:
        return await handle_universal_callback(update, context)

    user_id = update.effective_user.id
    faculty = get_faculty(user_id) or {}
    balances = faculty.get("balances", {})

    # Check if leave balance is low/insufficient
    if chosen == "SD":
        try:
            cl_bal = float(balances.get("CL", 0.0))
        except (ValueError, TypeError):
            cl_bal = 0.0
        if cl_bal < 0.25:
            warn_kb = [
                [InlineKeyboardButton("⚠️ Proceed Anyway", callback_data="FORCE_APPLY:SD")],
                [InlineKeyboardButton("🔙 Choose Another Leave Category", callback_data="BACK_TO_LEAVE_TYPE")],
                [InlineKeyboardButton("❌ Cancel Application", callback_data="CANCEL_APPLY")],
            ]
            await safe_edit_text(
                query,
                f"⚠️ **Insufficient Leave Balance Warning!**\n\n"
                f"Your current Casual Leave (CL) balance is **{cl_bal}**.\n"
                f"Short Day (SD) requires at least **0.25 CL**.\n\n"
                f"If you proceed, your CL balance will become **negative ({cl_bal - 0.25:.2f})**.\n\n"
                f"Do you still want to apply for **Short Day (SD)**?",
                reply_markup=InlineKeyboardMarkup(warn_kb)
            )
            return LEAVE_TYPE
    elif chosen != "LWP":
        cat_key = "EXL" if chosen.upper() in ("EXL", "EX.L") else chosen.upper()
        bal_val = 0.0
        for k in (cat_key, "EXL", "Ex.L", "ExL", chosen):
            if k in balances:
                try:
                    bal_val = float(balances[k])
                    break
                except (ValueError, TypeError):
                    pass

        # If balance is not found or 0.0, attempt live fetch from portal for portal users
        if bal_val <= 0.0 and faculty.get("password") and faculty.get("balance_mode") == "portal":
            try:
                portal_user = faculty.get("username", faculty.get("emp_code"))
                p = LeavePortalAPI(username=portal_user, password=faculty.get("password"))
                succ, _ = p.login()
                if succ:
                    fresh_b = p.get_all_balances()
                    if fresh_b:
                        faculty["balances"] = {k: str(v) for k, v in fresh_b.items()}
                        save_faculty(user_id, faculty)
                        balances = faculty["balances"]
                        bal_val = float(balances.get("EXL" if cat_key == "EXL" else chosen, 0.0))
            except Exception:
                pass

        if bal_val <= 0.0:
            display_chosen = "Exchanged Leave (Ex.L)" if chosen.upper() in ("EXL", "EX.L") else chosen
            warn_kb = [
                [InlineKeyboardButton("⚠️ Proceed Anyway", callback_data=f"FORCE_APPLY:{chosen}")],
                [InlineKeyboardButton("🔙 Choose Another Leave Category", callback_data="BACK_TO_LEAVE_TYPE")],
                [InlineKeyboardButton("❌ Cancel Application", callback_data="CANCEL_APPLY")],
            ]
            await safe_edit_text(
                query,
                f"⚠️ **Insufficient Leave Balance Warning!**\n\n"
                f"Your current **{display_chosen}** balance is **{bal_val}**.\n\n"
                f"If you proceed, your leave balance will become **negative**.\n\n"
                f"Do you still want to apply for **{display_chosen}**?",
                reply_markup=InlineKeyboardMarkup(warn_kb)
            )
            return LEAVE_TYPE

    return await proceed_with_leave_type(update, context, chosen)


async def day_type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    dtype = query.data

    if dtype == "BACK_TO_LEAVE_TYPE":
        return await apply_start(update, context)

    if dtype not in ["Full Day", "1st Half", "2nd Half", "Morning Short", "Afternoon Short"]:
        return await handle_universal_callback(update, context)

    context.user_data["day_type"] = dtype

    if "Short" in dtype:
        context.user_data["shift_type"] = dtype
        context.user_data["punch_timing"] = "In by 9:45 AM / Out at 1:30 PM"
    elif "Half" in dtype:
        context.user_data["shift_type"] = dtype
        context.user_data["punch_timing"] = "3h 23m Half Day (Shift cutoff 11:38 AM)"
    else:
        context.user_data["shift_type"] = ""
        context.user_data["punch_timing"] = "8:15 AM - 3:00 PM"

    return await show_date_picker(query, "Select **Date of Leave**:", back_callback="BACK_TO_DAY_TYPE")


async def show_date_picker(query, prompt: str, back_callback: str = "BACK_TO_DAY_TYPE"):
    t0 = get_ist_now()
    t1 = t0 + timedelta(days=1)
    t2 = t0 + timedelta(days=2)

    kb = [
        [InlineKeyboardButton(f"Today ({t0.strftime('%d/%m')})", callback_data=t0.strftime("%d/%m/%Y")),
         InlineKeyboardButton(f"Tomorrow ({t1.strftime('%d/%m')})", callback_data=t1.strftime("%d/%m/%Y"))],
        [InlineKeyboardButton(f"Day After ({t2.strftime('%d/%m')})", callback_data=t2.strftime("%d/%m/%Y")),
         InlineKeyboardButton("📅 Type Date / Range", callback_data="CUSTOM_DATE")],
        [InlineKeyboardButton("🔙 Back", callback_data=back_callback)],
    ]
    await safe_edit_text(query, prompt, reply_markup=InlineKeyboardMarkup(kb))
    return DATE_PICK


async def date_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "BACK_TO_LEAVE_TYPE":
        return await apply_start(update, context)
    if data == "BACK_TO_DAY_TYPE":
        chosen = context.user_data.get("leave_type", "CL")
        if chosen == "RH" or chosen == "VL":
            return await apply_start(update, context)
        elif chosen == "SD":
            kb = [
                [InlineKeyboardButton("🌅 Morning Short • In 9:45", callback_data="Morning Short")],
                [InlineKeyboardButton("🌇 Afternoon Short • Out 1:30", callback_data="Afternoon Short")],
                [InlineKeyboardButton("🔙 Back to Leave Category", callback_data="BACK_TO_LEAVE_TYPE")],
            ]
            await safe_edit_text(query, "Select Short Day Division:", reply_markup=InlineKeyboardMarkup(kb))
            return DAY_TYPE
        else:
            kb = [
                [InlineKeyboardButton("Full Day (8:15 AM - 3:00 PM)", callback_data="Full Day")],
                [InlineKeyboardButton("1st Half Day (In: 11:38 AM)", callback_data="1st Half")],
                [InlineKeyboardButton("2nd Half Day (Out: 11:38 AM)", callback_data="2nd Half")],
                [InlineKeyboardButton("🔙 Back to Leave Category", callback_data="BACK_TO_LEAVE_TYPE")],
            ]
            await safe_edit_text(query, f"Selected: **{chosen}**\nSelect Duration:", reply_markup=InlineKeyboardMarkup(kb))
            return DAY_TYPE

    if data == "CUSTOM_DATE":
        kb = [[InlineKeyboardButton("🔙 Back to Date Picker", callback_data="BACK_TO_DATE_PICK")]]
        await safe_edit_text(
            query,
            "Type date or range (`DD/MM/YYYY` or `DD/MM/YYYY to DD/MM/YYYY`):\n"
            "_Note: Short Day and Half Day can only be 1 single date._",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return CUSTOM_DATE

    if data == "FORCE_APPLY_DURATION":
        context.user_data["allow_negative_balance"] = True
        return await ask_load_choice(query.message)

    if not re.search(r"\d{1,2}/\d{1,2}/\d{4}", data):
        return await handle_universal_callback(update, context)

    res = parse_and_validate_dates(
        context.user_data.get("leave_type", "CL"),
        context.user_data.get("day_type", "Full Day"),
        data
    )
    if not res["valid"]:
        await query.message.reply_text(f"⚠️ {res['error']}\nPlease choose a valid date:")
        return DATE_PICK

    lt = context.user_data.get("leave_type", "CL")
    units = float(res.get("units", 1.0))
    if lt != "LWP" and not context.user_data.get("allow_negative_balance"):
        user_id = update.effective_user.id
        faculty = get_faculty(user_id) or {}
        balances = faculty.get("balances", {})
        cat_key = "CL" if lt.upper() in ("SD", "SHORTDAY") else ("EXL" if lt.upper() in ("EXL", "EX.L") else lt.upper())
        keys_to_search = ("CL",) if lt.upper() in ("SD", "SHORTDAY") else (cat_key, "EXL", "Ex.L", "ExL", lt)
        bal_val = 0.0
        for k in keys_to_search:
            if k in balances:
                try:
                    bal_val = float(balances[k])
                    break
                except (ValueError, TypeError):
                    pass
        if units > bal_val:
            context.user_data.update(res)
            new_bal = bal_val - units
            warn_kb = [
                [InlineKeyboardButton("⚠️ Proceed Anyway", callback_data="FORCE_APPLY_DURATION")],
                [InlineKeyboardButton("📅 Choose Another Date", callback_data="BACK_TO_DATE_PICK")],
                [InlineKeyboardButton("❌ Cancel Application", callback_data="CANCEL_APPLY")],
            ]
            await query.message.reply_text(
                f"⚠️ **Insufficient Leave Balance Warning!**\n\n"
                f"• **Leave Type:** {lt}\n"
                f"• **Available Balance:** {bal_val}\n"
                f"• **Requested Duration:** {units} day(s)\n"
                f"• **Balance after Application:** **{new_bal:.2f}** (Negative)\n\n"
                f"If you proceed, your leave balance will become **negative ({new_bal:.2f})**.\n\n"
                f"Do you still want to apply for this leave?",
                reply_markup=InlineKeyboardMarkup(warn_kb),
                parse_mode="Markdown"
            )
            return DATE_PICK

    context.user_data.update(res)
    return await ask_load_choice(query.message)


async def custom_date_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == "BACK_TO_DATE_PICK":
            return await show_date_picker(query, "Select **Date of Leave**:", back_callback="BACK_TO_DAY_TYPE")
        if query.data == "FORCE_APPLY_DURATION":
            context.user_data["allow_negative_balance"] = True
            return await ask_load_choice(query.message)
        return await handle_universal_callback(update, context)

    if not update.message or not update.message.text:
        return CUSTOM_DATE

    text = update.message.text.strip()
    res = parse_and_validate_dates(
        context.user_data.get("leave_type", "CL"),
        context.user_data.get("day_type", "Full Day"),
        text
    )
    if not res["valid"]:
        kb = [[InlineKeyboardButton("🔙 Back to Date Picker", callback_data="BACK_TO_DATE_PICK")]]
        await update.message.reply_text(f"⚠️ {res['error']}\nPlease enter the date again:", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return CUSTOM_DATE

    lt = context.user_data.get("leave_type", "CL")
    units = float(res.get("units", 1.0))
    if lt != "LWP" and not context.user_data.get("allow_negative_balance"):
        user_id = update.effective_user.id
        faculty = get_faculty(user_id) or {}
        balances = faculty.get("balances", {})
        cat_key = "CL" if lt.upper() in ("SD", "SHORTDAY") else ("EXL" if lt.upper() in ("EXL", "EX.L") else lt.upper())
        keys_to_search = ("CL",) if lt.upper() in ("SD", "SHORTDAY") else (cat_key, "EXL", "Ex.L", "ExL", lt)
        bal_val = 0.0
        for k in keys_to_search:
            if k in balances:
                try:
                    bal_val = float(balances[k])
                    break
                except (ValueError, TypeError):
                    pass
        if units > bal_val:
            context.user_data.update(res)
            new_bal = bal_val - units
            warn_kb = [
                [InlineKeyboardButton("⚠️ Proceed Anyway", callback_data="FORCE_APPLY_DURATION")],
                [InlineKeyboardButton("🔙 Back to Date Picker", callback_data="BACK_TO_DATE_PICK")],
                [InlineKeyboardButton("❌ Cancel Application", callback_data="CANCEL_APPLY")],
            ]
            await update.message.reply_text(
                f"⚠️ **Insufficient Leave Balance Warning!**\n\n"
                f"• **Leave Type:** {lt}\n"
                f"• **Available Balance:** {bal_val}\n"
                f"• **Requested Duration:** {units} day(s)\n"
                f"• **Balance after Application:** **{new_bal:.2f}** (Negative)\n\n"
                f"If you proceed, your leave balance will become **negative ({new_bal:.2f})**.\n\n"
                f"Do you still want to apply for this leave?",
                reply_markup=InlineKeyboardMarkup(warn_kb),
                parse_mode="Markdown"
            )
            return CUSTOM_DATE

    context.user_data.update(res)
    return await ask_load_choice(update.message)


async def ask_load_choice(message_obj):
    kb = [
        [InlineKeyboardButton("1️⃣ No Load", callback_data="LOAD:NO_LOAD")],
        [InlineKeyboardButton("2️⃣ Load Taken By Self", callback_data="LOAD:SELF")],
        [InlineKeyboardButton("3️⃣ 🤖 Auto-Adjust Timetable", callback_data="LOAD:AUTO_ADJUST")],
        [InlineKeyboardButton("4️⃣ ✍️ Manual Load Adjustment", callback_data="LOAD:ADJUSTED")],
        [InlineKeyboardButton("5️⃣ Load Not Adjusted", callback_data="LOAD:NOT_ADJUSTED")],
        [InlineKeyboardButton("🔙 Back to Date Selection", callback_data="BACK_TO_DATE")],
    ]
    prompt = (
        "⚙️ **Select Teaching Load Arrangement:**\n"
        "How will your lectures/labs be handled during your leave?"
    )
    await message_obj.reply_text(prompt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return LOAD_CHOICE


def _leave_units(user_data: dict) -> float:
    """Return the leave quantity used to scale credit deductions."""
    try:
        units = float((user_data or {}).get("units", 1.0))
    except (TypeError, ValueError):
        units = 1.0
    return units if units > 0 else 1.0


def _format_credit(value: float) -> str:
    """Format credit values without noisy trailing zeroes."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if abs(number) < 1e-9:
        return "0"
    return f"{number:.4f}".rstrip("0").rstrip(".")


def _scaled_credit(base_credit: float, user_data: dict) -> float:
    return round(float(base_credit) * _leave_units(user_data), 4)


async def load_choice_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    u = context.user_data

    if data == "BACK_TO_DATE":
        return await show_date_picker(query, "Select **Date of Leave**:", back_callback="BACK_TO_DAY_TYPE")

    if data == "LOAD:NO_LOAD" or data == "LOAD:SELF":
        u["load_status"] = "No Load" if data == "LOAD:NO_LOAD" else "Load Taken By Self"
        u["load_adjustments"] = []
        u["has_load"] = False
        return await ask_submit_timing_for_self_load(query, context)

    elif data == "LOAD:AUTO_ADJUST":
        return await start_auto_load_adjust(query, context)

    elif data == "LOAD:ADJUSTED":
        u["load_status"] = "Load Adjusted"
        u["has_load"] = True
        return await ask_duty_count(query)

    elif data == "LOAD:NOT_ADJUSTED":
        u["load_status"] = "Load Not Adjusted"
        u["load_adjustments"] = []
        u["has_load"] = False
        return await ask_load_not_adj_status(query, context)
    else:
        return await handle_universal_callback(update, context)


def _auto_leave_dates(user_data: dict) -> list[date]:
    """Return every calendar date covered by the current leave request."""
    first = user_data.get("d1")
    last = user_data.get("d2") or first
    if not isinstance(first, date):
        try:
            first = datetime.strptime(str(user_data.get("from_date", "")), "%d/%m/%Y").date()
        except Exception:
            return []
    if not isinstance(last, date):
        try:
            last = datetime.strptime(str(user_data.get("to_date", "")), "%d/%m/%Y").date()
        except Exception:
            last = first
    if last < first:
        return []
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def _auto_is_multiday(user_data: dict) -> bool:
    dates = _auto_leave_dates(user_data)
    return len(dates) > 1


def _flatten_auto_day_results(user_data: dict) -> list:
    """Flatten selected per-day arrangements in chronological order."""
    ordered = []
    for day_value in user_data.get("auto_day_dates", []):
        date_key = day_value.strftime("%d/%m/%Y") if isinstance(day_value, date) else str(day_value)
        for adjustment in user_data.get("auto_day_results", {}).get(date_key, []):
            item = dict(adjustment)
            item.setdefault("date", date_key)
            ordered.append(item)
    return ordered


async def _finish_or_advance_auto_day(query, context: ContextTypes.DEFAULT_TYPE):
    """Save the current day's choice and either show the next day or finish."""
    u = context.user_data
    message_obj = getattr(query, "message", None) or query
    if not _auto_is_multiday(u):
        return await ask_submit_timing_for_adjusted_load(message_obj, context)

    u["auto_day_index"] = u.get("auto_day_index", 0) + 1
    if u["auto_day_index"] < len(u.get("auto_day_dates", [])):
        return await start_auto_load_day(query, context)

    u["load_adjustments"] = _flatten_auto_day_results(u)
    u["load_status"] = "Load Adjusted" if u["load_adjustments"] else "No Load"
    u["has_load"] = bool(u["load_adjustments"])
    if u["load_adjustments"]:
        return await ask_submit_timing_for_adjusted_load(message_obj, context)
    return await ask_submit_timing_for_self_load(query, context)


async def start_auto_load_day(query, context: ContextTypes.DEFAULT_TYPE):
    """Calculate and display default auto-adjustment plans for one leave day."""
    u = context.user_data
    from timetable_engine import get_timetable_engine
    engine = get_timetable_engine()
    dates = u.get("auto_day_dates", [])
    index = u.get("auto_day_index", 0)
    if index >= len(dates):
        return await _finish_or_advance_auto_day(query, context)

    day_value = dates[index]
    day_str = day_value.strftime("%d/%m/%Y") if isinstance(day_value, date) else str(day_value)
    u["auto_current_date"] = day_str
    duties = engine.get_faculty_duties_for_date(u.get("faculty_initials", "FACULTY"), day_value)
    u["auto_load_duties"] = duties
    u["custom_slot_idx"] = 0
    u["custom_slot_assignments"] = []
    # Exclusions are specific to the currently displayed timetable day.  A
    # faculty unavailable in Monday's lecture 3 must not silently carry over
    # to Tuesday's lecture 3.
    u["auto_excluded_faculty_lectures"] = {}

    progress = f" (Day {index + 1} of {len(dates)})" if len(dates) > 1 else ""
    if not duties:
        u.setdefault("auto_day_results", {})[day_str] = []
        kb = [[InlineKeyboardButton("➡️ Continue to Next Day", callback_data="AUTO_DAY_NEXT")]]
        if index + 1 >= len(dates):
            kb = [[InlineKeyboardButton("➡️ Continue", callback_data="AUTO_DAY_NEXT")]]
        await safe_edit_text(
            query,
            f"ℹ️ **No Scheduled Teaching Load for {day_str}{progress}:**\n\n"
            "No timetable lectures or labs were found for this leave day.\n"
            "Continue to review the next leave day.",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return LOAD_AUTO_MAX_DIV

    u["auto_day_progress"] = progress
    return await run_auto_load_adjustments(query, context)


def _auto_constraint_summary(user_data: dict) -> str:
    """Describe the exact constraints used for the current automatic search."""
    max_value = user_data.get("auto_max_subject_lectures", 2)
    max_disp = "No Limit" if max_value == 999 else f"{max_value} lectures of any one subject per division"
    merged = "Allowed" if user_data.get("auto_allow_merged", False) else "Not allowed (free/cascade only)"
    department = "Other departments allowed" if user_data.get("auto_disturb_other_department", False) else "Same department only"
    return f"Max {max_disp} · Merged: {merged} · Department: {department} · Cascades: ON"


async def run_auto_load_adjustments(query, context: ContextTypes.DEFAULT_TYPE):
    """Run the complete search using the current constraints and exclusions."""
    u = context.user_data
    from timetable_engine import get_timetable_engine
    engine = get_timetable_engine()
    duties = u.get("auto_load_duties", [])
    max_limit = u.get("auto_max_subject_lectures", u.get("auto_max_lectures_per_div", 2))
    result = engine.suggest_load_adjustments(
        duties=duties,
        max_lectures_per_div=max_limit,
        max_lectures_per_subject=max_limit,
        allow_merged=u.get("auto_allow_merged", False),
        include_cascades=True,
        prefer_min_disturbance=u.get("auto_min_disturb", False),
        disturb_other_department=u.get("auto_disturb_other_department", False),
        include_existing_subject_load=u.get("auto_include_existing_subject_load", True),
        excluded_faculty_lectures=u.get("auto_excluded_faculty_lectures", {}),
    )
    u["auto_load_result"] = result
    return await display_auto_load_options(query, context)


def format_auto_load_config_screen(
    duties: list,
    fac_initial: str,
    from_date: str,
    max_div: int = 2,
    allow_merged: bool = False,
    min_disturb: bool = False,
    disturb_other_department: bool = False,
    max_subject: int = None,
    day_progress: str = "",
):
    """Render the optional constraint editor used after the default search."""
    duties_preview = "\n".join([
        f"• **Lec {d['lec_no']}** ({d.get('time', '')}): Div `{d.get('division', '')}` | {d.get('subject', '')} (Room {d.get('room', '')})"
        for d in duties
    ])
    max_value = max_subject if max_subject is not None else max_div
    max_disp = "No Limit" if max_value == 999 else f"{max_value} lectures of any one subject per division"
    merged_disp = "✅ Yes (Merged Allowed)" if allow_merged else "❌ No (Free Only)"
    dept_disp = "✅ Yes (Other Dept Allowed)" if disturb_other_department else "❌ No (Same Dept Only)"

    prompt = (
        f"🤖 **Auto Load Adjustment ({fac_initial}):**\n\n"
        f"📅 Date: `{from_date}`{day_progress}\n"
        f"📚 Found **{len(duties)}** scheduled duties:\n"
        f"{duties_preview}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ **Change Constraints (optional):**\n\n"
        f"• **Max lectures of any one subject in a division:** `{max_disp}`\n"
        f"• **Merged Classes:** `{merged_disp}`\n"
        f"• **Other Department Disturbance:** `{dept_disp}`\n"
        f"• **Cascades:** `ON` (all valid chains are searched)\n\n"
        f"The automatic search already used the defaults: **Max 2**, merged classes not allowed, same department only.\n"
        f"You may change the subject limit only to **Max 2**, **Max 3**, or **No Limit**, then run the search again."
    )
    btn_m2 = f"{'✅ ' if max_value == 2 else ''}2️⃣ Max 2"
    btn_m3 = f"{'✅ ' if max_value == 3 else ''}3️⃣ Max 3"
    btn_m9 = f"{'✅ ' if max_value == 999 else ''}♾️ No Limit"

    btn_mno = f"{'✅ ' if not allow_merged else ''}❌ Free Only"
    btn_myes = f"{'✅ ' if allow_merged else ''}🔄 Merged Allowed"

    kb = [
        [
            InlineKeyboardButton(btn_m2, callback_data="SET_MAX:2"),
            InlineKeyboardButton(btn_m3, callback_data="SET_MAX:3"),
            InlineKeyboardButton(btn_m9, callback_data="SET_MAX:999"),
        ],
        [
            InlineKeyboardButton(btn_mno, callback_data="SET_MERGED:NO"),
            InlineKeyboardButton(btn_myes, callback_data="SET_MERGED:YES"),
        ],
        [
            InlineKeyboardButton(
                f"{'✅ ' if not disturb_other_department else ''}❌ Same Dept Only",
                callback_data="SET_DEPT:NO",
            ),
            InlineKeyboardButton(
                f"{'✅ ' if disturb_other_department else ''}🌐 Other Dept Allowed",
                callback_data="SET_DEPT:YES",
            ),
        ],
        [
            InlineKeyboardButton("🚀 Find Load Adjustments", callback_data="PRESET_LOAD:RUN_CUSTOM"),
        ],
        [
            InlineKeyboardButton("✍️ Manual Load Entry", callback_data="LOAD:ADJUSTED"),
            InlineKeyboardButton("🔙 Back to Load Options", callback_data="BACK_TO_LOAD"),
        ]
    ]
    return prompt, InlineKeyboardMarkup(kb)


async def start_auto_load_adjust(query, context: ContextTypes.DEFAULT_TYPE):
    u = context.user_data
    from timetable_engine import get_timetable_engine
    engine = get_timetable_engine()

    # 1. Resolve faculty initials (strictly user-isolated)
    user_id = query.from_user.id if (query and hasattr(query, "from_user")) else None
    fac_rec = (get_faculty(user_id) if user_id else None) or u.get("faculty") or u.get("user_profile") or {}

    fac_initial = (
        u.get("short_name")
        or u.get("faculty_initials")
        or fac_rec.get("short_name")
        or fac_rec.get("initials")
    )

    emp_code = u.get("emp_code") or fac_rec.get("emp_code") or ""
    if not fac_initial:
        name = u.get("emp_name") or fac_rec.get("name") or ""
        fac_initial = engine.resolve_faculty_initials(name, emp_code=emp_code)

    if not fac_initial and emp_code:
        fac_store_item = get_faculty_by_emp_code(emp_code) or {}
        fac_initial = fac_store_item.get("short_name") or fac_store_item.get("initials") or engine.resolve_faculty_initials(fac_store_item.get("name", ""))

    if not fac_initial:
        name = u.get("emp_name") or fac_rec.get("name") or ""
        fac_initial = get_faculty_shortname(name) if name else ""

    if not fac_initial:
        fac_initial = fac_rec.get("emp_code") or "FACULTY"

    u["faculty_initials"] = fac_initial
    u["short_name"] = fac_initial

    # Keep the selected constraints across all days in a range.  Each day
    # gets its own timetable search and plan-selection screen.
    u.setdefault("auto_max_subject_lectures", 2)
    u.setdefault("auto_max_lectures_per_div", u.get("auto_max_subject_lectures", 2))
    u.setdefault("auto_allow_merged", False)
    u.setdefault("auto_disturb_other_department", False)
    u.setdefault("auto_min_disturb", False)
    u.setdefault("auto_include_existing_subject_load", True)

    if _auto_is_multiday(u):
        if not u.get("auto_day_dates"):
            u["auto_day_dates"] = _auto_leave_dates(u)
            u["auto_day_index"] = 0
            u["auto_day_results"] = {}
        return await start_auto_load_day(query, context)

    # 2. Get duties on leave date
    from_date = u.get("from_date") or get_ist_today_str("%d/%m/%Y")
    d1_obj = u.get("d1")
    duties = engine.get_faculty_duties_for_date(fac_initial, d1_obj or from_date)
    u["auto_load_duties"] = duties
    u["auto_current_date"] = from_date
    u["auto_excluded_faculty_lectures"] = {}

    if not duties:
        u["load_status"] = "No Load"
        u["load_adjustments"] = []
        u["has_load"] = False
        kb = [
            [InlineKeyboardButton("➡️ Continue (No Load)", callback_data="AUTO_NO_LOAD_CONTINUE")],
            [InlineKeyboardButton("✍️ Manual Load Adjustment", callback_data="LOAD:ADJUSTED")],
            [InlineKeyboardButton("🔙 Back to Load Options", callback_data="BACK_TO_LOAD")],
        ]
        msg = (
            f"ℹ️ **No Scheduled Teaching Load Found:**\n\n"
            f"No timetable lectures or labs were found for **{fac_initial}** on `{from_date}`.\n\n"
            f"Teaching load status will be set to **No Load**."
        )
        await safe_edit_text(query, msg, reply_markup=InlineKeyboardMarkup(kb))
        return LOAD_AUTO_MAX_DIV

    # 3. Scheduled duties found: apply the defaults immediately.  The
    # constraint editor remains available from the result screen.
    return await run_auto_load_adjustments(query, context)


async def load_auto_preset_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    u = context.user_data
    duties = u.get("auto_load_duties", [])
    fac_initial = u.get("faculty_initials", "FACULTY")
    from_date = u.get("from_date") or get_ist_today_str("%d/%m/%Y")

    # In-screen toggle: Max lectures in division
    if data.startswith("SET_MAX:") or data.startswith("MAX_DIV:"):
        max_limit = int(data.split(":")[1])
        u["auto_max_subject_lectures"] = max_limit
        u["auto_max_lectures_per_div"] = max_limit
        prompt, markup = format_auto_load_config_screen(
            duties=duties,
            fac_initial=fac_initial,
            from_date=from_date,
            max_div=max_limit,
            allow_merged=u.get("auto_allow_merged", False),
            min_disturb=u.get("auto_min_disturb", False),
            disturb_other_department=u.get("auto_disturb_other_department", False),
        )
        await safe_edit_text(query, prompt, reply_markup=markup)
        return LOAD_AUTO_MAX_DIV

    # In-screen toggle: Merged allowed or not
    if data.startswith("SET_MERGED:") or data.startswith("MERGED_OPT:"):
        allow_merged = (data in ["SET_MERGED:YES", "MERGED_OPT:YES"])
        u["auto_allow_merged"] = allow_merged
        prompt, markup = format_auto_load_config_screen(
            duties=duties,
            fac_initial=fac_initial,
            from_date=from_date,
            max_div=u.get("auto_max_subject_lectures", u.get("auto_max_lectures_per_div", 2)),
            allow_merged=allow_merged,
            min_disturb=u.get("auto_min_disturb", False),
            disturb_other_department=u.get("auto_disturb_other_department", False),
        )
        await safe_edit_text(query, prompt, reply_markup=markup)
        return LOAD_AUTO_MAX_DIV

    if data.startswith("SET_DEPT:"):
        u["auto_disturb_other_department"] = data == "SET_DEPT:YES"
        prompt, markup = format_auto_load_config_screen(
            duties=duties,
            fac_initial=fac_initial,
            from_date=from_date,
            max_div=u.get("auto_max_subject_lectures", u.get("auto_max_lectures_per_div", 2)),
            allow_merged=u.get("auto_allow_merged", False),
            min_disturb=u.get("auto_min_disturb", False),
            disturb_other_department=u["auto_disturb_other_department"],
        )
        await safe_edit_text(query, prompt, reply_markup=markup)
        return LOAD_AUTO_MAX_DIV

    # 1-Click Minimum Disturbance Execution
    if data in ["PRESET_LOAD:MIN_DISTURB", "RUN_AUTO:MIN_DISTURB"]:
        u["auto_include_existing_subject_load"] = True
        u["auto_min_disturb"] = True
        include_cascades = True
        prefer_min_disturbance = True
        max_limit = u.get("auto_max_subject_lectures", 2)
        allow_merged = u.get("auto_allow_merged", False)
    elif data == "PRESET_LOAD:FREE_MAX1":
        u["auto_include_existing_subject_load"] = False
        u["auto_max_subject_lectures"] = 1
        u["auto_max_lectures_per_div"] = 1
        u["auto_allow_merged"] = False
        u["auto_min_disturb"] = False
        include_cascades = False
        prefer_min_disturbance = False
        max_limit = 1
        allow_merged = False
    elif data in ["PRESET_LOAD:CASCADE_MAX1", "RUN_AUTO:OPTIMAL"]:
        u["auto_include_existing_subject_load"] = False
        u["auto_max_subject_lectures"] = 1
        u["auto_max_lectures_per_div"] = 1
        u["auto_allow_merged"] = False
        u["auto_min_disturb"] = False
        include_cascades = True
        prefer_min_disturbance = False
        max_limit = 1
        allow_merged = False
    elif data == "PRESET_LOAD:MERGED":
        u["auto_include_existing_subject_load"] = False
        u["auto_max_subject_lectures"] = 1
        u["auto_max_lectures_per_div"] = 1
        u["auto_allow_merged"] = True
        u["auto_min_disturb"] = False
        include_cascades = True
        prefer_min_disturbance = False
        max_limit = 1
        allow_merged = True
    else: # PRESET_LOAD:RUN_CUSTOM or other
        u["auto_include_existing_subject_load"] = True
        max_limit = u.get("auto_max_subject_lectures", u.get("auto_max_lectures_per_div", 2))
        allow_merged = u.get("auto_allow_merged", False)
        include_cascades = True
        prefer_min_disturbance = u.get("auto_min_disturb", False)

    # All current searches use the same complete subject-limit validation,
    # including every proxy and cascade step.  Legacy preset callbacks are
    # retained for old messages, but the visible UI no longer exposes them.
    return await run_auto_load_adjustments(query, context)


async def show_auto_constraint_editor(query, context: ContextTypes.DEFAULT_TYPE):
    """Open the optional constraint editor without restarting the leave flow."""
    u = context.user_data
    progress = u.get("auto_day_progress", "")
    prompt, markup = format_auto_load_config_screen(
        duties=u.get("auto_load_duties", []),
        fac_initial=u.get("faculty_initials", "FACULTY"),
        from_date=u.get("auto_current_date") or u.get("from_date") or get_ist_today_str("%d/%m/%Y"),
        max_div=u.get("auto_max_subject_lectures", 2),
        max_subject=u.get("auto_max_subject_lectures", 2),
        allow_merged=u.get("auto_allow_merged", False),
        min_disturb=u.get("auto_min_disturb", False),
        disturb_other_department=u.get("auto_disturb_other_department", False),
        day_progress=progress,
    )
    await safe_edit_text(query, prompt, reply_markup=markup)
    return LOAD_AUTO_MAX_DIV


def _auto_proxy_faculties(user_data: dict) -> list:
    """Collect proxy/cascade faculty initials currently visible to the user."""
    result = user_data.get("auto_load_result", {}) or {}
    found = {}

    def add_faculty(initials, name=""):
        initials = str(initials or "").upper().replace(" ", "")
        if not initials:
            return
        found.setdefault(initials, name or initials)

    def add_candidate(candidate):
        if not isinstance(candidate, dict):
            return
        add_faculty(candidate.get("initials"), candidate.get("name", ""))
        for step in candidate.get("chain", []) or []:
            add_faculty(step.get("reliever"), step.get("reliever_name", ""))

    for plan in result.get("plans", []) or []:
        for adjustment in plan.get("adjustments", []) or []:
            add_candidate(adjustment.get("substitute"))
            for step in adjustment.get("chain", []) or []:
                add_faculty(step.get("reliever"), step.get("reliever_name", ""))
    if not found:
        for slot in result.get("per_slot_candidates", []) or []:
            for candidate in slot.get("candidates", []) or []:
                add_candidate(candidate)

    excluded = user_data.get("auto_excluded_faculty_lectures", {}) or {}
    for initials in excluded:
        add_faculty(initials)
    return sorted(found.items(), key=lambda item: (item[0], item[1]))


def _auto_exclusion_display(user_data: dict) -> str:
    excluded = user_data.get("auto_excluded_faculty_lectures", {}) or {}
    parts = []
    for faculty, lectures in sorted(excluded.items()):
        values = set(lectures if isinstance(lectures, (list, tuple, set)) else [lectures])
        if "full_day" in values:
            text = "Full Day"
        else:
            text = ", ".join(str(v) for v in sorted(values, key=str))
        parts.append(f"{faculty} ({text})")
    return ", ".join(parts) if parts else "None"


async def prompt_auto_excluded_faculty(query, context: ContextTypes.DEFAULT_TYPE):
    """Ask which visible proxy faculty should be excluded from recalculation."""
    u = context.user_data
    faculties = _auto_proxy_faculties(u)
    lines = [
        "🚫 **Recalculate Without Proxy Faculty**",
        "",
        "Select a proxy/cascade faculty who is unavailable or denied. You can exclude multiple faculties, each for different lecture numbers.",
        f"**Current exclusions:** {_auto_exclusion_display(u)}",
        "",
        "The selected constraints will stay unchanged.",
    ]
    kb = []
    row = []
    for initials, name in faculties:
        label = f"🚫 {initials}"
        row.append(InlineKeyboardButton(label[:32], callback_data=f"AUTO_EXCL_FAC:{initials}"))
        if len(row) == 3:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    if not faculties:
        lines.append("\nNo proxy faculty was listed in the current result. Change constraints or use manual entry if the timetable has no valid candidate.")
    kb.extend([
        [InlineKeyboardButton("🔍 Recalculate Now", callback_data="AUTO_RECALCULATE_EXCLUDED")],
        [InlineKeyboardButton("⚙️ Change Constraints", callback_data="AUTO_CHANGE_CONSTRAINTS")],
        [InlineKeyboardButton("🔙 Back to Adjustments", callback_data="AUTO_EXCL_BACK")],
    ])
    await safe_edit_text(query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))
    return LOAD_AUTO_OPTIONS


async def prompt_auto_exclusion_lectures(query, context: ContextTypes.DEFAULT_TYPE):
    """Ask which lecture numbers are unavailable for the selected proxy."""
    u = context.user_data
    faculty = u.get("auto_exclusion_pending_faculty", "")
    excluded = u.setdefault("auto_excluded_faculty_lectures", {})
    selected = set(excluded.get(faculty, []))
    lines = [
        f"🚫 **Exclude `{faculty}` for which lectures?**",
        "",
        "Choose one or more lecture numbers. Use Full Day when the faculty cannot take any load today.",
        f"**Selected for {faculty}:** {_auto_exclusion_display(u)}",
    ]
    kb = []
    row = []
    for lec_no in range(1, 6):
        label = f"{'✅ ' if lec_no in selected else ''}Lec {lec_no}"
        row.append(InlineKeyboardButton(label, callback_data=f"AUTO_EXCL_LEC:{lec_no}"))
    kb.append(row)
    kb.append([InlineKeyboardButton(f"{'✅ ' if 'full_day' in selected else ''}🌞 Full Day", callback_data="AUTO_EXCL_LEC:FULL")])
    kb.extend([
        [InlineKeyboardButton("✅ Done With Faculty", callback_data="AUTO_EXCL_DONE")],
        [InlineKeyboardButton("🔍 Recalculate Now", callback_data="AUTO_RECALCULATE_EXCLUDED")],
        [InlineKeyboardButton("🔙 Back to Faculty List", callback_data="AUTO_EXCL_BACK")],
    ])
    await safe_edit_text(query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))
    return LOAD_AUTO_OPTIONS


async def load_auto_max_div_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "BACK_TO_LOAD":
        return await ask_load_choice(query.message)
    if data == "AUTO_NO_LOAD_CONTINUE":
        return await ask_submit_timing_for_self_load(query, context)
    if data == "LOAD:ADJUSTED":
        return await ask_duty_count(query)
    if data == "LOAD:AUTO_ADJUST":
        return await start_auto_load_adjust(query, context)
    if data == "AUTO_DAY_NEXT":
        u = context.user_data
        if u.get("auto_day_index", 0) + 1 < len(u.get("auto_day_dates", [])):
            u["auto_day_index"] = u.get("auto_day_index", 0) + 1
            return await start_auto_load_day(query, context)
        u["auto_day_index"] = len(u.get("auto_day_dates", []))
        u["load_adjustments"] = _flatten_auto_day_results(u)
        u["load_status"] = "Load Adjusted" if u["load_adjustments"] else "No Load"
        u["has_load"] = bool(u["load_adjustments"])
        if u["load_adjustments"]:
            return await ask_submit_timing_for_adjusted_load(query.message, context)
        return await ask_submit_timing_for_self_load(query, context)

    # Any setting toggle or execution
    if (data.startswith("SET_MAX:") or data.startswith("SET_MERGED:") or data.startswith("SET_DEPT:") or
            data.startswith("PRESET_LOAD:") or data.startswith("RUN_AUTO:") or
            data.startswith("MAX_DIV:") or data.startswith("MERGED_OPT:")):
        return await load_auto_preset_picked(update, context)

    return await handle_universal_callback(update, context)


async def load_auto_merged_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "LOAD:AUTO_ADJUST":
        return await start_auto_load_adjust(query, context)

    return await load_auto_preset_picked(update, context)


async def display_auto_load_options(query, context: ContextTypes.DEFAULT_TYPE):
    u = context.user_data
    result = u.get("auto_load_result", {})
    plans = result.get("plans", [])

    if not plans:
        kb = [
            [InlineKeyboardButton("🚫 Proxy Unavailable / Recalculate", callback_data="AUTO_EXCLUDE_PROXY")],
            [InlineKeyboardButton("🔄 Allow Merged & Retry", callback_data="MERGED_OPT:YES")],
            [InlineKeyboardButton("🔧 Customize Slot-by-Slot", callback_data="AUTO_CUSTOMIZE_SLOTS")],
            [InlineKeyboardButton("✍️ Enter Load Manually", callback_data="LOAD:ADJUSTED")],
            [InlineKeyboardButton("🔙 Change Constraints", callback_data="AUTO_CHANGE_CONSTRAINTS")],
        ]
        msg = (
            "⚠️ **No Complete Single Arrangement Found:**\n\n"
            f"**Constraints applied:** {_auto_constraint_summary(u)}\n\n"
            "Some slots do not have a valid proxy or cascade under these constraints. "
            "Try Max 3 or No Limit, allow merged/other-department faculty, exclude an unavailable proxy and recalculate, or use manual entry."
        )
        await safe_edit_text(query, msg, reply_markup=InlineKeyboardMarkup(kb))
        return LOAD_AUTO_OPTIONS

    current_date = u.get("auto_current_date") or u.get("from_date", "")
    day_dates = u.get("auto_day_dates", [])
    day_index = u.get("auto_day_index", 0)
    day_progress = ""
    if len(day_dates) > 1:
        day_progress = f" (Day {day_index + 1} of {len(day_dates)})"
    lines = [
        "🎯 **Suggested Teaching Load Adjustments:**",
        f"📅 **Leave Day:** `{current_date}`{day_progress}\n",
        f"⚙️ **Constraints applied:** {_auto_constraint_summary(u)}",
        f"🚫 **Excluded proxies:** {_auto_exclusion_display(u)}\n",
    ]
    kb = []
    for idx, plan in enumerate(plans, 1):
        plan_title = plan["title"]
        lines.append(f"📋 **{plan_title}:**")
        for adj in plan["adjustments"]:
            sub = adj["substitute"]
            sub_init = sub["initials"]
            subj = sub["subject"]
            status_icon = "🟢" if sub.get("is_free") else ("🔗" if sub.get("is_cascade") else "🔄")
            line = f"  • Lec {adj['duty']['lec_no']} ({adj['slot']}) [{adj['class_div']}]: **{sub_init}** ({subj}) {status_icon}"
            if sub.get("cascade_detail"):
                line += f"\n    ↳ *Cascade:* `{sub['cascade_detail']}`"
            lines.append(line)
        lines.append("")
        btn_text = f"✅ Select Option {idx} (Recommended)" if idx == 1 else f"📋 Select Option {idx}"
        kb.append([InlineKeyboardButton(btn_text, callback_data=f"SELECT_PLAN:{idx-1}")])

    kb.append([InlineKeyboardButton("🚫 Proxy Unavailable / Recalculate", callback_data="AUTO_EXCLUDE_PROXY")])
    kb.append([InlineKeyboardButton("🔧 Customize per Slot", callback_data="AUTO_CUSTOMIZE_SLOTS")])
    kb.append([InlineKeyboardButton("✍️ Manual Load Entry", callback_data="LOAD:ADJUSTED")])
    kb.append([InlineKeyboardButton("⚙️ Change Constraints", callback_data="AUTO_CHANGE_CONSTRAINTS")])

    await safe_edit_text(query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))
    return LOAD_AUTO_OPTIONS


async def load_auto_options_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    u = context.user_data

    if data == "AUTO_EXCLUDE_PROXY":
        return await prompt_auto_excluded_faculty(query, context)
    if data.startswith("AUTO_EXCL_FAC:"):
        u["auto_exclusion_pending_faculty"] = data.split(":", 1)[1].upper().replace(" ", "")
        return await prompt_auto_exclusion_lectures(query, context)
    if data.startswith("AUTO_EXCL_LEC:"):
        faculty = u.get("auto_exclusion_pending_faculty", "")
        if faculty:
            exclusions = u.setdefault("auto_excluded_faculty_lectures", {})
            selected = set(exclusions.get(faculty, []))
            lecture = data.split(":", 1)[1]
            if lecture == "FULL":
                selected = {"full_day"}
            else:
                selected.discard("full_day")
                lecture_no = int(lecture)
                if lecture_no in selected:
                    selected.remove(lecture_no)
                else:
                    selected.add(lecture_no)
            if selected:
                exclusions[faculty] = sorted(selected, key=lambda value: (value == "full_day", str(value)))
            else:
                exclusions.pop(faculty, None)
        return await prompt_auto_exclusion_lectures(query, context)
    if data == "AUTO_EXCL_DONE":
        u.pop("auto_exclusion_pending_faculty", None)
        return await prompt_auto_excluded_faculty(query, context)
    if data == "AUTO_RECALCULATE_EXCLUDED":
        u.pop("auto_exclusion_pending_faculty", None)
        return await run_auto_load_adjustments(query, context)
    if data == "AUTO_EXCL_BACK":
        u.pop("auto_exclusion_pending_faculty", None)
        return await display_auto_load_options(query, context)
    if data == "AUTO_CHANGE_CONSTRAINTS":
        return await show_auto_constraint_editor(query, context)
    if data == "LOAD:AUTO_ADJUST":
        return await start_auto_load_adjust(query, context)
    if data == "LOAD:ADJUSTED":
        return await ask_duty_count(query)
    if data == "MERGED_OPT:YES":
        u["auto_allow_merged"] = True
        return await run_auto_load_adjustments(query, context)

    if data == "AUTO_CUSTOMIZE_SLOTS":
        u["custom_slot_idx"] = 0
        u["custom_slot_assignments"] = []
        return await prompt_slot_customization(query, context)

    if data.startswith("SELECT_PLAN:"):
        p_idx = int(data.split(":")[1])
        result = u.get("auto_load_result", {})
        plans = result.get("plans", [])
        if p_idx < len(plans):
            chosen_plan = plans[p_idx]
            adjustments = []
            for item in chosen_plan["adjustments"]:
                sub = item["substitute"]
                sub_disp = f"{sub['initials']} ({sub['subject']})"
                chain_steps = sub.get("chain", []) if isinstance(sub, dict) else item.get("chain", [])
                adjustments.append({
                    "subject": sub["subject"] if isinstance(sub, dict) else item.get("subject", ""),
                    "class_div": item["class_div"],
                    "slot": item["slot"],
                    "substitute": sub_disp,
                    "room": item.get("room", ""),
                    "date": u.get("auto_current_date", u.get("from_date", "")),
                    "duty": item.get("duty"),
                    "cascade_detail": item.get("cascade_detail", ""),
                    "chain": chain_steps
                })
            if _auto_is_multiday(u):
                day_key = u.get("auto_current_date")
                u.setdefault("auto_day_results", {})[day_key] = adjustments
                u.setdefault("auto_day_plans", {})[day_key] = chosen_plan
                return await _finish_or_advance_auto_day(query, context)
            u["load_adjustments"] = adjustments
            u["chosen_plan"] = chosen_plan
            u["load_status"] = "Load Adjusted"
            u["has_load"] = True
            return await ask_submit_timing_for_adjusted_load(query.message, context)

    return await handle_universal_callback(update, context)


async def prompt_slot_customization(query, context: ContextTypes.DEFAULT_TYPE):
    u = context.user_data
    duties = u.get("auto_load_duties", [])
    idx = u.get("custom_slot_idx", 0)

    if idx >= len(duties):
        # All slots customized!
        day_adjustments = []
        for assignment in u.get("custom_slot_assignments", []):
            item = dict(assignment)
            item["date"] = u.get("auto_current_date", u.get("from_date", ""))
            day_adjustments.append(item)
        if _auto_is_multiday(u):
            day_key = u.get("auto_current_date")
            u.setdefault("auto_day_results", {})[day_key] = day_adjustments
            return await _finish_or_advance_auto_day(query, context)
        u["load_adjustments"] = day_adjustments
        u["load_status"] = "Load Adjusted"
        u["has_load"] = True
        return await ask_submit_timing_for_adjusted_load(query.message, context)

    duty = duties[idx]
    from timetable_engine import get_timetable_engine
    engine = get_timetable_engine()
    allow_merged = u.get("auto_allow_merged", False)
    max_limit = u.get("auto_max_subject_lectures", u.get("auto_max_lectures_per_div", 2))

    custom_assignments = u.get("custom_slot_assignments", [])
    current_counts = {}
    for ca in custom_assignments:
        c_div = ca.get("class_div", "")
        sub_raw = ca.get("substitute", "")
        m = re.match(r"^([A-Za-z0-9_]{2,6})", str(sub_raw).strip())
        c_init = m.group(1) if m else str(sub_raw).strip()
        c_subject = str(ca.get("subject") or "").strip().upper()
        if c_div and c_subject:
            current_counts[(c_div, c_subject)] = current_counts.get((c_div, c_subject), 0) + 1
        elif c_div and c_init:
            # Legacy custom assignments may not have stored the proxy
            # subject. Preserve their faculty counter as a fallback.
            current_counts[(c_div, c_init)] = current_counts.get((c_div, c_init), 0) + 1
        for step in ca.get("chain", []):
            s_div = step.get("division", "")
            s_rel = step.get("reliever", "")
            s_subject = str(step.get("subject") or step.get("reliever_subject") or "").strip().upper()
            if s_div and s_subject:
                current_counts[(s_div, s_subject)] = current_counts.get((s_div, s_subject), 0) + 1
            elif s_div and s_rel:
                current_counts[(s_div, s_rel)] = current_counts.get((s_div, s_rel), 0) + 1

    cands = engine.find_eligible_substitutes(
        duty,
        max_lectures_per_div=max_limit,
        allow_merged=allow_merged,
        include_cascades=True,
        current_counts=current_counts,
        max_lectures_per_subject=max_limit,
        disturb_other_department=u.get("auto_disturb_other_department", False),
        excluded_faculty_lectures=u.get("auto_excluded_faculty_lectures", {}),
    )

    kb = []
    for c in cands:
        icon = "🟢" if c.get("is_free") else ("🔗" if c.get("is_cascade") else "🔄")
        btn_label = f"{icon} {c['initials']} ({c['subject']}) - {c['name']}"
        kb.append([InlineKeyboardButton(btn_label[:40], callback_data=f"PICK_CAND:{c['initials']}")])

    kb.append([InlineKeyboardButton("🔙 Back to Suggested Plans", callback_data="BACK_TO_AUTO_PLANS")])

    prompt = (
        f"🔧 **Customize Slot {idx + 1} of {len(duties)}:**\n\n"
        f"• **Lecture:** Lec {duty.get('lec_no')} ({duty.get('time')})\n"
        f"• **Division:** `{duty.get('division')}`\n"
        f"• **Scheduled Subject:** {duty.get('subject')}\n\n"
        f"👉 **Select eligible substitute faculty from Division `{duty.get('division')}`:**\n"
        f"*(🟢 = Free | 🔗 = Cascade | 🔄 = Merged)*"
    )
    await safe_edit_text(query, prompt, reply_markup=InlineKeyboardMarkup(kb))
    return LOAD_AUTO_SLOT_PICK


async def load_auto_slot_pick_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    u = context.user_data

    if data == "BACK_TO_AUTO_PLANS":
        return await display_auto_load_options(query, context)

    if data.startswith("PICK_CAND:"):
        cand_init = data.split(":")[1]
        idx = u.get("custom_slot_idx", 0)
        duties = u.get("auto_load_duties", [])
        if idx < len(duties):
            duty = duties[idx]
            from timetable_engine import get_timetable_engine
            engine = get_timetable_engine()
            allow_merged = u.get("auto_allow_merged", False)
            max_limit = u.get("auto_max_subject_lectures", u.get("auto_max_lectures_per_div", 2))

            custom_assignments = u.get("custom_slot_assignments", [])
            current_counts = {}
            for ca in custom_assignments:
                c_div = ca.get("class_div", "")
                sub_raw = ca.get("substitute", "")
                m = re.match(r"^([A-Za-z0-9_]{2,6})", str(sub_raw).strip())
                c_init = m.group(1) if m else str(sub_raw).strip()
                c_subject = str(ca.get("subject") or "").strip().upper()
                if c_div and c_subject:
                    current_counts[(c_div, c_subject)] = current_counts.get((c_div, c_subject), 0) + 1
                elif c_div and c_init:
                    current_counts[(c_div, c_init)] = current_counts.get((c_div, c_init), 0) + 1
                for step in ca.get("chain", []):
                    s_div = step.get("division", "")
                    s_rel = step.get("reliever", "")
                    s_subject = str(step.get("subject") or step.get("reliever_subject") or "").strip().upper()
                    if s_div and s_subject:
                        current_counts[(s_div, s_subject)] = current_counts.get((s_div, s_subject), 0) + 1
                    elif s_div and s_rel:
                        current_counts[(s_div, s_rel)] = current_counts.get((s_div, s_rel), 0) + 1

            cands = engine.find_eligible_substitutes(
                duty,
                max_lectures_per_div=max_limit,
                allow_merged=allow_merged,
                include_cascades=True,
                current_counts=current_counts,
                max_lectures_per_subject=max_limit,
                disturb_other_department=u.get("auto_disturb_other_department", False),
                excluded_faculty_lectures=u.get("auto_excluded_faculty_lectures", {}),
            )
            cand_obj = next((c for c in cands if c['initials'] == cand_init), {})
            if not cand_obj:
                # Re-check the callback against the current constraints. A
                # stale button (for example after hot-reload or a previous
                # custom assignment) must never bypass proxy validation.
                await safe_edit_text(
                    query,
                    "⚠️ That proxy is no longer eligible under the selected constraints. Please choose again.",
                )
                return await prompt_slot_customization(query, context)
            subj = cand_obj.get("subject") or duty.get("subject")

            u.setdefault("custom_slot_assignments", []).append({
                "subject": subj,
                "class_div": duty.get("division"),
                "slot": duty.get("time", f"Lec {duty.get('lec_no')}"),
                "substitute": f"{cand_init} ({subj})",
                "room": duty.get("room", ""),
                "date": u.get("auto_current_date", u.get("from_date", "")),
                "duty": duty,
                "chain": cand_obj.get("chain", []),
                "cascade_detail": cand_obj.get("cascade_detail", "")
            })
            u["custom_slot_idx"] = idx + 1
            return await prompt_slot_customization(query, context)

    return await handle_universal_callback(update, context)


async def ask_duty_count(query):
    kb = [
        [InlineKeyboardButton("1 Duty", callback_data="DUTY_COUNT:1"), InlineKeyboardButton("2 Duties", callback_data="DUTY_COUNT:2")],
        [InlineKeyboardButton("3 Duties", callback_data="DUTY_COUNT:3"), InlineKeyboardButton("4 Duties", callback_data="DUTY_COUNT:4")],
        [InlineKeyboardButton("🤖 Auto-Adjust via Timetable", callback_data="LOAD:AUTO_ADJUST")],
        [InlineKeyboardButton("✍️ Custom Number", callback_data="DUTY_COUNT:CUSTOM")],
        [InlineKeyboardButton("🔙 Back to Load Options", callback_data="BACK_TO_LOAD")],
    ]
    prompt = "📚 **Load Adjusted:** How many duties/lectures do you want to adjust?"
    await safe_edit_text(query, prompt, reply_markup=InlineKeyboardMarkup(kb))
    return LOAD_COUNT


async def load_count_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "BACK_TO_LOAD":
        return await ask_load_choice(query.message)

    if data == "DUTY_COUNT:CUSTOM":
        kb = [[InlineKeyboardButton("🔙 Back to Duty Count", callback_data="BACK_TO_COUNT_OPTS")]]
        await safe_edit_text(query, "Type the number of duties to adjust (e.g. `2` or `5`):", reply_markup=InlineKeyboardMarkup(kb))
        return LOAD_COUNT

    if data == "BACK_TO_COUNT_OPTS":
        return await ask_duty_count(query)

    if not data.startswith("DUTY_COUNT:"):
        return await handle_universal_callback(update, context)

    count_str = data.replace("DUTY_COUNT:", "")
    try:
        count = int(count_str)
    except Exception:
        count = 1

    context.user_data["duty_count"] = count
    return await prompt_duty_details(query.message, count, context)


async def load_count_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        return await handle_universal_callback(update, context)
    if not update.message or not update.message.text:
        return LOAD_COUNT
    text = update.message.text.strip()
    try:
        count = int(text)
    except Exception:
        count = 1
    context.user_data["duty_count"] = count
    return await prompt_duty_details(update.message, count, context)


async def prompt_duty_details(message_obj, count: int, context=None):
    day_hint = ""
    if context is not None and _auto_is_multiday(context.user_data):
        day_hint = "\nFor a multi-day leave, enter duties for the currently displayed leave day only.\n"
    prompt = (
        f"✍️ **Enter Details for {count} Adjusted Duty/Duties** (one per line):\n\n"
        "**Format:**\n"
        "`Class/Div | Time Slot | Subject | Substitute Faculty`\n\n"
        "**Example:**\n"
        "`CE-4A | 9:15 - 10:15 AM | Geotech | Prof. Patel`"
        f"{day_hint}"
    )
    kb = [[InlineKeyboardButton("🔙 Back to Duty Count", callback_data="BACK_TO_COUNT_OPTS")]]
    await message_obj.reply_text(prompt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return LOAD_INPUT


async def load_input_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == "BACK_TO_COUNT_OPTS":
            return await ask_duty_count(query)
        return await handle_universal_callback(update, context)

    if not update.message or not update.message.text:
        return LOAD_INPUT

    lines = update.message.text.strip().split("\n")
    adjustments = []
    for line in lines:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 4:
            adjustments.append({
                "class_div": parts[0],
                "slot": parts[1],
                "subject": parts[2],
                "substitute": parts[3],
                "date": context.user_data.get("auto_current_date", context.user_data.get("from_date", "")),
            })
        else:
            adjustments.append({
                "class_div": parts[0] if len(parts) > 0 else "All",
                "slot": parts[1] if len(parts) > 1 else "Standard",
                "subject": parts[2] if len(parts) > 2 else "Subject",
                "substitute": parts[3] if len(parts) > 3 else "Substitute",
                "date": context.user_data.get("auto_current_date", context.user_data.get("from_date", "")),
            })

    if _auto_is_multiday(context.user_data):
        day_key = context.user_data.get("auto_current_date", context.user_data.get("from_date", ""))
        context.user_data.setdefault("auto_day_results", {})[day_key] = adjustments
        return await _finish_or_advance_auto_day(update.message, context)

    context.user_data["load_adjustments"] = adjustments
    return await ask_submit_timing_for_adjusted_load(update.message, context)


async def ask_submit_timing_for_adjusted_load(message_obj, context=None):
    units = _leave_units(getattr(context, "user_data", {}) if context else {})
    before_credit = _scaled_credit(-1.0, getattr(context, "user_data", {}) if context else {})
    after_credit = _scaled_credit(-1.5, getattr(context, "user_data", {}) if context else {})
    kb = [
        [InlineKeyboardButton(f"🟢 Before leave • { _format_credit(before_credit) } credit", callback_data="TIMING:BEFORE")],
        [InlineKeyboardButton(f"🔴 After leave • { _format_credit(after_credit) } credit", callback_data="TIMING:AFTER")],
        [InlineKeyboardButton("🔙 Back to Duty Details", callback_data="BACK_TO_DUTIES")],
    ]
    prompt = (
        "⏰ **Submission Time & Status for Adjusted Load:**\n\n"
        f"Leave quantity: **{_format_credit(units)} unit(s)**\n"
        "Select when this leave application is submitted:\n"
        f"• 🟢 **Submitted before leave** -> {_format_credit(before_credit)} credit\n"
        f"• 🔴 **Submitted after leave** -> {_format_credit(after_credit)} credit"
    )
    if hasattr(message_obj, "edit_text"):
        await message_obj.edit_text(prompt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else:
        await message_obj.reply_text(prompt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return SUBMIT_TIMING


async def ask_submit_timing_for_self_load(query, context=None):
    units = _leave_units(getattr(context, "user_data", {}) if context else {})
    before_credit = _scaled_credit(0.0, getattr(context, "user_data", {}) if context else {})
    after_credit = _scaled_credit(-0.5, getattr(context, "user_data", {}) if context else {})
    kb = [
        [InlineKeyboardButton(f"🟢 Before leave • { _format_credit(before_credit) } credit", callback_data="TIMING:BEFORE")],
        [InlineKeyboardButton(f"🔴 After leave • { _format_credit(after_credit) } credit", callback_data="TIMING:AFTER")],
        [InlineKeyboardButton("🔙 Back to Load Options", callback_data="BACK_TO_LOAD")],
    ]
    prompt = (
        "⏰ **Submission Time & Status:**\n\n"
        f"Leave quantity: **{_format_credit(units)} unit(s)**\n"
        "Select when this leave application is submitted:\n"
        f"• 🟢 **Submitted before leave** -> { _format_credit(before_credit) } credit\n"
        f"• 🔴 **Submitted after leave** -> { _format_credit(after_credit) } credit"
    )
    await safe_edit_text(query, prompt, reply_markup=InlineKeyboardMarkup(kb))
    return SUBMIT_TIMING


async def ask_load_not_adj_status(query, context=None):
    user_data = getattr(context, "user_data", {}) if context else {}
    units = _leave_units(user_data)
    credits = {
        "hod": _scaled_credit(-1.5, user_data),
        "informed": _scaled_credit(-1.75, user_data),
        "responded": _scaled_credit(-2.0, user_data),
        "not_responded": _scaled_credit(-2.5, user_data),
    }
    kb = [
        [InlineKeyboardButton(f"1️⃣ HOD adjusted • { _format_credit(credits['hod']) }", callback_data="NOT_ADJ:HOD_ADJUSTED")],
        [InlineKeyboardButton(f"2️⃣ Informed HOD by call • { _format_credit(credits['informed']) }", callback_data="NOT_ADJ:CALL_INFORMED")],
        [InlineKeyboardButton(f"3️⃣ Responded to HOD • { _format_credit(credits['responded']) }", callback_data="NOT_ADJ:CALL_RESPONDED")],
        [InlineKeyboardButton(f"4️⃣ Not responded • { _format_credit(credits['not_responded']) }", callback_data="NOT_ADJ:CALL_NOT_RESPONDED")],
        [InlineKeyboardButton("🔙 Back to Load Options", callback_data="BACK_TO_LOAD")],
    ]
    prompt = (
        "⚠️ **Load Not Adjusted - Select HOD & Submission Status:**\n\n"
        f"Leave quantity: **{_format_credit(units)} unit(s)**\n"
        f"1️⃣ Submitted before leave - Adjusted by HOD ({_format_credit(credits['hod'])} credit)\n"
        f"2️⃣ Submitted after leave - Informed HOD by call ({_format_credit(credits['informed'])} credit)\n"
        f"3️⃣ Did not inform - Responded to HOD ({_format_credit(credits['responded'])} credit)\n"
        f"4️⃣ Did not inform - Not responded ({_format_credit(credits['not_responded'])} credit)"
    )
    await safe_edit_text(query, prompt, reply_markup=InlineKeyboardMarkup(kb))
    return LOAD_NOT_ADJ_STATUS


async def load_not_adj_status_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    u = context.user_data

    if data == "BACK_TO_LOAD":
        return await ask_load_choice(query.message)

    mapping = {
        "NOT_ADJ:HOD_ADJUSTED": (-1.5, "Submitted before leave - Adjusted by HOD"),
        "NOT_ADJ:CALL_INFORMED": (-1.75, "Submitted after leave - Informed HOD by call"),
        "NOT_ADJ:CALL_RESPONDED": (-2.0, "Did not inform - Responded to HOD"),
        "NOT_ADJ:CALL_NOT_RESPONDED": (-2.5, "Did not inform - Not responded to HOD"),
    }
    if data not in mapping:
        return await handle_universal_callback(update, context)

    base_penalty, base_desc = mapping[data]
    penalty = _scaled_credit(base_penalty, u)
    desc = f"{base_desc} ({_format_credit(penalty)} credit)"
    u["credit_penalty"] = penalty
    u["submission_status_desc"] = desc

    return await ask_reason(query.message)


async def submit_timing_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    u = context.user_data

    if data == "BACK_TO_LOAD":
        return await ask_load_choice(query.message)
    if data == "BACK_TO_DUTIES":
        count = u.get("duty_count", 1)
        return await prompt_duty_details(query.message, count, context)

    is_adjusted = (u.get("load_status") == "Load Adjusted")

    if data == "TIMING:BEFORE":
        base_penalty = -1.0 if is_adjusted else 0.0
        penalty = _scaled_credit(base_penalty, u)
        desc = f"Submitted before leave ({_format_credit(penalty)} credit)"
    elif data == "TIMING:AFTER":
        base_penalty = -1.5 if is_adjusted else -0.5
        penalty = _scaled_credit(base_penalty, u)
        desc = f"Submitted after leave ({_format_credit(penalty)} credit)"
    else:
        return await handle_universal_callback(update, context)

    u["credit_penalty"] = penalty
    u["submission_status_desc"] = desc
    return await ask_reason(query.message)


async def ask_reason(message_obj):
    reasons = [
        [InlineKeyboardButton("Personal Work", callback_data="Personal Work"), InlineKeyboardButton("Health / Medical", callback_data="Health / Medical")],
        [InlineKeyboardButton("Family Function", callback_data="Family Function"), InlineKeyboardButton("Exam / Official Duty", callback_data="Exam / Official Duty")],
        [InlineKeyboardButton("✍️ Type Custom Reason", callback_data="CUSTOM_REASON")],
        [InlineKeyboardButton("🔙 Back", callback_data="BACK_TO_TIMING_STEP")],
    ]
    prompt = "Select or type **Reason for Leave**:"
    if hasattr(message_obj, "edit_text"):
        await message_obj.edit_text(prompt, reply_markup=InlineKeyboardMarkup(reasons), parse_mode="Markdown")
    else:
        await message_obj.reply_text(prompt, reply_markup=InlineKeyboardMarkup(reasons), parse_mode="Markdown")
    return REASON_CHOICE


async def reason_choice_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    u = context.user_data

    if data == "BACK_TO_TIMING_STEP":
        if u.get("load_status") == "Load Not Adjusted":
            return await ask_load_not_adj_status(query, context)
        elif u.get("load_status") == "Load Adjusted":
            return await ask_submit_timing_for_adjusted_load(query.message, context)
        else:
            return await ask_submit_timing_for_self_load(query, context)

    if data == "CUSTOM_REASON":
        kb = [[InlineKeyboardButton("🔙 Back to Reasons", callback_data="BACK_TO_REASONS")]]
        await safe_edit_text(query, "Type your reason for leave:", reply_markup=InlineKeyboardMarkup(kb))
        return CUSTOM_REASON

    valid_reasons = ["Personal Work", "Health / Medical", "Family Function", "Exam / Official Duty"]
    if data not in valid_reasons:
        return await handle_universal_callback(update, context)

    context.user_data["reason"] = data
    return await show_credit_confirmation(query.message, context)


async def custom_reason_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == "BACK_TO_REASONS":
            return await ask_reason(query.message)
        return await handle_universal_callback(update, context)

    if not update.message or not update.message.text:
        return CUSTOM_REASON

    context.user_data["reason"] = update.message.text.strip()
    return await show_credit_confirmation(update.message, context)


async def show_credit_confirmation(message_obj, context: ContextTypes.DEFAULT_TYPE):
    """Explicitly confirm negative credit before generating PDF."""
    user_id = message_obj.chat_id
    faculty = get_faculty(user_id)
    u = context.user_data

    penalty = u.get("credit_penalty", 0.0)
    penalty_str = _format_credit(penalty)
    if penalty < 0:
        penalty_display = f"🔻 **{penalty_str} Credit(s)**"
    else:
        penalty_display = "🟢 **0.0 Credit (No Deduction)**"

    prompt = (
        f"⚠️ **Credit Deduction & Application Evaluation:**\n\n"
        f"• **Faculty:** {faculty.get('name')} (`{faculty.get('emp_code')}`) - {faculty.get('position', 'AP')}\n"
        f"• **Category:** {u['leave_type']} ({u['units']} Units)\n"
        f"• **Dates:** {u['from_date']} to {u['to_date']}\n"
        f"• **Load Status:** {u.get('load_status')}\n"
        f"• **Submission & HOD Status:** {u.get('submission_status_desc')}\n"
        f"• **Reason:** {u.get('reason')}\n\n"
        f"• **Total Evaluated Credit:** {penalty_display}\n\n"
        f"**Do you confirm this credit deduction and proceed to generate the official PDF?**"
    )
    kb = [
        [InlineKeyboardButton("✅ Confirm & Generate PDF", callback_data="CONFIRM_GEN_PDF")],
        [InlineKeyboardButton("🔙 Change Load Arrangement", callback_data="BACK_TO_LOAD")],
        [InlineKeyboardButton("❌ Cancel Application", callback_data="CANCEL_APPLY")],
    ]
    if hasattr(message_obj, "edit_text"):
        await message_obj.edit_text(prompt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else:
        await message_obj.reply_text(prompt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return CREDIT_CONFIRM


async def credit_confirm_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "CANCEL_APPLY":
        return await return_to_home_screen(query, context, prefix_msg="❌ **Application canceled.**")

    if data == "BACK_TO_LOAD":
        return await ask_load_choice(query.message)

    if data == "CONFIRM_GEN_PDF":
        return await build_and_confirm(query.message, context)

    return await handle_universal_callback(update, context)


def clean_batch_name(div_str: str, batch_str: str = "") -> str:
    """Normalizes division / batch string (e.g. 'D-6' -> 'D6', 'DIV-2' -> 'D2')."""
    if batch_str and str(batch_str).strip():
        return str(batch_str).strip()
    s = (div_str or "").strip()
    m = re.match(r"^([A-Za-z]+)-?([0-9A-Za-z]+)$", s)
    if m:
        prefix = m.group(1).upper()
        suffix = m.group(2)
        if prefix in ("DIV", "DIVISION"):
            prefix = "D"
        return f"{prefix}{suffix}"
    return s.replace("DIV-", "D").replace("DIV ", "D").replace("-", "").strip()


def format_student_whatsapp_messages(u: dict, faculty: dict) -> list[str]:
    """
    Formats copy-ready WhatsApp messages for student class groups strictly matching:
    🔵 Lecture Adjustment Details:
    Batch: D6
    Date: 14-Sep-2026
    Day: Monday
    Lecture No: 1
    Subject as per TT: PYTHON-I(MDP)
    Proxy Subject: MATHS-I(PDB)
    Room No:   506-D
    Also formats adjustment messages for all cascading relieved classes.
    """
    load_adjustments = u.get("load_adjustments", [])
    if not load_adjustments:
        return []

    date_str = str(u.get("from_date", "")).strip()
    dt = None
    for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d-%b-%Y"]:
        try:
            dt = datetime.strptime(date_str, fmt)
            break
        except Exception:
            pass
    if not dt:
        dt = get_ist_now()

    absent_initials = (
        faculty.get("initials")
        or u.get("faculty_initials")
        or faculty.get("emp_code")
        or "FACULTY"
    )

    messages = []
    for idx, adj in enumerate(load_adjustments, 1):
        duty = adj.get("duty") or {}
        # Multi-day applications carry the actual leave day on every
        # adjustment; keep each student notice aligned with its own day.
        adj_date = adj.get("date") or duty.get("date") or date_str
        adj_dt = None
        for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d-%b-%Y"]:
            try:
                adj_dt = datetime.strptime(str(adj_date).strip(), fmt)
                break
            except Exception:
                pass
        adj_dt = adj_dt or dt
        date_formatted = adj_dt.strftime("%d-%b-%Y")
        day_name = adj_dt.strftime("%A")

        # 1. Batch
        raw_div = duty.get("division") or adj.get("class_div", "")
        batch_val = duty.get("batch", "")
        clean_batch = clean_batch_name(raw_div, batch_val)

        # 2. Lecture No
        lec_no = duty.get("lec_no")
        if not lec_no:
            slot_str = str(adj.get("slot", ""))
            m = re.search(r"Lec\s*(\d+)", slot_str, re.IGNORECASE)
            lec_no = int(m.group(1)) if m else idx

        # 3. Subject as per TT
        orig_subj = duty.get("subject") or "LECTURE"
        duty_fac = duty.get("faculty") or absent_initials
        orig_disp = f"{orig_subj}({duty_fac})"

        # 4. Proxy Subject
        sub = adj.get("substitute")
        proxy_init = ""
        proxy_subj = adj.get("subject") or orig_subj

        if isinstance(sub, dict):
            proxy_init = sub.get("initials", "")
            proxy_subj = sub.get("subject") or proxy_subj
        elif isinstance(sub, str):
            sub_clean = sub.strip()
            # Try "PDB (MATHS-I)"
            m1 = re.match(r"^([A-Za-z0-9_]{2,6})\s*\((.*?)\)$", sub_clean)
            if m1:
                proxy_init = m1.group(1)
                proxy_subj = m1.group(2)
            else:
                # Try "MATHS-I (PDB)"
                m2 = re.match(r"^(.*?)\s*\(([A-Za-z0-9_]{2,6})\)$", sub_clean)
                if m2:
                    proxy_subj = m2.group(1)
                    proxy_init = m2.group(2)
                else:
                    proxy_init = sub_clean
        elif sub:
            proxy_init = str(sub)

        proxy_disp = f"{proxy_subj}({proxy_init})"

        # 5. Room No (strictly 3 spaces after 'Room No:')
        room_no = duty.get("room") or adj.get("room") or "506-D"

        msg = (
            "🔵 Lecture Adjustment Details:\n"
            f"Batch: {clean_batch}\n"
            f"Date: {date_formatted}\n"
            f"Day: {day_name}\n"
            f"Lecture No: {lec_no}\n"
            f"Subject as per TT: {orig_disp}\n"
            f"Proxy Subject: {proxy_disp}\n"
            f"Room No:   {room_no}"
        )
        messages.append(msg)

        # Cascade arrangement notices for affected classes in the cascade chain
        chain = adj.get("chain") or []
        if not chain and isinstance(sub, dict):
            chain = sub.get("chain", [])

        for step in chain:
            step_div = step.get("division", "")
            step_batch = clean_batch_name(step_div, "")
            step_lec = step.get("lec_no") or lec_no

            step_orig_subj = step.get("relieved_subject") or "LECTURE"
            step_relieved = step.get("relieved") or ""
            step_orig_disp = f"{step_orig_subj}({step_relieved})"

            step_proxy_subj = step.get("reliever_subject") or step.get("subject") or "LECTURE"
            step_reliever = step.get("reliever") or ""
            step_proxy_disp = f"{step_proxy_subj}({step_reliever})"

            step_room = step.get("room") or room_no

            step_msg = (
                "🔵 Lecture Adjustment Details:\n"
                f"Batch: {step_batch}\n"
                f"Date: {date_formatted}\n"
                f"Day: {day_name}\n"
                f"Lecture No: {step_lec}\n"
                f"Subject as per TT: {step_orig_disp}\n"
                f"Proxy Subject: {step_proxy_disp}\n"
                f"Room No:   {step_room}"
            )
            messages.append(step_msg)

    return messages


async def build_and_confirm(message_obj, context: ContextTypes.DEFAULT_TYPE):
    user_id = message_obj.chat_id
    faculty = get_faculty(user_id)
    u = context.user_data

    deadline_eval = evaluate_submission_deadline(u["day_type"], u["d1"])
    u["deadline_eval"] = deadline_eval

    clean_type = u["leave_type"]
    if clean_type == "SD":
        clean_type = "0.25 CL"

    penalty = u.get("credit_penalty", 0.0)

    short_name = (
        faculty.get("short_name")
        or faculty.get("initials")
        or u.get("short_name")
        or u.get("faculty_initials")
        or ""
    )

    pdf_data = {
        "emp_code": faculty["emp_code"],
        "emp_name": faculty["name"],
        "short_name": short_name,
        "initials": short_name,
        "faculty_initials": short_name,
        "department": faculty["dept"],
        "position": faculty.get("position", "AP"),
        "leave_type": u["leave_type"],
        "day_type": u["day_type"],
        "shift_type": u.get("shift_type", ""),
        "units": u["units"],
        "total_days": u["units"],
        "from_date": u["from_date"],
        "to_date": u["to_date"],
        "punch_timing": u.get("punch_timing", "8:15 AM - 3:00 PM"),
        "reason": u["reason"],
        "deadline_status": deadline_eval["status"],
        "load_status": u.get("load_status", "No Load"),
        "load_adjustments": u.get("load_adjustments", []),
        "score_val": str(penalty),
        "credit_penalty": str(penalty),
        "submission_status_desc": u.get("submission_status_desc", ""),
        "balances_before": faculty.get("balances"),
    }
    file_name = get_leave_filename(pdf_data)
    output_path = os.path.join("generated_pdfs", file_name)
    await asyncio.to_thread(generate_leave_pdf, pdf_data, output_path=output_path)
    u["pdf_path"] = output_path

    # Deliver Generated Document
    with open(output_path, "rb") as f:
        await message_obj.reply_document(
            document=f,
            caption=f"📄 **Leave Report Generated** for {faculty['name']} ({clean_type}) | Credit Score: {_format_credit(penalty)}"
        )

    # WhatsApp Announcement Messages for Student Groups
    if u.get("load_status") == "Load Adjusted" and u.get("load_adjustments"):
        try:
            wa_msgs = format_student_whatsapp_messages(u, faculty)
            if wa_msgs:
                intro = (
                    "📲 **Student WhatsApp Group Notices:**\n"
                    "*(Tap each notice below to copy and forward to the respective student class group)*"
                )
                await message_obj.reply_text(intro, parse_mode="Markdown")
                for m in wa_msgs:
                    await message_obj.reply_text(f"```\n{m}\n```", parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Error generating WhatsApp student messages: {e}")

    # User Confirmation Prompt
    confirm_kb = [
        [InlineKeyboardButton("🚀 Post Leave on Portal", callback_data="POST_PORTAL")],
        [InlineKeyboardButton("📄 Keep PDF Only (Do NOT Post)", callback_data="PDF_ONLY")],
        [InlineKeyboardButton("❌ Discard", callback_data="DISCARD")],
    ]
    summary = (
        f"📋 **Official Application Ready:**\n"
        f"• **Faculty:** {faculty['name']} (`{faculty['emp_code']}`) - {faculty.get('position', 'AP')}\n"
        f"• **Category:** {u['leave_type']} ({u['units']} Units)\n"
        f"• **Dates:** {u['from_date']} to {u['to_date']}\n"
        f"• **Load Status:** {u.get('load_status')}\n"
        f"• **Credit Deduction:** {_format_credit(penalty)} Credit(s)\n\n"
        f"**Do you want to post this leave to the ARS portal?**"
    )
    await message_obj.reply_text(summary, reply_markup=InlineKeyboardMarkup(confirm_kb), parse_mode="Markdown")
    return CONFIRMATION


def deduct_faculty_balance(faculty: dict, leave_type: str, units: float):
    """Deducts leave units from faculty balance store."""
    b = faculty.get("balances", {})
    clean_lt = str(leave_type or "CL").upper().replace(" ", "").replace(".", "")
    if clean_lt in ("SD", "SHORTDAY"):
        cat_key = "CL"
    elif clean_lt in ("EXL", "EX", "EXCHANGE"):
        cat_key = "EXL"
    else:
        cat_key = clean_lt

    if cat_key in b and cat_key != "LWP":
        try:
            curr = float(b[cat_key])
            rem = curr - float(units)
            if abs(rem) < 1e-6:
                rem = 0.0
            b[cat_key] = str(int(rem)) if rem == int(rem) else f"{rem:.2f}".rstrip("0").rstrip(".")
        except Exception:
            pass
    faculty["balances"] = b
    return b


def format_balances_summary_text(faculty_name: str, balances: dict):
    b = balances or {}
    return (
        f"📊 **Updated Leave Balances for {faculty_name}:**\n\n"
        f"• Casual Leave (CL): **{b.get('CL', '0')}**\n"
        f"• Short Day (SD): **{b.get('SD', '0')}**\n"
        f"• Earned Leave (EL): **{b.get('EL', '0')}**\n"
        f"• Sick Leave (SL): **{b.get('SL', '0')}**\n"
        f"• Restricted Holiday (RH): **{b.get('RH', '0')}**\n"
        f"• Vacation Leave (VL): **{b.get('VL', '0')}**\n"
        f"• Duty Leave (DL): **{b.get('DL', '0')}**\n"
        f"• Exchanged Leave (Ex.L): **{b.get('EXL', '0')}**\n"
        f"• Women Medical Leave (WML): **{b.get('WML', '0')}**\n"
        f"• Leave Without Pay (LWP): **{b.get('LWP', '0')}**"
    )


async def confirmation_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data
    user_id = update.effective_user.id
    faculty = get_faculty(user_id) or {}
    u = context.user_data

    menu_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Apply for Another Leave", callback_data="CMD_APPLY")],
        [InlineKeyboardButton("📊 Check Balances", callback_data="CMD_BALANCE")],
    ])

    if choice == "POST_PORTAL":
        await safe_edit_text(query, "⏳ Submitting leave application to ARS portal...")
        emp_code = faculty.get("emp_code") or faculty.get("username")
        if not emp_code or not faculty.get("password"):
            return await return_to_home_screen(
                query,
                context,
                prefix_msg="❌ **Portal Credentials Missing!**\nPlease link your ARS portal credentials in /profile before applying to portal."
            )

        try:
            payload = {
                "username": emp_code,
                "password": faculty.get("password"),
                "login_year": faculty.get("login_year", "01/07/2026LJIET"),
                "leave_type": u.get("leave_type", "CL"),
                "day_type": u.get("day_type", "Full Day"),
                "from_date": u.get("from_date", ""),
                "to_date": u.get("to_date", ""),
                "units": u.get("units", 1),
                "reason": u.get("reason", "Personal Work"),
            }
            portal = PortalSession(
                username=emp_code,
                password=faculty.get("password"),
                login_year=faculty.get("login_year", "01/07/2026LJIET")
            )
            login_ok, login_msg = await asyncio.to_thread(portal.login)
            if login_ok:
                dry_run = is_dry_run_mode()
                succ, msg = await asyncio.to_thread(
                    portal.submit_leave,
                    payload,
                    dry_run=dry_run
                )
            else:
                succ, msg = False, login_msg or "Portal login failed"

            if succ:
                # Only deduct balance upon confirmed portal success!
                deduct_faculty_balance(faculty, u["leave_type"], u.get("units", 1))
                save_faculty(user_id, faculty)

                log_application_to_db({
                    "emp_code": emp_code,
                    "faculty_name": faculty.get("name", "Faculty"),
                    "category": "LEAVE",
                    "leave_type": u.get("leave_type", "CL"),
                    "from_date": u.get("from_date", ""),
                    "to_date": u.get("to_date") or u.get("from_date", ""),
                    "total_days": u.get("units", 1),
                    "reason": u.get("reason", "Personal Work"),
                    "submitted_at": get_ist_now().strftime("%d/%m/%Y %I:%M %p"),
                    "status": "✅ Applied on Portal",
                    "portal_verified": True
                })

                bal_summary = format_balances_summary_text(faculty.get("name", "Faculty"), faculty.get("balances", {}))
                success_msg = (
                    "✅ **Leave Posted to ARS Portal Successfully!**\n\n"
                    f"• **Faculty:** {faculty.get('name', 'Faculty')} (`{emp_code}`)\n"
                    f"• **Leave:** {u['leave_type']} ({u['day_type']})\n"
                    f"• **Dates:** {u['from_date']} to {u['to_date']}\n\n"
                    "🎉 **Leave balance updated successfully in your profile.**\n\n"
                    f"{bal_summary}\n\n"
                    "1. Forward the PDF to your HOD WhatsApp leave group.\n"
                    "2. Ensure substitute faculties reply with **'AGREE'**.\n"
                    "3. All leaves are granted by HOD on Saturdays."
                )
                await query.message.reply_text(success_msg, parse_mode="Markdown")
                return await return_to_home_screen(query.message, context)
            else:
                bal_summary = format_balances_summary_text(faculty.get("name", "Faculty"), faculty.get("balances", {}))
                await query.message.reply_text(
                    f"⚠️ **Portal Application Not Applied:**\n{msg}\n\n"
                    f"ℹ️ *Your balance was NOT deducted.* You can print the generated PDF for physical submission or check your password in /profile.\n\n"
                    f"{bal_summary}",
                    parse_mode="Markdown"
                )
                return await return_to_home_screen(query.message, context)
        except Exception as e:
            bal_summary = format_balances_summary_text(faculty.get("name", "Faculty"), faculty.get("balances", {}))
            await query.message.reply_text(
                f"⚠️ **Portal Connection Error:** {e}\n\n"
                f"ℹ️ *Your balance was NOT deducted.* You can print the PDF or check your connection/credentials in /profile.\n\n"
                f"{bal_summary}",
                parse_mode="Markdown"
            )
            return await return_to_home_screen(query.message, context)

    elif choice == "PDF_ONLY":
        log_application_to_db({
            "emp_code": faculty.get("emp_code") or faculty.get("username", ""),
            "faculty_name": faculty.get("name", "Faculty"),
            "category": "LEAVE",
            "leave_type": u.get("leave_type", "CL"),
            "from_date": u.get("from_date", ""),
            "to_date": u.get("to_date") or u.get("from_date", ""),
            "total_days": u.get("units", 1),
            "reason": u.get("reason", "Personal Work"),
            "submitted_at": get_ist_now().strftime("%d/%m/%Y %I:%M %p"),
            "status": "📄 PDF Generated (Manual)",
            "portal_verified": False
        })
        prompt = (
            "📄 **Leave Report PDF generated for manual portal submission.**\n\n"
            "Would you like to update/deduct this leave from your leave balance in the bot now?"
        )
        kb = [
            [InlineKeyboardButton("✅ Yes, Update My Balance", callback_data="BAL_DEDUCT:YES")],
            [InlineKeyboardButton("❌ No, Keep Current Balance", callback_data="BAL_DEDUCT:NO")],
        ]
        await safe_edit_text(query, prompt, reply_markup=InlineKeyboardMarkup(kb))
        return CONFIRMATION

    elif choice == "BAL_DEDUCT:YES":
        deduct_faculty_balance(faculty, u.get("leave_type", "CL"), u.get("units", 1))
        save_faculty(user_id, faculty)
        return await return_to_home_screen(
            query,
            context,
            prefix_msg="✅ **Leave balance updated successfully in your profile!**"
        )

    elif choice == "BAL_DEDUCT:NO":
        return await return_to_home_screen(
            query,
            context,
            prefix_msg="ℹ️ **Leave balance unchanged in bot profile.**"
        )

    elif choice == "DISCARD":
        return await return_to_home_screen(
            query,
            context,
            prefix_msg="❌ **Application canceled.**"
        )
    else:
        return await handle_universal_callback(update, context)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await return_to_home_screen(update, context, prefix_msg="❌ **Process canceled.**")


# ==========================================
# UNIVERSAL CALLBACK ROUTER & ERROR HANDLER
# ==========================================
async def handle_universal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Universal callback dispatcher:
    Catches any button clicked on older messages or outside current state.
    Ensures zero 'Bad Input' errors, immediately answers callback query so no spinner hangs,
    and intelligently transitions the user to the clicked action or restarts cleanly.
    """
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except Exception:
        pass

    data = query.data or ""
    user_id = update.effective_user.id
    faculty = get_faculty(user_id)
    u = context.user_data

    # 1. Registration entry & actions
    if data == "START_REG":
        return await register_start(update, context)
    elif data == "CANCEL_REG":
        return await return_to_home_screen(query, context, prefix_msg="❌ **Registration canceled.**")
    elif data in ["REG_CONFIRM_AUTO", "REG_TYPE_CUSTOM_NAME"]:
        return await reg_confirm_received(update, context)
    elif data.startswith("DEPT:"):
        return await reg_dept_received(update, context)
    elif data.startswith("POS:"):
        return await reg_pos_received(update, context)
    elif data.startswith("SHORT_CONFIRM:") or data in ["SHORT_CUSTOM", "BACK_TO_SHORT_PICK"]:
        return await reg_short_name_received(update, context)
    elif data.startswith("BAL_MODE:"):
        return await reg_bal_choice_picked(update, context)
    elif data.startswith("BAL_CONFIRM:"):
        return await reg_bal_verify_picked(update, context)
    elif data.startswith("BAL_EDIT:") or data in ["BAL_EDIT_CANCEL", "BACK_TO_BAL_MENU"]:
        return await reg_bal_manual_received(update, context)
    elif data in ["BAL_DEDUCT:YES", "BAL_DEDUCT:NO"]:
        return await confirmation_picked(update, context)
    elif data == "BACK_TO_POS":
        kb = [
            [InlineKeyboardButton("Assistant Professor (AP)", callback_data="POS:Assistant Professor:AP")],
            [InlineKeyboardButton("Associate Professor (ASP)", callback_data="POS:Associate Professor:ASP")],
            [InlineKeyboardButton("Professor (PROF)", callback_data="POS:Professor:PROF")],
            [InlineKeyboardButton("Lab Assistant (LA)", callback_data="POS:Lab Assistant:LA")],
            [InlineKeyboardButton("✍️ Type Custom Designation", callback_data="POS:CUSTOM")],
            [InlineKeyboardButton("🔙 Back to Department", callback_data="BACK_TO_DEPT")],
        ]
        await safe_edit_text(query, "Select your **Position / Designation**:", reply_markup=InlineKeyboardMarkup(kb))
        return REG_POS
    elif data == "BACK_TO_DEPT":
        kb = build_dept_keyboard()
        await safe_edit_text(query, "Select or type your **Department**:", reply_markup=InlineKeyboardMarkup(kb))
        return REG_DEPT

    # 2. Main menu commands
    if data == "CMD_BALANCE":
        await balance_cmd(update, context)
        return ConversationHandler.END
    if data in ["CMD_STATUS", "REFRESH_STATUS"]:
        await status_cmd(update, context)
        return ConversationHandler.END
    if data == "CMD_LOGOUT":
        await logout_cmd(update, context)
        return ConversationHandler.END
    if data == "CMD_APPLY":
        return await apply_start(update, context)
    if data == "CANCEL_APPLY":
        return await return_to_home_screen(query, context, prefix_msg="❌ **Leave application canceled.**")
    if data.startswith("PRESET_LOAD:"):
        return await load_auto_preset_picked(update, context)
    if data == "CMD_CHECK_LOAD":
        return await check_load_start(update, context)
    if data.startswith("CHK_"):
        return await check_load_view_picked(update, context)
    if data == "CMD_EDIT_PROFILE":
        return await profile_start(update, context)
    if data.startswith("PROF_EDIT:"):
        return await profile_menu_picked(update, context)
    if data in ["BACK_TO_PROFILE", "PROF_CANCEL"]:
        u.pop("is_profile_bal_edit", None)
        if faculty:
            p_text, p_markup = format_profile_menu(faculty)
            await safe_edit_text(query, p_text, reply_markup=p_markup)
            return PROF_MENU
        return await start_cmd(update, context)
    if data == "CMD_ATTENDANCE":
        return await attendance_cmd(update, context)
    if data.startswith("ATT_") or data.startswith("ATT:"):
        return await attendance_callback_handler(update, context)
    if data in ["CMD_WELCOME", "CMD_HOME"]:
        if faculty:
            text, markup = format_welcome_screen(faculty)
            await safe_edit_text(query, text, reply_markup=markup)
        else:
            await start_cmd(update, context)
        return ConversationHandler.END

    # Admin actions
    if data == "CMD_ADMIN":
        return await admin_start(update, context)
    if data in ["ADMIN_CANCEL", "ADMIN_EXIT"]:
        return await return_to_home_screen(query, context, prefix_msg="👋 Admin Panel closed.")
    if data == "ADMIN_LOGOUT":
        AUTHENTICATED_ADMINS.discard(user_id)
        return await return_to_home_screen(query, context, prefix_msg="🔒 **Logged out from Admin Panel.** Your admin session has ended.")
    if data == "ADMIN_CHOOSE_UPLOAD" or data.startswith("ADMIN_TARGET:") or data in ["ADMIN_VIEW_STATUS", "ADMIN_FORCE_RELOAD", "ADMIN_BACK_MENU"]:
        return await admin_menu_picked(update, context)

    # Shift change actions
    if data == "CMD_SHIFT_CHANGE":
        return await shift_start(update, context)
    if data == "CANCEL_SHIFT":
        return await return_to_home_screen(query, context, prefix_msg="❌ **Shift Change Application Canceled.**")
    if data.startswith("SHIFT_DATE:"):
        return await shift_date_picked(update, context)
    if data.startswith("SHIFT_CAT:") or data.startswith("SHIFT_NEW:"):
        return await shift_new_picked(update, context)
    if data.startswith("SHIFT_RSN:"):
        return await shift_reason_picked(update, context)
    if data == "SHIFT_BACK_TO_DATE":
        return await shift_start(update, context)
    if data == "SHIFT_BACK_TO_NEW":
        kb = build_shift_keyboard("teaching_common")
        await safe_edit_text(query, "Select your **New Shift**:", reply_markup=InlineKeyboardMarkup(kb))
        return SHIFT_NEW_PICK
    if data == "SHIFT_BACK_TO_REASON":
        kb = build_shift_reason_keyboard()
        await safe_edit_text(query, "Select or type the **Reason** for this shift change:", reply_markup=InlineKeyboardMarkup(kb))
        return SHIFT_REASON_PICK
    if data == "SHIFT_GEN_PDF":
        return await shift_confirm_picked(update, context)
    if data.startswith("SHIFT_SUBMIT:"):
        return await shift_submission_picked(update, context)

    # 3. Application Flow Buttons
    if not faculty:
        await query.message.reply_text(
            "⚠️ Please link your ARS portal account first with /register or click below:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Register Account", callback_data="START_REG")]])
        )
        return ConversationHandler.END

    if data == "FORCE_APPLY_DURATION":
        context.user_data["allow_negative_balance"] = True
        return await ask_load_choice(query.message)

    if data.startswith("FORCE_APPLY:"):
        actual_lt = data.split(":", 1)[1]
        context.user_data["allow_negative_balance"] = True
        return await proceed_with_leave_type(update, context, actual_lt)

    if data in ["CL", "SD", "SL", "EL", "RH", "VL", "LWP", "ExL", "DL"]:
        return await leave_type_chosen(update, context)

    if data in ["Full Day", "1st Half", "2nd Half", "Morning Short", "Afternoon Short"]:
        if "leave_type" not in u:
            u["leave_type"] = "SD" if "Short" in data else "CL"
        return await day_type_chosen(update, context)

    if data.startswith("DATE:") or data == "CUSTOM_DATE" or re.search(r"\d{1,2}/\d{1,2}/\d{4}", data):
        if "leave_type" not in u:
            u["leave_type"] = "CL"
        if "day_type" not in u:
            u["day_type"] = "Full Day"
        return await date_picked(update, context)

    if data.startswith("LOAD:"):
        if "leave_type" not in u or "from_date" not in u:
            today_str = get_ist_today_str("%d/%m/%Y")
            u.setdefault("leave_type", "CL")
            u.setdefault("day_type", "Full Day")
            u.setdefault("from_date", today_str)
            u.setdefault("to_date", today_str)
            u.setdefault("units", "1")
            u.setdefault("d1", get_ist_now())
        return await load_choice_picked(update, context)

    if data.startswith("DUTY_COUNT:") or data == "BACK_TO_COUNT_OPTS":
        return await load_count_picked(update, context)

    if data.startswith("NOT_ADJ:"):
        return await load_not_adj_status_picked(update, context)

    if data.startswith("MAX_DIV:") or data == "AUTO_NO_LOAD_CONTINUE":
        return await load_auto_max_div_picked(update, context)

    if data.startswith("MERGED_OPT:"):
        return await load_auto_merged_picked(update, context)

    if data.startswith("SELECT_PLAN:") or data in ["AUTO_CUSTOMIZE_SLOTS", "BACK_TO_AUTO_PLANS"]:
        return await load_auto_options_picked(update, context)

    if data.startswith("PICK_CAND:"):
        return await load_auto_slot_pick_handler(update, context)

    if data.startswith("TIMING:"):
        return await submit_timing_picked(update, context)

    if data in ["Personal Work", "Health / Medical", "Family Function", "Exam / Official Duty", "CUSTOM_REASON"] or data.startswith("REASON:"):
        return await reason_choice_picked(update, context)

    if data == "CONFIRM_GEN_PDF":
        return await credit_confirm_picked(update, context)

    if data in ["POST_PORTAL", "PDF_ONLY", "DISCARD"]:
        if "leave_type" in u and "from_date" in u:
            return await confirmation_picked(update, context)
        else:
            await query.message.reply_text("ℹ️ That leave application was already completed. Use /apply to submit a new leave.")
            return ConversationHandler.END

    if data == "BACK_TO_LEAVE_TYPE":
        return await apply_start(update, context)
    if data == "BACK_TO_DAY_TYPE":
        return await leave_type_chosen(update, context)
    if data in ["BACK_TO_DATE", "BACK_TO_DATE_PICK"]:
        return await show_date_picker(query, "Select **Date of Leave**:", back_callback="BACK_TO_DAY_TYPE")
    if data == "BACK_TO_LOAD":
        return await ask_load_choice(query.message)
    if data == "BACK_TO_TIMING_STEP":
        if u.get("load_status") == "Load Not Adjusted":
            return await ask_load_not_adj_status(query, context)
        elif u.get("load_status") == "Load Adjusted":
            return await ask_submit_timing_for_adjusted_load(query.message, context)
        else:
            return await ask_submit_timing_for_self_load(query, context)
    if data == "BACK_TO_REASONS":
        return await ask_reason(query.message)

    await query.message.reply_text("ℹ️ That selection is from an earlier action. Use /apply to start a new leave application or /balance to check balances.")
    return ConversationHandler.END


# ==========================================
# 7.5. SHIFT CHANGE APPLICATION HANDLERS
# ==========================================
def build_shift_keyboard(category="teaching_common"):
    kb = []
    if category == "teaching_common":
        common = [
            ("T2 (8:15 AM - 3:00 PM)", "SHIFT_NEW:T2"),
            ("T12 (9:15 AM - 4:00 PM)", "SHIFT_NEW:T12"),
            ("T1 (9:45 AM - 5:00 PM)", "SHIFT_NEW:T1"),
            ("T3 (8:15 AM - 4:00 PM)", "SHIFT_NEW:T3"),
            ("T4 (10:45 AM - 5:30 PM)", "SHIFT_NEW:T4"),
            ("T16 (9:30 AM - 4:15 PM)", "SHIFT_NEW:T16"),
        ]
        for i in range(0, len(common), 2):
            row = [InlineKeyboardButton(common[i][0], callback_data=common[i][1])]
            if i + 1 < len(common):
                row.append(InlineKeyboardButton(common[i+1][0], callback_data=common[i+1][1]))
            kb.append(row)
        kb.append([InlineKeyboardButton("📋 All Teaching Shifts (19)", callback_data="SHIFT_CAT:teaching_all")])
        kb.append([InlineKeyboardButton("🏢 Non-Teaching Shifts (15)", callback_data="SHIFT_CAT:non_teaching")])
    elif category == "teaching_all":
        teaching_codes = ["T1", "T2", "T3", "T4", "T6", "T7", "T8", "T9", "T12", "T13", "T14", "T15", "T16", "T17", "T18", "Ta5", "Ta10", "Ta11", "Ta12"]
        for i in range(0, len(teaching_codes), 2):
            c1 = teaching_codes[i]
            d1 = SHIFT_CATALOG.get(c1, {})
            row = [InlineKeyboardButton(f"{c1} ({d1.get('start')}-{d1.get('end')})", callback_data=f"SHIFT_NEW:{c1}")]
            if i + 1 < len(teaching_codes):
                c2 = teaching_codes[i+1]
                d2 = SHIFT_CATALOG.get(c2, {})
                row.append(InlineKeyboardButton(f"{c2} ({d2.get('start')}-{d2.get('end')})", callback_data=f"SHIFT_NEW:{c2}"))
            kb.append(row)
        kb.append([InlineKeyboardButton("⭐ Common Teaching Shifts", callback_data="SHIFT_CAT:teaching_common")])
        kb.append([InlineKeyboardButton("🏢 Non-Teaching Shifts", callback_data="SHIFT_CAT:non_teaching")])
    elif category == "non_teaching":
        nt_codes = [f"Nt{i}" for i in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16]]
        for i in range(0, len(nt_codes), 2):
            c1 = nt_codes[i]
            d1 = SHIFT_CATALOG.get(c1, {})
            row = [InlineKeyboardButton(f"{c1} ({d1.get('start')}-{d1.get('end')})", callback_data=f"SHIFT_NEW:{c1}")]
            if i + 1 < len(nt_codes):
                c2 = nt_codes[i+1]
                d2 = SHIFT_CATALOG.get(c2, {})
                row.append(InlineKeyboardButton(f"{c2} ({d2.get('start')}-{d2.get('end')})", callback_data=f"SHIFT_NEW:{c2}"))
            kb.append(row)
        kb.append([InlineKeyboardButton("⭐ Common Teaching Shifts", callback_data="SHIFT_CAT:teaching_common")])
        kb.append([InlineKeyboardButton("📋 All Teaching Shifts", callback_data="SHIFT_CAT:teaching_all")])

    kb.append([InlineKeyboardButton("🔙 Back to Date Selection", callback_data="SHIFT_BACK_TO_DATE")])
    kb.append([InlineKeyboardButton("❌ Cancel Shift Change", callback_data="CANCEL_SHIFT")])
    return kb


def build_shift_reason_keyboard():
    return [
        [InlineKeyboardButton("Exam Duty / Supervision", callback_data="SHIFT_RSN:Exam Duty / Supervision")],
        [InlineKeyboardButton("Academic Work / Project", callback_data="SHIFT_RSN:Academic Work / Project")],
        [InlineKeyboardButton("Departmental Activity", callback_data="SHIFT_RSN:Departmental Activity")],
        [InlineKeyboardButton("Meeting / Administrative", callback_data="SHIFT_RSN:Meeting / Administrative")],
        [InlineKeyboardButton("Personal Work", callback_data="SHIFT_RSN:Personal Work")],
        [InlineKeyboardButton("✍️ Type Custom Reason", callback_data="SHIFT_RSN:CUSTOM")],
        [InlineKeyboardButton("🔙 Back to Shift Selection", callback_data="SHIFT_BACK_TO_NEW")],
        [InlineKeyboardButton("❌ Cancel", callback_data="CANCEL_SHIFT")]
    ]


async def shift_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()

    user_id = update.effective_user.id
    faculty = get_faculty(user_id)
    if not faculty:
        msg = "⚠️ Please register your ARS credentials first before applying for a shift change."
        kb = [[InlineKeyboardButton("🔐 Register Account", callback_data="START_REG")]]
        if query:
            await safe_edit_text(query, msg, reply_markup=InlineKeyboardMarkup(kb))
        else:
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return ConversationHandler.END

    emp_code = faculty.get("emp_code") or faculty.get("username")
    if not emp_code:
        msg = "⚠️ Please register your ARS credentials first before applying for a shift change."
        kb = [[InlineKeyboardButton("🔐 Register Account", callback_data="START_REG")]]
        if query:
            await safe_edit_text(query, msg, reply_markup=InlineKeyboardMarkup(kb))
        else:
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return ConversationHandler.END

    emp_name = faculty.get("name") or "Faculty Member"
    dept = faculty.get("dept") or "FY1"
    designation = faculty.get("position") or faculty.get("pos_desc") or "Assistant Professor"
    short_name = faculty.get("short_name") or faculty.get("initials") or ""

    context.user_data["shift_data"] = {
        "emp_code": emp_code,
        "emp_name": emp_name,
        "short_name": short_name,
        "initials": short_name,
        "institute": "LJIET",
        "department": dept,
        "designation": designation,
        "current_shift": "T2",
        "new_shift": None,
        "from_date": None,
        "to_date": None,
        "reason": None,
        "director_remark": "",
        "password": faculty.get("password")
    }

    now = get_ist_now()
    today_str = now.strftime("%d/%m/%Y")
    tomorrow_str = (now + timedelta(days=1)).strftime("%d/%m/%Y")

    text = (
        "🔄 **Shift Change Application**\n\n"
        f"• **Faculty:** {emp_name} (`{emp_code}`)\n"
        f"• **Department:** {dept} | **Institute:** LJIET\n"
        f"• **Current Default Shift:** 8:15 AM TO 3:00 PM (T2)\n\n"
        "Please select the **Date** for this shift change:"
    )
    kb = [
        [InlineKeyboardButton(f"📅 Today ({today_str})", callback_data="SHIFT_DATE:TODAY")],
        [InlineKeyboardButton(f"📅 Tomorrow ({tomorrow_str})", callback_data="SHIFT_DATE:TOMORROW")],
        [InlineKeyboardButton("✍️ Custom Date or Date Range", callback_data="SHIFT_DATE:CUSTOM")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="CMD_WELCOME")]
    ]
    if query:
        await safe_edit_text(query, text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return SHIFT_DATE_PICK


async def shift_date_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        data = query.data

        if data == "SHIFT_DATE:TODAY":
            today = get_ist_today_str("%d/%m/%Y")
            context.user_data["shift_data"]["from_date"] = today
            context.user_data["shift_data"]["to_date"] = today
        elif data == "SHIFT_DATE:TOMORROW":
            tom = (get_ist_today() + timedelta(days=1)).strftime("%d/%m/%Y")
            context.user_data["shift_data"]["from_date"] = tom
            context.user_data["shift_data"]["to_date"] = tom
        elif data == "SHIFT_DATE:CUSTOM":
            msg = (
                "✍️ **Enter Shift Date or Range**\n\n"
                "Please reply with the date in `DD/MM/YYYY` format.\n"
                "Examples:\n"
                "• Single date: `30/07/2026`\n"
                "• Date range: `30/07/2026 to 05/08/2026`\n"
            )
            kb = [[InlineKeyboardButton("🔙 Back to Date Selection", callback_data="SHIFT_BACK_TO_DATE")]]
            await safe_edit_text(query, msg, reply_markup=InlineKeyboardMarkup(kb))
            return SHIFT_CUSTOM_DATE
        elif data == "SHIFT_BACK_TO_DATE":
            return await shift_start(update, context)
        elif data == "CANCEL_SHIFT":
            return await return_to_home_screen(query, context, prefix_msg="❌ **Shift change application canceled.**")
        else:
            return await handle_universal_callback(update, context)

    # Date is set, now show shift picker
    kb = build_shift_keyboard("teaching_common")
    s_data = context.user_data.get("shift_data", {})
    frm = s_data.get("from_date", "")
    to = s_data.get("to_date", frm)
    date_disp = frm if frm == to else f"{frm} to {to}"

    prompt = (
        f"📅 Shift Date: **{date_disp}**\n\n"
        f"Select the **New Shift** you want to apply for:\n"
        f"*(Current default shift is T2: 8:15 AM - 3:00 PM)*"
    )
    if query:
        await safe_edit_text(query, prompt, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(prompt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return SHIFT_NEW_PICK


def parse_custom_dates(text: str):
    """
    Parses a single date or date range from user input for shift change.
    Returns (from_date_str, to_date_str) in DD/MM/YYYY format, or (None, None) if invalid.
    """
    if not text:
        return None, None
    s = text.strip()

    # Check for range separated by " to " or " - "
    if " to " in s.lower():
        parts = [p.strip() for p in re.split(r"\s+to\s+", s, flags=re.IGNORECASE) if p.strip()]
    elif " - " in s:
        parts = [p.strip() for p in s.split(" - ") if p.strip()]
    else:
        parts = [s]

    def _parse_single(token: str):
        token = token.strip()
        tl = token.lower()
        if tl == "today":
            return get_ist_today()
        if tl == "tomorrow":
            return get_ist_today() + timedelta(days=1)

        for fmt in [
            "%d/%m/%Y", "%d-%m-%Y",
            "%d/%m/%y", "%d-%m-%y",
            "%d/%b/%Y", "%d-%b-%Y", "%d %b %Y",
            "%d/%B/%Y", "%d-%B-%Y", "%d %B %Y",
            "%Y-%m-%d", "%Y/%m/%d"
        ]:
            try:
                return datetime.strptime(token, fmt).date()
            except ValueError:
                pass

        m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$", token)
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if y < 100:
                y += 2000
            try:
                return date(y, mo, d)
            except ValueError:
                pass
        return None

    if len(parts) == 1:
        d = _parse_single(parts[0])
        if d:
            ds = d.strftime("%d/%m/%Y")
            return ds, ds
        return None, None
    elif len(parts) == 2:
        d1 = _parse_single(parts[0])
        d2 = _parse_single(parts[1])
        if d1 and d2:
            if d2 < d1:
                return None, None
            return d1.strftime("%d/%m/%Y"), d2.strftime("%d/%m/%Y")
        return None, None
    return None, None


async def shift_custom_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        data = query.data
        if data == "SHIFT_BACK_TO_DATE":
            return await shift_start(update, context)
        if data == "CANCEL_SHIFT":
            return await return_to_home_screen(query, context, prefix_msg="❌ **Shift change application canceled.**")
        return await handle_universal_callback(update, context)

    if not update.message or not update.message.text:
        return SHIFT_CUSTOM_DATE

    text = update.message.text.strip()
    d1, d2 = parse_custom_dates(text)
    if not d1:
        kb = [[InlineKeyboardButton("🔙 Back to Date Selection", callback_data="SHIFT_BACK_TO_DATE")]]
        await update.message.reply_text(
            "⚠️ **Invalid Date Format.**\n\n"
            "Please enter date like `30/07/2026` or range like `30/07/2026 to 05/08/2026`:",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return SHIFT_CUSTOM_DATE

    s_data = context.user_data.setdefault("shift_data", {})
    if "emp_code" not in s_data:
        user_id = update.effective_user.id
        fac = get_faculty(user_id) or {}
        s_data.update({
            "emp_code": fac.get("emp_code") or fac.get("username") or "00000365",
            "emp_name": fac.get("name") or "Faculty Member",
            "institute": "LJIET",
            "department": fac.get("dept") or "FY1",
            "designation": fac.get("position") or "Assistant Professor",
            "current_shift": "T2",
            "password": fac.get("password")
        })

    s_data["from_date"] = d1
    s_data["to_date"] = d2

    date_disp = d1 if d1 == d2 else f"{d1} to {d2}"
    kb = build_shift_keyboard("teaching_common")
    prompt = (
        f"📅 Shift Date: **{date_disp}**\n\n"
        f"Select the **New Shift** you want to apply for:\n"
        f"*(Current default shift is T2: 8:15 AM - 3:00 PM)*"
    )
    await update.message.reply_text(prompt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return SHIFT_NEW_PICK


async def shift_new_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return SHIFT_NEW_PICK
    await query.answer()
    data = query.data

    if data.startswith("SHIFT_CAT:"):
        cat = data.split(":")[1]
        kb = build_shift_keyboard(cat)
        s_data = context.user_data.get("shift_data", {})
        frm = s_data.get("from_date", "")
        to = s_data.get("to_date", frm)
        date_disp = frm if frm == to else f"{frm} to {to}"
        prompt = (
            f"📅 Shift Date: **{date_disp}**\n\n"
            f"Select the **New Shift** you want to apply for:"
        )
        await safe_edit_text(query, prompt, reply_markup=InlineKeyboardMarkup(kb))
        return SHIFT_NEW_PICK

    if data == "SHIFT_BACK_TO_DATE":
        return await shift_start(update, context)

    if data == "CANCEL_SHIFT":
        return await return_to_home_screen(query, context, prefix_msg="❌ **Shift change application canceled.**")

    if data.startswith("SHIFT_NEW:"):
        code = data.split(":")[1]
        context.user_data.setdefault("shift_data", {})["new_shift"] = code
        kb = build_shift_reason_keyboard()
        disp = get_shift_display(code)
        prompt = (
            f"🕒 Selected New Shift: **{disp}**\n\n"
            "Select or type the **Reason** for this shift change:"
        )
        await safe_edit_text(query, prompt, reply_markup=InlineKeyboardMarkup(kb))
        return SHIFT_REASON_PICK

    return await handle_universal_callback(update, context)


async def shift_reason_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return SHIFT_REASON_PICK
    await query.answer()
    data = query.data

    if data == "SHIFT_BACK_TO_NEW":
        kb = build_shift_keyboard("teaching_common")
        await safe_edit_text(query, "Select your **New Shift**:", reply_markup=InlineKeyboardMarkup(kb))
        return SHIFT_NEW_PICK

    if data == "CANCEL_SHIFT":
        return await return_to_home_screen(query, context, prefix_msg="❌ **Shift change application canceled.**")

    if data == "SHIFT_RSN:CUSTOM":
        msg = (
            "✍️ **Enter Shift Change Reason**\n\n"
            "Please type your reason (e.g. `PHY-I & PHY-II Rem Exam 2 to 4` or `Project Mentoring`):"
        )
        kb = [[InlineKeyboardButton("🔙 Back to Predefined Reasons", callback_data="SHIFT_BACK_TO_REASON")]]
        await safe_edit_text(query, msg, reply_markup=InlineKeyboardMarkup(kb))
        return SHIFT_CUSTOM_REASON

    if data.startswith("SHIFT_RSN:"):
        rsn = data.split(":", 1)[1]
        context.user_data.setdefault("shift_data", {})["reason"] = rsn
        return await shift_show_confirmation(query, context)

    return await handle_universal_callback(update, context)


async def shift_custom_reason_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        return await handle_universal_callback(update, context)
    if not update.message or not update.message.text:
        return SHIFT_CUSTOM_REASON

    rsn = update.message.text.strip()
    clean_rsn = re.sub(r"[^a-zA-Z0-9\s,\-\.\(\)\/\&\:\;\']", " ", rsn)
    clean_rsn = re.sub(r"\s+", " ", clean_rsn).strip()
    if not clean_rsn:
        clean_rsn = "Shift Adjustment"
    context.user_data.setdefault("shift_data", {})["reason"] = clean_rsn
    return await shift_show_confirmation(update.message, context)


async def shift_show_confirmation(target, context: ContextTypes.DEFAULT_TYPE):
    s = context.user_data.get("shift_data", {})
    frm = s.get("from_date", "")
    to = s.get("to_date", frm)
    date_disp = frm if frm == to else f"{frm} to {to}"

    cur_disp = get_shift_display(s.get("current_shift", "T2"))
    new_disp = get_shift_display(s.get("new_shift", "T12"))
    director_disp = s.get("director_remark") or "*(Empty - for physical signature)*"

    summary = (
        "📋 **Shift Change Application Summary**\n\n"
        f"• **Faculty:** {s.get('emp_name')} (`{s.get('emp_code')}`)\n"
        f"• **Department:** {s.get('department')} | **Institute:** {s.get('institute')}\n"
        f"• **Designation:** {s.get('designation')}\n"
        f"• **Shift Date:** {date_disp}\n"
        f"• **Current Shift:** {cur_disp}\n"
        f"• **New Shift:** **{new_disp}**\n"
        f"• **Reason:** {s.get('reason')}\n"
        f"• **Director's Remark:** {director_disp}\n\n"
        "Click below to generate the **official, 100% compliant company PDF**:"
    )
    kb = [
        [InlineKeyboardButton("📄 Confirm & Generate PDF", callback_data="SHIFT_GEN_PDF")],
        [InlineKeyboardButton("🔙 Change Reason", callback_data="SHIFT_BACK_TO_REASON")],
        [InlineKeyboardButton("❌ Cancel", callback_data="CANCEL_SHIFT")]
    ]
    markup = InlineKeyboardMarkup(kb)
    await safe_edit_text(target, summary, reply_markup=markup)
    return SHIFT_CONFIRM


async def shift_confirm_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()

    s = context.user_data.get("shift_data", {})
    if not s or not s.get("new_shift"):
        if query:
            await query.message.reply_text("⚠️ Shift change details missing. Please use /shift to start.")
        return ConversationHandler.END

    if query:
        await safe_edit_text(query, "⏳ *Generating official Shift Change PDF... Please wait.*")

    try:
        pdf_path, pdf_filename = generate_shift_change_pdf(s)
        context.user_data["shift_pdf_path"] = pdf_path
        context.user_data["shift_pdf_filename"] = pdf_filename

        caption = (
            f"📄 **Official Shift Change Application**\n\n"
            f"• **File:** `{pdf_filename}`\n"
            f"• **Faculty:** {s.get('emp_name')}\n"
            f"• **Date:** {s.get('from_date')}\n"
            f"• **New Shift:** {get_shift_display(s.get('new_shift'))}\n\n"
            f"✅ Exact company format applied with 100% compliance."
        )

        chat_id = update.effective_chat.id
        with open(pdf_path, "rb") as f:
            await context.bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=pdf_filename,
                caption=caption,
                parse_mode="Markdown"
            )

        prompt = (
            "✅ **PDF Generated & Delivered!**\n\n"
            f"Filename: `{pdf_filename}`\n\n"
            "**How would you like to apply this shift change?**\n"
            "• **🚀 Apply on ARS Portal Automatically**: Submits shift exception directly to portal.\n"
            "• **📝 Manual Physical Submission**: Print the PDF, sign, and submit to HOD/Director."
        )
        kb = [
            [InlineKeyboardButton("🚀 Apply on Portal Automatically", callback_data="SHIFT_SUBMIT:AUTO")],
            [InlineKeyboardButton("📝 Manual Submission (PDF)", callback_data="SHIFT_SUBMIT:MANUAL")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="CMD_WELCOME")]
        ]
        await context.bot.send_message(
            chat_id=chat_id,
            text=prompt,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return SHIFT_SUBMIT
    except Exception as e:
        err_msg = f"❌ Error generating shift change PDF: {str(e)}"
        if query:
            await query.message.reply_text(err_msg)
        else:
            await update.effective_message.reply_text(err_msg)
        return ConversationHandler.END


async def shift_submission_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()
    data = query.data

    s = context.user_data.get("shift_data", {})
    filename = context.user_data.get("shift_pdf_filename", "Shift Change PDF")

    if data == "SHIFT_SUBMIT:AUTO":
        await safe_edit_text(query, "⏳ *Connecting to ARS Portal and applying shift change...*")

        emp_code = s.get("emp_code")
        password = s.get("password")
        if not emp_code or not password:
            return await return_to_home_screen(
                query,
                context,
                prefix_msg="❌ **Portal Credentials Missing:** Please link your ARS portal credentials in /profile first."
            )

        portal = LeavePortalAPI(username=emp_code, password=password)
        login_succ, login_msg = portal.login()

        if not login_succ:
            await query.message.reply_text(
                f"❌ **Portal Login Failed:** {login_msg}\n\n"
                "Your generated PDF is still valid. You can print and submit it manually, or check your password in /profile."
            )
            return await return_to_home_screen(query.message, context)

        shift_cd = s.get("new_shift")
        frm_dt = s.get("from_date")
        to_dt = s.get("to_date") or frm_dt
        reason = s.get("reason", "Shift Change")

        dry_run_shift = os.getenv("DRY_RUN_SHIFT", "true").strip().lower() in ("1", "true", "yes")
        succ, msg = portal.apply_shift_change(shift_cd, frm_dt, to_dt, reason, dry_run=dry_run_shift)
        if succ:
            log_application_to_db({
                "emp_code": emp_code,
                "faculty_name": s.get("emp_name", "Faculty"),
                "category": "SHIFT",
                "new_shift": shift_cd,
                "from_date": frm_dt,
                "to_date": to_dt,
                "reason": reason,
                "submitted_at": get_ist_now().strftime("%d/%m/%Y %I:%M %p"),
                "status": "✅ Applied on Portal",
                "portal_verified": True
            })
            success_text = (
                "🎉 **Shift Change Applied Successfully on ARS Portal!**\n\n"
                f"• **Portal Status:** {msg}\n"
                f"• **New Shift:** {get_shift_display(shift_cd)}\n"
                f"• **Date:** {frm_dt} to {to_dt}\n"
                f"• **Saved PDF:** `{filename}`\n\n"
                "Please print your generated PDF for physical HOD & Director signatures."
            )
            await query.message.reply_text(success_text, parse_mode="Markdown")
            return await return_to_home_screen(query.message, context)
        else:
            err_text = (
                f"⚠️ **Portal Submission Notice:**\n{msg}\n\n"
                "Your generated PDF is ready. You can submit it physically to the department."
            )
            await query.message.reply_text(err_text, parse_mode="Markdown")
            return await return_to_home_screen(query.message, context)

    elif data == "SHIFT_SUBMIT:MANUAL":
        log_application_to_db({
            "emp_code": s.get("emp_code", ""),
            "faculty_name": s.get("emp_name", "Faculty"),
            "category": "SHIFT",
            "new_shift": s.get("new_shift", "Shift Change"),
            "from_date": s.get("from_date", ""),
            "to_date": s.get("to_date") or s.get("from_date", ""),
            "reason": s.get("reason", "Shift Change"),
            "submitted_at": get_ist_now().strftime("%d/%m/%Y %I:%M %p"),
            "status": "📄 PDF Generated (Manual)",
            "portal_verified": False
        })
        manual_text = (
            "📝 **Manual Submission Selected**\n\n"
            f"Your Shift Change Application has been saved as `{filename}`.\n\n"
            "Next Steps:\n"
            "1. Print the downloaded PDF document.\n"
            "2. Sign under **Signature of Employee**.\n"
            "3. Obtain signatures from **HOD** and **Director**.\n"
            "4. Submit the sheet to the administrative office."
        )
        await query.message.reply_text(manual_text, parse_mode="Markdown")
        return await return_to_home_screen(query.message, context)

    return await handle_universal_callback(update, context)


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catches uncaught exceptions to ensure Telegram never hangs or freezes."""
    import traceback
    err_str = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    print(f"⚠️ [Telegram Error Handler] Exception occurred:\n{err_str}")
    if isinstance(update, Update):
        if update.callback_query:
            try:
                await update.callback_query.answer("⚠️ Session updated. Please tap your option again.", show_alert=False)
            except Exception:
                pass
        elif update.effective_message:
            try:
                await update.effective_message.reply_text("⚠️ An error occurred. You can use /apply or /balance at any time.")
            except Exception:
                pass


# ==========================================
# 7B. STANDALONE LECTURE ADJUSTMENT CHECKER (/adjust, /checkload, /load)
# ==========================================

def _parse_input_date_or_day(raw_text: str) -> tuple[str, str, str]:
    """
    Parses date or day of week into (date_str_dmy, day_code, day_name).
    Returns (None, None, None) if completely unrecognizable.
    """
    if not raw_text:
        return None, None, None

    from timetable_engine import WEEKDAY_MAP, DAY_NAME_TO_CODE

    text = raw_text.strip().upper()

    # Relative words in IST
    if text == "TODAY":
        d = get_ist_today()
        day_code = WEEKDAY_MAP.get(d.weekday(), "MON")
        day_name = d.strftime("%A")
        return d.strftime("%d/%m/%Y"), day_code, day_name
    elif text == "TOMORROW":
        d = get_ist_today() + timedelta(days=1)
        day_code = WEEKDAY_MAP.get(d.weekday(), "MON")
        day_name = d.strftime("%A")
        return d.strftime("%d/%m/%Y"), day_code, day_name
    elif text in ["DAY AFTER", "DAY_AFTER", "DAY AFTER TOMORROW"]:
        d = get_ist_today() + timedelta(days=2)
        day_code = WEEKDAY_MAP.get(d.weekday(), "MON")
        day_name = d.strftime("%A")
        return d.strftime("%d/%m/%Y"), day_code, day_name

    # Day of week name (e.g. MONDAY, MON)
    if text in DAY_NAME_TO_CODE:
        code = DAY_NAME_TO_CODE[text]
        names = {
            "MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday",
            "THU": "Thursday", "FRI": "Friday", "SAT": "Saturday", "SUN": "Sunday"
        }
        # Find closest upcoming date matching this weekday in IST
        d = get_ist_today()
        for i in range(7):
            cand = d + timedelta(days=i)
            if WEEKDAY_MAP.get(cand.weekday()) == code:
                return cand.strftime("%d/%m/%Y"), code, names.get(code, code)
        return None, code, names.get(code, code)

    # Standard date formats
    for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%d/%m/%y", "%d-%m-%y"]:
        try:
            dt = datetime.strptime(raw_text.strip(), fmt)
            code = WEEKDAY_MAP.get(dt.weekday(), "MON")
            return dt.strftime("%d/%m/%Y"), code, dt.strftime("%A")
        except ValueError:
            pass

    return None, None, None


async def check_load_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Entry point for /adjust, /checkload, /load commands or CMD_CHECK_LOAD button.
    Allows checking possible lecture adjustments of ANY faculty on ANY day.
    Supports command arguments: e.g. `/adjust MDP 14/09/2026` or `/adjust IRS Monday` or `/adjust PDB`.
    """
    user_id = update.effective_user.id
    query = update.callback_query
    if query:
        await query.answer()

    from timetable_engine import get_timetable_engine
    engine = get_timetable_engine()
    engine.ensure_up_to_date()

    context.user_data["check_load"] = {
        "fac_initial": None,
        "fac_name": None,
        "dept": None,
        "date_str": None,
        "day_code": None,
        "day_name": None,
        "max_div": 2,
        "max_subject_lectures": 2,
        "allow_merged": False,
        "disturb_other_department": False,
        "include_cascades": True,
        "duties": [],
        "result": None,
        "selected_plan_idx": 0,
        "view_mode": "plan",
    }
    cdata = context.user_data["check_load"]

    # Check command arguments if passed
    args = context.args or []
    if args:
        # Arg 0: Faculty
        arg_fac = args[0].strip()
        resolved_init = engine.resolve_faculty_initials(arg_fac)
        if resolved_init:
            info = engine.get_faculty_info(resolved_init)
            cdata["fac_initial"] = resolved_init
            cdata["fac_name"] = info.get("name", resolved_init)
            cdata["dept"] = info.get("dept", "")

        # Arg 1: Date or Day (if passed)
        if len(args) > 1:
            arg_date = " ".join(args[1:]).strip()
            d_str, d_code, d_name = _parse_input_date_or_day(arg_date)
            if d_str or d_code:
                cdata["date_str"] = d_str
                cdata["day_code"] = d_code
                cdata["day_name"] = d_name

        # If both faculty and date were passed via arguments
        if cdata["fac_initial"] and (cdata["date_str"] or cdata["day_code"]):
            duties = engine.get_faculty_duties_for_date(cdata["fac_initial"], cdata["date_str"] or cdata["day_code"])
            cdata["duties"] = duties
            if not duties:
                date_disp = cdata["date_str"] or cdata["day_name"] or "Selected Date"
                text = (
                    f"ℹ️ **No Scheduled Duties Found**\n\n"
                    f"• **Faculty:** {cdata['fac_name']} (`{cdata['fac_initial']}`)\n"
                    f"• **Date / Day:** `{date_disp}` ({cdata.get('day_name', '')})\n\n"
                    f"This faculty has no scheduled teaching load (lectures/labs) on this day in the master timetable."
                )
                kb = [
                    [InlineKeyboardButton("📅 Pick Another Date", callback_data="CHK_CHANGE_DATE")],
                    [InlineKeyboardButton("👤 Check Another Faculty", callback_data="CHK_CHANGE_FACULTY")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="CMD_WELCOME")],
                ]
                await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
                return CHECK_LOAD_VIEW
            else:
                return await prompt_check_load_max_div(update.effective_message, context)

        # If only faculty was passed
        if cdata["fac_initial"]:
            return await prompt_check_load_date(update.effective_message, context)

    # If no arguments or from callback, prompt for Faculty
    return await prompt_check_load_faculty(query or update.effective_message, context)


async def prompt_check_load_faculty(target, context: ContextTypes.DEFAULT_TYPE):
    """Displays the interactive faculty selection screen."""
    from timetable_engine import get_timetable_engine
    engine = get_timetable_engine()
    engine.ensure_up_to_date()

    user_id = target.chat_id if hasattr(target, "chat_id") else target.from_user.id if hasattr(target, "from_user") else 0
    faculty = get_faculty(user_id)

    my_init = ""
    if faculty:
        my_init = (faculty.get("short_name") or faculty.get("initials") or "").strip().upper()
        if not my_init:
            my_init = engine.resolve_faculty_initials(faculty.get("name"), faculty.get("emp_code"))

    kb = []
    if my_init:
        kb.append([InlineKeyboardButton(f"👤 Check My Load ({my_init})", callback_data=f"CHK_FAC:{my_init}")])

    # Common department faculties
    common_facs = ["MDP", "IRS", "PDB", "KGB", "NND", "HDS"]
    row = []
    for f_init in common_facs:
        if f_init != my_init:
            row.append(InlineKeyboardButton(f"{f_init}", callback_data=f"CHK_FAC:{f_init}"))
            if len(row) == 3:
                kb.append(row)
                row = []
    if row:
        kb.append(row)

    kb.append([InlineKeyboardButton("✍️ Type Faculty Name / Initials", callback_data="CHK_FAC_TYPE")])
    kb.append([InlineKeyboardButton("🏠 Main Menu", callback_data="CMD_WELCOME")])

    text = (
        "🔍 **Lecture Adjustment Checker**\n\n"
        "Check possible teaching load adjustments for **any faculty** on **any day** "
        "with 100% adherence to selected constraints.\n\n"
        "Whose lecture adjustments would you like to check?\n"
        "*(Tap a faculty below or type their initials/full name)*"
    )
    if isinstance(target, CallbackQuery):
        await safe_edit_text(target, text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return CHECK_LOAD_FACULTY


async def check_load_faculty_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles faculty selection callback or typed text."""
    query = update.callback_query
    from timetable_engine import get_timetable_engine
    engine = get_timetable_engine()
    engine.ensure_up_to_date()

    cdata = context.user_data.setdefault("check_load", {})

    if query:
        await query.answer()
        data = query.data
        if data == "CMD_WELCOME":
            return await return_to_home_screen(query, context)
        if data == "CHK_CHANGE_FACULTY":
            return await prompt_check_load_faculty(query, context)
        if data == "CHK_FAC_TYPE":
            msg = (
                "✍️ **Enter Faculty Initials or Full Name:**\n\n"
                "Please reply with the faculty short name (e.g. `MDP`, `IRS`, `PDB`, `HDS`) "
                "or full name (e.g. `Milan Patel`, `Irmi Saiyed`):"
            )
            kb = [[InlineKeyboardButton("🔙 Back to Faculty List", callback_data="CHK_CHANGE_FACULTY")]]
            await safe_edit_text(query, msg, reply_markup=InlineKeyboardMarkup(kb))
            return CHECK_LOAD_FACULTY
        if data.startswith("CHK_FAC:"):
            fac_initial = data.split(":")[1].strip().upper()
            info = engine.get_faculty_info(fac_initial)
            cdata["fac_initial"] = fac_initial
            cdata["fac_name"] = info.get("name", fac_initial)
            cdata["dept"] = info.get("dept", "")
            return await prompt_check_load_date(query, context)

    # Text message received
    if update.message and update.message.text:
        raw_text = update.message.text.strip()
        resolved = engine.resolve_faculty_initials(raw_text)
        if not resolved:
            kb = [
                [InlineKeyboardButton("🔙 Back to Faculty Menu", callback_data="CHK_CHANGE_FACULTY")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="CMD_WELCOME")]
            ]
            await update.message.reply_text(
                f"⚠️ Could not find faculty matching **'{raw_text}'** in the timetable or DR roster.\n\n"
                f"Please type valid initials (e.g. `MDP`, `IRS`, `PDB`, `KGB`, `HDS`, `DAM`) or full name:",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown"
            )
            return CHECK_LOAD_FACULTY

        info = engine.get_faculty_info(resolved)
        cdata["fac_initial"] = resolved
        cdata["fac_name"] = info.get("name", resolved)
        cdata["dept"] = info.get("dept", "")
        return await prompt_check_load_date(update.message, context)

    return CHECK_LOAD_FACULTY


async def prompt_check_load_date(target, context: ContextTypes.DEFAULT_TYPE):
    """Displays the date selection screen in IST."""
    cdata = context.user_data.get("check_load", {})
    fac_initial = cdata.get("fac_initial", "FACULTY")
    fac_name = cdata.get("fac_name", fac_initial)
    dept = cdata.get("dept", "")
    dept_disp = f" | Dept: `{dept}`" if dept else ""

    t0 = get_ist_now()
    t1 = t0 + timedelta(days=1)
    t2 = t0 + timedelta(days=2)

    kb = [
        [
            InlineKeyboardButton(f"📅 Today ({t0.strftime('%d/%m')})", callback_data=f"CHK_DATE:{t0.strftime('%d/%m/%Y')}"),
            InlineKeyboardButton(f"📅 Tomorrow ({t1.strftime('%d/%m')})", callback_data=f"CHK_DATE:{t1.strftime('%d/%m/%Y')}"),
            InlineKeyboardButton(f"📅 Day After ({t2.strftime('%d/%m')})", callback_data=f"CHK_DATE:{t2.strftime('%d/%m/%Y')}"),
        ],
        [
            InlineKeyboardButton("📅 Monday", callback_data="CHK_DATE:MON"),
            InlineKeyboardButton("📅 Tuesday", callback_data="CHK_DATE:TUE"),
            InlineKeyboardButton("📅 Wednesday", callback_data="CHK_DATE:WED"),
        ],
        [
            InlineKeyboardButton("📅 Thursday", callback_data="CHK_DATE:THU"),
            InlineKeyboardButton("📅 Friday", callback_data="CHK_DATE:FRI"),
            InlineKeyboardButton("📅 Saturday", callback_data="CHK_DATE:SAT"),
        ],
        [InlineKeyboardButton("✍️ Type Custom Date (DD/MM/YYYY)", callback_data="CHK_DATE_TYPE")],
        [
            InlineKeyboardButton("🔙 Back to Faculty", callback_data="CHK_CHANGE_FACULTY"),
            InlineKeyboardButton("🏠 Main Menu", callback_data="CMD_WELCOME")
        ]
    ]

    text = (
        f"📅 **Select Date to Check Load:**\n\n"
        f"• **Faculty:** **{fac_name}** (`{fac_initial}`){dept_disp}\n\n"
        f"Choose a date or day below, or type any date in `DD/MM/YYYY` format:"
    )
    if isinstance(target, CallbackQuery):
        await safe_edit_text(target, text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return CHECK_LOAD_DATE


async def check_load_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles date selection callback or custom text input."""
    query = update.callback_query
    cdata = context.user_data.setdefault("check_load", {})
    from timetable_engine import get_timetable_engine
    engine = get_timetable_engine()
    engine.ensure_up_to_date()

    if query:
        await query.answer()
        data = query.data
        if data == "CMD_WELCOME":
            return await return_to_home_screen(query, context)
        if data == "CHK_CHANGE_FACULTY":
            return await prompt_check_load_faculty(query, context)
        if data == "CHK_CHANGE_DATE":
            return await prompt_check_load_date(query, context)
        if data == "CHK_DATE_TYPE":
            prompt = (
                "✍️ **Enter Date or Day of Week**\n\n"
                "Please type a date (e.g. `14/09/2026`, `tomorrow`) or day of week (e.g. `Monday`):"
            )
            kb = [[InlineKeyboardButton("🔙 Back to Date Selection", callback_data="CHK_CHANGE_DATE")]]
            await safe_edit_text(query, prompt, reply_markup=InlineKeyboardMarkup(kb))
            return CHECK_LOAD_DATE
        if data.startswith("CHK_DATE:"):
            token = data.split(":")[1].strip()
            d_str, d_code, d_name = _parse_input_date_or_day(token)
            cdata["date_str"] = d_str
            cdata["day_code"] = d_code
            cdata["day_name"] = d_name

    elif update.message and update.message.text:
        token = update.message.text.strip()
        d_str, d_code, d_name = _parse_input_date_or_day(token)
        if not d_str and not d_code:
            kb = [
                [InlineKeyboardButton("🔙 Pick from Date Menu", callback_data="CHK_CHANGE_DATE")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="CMD_WELCOME")]
            ]
            await update.message.reply_text(
                "⚠️ Invalid date format. Please enter as `DD/MM/YYYY` (e.g. `14/09/2026`) or weekday (e.g. `Monday`):",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown"
            )
            return CHECK_LOAD_DATE
        cdata["date_str"] = d_str
        cdata["day_code"] = d_code
        cdata["day_name"] = d_name

    fac_initial = cdata.get("fac_initial", "FACULTY")
    duties = engine.get_faculty_duties_for_date(fac_initial, cdata.get("date_str") or cdata.get("day_code"))
    cdata["duties"] = duties

    target = query if query else update.message
    if not duties:
        date_disp = cdata.get("date_str") or cdata.get("day_name") or "Selected Date"
        text = (
            f"ℹ️ **No Scheduled Duties Found**\n\n"
            f"• **Faculty:** {cdata.get('fac_name', fac_initial)} (`{fac_initial}`)\n"
            f"• **Date / Day:** `{date_disp}` ({cdata.get('day_name', '')})\n\n"
            f"This faculty has no scheduled teaching load (lectures/labs) on this day in the master timetable."
        )
        kb = [
            [InlineKeyboardButton("📅 Pick Another Date", callback_data="CHK_CHANGE_DATE")],
            [InlineKeyboardButton("👤 Check Another Faculty", callback_data="CHK_CHANGE_FACULTY")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="CMD_WELCOME")],
        ]
        if isinstance(target, CallbackQuery):
            await safe_edit_text(target, text, reply_markup=InlineKeyboardMarkup(kb))
        else:
            await target.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return CHECK_LOAD_VIEW

    return await prompt_check_load_max_div(target, context)


def format_check_load_config_screen(cdata: dict):
    """Renders the unified 1-screen constraint configurator for /checkload and /adjust."""
    fac_initial = cdata.get("fac_initial", "FACULTY")
    fac_name = cdata.get("fac_name", fac_initial)
    date_disp = cdata.get("date_str") or cdata.get("day_name") or "Date"
    day_name = cdata.get("day_name", "")
    duties = cdata.get("duties", [])
    max_div = cdata.get("max_div", 2)
    max_subject = cdata.get("max_subject_lectures", max_div)
    allow_merged = cdata.get("allow_merged", False)
    disturb_other_department = cdata.get("disturb_other_department", False)
    min_disturb = cdata.get("prefer_min_disturbance", False)

    duties_preview = "\n".join([
        f"• **Lec {d['lec_no']}** ({d.get('time', '')}): Div `{d.get('division', '')}` | {d.get('subject', '')} (Room {d.get('room', '')})"
        for d in duties
    ])

    max_disp = "No Limit" if max_subject == 999 else f"{max_subject} lectures of any one subject per division"
    merged_disp = "✅ Yes (Merged Allowed)" if allow_merged else "❌ No (Free Only)"
    dept_disp = "✅ Yes (Other Dept Allowed)" if disturb_other_department else "❌ No (Same Dept Only)"

    prompt = (
        f"📚 **Load Adjustment for {fac_name} (`{fac_initial}`):**\n"
        f"📅 Date: `{date_disp}` ({day_name})\n"
        f"📋 Found **{len(duties)}** scheduled duties:\n"
        f"{duties_preview}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ **Configure Constraints (optional):**\n\n"
        f"• **Max lectures of any one subject in a division:** `{max_disp}`\n"
        f"• **Merged Classes:** `{merged_disp}`\n"
        f"• **Other Department Disturbance:** `{dept_disp}`\n"
        f"• **Cascades:** `ON` (all valid chains are searched)\n\n"
        f"Default: **Max 2**, merged classes not allowed, same department only.\n"
        f"Subject limit choices: **Max 2**, **Max 3**, or **No Limit**."
    )

    btn_m2 = f"{'✅ ' if max_subject == 2 else ''}2️⃣ Max 2"
    btn_m3 = f"{'✅ ' if max_subject == 3 else ''}3️⃣ Max 3"
    btn_m9 = f"{'✅ ' if max_subject == 999 else ''}♾️ No Limit"

    btn_mno = f"{'✅ ' if not allow_merged else ''}❌ Free Only"
    btn_myes = f"{'✅ ' if allow_merged else ''}🔄 Merged Allowed"

    kb = [
        [
            InlineKeyboardButton(btn_m2, callback_data="CHK_SET_MAX:2"),
            InlineKeyboardButton(btn_m3, callback_data="CHK_SET_MAX:3"),
            InlineKeyboardButton(btn_m9, callback_data="CHK_SET_MAX:999"),
        ],
        [
            InlineKeyboardButton(btn_mno, callback_data="CHK_SET_MERGED:NO"),
            InlineKeyboardButton(btn_myes, callback_data="CHK_SET_MERGED:YES"),
        ],
        [
            InlineKeyboardButton(
                f"{'✅ ' if not disturb_other_department else ''}❌ Same Dept Only",
                callback_data="CHK_SET_DEPT:NO",
            ),
            InlineKeyboardButton(
                f"{'✅ ' if disturb_other_department else ''}🌐 Other Dept Allowed",
                callback_data="CHK_SET_DEPT:YES",
            ),
        ],
        [
            InlineKeyboardButton("🚀 Find Load Adjustments", callback_data="CHK_RUN:CUSTOM"),
        ],
        [
            InlineKeyboardButton("📅 Change Date", callback_data="CHK_CHANGE_DATE"),
            InlineKeyboardButton("👤 Change Faculty", callback_data="CHK_CHANGE_FACULTY"),
        ],
        [
            InlineKeyboardButton("🏠 Main Menu", callback_data="CMD_WELCOME")
        ]
    ]
    return prompt, InlineKeyboardMarkup(kb)


async def prompt_check_load_max_div(target, context: ContextTypes.DEFAULT_TYPE):
    """Displays Unified 1-Screen Constraints & Strategy Configurator."""
    cdata = context.user_data.setdefault("check_load", {})
    cdata.setdefault("max_subject_lectures", cdata.get("max_div", 2))
    cdata.setdefault("max_div", cdata.get("max_subject_lectures", 2))
    cdata.setdefault("allow_merged", False)
    cdata.setdefault("disturb_other_department", False)
    cdata.setdefault("include_cascades", True)
    cdata.setdefault("prefer_min_disturbance", False)

    prompt, markup = format_check_load_config_screen(cdata)
    if isinstance(target, CallbackQuery):
        await safe_edit_text(target, prompt, reply_markup=markup)
    else:
        await target.reply_text(prompt, reply_markup=markup, parse_mode="Markdown")
    return CHECK_LOAD_MAX_DIV


async def check_load_max_div_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles 1-screen constraint updates and strategy execution."""
    query = update.callback_query
    if not query:
        return CHECK_LOAD_MAX_DIV
    await query.answer()
    data = query.data
    cdata = context.user_data.setdefault("check_load", {})

    if data == "CMD_WELCOME":
        return await return_to_home_screen(query, context)
    if data == "CHK_CHANGE_DATE":
        return await prompt_check_load_date(query, context)
    if data == "CHK_CHANGE_FACULTY":
        return await prompt_check_load_faculty(query, context)

    # In-screen toggle: Max lectures in division
    if data.startswith("CHK_SET_MAX:"):
        cdata["max_subject_lectures"] = int(data.split(":")[1])
        cdata["max_div"] = cdata["max_subject_lectures"]
        prompt, markup = format_check_load_config_screen(cdata)
        await safe_edit_text(query, prompt, reply_markup=markup)
        return CHECK_LOAD_MAX_DIV

    # In-screen toggle: Merged allowed or not
    if data.startswith("CHK_SET_MERGED:"):
        cdata["allow_merged"] = (data == "CHK_SET_MERGED:YES")
        prompt, markup = format_check_load_config_screen(cdata)
        await safe_edit_text(query, prompt, reply_markup=markup)
        return CHECK_LOAD_MAX_DIV

    if data.startswith("CHK_SET_DEPT:"):
        cdata["disturb_other_department"] = data == "CHK_SET_DEPT:YES"
        prompt, markup = format_check_load_config_screen(cdata)
        await safe_edit_text(query, prompt, reply_markup=markup)
        return CHECK_LOAD_MAX_DIV

    # 1-Click Minimum Disturbance Execution
    if data in ["CHK_RUN:MIN_DISTURB", "PRESET_LOAD:MIN_DISTURB"]:
        cdata["prefer_min_disturbance"] = True
        cdata["include_cascades"] = True
        cdata.setdefault("max_subject_lectures", 2)
        cdata.setdefault("max_div", cdata.get("max_subject_lectures", 2))
        cdata.setdefault("allow_merged", False)
        return await run_and_display_check_load(query, context)

    # Legacy quick-check callback: use the documented default constraints.
    if data == "CHK_QUICK":
        cdata["max_subject_lectures"] = 2
        cdata["max_div"] = 2
        cdata["allow_merged"] = False
        cdata["include_cascades"] = True
        cdata["prefer_min_disturbance"] = False
        return await run_and_display_check_load(query, context)

    # Find Load Adjustments with current screen settings
    if data in ["CHK_RUN:CUSTOM", "PRESET_LOAD:RUN_CUSTOM"]:
        cdata["include_cascades"] = True
        return await run_and_display_check_load(query, context)

    # Legacy support if CHK_MAX: clicked
    if data.startswith("CHK_MAX:"):
        cdata["max_subject_lectures"] = int(data.split(":")[1])
        cdata["max_div"] = cdata["max_subject_lectures"]
        prompt, markup = format_check_load_config_screen(cdata)
        await safe_edit_text(query, prompt, reply_markup=markup)
        return CHECK_LOAD_MAX_DIV

    return CHECK_LOAD_MAX_DIV


async def check_load_merged_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles Constraint 2 choice (legacy support)."""
    query = update.callback_query
    if not query:
        return CHECK_LOAD_MERGED
    await query.answer()
    data = query.data
    cdata = context.user_data.setdefault("check_load", {})

    if data == "CMD_WELCOME":
        return await return_to_home_screen(query, context)
    if data == "CHK_BACK_MAX":
        return await prompt_check_load_max_div(query, context)

    if data.startswith("CHK_MERGED:"):
        cdata["allow_merged"] = (data == "CHK_MERGED:YES")
        return await run_and_display_check_load(query, context)

    return CHECK_LOAD_MERGED


async def check_load_cascade_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles Constraint 3 choice (legacy support)."""
    query = update.callback_query
    if not query:
        return CHECK_LOAD_CASCADE
    await query.answer()
    data = query.data
    cdata = context.user_data.setdefault("check_load", {})

    if data == "CMD_WELCOME":
        return await return_to_home_screen(query, context)
    if data == "CHK_BACK_MERGED":
        return await prompt_check_load_max_div(query, context)

    if data.startswith("CHK_CASC:"):
        cdata["include_cascades"] = (data == "CHK_CASC:YES")
        return await run_and_display_check_load(query, context)

    return CHECK_LOAD_CASCADE


async def run_and_display_check_load(query, context: ContextTypes.DEFAULT_TYPE):
    """Executes suggest_load_adjustments and renders the results."""
    from timetable_engine import get_timetable_engine
    engine = get_timetable_engine()
    engine.ensure_up_to_date()

    cdata = context.user_data.setdefault("check_load", {})
    duties = cdata.get("duties", [])
    max_div = cdata.get("max_subject_lectures", cdata.get("max_div", 2))
    allow_merged = cdata.get("allow_merged", False)
    include_cascades = cdata.get("include_cascades", True)
    prefer_min_disturbance = cdata.get("prefer_min_disturbance", False)
    disturb_other_department = cdata.get("disturb_other_department", False)

    result = engine.suggest_load_adjustments(
        duties=duties,
        max_lectures_per_div=max_div,
        max_lectures_per_subject=max_div,
        allow_merged=allow_merged,
        include_cascades=include_cascades,
        prefer_min_disturbance=prefer_min_disturbance,
        disturb_other_department=disturb_other_department,
    )
    cdata["result"] = result
    cdata["selected_plan_idx"] = 0
    cdata["view_mode"] = "plan"

    return await display_check_load_results(query, context, selected_plan_idx=0, show_whatsapp=False)


async def display_check_load_results(query, context: ContextTypes.DEFAULT_TYPE, selected_plan_idx: int = 0, show_whatsapp: bool = False):
    """Renders the detailed breakdown or student WhatsApp notices for the selected plan."""
    cdata = context.user_data.get("check_load", {})
    result = cdata.get("result", {})
    plans = result.get("plans", []) if result else []
    fac_initial = cdata.get("fac_initial", "FACULTY")
    fac_name = cdata.get("fac_name", fac_initial)
    dept = cdata.get("dept", "")
    date_disp = cdata.get("date_str") or cdata.get("day_name") or "Date"
    day_name = cdata.get("day_name", "")
    duties = cdata.get("duties", [])
    max_div = cdata.get("max_subject_lectures", cdata.get("max_div", 2))
    max_desc = "No Limit" if max_div == 999 else f"Max {max_div}/Subject/Div"
    merged_desc = "Merged Allowed" if cdata.get("allow_merged") else "Free Only"
    casc_desc = "Cascades ON" if cdata.get("include_cascades") else "Cascades OFF"
    dept_desc = "Other Dept Allowed" if cdata.get("disturb_other_department") else "Same Dept Only"

    if not plans:
        text = (
            f"⚠️ **No Complete Adjustment Plan Found**\n\n"
            f"• **Faculty:** **{fac_name}** (`{fac_initial}`)\n"
            f"• **Date / Day:** `{date_disp}` ({day_name}) | **{len(duties)}** Duties\n"
            f"• **Active Constraints:** `{max_desc}` | `{merged_desc}` | `{casc_desc}`\n\n"
            f"No valid combination of whitelisted faculty satisfied all selected constraints for every slot.\n\n"
            f"💡 **Suggested Fixes:**\n"
            f"• Enable **Cascade Arrangements** to find multi-hop faculty swaps.\n"
            f"• Enable **Merged Lectures** if teachers have combined classes."
        )
        kb = []
        if not cdata.get("include_cascades"):
            kb.append([InlineKeyboardButton("🔗 Allow Cascades & Retry", callback_data="CHK_RETRY_CASCADE")])
        if not cdata.get("allow_merged"):
            kb.append([InlineKeyboardButton("🔄 Allow Merged & Retry", callback_data="CHK_RETRY_MERGED")])
        kb.append([InlineKeyboardButton("⚙️ Change Constraints", callback_data="CHK_CHANGE_CONSTRAINTS")])
        kb.append([
            InlineKeyboardButton("📅 Change Date", callback_data="CHK_CHANGE_DATE"),
            InlineKeyboardButton("👤 Change Faculty", callback_data="CHK_CHANGE_FACULTY")
        ])
        kb.append([InlineKeyboardButton("🏠 Main Menu", callback_data="CMD_WELCOME")])

        await safe_edit_text(query, text, reply_markup=InlineKeyboardMarkup(kb))
        return CHECK_LOAD_VIEW

    if selected_plan_idx >= len(plans):
        selected_plan_idx = 0
    cdata["selected_plan_idx"] = selected_plan_idx
    chosen_plan = plans[selected_plan_idx]

    # --- Mode 1: Copy-Ready WhatsApp Student Notices ---
    if show_whatsapp:
        wa_messages = []
        for adj in chosen_plan["adjustments"]:
            duty = adj.get("duty") or {}
            sub = adj.get("substitute") or {}
            sub_init = sub.get("initials") if isinstance(sub, dict) else str(sub)
            proxy_subj = sub.get("subject") if isinstance(sub, dict) else adj.get("subject", "")

            orig_subj = duty.get("subject", "")
            batch = duty.get("division", "")
            lec_no = duty.get("lec_no", 1)
            room = duty.get("room", "")

            wa_msg = (
                f"🔵 Lecture Adjustment Details:\n"
                f"Batch: {batch}\n"
                f"Date: {date_disp}\n"
                f"Day: {day_name}\n"
                f"Lecture No: {lec_no}\n"
                f"Subject as per TT: {orig_subj}({fac_initial})\n"
                f"Proxy Subject: {proxy_subj}({sub_init})\n"
                f"Room No:   {room}"
            )
            wa_messages.append(wa_msg)

            # Cascade chain messages
            chain_steps = sub.get("chain", []) if isinstance(sub, dict) else []
            for step in chain_steps:
                c_wa = (
                    f"🔵 Lecture Adjustment Details:\n"
                    f"Batch: {step.get('division', '')}\n"
                    f"Date: {date_disp}\n"
                    f"Day: {day_name}\n"
                    f"Lecture No: {step.get('lec_no', lec_no)}\n"
                    f"Subject as per TT: {step.get('relieved_subject', '')}({step.get('relieved', '')})\n"
                    f"Proxy Subject: {step.get('reliever_subject', '')}({step.get('reliever', '')})\n"
                    f"Room No:   {step.get('room', room)}"
                )
                wa_messages.append(c_wa)

        formatted_body = "\n\n━━━━━━━━━━━━━━━━━━━━━\n\n".join(wa_messages)
        full_text = (
            f"📱 **Student Group WhatsApp Messages ({chosen_plan['title']}):**\n\n"
            f"Copy and send each notice to the respective student class group:\n\n"
            f"```text\n{formatted_body}\n```"
        )
        kb = [
            [InlineKeyboardButton("🔙 Back to Adjustment Details", callback_data="CHK_HIDE_WA")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="CMD_WELCOME")]
        ]
        await safe_edit_text(query, full_text, reply_markup=InlineKeyboardMarkup(kb))
        return CHECK_LOAD_VIEW

    # --- Mode 2: Detailed Plan Breakdown ---
    lines = [
        f"🔍 **Lecture Adjustment Plans:**",
        f"👤 **Faculty:** **{fac_name}** (`{fac_initial}`) {f'| {dept}' if dept else ''}",
        f"📅 **Date:** `{date_disp}` ({day_name}) | **{len(duties)}** Duties",
        f"⚙️ **Constraints:** `{max_desc}` | `{merged_desc}` | `{dept_desc}` | `{casc_desc}`\n",
        f"━━━━━━━━━━━━━━━━━━━━━",
        f"📋 **{chosen_plan['title']}:**\n"
    ]

    for adj in chosen_plan["adjustments"]:
        duty = adj.get("duty") or {}
        sub = adj.get("substitute") or {}
        sub_init = sub.get("initials") if isinstance(sub, dict) else str(sub)
        sub_name = sub.get("name", sub_init) if isinstance(sub, dict) else sub_init
        proxy_subj = sub.get("subject") if isinstance(sub, dict) else adj.get("subject", "")
        status_icon = "🟢" if (isinstance(sub, dict) and sub.get("is_free")) else ("🔗" if (isinstance(sub, dict) and sub.get("is_cascade")) else "🔄")
        status_label = "Direct Free" if (isinstance(sub, dict) and sub.get("is_free")) else ("Cascade" if (isinstance(sub, dict) and sub.get("is_cascade")) else "Merged")

        lines.append(
            f"• **Lec {duty.get('lec_no')}** ({duty.get('time', '')}) [**Div {duty.get('division', '')}**] (Room {duty.get('room', '')}):\n"
            f"  Subject: `{duty.get('subject', '')}({fac_initial})`\n"
            f"  Proxy: **{proxy_subj}** by **{sub_name}** (`{sub_init}`) {status_icon} *({status_label})*"
        )
        chain = sub.get("chain", []) if isinstance(sub, dict) else []
        if chain:
            for step_idx, step in enumerate(chain, 1):
                lines.append(
                    f"  ↳ 🔗 *Cascade Step {step_idx}:* `{step.get('reliever')}` ({step.get('subject')}) "
                    f"relieves `{step.get('relieved')}` in **Div {step.get('division')}** (Room {step.get('room')})"
                )
        lines.append("")

    # Summary of other options
    if len(plans) > 1:
        lines.append("━━━━━━━━━━━━━━━━━━━━━")
        lines.append("📊 **Available Options:**")
        for p_i, p in enumerate(plans):
            marker = "👉 " if p_i == selected_plan_idx else "• "
            lines.append(f"{marker}**Option {p_i+1}:** {p['title']}")
        lines.append("")

    kb = []
    # Option buttons
    opt_row = []
    for p_i in range(len(plans)):
        star = "⭐ " if p_i == 0 else ""
        chk = "✅ " if p_i == selected_plan_idx else ""
        lbl = f"{chk}{star}Option {p_i+1}"
        opt_row.append(InlineKeyboardButton(lbl, callback_data=f"CHK_PLAN:{p_i}"))
    if opt_row:
        kb.append(opt_row)

    kb.append([InlineKeyboardButton("💬 Copy Student WhatsApp Messages", callback_data="CHK_SHOW_WA")])
    kb.append([
        InlineKeyboardButton("⚙️ Change Constraints", callback_data="CHK_CHANGE_CONSTRAINTS"),
        InlineKeyboardButton("📅 Change Date", callback_data="CHK_CHANGE_DATE")
    ])
    kb.append([
        InlineKeyboardButton("👤 Change Faculty", callback_data="CHK_CHANGE_FACULTY"),
        InlineKeyboardButton("🏠 Main Menu", callback_data="CMD_WELCOME")
    ])

    await safe_edit_text(query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))
    return CHECK_LOAD_VIEW


async def check_load_view_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles interactive button taps on the check load view screen."""
    query = update.callback_query
    if not query:
        return CHECK_LOAD_VIEW
    await query.answer()
    data = query.data
    cdata = context.user_data.setdefault("check_load", {})

    if data == "CMD_WELCOME":
        return await return_to_home_screen(query, context)
    if data == "CHK_CHANGE_FACULTY":
        return await prompt_check_load_faculty(query, context)
    if data == "CHK_CHANGE_DATE":
        return await prompt_check_load_date(query, context)
    if data == "CHK_CHANGE_CONSTRAINTS":
        return await prompt_check_load_max_div(query, context)
    if data == "CHK_RETRY_MERGED":
        cdata["allow_merged"] = True
        return await run_and_display_check_load(query, context)
    if data == "CHK_RETRY_CASCADE":
        cdata["include_cascades"] = True
        return await run_and_display_check_load(query, context)
    if data.startswith("CHK_PLAN:"):
        idx = int(data.split(":")[1])
        return await display_check_load_results(query, context, selected_plan_idx=idx, show_whatsapp=False)
    if data == "CHK_SHOW_WA":
        return await display_check_load_results(query, context, selected_plan_idx=cdata.get("selected_plan_idx", 0), show_whatsapp=True)
    if data == "CHK_HIDE_WA":
        return await display_check_load_results(query, context, selected_plan_idx=cdata.get("selected_plan_idx", 0), show_whatsapp=False)

    return await handle_universal_callback(update, context)


# ==========================================
# 8. TELEGRAM BOT THREAD RUNNER
# ==========================================
async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for /admin command or CMD_ADMIN callback."""
    user_id = update.effective_user.id
    query = update.callback_query
    if query:
        await query.answer()

    if user_id in AUTHENTICATED_ADMINS:
        target = query if query else update.message
        await show_admin_menu(target, context)
        return ADMIN_MENU

    msg = (
        "🔐 **Timetable Admin Authentication**\n\n"
        "Access to timetable management and `.xlsx` uploads is restricted.\n\n"
        "Please enter the **Admin Password** to continue:"
    )
    kb = [[InlineKeyboardButton("❌ Cancel", callback_data="ADMIN_CANCEL")]]
    if query:
        await safe_edit_text(query, msg, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return ADMIN_PASS_INPUT


async def admin_pass_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Validates the entered admin password."""
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    if text == ADMIN_PASSWORD:
        AUTHENTICATED_ADMINS.add(user_id)
        await update.message.reply_text(
            "✅ **Admin Access Granted!** Welcome to the Timetable Admin Panel.",
            parse_mode="Markdown"
        )
        await show_admin_menu(update.message, context)
        return ADMIN_MENU
    else:
        await update.message.reply_text(
            "❌ **Incorrect Admin Password!** Access denied.\n\n"
            "Use /admin to try again.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END


async def admin_cancel_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels admin authentication or operation."""
    query = update.callback_query
    if query:
        await query.answer()
        await safe_edit_text(query, "❌ Admin operation cancelled.")
    return ConversationHandler.END


async def show_admin_menu(target_obj, context=None):
    """Renders the main administrative control panel."""
    from timetable_engine import get_timetable_engine
    engine = get_timetable_engine()
    summary = engine.get_status()

    dept_lines = []
    for d in ["FY1", "FY2", "FY3", "FY4", "FY5"]:
        fname = summary["departments"].get(d)
        if fname:
            dept_lines.append(f"• **{d}:** `{fname}`")
        else:
            dept_lines.append(f"• **{d}:** *Not Loaded*")

    dr_fname = summary.get("dr_file") or "*Not Loaded*"

    text = (
        "🛠️ **Timetable Administration Panel**\n"
        "Manage First-Year timetables and Daily Internship DR rosters.\n\n"
        "📁 **Active Timetable Files:**\n"
        + "\n".join(dept_lines) + f"\n• **Daily Internship DR:** `{dr_fname}`\n\n"
        f"📊 **Current Engine Statistics:**\n"
        f"• Total Active Divisions: **{summary['total_divisions']}**\n"
        f"• Timetable Faculty Initials: **{summary['timetable_faculty_count']}**\n"
        f"• DR Faculty Profiles: **{summary['dr_faculty_count']}**\n"
        f"• Last Synced: `{summary.get('last_sync_time') or 'Never'}`\n\n"
        "Select an administrative action below:"
    )
    kb = [
        [InlineKeyboardButton("📤 Upload Timetable / DR File", callback_data="ADMIN_CHOOSE_UPLOAD")],
        [
            InlineKeyboardButton("🔄 Force Reload Engine", callback_data="ADMIN_FORCE_RELOAD"),
            InlineKeyboardButton("📊 Detailed Status", callback_data="ADMIN_VIEW_STATUS"),
        ],
        [
            InlineKeyboardButton("🔒 Logout Session", callback_data="ADMIN_LOGOUT"),
            InlineKeyboardButton("❌ Close Panel", callback_data="ADMIN_EXIT"),
        ]
    ]
    await safe_edit_text(target_obj, text, reply_markup=InlineKeyboardMarkup(kb))


async def admin_menu_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles admin dashboard button clicks."""
    query = update.callback_query
    if not query:
        return ADMIN_MENU
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if user_id not in AUTHENTICATED_ADMINS:
        await safe_edit_text(query, "⛔ Session expired or unauthorized. Please use /admin to log in.")
        return ConversationHandler.END

    if data == "ADMIN_CHOOSE_UPLOAD":
        kb = [
            [InlineKeyboardButton("📘 FY1 Timetable", callback_data="ADMIN_TARGET:FY1"), InlineKeyboardButton("📗 FY2 Timetable", callback_data="ADMIN_TARGET:FY2")],
            [InlineKeyboardButton("📙 FY3 Timetable", callback_data="ADMIN_TARGET:FY3"), InlineKeyboardButton("📕 FY4 Timetable", callback_data="ADMIN_TARGET:FY4")],
            [InlineKeyboardButton("📓 FY5 Timetable", callback_data="ADMIN_TARGET:FY5"), InlineKeyboardButton("📋 Daily Internship DR", callback_data="ADMIN_TARGET:DR")],
            [InlineKeyboardButton("⚡ Auto-detect by Filename", callback_data="ADMIN_TARGET:AUTO")],
            [InlineKeyboardButton("🔙 Back to Admin Menu", callback_data="ADMIN_BACK_MENU")],
        ]
        text = (
            "📤 **Select Timetable / Roster to Upload:**\n\n"
            "Choose which category to update:\n"
            "• **FY1 – FY5**: Department branch timetable spreadsheets\n"
            "• **Daily Internship DR**: Central faculty roster & internship sheets\n"
            "• **Auto-detect**: Infers department from uploaded filename"
        )
        await safe_edit_text(query, text, reply_markup=InlineKeyboardMarkup(kb))
        return ADMIN_MENU

    if data.startswith("ADMIN_TARGET:"):
        target = data.split(":", 1)[1]
        context.user_data["admin_upload_target"] = target
        labels = {
            "FY1": "FY1 Timetable",
            "FY2": "FY2 Timetable",
            "FY3": "FY3 Timetable",
            "FY4": "FY4 Timetable",
            "FY5": "FY5 Timetable",
            "DR": "Daily Internship DR",
            "AUTO": "Auto-detect Spreadsheet"
        }
        lbl = labels.get(target, target)
        text = (
            f"📤 **Upload Spreadsheet for {lbl}**\n\n"
            "Please send the `.xlsx` file as a **Document** attachment in this chat.\n\n"
            "💡 *The engine will immediately parse the sheet, validate divisions and faculty initials, and hot-reload.*"
        )
        kb = [[InlineKeyboardButton("🔙 Cancel / Back to Admin Menu", callback_data="ADMIN_BACK_MENU")]]
        await safe_edit_text(query, text, reply_markup=InlineKeyboardMarkup(kb))
        return ADMIN_WAIT_UPLOAD

    if data == "ADMIN_FORCE_RELOAD":
        from timetable_engine import get_timetable_engine
        engine = get_timetable_engine()
        summary = engine.reload()
        text = (
            f"🔄 **Timetable Engine Successfully Reloaded!**\n\n"
            f"• Total Divisions: **{summary['total_divisions']}**\n"
            f"• Timetable Faculty Initials: **{summary['timetable_faculty_count']}**\n"
            f"• DR Faculty Profiles: **{summary['dr_faculty_count']}**\n"
            f"• Last Synced: `{summary['last_sync_time']}`"
        )
        kb = [[InlineKeyboardButton("🔙 Back to Admin Menu", callback_data="ADMIN_BACK_MENU")]]
        await safe_edit_text(query, text, reply_markup=InlineKeyboardMarkup(kb))
        return ADMIN_MENU

    if data == "ADMIN_VIEW_STATUS":
        from timetable_engine import get_timetable_engine
        engine = get_timetable_engine()
        summary = engine.get_status()
        all_divs = sorted(list(engine.all_divisions))
        div_str = ", ".join(all_divs[:25])
        if len(all_divs) > 25:
            div_str += f" ... (+{len(all_divs) - 25} more)"

        text = (
            "📊 **Detailed Timetable Status Report**\n\n"
            f"• **Active Divisions ({len(all_divs)}):**\n`{div_str}`\n\n"
            f"• **Timetable Faculty Initials Registered:** **{summary['timetable_faculty_count']}**\n"
            f"• **DR Roster Faculty Registered:** **{summary['dr_faculty_count']}**\n"
            f"• **Last Synced:** `{summary.get('last_sync_time') or 'Never'}`"
        )
        kb = [[InlineKeyboardButton("🔙 Back to Admin Menu", callback_data="ADMIN_BACK_MENU")]]
        await safe_edit_text(query, text, reply_markup=InlineKeyboardMarkup(kb))
        return ADMIN_MENU

    if data == "ADMIN_BACK_MENU":
        await show_admin_menu(query, context)
        return ADMIN_MENU

    if data == "ADMIN_LOGOUT":
        AUTHENTICATED_ADMINS.discard(user_id)
        await safe_edit_text(query, "🔒 **Logged out from Admin Panel.** Your admin session has ended.")
        return ConversationHandler.END

    if data in ["ADMIN_EXIT", "ADMIN_CANCEL"]:
        await safe_edit_text(query, "👋 Admin Panel closed.")
        return ConversationHandler.END

    return ADMIN_MENU


async def admin_file_upload_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes uploaded timetable spreadsheet from an authenticated admin."""
    user_id = update.effective_user.id
    if user_id not in AUTHENTICATED_ADMINS:
        await update.message.reply_text("⛔ Unauthorized: Please authenticate via /admin first.")
        return ConversationHandler.END

    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".xlsx"):
        kb = [[InlineKeyboardButton("🔙 Back to Admin Menu", callback_data="ADMIN_BACK_MENU")]]
        await update.message.reply_text(
            "⚠️ Please upload a valid Excel spreadsheet (`.xlsx` file).\n\nOr click below to return to the menu:",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return ADMIN_WAIT_UPLOAD

    target = context.user_data.get("admin_upload_target", "AUTO")
    raw_fname = os.path.basename(doc.file_name)

    # Determine saved filename based on department target to ensure timetable_engine regex discovery
    if target in ["FY1", "FY2", "FY3", "FY4", "FY5"]:
        if target.lower() not in raw_fname.lower():
            saved_name = f"{target}_{raw_fname}"
        else:
            saved_name = raw_fname
    elif target == "DR":
        if not any(k in raw_fname.lower() for k in ["dr", "internship", "daily"]):
            saved_name = f"Daily_Internship_DR_{raw_fname}"
        else:
            saved_name = raw_fname
    else:
        saved_name = raw_fname

    await update.message.reply_text(f"📥 Receiving `{saved_name}` and hot-reloading engine...", parse_mode="Markdown")

    file = await doc.get_file()
    dest_path = os.path.join(os.getcwd(), saved_name)
    await file.download_to_drive(dest_path)

    from timetable_engine import get_timetable_engine
    engine = get_timetable_engine()
    summary = engine.reload()

    text = (
        f"✅ **Timetable File Uploaded & Successfully Applied!**\n\n"
        f"• **Saved File:** `{saved_name}`\n"
        f"• **Target Category:** {target}\n"
        f"• **Active Divisions:** **{summary['total_divisions']}**\n"
        f"• **Timetable Faculty Initials:** **{summary['timetable_faculty_count']}**\n"
        f"• **DR Faculty Profiles:** **{summary['dr_faculty_count']}**\n"
        f"• **Last Synced:** `{summary['last_sync_time']}`\n\n"
        f"⚡ All upcoming leave load adjustments will automatically use this updated schedule."
    )
    kb = [
        [InlineKeyboardButton("📤 Upload Another File", callback_data="ADMIN_CHOOSE_UPLOAD")],
        [InlineKeyboardButton("📊 Admin Menu", callback_data="ADMIN_BACK_MENU")],
        [InlineKeyboardButton("❌ Close Admin Panel", callback_data="ADMIN_EXIT")],
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return ADMIN_MENU


async def admin_wait_upload_text_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback when admin sends text instead of a document in upload state."""
    kb = [[InlineKeyboardButton("🔙 Back to Admin Menu", callback_data="ADMIN_BACK_MENU")]]
    await update.message.reply_text(
        "⏳ Please attach and send the `.xlsx` file as a **Document** attachment.\n\n"
        "Or click below to return to the Admin Menu:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ADMIN_WAIT_UPLOAD


async def unauthorized_upload_attempt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Warns users that public spreadsheet uploads are restricted to the admin panel."""
    user_id = update.effective_user.id
    if user_id in AUTHENTICATED_ADMINS:
        await update.message.reply_text(
            "💡 You are authenticated as an Admin. To upload a timetable, please use /admin and tap **Upload Timetable / DR File** to select the department target.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "⛔ **Upload Restricted:** Timetable spreadsheets can only be uploaded through the Admin Panel.\n\n"
            "Please use `/admin` and authenticate with the admin password.",
            parse_mode="Markdown"
        )


async def reload_timetables_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Restricted reload command."""
    user_id = update.effective_user.id
    if user_id not in AUTHENTICATED_ADMINS:
        await update.message.reply_text(
            "🔒 **Admin Authorization Required**\n\n"
            "Reloading timetables requires administrator access. Please type /admin and authenticate with the admin password.",
            parse_mode="Markdown"
        )
        return

    from timetable_engine import get_timetable_engine
    engine = get_timetable_engine()
    summary = engine.reload()

    lines = [
        "🔄 **Timetables & Daily Internship DR Reloaded:**\n",
        "**Active Department Files:**"
    ]
    for dept, fname in summary["departments"].items():
        lines.append(f"• **{dept}:** `{fname}`")

    if summary.get("dr_file"):
        lines.append(f"• **Daily Internship DR:** `{summary['dr_file']}`")

    lines.append(f"\n📊 **Statistics:**")
    lines.append(f"• Total Divisions: **{summary['total_divisions']}**")
    lines.append(f"• Faculty Initials in Timetables: **{summary['timetable_faculty_count']}**")
    lines.append(f"• Faculty Profiles in DR Roster: **{summary['dr_faculty_count']}**")
    if summary.get("last_sync_time"):
        lines.append(f"• Last Synced: `{summary['last_sync_time']}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


def run_telegram_bot():
    if not TELEGRAM_BOT_TOKEN or "YOUR_" in TELEGRAM_BOT_TOKEN:
        print("⚠️ TELEGRAM_BOT_TOKEN not configured in .env. Bot thread halted.")
        return

    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    reg_handler = ConversationHandler(
        entry_points=[
            CommandHandler(["register", "login"], register_start),
            CallbackQueryHandler(register_start, pattern="^START_REG$")
        ],
        states={
            REG_EMP: [
                CallbackQueryHandler(reg_emp_received, pattern="^CANCEL_REG$"),
                CallbackQueryHandler(handle_universal_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_emp_received)
            ],
            REG_PASS: [
                CallbackQueryHandler(reg_pass_received),
                CallbackQueryHandler(handle_universal_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_pass_received)
            ],
            REG_NAME: [
                CallbackQueryHandler(reg_confirm_received, pattern="^(REG_CONFIRM_AUTO|REG_TYPE_CUSTOM_NAME)$"),
                CallbackQueryHandler(handle_universal_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name_received)
            ],
            REG_DEPT: [
                CallbackQueryHandler(reg_dept_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_dept_received)
            ],
            REG_POS: [
                CallbackQueryHandler(reg_pos_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_pos_received)
            ],
            REG_SHORT_NAME: [
                CallbackQueryHandler(reg_short_name_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_short_name_received)
            ],
            REG_BAL_CHOICE: [CallbackQueryHandler(reg_bal_choice_picked)],
            REG_BAL_VERIFY: [CallbackQueryHandler(reg_bal_verify_picked)],
            REG_BAL_MANUAL: [
                CallbackQueryHandler(reg_bal_manual_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_bal_manual_received)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_cmd),
            CallbackQueryHandler(handle_universal_callback)
        ],
        allow_reentry=True,
        per_message=False,
    )

    apply_handler = ConversationHandler(
        entry_points=[
            CommandHandler("apply", apply_start),
            CallbackQueryHandler(apply_start, pattern="^CMD_APPLY$"),
            CallbackQueryHandler(leave_type_chosen, pattern="^(CL|SD|SL|EL|RH|VL|LWP|ExL|DL)$"),
        ],
        states={
            LEAVE_TYPE: [CallbackQueryHandler(leave_type_chosen)],
            DAY_TYPE: [CallbackQueryHandler(day_type_chosen)],
            DATE_PICK: [CallbackQueryHandler(date_picked)],
            CUSTOM_DATE: [
                CallbackQueryHandler(date_picked),
                MessageHandler(filters.TEXT & ~filters.COMMAND, custom_date_entered)
            ],
            LOAD_CHOICE: [CallbackQueryHandler(load_choice_picked)],
            LOAD_COUNT: [
                CallbackQueryHandler(load_count_picked),
                MessageHandler(filters.TEXT & ~filters.COMMAND, load_count_received)
            ],
            LOAD_INPUT: [
                CallbackQueryHandler(load_input_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND, load_input_received)
            ],
            LOAD_NOT_ADJ_STATUS: [CallbackQueryHandler(load_not_adj_status_picked)],
            LOAD_AUTO_MAX_DIV: [CallbackQueryHandler(load_auto_max_div_picked)],
            LOAD_AUTO_MERGED: [CallbackQueryHandler(load_auto_merged_picked)],
            LOAD_AUTO_OPTIONS: [CallbackQueryHandler(load_auto_options_picked)],
            LOAD_AUTO_SLOT_PICK: [CallbackQueryHandler(load_auto_slot_pick_handler)],
            SUBMIT_TIMING: [CallbackQueryHandler(submit_timing_picked)],
            REASON_CHOICE: [CallbackQueryHandler(reason_choice_picked)],
            CUSTOM_REASON: [
                CallbackQueryHandler(reason_choice_picked),
                MessageHandler(filters.TEXT & ~filters.COMMAND, custom_reason_received)
            ],
            CREDIT_CONFIRM: [CallbackQueryHandler(credit_confirm_picked)],
            CONFIRMATION: [CallbackQueryHandler(confirmation_picked)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_cmd),
            CallbackQueryHandler(handle_universal_callback)
        ],
        allow_reentry=True,
        per_message=False,
    )

    profile_handler = ConversationHandler(
        entry_points=[
            CommandHandler("profile", profile_start),
            CallbackQueryHandler(profile_start, pattern="^CMD_EDIT_PROFILE$"),
            CallbackQueryHandler(profile_menu_picked, pattern="^PROF_EDIT:"),
        ],
        states={
            PROF_MENU: [
                CallbackQueryHandler(profile_menu_picked),
            ],
            PROF_INPUT_NAME: [
                CallbackQueryHandler(profile_menu_picked, pattern="^(BACK_TO_PROFILE|PROF_CANCEL)$"),
                CallbackQueryHandler(handle_universal_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, profile_name_received),
            ],
            PROF_INPUT_INITIALS: [
                CallbackQueryHandler(profile_menu_picked, pattern="^(BACK_TO_PROFILE|PROF_CANCEL)$"),
                CallbackQueryHandler(handle_universal_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, profile_initials_received),
            ],
            PROF_INPUT_DEPT: [
                CallbackQueryHandler(profile_dept_received),
                CallbackQueryHandler(handle_universal_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, profile_dept_received),
            ],
            PROF_INPUT_POS: [
                CallbackQueryHandler(profile_pos_received),
                CallbackQueryHandler(handle_universal_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, profile_pos_received),
            ],
            PROF_INPUT_PASS: [
                CallbackQueryHandler(profile_menu_picked, pattern="^(BACK_TO_PROFILE|PROF_CANCEL)$"),
                CallbackQueryHandler(handle_universal_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, profile_pass_received),
            ],
            REG_BAL_MANUAL: [
                CallbackQueryHandler(reg_bal_manual_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_bal_manual_received),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_cmd),
            CallbackQueryHandler(handle_universal_callback),
        ],
        allow_reentry=True,
        per_message=False,
    )

    att_handler = ConversationHandler(
        entry_points=[
            CommandHandler(["attendance", "punch"], attendance_cmd),
            CallbackQueryHandler(attendance_cmd, pattern="^CMD_ATTENDANCE$"),
            CallbackQueryHandler(attendance_callback_handler, pattern="^ATT_"),
        ],
        states={
            ATT_CUSTOM_DATE: [
                CallbackQueryHandler(attendance_callback_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, attendance_custom_date_received)
            ]
        },
        fallbacks=[
            CommandHandler("cancel", cancel_cmd),
            CallbackQueryHandler(handle_universal_callback)
        ],
        allow_reentry=True,
        per_message=False,
    )

    shift_handler = ConversationHandler(
        entry_points=[
            CommandHandler(["shift", "shiftchange"], shift_start),
            CallbackQueryHandler(shift_start, pattern="^CMD_SHIFT_CHANGE$"),
            CallbackQueryHandler(shift_date_picked, pattern="^SHIFT_"),
        ],
        states={
            SHIFT_DATE_PICK: [
                CallbackQueryHandler(shift_date_picked),
            ],
            SHIFT_CUSTOM_DATE: [
                CallbackQueryHandler(shift_date_picked),
                MessageHandler(filters.TEXT & ~filters.COMMAND, shift_custom_date_received),
            ],
            SHIFT_NEW_PICK: [
                CallbackQueryHandler(shift_new_picked),
            ],
            SHIFT_REASON_PICK: [
                CallbackQueryHandler(shift_reason_picked),
            ],
            SHIFT_CUSTOM_REASON: [
                CallbackQueryHandler(shift_reason_picked),
                MessageHandler(filters.TEXT & ~filters.COMMAND, shift_custom_reason_received),
            ],
            SHIFT_CONFIRM: [
                CallbackQueryHandler(shift_confirm_picked),
            ],
            SHIFT_SUBMIT: [
                CallbackQueryHandler(shift_submission_picked),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_cmd),
            CallbackQueryHandler(handle_universal_callback),
        ],
        allow_reentry=True,
        per_message=False,
    )

    admin_handler = ConversationHandler(
        entry_points=[
            CommandHandler("admin", admin_start),
            CallbackQueryHandler(admin_start, pattern="^CMD_ADMIN$"),
        ],
        states={
            ADMIN_PASS_INPUT: [
                CallbackQueryHandler(admin_cancel_picked, pattern="^(ADMIN_CANCEL|ADMIN_EXIT)$"),
                CallbackQueryHandler(handle_universal_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_pass_received),
            ],
            ADMIN_MENU: [
                CallbackQueryHandler(admin_menu_picked, pattern="^(ADMIN_|BACK_TO_ADMIN)"),
                CallbackQueryHandler(handle_universal_callback),
            ],
            ADMIN_WAIT_UPLOAD: [
                CallbackQueryHandler(admin_menu_picked, pattern="^(ADMIN_|BACK_TO_ADMIN)"),
                CallbackQueryHandler(handle_universal_callback),
                MessageHandler(filters.Document.ALL, admin_file_upload_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_wait_upload_text_fallback),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_cmd),
            CallbackQueryHandler(handle_universal_callback),
        ],
        allow_reentry=True,
        per_message=False,
    )

    check_load_handler = ConversationHandler(
        entry_points=[
            CommandHandler(["adjust", "checkload", "load"], check_load_start),
            CallbackQueryHandler(check_load_start, pattern="^CMD_CHECK_LOAD$"),
        ],
        states={
            CHECK_LOAD_FACULTY: [
                CallbackQueryHandler(check_load_faculty_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND, check_load_faculty_received),
            ],
            CHECK_LOAD_DATE: [
                CallbackQueryHandler(check_load_date_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND, check_load_date_received),
            ],
            CHECK_LOAD_MAX_DIV: [
                CallbackQueryHandler(check_load_max_div_picked),
            ],
            CHECK_LOAD_MERGED: [
                CallbackQueryHandler(check_load_merged_picked),
            ],
            CHECK_LOAD_CASCADE: [
                CallbackQueryHandler(check_load_cascade_picked),
            ],
            CHECK_LOAD_VIEW: [
                CallbackQueryHandler(check_load_view_picked),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_cmd),
            CallbackQueryHandler(handle_universal_callback),
        ],
        allow_reentry=True,
        per_message=False,
    )

    application.add_handler(CommandHandler(["start", "menu"], start_cmd))
    application.add_handler(CommandHandler(["status", "appstatus", "checkstatus"], status_cmd))
    application.add_handler(CommandHandler(["logout", "signout"], logout_cmd))
    application.add_handler(CommandHandler(["balance", "balances"], balance_cmd))
    application.add_handler(CommandHandler(["reload_timetables", "sync_timetables"], reload_timetables_cmd))
    application.add_handler(CallbackQueryHandler(balance_cmd, pattern="^CMD_BALANCE$"))
    application.add_handler(CallbackQueryHandler(status_cmd, pattern="^(CMD_STATUS|REFRESH_STATUS)$"))
    application.add_handler(CallbackQueryHandler(logout_cmd, pattern="^CMD_LOGOUT$"))
    application.add_handler(check_load_handler)
    application.add_handler(admin_handler)
    application.add_handler(reg_handler)
    application.add_handler(apply_handler)
    application.add_handler(profile_handler)
    application.add_handler(att_handler)
    application.add_handler(shift_handler)
    application.add_handler(MessageHandler(filters.Document.FileExtension("xlsx"), unauthorized_upload_attempt))
    application.add_handler(CallbackQueryHandler(handle_universal_callback))
    application.add_error_handler(global_error_handler)

    print("🤖 Telegram Bot thread is active.")
    application.run_polling(stop_signals=None, close_loop=False)


def run_flask():
    print(f"🌐 Starting Flask Web & Portal Engine on port {APP_PORT}...")
    app.run(host="0.0.0.0", port=APP_PORT, use_reloader=False)


# ==========================================
# 9. UNIFIED MAIN ENTRY
# ==========================================
if __name__ == "__main__":
    os.makedirs("generated_pdfs", exist_ok=True)
    
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

    run_telegram_bot()
