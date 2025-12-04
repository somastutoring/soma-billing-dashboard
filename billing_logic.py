# billing_logic.py

from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple

import gspread
from google.oauth2.service_account import Credentials

# ----------------- CONSTANTS / CONFIG -----------------

SHEET_TAB = "sessions"

COLUMNS = [
    "id",
    "student_name",
    "date",
    "minutes",
    "hours_decimal",
    "service",
    "mode",
    "tutor",
    "notes",
    "rate_tier",
    "rate",
    "amount_due",
    "paid_status",
]

SERVICES = ["K–12 Tutoring", "SAT & ACT Prep", "College & AP Courses"]
MODES = ["Online", "In-Person"]
PAID_OPTIONS = ["Not Paid", "Paid", "Free session"]

# Legacy = same rate regardless of mode (hourly)
LEGACY_RATES = {
    "K–12 Tutoring": 25.0,
    "SAT & ACT Prep": 35.0,
    "College & AP Courses": 30.0,
}

# NEW = mode-specific hourly rates
NEW_RATES = {
    "K–12 Tutoring": {"Online": 30.0, "In-Person": 40.0},
    "SAT & ACT Prep": {"Online": 35.0, "In-Person": 45.0},
    "College & AP Courses": {"Online": 40.0, "In-Person": 50.0},
}

# ----------------- GOOGLE SHEETS HELPERS -----------------

def create_gc_from_info(info: Dict[str, Any]) -> gspread.Client:
    """
    info comes from Streamlit secrets: st.secrets["gcp_service_account"]
    """
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def open_or_create_sheet(gc: gspread.Client, sheet_ref: str):
    """
    sheet_ref is either:
      - Full Spreadsheet URL, or
      - Spreadsheet title in Drive.
    Ensures a 'sessions' worksheet exists with the proper columns.
    """
    if sheet_ref.startswith("http"):
        sh = gc.open_by_url(sheet_ref)
    else:
        try:
            sh = gc.open(sheet_ref)
        except gspread.SpreadsheetNotFound:
            sh = gc.create(sheet_ref)

    try:
        ws = sh.worksheet(SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_TAB, rows=1000, cols=len(COLUMNS))
        ws.append_row(COLUMNS)

    header = ws.row_values(1)
    if header != COLUMNS:
        last_col_letter = chr(ord("A") + len(COLUMNS) - 1)
        ws.update(f"A1:{last_col_letter}1", [COLUMNS])

    return sh, ws

# ----------------- DATE & DURATION PARSING -----------------

def parse_date(s: str) -> str:
    """
    Accepts 'YYYY-MM-DD' or 'MM/DD/YYYY'.
    Returns ISO date 'YYYY-MM-DD'.
    """
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError("Date must be YYYY-MM-DD or MM/DD/YYYY")


def parse_duration(minutes_text: str, hhmm_text: str) -> int:
    """
    One of minutes_text OR hhmm_text must be provided.
    """
    minutes_text = (minutes_text or "").strip()
    hhmm_text = (hhmm_text or "").strip()

    if minutes_text and hhmm_text:
        raise ValueError("Fill either Minutes OR HH:MM, not both.")

    if not minutes_text and not hhmm_text:
        raise ValueError("Enter Minutes OR HH:MM.")

    if minutes_text:
        m = int(minutes_text)
        if m <= 0:
            raise ValueError("Minutes must be positive.")
        return m

    if ":" not in hhmm_text:
        raise ValueError("HH:MM must include a colon, e.g. 1:30")

    h, m = hhmm_text.split(":", 1)
    h, m = int(h), int(m)
    if not (0 <= m < 60):
        raise ValueError("MM must be 0-59.")

    total = h * 60 + m
    if total <= 0:
        raise ValueError("Duration must be positive.")
    return total


def hours_from_minutes(total_minutes: int) -> float:
    return round(total_minutes / 60.0, 2)

# ----------------- RATES & PRICING -----------------

def get_rate_for_student(
    student_name: str,
    service_name: str,
    mode_name: str,
    legacy_clients: List[str],
) -> Tuple[str, float]:
    """
    Return (tier, hourly_rate):
      - 'Legacy' uses LEGACY_RATES
      - 'New' uses NEW_RATES with mode-specific rates
    """
    legacy_lower = [x.strip().lower() for x in legacy_clients]
    if student_name.strip().lower() in legacy_lower:
        return "Legacy", LEGACY_RATES[service_name]
    return "New", NEW_RATES[service_name][mode_name]


