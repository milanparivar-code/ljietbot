"""LJIET Leave Application PDF generator — pixel-faithful to company samples.

Reconstructed (05/09/2026) by measuring the official sample PDFs gridline by
gridline: one 13-column table on A4, Times New Roman, exact strings, fills and
row heights. DO NOT "redesign" this layout — the company fines for format
deviations. If the template changes, re-measure with pdf_audit/grid.py.
"""

import os
from datetime import datetime, date


def compute_leave_semester(date_val, dept=None, division=None) -> str:
    """
    Computes semester Roman numeral according to LJIET rules:
    - First year department: Sem I or II
    - Second year department: Sem III or IV
    - Date range:
      - March to July (months 3 to 7): 1st sem (I) or III sem (III)
      - August to February (months 8 to 12, 1, 2): 2nd sem (II) or IV sem (IV)
    """
    dt = None
    if isinstance(date_val, (datetime, date)):
        dt = date_val
    elif isinstance(date_val, str) and date_val.strip():
        for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y", "%d %b %Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%y"]:
            try:
                dt = datetime.strptime(date_val.strip(), fmt)
                break
            except ValueError:
                pass
    if not dt:
        from timezone_utils import get_ist_now
        dt = get_ist_now()

    month = dt.month
    is_first_year = True
    dept_str = str(dept or "").upper()
    div_str = str(division or "").upper().strip()

    if "SY" in dept_str or "SECOND" in dept_str:
        is_first_year = False
    elif "FY" in dept_str or "FIRST" in dept_str:
        is_first_year = True
    elif div_str.startswith("SY"):
        is_first_year = False
    else:
        # Default to First Year (all current Master TT divisions A1-A9, B1-B7, C1-C6, D1-D7, E1-E4, F1-F3, G1-G3, CH are FY)
        is_first_year = True

    # March to July (3 <= month <= 7) -> I or III
    if 3 <= month <= 7:
        return "I" if is_first_year else "III"
    else:
        # August to February -> II or IV
        return "II" if is_first_year else "IV"


def format_load_row_values(adj_item: dict, default_date: str = "") -> dict:
    """Formats lecture adjustment row data for leave PDF table."""
    dt_s = adj_item.get("date", default_date)
    duty = adj_item.get("duty", {}) if isinstance(adj_item.get("duty"), dict) else {}
    sbj = adj_item.get("original_subject") or duty.get("subject") or adj_item.get("subject", "")
    s_div = adj_item.get("class_div", "")
    t_slot = adj_item.get("slot", "")
    eng = adj_item.get("substitute", "")

    dept_val = adj_item.get("dept") or duty.get("dept")
    sem_val = adj_item.get("sem") or compute_leave_semester(dt_s, dept=dept_val, division=s_div)

    if isinstance(eng, dict):
        sub_init = eng.get("initials", "")
        sub_subj = eng.get("subject", "")
        if sub_subj and sub_subj != sbj:
            eng_disp = f"{sub_init} ({sub_subj})"
        else:
            eng_disp = sub_init
    elif eng:
        eng_disp = str(eng)
    else:
        eng_disp = adj_item.get("engager_display", "")

    return {
        "date": dt_s,
        "subject": sbj,
        "sem": sem_val,
        "class_div": s_div,
        "slot": t_slot,
        "engager": eng_disp
    }


from timezone_utils import get_ist_today_str
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, PageBreak, Spacer
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from portal_api import LeavePortalAPI

# Metric-exact Times New Roman clone (bundled OFL fonts) so text widths
# match the company Excel template to the sub-point on every server.
_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
try:
    pdfmetrics.registerFont(TTFont("TNR", os.path.join(_FONTS_DIR, "LiberationSerif-Regular.ttf")))
    pdfmetrics.registerFont(TTFont("TNR-Bold", os.path.join(_FONTS_DIR, "LiberationSerif-Bold.ttf")))
    FONT_R, FONT_B = "TNR", "TNR-Bold"
except Exception:
    FONT_R, FONT_B = "Times-Roman", "Times-Bold"  # offline fallback

# ----------------------------------------------------------------------------
# Measured geometry (points). Column boundaries:
# 16.7 | 77.3 | 111.4 | 150.8 | 188.3 | 225.8 | 275.6 | 313.7 | 351.7 |
# 389.8 | 427.8 | 465.9 | 522.7 | 573.6  (content width 556.9)
# ----------------------------------------------------------------------------
COLS = [60.6, 34.1, 39.4, 37.5, 37.5, 49.8, 38.1, 38.0, 38.1, 38.0, 38.1,
        56.8, 50.9]
# Row heights top -> bottom (sum = 428.6 = 491.6 - 63.0 in samples)
ROWS = [11.4, 11.1, 6.6, 12.1, 6.2, 9.3, 5.7, 9.4, 13.3, 13.7, 7.0, 12.4,
        6.2, 2.3, 6.1, 9.3, 11.1, 9.4, 16.28, 16.28, 16.28, 16.28, 16.28, 8.9,
        18.6, 57.2, 26.0, 16.7, 20.9, 13.3, 17.4, 18.8, 0.4]

LEFT_MARGIN = 16.7
RIGHT_MARGIN = A4[0] - LEFT_MARGIN - sum(COLS)  # ~21.67
# NOTE: ReportLab's frame puts content 6pt below topMargin, so we
# calibrate: 57 -> content lands exactly on the sample grid (verified).
TOP_MARGIN = 57.0

# ----------------------------------------------------------------------------
# Measured fills
# ----------------------------------------------------------------------------
C_RED = colors.HexColor("#FF0000")
C_BLUE = colors.HexColor("#D9E1F3")
C_GREY = colors.HexColor("#C7C7C7")
C_EBB = colors.HexColor("#EBEBEB")
C_DARKGREY = colors.HexColor("#7A7A7A")
C_BROWN = colors.HexColor("#C55A11")
C_BLUEGREY = colors.HexColor("#ACB8C9")
C_LGREY2 = colors.HexColor("#BEBFBE")
C_YEL1 = colors.HexColor("#FFD964")
C_GRN1 = colors.HexColor("#A8D08D")
C_PURPLE = colors.HexColor("#A340A6")
C_LBLUE = colors.HexColor("#9CC2E4")
C_YEL2 = colors.HexColor("#FFFF00")
C_CYAN = colors.HexColor("#00AFEF")
C_BLACK = colors.black

