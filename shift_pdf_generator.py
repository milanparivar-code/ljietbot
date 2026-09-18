import os
import re
from datetime import datetime
from timezone_utils import get_ist_now, get_ist_today_str
import pymupdf
from pdf_generator import get_faculty_shortname

# Complete ARS portal shift catalog scraped from ShException.asp
SHIFT_CATALOG = {
    "T2": {"code": "T2", "name": "T2", "start": "08:15", "end": "15:00", "display": "8:15 AM TO 3:00 PM (T2)", "category": "Teaching"},
    "T12": {"code": "T12", "name": "T12", "start": "09:15", "end": "16:00", "display": "9:15 AM TO 4:00 PM (T12)", "category": "Teaching"},
    "T1": {"code": "T1", "name": "T1", "start": "09:45", "end": "17:00", "display": "9:45 AM TO 5:00 PM (T1)", "category": "Teaching"},
    "T3": {"code": "T3", "name": "T3", "start": "08:15", "end": "16:00", "display": "8:15 AM TO 4:00 PM (T3)", "category": "Teaching"},
    "T4": {"code": "T4", "name": "T4", "start": "10:45", "end": "17:30", "display": "10:45 AM TO 5:30 PM (T4)", "category": "Teaching"},
    "T6": {"code": "T6", "name": "T6", "start": "10:15", "end": "17:00", "display": "10:15 AM TO 5:00 PM (T6)", "category": "Teaching"},
    "T7": {"code": "T7", "name": "T7", "start": "08:45", "end": "15:45", "display": "8:45 AM TO 3:45 PM (T7)", "category": "Teaching"},
    "T8": {"code": "T8", "name": "T8", "start": "08:45", "end": "15:00", "display": "8:45 AM TO 3:00 PM (T8)", "category": "Teaching"},
    "T9": {"code": "T9", "name": "T9", "start": "11:45", "end": "19:00", "display": "11:45 AM TO 7:00 PM (T9)", "category": "Teaching"},
    "T13": {"code": "T13", "name": "T13", "start": "07:30", "end": "14:15", "display": "7:30 AM TO 2:15 PM (T13)", "category": "Teaching"},
    "T14": {"code": "T14", "name": "T14", "start": "09:30", "end": "14:30", "display": "9:30 AM TO 2:30 PM (T14)", "category": "Teaching"},
    "T15": {"code": "T15", "name": "T15", "start": "11:30", "end": "18:30", "display": "11:30 AM TO 6:30 PM (T15)", "category": "Teaching"},
    "T16": {"code": "T16", "name": "T16", "start": "09:30", "end": "16:15", "display": "9:30 AM TO 4:15 PM (T16)", "category": "Teaching"},
    "T17": {"code": "T17", "name": "T17", "start": "12:15", "end": "19:00", "display": "12:15 PM TO 7:00 PM (T17)", "category": "Teaching"},
    "T18": {"code": "T18", "name": "T18", "start": "07:00", "end": "13:45", "display": "7:00 AM TO 1:45 PM (T18)", "category": "Teaching"},
    "Ta5": {"code": "Ta5", "name": "Ta5", "start": "08:15", "end": "15:30", "display": "8:15 AM TO 3:30 PM (Ta5)", "category": "Teaching"},
    "Ta10": {"code": "Ta10", "name": "Ta10", "start": "09:00", "end": "16:15", "display": "9:00 AM TO 4:15 PM (Ta10)", "category": "Teaching"},
    "Ta11": {"code": "Ta11", "name": "Ta11", "start": "10:15", "end": "17:30", "display": "10:15 AM TO 5:30 PM (Ta11)", "category": "Teaching"},
    "Ta12": {"code": "Ta12", "name": "Ta12", "start": "07:00", "end": "14:15", "display": "7:00 AM TO 2:15 PM (Ta12)", "category": "Teaching"},
    "Nt1": {"code": "Nt1", "name": "Nt1", "start": "08:30", "end": "16:30", "display": "8:30 AM TO 4:30 PM (Nt1)", "category": "Non-Teaching"},
    "Nt2": {"code": "Nt2", "name": "Nt2", "start": "08:00", "end": "16:00", "display": "8:00 AM TO 4:00 PM (Nt2)", "category": "Non-Teaching"},
    "Nt3": {"code": "Nt3", "name": "Nt3", "start": "09:30", "end": "17:30", "display": "9:30 AM TO 5:30 PM (Nt3)", "category": "Non-Teaching"},
    "Nt4": {"code": "Nt4", "name": "Nt4", "start": "07:30", "end": "15:30", "display": "7:30 AM TO 3:30 PM (Nt4)", "category": "Non-Teaching"},
    "Nt5": {"code": "Nt5", "name": "Nt5", "start": "10:00", "end": "18:00", "display": "10:00 AM TO 6:00 PM (Nt5)", "category": "Non-Teaching"},
    "Nt6": {"code": "Nt6", "name": "Nt6", "start": "08:00", "end": "15:30", "display": "8:00 AM TO 3:30 PM (Nt6)", "category": "Non-Teaching"},
    "Nt7": {"code": "Nt7", "name": "Nt7", "start": "10:00", "end": "19:00", "display": "10:00 AM TO 7:00 PM (Nt7)", "category": "Non-Teaching"},
    "Nt8": {"code": "Nt8", "name": "Nt8", "start": "09:15", "end": "17:15", "display": "9:15 AM TO 5:15 PM (Nt8)", "category": "Non-Teaching"},
    "Nt9": {"code": "Nt9", "name": "Nt9", "start": "08:45", "end": "16:45", "display": "8:45 AM TO 4:45 PM (Nt9)", "category": "Non-Teaching"},
    "Nt10": {"code": "Nt10", "name": "Nt10", "start": "09:45", "end": "17:45", "display": "9:45 AM TO 5:45 PM (Nt10)", "category": "Non-Teaching"},
    "Nt11": {"code": "Nt11", "name": "Nt11", "start": "09:00", "end": "17:00", "display": "9:00 AM TO 5:00 PM (Nt11)", "category": "Non-Teaching"},
    "Nt13": {"code": "Nt13", "name": "Nt13", "start": "10:30", "end": "17:30", "display": "10:30 AM TO 5:30 PM (Nt13)", "category": "Non-Teaching"},
    "Nt14": {"code": "Nt14", "name": "Nt14", "start": "11:30", "end": "19:30", "display": "11:30 AM TO 7:30 PM (Nt14)", "category": "Non-Teaching"},
    "Nt15": {"code": "Nt15", "name": "Nt15", "start": "10:30", "end": "18:30", "display": "10:30 AM TO 6:30 PM (Nt15)", "category": "Non-Teaching"},
    "Nt16": {"code": "Nt16", "name": "Nt16", "start": "10:15", "end": "18:15", "display": "10:15 AM TO 6:15 PM (Nt16)", "category": "Non-Teaching"}
}