def append_session(
    ws,
    student: str,
    date_iso: str,
    minutes: int,
    service: str,
    mode: str,
    tutor: str,
    paid_status: str,
    legacy_clients: List[str],
) -> Dict[str, Any]:
    """
    Core logic that:
      - Computes tier, hourly_rate, amount_due, tutor_pay
      - Builds 'Pay <Tutor> $X.XX' notes
      - Appends the row to the 'sessions' sheet.

    Returns a dict with keys:
      'tier', 'hourly_rate', 'amount_due', 'tutor_pay', 'notes'
    """
    if service not in LEGACY_RATES:
        raise ValueError("Invalid service.")
    if mode not in MODES:
        raise ValueError("Invalid mode.")
    if not student:
        raise ValueError("Student cannot be empty.")
    if not tutor:
        raise ValueError("Tutor cannot be empty.")

    hours_decimal = hours_from_minutes(minutes)
    tier, hourly_rate = get_rate_for_student(student, service, mode, legacy_clients)

    full_amount = round(hours_decimal * hourly_rate, 2)
    paid_status = (paid_status or "Not Paid").strip()
    is_free = paid_status.lower().startswith("free")

    # Parent pays:
    if is_free:
        amount_due = 0.00
    else:
        amount_due = full_amount

    # Tutor pay rules:
    # - Nitin: 100% of amount_due
    # - Others:
    #     * Free:  50% of full_amount
    #     * Paid:  50% of amount_due
    if tutor == "Nitin":
        tutor_pay = amount_due
    else:
        if is_free:
            tutor_pay = round(full_amount / 2.0, 2)
        else:
            tutor_pay = round(amount_due / 2.0, 2)

    notes = f"Pay {tutor} ${tutor_pay:.2f}"

    # Build unique id
    existing = ws.get_all_records()
    serial = (
        sum(
            1
            for r in existing
            if (r.get("date", "") == date_iso)
            and (r.get("student_name", "") or "").strip().lower()
            == student.lower()
        )
        + 1
    )
    rid = f"{date_iso.replace('-','')}-{student.lower().replace(' ','_')}-{serial}"

    header = ws.row_values(1)
    if header != COLUMNS:
        last_col_letter = chr(ord("A") + len(COLUMNS) - 1)
        ws.update(f"A1:{last_col_letter}1", [COLUMNS])

    ws.append_row(
        [
            rid,
            student,
            date_iso,
            str(minutes),
            f"{hours_decimal:.2f}",
            service,
            mode,
            tutor,
            notes,
            tier,
            f"{hourly_rate:.2f}",
            f"{amount_due:.2f}",
            paid_status,
        ]
    )

    return {
        "tier": tier,
        "hourly_rate": hourly_rate,
        "amount_due": amount_due,
        "tutor_pay": tutor_pay,
        "notes": notes,
    }

# ----------------- UNPAID / CLIENT PAYMENT LOGIC -----------------

def list_unpaid_sessions(ws) -> List[Dict[str, Any]]:
    """
    Return a list of session dicts where the client still owes money.

    Treat as unpaid if:
      - paid_status is blank, 'Not Paid', 'Unpaid' (case-insensitive)
    Ignore:
      - 'Paid'
      - 'Free session'
    """
    records = ws.get_all_records()
    results = []

    for r in records:
        status_raw = (r.get("paid_status") or "").strip()
        status = status_raw.lower()

        if status in ("paid", "free session"):
            continue

        if status not in ("not paid", "unpaid", ""):
            # Ignore weird statuses
            continue

        try:
            amt = float(r.get("amount_due") or 0)
        except (TypeError, ValueError):
            amt = 0.0

        results.append(
            {
                "id": r.get("id", ""),  # include ID so we can mark by session
                "student_name": r.get("student_name", ""),
                "date": r.get("date", ""),
                "service": r.get("service", ""),
                "tutor": r.get("tutor", ""),
                "amount_due": amt,
                "paid_status": status_raw,
            }
        )

    return results


def mark_session_paid_by_id(ws, session_id: str) -> int:
    """
    Mark a specific session as Paid by its unique 'id' field.
    Returns 1 if updated, 0 if not found or already paid.
    """
    session_id = (session_id or "").strip()
    if not session_id:
        return 0

    header = ws.row_values(1)
    try:
        id_idx = header.index("id") + 1
        paid_idx = header.index("paid_status") + 1
    except ValueError:
        return 0

    all_values = ws.get_all_values()

    for row_num in range(2, len(all_values) + 1):
        row = all_values[row_num - 1]
        if len(row) < max(id_idx, paid_idx):
            continue

        r_id = (row[id_idx - 1] or "").strip()
        r_status = (row[paid_idx - 1] or "").strip().lower()

        if r_id == session_id:
            if r_status not in ("paid", "free session"):
                ws.update_cell(row_num, paid_idx, "Paid")
                return 1
            return 0

    return 0


