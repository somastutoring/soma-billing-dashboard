# streamlit_app.py
from datetime import datetime, timedelta
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
    mark_tutor_notes_paid,
    update_tutor_summary_sheet,
    list_unpaid_sessions,
    list_recent_sessions,
    search_sessions_by_student_month,
    mark_session_paid_by_id,  # <-- NEW
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

legacy_default = ["Brie", "Rafi", "Caylee", "Rishi"]
legacy_secret = st.secrets.get("legacy_clients", legacy_default)

if isinstance(legacy_secret, list):
    legacy_clients = [x.strip() for x in legacy_secret if x.strip()]
else:
    legacy_clients = [
        x.strip() for x in str(legacy_secret).split(",") if x.strip()
    ]

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


def get_default_sunday_str():
    today = datetime.today().date()
    weekday = today.weekday()  # 0=Mon, 6=Sun
    days_until_sun = (6 - weekday) % 7
    default_sunday = today + timedelta(days=days_until_sun)
    return default_sunday.isoformat()

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
            if not student.strip():
                st.error("Student cannot be empty.")
                st.stop()
            if not tutor.strip():
                st.error("Tutor cannot be empty.")
                st.stop()

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
                f"Saved session for {student} on {date_iso}. "
                f"Parent Pays: ${fin['amount_due']:.2f} • "
                f"Tutor Pay: ${fin['tutor_pay']:.2f} • Notes: {fin['notes']}"
            )
        except Exception as e:
            st.error(str(e))

    st.markdown("---")
    st.markdown("### Recent Sessions (Newest First)")

    try:
        recent = list_recent_sessions(ws, limit=10)
        if recent:
            st.dataframe(recent)
        else:
            st.info("No sessions logged yet.")
    except Exception as e:
        st.error(f"Error loading recent sessions: {e}")

# ---------------- TAB 2: CLIENT PAYMENTS (SEARCH + UNPAID CLICK-TO-PAID) ----------------

with tab_client:
    st.subheader("Client Payments")
    require_ws()

    records = ws.get_all_records()
    students = sorted({r["student_name"] for r in records if r.get("student_name")})

    # ---- Search sessions by student + month ----
    st.markdown("### Search Sessions by Student & Month")

    cols_search = st.columns(2)
    with cols_search[0]:
        student_search = st.selectbox(
            "Student (search)", students, key="student_search"
        )
    with cols_search[1]:
        default_month = datetime.today().strftime("%Y-%m")
        month_search = st.text_input(
            "Month (YYYY-MM)", value=default_month, key="month_search"
        )

    if st.button("Search Sessions"):
        try:
            matches = search_sessions_by_student_month(ws, student_search, month_search)
            if not matches:
                st.info(f"No sessions found for {student_search} in {month_search}.")
            else:
                st.success(
                    f"Found {len(matches)} session(s) for {student_search} in {month_search}."
                )
                st.dataframe(matches)
        except Exception as e:
            st.error(f"Error searching sessions: {e}")

    st.markdown("---")

    # ---- Unpaid list + click to mark paid ----
    st.markdown("### Clients With Unpaid Sessions")

    unpaid = []
    try:
        unpaid = list_unpaid_sessions(ws)
    except Exception as e:
        st.error(f"Error loading unpaid sessions: {e}")

    if not unpaid:
        st.success("🎉 All sessions are either Paid or Free.")
    else:
        st.markdown("**Unpaid totals by student:**")
        totals = {}
        for r in unpaid:
            name = r["student_name"] or "(Unknown)"
            totals[name] = totals.get(name, 0.0) + r["amount_due"]
        for name, amt in sorted(totals.items()):
            st.write(f"- **{name}**: ${amt:.2f}")

        st.markdown("**Unpaid session details:**")
        st.dataframe(unpaid)

        unpaid_ids = [r["id"] for r in unpaid if r.get("id")]
        if unpaid_ids:
            selected_id = st.selectbox(
                "Select a session ID to mark as Paid",
                unpaid_ids,
                format_func=lambda x: x if x else "(no id)",
            )

            if st.button("Mark Selected Session Paid"):
                try:
                    updated = mark_session_paid_by_id(ws, selected_id)
                    update_tutor_summary_sheet(gc, sheet_ref)
                    if updated:
                        st.success(f"Session {selected_id} marked as Paid.")
                    else:
                        st.info("No session updated (maybe already Paid).")
                except Exception as e:
                    st.error(f"Error updating session: {e}")
        else:
            st.info("Unpaid sessions have no IDs; cannot mark by click. Check sheet IDs.")

# ---------------- TAB 3: WEEKLY PAYROLL ----------------

with tab_weekly:
    st.subheader("Weekly Tutor Payroll (Sunday Pay)")

    default_sunday_str = get_default_sunday_str()
    sunday_input = st.text_input(
        "Week Ending Sunday",
        value=default_sunday_str,
        help="Defaults to the upcoming Sunday (or today if today is Sunday).",
    )

    colw1, colw2 = st.columns(2)
    with colw1:
        if st.button("Show Weekly Totals"):
            try:
                sunday_iso = parse_date(sunday_input)
                info = compute_weekly_tutor_totals(ws, sunday_iso)

                st.write(f"Week: {info['start']} → {info['end']}")
                if not info["totals"]:
                    st.info("No non-Nitin tutor sessions in this week.")
                else:
                    for tutor_name, amt in sorted(info["totals"].items()):
                        st.write(f"**{tutor_name}**: ${amt:.2f}")
            except Exception as e:
                st.error(str(e))

    with colw2:
        if st.button("Mark Tutor Notes Paid for Week"):
            try:
                sunday_iso = parse_date(sunday_input)
                updated = mark_tutor_notes_paid(ws, sunday_iso)
                update_tutor_summary_sheet(gc, sheet_ref)
                if updated == 0:
                    st.info("No 'Pay <Tutor>' notes found for that week.")
                else:
                    st.success(f"{updated} tutor note(s) updated to 'Paid ...'.")
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
        if vals:
            st.table(vals)
        else:
            st.info("Summary sheet is empty.")
    except Exception:
        st.info("No summary sheet yet. Click 'Rebuild Summary' to create it.")
