from __future__ import annotations

import re
from typing import Optional, Tuple

PROOFING_DAYS = [
    "1. Proofs",
    "2. Sort",
    "3. Upload & PDFs",
    "4. School / Parent Delivery",
    "5. Finished",
    "6. Edit",
    "7. Print",
    "8. Package",
    "9. Deliver",
]

PROOFING_DAY_TO_STAGE = {
    "1. Proofs": 1,
    "2. Sort": 2,
    "3. Upload & PDFs": 3,
    "4. School / Parent Delivery": 4,
    "5. Finished": 5,
    "6. Edit": 6,
    "7. Print": 7,
    "8. Package": 8,
    "9. Deliver": 9,
}

PROOFING_DAY_PREFIX_ALIASES = {
    "2. Sort": ("2. Sort", "2. Photodeck Upload"),
    "3. Upload & PDFs": (
        "3. Upload & PDFs",
        "3. Photodeck Upload/PDF Packets",
        "3. Photodeck Upload",
        "3. PDF Packets",
        "3. Upload/PDF Packets",
    ),
}

PROOFING_EDIT_STAGE = 6
PROOFING_PRINT_STAGE = 7
PROOFING_PACKAGE_STAGE = 8
PROOFING_DELIVER_STAGE = 9


def day_prefixes(day: str) -> Tuple[str, ...]:
    return PROOFING_DAY_PREFIX_ALIASES.get(day, (day,))


def matches_day(folder_name: str, day: str) -> bool:
    return any(prefix in folder_name for prefix in day_prefixes(day))


def extract_day_suffix(folder_name: str, day: str) -> str:
    for prefix in day_prefixes(day):
        if prefix in folder_name:
            return folder_name.split(prefix, 1)[-1].strip()
    return folder_name.strip()


def detect_day_from_folder_name(folder_name: str) -> Optional[str]:
    for day in PROOFING_DAYS:
        if matches_day(folder_name, day):
            return day
    return None


def stage_for_day(day: str) -> Optional[int]:
    try:
        return int(PROOFING_DAY_TO_STAGE.get(day, 0)) or None
    except Exception:
        return None


def strip_stage_prefix_text(value: str) -> str:
    text = str(value or "").strip()
    match = re.match(r"^\s*(?:[1-9]|1[0-9])\s*[\.\-_)]+\s*(.*)$", text)
    if not match:
        return text
    return str(match.group(1) or "").strip() or text