def mark_client_paid(ws, student_name: str, date_iso: str) -> int:
    """
    (Legacy helper – not used in the new UI, but kept for completeness.)
    For a given student + date, change paid_status from Not Paid/blank/Unpaid
    to 'Paid'. Returns number of rows updated.
    """
    student_name = (student_name or "").strip()
    date_iso = (date_iso or "").strip()
    if not student_name or not date_iso:
        return 0

    header = ws.row_values(1)
    try:
        paid_idx = header.index("paid_status") + 1  # 1-based
        date_idx = header.index("date") + 1
        student_idx = header.index("student_name") + 1
    except ValueError:
        return 0

    all_values = ws.get_all_values()
    updated = 0

    for row_num in range(2, len(all_values) + 1):
        row = all_values[row_num - 1]
        if len(row) < max(paid_idx, date_idx, student_idx):
            continue

        r_date = (row[date_idx - 1] or "").strip()
        r_student = (row[student_idx - 1] or "").strip()
        r_status = (row[paid_idx - 1] or "").strip().lower()

        if r_date == date_iso and r_student.lower() == student_name.lower():
            if r_status in ("", "not paid", "unpaid"):
                ws.update_cell(row_num, paid_idx, "Paid")
                updated += 1

    return updated

# ----------------- WEEKLY PAYROLL LOGIC -----------------

def _week_range_from_sunday(sunday_iso: str):
    """
    Given week-ending Sunday date (YYYY-MM-DD),
    return (monday_iso, sunday_iso) for that week.
    """
    sunday = datetime.strptime(sunday_iso, "%Y-%m-%d").date()
    monday = sunday - timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


def compute_weekly_tutor_totals(ws, sunday_iso: str) -> Dict[str, Any]:
    """
    Compute weekly totals for *non-Nitin* tutors for payroll.

    Returns:
    {
      "start": monday_iso,
      "end": sunday_iso,
      "totals": { "Aryan": 123.45, "Neha": 67.89, ... }
    }
    """
    start_iso, end_iso = _week_range_from_sunday(sunday_iso)
    start_dt = datetime.strptime(start_iso, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end_iso, "%Y-%m-%d").date()

    records = ws.get_all_records()
    totals: Dict[str, float] = {}

    for r in records:
        tutor = (r.get("tutor") or "").strip()
        if not tutor or tutor == "Nitin":
            continue

        date_str = (r.get("date") or "").strip()
        if not date_str:
            continue

        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        if not (start_dt <= d <= end_dt):
            continue

        try:
            hours_decimal = float(r.get("hours_decimal") or 0)
            rate = float(r.get("rate") or 0)
            amount_due = float(r.get("amount_due") or 0)
        except (TypeError, ValueError):
            continue

        paid_status = (r.get("paid_status") or "").strip().lower()
        is_free = paid_status.startswith("free")
        full_value = hours_decimal * rate

        # Tutor earnings for non-Nitin:
        #  - Free:  50% of full_value
        #  - Paid:  50% of amount_due
        if is_free:
            tutor_earn = 0.5 * full_value
        else:
            tutor_earn = 0.5 * amount_due

        totals[tutor] = totals.get(tutor, 0.0) + tutor_earn

    return {"start": start_iso, "end": end_iso, "totals": totals}


def mark_tutor_notes_paid(ws, sunday_iso: str) -> int:
    """
    For all sessions in the given week (Mon–Sun) with notes like
    'Pay Aryan $X.XX', change that note to 'Paid Aryan $X.XX'.

    Returns number of rows updated.
    """
    start_iso, end_iso = _week_range_from_sunday(sunday_iso)
    start_dt = datetime.strptime(start_iso, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end_iso, "%Y-%m-%d").date()

    header = ws.row_values(1)
    try:
        notes_idx = header.index("notes") + 1
        date_idx = header.index("date") + 1
    except ValueError:
        return 0

    all_values = ws.get_all_values()
    updated = 0

    for row_num in range(2, len(all_values) + 1):
        row = all_values[row_num - 1]
        if len(row) < max(notes_idx, date_idx):
            continue

        r_date = (row[date_idx - 1] or "").strip()
        r_notes = (row[notes_idx - 1] or "").strip()

        if not r_date:
            continue

        try:
            d = datetime.strptime(r_date, "%Y-%m-%d").date()
        except ValueError:
            continue

        if not (start_dt <= d <= end_dt):
            continue

        if r_notes.startswith("Pay "):
            ws.update_cell(row_num, notes_idx, r_notes.replace("Pay ", "Paid ", 1))
            updated += 1

    return updated

