from __future__ import annotations

import re
from typing import Dict, Optional, Sequence


class SmsConfigError(RuntimeError):
    """Raised when SMS sending is not configured correctly."""


def normalize_us_phone(raw_phone: str) -> str:
    value = str(raw_phone or "").strip()
    digits = re.sub(r"\D+", "", value)
    if value.startswith("+") and len(digits) >= 10:
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    raise ValueError(f"Invalid US phone number: {raw_phone}")


def send_sms(
    *,
    account_sid: str,
    auth_token: str,
    from_number: str,
    to_phone: str,
    body: str,
    media_urls: Optional[Sequence[str]] = None,
) -> Dict[str, str]:
    sid = str(account_sid or "").strip()
    token = str(auth_token or "").strip()
    sender = normalize_us_phone(from_number)
    recipient = normalize_us_phone(to_phone)
    message_body = str(body or "").strip()

    if not sid or not token or not sender:
        raise SmsConfigError("Twilio Account SID, Auth Token, and From Number are required.")
    if not message_body:
        raise ValueError("Text message body cannot be empty.")

    try:
        from twilio.rest import Client
    except ImportError as exc:
        raise SmsConfigError(
            "The Twilio Python package is not installed. Install project requirements again."
        ) from exc

    client = Client(sid, token)
    payload = {
        "body": message_body,
        "from_": sender,
        "to": recipient,
    }
    clean_media_urls = [str(url or "").strip() for url in (media_urls or []) if str(url or "").strip()]
    if clean_media_urls:
        payload["media_url"] = clean_media_urls
    message = client.messages.create(**payload)
    return {
        "sid": str(getattr(message, "sid", "") or ""),
        "status": str(getattr(message, "status", "") or ""),
        "from": sender,
        "to": recipient,
    }
