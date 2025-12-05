# zoom_integration.py
import base64
import requests
from typing import Dict

ZOOM_TOKEN_URL = "https://zoom.us/oauth/token"
ZOOM_CREATE_MEETING_URL = "https://api.zoom.us/v2/users/me/meetings"


def get_zoom_access_token(zoom_cfg: Dict[str, str]) -> str:
    """
    Use Server-to-Server OAuth client_credentials style to get an access token.
    zoom_cfg must contain: account_id, client_id, client_secret
    """
    account_id = zoom_cfg.get("account_id", "")
    client_id = zoom_cfg.get("client_id", "")
    client_secret = zoom_cfg.get("client_secret", "")

    if not account_id or not client_id or not client_secret:
        raise ValueError("Zoom config missing account_id/client_id/client_secret.")

    auth_str = f"{client_id}:{client_secret}".encode("utf-8")
    auth_b64 = base64.b64encode(auth_str).decode("utf-8")

    params = {
        "grant_type": "account_credentials",
        "account_id": account_id,
    }

    headers = {
        "Authorization": f"Basic {auth_b64}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    resp = requests.post(ZOOM_TOKEN_URL, params=params, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data["access_token"]


def create_zoom_meeting(
    zoom_cfg: Dict[str, str],
    topic: str,
    start_time_iso: str,
    duration_minutes: int,
) -> str:
    """
    Create a Zoom meeting and return the join_url.
    NOTE: This does NOT automatically email participants.
    """
    access_token = get_zoom_access_token(zoom_cfg)

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "topic": topic,
        "type": 2,  # scheduled
        "start_time": start_time_iso,
        "duration": duration_minutes,
        "settings": {
            "join_before_host": True,
        },
    }

    resp = requests.post(ZOOM_CREATE_MEETING_URL, headers=headers, json=payload)
    resp.raise_for_status()
    data = resp.json()
    return data["join_url"]
