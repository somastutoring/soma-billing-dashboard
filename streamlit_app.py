#!/usr/bin/env python3
import uuid
import smtplib
from email.message import EmailMessage
from datetime import datetime, date, time, timedelta

import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import requests
import pandas as pd

# -----------------------------------------------------------------------------
# CONSTANTS / PRICING
# -----------------------------------------------------------------------------

SERVICES = ["K–12 Tutoring", "SAT & ACT Prep", "College & AP Courses"]
MODES = ["Online", "In-Person"]
PAID_OPTIONS = ["Not Paid", "Paid", "Free session"]

LEGACY_RATES = {
    "K–12 Tutoring": 25.0,
    "SAT & ACT Prep": 35.0,
    "College & AP Courses": 30.0,
}

NEW_RATES = {
    "K–12 Tutoring": {"Online": 30.0, "In-Person": 40.0},
    "SAT & ACT Prep": {"Online": 35.0, "In-Person": 45.0},
    "College & AP Courses": {"Online": 40.0, "In-Person": 50.0},
}

SCOPES_SHEETS = ["https://www.googleapis.com/auth/spreadsheets"]
SCOPES_CAL = ["https://www.googleapis.com/auth/calendar"]


# -----------------------------------------------------------------------------
# GOOGLE CLIENTS
# -----------------------------------------------------------------------------

@st.cache_resource
def get_gspread_client():
    sa_info = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES_SHEETS)
    return gspread.authorize(creds)


@st.cache_resource
def get_calendar_service():
    sa_info = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES_CAL)
    return build("calendar", "v3", credentials=creds)


@st.cache_resource
def get_workbook_and_sessions():
    gc = get_gspread_client()
    sheet_ref = st.secrets["sheet_ref"]
    if sheet_ref.startswith("http"):
        sh = gc.open_by_url(sheet_ref)
    else:
        sh = gc.open(sheet_ref)

    try:
        ws = sh.worksheet("sessions")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="sessions", rows=1000, cols=20)
        ws.append_row(
            [
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
        )
    return sh, ws


def require_ws():
    try:
        sh, ws = get_workbook_and_sessions()
        st.session_state["sh"] = sh
        st.session_state["ws"] = ws
        st.success(f"Connected to: {sh.title}")
        return sh, ws
    except Exception as e:
        st.error(f"Google Sheets connection failed: {e}")
        st.stop()


# -----------------------------------------------------------------------------
# STUDENT EMAIL SHEET HELPERS
# -----------------------------------------------------------------------------

def get_student_email_sheet(sh):
    try:
        ws = sh.worksheet("Student Email")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Student Email", rows=200, cols=2)
        ws.append_row(["student_name", "email"])
    return ws


def get_student_email(sh, student_name):
    ws_email = get_student_email_sheet(sh)
    records = ws_email.get_all_records()
    for r in records:
        if (r.get("student_name") or "").strip().lower() == student_name.strip().lower():
            return (r.get("email") or "").strip()
    return ""


def save_student_email(sh, student_name, email):
    ws_email = get_student_email_sheet(sh)
    records = ws_email.get_all_records()
    for idx, r in enumerate(records, start=2):
        if (r.get("student_name") or "").strip().lower() == student_name.strip().lower():
            ws_email.update_acell(f"A{idx}", student_name)
            ws_email.update_acell(f"B{idx}", email)
            return
    ws_email.append_row([student_name, email])


# -----------------------------------------------------------------------------
# BILLING HELPERS
# -----------------------------------------------------------------------------

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


def get_legacy_clients():
    # matches your secrets: legacy_clients = ["Brie", "Rafi", "Caylee", "Rishi"]
    if "legacy_clients" in st.secrets:
        val = st.secrets["legacy_clients"]
        if isinstance(val, (list, tuple)):
            return [str(x) for x in val]
        if isinstance(val, str):
            return [s.strip() for s in val.split(",") if s.strip()]
    return ["Brie", "Rafi", "Caylee", "Rishi"]


def get_rate_for_student(student_name, service_name, mode_name):
    legacy = get_legacy_clients()
    if student_name.strip().lower() in [s.lower() for s in legacy]:
        return "Legacy", LEGACY_RATES[service_name]
    return "New", NEW_RATES[service_name][mode_name]


def compute_unpaid_df(records):
    df = pd.DataFrame(records)
    if df.empty or "paid_status" not in df.columns:
        return pd.DataFrame()
    return df[df["paid_status"].str.lower() == "not paid"]


def get_next_sunday(from_date=None):
    if from_date is None:
        from_date = datetime.today().date()
    offset = (6 - from_date.weekday()) % 7
    if offset == 0:
        offset = 7
    return from_date + timedelta(days=offset)


# -----------------------------------------------------------------------------
# ZOOM / EMAIL / CALENDAR HELPERS
# -----------------------------------------------------------------------------

def create_zoom_meeting(zoom_cfg, topic, start_time_iso, duration_minutes):
    account_id = zoom_cfg["account_id"]
    client_id = zoom_cfg["client_id"]
    client_secret = zoom_cfg["client_secret"]

    token_url = (
        f"https://zoom.us/oauth/token?grant_type=account_credentials"
        f"&account_id={account_id}"
    )
    resp = requests.post(token_url, auth=(client_id, client_secret))
    resp.raise_for_status()
    access_token = resp.json()["access_token"]

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    body = {
        "topic": topic,
        "type": 2,
        "start_time": start_time_iso,
        "duration": duration_minutes,
        "timezone": "America/New_York",
        "settings": {
            "join_before_host": False,
            "waiting_room": True,
        },
    }

    resp = requests.post(
        "https://api.zoom.us/v2/users/me/meetings", json=body, headers=headers
    )
    resp.raise_for_status()
    data = resp.json()
    return data["join_url"]


def build_ics(summary, start_dt, duration_minutes, organizer_email,
              attendee_email=None, extra_attendee_email=None):
    dt_end = start_dt + timedelta(minutes=duration_minutes)
    uid = uuid.uuid4().hex
    dtstamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    def fmt(dt):
        return dt.strftime("%Y%m%dT%H%M00")

    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//Soma Tutoring//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{fmt(start_dt)}",
        f"DTEND:{fmt(dt_end)}",
        f"SUMMARY:{summary}",
        f"ORGANIZER:MAILTO:{organizer_email}",
    ]
    if attendee_email:
        lines.append(
            f"ATTENDEE;CN=Client;RSVP=TRUE:MAILTO:{attendee_email}"
        )
    if extra_attendee_email:
        lines.append(
            f"ATTENDEE;CN=GroupCalendar;RSVP=FALSE:MAILTO:{extra_attendee_email}"
        )

    lines.extend(["END:VEVENT", "END:VCALENDAR", ""])
    return "\r\n".join(lines)