# ----------------- MONTHLY SUMMARY (BUSINESS + TUTORS + FREE COST) -----------------

def update_tutor_summary_sheet(gc: gspread.Client, sheet_ref: str):
    """
    Rebuilds a 'tutor_summary' sheet that shows, per month:
      - Each tutor's total earnings
      - Free Session Cost (what you paid out-of-pocket for free sessions)
      - Nitin Business Earnings (Nitin as tutor + profit share from others - free session costs)
    """
    if sheet_ref.startswith("http"):
        sh = gc.open_by_url(sheet_ref)
    else:
        sh = gc.open(sheet_ref)

    try:
        ws = sh.worksheet(SHEET_TAB)
    except gspread.WorksheetNotFound:
        return []

    records = ws.get_all_records()

    try:
        summary_ws = sh.worksheet("tutor_summary")
    except gspread.WorksheetNotFound:
        summary_ws = sh.add_worksheet(title="tutor_summary", rows=200, cols=3)

    # month_tutor_totals["YYYY-MM"]["TutorName"] = amount
    month_tutor_totals: Dict[str, Dict[str, float]] = {}
    month_nitin_business: Dict[str, float] = {}
    month_free_cost: Dict[str, float] = {}  # total you spent on free sessions

    for r in records:
        tutor = (r.get("tutor") or "").strip()
        if not tutor:
            continue

        date_str = (r.get("date") or "").strip()
        if len(date_str) < 7:
            continue

        ym = date_str[:7]  # YYYY-MM

        try:
            hours_decimal = float(r.get("hours_decimal") or 0)
            rate = float(r.get("rate") or 0)
            amount_due = float(r.get("amount_due") or 0)
        except (TypeError, ValueError):
            continue

        paid_status = (r.get("paid_status") or "").strip().lower()
        is_free = paid_status.startswith("free")
        full_value = hours_decimal * rate

        if ym not in month_tutor_totals:
            month_tutor_totals[ym] = {}
            month_nitin_business[ym] = 0.0
            month_free_cost[ym] = 0.0

        # Tutor earnings
        if tutor == "Nitin":
            tutor_earn = amount_due
        else:
            if is_free:
                tutor_earn = 0.5 * full_value
            else:
                tutor_earn = 0.5 * amount_due

        # Nitin business earnings + free session tracking
        if tutor == "Nitin":
            nitin_contrib = amount_due
        else:
            if is_free:
                # you pay half of full_value on free session
                free_cost = 0.5 * full_value
                nitin_contrib = -free_cost
                month_free_cost[ym] += free_cost
            else:
                nitin_contrib = 0.5 * amount_due

        month_tutor_totals[ym][tutor] = month_tutor_totals[ym].get(tutor, 0.0) + tutor_earn
        month_nitin_business[ym] = month_nitin_business.get(ym, 0.0) + nitin_contrib

    # Build rows
    rows: List[List[str]] = []
    for ym in sorted(month_tutor_totals.keys()):
        rows.append([f"Month: {ym}", "", ""])
        rows.append(["Tutor", "Tutor Earnings", ""])
        for tutor_name in sorted(month_tutor_totals[ym].keys()):
            total = month_tutor_totals[ym][tutor_name]
            rows.append([tutor_name, f"{total:.2f}", ""])

        # Free session cost + business earnings
        rows.append(["Free Session Cost", f"{month_free_cost[ym]:.2f}", ""])
        rows.append(["Nitin Business Earnings", f"{month_nitin_business[ym]:.2f}", ""])
        rows.append(["", "", ""])

    summary_ws.clear()
    if rows:
        summary_ws.update(f"A1:C{len(rows)}", rows)

    return rows

# ----------------- RECENT SESSIONS & SEARCH -----------------

def list_recent_sessions(ws, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Return up to `limit` most recent sessions, based on date + id.
    """
    records = ws.get_all_records()
    enriched = []
    for i, r in enumerate(records):
        date = (r.get("date") or "").strip()
        sid = (r.get("id") or "").strip()
        enriched.append((date, sid, i, r))

    enriched.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [e[3] for e in enriched[:limit]]


def search_sessions_by_student_month(ws, student_name: str, year_month: str) -> List[Dict[str, Any]]:
    """
    Return all sessions for a given student within a given year-month (YYYY-MM).
    Example: year_month = '2025-11'
    """
    ym = (year_month or "").strip()
    target = (student_name or "").strip().lower()
    if not ym or not target:
        return []

    records = ws.get_all_records()
    results = []
    for r in records:
        sname = (r.get("student_name") or "").strip().lower()
        date_str = (r.get("date") or "").strip()
        if sname == target and date_str.startswith(ym):
            results.append(r)
    return results
