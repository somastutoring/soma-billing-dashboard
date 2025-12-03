# billing_logic.py
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta

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
MODES    = ["Online", "In-Person"]
PAID_OPTIONS = ["Not Paid", "Paid", "Free session"]

LEGACY_RATES = {
    "K–12 Tutoring":        25.0,
    "SAT & ACT Prep":       35.0,
    "College & AP Courses": 30.0,
}

NEW_RATES = {
    "K–12 Tutoring":        {"Online": 30.0, "In-Person": 40.0},
    "SAT & ACT Prep":       {"Online": 35.0, "In-Person": 45.0},
    "College & AP Courses": {"Online": 40.0, "In-Person": 50.0},
}


# ---------- Basic helpers ----------

def parse_date(s: str) -> str:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError("Date must be YYYY-MM-DD or MM/DD/YYYY")


def parse_duration(minutes_text: str, hhmm_text: str) -> int:
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


def get_rate_for_student(student_name: str, service_name: str,
                         mode_name: str, legacy_list):
    legacy_lower = [x.strip().lower() for x in legacy_list]
    if student_name.strip().lower() in legacy_lower:
        return "Legacy", LEGACY_RATES[service_name]
    return "New", NEW_RATES[service_name][mode_name]


# ---------- Google Sheets connection ----------

def create_gc_from_info(creds_info: dict):
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    return gspread.authorize(creds)


def open_or_create_sheet(gc, sheet_ref: str):
    """Return (spreadsheet, sessions_ws)."""
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


# ---------- Session creation / math ----------

def compute_session_financials(student: str, service: str, mode: str,
                               minutes: int, paid_status: str,
                               tutor: str, legacy_clients):
    """Return dict with tier, hourly_rate, hours, full_amount, amount_due, tutor_pay, notes."""
    if service not in LEGACY_RATES:
        raise ValueError("Invalid service.")

    if mode not in MODES:
        raise ValueError("Invalid mode.")

    minutes_val = minutes
    hours = hours_from_minutes(minutes_val)

    tier, hourly_rate = get_rate_for_student(student, service, mode, legacy_clients)
    full_amount = round(hours * hourly_rate, 2)

    is_free = (paid_status or "").strip().lower().startswith("free")
    if is_free:
        amount_due = 0.0
    else:
        amount_due = full_amount

    # Tutor pay rules
    if tutor == "Nitin":
        tutor_pay = amount_due
    else:
        if is_free:
            tutor_pay = round(full_amount / 2.0, 2)
        else:
            tutor_pay = round(amount_due / 2.0, 2)

    notes = f"Pay {tutor} ${tutor_pay:.2f}"

    return {
        "hours_decimal": hours,
        "tier": tier,
        "hourly_rate": hourly_rate,
        "full_amount": full_amount,
        "amount_due": amount_due,
        "tutor_pay": tutor_pay,
        "notes": notes,
    }


def append_session(ws, student: str, date_iso: str, minutes: int,
                   service: str, mode: str, tutor: str,
                   paid_status: str, legacy_clients):
    """Append a new session row to the sessions sheet."""
    financials = compute_session_financials(
        student, service, mode, minutes, paid_status, tutor, legacy_clients
    )

    records = ws.get_all_records()
    serial = sum(
        1
        for r in records
        if (
            (r.get("date", "") or "") == date_iso
            and (r.get("student_name", "") or "").strip().lower() == student.lower()
        )
    ) + 1

    rid = f"{date_iso.replace('-','')}-{student.lower().replace(' ','_')}-{serial}"

    row = [
        rid,
        student,
        date_iso,
        str(minutes),
        f"{financials['hours_decimal']:.2f}",
        service,
        mode,
        tutor,
        financials["notes"],
        financials["tier"],
        f"{financials['hourly_rate']:.2f}",
        f"{financials['amount_due']:.2f}",
        paid_status,
    ]
    ws.append_row(row)
    return financials


# ---------- Client paid / tutor notes updates ----------

def mark_client_paid(ws, student: str, date_iso: str) -> int:
    """Set paid_status='Paid' for matching student+date rows. Returns count updated."""
    records = ws.get_all_records()
    paid_col = COLUMNS.index("paid_status") + 1

    updated = 0
    for idx, r in enumerate(records, start=2):
        r_date = (r.get("date", "") or "").strip()
        r_student = (r.get("student_name", "") or "").strip()

        if r_date == date_iso and r_student.lower() == student.lower():
            cur_status = (r.get("paid_status", "") or "").strip().lower()
            if cur_status == "paid":
                continue
            ws.update_cell(idx, paid_col, "Paid")
            updated += 1
    return updated


def _week_range_from_sunday(sunday_iso: str):
    end_dt = datetime.fromisoformat(sunday_iso)
    start_dt = end_dt - timedelta(days=6)
    return start_dt.date(), end_dt.date()


def iter_week_rows(ws, sunday_iso: str):
    """Yield (row_idx, record) for rows in Monday–Sunday week, non-Nitin tutors."""
    start_date, end_date = _week_range_from_sunday(sunday_iso)
    records = ws.get_all_records()

    for idx, r in enumerate(records, start=2):
        tutor = (r.get("tutor", "") or "").strip()
        if not tutor or tutor == "Nitin":
            continue
        date_str = (r.get("date", "") or "").strip()
        if not date_str:
            continue
        try:
            d = datetime.fromisoformat(date_str).date()
        except ValueError:
            continue
        if start_date <= d <= end_date:
            yield idx, r


