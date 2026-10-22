"""WS-Security UsernameToken verification (PasswordDigest and PasswordText)."""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

from milesight_onvif_proxy.onvif.soap_utils import find_text, local_name

# Operations the ONVIF core spec allows without authentication.
UNAUTHENTICATED_ACTIONS = frozenset(
    {
        "GetSystemDateAndTime",
        "GetServices",
        "GetServiceCapabilities",
        "GetCapabilities",
        "GetWsdlUrl",
        "GetEndpointReference",
    }
)

_MAX_CLOCK_SKEW_S = 300


def make_digest(nonce: bytes, created: str, password: str) -> str:
    return base64.b64encode(
        hashlib.sha1(nonce + created.encode() + password.encode()).digest()
    ).decode()


def _parse_created(created: str) -> datetime | None:
    try:
        return datetime.fromisoformat(created)
    except ValueError:
        return None


def verify_username_token(
    header: ET.Element | None, username: str, password: str, now: datetime | None = None
) -> bool:
    if header is None:
        return False
    token = next((e for e in header.iter() if local_name(e.tag) == "UsernameToken"), None)
    if token is None or find_text(token, "Username") != username:
        return False
    password_el = next((e for e in token if local_name(e.tag) == "Password"), None)
    if password_el is None or password_el.text is None:
        return False
    if password_el.get("Type", "").endswith("PasswordText"):
        return hmac.compare_digest(password_el.text, password)

    nonce_text, created = find_text(token, "Nonce"), find_text(token, "Created")
    if not nonce_text or not created:
        return False
    when = _parse_created(created)
    now = now or datetime.now(UTC)
    if when is None or when.tzinfo is None or abs((now - when).total_seconds()) > _MAX_CLOCK_SKEW_S:
        return False
    try:
        nonce = base64.b64decode(nonce_text)
    except ValueError:
        return False
    return hmac.compare_digest(password_el.text, make_digest(nonce, created, password))
