from __future__ import annotations

import traceback
from dataclasses import dataclass


@dataclass
class UserFacingError(Exception):
    message: str
    next_step: str = ""
    checked: str = ""

    def __str__(self) -> str:
        return self.message


@dataclass
class DeveloperError(Exception):
    message: str
    detail: str = ""

    def __str__(self) -> str:
        return self.message


def error_detail(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()


def split_user_error(error: object) -> tuple[str, str]:
    if isinstance(error, UserFacingError):
        return str(error).strip() or "The action failed.", ""
    if isinstance(error, DeveloperError):
        return str(error).strip() or "The action failed.", str(error.detail or "").strip()

    raw = str(error or "").strip()
    if not raw:
        return "The action failed.", ""
    marker = "Traceback (most recent call last):"
    if marker in raw:
        user_text = raw.split(marker, 1)[0].strip()
        return user_text or "The action failed.", raw
    return raw, ""


def friendly_parent_delivery_error(exc: Exception) -> tuple[str, str]:
    detail = str(exc or "").strip()
    user_text = detail.split("Traceback (most recent call last):", 1)[0].strip()

    if "PDFs folder not found:" in user_text:
        missing_path = user_text.split("PDFs folder not found:", 1)[-1].strip()
        return (
            "Parent Delivery needs the Stage 3 PDFs, but the PDFs folder was not found.",
            f"Checked folder:\n{missing_path}\n\nRun Stage 3 Upload & PDFs again, then retry Parent Delivery.",
        )

    if "No PDF files found under:" in user_text:
        pdf_path = user_text.split("No PDF files found under:", 1)[-1].strip()
        return (
            "Parent Delivery found the PDFs folder, but there are no PDF files inside it.",
            f"Checked folder:\n{pdf_path}\n\nRun Stage 3 Upload & PDFs again, then retry Parent Delivery.",
        )

    if "Missing Cloudflare R2 settings" in user_text:
        return (
            "Parent Delivery needs Cloudflare R2 settings before it can publish preview links.",
            user_text,
        )

    return (
        user_text or "Parent Delivery could not be prepared.",
        "Check the Stage 3 PDFs, then try again.",
    )
