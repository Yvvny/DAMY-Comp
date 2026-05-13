from __future__ import annotations

import re

from folder_manager.proofing_online.passwords import extract_proofing_password, strip_proofing_password

PHOTODECK_PUBLIC_BASE_URL = "https://theschoolphotocompany.photodeck.com/-/galleries"


def strip_trailing_gallery_password(text: str) -> str:
    """Remove the proof password suffix from a student gallery name/path source."""
    return strip_proofing_password(text)


def trailing_gallery_password(text: str) -> str:
    return extract_proofing_password(text)


def photodeck_url_path(text: str) -> str:
    """Deterministic PhotoDeck URL path used for created galleries and PDFs."""
    value = str(text or "").replace("[", " ").replace("]", " ")
    value = re.sub(r"[\u2013\u2014]", "-", value)
    value = re.sub(r"[^a-zA-Z0-9\s-]", " ", value)
    value = "-".join(value.lower().split())
    return re.sub(r"-{2,}", "-", value).strip("-")


def student_gallery_url_path(student_name: str, class_name: str | None = None) -> str:
    parts = [strip_trailing_gallery_password(str(student_name or "").strip())]
    if class_name:
        parts.append(str(class_name or "").strip())
    return photodeck_url_path(" ".join(part for part in parts if part))


def build_root_gallery_url(root_gallery_name: str) -> str:
    root_path = photodeck_url_path(root_gallery_name)
    if not root_path:
        raise ValueError("PhotoDeck root gallery name is required for PDF links.")
    return f"{PHOTODECK_PUBLIC_BASE_URL}/{root_path}"


def build_student_gallery_url(root_gallery_name: str, student_name: str, class_name: str | None = None) -> str:
    student_path = student_gallery_url_path(student_name, class_name)
    if not student_path:
        raise ValueError("PhotoDeck student gallery name is required for PDF links.")
    return f"{build_root_gallery_url(root_gallery_name)}/{student_path}/-/medias/first"