def compute_weekly_tutor_totals(ws, sunday_iso: str):
    """
    Returns dict tutor -> amount owed (for non-Nitin tutors)
    based on your pay rules.
    """
    totals = {}
    start_date, end_date = _week_range_from_sunday(sunday_iso)

    for _, r in iter_week_rows(ws, sunday_iso):
        tutor = (r.get("tutor", "") or "").strip()
        try:
            hours_decimal = float(r.get("hours_decimal") or 0)
            rate = float(r.get("rate") or 0)
            amount_due = float(r.get("amount_due") or 0)
        except (TypeError, ValueError):
            continue

        paid_status = (r.get("paid_status", "") or "").strip()
        is_free = paid_status.lower().startswith("free")
        full_value = hours_decimal * rate

        if is_free:
            tutor_pay = 0.5 * full_value
        else:
            tutor_pay = 0.5 * amount_due

        totals[tutor] = totals.get(tutor, 0.0) + tutor_pay

    return {
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "totals": totals,
    }


def mark_tutor_notes_paid(ws, sunday_iso: str) -> int:
    """
    For the given week, flip 'Pay <Tutor> $X' -> 'Paid <Tutor> $X' for non-Nitin tutors.
    Returns count updated.
    """
    notes_col = COLUMNS.index("notes") + 1
    updated = 0

    for row_idx, r in iter_week_rows(ws, sunday_iso):
        notes = (r.get("notes", "") or "")
        if notes.startswith("Pay "):
            new_notes = "Paid " + notes[4:]
            ws.update_cell(row_idx, notes_col, new_notes)
            updated += 1
    return updated


# ---------- Monthly tutor summary sheet ----------

def update_tutor_summary_sheet(gc, sheet_ref: str):
    """
    Build/refresh 'tutor_summary' with, per month:
      - Tutor earnings (what each tutor gets paid)
      - Nitin Business Earnings
    """
    if sheet_ref.startswith("http"):
        sh = gc.open_by_url(sheet_ref)
    else:
        sh = gc.open(sheet_ref)

    try:
        ws = sh.worksheet(SHEET_TAB)
    except gspread.WorksheetNotFound:
        return  # nothing to do

    try:
        summary_ws = sh.worksheet("tutor_summary")
    except gspread.WorksheetNotFound:
        summary_ws = sh.add_worksheet(title="tutor_summary", rows=200, cols=3)

    records = ws.get_all_records()

    month_tutor_totals = {}    # { 'YYYY-MM': { tutor: total } }
    month_nitin_business = {}  # { 'YYYY-MM': total }

    for r in records:
        tutor = (r.get("tutor", "") or "").strip()
        if not tutor:
            continue
        date_str = (r.get("date", "") or "").strip()
        if len(date_str) < 7:
            continue
        ym = date_str[:7]  # 'YYYY-MM'

        try:
            hours_decimal = float(r.get("hours_decimal") or 0)
            rate = float(r.get("rate") or 0)
            amount_due = float(r.get("amount_due") or 0)
        except (TypeError, ValueError):
            continue

        paid_status = (r.get("paid_status", "") or "").strip()
        is_free = paid_status.lower().startswith("free")
        full_value = hours_decimal * rate

        # Tutor earnings
        if tutor == "Nitin":
            tutor_earn = amount_due
        else:
            if is_free:
                tutor_earn = 0.5 * full_value
            else:
                tutor_earn = 0.5 * amount_due

        # Nitin business earnings
        if tutor == "Nitin":
            nitin_contrib = amount_due
        else:
            if is_free:
                nitin_contrib = -0.5 * full_value
            else:
                nitin_contrib = 0.5 * amount_due

        if ym not in month_tutor_totals:
            month_tutor_totals[ym] = {}
            month_nitin_business[ym] = 0.0

        month_tutor_totals[ym][tutor] = (
            month_tutor_totals[ym].get(tutor, 0.0) + tutor_earn
        )
        month_nitin_business[ym] += nitin_contrib

    # Build rows
    rows = []
    from datetime import datetime as _dt

    for ym in sorted(month_tutor_totals.keys()):
        try:
            label = _dt.strptime(ym, "%Y-%m").strftime("%B %Y")
        except ValueError:
            label = ym

        rows.append([label, "", ""])
        rows.append(["Tutor", "Tutor Earnings", ""])

        tutors_for_month = month_tutor_totals.get(ym, {})
        for tutor_name in sorted(tutors_for_month.keys()):
            total = tutors_for_month[tutor_name]
            rows.append([tutor_name, f"{total:.2f}", ""])

        rows.append(
            [
                "Nitin Business Earnings",
                f"{month_nitin_business.get(ym, 0.0):.2f}",
                "",
            ]
        )
        rows.append(["", "", ""])

    summary_ws.clear()
    if rows:
        summary_ws.update(f"A1:C{len(rows)}", rows)
    return rows
