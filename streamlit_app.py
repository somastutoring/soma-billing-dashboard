# streamlit_app.py
from datetime import datetime
import streamlit as st

from billing_logic import (
    create_gc_from_info,
    open_or_create_sheet,
    SERVICES,
    MODES,
    PAID_OPTIONS,
    parse_date,
    parse_duration,
    append_session,
    compute_weekly_tutor_totals,
    mark_client_paid,
    mark_tutor_notes_paid,
    update_tutor_summary_sheet,
)

# ---------------- PASSWORD GATE ----------------

def check_password():
    correct_pw = st.secrets.get("admin_password", "")
    if not correct_pw:
        st.error("Admin password missing in secrets.")
        return False

    if "pw_ok" not in st.session_state:
        st.session_state.pw_ok = False

    if not st.session_state.pw_ok:
        st.title("Soma's Tutoring – Admin Login")
        pw = st.text_input("Enter admin password", type="password")
        if st.button("Log in"):
            if pw == correct_pw:
                st.session_state.pw_ok = True
                st.success("Logged in ✅")
            else:
                st.error("Incorrect password.")
        return False

    return True


if not check_password():
    st.stop()

# ---------------- APP CONFIG ----------------

st.set_page_config(page_title="Soma's Tutoring Billing", layout="centered")
st.title("Soma's Tutoring – Billing Dashboard")

# ---------------- HIDDEN AUTO GOOGLE CONNECTION ----------------

sheet_ref = st.secrets.get("sheet_ref", "").strip()

legacy_clients_str = st.sidebar.text_input(
    "Legacy Clients (comma-separated)",
    value="Brie, Rafi, Caylee, Rishi",
)

legacy_clients = [x.strip() for x in legacy_clients_str.split(",") if x.strip()]

gc = None
sh = None
ws = None

if "gcp_service_account" in st.secrets and sheet_ref:
    try:
        gc = create_gc_from_info(dict(st.secrets["gcp_service_account"]))
        sh, ws = open_or_create_sheet(gc, sheet_ref)
    except Exception as e:
        st.error(f"❌ Google Sheets connection failed: {e}")
        st.stop()
else:
    st.error("❌ Missing Google credentials or sheet_ref in secrets.")
    st.stop()


def require_ws():
    if ws is None:
        st.error("❌ Google Sheet not connected.")
        st.stop()

# ---------------- TABS ----------------

tab_log, tab_client, tab_weekly, tab_month = st.tabs(
    ["➕ Log Session", "💳 Client Payments", "📅 Weekly Tutor Payouts", "📊 Monthly Summary"]
)

# ---------------- TAB 1: LOG SESSION ----------------

with tab_log:
    st.subheader("Log a New Session")
    require_ws()

    records = ws.get_all_records()
    existing_students = sorted({r["student_name"] for r in records if r.get("student_name")})
    existing_tutors = sorted({r["tutor"] for r in records if r.get("tutor")})

    col1, col2 = st.columns(2)

    with col1:
        student_opts = ["➕ New student…"] + existing_students
        student_pick = st.selectbox("Student Name", student_opts)

        student = (
            st.text_input("New student name", "")
            if student_pick == "➕ New student…"
            else student_pick
        )

        date_str = st.text_input("Date", value=datetime.today().date().isoformat())
        service = st.selectbox("Service", SERVICES)

    with col2:
        minutes_text = st.text_input("Minutes", "")
        hhmm_text = st.text_input("HH:MM", "")
        mode = st.selectbox("Mode", MODES)

    col3, col4 = st.columns(2)

    with col3:
        if "Nitin" not in existing_tutors:
            existing_tutors.append("Nitin")

        tutor_opts = ["➕ New tutor…"] + sorted(existing_tutors)
        tutor_pick = st.selectbox("Tutor", tutor_opts)

        tutor = (
            st.text_input("New tutor name", "")
            if tutor_pick == "➕ New tutor…"
            else tutor_pick
        )

    with col4:
        paid_status = st.selectbox("Paid Status", PAID_OPTIONS)

    if st.button("Submit Session", type="primary"):
        try:
            date_iso = parse_date(date_str)
            minutes_val = parse_duration(minutes_text, hhmm_text)

            fin = append_session(
                ws=ws,
                student=student.strip(),
                date_iso=date_iso,
                minutes=minutes_val,
                service=service,
                mode=mode,
                tutor=tutor.strip(),
                paid_status=paid_status,
                legacy_clients=legacy_clients,
            )

            update_tutor_summary_sheet(gc, sheet_ref)

            st.success(
                f"Parent pays: ${fin['amount_due']:.2f} • "
                f"Tutor pay: ${fin['tutor_pay']:.2f} • "
                f"Notes: {fin['notes']}"
            )
        except Exception as e:
            st.error(str(e))

# ---------------- TAB 2: MARK CLIENT PAID ----------------

with tab_client:
    st.subheader("Mark Client as Paid")
    require_ws()

    records = ws.get_all_records()
    students = sorted({r["student_name"] for r in records if r.get("student_name")})

    student_pick = st.selectbox("Student", students)
    date_str = st.text_input("Session Date", value=datetime.today().date().isoformat())

    if st.button("Mark Client Paid"):
        try:
            date_iso = parse_date(date_str)
            count = mark_client_paid(ws, student_pick, date_iso)
            update_tutor_summary_sheet(gc, sheet_ref)
            st.success(f"{count} session(s) updated.")
        except Exception as e:
            st.error(str(e))

# ---------------- TAB 3: WEEKLY PAYROLL ----------------

with tab_weekly:
    st.subheader("Weekly Tutor Payroll (Sunday Pay)")

    sunday_input = st.text_input(
        "Week Ending Sunday",
        value=datetime.today().date().isoformat()
    )

    if st.button("Show Weekly Totals"):
        try:
            sunday_iso = parse_date(sunday_input)
            info = compute_weekly_tutor_totals(ws, sunday_iso)

            st.write(f"Week: {info['start']} → {info['end']}")
            for tutor, amt in info["totals"].items():
                st.write(f"**{tutor}**: ${amt:.2f}")

        except Exception as e:
            st.error(str(e))

    if st.button("Mark Tutor Notes Paid for Week"):
        try:
            sunday_iso = parse_date(sunday_input)
            updated = mark_tutor_notes_paid(ws, sunday_iso)
            update_tutor_summary_sheet(gc, sheet_ref)
            st.success(f"{updated} tutor notes updated to Paid.")
        except Exception as e:
            st.error(str(e))

# ---------------- TAB 4: MONTHLY SUMMARY ----------------

with tab_month:
    st.subheader("Monthly Summary")

    if st.button("Rebuild Summary"):
        try:
            update_tutor_summary_sheet(gc, sheet_ref)
            st.success("Summary rebuilt.")
        except Exception as e:
            st.error(str(e))

    try:
        summary_ws = sh.worksheet("tutor_summary")
        vals = summary_ws.get_all_values()
        st.table(vals)
    except:
        st.info("No summary sheet yet.")
