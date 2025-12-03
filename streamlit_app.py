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

# ----- Simple password gate -----
def check_password():
    """Simple password-based auth using Streamlit secrets."""
    correct_pw = st.secrets.get("admin_password", "")
    if not correct_pw:
        st.error("Admin password not set in secrets (admin_password).")
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

# ----- App config -----
st.set_page_config(page_title="Soma's Tutoring Billing", layout="centered")

# ---------- Sidebar: connection + settings ----------

st.sidebar.title("Google Sheets Setup")

# Get sheet_ref from secrets (you already added this)
sheet_ref = st.secrets.get("sheet_ref", "").strip()

legacy_clients_str = st.sidebar.text_input(
    "Legacy Clients (comma-separated)",
    value="Brie, Rafi, Caylee, Rishi",
    help="Used to decide who gets legacy rates.",
)

legacy_clients = [x.strip() for x in legacy_clients_str.split(",") if x.strip()]

gc = None
sh = None
ws = None

if not sheet_ref:
    st.sidebar.error("❌ No sheet_ref found in secrets. Add sheet_ref to your Streamlit secrets.")
else:
    st.sidebar.write("📄 Using sheet:")
    st.sidebar.code(sheet_ref)

# ✅ Use Streamlit secrets for Google auth + sheet
if "gcp_service_account" in st.secrets and sheet_ref:
    try:
        gc = create_gc_from_info(dict(st.secrets["gcp_service_account"]))
        sh, ws = open_or_create_sheet(gc, sheet_ref)
        st.sidebar.success(f"✅ Connected to: {sh.title}")
    except Exception as e:
        st.sidebar.error(f"❌ Connection error: {e}")
else:
    if sheet_ref:
        st.sidebar.error("❌ gcp_service_account missing in secrets.")

# ✅ Test button to verify key + sheet access
if st.sidebar.button("✅ Test Google Connection"):
    if ws is None:
        st.sidebar.error("❌ Not connected. Check secrets (gcp_service_account + sheet_ref).")
    else:
        try:
            test_rows = ws.get_all_records()
            st.sidebar.success(f"✅ Connection OK! Found {len(test_rows)} rows in 'sessions'.")
        except Exception as e:
            st.sidebar.error(f"❌ Sheet read test failed: {e}")


def require_ws():
    if ws is None:
        st.error("❌ Not connected to Google Sheets. Check the sidebar/secrets.")
        st.stop()


# ---------- Main UI ----------

st.title("Soma's Tutoring – Billing Dashboard")

tab_log, tab_client, tab_weekly, tab_month = st.tabs(
    ["➕ Log Session", "💳 Client Payments", "📅 Weekly Tutor Payouts", "📊 Monthly Summary"]
)

# ---------- TAB 1: Log Session ----------

with tab_log:
    st.subheader("Log a New Session")

    require_ws()

    # Pull current data from sheet
    records = ws.get_all_records()
    existing_students = sorted(
        {r["student_name"] for r in records if r.get("student_name")}
    )
    existing_tutors = sorted(
        {r["tutor"] for r in records if r.get("tutor")}
    )

    # ----- student dropdown + optional new -----
    col1, col2 = st.columns(2)
    with col1:
        student_options = ["➕ New student…"] + existing_students
        default_idx = 1 if existing_students else 0
        selected_student_opt = st.selectbox(
            "Student Name", student_options, index=default_idx
        )

        if selected_student_opt == "➕ New student…":
            student = st.text_input("New student name", "")
        else:
            student = selected_student_opt

        date_str = st.text_input(
            "Date (YYYY-MM-DD or MM/DD/YYYY)",
            value=datetime.today().date().isoformat(),
        )

        service = st.selectbox("Service", SERVICES)

    with col2:
        minutes_text = st.text_input("Minutes (leave HH:MM empty if using this)", "")
        hhmm_text = st.text_input("HH:MM (leave Minutes empty if using this)", "")
        mode = st.selectbox("Mode", MODES)

    # ----- tutor dropdown + optional new -----
    col3, col4 = st.columns(2)
    with col3:
        # Always include Nitin in list
        if "Nitin" not in existing_tutors:
            existing_tutors.append("Nitin")
        existing_tutors = sorted(existing_tutors)

        tutor_options = ["➕ New tutor…"] + existing_tutors
        # default to Nitin if present, else first existing tutor
        default_tutor_idx = (
            tutor_options.index("Nitin") if "Nitin" in tutor_options else 1
        )
        selected_tutor_opt = st.selectbox(
            "Tutor", tutor_options, index=default_tutor_idx
        )

        if selected_tutor_opt == "➕ New tutor…":
            tutor = st.text_input("New tutor name", "")
        else:
            tutor = selected_tutor_opt

    with col4:
        paid_status = st.selectbox("Paid Status", PAID_OPTIONS, index=0)

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

            # Rebuild summary sheet
            update_tutor_summary_sheet(gc, sheet_ref)

            st.success(
                f"Saved session for {student} on {date_iso}. "
                f"Parent Pays: ${fin['amount_due']:.2f} • "
                f"Tutor Pay: ${fin['tutor_pay']:.2f} • Notes: {fin['notes']}"
            )
            st.info("New students/tutors will automatically appear in the dropdown after refresh.")
        except Exception as e:
            st.error(f"Error submitting session: {e}")