GRID_W = 0.6

# PDF leave types and balance columns in exact requested sequence:
# CL SD Ex.L EL SL RH LWP VL DL
LEAVE_COLS = ["CL", "SD", "Ex.L", "EL", "SL", "RH", "LWP", "VL", "DL"]
BAL_COLS = LEAVE_COLS
# Columns left blank when their value is exactly 0 (matches samples).
# DL and LWP balances are explicitly written as per user balance requirement.
BLANK_IF_ZERO = {"SD"}


def _get_balance_value(bal_dict, col):
    """Safely extracts balance float for col from dictionary with aliases."""
    if not isinstance(bal_dict, dict):
        return 0.0
    aliases = [col]
    if col in ("Ex.L", "EXL", "ExL"):
        aliases.extend(["EXL", "Ex.L", "ExL", "EXCHANGE"])
    elif col.upper() == "LWP":
        aliases.extend(["LWP", "Lwp", "lwp"])
    elif col.upper() == "DL":
        aliases.extend(["DL", "Dl", "dl"])
    elif col.upper() == "CL":
        aliases.extend(["CL", "Cl", "cl"])
    elif col.upper() == "SD":
        aliases.extend(["SD", "Sd", "sd", "SPCL", "Spcl"])
    elif col.upper() == "SL":
        aliases.extend(["SL", "Sl", "sl", "ML", "Ml"])
    elif col.upper() == "EL":
        aliases.extend(["EL", "El", "el"])
    elif col.upper() == "VL":
        aliases.extend(["VL", "Vl", "vl"])
    elif col.upper() == "RH":
        aliases.extend(["RH", "Rh", "rh"])
    for a in aliases:
        if a in bal_dict:
            val = bal_dict[a]
            if isinstance(val, dict):
                val = val.get("actual", val.get("portal", 0.0))
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    return 0.0


def get_faculty_shortname(emp_name: str) -> str:
    """Return concise shortname/initials for faculty member, e.g. Milan Patel -> MDP."""
    parts = [p.strip() for p in (emp_name or "").split() if p.strip()]
    if not parts:
        return "FACULTY"
    if len(parts) >= 3:
        return "".join([p[0].upper() for p in parts[:3]])
    elif len(parts) == 2:
        first, last = parts[0].upper(), parts[1].upper()
        if "MILAN" in first and "PATEL" in last:
            return "MDP"
        return f"{first[0]}{last[0]}"
    elif len(parts) == 1:
        return parts[0].upper()
    return "EMP"


def build_leave_filename(leave_data: dict) -> str:
    """
    Filename format: FacultyShortname_No of days_Category of leave_Date or Range of date_Load adjustment arrangement.pdf
    Example: MDP_1_CL_09_Sep_2026_Load Adjusted.pdf
    """
    shortname = leave_data.get("short_name") or leave_data.get("initials") or leave_data.get("faculty_initials")
    if not shortname:
        emp_name = leave_data.get("emp_name") or leave_data.get("name") or "FACULTY"
        shortname = get_faculty_shortname(emp_name)

    days = float(leave_data.get("total_days", leave_data.get("units", 1)))
    days_str = str(int(days)) if days == int(days) else str(days)

    leave_type = str(leave_data.get("leave_type", "CL")).upper()

    frm_dt = str(leave_data.get("from_date", get_ist_today_str("%d/%m/%Y"))).strip()
    to_dt = str(leave_data.get("to_date", frm_dt)).strip() or frm_dt

    try:
        d1 = datetime.strptime(frm_dt, "%d/%m/%Y")
        frm_disp = d1.strftime("%d_%b_%Y")
    except Exception:
        frm_disp = frm_dt.replace("/", "_").replace("-", "_")

    try:
        d2 = datetime.strptime(to_dt, "%d/%m/%Y")
        to_disp = d2.strftime("%d_%b_%Y")
    except Exception:
        to_disp = to_dt.replace("/", "_").replace("-", "_")

    if frm_dt == to_dt or not to_dt:
        date_str = frm_disp
    else:
        date_str = f"{frm_disp}_to_{to_disp}"

    load_status = leave_data.get("load_status", "Load Adjusted").strip()
    if load_status.startswith("_"):
        load_status = load_status[1:]

    return f"{shortname}_{days_str}_{leave_type}_{date_str}_{load_status}.pdf"


def get_leave_filename(leave_data, legacy=False):
    """Generates PDF filename. Defaults to the new standard format:
    FacultyShortname_No of days_Category of leave_Date or Range of date_Load adjustment arrangement.pdf
    """
    if not legacy:
        return build_leave_filename(leave_data)

    shortname = leave_data.get("short_name") or leave_data.get("initials") or leave_data.get("faculty_initials")
    if shortname:
        initials = shortname
    else:
        emp_name = leave_data.get("emp_name", "FACULTY").strip()
        parts = [p for p in emp_name.split() if p]
        if len(parts) >= 3:
            initials = "".join([p[0].upper() for p in parts[:3]])
        elif len(parts) == 2:
            if "MILAN" in parts[0].upper() and "PATEL" in parts[1].upper():
                initials = "MDP"
            else:
                initials = f"{parts[0][0]}D{parts[1][0]}".upper() if len(parts[0]) > 1 and parts[0].upper().endswith("D") else f"{parts[0][0]}{parts[1][0]}".upper()
        elif len(parts) == 1 and parts[0]:
            initials = parts[0][:3].upper()
        else:
            initials = "EMP"

    days = float(leave_data.get("total_days", 1))
    days_str = str(int(days)) if days == int(days) else str(days)
    leave_type = leave_data.get("leave_type", "CL").upper()

    frm_dt = leave_data.get("from_date", get_ist_today_str("%d/%m/%Y"))
    to_dt = leave_data.get("to_date", frm_dt)
    try:
        d1 = datetime.strptime(frm_dt, "%d/%m/%Y")
        frm_fname = d1.strftime("%d_%b")
        frm_display = d1.strftime("%d-%b-%y")
    except Exception:
        frm_fname = frm_dt.replace("-", "_").replace("/", "_")
        frm_display = frm_dt
    try:
        d2 = datetime.strptime(to_dt, "%d/%m/%Y")
        to_display = d2.strftime("%d-%b-%y")
    except Exception:
        to_display = to_dt

    half_type = leave_data.get("half_type", "First Half")
    short_type = leave_data.get("short_type", "Morning Short")

    if days > 1:
        dates_fname = (f"{frm_display.split('-')[0]} {frm_display.split('-')[1]} "
                       f"To {to_display.split('-')[0]} {to_display.split('-')[1]}")
        day_mode = f"_{days_str} days_"
        middle = f"{dates_fname}{day_mode}"
    elif days == 1:
        middle = f"{frm_fname} _Full Day_"
    elif days == 0.5:
        middle = f"{frm_fname} _Half Day_ {half_type}_"
    else:
        middle = f"{frm_fname} _Short Day_ {short_type}_"

    load_status = leave_data.get("load_status", "Load Adjusted")
    if load_status.startswith("_"):
        load_status = load_status[1:]

    return (f"Leave _Report_{initials}_{days_str} {leave_type}_"
            f"{middle}{load_status}.pdf")