TEMPLATE_FILENAME = "SHIFT CHANGE_biometric number_Date_Name of Faculty_Dept name (2).pdf"


def get_shift_display(shift_code_or_str: str) -> str:
    """Returns formatted shift string like '9:15 AM TO 4:00 PM (T12)'."""
    if not shift_code_or_str:
        return ""
    code = shift_code_or_str.strip()
    if code in SHIFT_CATALOG:
        return SHIFT_CATALOG[code]["display"]
    # Case insensitive lookup
    for k, v in SHIFT_CATALOG.items():
        if k.lower() == code.lower():
            return v["display"]
    return code


def parse_date_parts(date_str: str):
    """Parses date string ('DD/MM/YYYY' or 'YYYY-MM-DD') into (day, month, year)."""
    if not date_str:
        now = get_ist_now()
        return now.strftime("%d"), now.strftime("%m"), now.strftime("%Y")
    d_str = str(date_str).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            dt = datetime.strptime(d_str, fmt)
            return dt.strftime("%d"), dt.strftime("%m"), dt.strftime("%Y")
        except ValueError:
            continue
    parts = re.split(r"[/ -]", d_str)
    if len(parts) >= 3:
        return parts[0].zfill(2), parts[1].zfill(2), parts[2]
    now = get_ist_now()
    return now.strftime("%d"), now.strftime("%m"), now.strftime("%Y")