def send_invite_email(email_cfg, to_email, subject, body_text,
                      ics_content, extra_recipients=None):
    smtp_host = email_cfg["smtp_host"]
    smtp_port = int(email_cfg.get("smtp_port", 587))
    smtp_user = email_cfg["smtp_user"]
    smtp_password = email_cfg["smtp_password"]

    msg = EmailMessage()
    recipients = [to_email] if to_email else []
    if extra_recipients:
        recipients.extend([r for r in extra_recipients if r])

    if not recipients:
        raise ValueError("No recipients to send email to.")

    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body_text)

    msg.add_attachment(
        ics_content,
        subtype="calendar",
        filename="invite.ics",
        params={"method": "REQUEST"},
    )

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)


def add_event_to_calendar(calendar_id, summary, start_dt, duration_minutes, zoom_link):
    service = get_calendar_service()
    event = {
        "summary": summary,
        "description": f"Zoom link: {zoom_link}",
        "start": {"dateTime": start_dt.isoformat()},
        "end": {
            "dateTime": (start_dt + timedelta(minutes=duration_minutes)).isoformat()
        },
    }
    service.events().insert(calendarId=calendar_id, body=event).execute()


# -----------------------------------------------------------------------------
# STREAMLIT APP
# -----------------------------------------------------------------------------

st.set_page_config(
    page_title="Soma's Tutoring – Billing Dashboard",
    page_icon="📚",
    layout="wide",
)

st.title("Soma's Tutoring – Billing Dashboard")

sh, ws = require_ws()

tab_log, tab_payments, tab_payouts, tab_monthly, tab_zoom = st.tabs(
    [
        "➕ Log Session",
        "💳 Client Payments",
        "📅 Weekly Tutor Payouts",
        "📊 Monthly Summary",
        "📹 Schedule Zoom Meeting",
    ]
)