# ---------- TAB 2: Client Payments ----------

with tab_client:
    st.subheader("Mark Client as Paid")

    require_ws()

    records = ws.get_all_records()
    existing_students = sorted(
        {r["student_name"] for r in records if r.get("student_name")}
    )

    student_cp = st.selectbox(
        "Student Name",
        ["(type manually)"] + existing_students,
        index=1 if existing_students else 0,
    )
    if student_cp == "(type manually)":
        student_cp = st.text_input("Student name", "")

    date_cp = st.text_input(
        "Session Date (YYYY-MM-DD or MM/DD/YYYY)",
        value=datetime.today().date().isoformat(),
    )

    if st.button("Mark Client Paid"):
        try:
            if not student_cp.strip():
                st.error("Student cannot be empty.")
                st.stop()
            date_iso = parse_date(date_cp)
            count = mark_client_paid(ws, student_cp.strip(), date_iso)
            update_tutor_summary_sheet(gc, sheet_ref)
            if count == 0:
                st.info("No matching sessions found that were not already Paid.")
            else:
                st.success(
                    f"Updated {count} session(s) to Paid for {student_cp} on {date_iso}."
                )
        except Exception as e:
            st.error(f"Error marking paid: {e}")


# ---------- TAB 3: Weekly Tutor Payouts ----------

with tab_weekly:
    st.subheader("Weekly Tutor Payouts (Non-Nitin)")

    require_ws()

    sunday_input = st.text_input(
        "Week-ending Sunday (YYYY-MM-DD or MM/DD/YYYY)",
        value=datetime.today().date().isoformat(),
        help="Week is Monday–Sunday. Enter the Sunday date.",
    )

    colw1, colw2 = st.columns(2)
    with colw1:
        if st.button("Show Weekly Totals"):
            try:
                sunday_iso = parse_date(sunday_input)
                info = compute_weekly_tutor_totals(ws, sunday_iso)
                totals = info["totals"]
                if not totals:
                    st.info(
                        f"No non-Nitin tutor sessions found between "
                        f"{info['start']} and {info['end']}."
                    )
                else:
                    st.write(f"Week: **{info['start']} → {info['end']}**")
                    for tname, amt in sorted(totals.items()):
                        st.write(f"- **{tname}**: ${amt:.2f}")
            except Exception as e:
                st.error(f"Error computing weekly totals: {e}")

    with colw2:
        if st.button("Mark Tutor Notes Paid for Week"):
            try:
                sunday_iso = parse_date(sunday_input)
                updated = mark_tutor_notes_paid(ws, sunday_iso)
                update_tutor_summary_sheet(gc, sheet_ref)
                if updated == 0:
                    st.info("No 'Pay <Tutor>' notes found for that week.")
                else:
                    st.success(f"Updated {updated} note(s) from 'Pay ...' to 'Paid ...'.")
            except Exception as e:
                st.error(f"Error updating tutor notes: {e}")


# ---------- TAB 4: Monthly Summary ----------

with tab_month:
    st.subheader("Monthly Summary (Tutor + Business Earnings)")

    require_ws()

    if st.button("Rebuild tutor_summary sheet"):
        try:
            rows = update_tutor_summary_sheet(gc, sheet_ref)
            if rows:
                st.success("tutor_summary sheet rebuilt.")
            else:
                st.info("No data to summarize yet.")
        except Exception as e:
            st.error(f"Error rebuilding summary: {e}")

    # Show preview if exists
    try:
        summary_ws = sh.worksheet("tutor_summary")
        vals = summary_ws.get_all_values()
        if vals:
            st.markdown("### tutor_summary preview")
            st.table(vals)
    except Exception:
        st.info("No tutor_summary sheet yet. Click the button above to create it.")