def format_filename_date(date_str: str) -> str:
    """Formats 'DD/MM/YYYY' into '30-July-26'."""
    day, month, year = parse_date_parts(date_str)
    if len(year) == 2:
        year = "20" + year
    try:
        dt = datetime.strptime(f"{day}/{month}/{year}", "%d/%m/%Y")
        month_full = dt.strftime("%B")  # 'July'
        year_short = dt.strftime("%y")  # '26'
        return f"{day}-{month_full}-{year_short}"
    except Exception:
        return f"{day}-{month}-{year}"


def get_shift_change_filename(shift_data: dict) -> str:
    """
    Constructs the exact filename:
    SHIFT CHANGE_{biometric}_{Date}_{FacultyShortname}_{Dept}.pdf
    Sample: SHIFT CHANGE_00000365 _30-July-26_MDP_FY1.pdf
    """
    emp_code = str(shift_data.get("emp_code") or shift_data.get("username") or "00000365").strip()
    frm_dt = str(shift_data.get("from_date") or shift_data.get("date") or get_ist_today_str("%d/%m/%Y")).strip()
    to_dt = str(shift_data.get("to_date") or frm_dt).strip()
    emp_name = str(shift_data.get("emp_name") or "Milan Patel").strip()
    dept = str(shift_data.get("department") or "FY1").strip()

    shortname = shift_data.get("short_name") or shift_data.get("initials")
    if not shortname:
        shortname = get_faculty_shortname(emp_name)
    date_part = format_filename_date(frm_dt)
    if to_dt and to_dt != frm_dt:
        date_part = f"{date_part}_to_{format_filename_date(to_dt)}"

    return f"SHIFT CHANGE_{emp_code}_{date_part}_{shortname}_{dept}.pdf"