# -----------------------------------------------------------------------------
# TAB 1 – LOG SESSION
# -----------------------------------------------------------------------------

with tab_log:
    st.subheader("Log a New Session")

    records = ws.get_all_records()
    existing_students = sorted(
        {r["student_name"] for r in records if r.get("student_name")}
    )
    existing_tutors = sorted(
        {r["tutor"] for r in records if r.get("tutor")}
    )

    col1, col2 = st.columns(2)

    with col1:
        student_opts = ["➕ New student…"] + existing_students
        student_pick = st.selectbox("Student Name", student_opts, key="log_student")

        if student_pick == "➕ New student…":
            student = st.text_input("New Student Name", key="log_new_student")
        else:
            student = student_pick

        date_str = st.text_input(
            "Date (YYYY-MM-DD or MM/DD/YYYY)",
            value=datetime.today().date().isoformat(),
            key="log_date",
        )

        minutes_text = st.text_input("Minutes", value="", key="log_minutes")
        hhmm_text = st.text_input(
            "HH:MM (leave Minutes empty if using this)",
            value="",
            key="log_hhmm",
        )

        service = st.selectbox("Service", SERVICES, key="log_service")
        mode = st.selectbox("Mode", MODES, key="log_mode")

    with col2:
        tutor_opts = ["Nitin"] + [t for t in existing_tutors if t != "Nitin"]
        tutor = st.selectbox("Tutor", tutor_opts, key="log_tutor")

        paid_status = st.selectbox("Paid Status", PAID_OPTIONS, key="log_paid")

        notes = st.text_input(
            "Notes (auto-filled for tutor pay)", value="", key="log_notes"
        )

        # Live preview
        try:
            if student and service in LEGACY_RATES and mode in MODES:
                tier, hourly_rate = get_rate_for_student(student, service, mode)
                total_minutes = None
                if minutes_text.strip():
                    total_minutes = int(minutes_text)
                elif ":" in hhmm_text:
                    h, m = hhmm_text.split(":", 1)
                    total_minutes = int(h) * 60 + int(m)

                if total_minutes and total_minutes > 0:
                    hours_decimal = hours_from_minutes(total_minutes)
                    full_amount = round(hours_decimal * hourly_rate, 2)
                    is_free = paid_status.lower().startswith("free")
                    amount_due = 0.0 if is_free else full_amount

                    if tutor == "Nitin":
                        tutor_pay = amount_due
                    else:
                        if is_free:
                            tutor_pay = round(full_amount / 2.0, 2)
                        else:
                            tutor_pay = round(amount_due / 2.0, 2)

                    st.markdown(
                        f"**Rate:** ${hourly_rate:.2f}/hr ({tier})  \n"
                        f"**Parent Amount Due:** ${amount_due:.2f}  \n"
                        f"**Tutor Pay Preview ({tutor}):** ${tutor_pay:.2f}"
                    )
        except Exception:
            pass

    if st.button("Submit Session", type="primary"):
        try:
            if not student.strip():
                raise ValueError("Student cannot be empty.")

            date_iso = parse_date(date_str)
            minutes_val = parse_duration(minutes_text, hhmm_text)
            hours_decimal = hours_from_minutes(minutes_val)

            if service not in LEGACY_RATES:
                raise ValueError("Pick a valid Service.")

            tier, hourly_rate = get_rate_for_student(student, service, mode)
            full_amount = round(hours_decimal * hourly_rate, 2)
            is_free = paid_status.lower().startswith("free")
            amount_due = 0.0 if is_free else full_amount

            if tutor == "Nitin":
                tutor_pay = amount_due
            else:
                if is_free:
                    tutor_pay = round(full_amount / 2.0, 2)
                else:
                    tutor_pay = round(amount_due / 2.0, 2)

            auto_notes = f"Pay {tutor} ${tutor_pay:.2f}"
            if not notes.strip() or notes.startswith("Pay "):
                notes = auto_notes

            existing = ws.get_all_records()
            serial = (
                sum(
                    1
                    for r in existing
                    if (
                        r.get("date") == date_iso
                        and (r.get("student_name") or "").strip().lower()
                        == student.strip().lower()
                    )
                )
                + 1
            )

            rid = f"{date_iso.replace('-','')}-{student.lower().replace(' ','_')}-{serial}"

            ws.append_row(
                [
                    rid,
                    student,
                    date_iso,
                    str(minutes_val),
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

            st.success(
                f"Submitted: {student} on {date_iso} • {hours_decimal:.2f} hrs • "
                f"{tier} ${hourly_rate:.2f}/hr • Parent Pays ${amount_due:.2f}"
            )

        except Exception as e:
            st.error(f"Submit error: {e}")


# -----------------------------------------------------------------------------
# TAB 2 – CLIENT PAYMENTS (UNPAID)
# -----------------------------------------------------------------------------

with tab_payments:
    st.subheader("Unpaid Sessions")
    records = ws.get_all_records()
    df_unpaid = compute_unpaid_df(records)
    if df_unpaid.empty:
        st.info("All sessions are marked as paid or free. 🎉")
    else:
        st.dataframe(df_unpaid)


# -----------------------------------------------------------------------------
# TAB 3 – WEEKLY TUTOR PAYOUTS (simple view)
# -----------------------------------------------------------------------------

with tab_payouts:
    st.subheader("Weekly Tutor Payouts (simple summary)")

    records = ws.get_all_records()
    if not records:
        st.info("No sessions yet.")
    else:
        df = pd.DataFrame(records)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        this_monday = datetime.today().date() - timedelta(
            days=datetime.today().weekday()
        )
        next_sunday = this_monday + timedelta(days=6)

        st.write(f"Showing week: **{this_monday} → {next_sunday}**")

        in_range = (df["date"].dt.date >= this_monday) & (
            df["date"].dt.date <= next_sunday
        )
        dfw = df[in_range]

        if dfw.empty:
            st.info("No sessions in this week.")
        else:
            dfw["amount_due"] = pd.to_numeric(dfw["amount_due"], errors="coerce").fillna(
                0.0
            )
            tutor_totals = dfw.groupby("tutor")["amount_due"].sum().reset_index()
            st.dataframe(tutor_totals)


# -----------------------------------------------------------------------------
# TAB 4 – MONTHLY SUMMARY (simple)
# -----------------------------------------------------------------------------

with tab_monthly:
    st.subheader("Monthly Summary")

    records = ws.get_all_records()
    if not records:
        st.info("No sessions yet.")
    else:
        df = pd.DataFrame(records)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["year_month"] = df["date"].dt.to_period("M").astype(str)
        df["amount_due"] = pd.to_numeric(df["amount_due"], errors="coerce").fillna(0.0)

        months = sorted(df["year_month"].unique())
        month = st.selectbox(
            "Select month (YYYY-MM)", months, index=len(months) - 1
        )

        dfm = df[df["year_month"] == month]

        total_revenue = dfm["amount_due"].sum()

        st.write(f"**Total business revenue for {month}: ${total_revenue:.2f}**")

        by_tutor = dfm.groupby("tutor")["amount_due"].sum().reset_index()
        st.write("**Amount due per tutor (not split):**")
        st.dataframe(by_tutor)


# -----------------------------------------------------------------------------
# TAB 5 – SCHEDULE ZOOM MEETING
# -----------------------------------------------------------------------------

with tab_zoom:
    st.subheader("Schedule Zoom Meeting")
    ws_records = ws.get_all_records()
    existing_students = sorted(
        {r["student_name"] for r in ws_records if r.get("student_name")}
    )

    zoom_cfg = st.secrets.get("zoom", {})

    if (
        not zoom_cfg
        or not zoom_cfg.get("account_id")
        or not zoom_cfg.get("client_id")
        or not zoom_cfg.get("client_secret")
    ):
        st.warning(
            "Zoom API credentials are not fully set in secrets. "
            "Fill in [zoom] in your secrets to enable this tab."
        )
    else:
        colz1, colz2 = st.columns(2)

        with colz1:
            student_opts = ["➕ New student…"] + existing_students
            student_pick = st.selectbox(
                "Student", student_opts, key="zoom_student_pick"
            )

            if student_pick == "➕ New student…":
                student = st.text_input(
                    "New student name (for Zoom)", key="zoom_new_student"
                )
            else:
                student = student_pick

            existing_email = ""
            if student and student_pick != "➕ New student…":
                existing_email = get_student_email(sh, student) or ""

            email = st.text_input(
                "Student email (for Zoom invite)",
                value=existing_email,
                key="zoom_email",
            )

            date_str = st.text_input(
                "Meeting Date (YYYY-MM-DD)",
                value=datetime.today().date().isoformat(),
                key="zoom_date",
            )

            service_for_title = st.selectbox(
                "Service (for meeting title only)",
                SERVICES,
                key="zoom_service",
            )

        with colz2:
            duration_text = st.text_input(
                "Duration (minutes)", value="60", key="zoom_minutes"
            )
            default_time = datetime.now().replace(
                minute=0, second=0, microsecond=0
            ).time()
            start_time = st.time_input(
                "Meeting Start Time",
                value=default_time,
                key="zoom_time",
            )
            mode_for_info = st.selectbox(
                "Mode (just for info)",
                MODES,
                key="zoom_mode",
            )

        if st.button("Create Zoom Meeting", type="primary"):
            try:
                if not student or not student.strip():
                    st.error("Student name is required.")
                    st.stop()

                if email.strip():
                    save_student_email(sh, student.strip(), email.strip())

                date_iso = parse_date(date_str)
                duration_minutes = int(duration_text)

                meeting_date = datetime.strptime(date_iso, "%Y-%m-%d").date()
                start_dt = datetime.combine(meeting_date, start_time)

                topic = f"{student} – {service_for_title}"

                start_time_iso = start_dt.isoformat()
                zoom_link = create_zoom_meeting(
                    zoom_cfg=zoom_cfg,
                    topic=topic,
                    start_time_iso=start_time_iso,
                    duration_minutes=duration_minutes,
                )

                st.success("✅ Zoom meeting created successfully!")
                st.write("**Topic:**", topic)
                st.write("**Start:**", start_time_iso)
                st.write("**Duration:**", f"{duration_minutes} minutes")
                st.markdown(f"**Join link:** [{zoom_link}]({zoom_link})")

                # ------------ 1) EMAIL + ICS INVITE ------------
                email_cfg = dict(st.secrets.get("email", {}))
                group_cal_email = email_cfg.get("group_calendar")

                if email_cfg.get("smtp_user"):
                    ics = build_ics(
                        summary=topic,
                        start_dt=start_dt,
                        duration_minutes=duration_minutes,
                        organizer_email=email_cfg["smtp_user"],
                        attendee_email=email or None,
                        extra_attendee_email=group_cal_email,
                    )

                    body = (
                        f"Hi,\n\n"
                        f"Here is your Zoom session for {topic}.\n\n"
                        f"Join link: {zoom_link}\n\n"
                        f"Best,\nSoma's Tutoring"
                    )

                    try:
                        send_invite_email(
                            email_cfg=email_cfg,
                            to_email=email or group_cal_email,
                            subject=topic,
                            body_text=body,
                            ics_content=ics,
                            extra_recipients=(
                                [group_cal_email]
                                if email and group_cal_email
                                else None
                            ),
                        )
                        st.info(
                            "📧 Email invite with calendar event sent "
                            "to the client and shared calendar email."
                        )
                    except Exception as e:
                        st.warning(f"Zoom created, but email failed: {e}")
                else:
                    st.warning(
                        "Zoom created, but no SMTP credentials found so no email was sent."
                    )

                # ------------ 2) ADD EVENT TO GOOGLE CALENDAR ------------
                calendar_id = None
                cal_cfg = st.secrets.get("calendar")
                if isinstance(cal_cfg, dict):
                    calendar_id = cal_cfg.get("calendar_id")

                if not calendar_id:
                    calendar_id = group_cal_email

                if calendar_id:
                    try:
                        add_event_to_calendar(
                            calendar_id=calendar_id,
                            summary=topic,
                            start_dt=start_dt,
                            duration_minutes=duration_minutes,
                            zoom_link=zoom_link,
                        )
                        st.success("📅 Event also added to the shared Google Calendar.")
                    except Exception as e:
                        st.warning(
                            f"Zoom created, but calendar event failed: {e}"
                        )
                else:
                    st.info(
                        "Calendar ID missing — event not added to shared Google Calendar."
                    )

            except Exception as e:
                st.error(f"Error creating Zoom meeting: {e}")
