from __future__ import annotations

import re

PROOFING_PASSWORD_RE = re.compile(r"^(?P<name>.*?)[\s._-]+(?P<password>\d{4})[\s._-]*$")


def extract_proofing_password(value: object) -> str:
    """Return the trailing four-digit proof password from a sorted student folder name."""
    text = str(value or "").strip()
    match = PROOFING_PASSWORD_RE.match(text)
    return match.group("password") if match else ""


def strip_proofing_password(value: object) -> str:
    """Return the student gallery name without the trailing proof password."""
    text = str(value or "").strip()
    if not text:
        return ""
    match = PROOFING_PASSWORD_RE.match(text)
    if not match:
        return text
    cleaned = str(match.group("name") or "").strip(" ._-")
    return cleaned or text


def format_proofing_password_folder_name(name: object, password: object) -> str:
    """Canonical Stage 2 folder format: Name_1234_."""
    clean_name = str(name or "").strip(" ._-")
    clean_password = str(password or "").strip()
    if not clean_name:
        clean_name = "Unassigned"
    if not re.fullmatch(r"\d{4}", clean_password):
        raise ValueError("Proofing password must be exactly four digits.")
    return f"{clean_name}_{clean_password}_"