def generate_shift_change_pdf(shift_data: dict, output_dir: str = "generated_pdfs") -> tuple[str, str]:
    """
    Generates the official Shift Change application PDF using the official company template.
    Returns (full_file_path, filename).
    """
    os.makedirs(output_dir, exist_ok=True)
    filename = get_shift_change_filename(shift_data)
    output_path = os.path.join(output_dir, filename)

    # Locate base template
    template_path = TEMPLATE_FILENAME
    if not os.path.exists(template_path):
        # Look in current directory or parent
        alt_paths = [
            os.path.join(os.path.dirname(__file__), TEMPLATE_FILENAME),
            os.path.join(os.getcwd(), TEMPLATE_FILENAME),
            os.path.join(os.path.dirname(__file__), "shift_template.pdf"),
        ]
        for p in alt_paths:
            if os.path.exists(p):
                template_path = p
                break

    if not os.path.exists(template_path):
        raise FileNotFoundError(f"Shift change template PDF '{TEMPLATE_FILENAME}' not found.")

    doc = pymupdf.open(template_path)
    page = doc[0]

    # Locate handwritten bold font
    font_paths = [
        os.path.join(os.path.dirname(__file__), "fonts", "SegoePrintBold.ttf"),
        os.path.join(os.getcwd(), "fonts", "SegoePrintBold.ttf"),
        r"C:\Windows\Fonts\segoeprb.ttf",
        r"C:\Windows\Fonts\comicbd.ttf",
        r"C:\Windows\Fonts\Inkfree.ttf",
    ]
    hw_font_path = None
    for fp in font_paths:
        if os.path.exists(fp):
            hw_font_path = fp
            break

    fontname = "hw_bold"
    if hw_font_path:
        page.insert_font(fontname=fontname, fontfile=hw_font_path)
        font_obj = pymupdf.Font(fontfile=hw_font_path)
    else:
        fontname = "helv-bold"
        font_obj = pymupdf.Font("helv-bold")

    fontsize = 12.5
    text_color = (0, 0, 0)  # Pure black to match official handwritten submissions

    def draw_centered(y: float, text: str, x1: float = 195.0, x2: float = 535.0):
        if not text:
            return
        t_len = font_obj.text_length(text, fontsize=fontsize)
        cx = (x1 + x2) / 2.0
        x = max(x1, cx - (t_len / 2.0))
        page.insert_text(pymupdf.Point(x, y), text, fontname=fontname, fontsize=fontsize, color=text_color)

    # 1. Application Date (top right)
    app_date = str(shift_data.get("app_date") or get_ist_today_str("%d/%m/%Y"))
    d, m, y = parse_date_parts(app_date)
    app_date_disp = f"{d}/{m}/{y[-2:]}"
    page.insert_text(pymupdf.Point(480, 107), app_date_disp, fontname=fontname, fontsize=fontsize, color=text_color)

    # 2. Name of Institute (Centered on line)
    institute = str(shift_data.get("institute") or "LJIET").strip()
    draw_centered(150, institute)

    # 3. Name of Employee (Centered on line)
    emp_name = str(shift_data.get("emp_name") or "Milan D. Patel").strip()
    draw_centered(174, emp_name)

    # 4. Employee ID / Biometric No. (Centered on line from x=240 to x=535)
    emp_code = str(shift_data.get("emp_code") or shift_data.get("username") or "00000365").strip()
    draw_centered(198, emp_code, x1=240.0, x2=535.0)

    # 5. Designation (Centered on line)
    designation = str(shift_data.get("designation") or "Asst. Professor").strip()
    draw_centered(222, designation)

    # 6. Department (Centered on line)
    dept = str(shift_data.get("department") or "FY1").strip()
    draw_centered(246, dept)

    # 7. Current Shift Time (Centered on line)
    cur_shift = shift_data.get("current_shift") or "T2"
    cur_shift_disp = get_shift_display(cur_shift)
    draw_centered(270, cur_shift_disp)

    # 8. New Shift Time (Centered on line)
    new_shift = shift_data.get("new_shift") or "T12"
    new_shift_disp = get_shift_display(new_shift)
    draw_centered(294, new_shift_disp)

    # 9. New Shift Start From : __ / __ / ____    To __ / __ / ____
    frm_dt = str(shift_data.get("from_date") or shift_data.get("date") or get_ist_today_str("%d/%m/%Y")).strip()
    to_dt = str(shift_data.get("to_date") or frm_dt).strip()

    frm_d, frm_m, frm_y = parse_date_parts(frm_dt)
    to_d, to_m, to_y = parse_date_parts(to_dt)

    page.insert_text(pymupdf.Point(190, 318), frm_d, fontname=fontname, fontsize=fontsize, color=text_color)
    page.insert_text(pymupdf.Point(220, 318), frm_m, fontname=fontname, fontsize=fontsize, color=text_color)
    page.insert_text(pymupdf.Point(248, 318), frm_y, fontname=fontname, fontsize=fontsize, color=text_color)

    page.insert_text(pymupdf.Point(333, 318), to_d, fontname=fontname, fontsize=fontsize, color=text_color)
    page.insert_text(pymupdf.Point(363, 318), to_m, fontname=fontname, fontsize=fontsize, color=text_color)
    page.insert_text(pymupdf.Point(391, 318), to_y, fontname=fontname, fontsize=fontsize, color=text_color)

    # 10. Employee Reason (Centered on line)
    reason = str(shift_data.get("reason") or "Academic Work").strip()
    draw_centered(342, reason)

    # 11. Director's Remark: Left completely empty for physical entry as requested
    # (Do not print any text here)

    doc.save(output_path)
    doc.close()
    return output_path, filename
