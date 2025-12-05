# zoom_integration.py

import requests
from datetime import datetime
from email.message import EmailMessage
import smtplib
import uuid
import pytz

ZOOM_API_BASE = "https://api.zoom.us/v2"


def _get_zoom_access_token(zoom_cfg: dict) -> str:
    """
    Get Server-to-Server OAuth access token using your Zoom app credentials.
    zoom_cfg must contain account_id, client_id, client_secret.
    """
    account_id = zoom_cfg["account_id"]
    client_id = zoom_cfg["client_id"]
    client_secret = zoom_cfg["client_secret"]

    token_url = "https://zoom.us/oauth/token"
    resp = requests.post(
        token_url,
        params={"grant_type": "account_credentials", "account_id": account_id},
        auth=(client_id, client_secret),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def create_zoom_meeting(zoom_cfg: dict, topic: str, start_time_iso: str, duration_minutes: int) -> str:
    """
    Create a Zoom meeting for the main account user.
    Returns the join_url string.
    """
    token = _get_zoom_access_token(zoom_cfg)

    # Zoom expects UTC ISO time with Z suffix
    dt_local = datetime.fromisoformat(start_time_iso)
    dt_utc = dt_local.astimezone(pytz.UTC)
    start_time_zoom = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    payload = {
        "topic": topic,
        "type": 2,  # scheduled meeting
        "start_time": start_time_zoom,
        "duration": duration_minutes,
        "timezone": "UTC",
        "settings": {
            "join_before_host": False,
            "approval_type": 2,  # no registration
            "waiting_room": True,
        },
    }

    resp = requests.post(
        f"{ZOOM_API_BASE}/users/me/meetings",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["join_url"]


# ---------- EMAIL / ICS HELPERS ----------

def build_ics(
    summary: str,
    start_time_iso: str,
    duration_minutes: int,
    organizer_email: str,
    attendee_email: str | None,
) -> str:
    """
    Build a simple ICS calendar invite string.
    start_time_iso is local ISO (e.g. 2025-12-03T18:00:00).
    """
    dt_local = datetime.fromisoformat(start_time_iso)
    dt_end = dt_local + timedelta(minutes=duration_minutes)

    uid = uuid.uuid4().hex
    dtstamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    def fmt(dt: datetime) -> str:
        # floating time; Google will treat it as in account timezone
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
        f"DTSTART:{fmt(dt_local)}",
        f"DTEND:{fmt(dt_end)}",
        f"SUMMARY:{summary}",
        f"ORGANIZER:MAILTO:{organizer_email}",
    ]

    if attendee_email:
        lines.append(f"ATTENDEE;CN=Client;RSVP=TRUE:MAILTO:{attendee_email}")

    lines.extend([
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ])
    return "\r\n".join(lines)


def send_zoom_invite_email(
    email_cfg: dict,
    to_email: str,
    subject: str,
    body_text: str,
    ics_content: str,
):
    """
    Send an email with an ICS calendar invite attached.
    email_cfg = st.secrets['email'] with smtp_host, smtp_port, smtp_user, smtp_password
    """
    smtp_host = email_cfg["smtp_host"]
    smtp_port = int(email_cfg.get("smtp_port", 587))
    smtp_user = email_cfg["smtp_user"]
    smtp_password = email_cfg["smtp_password"]

    msg = EmailMessage()
    msg["From"] = smtp_user
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body_text)

    # ICS attachment
    msg.add_attachment(
        ics_content,
        subtype="calendar",
        maintype="text",
        filename="invite.ics",
        params={"method": "REQUEST"},
    )

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)
