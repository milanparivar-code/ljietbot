"""
timetable_engine.py
===================
Automated & Dynamic Teaching Load Adjustment Engine for Telegram Leave Bot.
Supports:
  1. Dynamic File Discovery: Auto-detects and loads latest timetable revisions (FY1..FY5)
     and Daily Internship DR workbooks using pattern matching (handles V1, V2, WEF_dates, etc.).
  2. Zero-Downtime Hot-Reloading: Automatically checks file modification timestamps (`mtime`)
     and re-indexes data instantly when any file is updated, added, or replaced.
  3. Adaptive Schema Parsing: Dynamically locates division names, day/lecture header rows,
     and column layouts across different workbook versions.
  4. Strict Operational Rules:
     - Whitelist: Only faculty who already teach in that division can substitute (NO new faculty).
     - No self-substitution and subject continuity for every proxy/cascade step.
     - Subject Continuity: Substitute takes a lecture of their own subject in that division.
     - Configurable maximum lectures per subject in each division.
     - Configurable Merged Lectures toggle (only same-time/lecture divisions the faculty teaches).
     - Exhaustive direct, merged, and arbitrary-depth cascade/swap search before ranking.
     - ES is a structural two-division unit; singleton ES rows and ES faculty proxies for
       ordinary single-division classes are never proposed.
  5. In-Bot Upload & GitHub Sync integration.
"""

import os
import re
import glob
import json
import logging
from datetime import datetime, date, timedelta
from timezone_utils import get_ist_now, get_ist_today
from collections import defaultdict
import openpyxl

logger = logging.getLogger(__name__)

WEEKDAY_MAP = {
    0: "MON",
    1: "TUE",
    2: "WED",
    3: "THU",
    4: "FRI",
    5: "SAT",
    6: "SUN"
}

DAY_NAME_TO_CODE = {
    "MON": "MON", "MONDAY": "MON",
    "TUE": "TUE", "TUESDAY": "TUE",
    "WED": "WED", "WEDNESDAY": "WED",
    "THU": "THU", "THURSDAY": "THU",
    "FRI": "FRI", "FRIDAY": "FRI",
    "SAT": "SAT", "SATURDAY": "SAT",
    "SUN": "SUN", "SUNDAY": "SUN",
}

# Regex patterns for auto-discovering departmental workbooks
DEPT_PATTERNS = {
    "FY1": [r"FY[-_ ]*1", r"SEM[-_ ]*I.*FY1", r"\bFY1\b"],
    "FY2": [r"FY[-_ ]*2", r"SEM[-_ ]*I.*FY2", r"\bFY2\b"],
    "FY3": [r"FY[-_ ]*3", r"SEM[-_ ]*I.*FY3", r"\bFY3\b"],
    "FY4": [r"FY[-_ ]*4", r"SEM[-_ ]*I.*FY4", r"\bFY4\b"],
    "FY5": [r"FY[-_ ]*5", r"SEM[-_ ]*I.*FY5", r"\bFY5\b"],
}

# Patterns for discovering Daily Internship DR workbooks
DR_PATTERNS = [
    r"INTERNSHIP",
    r"DAILY.*REPORT",
    r"EXTRA.*ACTIVITY",
    r"CENTRAL.*ACTIVITY",
    r"\bDR\b"
]