def clean_position(pos: str) -> str:
    p = str(pos or "").strip()
    if not p:
        return "AP"
    low = p.lower()
    if "assistant professor" in low or low == "ap":
        return "AP"
    if "associate professor" in low or low == "asp":
        return "ASP"
    if "professor" in low or low == "prof":
        return "PROF"
    if "lab assistant" in low or low == "la":
        return "LA"
    return p


def _fmt_bal(v):
    if v is None:
        return ""
    if v == 0:
        return "0"
    if v == int(v):
        return str(int(v))
    return f"{v:.2f}".rstrip("0").rstrip(".")


class LeavePDFGenerator:
    def __init__(self, output_dir="generated_pdfs"):
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)
        self._setup_styles()

    def _setup_styles(self):
        def st(name, font, size, align, color=C_BLACK, leading=None):
            return ParagraphStyle(name, fontName=font, fontSize=size,
                                  leading=leading or size * 1.15,
                                  alignment=align, textColor=color)
        T, TB = FONT_R, FONT_B
        self.s_title = st("title", TB, 7.4, TA_CENTER)
        self.s_name_lab = st("namelab", TB, 7.4, TA_RIGHT)
        self.s_name_val = st("nameval", TB, 8.5, TA_CENTER, C_RED)
        self.s_c_lab = st("clab", TB, 7.4, TA_CENTER)
        self.s_lab_r = st("labr", TB, 7.4, TA_RIGHT)
        self.s_lab_l = st("labl", TB, 7.4, TA_LEFT)
        self.s_val = st("val", TB, 7.4, TA_CENTER, C_RED)
        self.s_from_lab = st("fromlab", TB, 7.4, TA_RIGHT)
        self.s_type_lab = st("typelab", TB, 7.4, TA_LEFT)
        self.s_type_h = st("typeh", TB, 7.4, TA_CENTER)
        self.s_bal_lab = st("ballab", TB, 6.2, TA_LEFT)
        self.s_bal = st("bal", T, 7.4, TA_CENTER)
        self.s_bal.splitLongWords = 0
        self.s_hi = st("hi", T, 8.5, TA_CENTER)
        self.s_dept = st("dept", TB, 7.4, TA_CENTER)
        self.s_alt = st("alt", T, 8.5, TA_CENTER)
        self.s_load_h = st("loadh", TB, 7.4, TA_CENTER)
        self.s_load_d = st("loadd", T, 6.8, TA_CENTER)
        self.s_status = st("status", T, 7.9, TA_CENTER)
        self.s_strip = st("strip", T, 6.2, TA_LEFT)
        self.s_noload = st("noload", TB, 7.4, TA_CENTER)
        self.s_loadhead = st("loadhead", TB, 7.4, TA_CENTER)
        self.s_tall = st("tall", T, 7.4, TA_CENTER, leading=9.2)
        self.s_tall_big = st("tallbig", T, 8.5, TA_CENTER)
        self.s_mini = st("mini", T, 5.8, TA_CENTER, leading=6.5)
        self.s_fac = st("fac", T, 7.4, TA_CENTER)
        self.s_c74 = st("c74", T, 7.4, TA_CENTER)
        self.s_bot = st("bot", T, 6.8, TA_CENTER)
        self.s_score = st("score", T, 7.4, TA_CENTER)
        self.s_date_lab = st("datelab", TB, 7.4, TA_CENTER)
        self.s_date_val = st("dateval", TB, 7.4, TA_CENTER)
        self.s_sig = st("sig", TB, 7.4, TA_LEFT)

    def P(self, text, style):
        return Paragraph(text, style) if text != "" else ""

    # ------------------------------------------------------------------
    def generate_pdf(self, leave_data, balances_before=None):
        """Build the exact-format PDF. `balances_before` (dict col->float)
        may be injected (tests); otherwise live portal balances are used."""
        filename = get_leave_filename(leave_data)
        filepath = os.path.join(self.output_dir, filename)

        # ---- dynamic values ----
        frm_dt = leave_data.get("from_date", get_ist_today_str("%d/%m/%Y"))
        to_dt = leave_data.get("to_date", frm_dt)
        try:
            frm_disp = datetime.strptime(frm_dt, "%d/%m/%Y").strftime("%d-%b-%y")
        except Exception:
            frm_disp = frm_dt
        try:
            to_disp = datetime.strptime(to_dt, "%d/%m/%Y").strftime("%d-%b-%y")
        except Exception:
            to_disp = to_dt

        emp_name = leave_data.get("emp_name") or leave_data.get("name") or "Faculty Member"
        department = leave_data.get("department") or leave_data.get("dept") or "Civil Engineering"
        position = clean_position(leave_data.get("position", "AP"))
        days = float(leave_data.get("total_days", leave_data.get("units", 1)))
        days_str = str(int(days)) if days == int(days) else str(days)
        leave_type = leave_data.get("leave_type", "CL").upper()

        if days >= 1:
            half_cell = ""
        elif days == 0.25:
            half_cell = ("Second Half"
                         if leave_data.get("short_type", "Morning Short") in ("Evening Short", "Afternoon Short")
                         else "First Half")
        else:
            half_cell = leave_data.get("half_type", "First Half")

        if balances_before is None:
            api = LeavePortalAPI()
            api.login()
            rec = api.get_reconciled_balances()
            balances_before = {c: _get_balance_value(rec, c) for c in BAL_COLS}
        else:
            balances_before = {c: _get_balance_value(balances_before, c) for c in BAL_COLS}

        # Standardize applied leave code
        clean_lt = str(leave_type or "CL").upper().replace(" ", "").replace(".", "")
        if clean_lt in ("EXL", "EX", "EXCHANGE"):
            applied_col = "Ex.L"
        elif clean_lt in ("SD", "SHORTDAY"):
            applied_col = "SD"
        elif clean_lt in ("CL", "CASUAL"):
            applied_col = "CL"
        else:
            applied_col = leave_type

        # Calculate balances for all columns: CL & SD share the same pool
        before_txt, after_txt = {}, {}
        b_cl = float(balances_before.get("CL", 0.0))
        if applied_col in ("CL", "SD"):
            a_cl = b_cl - days
        else:
            a_cl = b_cl
        if abs(a_cl) < 1e-6:
            a_cl = 0.0

        for c in LEAVE_COLS[2:]:
            b = float(balances_before.get(c, 0.0))
            a = (b - days) if applied_col == c else b
            if abs(a) < 1e-6:
                a = 0.0
            if c in BLANK_IF_ZERO and applied_col != c and b == 0 and a == 0:
                before_txt[c] = ""
                after_txt[c] = ""
            else:
                before_txt[c] = _fmt_bal(b)
                after_txt[c] = _fmt_bal(a)

        load_status = leave_data.get("load_status", "Load Adjusted")
        load_subject = (leave_data.get("load_subject") or "").strip()
        load_sem = (leave_data.get("load_sem") or "").strip()
        load_time = (leave_data.get("load_time") or "").strip()
        load_engager = (leave_data.get("load_engager") or "").strip()
        load_adjustments = leave_data.get("load_adjustments", [])
        if not load_adjustments and (load_subject or load_sem or load_time or load_engager):
            load_adjustments = [{
                "subject": load_subject,
                "class_div": load_sem,
                "slot": load_time,
                "substitute": load_engager,
                "date": frm_disp
            }]

        has_arrangement = bool(load_adjustments)
        if "Taken By Self" in load_status:
            status_txt = "Load Taken By Self"
        elif "No Load" in load_status:
            status_txt = "No Load"
        else:
            status_txt = "Load Adjusted"

        bot_label = f"{days_str} {leave_type}" if days < 1 else leave_type
        score_val = leave_data.get("score_val")
        if score_val is None:
            score_val = leave_data.get("credit_penalty")
        if score_val is None:
            score_val = "-1" if "Adjusted" in load_status else "0"

        S = self
        P = self.P
        E = ""  # empty cell

        def _format_load_row(adj_item):
            vals = format_load_row_values(adj_item, frm_disp)
            return [P(vals["date"], S.s_load_d),
                    P(vals["subject"], S.s_load_d), E,
                    P(vals["sem"], S.s_load_d),
                    P(vals["slot"], S.s_load_d), E,
                    P(vals["engager"], S.s_load_d), E, E, E, E, E]

        # Allocate up to 5 adjustment rows sequentially across rows 18, 19, 20, 21, 22
        r18_cells = [E] * 13
        r19_cells = [E] * 13
        r20_cells = [E] * 13
        r21_cells = [E] * 13
        r22_cells = [E] * 13

        if has_arrangement and len(load_adjustments) > 0:
            target_rows = [r18_cells, r19_cells, r20_cells, r21_cells, r22_cells]
            for i, adj in enumerate(load_adjustments[:5]):
                target_rows[i] = _format_load_row(adj)
            r18_cells, r19_cells, r20_cells, r21_cells, r22_cells = target_rows
        else:
            r19_cells = [P(status_txt, S.s_status)] + [E] * 12

        # ---- 32 rows x 13 cols ----
        t = []
        t.append([P("LEAVE APPLICATION FORM", S.s_title)] + [E] * 12)          # 0
        t.append([P("L. J. Institute of Engineering &amp; Technology",
                    S.s_title)] + [E] * 12)                                    # 1
        t.append([E] * 13)                                                     # 2 strip
        # Dynamic font scaling to avoid multi-line overflow on longer faculty names/departments
        name_len = len(emp_name)
        if name_len > 22:
            s_name = ParagraphStyle("name_dyn", parent=S.s_name_val, fontSize=6.5, leading=7.2)
        elif name_len > 16:
            s_name = ParagraphStyle("name_dyn", parent=S.s_name_val, fontSize=7.4, leading=8.2)
        else:
            s_name = S.s_name_val

        dept_len = len(department)
        if dept_len > 22:
            s_dept_val = ParagraphStyle("dept_dyn", parent=S.s_val, fontSize=5.8, leading=6.6)
        elif dept_len > 15:
            s_dept_val = ParagraphStyle("dept_dyn", parent=S.s_val, fontSize=6.5, leading=7.3)
        else:
            s_dept_val = S.s_val

        t.append([P("Name:\xa0", S.s_name_lab),                                    # 3
                  P(emp_name, s_name), E, E, E,
                  P("Department:", S.s_lab_r),
                  P(department, s_dept_val), E,
                  P("Position:", S.s_lab_l),
                  P(position, S.s_val), E, E, E])
        t.append([E] * 13)                                                     # 4 strip
        t.append([P("Days:\xa0\xa0", S.s_name_lab),                                    # 5
                  P(days_str, S.s_val),
                  P("From", S.s_from_lab),
                  P(frm_disp, S.s_val), E,
                  P("to", S.s_c_lab),
                  P(to_disp, S.s_val), E, E,
                  P(half_cell, S.s_val), E, E, E])
        t.append([E] * 13)                                                     # 6 strip
        t.append([P("Type of Leave)", S.s_type_lab), E, E, E] +             # 7
                 [P(c, S.s_type_h) for c in LEAVE_COLS])
        t.append([P("Leave Balance before Application:", S.s_bal_lab),         # 8
                  E, E, E, P(_fmt_bal(b_cl), S.s_bal), E] +
                 [P(before_txt[c], S.s_bal) for c in LEAVE_COLS[2:]])
        t.append([P("Leave Balance after Application:", S.s_bal_lab),          # 9
                  E, E, E, P(_fmt_bal(a_cl), S.s_bal), E] +
                 [P(after_txt[c], S.s_bal) for c in LEAVE_COLS[2:]])
        t.append([E] * 13)                                                     # 10 strip
        t.append([P("Highlight the Leave type applied", S.s_hi)] + [E] * 12)  # 11
        t.append([E] * 13)                                                     # 12 strip
        t.append([E] * 13)                                                     # 13 hairline
        t.append([E] * 13)                                                     # 14 strip
        t.append([P("For Department Use Only (Alternate Load Arrangement)",
                    S.s_dept)] + [E] * 12)                                    # 15
        t.append([P("Alternate arrangement of work load is made as below:",
                    S.s_alt)] + [E] * 12)                                     # 16
        t.append([P("Date", S.s_load_h),                                       # 17
                  P("Subject", S.s_load_h), E,
                  P("Sem", S.s_load_h),
                  P("Time", S.s_load_h), E,
                  P("Staff member who will engage the work", S.s_load_h),
                  E, E, E, E, E])
        t.append(r18_cells)                                                    # 18 R1
        t.append(r19_cells)                                                    # 19 R2
        t.append(r20_cells)                                                    # 20 R3
        t.append(r21_cells)                                                    # 21 R4
        t.append(r22_cells)                                                    # 22 R5
        t.append([P("For Department Use ", S.s_strip)] + [E] * 12)             # 23
        t.append([E,                                                          # 24
                  P("No Load", S.s_noload), E,
                  P("Load (Lec/Lab/ TL/Supervision/Paper assessment / "
                    "Uni. duty )", S.s_loadhead),
                  E, E, E, E, E, E, E, E, E])
        t.append([P("Category of Leave", S.s_tall),                            # 25
                  P("Leave<br/>form<br/>Submitted<br/>before<br/>taking<br/>"
                    "leave", S.s_tall),
                  P("Leave form<br/>submitted<br/>after taking<br/>leave",
                    S.s_tall),
                  P("Leave form submitted<br/>before taking leave", S.s_tall),
                  E,
                  P("Leave form submitted after taking leave", S.s_tall_big),
                  E, E, E, E, E, E, E])
        t.append([E, E, E,                                                     # 26
                  P("Load<br/>adjusted by", S.s_mini),
                  P("Load<br/>adjusted by", S.s_mini),
                  P("Load adjusted by Faculty", S.s_fac), E,
                  P("Load adjusted by HOD", S.s_fac), E, E, E, E, E])
        t.append([E, E, E, E, E,                                               # 27
                  P("Informed by call about<br/>leave (-1.25)", S.s_c74), E,
                  P("Informed to HOD by<br/>call\xa0about\xa0leave\xa0\xa0\xa0\xa0\xa0\xa0\xa0\xa0\xa0\xa0\xa0\xa0\xa0<br/>(- 1.75)",
                    S.s_c74), E,
                  P("Did not informed to HOD", S.s_c74), E, E, E])
        t.append([E,                                                          # 28
                  P("( 0 )", S.s_c74), P("( -0.5 )", S.s_c74),
                  P("(-1)", S.s_c74), P("(-1.5)", S.s_c74), E, E, E, E,
                  P("Responded to HOD call", S.s_c74), E,
                  P("Not Responded to<br/>HOD call", S.s_c74), E])
        t.append([E, E, E, E, E, E, E, E, E,                                  # 29
                  P("( -2 )", S.s_c74), E,
                  P("( -2.5 )", S.s_c74), E])

        try:
            score_num = float(score_val)
        except (TypeError, ValueError):
            score_num = 0.0
        score_text = _fmt_bal(score_num)
        score_desc = str(leave_data.get("submission_status_desc", "")).lower()
        status_lower = str(load_status).lower()

        # Put proportional scores in the same category column as the full-day
        # rule they came from.  This keeps 0.5/0.25 leave visible in the
        # official table instead of silently dropping an unrecognised value.
        score_column = None
        if abs(score_num) < 1e-9:
            score_column = 1
        elif "adjusted by hod" in score_desc:
            score_column = 4
        elif "informed hod by call" in score_desc:
            score_column = 7
        elif "not responded" in score_desc:
            score_column = 11
        elif "responded to hod" in score_desc:
            score_column = 9
        elif "load adjusted" in status_lower and "before" in score_desc:
            score_column = 3
        elif "load adjusted" in status_lower:
            score_column = 4
        elif "after" in score_desc:
            score_column = 2
        else:
            # Backward-compatible fallback for older PDF callers that pass
            # only the original full-day score.
            score_column = {
                -0.5: 2, -1.0: 3, -1.5: 4, -1.25: 5,
                -1.75: 7, -2.0: 9, -2.5: 11,
            }.get(round(score_num, 2), None)

        score_cells = [E] * 13
        if score_column is not None:
            score_cells[score_column] = P(score_text, S.s_score)
        score_cells[0] = P(bot_label, S.s_bot)
        t.append(score_cells)                                                   # 30

        t.append([P("Date", S.s_date_lab),                                    # 31
                  P(frm_disp, S.s_date_val), E,
                  P("( Signature of Applicant)", S.s_sig), E, E, E, E,
                  P("HOD Sign with Date", S.s_sig), E, E, E, E])
        t.append([E] * 13)                                                     # 32 hairline

        # ---- spans ----
        spans = [
            ((0, 0), (12, 0)), ((0, 1), (12, 1)),
            ((1, 3), (3, 3)), ((6, 3), (7, 3)), ((9, 3), (12, 3)),
            ((3, 5), (4, 5)), ((6, 5), (8, 5)), ((9, 5), (12, 5)),
            ((0, 7), (3, 7)),
            ((0, 8), (3, 8)), ((4, 8), (5, 8)),
            ((0, 9), (3, 9)), ((4, 9), (5, 9)),
            ((0, 10), (12, 10)), ((0, 11), (12, 11)), ((0, 32), (12, 32)),
            ((0, 13), (12, 13)),
            ((0, 15), (12, 15)), ((0, 16), (12, 16)),
            ((1, 17), (2, 17)), ((4, 17), (5, 17)), ((6, 17), (12, 17)),
            ((1, 18), (2, 18)), ((4, 18), (5, 18)), ((6, 18), (12, 18)),
        ]
        if has_arrangement:
            spans.extend([((1, 19), (2, 19)), ((4, 19), (5, 19)), ((6, 19), (12, 19))])
        else:
            spans.append(((0, 19), (12, 19)))

        spans.extend([
            ((1, 20), (2, 20)), ((4, 20), (5, 20)), ((6, 20), (12, 20)),
            ((1, 21), (2, 21)), ((4, 21), (5, 21)), ((6, 21), (12, 21)),
            ((1, 22), (2, 22)), ((4, 22), (5, 22)), ((6, 22), (12, 22)),
            ((0, 23), (12, 23)),
            ((1, 24), (2, 24)), ((3, 24), (12, 24)),
            ((3, 25), (4, 25)), ((5, 25), (12, 25)),
            ((5, 26), (6, 26)), ((7, 26), (12, 26)),
            ((5, 27), (6, 29)), ((7, 27), (8, 29)), ((9, 27), (12, 27)),
            ((9, 28), (10, 28)), ((9, 29), (10, 29)),
            ((1, 31), (2, 31)), ((3, 31), (5, 31)), ((8, 31), (9, 31)),
        ])

        # ---- horizontal lines: (row, col_from, col_to) ----
        hlines = [(r, 0, 12) for r in range(0, 24)]
        hlines += [(24, 1, 12), (25, 1, 12), (26, 1, 12),
                   (27, 9, 12), (28, 9, 12), (29, 0, 12), (30, 0, 12),
                   (31, 0, 12)]

        # ---- vertical lines: row -> cells whose RIGHT edge gets a line ----
        vlines = {
            2: list(range(0, 12)), 4: list(range(0, 12)),
            6: list(range(0, 12)), 12: list(range(0, 12)),
            14: list(range(0, 12)), 30: list(range(0, 12)),
            3: [0, 1, 4, 5, 6, 8],
            5: [0, 1, 2, 3, 5, 6],
            7: [0, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
            8: [0, 3, 5, 6, 7, 8, 9, 10, 11, 12],
            9: [0, 3, 5, 6, 7, 8, 9, 10, 11, 12],
            10: [0, 1, 2, 3, 5],
            17: [0, 2, 3, 5], 18: [0, 2, 3, 5], 20: [0, 2, 3, 5],
            21: [0, 2, 3, 5], 22: [0, 2, 3, 5],
            24: [0, 1], 25: [0, 1, 2, 3], 26: [0, 1, 2, 3, 4, 5],
            27: [0, 1, 2, 3, 4, 5, 7],
            28: [0, 1, 2, 3, 4, 9, 11],
            29: [0, 1, 2, 3, 4, 9, 11],
            31: [0, 1, 3, 6, 7, 8, 10, 11],
        }
        if has_arrangement:
            vlines[19] = [0, 2, 3, 5]

        # ---- fills ----
        fills = [
            ((1, 3), (3, 3), C_BLUE), ((6, 3), (7, 3), C_BLUE),
            ((9, 3), (12, 3), C_BLUE),
            ((1, 5), (1, 5), C_BLUE), ((3, 5), (4, 5), C_BLUE),
            ((6, 5), (8, 5), C_BLUE), ((9, 5), (12, 5), C_BLUE),
            ((0, 7), (3, 7), C_GREY), ((0, 8), (3, 9), C_GREY),
            ((0, 24), (0, 24), C_EBB), ((1, 24), (2, 24), C_DARKGREY),
            ((3, 24), (12, 24), C_BROWN),
            ((0, 25), (0, 25), C_EBB), ((1, 25), (1, 25), C_BLUEGREY),
            ((2, 25), (2, 25), C_LGREY2), ((3, 25), (4, 25), C_BLUEGREY),
            ((5, 25), (12, 25), C_LGREY2),
            ((0, 26), (0, 26), C_EBB), ((3, 26), (3, 26), C_YEL1),
            ((4, 26), (4, 26), C_GRN1), ((5, 26), (6, 26), C_YEL1),
            ((7, 26), (12, 26), C_GRN1),
            ((0, 27), (0, 29), C_EBB),
            ((5, 27), (6, 29), C_PURPLE), ((7, 27), (8, 29), C_LBLUE),
            ((9, 27), (12, 27), C_RED),
            ((9, 28), (10, 28), C_YEL2), ((11, 28), (11, 28), C_CYAN),
            ((0, 30), (0, 30), C_BLUE), ((1, 31), (2, 31), C_BLUE),
        ]
        if applied_col in LEAVE_COLS:  # red highlight on applied type
            c_idx = 4 + LEAVE_COLS.index(applied_col)
            fills.append(((c_idx, 7), (c_idx, 7), C_RED))

        style_cmds = [
            ("BOX", (0, 0), (-1, -1), GRID_W, C_BLACK),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("VALIGN", (0, 25), (0, 25), "BOTTOM"),
            ("LEFTPADDING", (0, 0), (-1, -1), 1.5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 1.5),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            # Cell-specific nudges MUST come after globals (later wins).
            # +0.7 x-bias ranges (Excel centers 0.7 right of ReportLab here).
            ("LEFTPADDING", (5, 7), (12, 9), 2.2),
            ("RIGHTPADDING", (5, 7), (12, 9), 0.8),
            ("BOTTOMPADDING", (0, 25), (0, 25), 5),
            ("VALIGN", (1, 28), (4, 28), "TOP"),
            ("TOPPADDING", (1, 28), (4, 28), 2),
            ("VALIGN", (9, 28), (10, 28), "TOP"),
            ("TOPPADDING", (9, 28), (10, 28), 5),
            ("VALIGN", (11, 28), (11, 28), "TOP"),
            ("TOPPADDING", (11, 28), (11, 28), 0),
            # ---- measured TOP anchors (gap 7.4pt = 1.76; scaled by size) ----
            ("VALIGN", (1, 3), (1, 3), "TOP"), ("TOPPADDING", (1, 3), (1, 3), -0.72),
            ("VALIGN", (6, 3), (6, 3), "TOP"), ("TOPPADDING", (6, 3), (6, 3), 0.24),
            ("VALIGN", (9, 3), (9, 3), "TOP"), ("TOPPADDING", (9, 3), (9, 3), 0.24),
            ("RIGHTPADDING", (0, 3), (0, 3), 1.3),
            ("RIGHTPADDING", (5, 3), (5, 3), 1.3),
            ("LEFTPADDING", (8, 3), (8, 3), 1.7),
            ("VALIGN", (0, 5), (9, 5), "TOP"), ("TOPPADDING", (0, 5), (9, 5), -0.76),
            ("RIGHTPADDING", (0, 5), (0, 5), 1.3),
            ("RIGHTPADDING", (2, 5), (2, 5), 1.2),
            ("VALIGN", (0, 7), (12, 7), "TOP"),
            ("TOPPADDING", (0, 7), (0, 7), -1.16),
            ("TOPPADDING", (5, 7), (12, 7), -0.66),
            ("VALIGN", (4, 7), (4, 7), "MIDDLE"),
            ("TOPPADDING", (4, 7), (4, 7), 0),
            ("BOTTOMPADDING", (4, 7), (4, 7), 0),
            ("VALIGN", (0, 8), (12, 9), "TOP"),
            ("TOPPADDING", (5, 8), (5, 8), 0.64),
            ("TOPPADDING", (6, 8), (12, 8), 1.14),
            ("TOPPADDING", (0, 8), (0, 8), 1.62),
            ("TOPPADDING", (5, 9), (5, 9), 0.84),
            ("TOPPADDING", (6, 9), (12, 9), 1.34),
            ("TOPPADDING", (0, 9), (0, 9), 1.82),
            ("VALIGN", (0, 11), (0, 11), "TOP"), ("TOPPADDING", (0, 11), (0, 11), -0.72),
            ("VALIGN", (0, 15), (0, 16), "TOP"),
            ("TOPPADDING", (0, 15), (0, 15), -0.76),
            ("TOPPADDING", (0, 16), (0, 16), -0.82),
            ("VALIGN", (0, 17), (12, 22), "MIDDLE"),
            ("TOPPADDING", (0, 17), (12, 22), 1.0),
            ("BOTTOMPADDING", (0, 17), (12, 22), 1.0),
            ("VALIGN", (0, 23), (0, 23), "TOP"), ("TOPPADDING", (0, 23), (0, 23), -0.58),
            ("LEFTPADDING", (0, 23), (0, 23), 4.7),
            ("VALIGN", (1, 24), (1, 24), "TOP"), ("TOPPADDING", (1, 24), (1, 24), 3.74),
            ("VALIGN", (3, 24), (3, 24), "TOP"), ("TOPPADDING", (3, 24), (3, 24), 3.84),
            ("VALIGN", (1, 25), (1, 25), "TOP"), ("TOPPADDING", (1, 25), (1, 25), 1.14),
            ("VALIGN", (2, 25), (2, 25), "TOP"), ("TOPPADDING", (2, 25), (2, 25), 10.34),
            ("VALIGN", (3, 25), (3, 25), "TOP"), ("TOPPADDING", (3, 25), (3, 25), 19.14),
            ("VALIGN", (5, 25), (5, 25), "TOP"), ("TOPPADDING", (5, 25), (5, 25), 22.88),
            ("VALIGN", (5, 26), (5, 26), "TOP"), ("TOPPADDING", (5, 26), (5, 26), -1.06),
            ("VALIGN", (7, 26), (7, 26), "TOP"), ("TOPPADDING", (7, 26), (7, 26), -1.36),
            ("VALIGN", (5, 27), (5, 27), "TOP"), ("TOPPADDING", (5, 27), (5, 27), 15.74),
            ("VALIGN", (7, 27), (7, 27), "TOP"), ("TOPPADDING", (7, 27), (7, 27), 11.24),
            ("VALIGN", (9, 27), (9, 27), "TOP"), ("TOPPADDING", (9, 27), (9, 27), 3.34),
            ("VALIGN", (9, 29), (9, 29), "TOP"), ("TOPPADDING", (9, 29), (9, 29), 0.84),
            ("VALIGN", (11, 29), (11, 29), "TOP"), ("TOPPADDING", (11, 29), (11, 29), 1.34),
            ("VALIGN", (3, 31), (3, 31), "BOTTOM"),
            ("BOTTOMPADDING", (3, 31), (3, 31), 1.15),
            ("VALIGN", (8, 31), (8, 31), "BOTTOM"),
            ("BOTTOMPADDING", (8, 31), (8, 31), 1.15),
            # ---- +0.7 x-shift cells ----
            ("LEFTPADDING", (1, 3), (1, 3), 2.2), ("RIGHTPADDING", (1, 3), (1, 3), 0.8),
            ("LEFTPADDING", (6, 3), (6, 3), 2.2), ("RIGHTPADDING", (6, 3), (6, 3), 0.8),
            ("LEFTPADDING", (9, 3), (9, 3), 2.2), ("RIGHTPADDING", (9, 3), (9, 3), 0.8),
            ("LEFTPADDING", (1, 5), (1, 5), 2.2), ("RIGHTPADDING", (1, 5), (1, 5), 0.8),
            ("LEFTPADDING", (3, 5), (3, 5), 2.2), ("RIGHTPADDING", (3, 5), (3, 5), 0.8),
            ("LEFTPADDING", (5, 5), (6, 5), 2.2), ("RIGHTPADDING", (5, 5), (6, 5), 0.8),
            ("LEFTPADDING", (9, 5), (9, 5), 2.2), ("RIGHTPADDING", (9, 5), (9, 5), 0.8),
            ("LEFTPADDING", (0, 11), (0, 11), 2.2), ("RIGHTPADDING", (0, 11), (0, 11), 0.8),
            ("LEFTPADDING", (0, 15), (0, 16), 2.2), ("RIGHTPADDING", (0, 15), (0, 16), 0.8),
            ("LEFTPADDING", (1, 24), (1, 24), 2.2), ("RIGHTPADDING", (1, 24), (1, 24), 0.8),
            ("LEFTPADDING", (5, 25), (5, 25), 2.2), ("RIGHTPADDING", (5, 25), (5, 25), 0.8),
            ("LEFTPADDING", (5, 26), (5, 26), 2.2), ("RIGHTPADDING", (5, 26), (5, 26), 0.8),
            ("LEFTPADDING", (7, 26), (7, 26), 2.2), ("RIGHTPADDING", (7, 26), (7, 26), 0.8),
            ("LEFTPADDING", (9, 27), (9, 27), 2.2), ("RIGHTPADDING", (9, 27), (9, 27), 0.8),
            ("LEFTPADDING", (1, 28), (2, 28), 2.2), ("RIGHTPADDING", (1, 28), (2, 28), 0.8),
            ("LEFTPADDING", (4, 28), (4, 28), 2.2), ("RIGHTPADDING", (4, 28), (4, 28), 0.8),
            ("LEFTPADDING", (9, 28), (9, 28), 2.2), ("RIGHTPADDING", (9, 28), (9, 28), 0.8),
        ]
        for a, b in spans:
            style_cmds.append(("SPAN", a, b))
        for r, c0, c1 in hlines:
            style_cmds.append(("LINEBELOW", (c0, r), (c1, r), GRID_W,
                               C_BLACK))
        for r, cells in vlines.items():
            for c in cells:
                style_cmds.append(("LINEAFTER", (c, r), (c, r), GRID_W,
                                   C_BLACK))
        for a, b, col in fills:
            style_cmds.append(("BACKGROUND", a, b, col))

        table = Table(t, colWidths=COLS, rowHeights=ROWS)
        table.setStyle(TableStyle(style_cmds))

        # The official form has five alternate-workload rows on page 1.
        # Continue with the same official section/grid on additional A4
        # portrait pages.  This deliberately avoids a separate appendix
        # table, so every adjustment remains in the official row format.
        continuation_pages = []
        extra_adjustments = load_adjustments[5:] if has_arrangement else []
        if extra_adjustments:
            continuation_row_heights = [24, 20, 20, 24, 24, 24, 24, 24, 24]
            for page_no, start in enumerate(range(0, len(extra_adjustments), 5), 1):
                page_rows = extra_adjustments[start:start + 5]
                continuation_rows = [
                    [P("LEAVE APPLICATION FORM (CONTINUED)", S.s_title)] + [E] * 12,
                    [P("For Department Use Only (Alternate Load Arrangement)", S.s_dept)] + [E] * 12,
                    [P("Alternate arrangement of work load is made as below:", S.s_alt)] + [E] * 12,
                    [P("Date", S.s_load_h), P("Subject", S.s_load_h), E,
                     P("Sem", S.s_load_h), P("Time", S.s_load_h), E,
                     P("Staff member who will engage the work", S.s_load_h), E, E, E, E, E],
                ]
                continuation_rows.extend(
                    [_format_load_row(adj) for adj in page_rows]
                )
                while len(continuation_rows) < len(continuation_row_heights):
                    continuation_rows.append([E] * 13)

                continuation_spans = [
                    ((0, 0), (12, 0)), ((0, 1), (12, 1)), ((0, 2), (12, 2)),
                    ((1, 3), (2, 3)), ((4, 3), (5, 3)), ((6, 3), (12, 3)),
                ]
                continuation_spans.extend(
                    [item
                     for row in range(4, 9)
                     for item in (
                         ((1, row), (2, row)),
                         ((4, row), (5, row)),
                         ((6, row), (12, row)),
                     )]
                )
                continuation_vlines = {
                    3: [0, 2, 3, 5],
                    4: [0, 2, 3, 5], 5: [0, 2, 3, 5],
                    6: [0, 2, 3, 5], 7: [0, 2, 3, 5],
                    8: [0, 2, 3, 5],
                }
                continuation_style = [
                    ("BOX", (0, 0), (-1, -1), GRID_W, C_BLACK),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 1.5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 1.5),
                    ("TOPPADDING", (0, 0), (-1, -1), 1),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                    ("BACKGROUND", (0, 0), (12, 0), C_BLUE),
                    ("VALIGN", (0, 1), (12, 2), "TOP"),
                    ("TOPPADDING", (0, 1), (12, 1), 4),
                    ("TOPPADDING", (0, 2), (12, 2), 3),
                ]
                continuation_style.extend(
                    [("SPAN", a, b) for a, b in continuation_spans]
                )
                for row in range(len(continuation_rows)):
                    continuation_style.append(("LINEBELOW", (0, row), (12, row), GRID_W, C_BLACK))
                for row, cells in continuation_vlines.items():
                    for col in cells:
                        continuation_style.append(("LINEAFTER", (col, row), (col, row), GRID_W, C_BLACK))

                continuation_table = Table(
                    continuation_rows,
                    colWidths=COLS,
                    rowHeights=continuation_row_heights,
                )
                continuation_table.setStyle(TableStyle(continuation_style))
                continuation_pages.extend([PageBreak(), continuation_table])

        doc = SimpleDocTemplate(filepath, pagesize=A4,
                                leftMargin=LEFT_MARGIN,
                                rightMargin=RIGHT_MARGIN, topMargin=TOP_MARGIN,
                                bottomMargin=20, title="Leave Application Form",
                                author="LJIET Leave Bot")
        doc.build([table] + continuation_pages)
        return filepath, filename


if __name__ == "__main__":
    gen = LeavePDFGenerator()
    test_data = {
        "empid": "00000365",
        "emp_name": "MILAN PATEL",
        "department": "FY1",
        "position": "AP",
        "leave_type": "CL",
        "from_date": "22/04/2026",
        "to_date": "22/04/2026",
        "total_days": 1,
        "load_subject": "JAVA-II",
        "load_sem": "II",
        "load_time": "11:30 AM TO 1:30 PM",
        "load_engager": "DJU (MATHS-II)",
        "load_status": "Load Adjusted",
    }
    fpath, fname = gen.generate_pdf(test_data)
    print("Generated:", fpath)