class TimetableEngine:
    _instance = None

    def __init__(self, base_dir: str = None):
        self.base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
        self.division_faculty = defaultdict(dict)       # div -> {fac_initial: subject}
        self.division_faculty_subjects = defaultdict(lambda: defaultdict(set))
        self.faculty_divisions = defaultdict(set)       # fac_initial -> set(divisions)
        self.schedule_by_slot = defaultdict(list)       # (day, lec_no) -> [entry]
        self.faculty_slot_entries = defaultdict(list)   # (fac_initial, day, lec_no) -> [entry]
        self.initial_to_faculty = {}                    # fac_initial -> info_dict
        self.name_to_initial = {}                       # norm_name -> fac_initial
        self.all_divisions = []
        self.all_faculty_initials = set()

        # Dynamic tracking
        self.loaded_files = {}          # category -> filepath
        self.loaded_mtimes = {}         # category -> mtime
        self.last_sync_time = None
        self.is_loaded = False
        self.load()

    @classmethod
    def get_instance(cls, base_dir: str = None):
        if cls._instance is None:
            cls._instance = TimetableEngine(base_dir)
        return cls._instance

    def discover_files(self) -> tuple[dict, str]:
        """
        Dynamically scans base_dir and any subdirectories for *.xlsx files.
        Selects the highest version or most recently modified file for each dept and DR.
        """
        search_dirs = [self.base_dir]
        sub_tt = os.path.join(self.base_dir, "timetables")
        if os.path.isdir(sub_tt):
            search_dirs.append(sub_tt)

        candidates = []
        for d in search_dirs:
            candidates.extend(glob.glob(os.path.join(d, "*.xlsx")))

        dept_candidates = defaultdict(list)
        dr_candidates = []

        for fpath in candidates:
            fname = os.path.basename(fpath)
            # Skip temporary/lock files
            if fname.startswith("~$") or fname.startswith("."):
                continue

            # Check DR
            if any(re.search(p, fname, re.IGNORECASE) for p in DR_PATTERNS):
                dr_candidates.append(fpath)
                continue

            # Check Depts
            for dept, pats in DEPT_PATTERNS.items():
                if any(re.search(p, fname, re.IGNORECASE) for p in pats):
                    dept_candidates[dept].append(fpath)
                    break

        def _get_sort_key(filepath):
            fname = os.path.basename(filepath)
            # Extract version number if present (e.g., V1.2 -> 1.2, V2 -> 2.0)
            v_match = re.search(r"\bV\s*(\d+(?:\.\d+)?)", fname, re.IGNORECASE)
            version = float(v_match.group(1)) if v_match else 1.0
            mtime = os.path.getmtime(filepath) if os.path.exists(filepath) else 0
            return (version, mtime)

        discovered_depts = {}
        for dept, flist in dept_candidates.items():
            flist.sort(key=_get_sort_key, reverse=True)
            discovered_depts[dept] = flist[0]

        discovered_dr = None
        if dr_candidates:
            dr_candidates.sort(key=_get_sort_key, reverse=True)
            discovered_dr = dr_candidates[0]

        return discovered_depts, discovered_dr

    def ensure_up_to_date(self) -> bool:
        """
        Checks if any timetable or DR file has changed or if new files were added.
        If changed, triggers a dynamic reload. Returns True if reloaded.
        """
        dept_files, dr_file = self.discover_files()
        needs_reload = False

        # Compare the complete discovered set so removed/replaced workbooks
        # also invalidate every derived index.
        loaded_depts = {k: v for k, v in self.loaded_files.items() if k != "DR"}
        if set(loaded_depts) != set(dept_files):
            needs_reload = True

        # Check department files
        for dept, fpath in dept_files.items():
            if dept not in self.loaded_files or self.loaded_files[dept] != fpath:
                needs_reload = True
                break
            cur_mtime = os.path.getmtime(fpath) if os.path.exists(fpath) else 0
            if cur_mtime != self.loaded_mtimes.get(dept, 0):
                needs_reload = True
                break

        # Check DR file
        if dr_file:
            if self.loaded_files.get("DR") != dr_file:
                needs_reload = True
            else:
                cur_dr_mtime = os.path.getmtime(dr_file) if os.path.exists(dr_file) else 0
                if cur_dr_mtime != self.loaded_mtimes.get("DR", 0):
                    needs_reload = True
        elif self.loaded_files.get("DR"):
            needs_reload = True

        if needs_reload:
            logger.info("Changes detected in timetable spreadsheets. Hot-reloading...")
            self.load()
            return True
        return False

    def reload(self) -> dict:
        """Explicitly forces a reload and returns summary of loaded datasets."""
        self.load()
        return self.get_summary()

    def get_summary(self) -> dict:
        """Returns metadata summary of active timetables and rosters."""
        return {
            "departments": {dept: os.path.basename(path) for dept, path in self.loaded_files.items() if dept != "DR"},
            "dr_file": os.path.basename(self.loaded_files.get("DR", "")),
            "total_divisions": len(self.division_faculty),
            "divisions": list(self.division_faculty.keys()),
            "timetable_faculty_count": len(self.all_faculty_initials),
            "dr_faculty_count": len(self.initial_to_faculty),
            "last_sync_time": self.last_sync_time.strftime("%Y-%m-%d %H:%M:%S") if self.last_sync_time else None
        }

    def get_status(self) -> dict:
        """Alias for get_summary()."""
        return self.get_summary()

    def load(self):
        """Discovers and parses all timetables and DR directory."""
        dept_files, dr_file = self.discover_files()

        # Reset in-memory data structures
        self.division_faculty = defaultdict(dict)
        self.division_faculty_subjects = defaultdict(lambda: defaultdict(set))
        self.faculty_divisions = defaultdict(set)
        self.schedule_by_slot = defaultdict(list)
        self.faculty_slot_entries = defaultdict(list)
        self.initial_to_faculty = {}
        self.name_to_initial = {}
        self.all_divisions = []
        self.all_faculty_initials = set()
        self.loaded_files = {}
        self.loaded_mtimes = {}

        # 1. Load DR Roster
        if dr_file and os.path.exists(dr_file):
            self._load_dr_roster(dr_file)
            self.loaded_files["DR"] = dr_file
            self.loaded_mtimes["DR"] = os.path.getmtime(dr_file)

        # 2. Load Master Timetables
        for dept, fpath in dept_files.items():
            if os.path.exists(fpath):
                self._load_master_timetable(dept, fpath)
                self.loaded_files[dept] = fpath
                self.loaded_mtimes[dept] = os.path.getmtime(fpath)

        self.last_sync_time = get_ist_now()
        self.is_loaded = True
        logger.info(
            f"TimetableEngine dynamic load complete: {len(self.division_faculty)} divisions, "
            f"{len(self.all_faculty_initials)} timetable initials, {len(self.initial_to_faculty)} DR profiles."
        )

    def _load_dr_roster(self, dr_path: str):
        """Parses the Daily Internship DR file to build faculty lookup."""
        try:
            wb = openpyxl.load_workbook(dr_path, read_only=True, data_only=True)
            # Find candidate day sheet
            target_sheet = None
            day_sheet_regex = r"(MON|TUE|WED|THU|FRI|SAT|SUN)"
            for sname in wb.sheetnames:
                if re.search(day_sheet_regex, sname, re.IGNORECASE):
                    target_sheet = wb[sname]
                    break
            if target_sheet is None and wb.sheetnames:
                target_sheet = wb[wb.sheetnames[0]]

            # Find header row dynamically (rows 1..10)
            header_row_idx = 3
            for r_idx, r in enumerate(target_sheet.iter_rows(min_row=1, max_row=10, max_col=10, values_only=True), 1):
                row_str = " ".join([str(x or "").lower() for x in r])
                if "sr" in row_str and ("name" in row_str or "initial" in row_str):
                    header_row_idx = r_idx
                    break

            for r in target_sheet.iter_rows(min_row=header_row_idx + 1, max_row=target_sheet.max_row, max_col=5, values_only=True):
                sr = r[0]
                name_str = r[1]
                init_str = r[2]
                if sr is not None and str(sr).strip().isdigit() and name_str and init_str:
                    parts = [p.strip() for p in str(name_str).split(",") if p.strip()]
                    full_name = parts[0] if parts else str(name_str).strip()
                    doj = parts[1] if len(parts) > 1 else ""
                    mobile = parts[2] if len(parts) > 2 else ""

                    init_parts = [p.strip() for p in str(init_str).split("-") if p.strip()]
                    initials = init_parts[0] if init_parts else str(init_str).strip()
                    hod = init_parts[1] if len(init_parts) > 1 else ""
                    dept = init_parts[2] if len(init_parts) > 2 else ""

                    clean_init = initials.upper().replace(" ", "")
                    fac_obj = {
                        "sr": int(str(sr).strip()),
                        "name": full_name,
                        "initials": clean_init,
                        "hod": hod.upper(),
                        "dept": dept.upper(),
                        "doj": doj,
                        "mobile": mobile
                    }
                    self.initial_to_faculty[clean_init] = fac_obj
                    norm_name = re.sub(r"\s+", " ", full_name.upper().strip())
                    self.name_to_initial[norm_name] = clean_init
            wb.close()
        except Exception as e:
            logger.error(f"Error reading DR roster from {dr_path}: {e}")

    def _load_master_timetable(self, dept: str, fpath: str):
        """Adaptively parses a master timetable workbook."""
        try:
            wb = openpyxl.load_workbook(fpath, read_only=True, data_only=True)
            # Find TT sheet
            target_sheet_name = None
            if "TT" in wb.sheetnames:
                target_sheet_name = "TT"
            else:
                for s in wb.sheetnames:
                    if "TT" in s.upper() and "FACULTY" not in s.upper() and "CLASS" not in s.upper():
                        target_sheet_name = s
                        break
            if not target_sheet_name:
                target_sheet_name = wb.sheetnames[0]

            ws = wb[target_sheet_name]

            # 1. Adaptively find division row and header row (rows 1..15)
            div_row_idx = 5
            header_row_idx = 7

            for r_idx, r in enumerate(ws.iter_rows(min_row=1, max_row=15, max_col=15, values_only=True), 1):
                row_str = " ".join([str(x or "").upper() for x in r])
                if "DIVISION" in row_str:
                    div_row_idx = r_idx
                if "DAY" in row_str and ("LECTURE" in row_str or "TIME" in row_str):
                    header_row_idx = r_idx

            # 2. Extract divisions from division row
            r_div = list(ws.iter_rows(min_row=div_row_idx, max_row=div_row_idx, values_only=True))[0]
            div_cols = {}
            for c in range(3, len(r_div), 4):
                val = r_div[c]
                if val and str(val).strip() and str(val).strip() not in ["BRANCH", "DIVISION", "V 1.0", "V 1.1", "V 1.2", "V 2.0"]:
                    div_name = str(val).strip()
                    div_cols[c] = div_name
                    if div_name not in self.all_divisions:
                        self.all_divisions.append(div_name)

            # 3. Iterate lecture rows starting after header row
            current_day = None
            for r in ws.iter_rows(min_row=header_row_idx + 1, max_row=min(ws.max_row, 150), values_only=True):
                d_val = r[0]
                if d_val and str(d_val).strip().upper() in ["MON", "TUE", "WED", "THU", "FRI", "SAT"]:
                    current_day = str(d_val).strip().upper()
                lec = r[1]
                time_v = r[2]
                if current_day and lec and str(lec).strip().isdigit():
                    lec_no = int(str(lec).strip())
                    time_str = str(time_v).strip() if time_v else ""

                    for c, div_name in div_cols.items():
                        if c + 3 < len(r):
                            subj = str(r[c]).strip() if r[c] else ""
                            batch = str(r[c+1]).strip() if r[c+1] else ""
                            fac = str(r[c+2]).strip() if r[c+2] else ""
                            room = str(r[c+3]).strip() if r[c+3] else ""

                            if fac:
                                fac_clean = fac.upper().replace(" ", "")
                                self.division_faculty[div_name][fac_clean] = subj
                                self.division_faculty_subjects[div_name][fac_clean].add(subj)
                                self.faculty_divisions[fac_clean].add(div_name)
                                self.all_faculty_initials.add(fac_clean)

                                entry = {
                                    "dept": dept,
                                    "division": div_name,
                                    "subject": subj,
                                    "batch": batch,
                                    "faculty": fac_clean,
                                    "room": room,
                                    "lec_no": lec_no,
                                    "time": time_str,
                                    "day": current_day
                                }
                                self.schedule_by_slot[(current_day, lec_no)].append(entry)
                                self.faculty_slot_entries[(fac_clean, current_day, lec_no)].append(entry)
            wb.close()
        except Exception as e:
            logger.error(f"Error loading timetable for {dept} from {fpath}: {e}")

    def resolve_faculty_initials(self, name_or_query: str, emp_code: str = None) -> str:
        """Resolves faculty initials from full name, query string, or employee code."""
        self.ensure_up_to_date()
        if not name_or_query and not emp_code:
            return ""

        # 1. Try resolving via employee code if given or if query looks like digits
        ec = str(emp_code or "").strip()
        if not ec and name_or_query and str(name_or_query).strip().isdigit():
            ec = str(name_or_query).strip()

        if ec:
            # Check DR roster profiles by sr or mobile
            for init, fac in self.initial_to_faculty.items():
                if str(fac.get("sr", "")).strip() == ec or str(fac.get("mobile", "")).strip() == ec:
                    return init
            # Check faculty_store.json
            try:
                store_path = os.path.join(self.base_dir or ".", "faculty_store.json")
                if os.path.exists(store_path):
                    with open(store_path, "r", encoding="utf-8") as f:
                        store = json.load(f)
                        for uid, f_data in store.items():
                            if str(f_data.get("emp_code") or "").strip() == ec:
                                s_name = str(f_data.get("short_name") or f_data.get("initials") or "").strip().upper()
                                if s_name and (s_name in self.all_faculty_initials or s_name in self.initial_to_faculty):
                                    return s_name
                                f_name = str(f_data.get("name") or "").strip().upper()
                                if f_name in self.name_to_initial:
                                    return self.name_to_initial[f_name]
                                for n, i in self.name_to_initial.items():
                                    if f_name in n or n in f_name:
                                        return i
            except Exception:
                pass

        query = str(name_or_query or "").strip().upper()
        if not query:
            return ""

        # 2. Direct match in timetable initials or DR roster
        if query in self.all_faculty_initials or query in self.initial_to_faculty:
            return query

        # 3. Check faculty_store.json for registered name or short_name
        try:
            store_path = os.path.join(self.base_dir or ".", "faculty_store.json")
            if os.path.exists(store_path):
                with open(store_path, "r", encoding="utf-8") as f:
                    store = json.load(f)
                    for uid, f_data in store.items():
                        s_name = str(f_data.get("short_name") or f_data.get("initials") or "").strip().upper()
                        f_name = str(f_data.get("name") or "").strip().upper()
                        if query in [s_name, f_name]:
                            if s_name and (s_name in self.all_faculty_initials or s_name in self.initial_to_faculty):
                                return s_name
        except Exception:
            pass

        # 4. Normalized full name match in name_to_initial
        norm_name = re.sub(r"\s+", " ", query)
        if norm_name in self.name_to_initial:
            return self.name_to_initial[norm_name]

        # 5. Substring match
        for name, init in self.name_to_initial.items():
            if norm_name in name or name in norm_name:
                return init

        # 6. Initials of words
        letters = "".join([part[0] for part in norm_name.split() if part])
        if letters in self.all_faculty_initials:
            return letters

        return ""

    def get_faculty_info(self, initials: str) -> dict:
        """Returns full faculty info dict for given initials."""
        self.ensure_up_to_date()
        init = (initials or "").upper().strip()
        if init in self.initial_to_faculty:
            return self.initial_to_faculty[init]
        return {
            "name": init,
            "initials": init,
            "dept": "",
            "mobile": "",
            "hod": ""
        }

    def get_faculty_duties_for_date(self, faculty_initials: str, date_obj_or_str) -> list:
        """Returns all scheduled duties (lectures/labs) for the faculty on the specified date."""
        self.ensure_up_to_date()
        init = (faculty_initials or "").upper().strip()
        day_str = None

        if isinstance(date_obj_or_str, (datetime, date)):
            day_str = WEEKDAY_MAP.get(date_obj_or_str.weekday(), "MON")
        elif isinstance(date_obj_or_str, str):
            raw_s = date_obj_or_str.strip().upper()
            if raw_s in DAY_NAME_TO_CODE:
                day_str = DAY_NAME_TO_CODE[raw_s]
            elif raw_s == "TODAY":
                day_str = WEEKDAY_MAP.get(get_ist_today().weekday(), "MON")
            elif raw_s == "TOMORROW":
                day_str = WEEKDAY_MAP.get((get_ist_today() + timedelta(days=1)).weekday(), "MON")
            else:
                for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%d/%m/%y", "%d-%m-%y"]:
                    try:
                        dt = datetime.strptime(date_obj_or_str.strip(), fmt)
                        day_str = WEEKDAY_MAP.get(dt.weekday(), "MON")
                        break
                    except ValueError:
                        pass
                if not day_str and raw_s in DAY_NAME_TO_CODE:
                    day_str = DAY_NAME_TO_CODE[raw_s]

        if not day_str or day_str == "SUN":
            return []

        duties = []
        for lec_no in range(1, 6):
            entries = self.faculty_slot_entries.get((init, day_str, lec_no), [])
            for e in entries:
                duties.append(dict(e))
        return duties

    @staticmethod
    def _norm_day(value) -> str:
        raw = str(value or "").strip().upper()
        return DAY_NAME_TO_CODE.get(raw, raw[:3])

    @staticmethod
    def _norm_time(value) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip().upper())

    @staticmethod
    def _is_es_subject(subject) -> bool:
        return re.sub(r"[^A-Z0-9]", "", str(subject or "").upper()) == "ES"

    def _subjects_for(self, division: str, initials: str) -> set:
        subjects = set(self.division_faculty_subjects.get(division, {}).get(initials, set()))
        if not subjects:
            subject = self.division_faculty.get(division, {}).get(initials)
            if subject:
                subjects.add(subject)
        return subjects

    def _is_es_faculty(self, initials: str) -> bool:
        return any(
            self._is_es_subject(subject)
            for division in self.faculty_divisions.get(initials, set())
            for subject in self._subjects_for(division, initials)
        )

    @staticmethod
    def _compute_semester(date_value, dept=None, division=None) -> str:
        dt = date_value if isinstance(date_value, (datetime, date)) else None
        if dt is None and isinstance(date_value, str):
            for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y", "%d %b %Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%y"]:
                try:
                    dt = datetime.strptime(date_value.strip(), fmt)
                    break
                except ValueError:
                    pass
        month = dt.month if dt else get_ist_now().month
        dept_text = str(dept or "").upper()
        division_text = str(division or "").upper().strip()
        first_year = not ("SY" in dept_text or "SECOND" in dept_text)
        if "FY" not in dept_text and "FIRST" not in dept_text and (division_text.startswith("SY") or "SY" in dept_text):
            first_year = False
        return ("I" if first_year else "III") if 3 <= month <= 7 else ("II" if first_year else "IV")

    def _slot_entries(self, initials: str, day, lec_no, time=None) -> list:
        entries = self.faculty_slot_entries.get((initials, self._norm_day(day), lec_no), [])
        wanted_time = self._norm_time(time)
        if not wanted_time:
            return list(entries)
        return [e for e in entries if self._norm_time(e.get("time")) == wanted_time]

    def _faculty_departments(self, initials: str) -> set:
        departments = set()
        profile_dept = self.initial_to_faculty.get(initials, {}).get("dept")
        if profile_dept:
            departments.add(str(profile_dept).strip().upper())
        for entries in self.faculty_slot_entries.values():
            for entry in entries:
                if entry.get("faculty") == initials and entry.get("dept"):
                    departments.add(str(entry.get("dept")).strip().upper())
        return departments

    @staticmethod
    def _normalise_department(value) -> str:
        return str(value or "").strip().upper()

    def _capacity_allows(self, events: list, counts: dict, limit,
                         include_existing_subject_load: bool = False) -> bool:
        if limit is None or limit <= 0 or limit >= 900:
            return True
        pending_subject = defaultdict(int)
        existing_subject = defaultdict(int)
        existing_seen = set()
        for event in events:
            division, initials = event[0], event[1]
            subject = event[2] if len(event) > 2 else ""
            subject_key = (division, str(subject or "").strip().upper())
            pending_subject[subject_key] += 1
            event_day = self._norm_day(event[3]) if len(event) > 3 else ""
            existing_key = (division, subject_key[1], event_day, initials)
            if (include_existing_subject_load and event_day and subject_key[1]
                    and existing_key not in existing_seen):
                existing_seen.add(existing_key)
                existing_subject[subject_key] = self._existing_subject_count(
                    event_day, division, subject_key[1], initials
                )
            subject_base = counts.get(subject_key, 0)
            # Older callers used faculty-based counters.  Continue honoring
            # those only when such a key was explicitly supplied; the solver
            # itself tracks subject keys exclusively so different subjects
            # taught by one faculty remain independent.
            if subject_key[1] == "":
                subject_base = counts.get((division, initials), counts.get(initials, 0))
            legacy_faculty_base = None
            if (division, initials) in counts:
                legacy_faculty_base = counts[(division, initials)]
            elif initials in counts:
                legacy_faculty_base = counts[initials]
            if (subject_base + existing_subject[subject_key]
                    + pending_subject[subject_key] > limit):
                return False
            if legacy_faculty_base is not None and legacy_faculty_base + pending_subject[subject_key] > limit:
                return False
        return True

    def _existing_subject_count(self, day, division: str, subject: str, initials: str = "") -> int:
        """Count a proxy faculty's existing subject lectures in one division/day."""
        wanted_day = self._norm_day(day)
        wanted_subject = str(subject or "").strip().upper()
        wanted_initials = str(initials or "").strip().upper()
        seen = set()
        for (entry_initials, entry_day, lec_no), entries in self.faculty_slot_entries.items():
            if self._norm_day(entry_day) != wanted_day:
                continue
            if wanted_initials and str(entry_initials).strip().upper() != wanted_initials:
                continue
            for entry in entries:
                if (entry.get("division") != division
                        or str(entry.get("subject") or "").strip().upper() != wanted_subject):
                    continue
                # Count a lecture once even if the workbook contains a
                # duplicated row for the same faculty/division/period.
                key = (
                    entry_initials,
                    entry.get("division"),
                    entry.get("lec_no", lec_no),
                    self._norm_time(entry.get("time")),
                    wanted_subject,
                )
                seen.add(key)
        return len(seen)

    @staticmethod
    def _counts_after_events(counts: dict, events: list) -> dict:
        """Return subject-per-division counts after applying assignment events."""
        updated = dict(counts or {})
        for event in events:
            division, _initials = event[0], event[1]
            subject = str(event[2] if len(event) > 2 else "").strip().upper()
            key = (division, subject)
            updated[key] = updated.get(key, 0) + 1
        return updated

    def _step_events(self, step: dict) -> list:
        divisions = step.get("combined_divisions") or [step.get("division", "")]
        subject = step.get("subject") or step.get("reliever_subject", "")
        day = step.get("day", "")
        return [
            (division, step.get("reliever", ""), subject, day)
            for division in divisions if division
        ]

    def _home_department(self, duty: dict) -> str:
        if duty.get("dept"):
            return self._normalise_department(duty.get("dept"))
        day = self._norm_day(duty.get("day"))
        lec_no = duty.get("lec_no", 1)
        absent = str(duty.get("faculty") or "").upper().replace(" ", "")
        for entry in self.faculty_slot_entries.get((absent, day, lec_no), []):
            if entry.get("division") == duty.get("division") and entry.get("dept"):
                return self._normalise_department(entry.get("dept"))
        return ""

    def _department_allowed(self, initials: str, home_dept: str, allow_other_departments: bool) -> bool:
        if allow_other_departments or not home_dept:
            return True
        departments = self._faculty_departments(initials)
        return not departments or home_dept in departments

    def _es_combined_group(self, duty: dict) -> list:
        """Return the exact two-division ES unit represented by a timetable row."""
        if not self._is_es_subject(duty.get("subject")):
            return []
        day = self._norm_day(duty.get("day"))
        lec_no = duty.get("lec_no", 1)
        time = self._norm_time(duty.get("time"))
        faculty = str(duty.get("faculty") or "").upper().replace(" ", "")
        group = []
        for entry in self.schedule_by_slot.get((day, lec_no), []):
            if not self._is_es_subject(entry.get("subject")):
                continue
            if time and self._norm_time(entry.get("time")) != time:
                continue
            if faculty and entry.get("faculty") != faculty:
                continue
            if entry.get("division") == duty.get("division") or not faculty:
                group.append(entry)
        if faculty:
            group = [
                entry for entry in self.schedule_by_slot.get((day, lec_no), [])
                if self._is_es_subject(entry.get("subject"))
                and entry.get("faculty") == faculty
                and (not time or self._norm_time(entry.get("time")) == time)
            ]
        divisions = {entry.get("division") for entry in group if entry.get("division")}
        if len(group) == 2 and len(divisions) == 2 and duty.get("division") in divisions:
            return sorted(group, key=lambda e: str(e.get("division", "")))
        return []

    def _duty_matches_entry(self, duty: dict, entry: dict) -> bool:
        return (
            self._norm_day(duty.get("day")) == self._norm_day(entry.get("day"))
            and duty.get("lec_no", 1) == entry.get("lec_no")
            and duty.get("division") == entry.get("division")
            and (not duty.get("faculty") or duty.get("faculty") == entry.get("faculty"))
            and (not duty.get("time") or self._norm_time(duty.get("time")) == self._norm_time(entry.get("time")))
        )

    def _conflict_units(self, initials: str, day, lec_no, time) -> list:
        """Collapse a faculty's occupied slot into normal or ES units."""
        entries = self._slot_entries(initials, day, lec_no, time)
        units = []
        seen = set()
        for entry in entries:
            if self._is_es_subject(entry.get("subject")):
                group = self._es_combined_group(entry)
                if not group:
                    # Keep the occupancy visible.  It can never be resolved
                    # as a one-division ES step, so the faculty is not
                    # accidentally treated as free.
                    key = (entry.get("division"), entry.get("faculty"), entry.get("lec_no"), self._norm_time(entry.get("time")))
                    if key not in seen:
                        seen.add(key)
                        units.append([entry])
                    continue
                key = tuple(sorted((e.get("division"), e.get("faculty"), e.get("lec_no"), self._norm_time(e.get("time"))) for e in group))
                if key not in seen:
                    seen.add(key)
                    units.append(group)
            else:
                key = (entry.get("division"), entry.get("faculty"), entry.get("lec_no"), self._norm_time(entry.get("time")))
                if key not in seen:
                    seen.add(key)
                    units.append([entry])
        return units

    @staticmethod
    def _normalise_excluded_faculty_lectures(excluded_faculty_lectures=None) -> dict:
        """Normalise UI exclusion choices to ``{INITIALS: {lecture, ...}}``.

        The load-adjustment UI stores lecture numbers as strings and uses
        ``full_day`` for a whole-day exclusion.  Keeping the normalisation in
        the engine means every entry point (automatic plans, custom slot
        choices, and direct engine callers) applies the same rule.
        """
        normalised = {}
        if not isinstance(excluded_faculty_lectures, dict):
            return normalised
        for raw_faculty, raw_lectures in excluded_faculty_lectures.items():
            faculty = str(raw_faculty or "").upper().replace(" ", "")
            if not faculty:
                continue
            values = raw_lectures if isinstance(raw_lectures, (list, tuple, set)) else [raw_lectures]
            parsed = set()
            for value in values:
                token = str(value or "").strip().lower().replace(" ", "_")
                if token in {"full", "full_day", "allday", "all_day"}:
                    parsed.add("full_day")
                    continue
                try:
                    parsed.add(int(token))
                except (TypeError, ValueError):
                    continue
            if parsed:
                normalised[faculty] = parsed
        return normalised

    @staticmethod
    def _faculty_excluded(initials: str, lec_no, excluded_map: dict) -> bool:
        """Return whether a faculty is unavailable for this lecture."""
        faculty = str(initials or "").upper().replace(" ", "")
        if not faculty:
            return False
        excluded = (excluded_map or {}).get(faculty, set())
        if "full_day" in excluded:
            return True
        try:
            return int(lec_no) in excluded
        except (TypeError, ValueError):
            return False

    def _cascade_chains(self, primary: str, conflicts: list, duty: dict, counts: dict, limit, absent: str,
                        max_hops=None, home_dept: str = "", allow_other_departments: bool = False,
                        include_existing_subject_load: bool = False,
                        excluded_faculty_lectures: dict = None) -> list:
        """Exhaustively enumerate simple, whitelist-valid cascade chains."""
        day = self._norm_day(duty.get("day"))
        lec_no = duty.get("lec_no", 1)
        time = duty.get("time", "")
        excluded_map = self._normalise_excluded_faculty_lectures(excluded_faculty_lectures)

        def resolve_units(current_fac, units, path, visited, path_counts):
            if not units:
                yield path, visited, path_counts
                return
            unit = units[0]
            for resolved_path, resolved_visited, resolved_counts in resolve_unit(
                current_fac, unit, path, visited, path_counts
            ):
                yield from resolve_units(
                    current_fac, units[1:], resolved_path, resolved_visited, resolved_counts
                )

        def resolve_unit(relieved_fac, unit, path, visited, path_counts):
            first = unit[0]
            unit_divisions = [e.get("division", "") for e in unit if e.get("division")]
            is_es_unit = self._is_es_subject(first.get("subject"))
            if is_es_unit and len(set(unit_divisions)) != 2:
                return
            if not allow_other_departments and home_dept:
                unit_depts = {self._normalise_department(e.get("dept")) for e in unit if e.get("dept")}
                if unit_depts and unit_depts != {home_dept}:
                    return
            if max_hops is not None and len(path) >= max_hops:
                return

            for rel_div in dict.fromkeys(unit_divisions):
                div_faculties = self.division_faculty.get(rel_div, {})
                for next_fac in div_faculties:
                    if next_fac in visited or next_fac == absent:
                        continue
                    if self._faculty_excluded(next_fac, first.get("lec_no", lec_no), excluded_map):
                        continue
                    subjects = self._subjects_for(rel_div, next_fac)
                    if is_es_unit:
                        if len(set(unit_divisions)) != 2 or any(
                            not any(self._is_es_subject(s) for s in self._subjects_for(d, next_fac))
                            for d in set(unit_divisions)
                        ):
                            continue
                        step_subject = "ES"
                    else:
                        if self._is_es_faculty(next_fac):
                            continue
                        step_subject = sorted(subjects)[0] if subjects else ""
                    if not self._department_allowed(next_fac, home_dept, allow_other_departments):
                        continue
                    step = {
                        "reliever": next_fac,
                        "reliever_name": self.get_faculty_info(next_fac).get("name", next_fac),
                        "reliever_subject": step_subject,
                        "relieved": relieved_fac,
                        "relieved_subject": first.get("subject", ""),
                        "division": rel_div,
                        "subject": step_subject,
                        "room": first.get("room", "") or "506-D",
                        "lec_no": first.get("lec_no", lec_no),
                        "day": day,
                        "time": first.get("time", "") or time,
                        "is_merged": False,
                    }
                    if is_es_unit:
                        step["is_combined_es"] = True
                        step["combined_divisions"] = list(dict.fromkeys(unit_divisions))
                    step_events = self._step_events(step)
                    if not self._capacity_allows(
                        step_events, path_counts, limit, include_existing_subject_load
                    ):
                        continue

                    next_units = self._conflict_units(next_fac, day, lec_no, time)
                    new_path = path + [step]
                    new_visited = visited | {next_fac}
                    new_counts = self._counts_after_events(path_counts, step_events)
                    if not next_units:
                        yield new_path, new_visited, new_counts
                    else:
                        yield from resolve_units(
                            next_fac, next_units, new_path, new_visited, new_counts
                        )

        # Keep the helper's public result shape stable: callers receive the
        # chain and visited faculty set; the internal count state is only
        # carried through recursion for constraint validation.
        for chain, visited, _final_counts in resolve_units(
            primary, conflicts, [], {primary, absent}, dict(counts or {})
        ):
            yield chain, visited

    def _candidate_base(self, initials: str, subject: str, status: str, info: dict, **flags) -> dict:
        return {
            "initials": initials,
            "name": info.get("name", initials),
            "mobile": info.get("mobile", ""),
            "dept": info.get("dept", ""),
            "subject": subject,
            "status": status,
            "status_desc": "",
            "is_free": False,
            "is_cascade": False,
            "is_merged": False,
            "is_combined": False,
            "hops": 0,
            "chain": [],
            "cascade_detail": "",
            **flags,
        }

    def _find_candidates_for_task(self, task_duties: list, max_lectures_per_subject: int, allow_merged: bool,
                                  counts: dict, include_cascades: bool, max_hops=None,
                                  disturb_other_department: bool = False,
                                  include_existing_subject_load: bool = False,
                                  excluded_faculty_lectures: dict = None) -> list:
        duty = task_duties[0]
        division = duty.get("division", "")
        day = self._norm_day(duty.get("day"))
        lec_no = duty.get("lec_no", 1)
        absent = str(duty.get("faculty") or "").upper().replace(" ", "")
        home_dept = self._home_department(duty)
        is_es = self._is_es_subject(duty.get("subject"))
        excluded_map = self._normalise_excluded_faculty_lectures(excluded_faculty_lectures)
        target_divisions = [d.get("division", "") for d in task_duties]
        if is_es:
            group = self._es_combined_group(duty)
            if len(task_duties) != 2 or len(group) != 2 or set(target_divisions) != {e.get("division") for e in group}:
                return []
            target_divisions = list(dict.fromkeys(target_divisions))

        div_faculties = self.division_faculty.get(division, {})
        if not div_faculties:
            return []
        candidates = []
        seen = set()
        for cand_init in div_faculties:
            if cand_init == absent:
                continue
            if self._faculty_excluded(cand_init, lec_no, excluded_map):
                continue
            if not self._department_allowed(cand_init, home_dept, disturb_other_department):
                continue
            if is_es:
                if any(cand_init not in self.division_faculty.get(d, {}) for d in target_divisions):
                    continue
                if any(not any(self._is_es_subject(s) for s in self._subjects_for(d, cand_init)) for d in target_divisions):
                    continue
                subjects = ["ES"]
            else:
                if self._is_es_faculty(cand_init):
                    continue  # ES faculty cannot proxy a single-division non-ES class.
                subjects = sorted(self._subjects_for(division, cand_init)) or [div_faculties.get(cand_init, duty.get("subject", ""))]

            slot_entries = self._slot_entries(cand_init, day, lec_no, duty.get("time"))
            info = self.get_faculty_info(cand_init)
            for subject in subjects:
                primary_events = [
                    (d, cand_init, subject, day) for d in target_divisions
                ]
                if not self._capacity_allows(
                    primary_events, counts, max_lectures_per_subject,
                    include_existing_subject_load
                ):
                    continue
                primary_counts = self._counts_after_events(counts, primary_events)
                if not slot_entries:
                    cand = self._candidate_base(cand_init, subject, "Combined" if is_es else "Free", info)
                    cand.update({
                        "status_desc": "🟢 Direct Free Faculty" if not is_es else "🟢 Combined ES Faculty",
                        "is_free": True,
                        "is_combined": is_es,
                        "combined_divisions": target_divisions if is_es else [],
                    })
                    key = (cand_init, subject, "free", tuple(target_divisions))
                    if key not in seen:
                        seen.add(key)
                        candidates.append(cand)
                    continue

                if not is_es and allow_merged:
                    other_divs = list(dict.fromkeys(e.get("division", "") for e in slot_entries))
                    other_depts = {self._normalise_department(e.get("dept")) for e in slot_entries if e.get("dept")}
                    if (division not in other_divs
                            and all(cand_init in self.division_faculty.get(d, {}) for d in other_divs)
                            and (disturb_other_department or not home_dept or not other_depts or other_depts == {home_dept})):
                        cand = self._candidate_base(cand_init, subject, "Merged", info)
                        cand.update({
                            "status_desc": f"🔄 Merged with {', '.join(other_divs)}",
                            "is_merged": True,
                            "merged_divisions": other_divs,
                            "cascade_detail": f"Merged with {', '.join(other_divs)}",
                        })
                        key = (cand_init, subject, "merged", tuple(other_divs))
                        if key not in seen:
                            seen.add(key)
                            candidates.append(cand)

                if include_cascades:
                    conflicts = self._conflict_units(cand_init, day, lec_no, duty.get("time"))
                    if any(any(e.get("division") == division for e in unit) for unit in conflicts):
                        continue  # A same-division clash cannot be made into a valid proxy.
                    for chain, _ in self._cascade_chains(
                        cand_init, conflicts, duty, primary_counts, max_lectures_per_subject, absent,
                        max_hops, home_dept, disturb_other_department,
                        include_existing_subject_load, excluded_map
                    ):
                        if not chain:
                            continue
                        cand = self._candidate_base(cand_init, subject, "Cascade", info)
                        chain_desc = " ➔ ".join(
                            f"{s['reliever']} ({s['subject']}) relieves {s['relieved']} in {s['division']}"
                            for s in chain
                        )
                        cand.update({
                            "status_desc": f"🔗 Cascade ({len(chain)}-hop): {chain_desc}",
                            "is_cascade": True,
                            "hops": len(chain),
                            "chain": chain,
                            "cascade_detail": chain_desc,
                            "is_combined": is_es,
                            "combined_divisions": target_divisions if is_es else [],
                        })
                        key = (cand_init, subject, "cascade", tuple((s.get("division"), s.get("reliever"), tuple(s.get("combined_divisions", []))) for s in chain))
                        if key not in seen:
                            seen.add(key)
                            candidates.append(cand)

        candidates.sort(key=lambda c: (0 if c.get("is_free") else 1, c.get("hops", 0), 0 if c.get("is_combined") else 1, c.get("name", ""), c.get("initials", "")))
        return candidates

    def find_eligible_substitutes(
        self,
        duty: dict,
        max_lectures_per_div: int = 2,
        allow_merged: bool = False,
        current_division_counts: dict = None,
        include_cascades: bool = False,
        max_hops: int = None,
        current_counts: dict = None,
        max_lectures_per_subject: int = None,
        disturb_other_department: bool = False,
        allow_other_departments: bool = None,
        disturb_other_dept: bool = None,
        include_existing_subject_load: bool = None,
        excluded_faculty_lectures: dict = None,
    ) -> list:
        """Return every candidate satisfying the timetable and ES rules."""
        self.ensure_up_to_date()
        counts = current_division_counts if current_division_counts is not None else (current_counts or {})
        subject_limit_was_explicit = max_lectures_per_subject is not None
        if max_lectures_per_subject is None:
            max_lectures_per_subject = max_lectures_per_div
        if allow_other_departments is not None:
            disturb_other_department = allow_other_departments
        if disturb_other_dept is not None:
            disturb_other_department = disturb_other_dept
        if include_existing_subject_load is None:
            # New callers using the explicit subject-limit parameter get the
            # corrected total-own-load check. Legacy callers using only
            # max_lectures_per_div retain their historical behavior.
            include_existing_subject_load = subject_limit_was_explicit
        task_duties = [duty]
        if self._is_es_subject(duty.get("subject")):
            group = self._es_combined_group(duty)
            if len(group) == 2:
                task_duties = [dict(entry) for entry in group]
        return self._find_candidates_for_task(
            task_duties, max_lectures_per_subject, allow_merged, counts,
            include_cascades, max_hops, disturb_other_department,
            include_existing_subject_load, excluded_faculty_lectures
        )

    def suggest_load_adjustments(
        self,
        duties: list,
        max_lectures_per_div: int = 2,
        allow_merged: bool = False,
        include_cascades: bool = True,
        prefer_min_disturbance: bool = False,
        max_lectures_per_subject: int = None,
        disturb_other_department: bool = False,
        allow_other_departments: bool = None,
        disturb_other_dept: bool = None,
        include_existing_subject_load: bool = None,
        excluded_faculty_lectures: dict = None,
    ) -> dict:
        """Enumerate valid arrangements, then rank the complete solutions."""
        self.ensure_up_to_date()
        subject_limit_was_explicit = max_lectures_per_subject is not None
        if max_lectures_per_subject is None:
            max_lectures_per_subject = max_lectures_per_div
        if allow_other_departments is not None:
            disturb_other_department = allow_other_departments
        if disturb_other_dept is not None:
            disturb_other_department = disturb_other_dept
        if include_existing_subject_load is None:
            include_existing_subject_load = subject_limit_was_explicit
        excluded_map = self._normalise_excluded_faculty_lectures(excluded_faculty_lectures)
        if not duties:
            return {"success": True, "duties_count": 0, "plans": [], "fallback_plans": [], "per_slot_candidates": [], "solution_count": 0}

        work_duties = [dict(d) for d in duties]
        for duty in work_duties:
            duty["original_subject"] = duty.get("subject", "")
            duty["sem"] = self._compute_semester(duty.get("date") or duty.get("day", ""), dept=duty.get("dept"), division=duty.get("division"))

        tasks = []
        per_slot = [None] * len(work_duties)
        consumed = set()
        invalid_es = False
        for idx, duty in enumerate(work_duties):
            if idx in consumed:
                continue
            if self._is_es_subject(duty.get("subject")):
                group = self._es_combined_group(duty)
                matches = [
                    j for j, candidate in enumerate(work_duties)
                    if any(self._duty_matches_entry(candidate, entry) for entry in group)
                ]
                matches = sorted(set(matches))
                if len(group) != 2 or len(matches) != 2 or idx not in matches:
                    invalid_es = True
                    per_slot[idx] = {"duty": duty, "candidates": []}
                    continue
                task_duties = [work_duties[j] for j in matches]
                consumed.update(matches)
            else:
                task_duties = [duty]
                matches = [idx]
                consumed.add(idx)
            cands = self._find_candidates_for_task(
                task_duties, max_lectures_per_subject, allow_merged, {}, include_cascades,
                None, disturb_other_department, include_existing_subject_load,
                excluded_map
            )
            task = {"duties": task_duties, "indices": matches, "candidates": cands}
            tasks.append(task)
            for task_idx in task["indices"]:
                per_slot[task_idx] = {"duty": work_duties[task_idx], "candidates": cands}

        if invalid_es:
            return {"success": False, "duties_count": len(duties), "plans": [], "fallback_plans": [], "per_slot_candidates": [x or {"duty": work_duties[i], "candidates": []} for i, x in enumerate(per_slot)], "solution_count": 0}
        if any(not task["candidates"] for task in tasks):
            return {"success": False, "duties_count": len(duties), "plans": [], "fallback_plans": [], "per_slot_candidates": [x or {"duty": work_duties[i], "candidates": []} for i, x in enumerate(per_slot)], "solution_count": 0}

        # Solve the most constrained units first, but always emit adjustments
        # in the user's original duty order.
        solve_tasks = sorted(tasks, key=lambda t: (len(t["candidates"]), min(t["indices"])))
        all_solutions = []

        def add_events(counts, events):
            new_counts = dict(counts)
            for event in events:
                division, initials = event[0], event[1]
                subject = event[2] if len(event) > 2 else ""
                subject_key = (division, str(subject or "").strip().upper())
                new_counts[subject_key] = new_counts.get(subject_key, 0) + 1
            return new_counts

        def solve(pos, chosen, counts, used_slots):
            if pos == len(solve_tasks):
                ordered = []
                for task in tasks:
                    selected = next(item for item in chosen if item[0] is task)
                    ordered.extend(selected[1])
                all_solutions.append(ordered)
                return
            task = solve_tasks[pos]
            task_duties = task["duties"]
            for candidate in task["candidates"]:
                chain = candidate.get("chain", [])
                if candidate.get("is_merged") and (not allow_merged or self._is_es_subject(task_duties[0].get("subject"))):
                    continue
                if chain and not include_cascades:
                    continue
                events = [
                    (
                        d.get("division", ""),
                        candidate.get("initials", ""),
                        candidate.get("subject", ""),
                        self._norm_day(d.get("day")),
                    )
                    for d in task_duties
                ]
                for step in chain:
                    events.extend(self._step_events(step))
                if not self._capacity_allows(
                    events, counts, max_lectures_per_subject,
                    include_existing_subject_load
                ):
                    continue
                involved = {candidate.get("initials", "")} | {s.get("reliever") for s in chain if s.get("reliever")}
                slots = {
                    (self._norm_day(d.get("day")), d.get("lec_no", 1), self._norm_time(d.get("time")))
                    for d in task_duties
                }
                if any(involved & used_slots.get(slot, set()) for slot in slots):
                    continue
                new_used = {slot: set(faculties) for slot, faculties in used_slots.items()}
                for slot in slots:
                    new_used.setdefault(slot, set()).update(involved)
                adjustments = []
                for duty in task_duties:
                    adjustments.append({
                        "duty": duty,
                        "substitute": candidate,
                        "subject": duty.get("subject", ""),
                        "original_subject": duty.get("subject", ""),
                        "substitute_subject": candidate.get("subject", ""),
                        "class_div": duty.get("division", ""),
                        "slot": duty.get("time", f"Lec {duty.get('lec_no')}"),
                        "room": duty.get("room", ""),
                        "engager_display": f"{candidate.get('initials')} ({candidate.get('subject', '')})",
                        "cascade_detail": candidate.get("cascade_detail", ""),
                        "chain": chain,
                        "sem": duty.get("sem", ""),
                        "dept": duty.get("dept", ""),
                        "is_combined_es": candidate.get("is_combined", False),
                        "combined_divisions": candidate.get("combined_divisions", []),
                    })
                chosen.append((task, adjustments))
                solve(pos + 1, chosen, add_events(counts, events), new_used)
                chosen.pop()

        solve(0, [], {}, {})

        from collections import Counter
        scored = []
        seen = set()
        for solution in all_solutions:
            substitutes = [item["substitute"]["initials"] for item in solution]
            sub_counts = Counter(substitutes)
            unique_subs = set(substitutes)
            all_involved = set(unique_subs)
            total_hops = sum(item["substitute"].get("hops", 0) for item in solution)
            merged_count = sum(1 for item in solution if item["substitute"].get("is_merged"))
            cascade_count = sum(1 for item in solution if item["substitute"].get("is_cascade"))
            for item in solution:
                all_involved.update(s.get("reliever") for s in item.get("chain", []) if s.get("reliever"))
            signature = tuple((item["duty"].get("division"), item["duty"].get("lec_no"), item["substitute"].get("initials"), tuple((s.get("division"), s.get("reliever")) for s in item.get("chain", []))) for item in solution)
            if signature in seen:
                continue
            seen.add(signature)
            consolidated = sum(1 for n in sub_counts.values() if n >= 2)
            if prefer_min_disturbance:
                score = (
                    len(all_involved) * 1000
                    + len(unique_subs) * 100
                    + total_hops * 10
                    + merged_count * 50
                    - consolidated * 180
                    - max(sub_counts.values(), default=0) * 50
                )
            else:
                # The normal result ordering is also faculty-disturbance
                # aware.  Constraints decide which plans are valid; among
                # valid plans, use the fewest affected faculty as the first
                # objective, then prefer simpler arrangements.
                score = (
                    len(all_involved) * 1000
                    + cascade_count * 500
                    + merged_count * 200
                    + total_hops * 20
                    + len(unique_subs) * 10
                    - consolidated * 2
                )
            scored.append((score, solution, sub_counts, all_involved))
        scored.sort(key=lambda item: item[0])

        formatted = []
        for rank, (score, solution, sub_counts, involved) in enumerate(scored[:3], 1):
            has_cascade = any(item["substitute"].get("is_cascade") for item in solution)
            has_merged = any(item["substitute"].get("is_merged") for item in solution)
            max_duties = max(sub_counts.values(), default=0)
            if prefer_min_disturbance:
                if max_duties >= 2:
                    badge = f"⭐ Min Disturbance [2+ Duties to {' & '.join(f for f, n in sub_counts.items() if n >= 2)}]"
                else:
                    badge = f"⭐ Min Disturbance [{len(involved)} Faculty Disturbed]"
            elif not has_cascade and not has_merged:
                badge = "🟢 All Direct Free Faculty"
            elif has_cascade:
                badge = "🔗 Includes Cascade Arrangement"
            else:
                badge = "🔄 Includes Merged Class"
            prefix = "Option 1 (Best - Recommended)" if rank == 1 else f"Option {rank} (Alternative)"
            formatted.append({
                "plan_id": f"PLAN_{rank}",
                "title": f"{prefix} [{badge}]",
                "is_recommended": rank == 1,
                "is_min_disturbance": prefer_min_disturbance,
                "score": score,
                "adjustments": solution,
            })
        return {
            "success": bool(formatted),
            "duties_count": len(duties),
            "plans": formatted,
            "fallback_plans": [],
            "per_slot_candidates": [x or {"duty": work_duties[i], "candidates": []} for i, x in enumerate(per_slot)],
            "solution_count": len(scored),
        }


def get_timetable_engine(base_dir: str = None) -> TimetableEngine:
    return TimetableEngine.get_instance(base_dir)
