from __future__ import annotations

import argparse
import base64
import html
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pypdf import PdfReader, PdfWriter

try:
    from folder_manager.config import (
        CALENDAR_CREDENTIALS_PATH,
        DB_HOST,
        DB_NAME,
        DB_PASS,
        DB_PORT,
        DB_USER,
        ORDER_IMPORT_CREDENTIALS_PATH,
        ORDER_IMPORT_TOKEN_PATH,
    )
    from folder_manager.db import DB, STAGES, WORKFLOW_DOMAIN_PREPAID
except Exception:
    DB = None
    STAGES = []
    WORKFLOW_DOMAIN_PREPAID = "prepaid"
    DB_HOST = DB_NAME = DB_PASS = DB_USER = None
    DB_PORT = 5432
    CALENDAR_CREDENTIALS_PATH = os.path.join("folder_manager", "calendar_import_v3", "credentials.json")
    ORDER_IMPORT_CREDENTIALS_PATH = CALENDAR_CREDENTIALS_PATH
    ORDER_IMPORT_TOKEN_PATH = os.path.join("folder_manager", "order_import_v1", "token.json")


SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_BASE_PATH = r"T:\DAMY"
DEFAULT_TEST_LABEL = "GODADDY ORDER"
DEFAULT_IMPORTED_LABEL = "GODADDY IMPORTED"
ORDER_IMPORT_SOURCE = "godaddy"
EXIT_CODE_CANCELLED = 41
PREPAID_WORKFLOW_DOMAIN = str(WORKFLOW_DOMAIN_PREPAID or "prepaid").strip().lower() or "prepaid"
UPCOMING_STAGE = 1

ORDER_PDF_RE = re.compile(
    r"^(?P<date>\d{6})\s+(?P<pid>P\d{7,})\s+Ordered\s+(?P<count>\d+)\.pdf$",
    re.IGNORECASE,
)
PID_RE = re.compile(r"\bP\d{7,}\b", re.IGNORECASE)
ORDER_NO_RE = re.compile(r"\bR\d{6,}\b", re.IGNORECASE)
INVALID_FS_CHARS_RE = re.compile(r'[\\/:*?"<>|]+')
MULTISPACE_RE = re.compile(r"\s+")
PRINT_STYLE_BLOCK = """
<style>
@page {
  size: Letter portrait;
  margin: 0.22in;
}
html, body {
  margin: 0 !important;
  padding: 0 !important;
  background: #ffffff !important;
}
body {
  -webkit-print-color-adjust: exact !important;
  print-color-adjust: exact !important;
  zoom: 0.96;
}
img, svg, table, thead, tbody, tfoot, tr, td, th, p, h1, h2, h3, h4, h5, h6, section, article, aside, div {
  break-inside: avoid;
  page-break-inside: avoid;
}
body > *:last-child {
  break-after: avoid;
  page-break-after: avoid;
}
</style>
""".strip()


@dataclass(frozen=True)
class MessageSortRow:
    message_id: str
    internal_date_ms: int


@dataclass(frozen=True)
class ParsedOrder:
    message_id: str
    pid: str | None
    order_no: str
    pid_auto_corrected: bool
    pid_raw: str | None
    school_name: str
    order_date: date
    subject: str
    html_body: str
    plain_body: str


@dataclass(frozen=True)
class FolderCandidate:
    name: str
    path: Path
    pids: tuple[str, ...]
    school_text: str
    school_norm: str
    item_id: int | None = None
    stage: int | None = None
    source: str = "disk"


class OrderImportCancelled(RuntimeError):
    pass


class OrderImportMessageError(RuntimeError):
    def __init__(
        self,
        *,
        message_id: str,
        reason: str,
        detail: str,
        subject: str = "",
        from_header: str = "",
        header_date: str = "",
        pid: str | None = None,
        order_no: str | None = None,
        school_name: str = "",
    ):
        super().__init__(detail or reason or "Order import failed")
        self.message_id = str(message_id or "").strip()
        self.reason = str(reason or "").strip() or "Other error"
        self.detail = str(detail or "").strip()
        self.subject = str(subject or "").strip()
        self.from_header = str(from_header or "").strip()
        self.header_date = str(header_date or "").strip()
        self.pid = str(pid or "").strip() or None
        self.order_no = str(order_no or "").strip() or None
        self.school_name = str(school_name or "").strip()


@dataclass
class AppliedAction:
    message_id: str
    pid: str | None
    order_no: str | None
    folder_name: str
    folder_path: str
    folder_action: str
    item_id: int | None
    db_inserted_item_id: int | None
    label_moved: bool
    pdf_output_path: str | None
    previous_pdf_path: str | None
    previous_pdf_backup_path: str | None
    previous_db_pdf_path: str | None
    order_history_recorded: bool


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import GoDaddy order emails into DAMY folders.")
    parser.add_argument("--order-import", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--base-dir",
        default=None,
        help=(
            "Override DAMY root directory. If omitted, uses DAMY_ORDER_BASE_DIR, "
            "DAMY_BASE_DIR, DAMY_ORDER_TEST_BASE_DIR, DAMY_TEST_BASE_DIR, then T:\\DAMY."
        ),
    )
    parser.add_argument(
        "--source-base-dir",
        default=None,
        help=(
            "Optional fallback DAMY source directory. "
            "When a DB-matched folder exists in source but not in the active base dir, "
            "it is copied into the active base first."
        ),
    )
    parser.add_argument(
        "--token-path",
        default=None,
        help="Override Gmail OAuth token path.",
    )
    parser.add_argument(
        "--credentials-path",
        default=None,
        help="Override Gmail OAuth credentials path.",
    )
    parser.add_argument(
        "--test-label",
        default=DEFAULT_TEST_LABEL,
        help=f"Gmail label to import from. Default: {DEFAULT_TEST_LABEL}",
    )
    parser.add_argument(
        "--imported-label",
        default=DEFAULT_IMPORTED_LABEL,
        help=f"Gmail label to add after success. Default: {DEFAULT_IMPORTED_LABEL}",
    )
    parser.add_argument(
        "--no-label-update",
        action="store_true",
        help="Do not change Gmail labels after successful processing.",
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        default=0,
        help="Optional cap for oldest messages to process. 0 means all.",
    )
    parser.add_argument(
        "--label-window",
        type=int,
        default=0,
        help=(
            "Only fetch first N messages currently listed under target label, then sort by internalDate "
            "within that subset. 0 means fetch all label messages."
        ),
    )
    parser.add_argument(
        "--cancel-token-path",
        default=None,
        help="When this file appears, stop safely, rollback this run, and exit.",
    )
    parser.set_defaults(allow_non_damy_base=False)
    parser.add_argument(
        "--allow-non-damy-base",
        dest="allow_non_damy_base",
        action="store_true",
        help="Allow writing outside DAMY. Disabled by default for safety.",
    )
    parser.add_argument(
        "--allow-non-test-base",
        dest="allow_non_damy_base",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def _runtime_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _resolve_runtime_path(
    cli_path: str | None,
    env_name: str,
    default_name: str,
    *,
    must_exist: bool,
    preferred_path: str | os.PathLike[str] | None = None,
) -> Path:
    if cli_path:
        return Path(cli_path).expanduser().resolve()

    env_path = (os.environ.get(env_name) or "").strip().strip('"')
    if env_path:
        return Path(env_path).expanduser().resolve()

    app_dir = _runtime_app_dir()
    candidates: list[Path] = []
    if preferred_path:
        candidates.append(Path(preferred_path).expanduser())
    candidates.extend([
        MODULE_DIR / default_name,
        app_dir / default_name,
        app_dir / "folder_manager" / "order_import_v1" / default_name,
        Path.cwd() / default_name,
        Path.cwd() / "folder_manager" / "order_import_v1" / default_name,
    ])
    if must_exist:
        for path in candidates:
            if path.exists():
                return path.resolve()

    fallback = candidates[0]
    return fallback.resolve()


def _resolve_base_dir(base_dir_arg: str | None) -> str:
    if base_dir_arg:
        return base_dir_arg
    env_base = (
        (os.environ.get("DAMY_ORDER_BASE_DIR") or "").strip()
        or (os.environ.get("DAMY_BASE_DIR") or "").strip()
        or (os.environ.get("DAMY_ORDER_TEST_BASE_DIR") or "").strip()
        or (os.environ.get("DAMY_TEST_BASE_DIR") or "").strip()
    )
    return env_base or DEFAULT_BASE_PATH


def _looks_like_damy_base_dir(path_value: Path) -> bool:
    normalized = str(path_value).replace("/", "\\").lower()
    return bool(re.search(r"(^|\\)damy(?=\\|$)", normalized))


def _resolve_source_base_dir(base_dir: Path, source_base_dir_arg: str | None) -> Path | None:
    candidates: list[str] = []
    if source_base_dir_arg:
        candidates.append(source_base_dir_arg)

    env_source = (
        (os.environ.get("DAMY_ORDER_SOURCE_BASE_DIR") or "").strip()
        or (os.environ.get("DAMY_SOURCE_BASE_DIR") or "").strip()
    )
    if env_source:
        candidates.append(env_source)

    if not candidates:
        guessed = re.sub(
            r"(?i)(^|[\\/])damy_test(?=([\\/]|$))",
            r"\1DAMY",
            str(base_dir),
            count=1,
        )
        if guessed != str(base_dir):
            candidates.append(guessed)

    seen: set[str] = set()
    for raw in candidates:
        value = (raw or "").strip().strip('"')
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        path = Path(value).expanduser()
        if path.exists() and path.is_dir():
            return path.resolve()

    return None


def _build_db_client():
    if DB is None:
        return None
    host = os.environ.get("DAMY_DB_HOST", str(DB_HOST or "192.168.1.206"))
    name = os.environ.get("DAMY_DB_NAME", str(DB_NAME or "damy_workflow"))
    user = os.environ.get("DAMY_DB_USER", str(DB_USER or "damy_app"))
    password = os.environ.get("DAMY_DB_PASS", str(DB_PASS or "2357"))
    port = int(os.environ.get("DAMY_DB_PORT", str(DB_PORT or 5432)))
    try:
        return DB(host=host, dbname=name, user=user, password=password, port=port)
    except Exception as exc:
        print(f"[WARN] Could not initialize DB client: {exc}")
        return None


def _build_cancel_checker(cancel_token_path: str | None):
    token_file: Path | None = None
    value = (cancel_token_path or "").strip()
    if value:
        token_file = Path(value).expanduser().resolve()

    def _is_cancelled() -> bool:
        return bool(token_file and token_file.exists())

    return _is_cancelled, token_file


def _load_credentials(token_path: Path, credentials_path: Path):
    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception as exc:
            print(f"[WARN] Could not read token file; re-auth required: {exc}")
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            print(f"[WARN] Token refresh failed; re-auth required: {exc}")
            creds = None

    if not creds or not creds.valid:
        if not credentials_path.exists():
            raise FileNotFoundError(f"Credentials file not found: {credentials_path}")
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
        creds = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _build_gmail_service(token_path: Path, credentials_path: Path):
    creds = _load_credentials(token_path, credentials_path)
    return build("gmail", "v1", credentials=creds)


def _normalize_label_name(name: str) -> str:
    return MULTISPACE_RE.sub(" ", (name or "").strip().lower())


def _load_label_ids(service) -> dict[str, str]:
    resp = service.users().labels().list(userId="me").execute()
    labels = resp.get("labels", [])
    mapping: dict[str, str] = {}
    for item in labels:
        key = _normalize_label_name(str(item.get("name", "")))
        label_id = str(item.get("id", ""))
        if key and label_id:
            mapping[key] = label_id
    return mapping


def _move_message_to_imported_label(
    service,
    *,
    msg_id: str,
    test_label_id: str,
    imported_label_id: str,
    no_label_update: bool,
) -> bool:
    if no_label_update:
        print(f"[LABEL] id={msg_id} no_label_update=True (unchanged)")
        return False
    try:
        service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"addLabelIds": [imported_label_id], "removeLabelIds": [test_label_id]},
        ).execute()
        print(f"[LABEL] id={msg_id} removed_test_label={test_label_id} added_imported_label={imported_label_id}")
        return True
    except HttpError as exc:
        raise RuntimeError(f"Gmail label update failed: {exc}")


def _list_message_ids_for_label(service, label_id: str, *, label_window: int = 0) -> list[str]:
    results: list[str] = []
    page_token = None
    limit = max(0, int(label_window or 0))
    while True:
        page_size = 500
        if limit > 0:
            remaining = limit - len(results)
            if remaining <= 0:
                break
            page_size = max(1, min(500, remaining))
        kwargs: dict[str, Any] = {"userId": "me", "labelIds": [label_id], "maxResults": page_size}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        for msg in resp.get("messages", []):
            msg_id = str(msg.get("id", "")).strip()
            if msg_id:
                results.append(msg_id)
                if limit > 0 and len(results) >= limit:
                    break
        if limit > 0 and len(results) >= limit:
            break
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _load_message_sort_rows(service, message_ids: list[str]) -> list[MessageSortRow]:
    rows: list[MessageSortRow] = []
    for msg_id in message_ids:
        meta = service.users().messages().get(userId="me", id=msg_id, format="minimal").execute()
        rows.append(MessageSortRow(message_id=msg_id, internal_date_ms=_to_int(meta.get("internalDate"), 0)))
    rows.sort(key=lambda row: row.internal_date_ms)
    return rows


def _decode_body_data(data: str) -> str:
    raw = (data or "").strip()
    if not raw:
        return ""
    padding = "=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(raw + padding).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _collect_message_bodies(part: dict[str, Any], plain_parts: list[str], html_parts: list[str]) -> None:
    mime_type = str(part.get("mimeType", "")).lower()
    body = part.get("body", {}) or {}
    data = body.get("data")
    if data and mime_type in {"text/plain", "text/html"}:
        decoded = _decode_body_data(data)
        if decoded:
            if mime_type == "text/plain":
                plain_parts.append(decoded)
            elif mime_type == "text/html":
                html_parts.append(decoded)

    for sub in part.get("parts", []) or []:
        if isinstance(sub, dict):
            _collect_message_bodies(sub, plain_parts, html_parts)


def _extract_header(payload: dict[str, Any], header_name: str) -> str:
    target = header_name.strip().lower()
    for header in payload.get("headers", []) or []:
        name = str(header.get("name", "")).strip().lower()
        if name == target:
            return str(header.get("value", "")).strip()
    return ""


def _strip_html_for_text(text: str) -> str:
    if not text:
        return ""
    cleaned = text
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(r"(?i)</p\s*>", "\n", cleaned)
    cleaned = re.sub(r"(?i)<p\s*>", "", cleaned)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _extract_picture_day_pid(*values: str) -> tuple[str | None, bool, str | None]:
    # Prefer PID located after "Picture Day ID:".
    marker_re = re.compile(r"(?is)picture\s*day\s*id\s*[:#]?\s*([P]?\s*[0-9O\s]{7,12})")
    for value in values:
        if not value:
            continue
        text = _strip_html_for_text(value) if "<" in value and ">" in value else value
        m = marker_re.search(text)
        if not m:
            continue
        raw = (m.group(1) or "").strip().upper().replace(" ", "")
        if not raw:
            continue
        original_raw = raw
        if not raw.startswith("P"):
            raw = f"P{raw}"
        normalized = _normalize_pid(raw)
        if normalized:
            return normalized, False, original_raw
        corrected_raw = raw.replace("O", "0")
        if corrected_raw != raw:
            normalized_corrected = _normalize_pid(corrected_raw)
            if normalized_corrected:
                return normalized_corrected, True, original_raw
    return None, False, None


def _extract_any_pid(*values: str) -> str | None:
    for value in values:
        if not value:
            continue
        text = _strip_html_for_text(value) if "<" in value and ">" in value else value
        m = PID_RE.search(text.upper())
        if not m:
            continue
        normalized = _normalize_pid(m.group(0))
        if normalized:
            return normalized
    return None


def _normalize_pid(value: str) -> str | None:
    token = (value or "").strip().upper()
    m = re.fullmatch(r"P(\d{7,})", token)
    if not m:
        return None
    digits = m.group(1)
    if len(digits) < 8:
        digits = digits.zfill(8)
    # Some emails carry a 9-digit PID with an extra leading zero (e.g. P002234834).
    # Canonicalize by dropping only the first leading zero when that yields 8 digits.
    if len(digits) == 9 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 8:
        return None
    return f"P{digits}"


def _extract_school_name(text: str) -> str | None:
    if not text:
        return None
    patterns = [
        r"(?im)^\s*School\s*Name\s*:\s*(.+?)\s*$",
        r"(?im)^\s*Your\s*School\s*:\s*(.+?)\s*$",
        r"(?im)^\s*School\s*:\s*(.+?)\s*$",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            value = MULTISPACE_RE.sub(" ", m.group(1)).strip(" -:|")
            if value:
                return value
    return None


def _extract_order_date(text: str) -> date | None:
    if not text:
        return None
    m = re.search(r"(?i)\bDate\s*:\s*(\d{4})[-/](\d{2})[-/](\d{2})\b", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            return None
    return None


def _extract_order_no(*texts: str) -> str | None:
    patterns = [
        r"(?i)\bOrder\s*:\s*(R\d{6,})\b",
        r"(?i)\bNew\s+Order\s*#\s*(R\d{6,})\b",
        r"\b(R\d{6,})\b",
    ]
    for text in texts:
        raw = (text or "").strip()
        if not raw:
            continue
        normalized = _strip_html_for_text(raw) if "<" in raw and ">" in raw else raw
        for pattern in patterns:
            match = re.search(pattern, normalized)
            if match:
                return str(match.group(1) or "").strip().upper() or None
    return None


def _format_internal_date_ms(internal_date_ms: int) -> str:
    if internal_date_ms <= 0:
        return "N/A"
    try:
        return datetime.fromtimestamp(internal_date_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(internal_date_ms)


def _parse_message_order(message: dict[str, Any]) -> ParsedOrder:
    payload = message.get("payload", {}) or {}
    plain_parts: list[str] = []
    html_parts: list[str] = []
    _collect_message_bodies(payload, plain_parts, html_parts)

    plain_body = "\n\n".join([p for p in plain_parts if p.strip()]).strip()
    html_body = "\n".join([p for p in html_parts if p.strip()]).strip()
    fallback_text = _strip_html_for_text(html_body)
    text_for_parse = plain_body or fallback_text

    subject = _extract_header(payload, "Subject")
    header_date_raw = _extract_header(payload, "Date")
    internal_date_ms = _to_int(message.get("internalDate"), 0)

    pid, pid_auto_corrected, pid_raw = _extract_picture_day_pid(text_for_parse, fallback_text, html_body)
    if not pid:
        # Fallback: when Picture Day ID field has no PID (e.g. date text), use general PID in email.
        pid = _extract_any_pid(subject, text_for_parse, fallback_text, html_body)
        pid_auto_corrected = False
        pid_raw = None

    order_no = _extract_order_no(text_for_parse, fallback_text, html_body, subject)
    if not order_no:
        raise RuntimeError(f"Order No. not found in email. subject='{subject}'")

    school_name = _extract_school_name(text_for_parse) or _extract_school_name(fallback_text)
    if not school_name:
        raise RuntimeError(f"School Name not found in email. subject='{subject}'")

    order_date = _extract_order_date(text_for_parse) or _extract_order_date(subject)
    if not order_date and header_date_raw:
        try:
            order_date = parsedate_to_datetime(header_date_raw).date()
        except Exception:
            order_date = None
    if not order_date and internal_date_ms > 0:
        order_date = datetime.fromtimestamp(internal_date_ms / 1000).date()
    if not order_date:
        order_date = datetime.now().date()

    return ParsedOrder(
        message_id=str(message.get("id", "")).strip(),
        pid=pid,
        order_no=order_no,
        pid_auto_corrected=bool(pid_auto_corrected),
        pid_raw=pid_raw,
        school_name=school_name,
        order_date=order_date,
        subject=subject,
        html_body=html_body,
        plain_body=plain_body,
    )


def _normalize_school_name(name: str) -> str:
    lowered = (name or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "", lowered)


def _school_tokens(name: str) -> set[str]:
    s = (name or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"\bchild\s+care\b", "childcare", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    raw_tokens = [tok.strip() for tok in s.split() if tok.strip()]
    stop = {
        "the",
        "school",
        "prep",
        "ps",
        "is",
    }
    return {tok for tok in raw_tokens if tok not in stop}


def _school_similarity(email_school: str, folder_school: str) -> tuple[int, int, float, list[str]]:
    email_tokens = _school_tokens(email_school)
    folder_tokens = _school_tokens(folder_school)
    if not email_tokens or not folder_tokens:
        return (0, 0, 0.0, [])

    common = sorted(email_tokens & folder_tokens)
    common_count = len(common)
    ratio = common_count / max(1, min(len(email_tokens), len(folder_tokens)))
    score = common_count * 100 + int(ratio * 100)
    return (score, common_count, ratio, common)


def _prompt_user_choose_folder_with_preview(
    *,
    window_title: str,
    header_text: str,
    subheader_text: str,
    preview_pdf_path: Path | None,
    choose_button_text: str,
    is_cancelled=None,
    options: list[tuple[FolderCandidate, int, int, float, list[str]]],
) -> FolderCandidate | None:
    try:
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtWidgets import (
            QApplication,
            QDialog,
            QFrame,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QListWidget,
            QListWidgetItem,
            QPushButton,
            QVBoxLayout,
        )
        from PySide6.QtPdf import QPdfDocument
        from PySide6.QtPdfWidgets import QPdfView
    except Exception as exc:
        raise RuntimeError(f"Manual folder-selection popup UI is unavailable: {exc}")

    class _WheelZoomPdfView(QPdfView):
        def wheelEvent(self, event):  # type: ignore[override]
            delta = event.angleDelta().y()
            if delta:
                current = float(self.zoomFactor() or 1.0)
                factor = 1.15 if delta > 0 else (1 / 1.15)
                next_zoom = max(0.35, min(4.0, current * factor))
                self.setZoomMode(QPdfView.ZoomMode.Custom)
                self.setZoomFactor(next_zoom)
                event.accept()
                return
            super().wheelEvent(event)

    app = QApplication.instance()
    app_created = False
    if app is None:
        app = QApplication(sys.argv[:1])
        app_created = True

    holder: dict[str, int | None | bool] = {"idx": None, "cancelled": False}
    dlg = QDialog()
    dlg.setWindowTitle(window_title)
    dlg.setModal(True)
    dlg.setMinimumSize(1180, 760)
    dlg.resize(1280, 820)
    dlg.setWindowFlag(Qt.WindowStaysOnTopHint, True)

    root_layout = QVBoxLayout(dlg)
    root_layout.setContentsMargins(16, 16, 16, 16)
    root_layout.setSpacing(12)

    header = QLabel(header_text)
    header.setWordWrap(True)
    root_layout.addWidget(header)

    subheader = QLabel(subheader_text)
    subheader.setWordWrap(True)
    root_layout.addWidget(subheader)

    content = QHBoxLayout()
    content.setSpacing(16)
    root_layout.addLayout(content, 1)

    pdf_frame = QFrame()
    pdf_frame.setFrameShape(QFrame.StyledPanel)
    pdf_layout = QVBoxLayout(pdf_frame)
    pdf_layout.setContentsMargins(10, 10, 10, 10)
    pdf_layout.setSpacing(8)
    pdf_title = QLabel("PDF")
    pdf_layout.addWidget(pdf_title)

    if preview_pdf_path and preview_pdf_path.exists():
        pdf_doc = QPdfDocument(dlg)
        load_status = pdf_doc.load(str(preview_pdf_path))
        if load_status == QPdfDocument.Error.None_:
            pdf_view = _WheelZoomPdfView()
            pdf_view.setDocument(pdf_doc)
            pdf_view.setPageMode(QPdfView.PageMode.SinglePage)
            pdf_view.setZoomMode(QPdfView.ZoomMode.FitInView)
            pdf_layout.addWidget(pdf_view, 1)
        else:
            pdf_layout.addWidget(QLabel("PDF preview could not be loaded."), 1)
    else:
        pdf_layout.addWidget(QLabel("PDF preview is unavailable for this email."), 1)

    list_frame = QFrame()
    list_frame.setFrameShape(QFrame.StyledPanel)
    list_layout = QVBoxLayout(list_frame)
    list_layout.setContentsMargins(10, 10, 10, 10)
    list_layout.setSpacing(8)

    filter_entry = QLineEdit()
    filter_entry.setPlaceholderText("Search")
    list_layout.addWidget(filter_entry)

    listbox = QListWidget()
    list_layout.addWidget(listbox, 1)

    content.addWidget(pdf_frame, 5)
    content.addWidget(list_frame, 6)

    buttons = QHBoxLayout()
    root_layout.addLayout(buttons)
    btn_choose = QPushButton(choose_button_text)
    btn_skip = QPushButton("Skip This Email")
    buttons.addWidget(btn_choose)
    buttons.addStretch(1)
    buttons.addWidget(btn_skip)

    visible_rows: list[int] = []

    def _render_row(cand: FolderCandidate, score: int, common_count: int, ratio: float, common_tokens: list[str]) -> str:
        _ = (score, common_count, ratio, common_tokens)  # kept in signature for compatibility
        return f"{cand.name}"

    def _refresh_list() -> None:
        needle = (filter_entry.text() or "").strip().lower()
        listbox.clear()
        visible_rows.clear()
        for idx, row in enumerate(options):
            label = _render_row(*row)
            if needle and needle not in label.lower():
                continue
            visible_rows.append(idx)
            listbox.addItem(QListWidgetItem(label))
        if visible_rows:
            listbox.setCurrentRow(0)

    def _choose() -> None:
        current_row = listbox.currentRow()
        holder["idx"] = visible_rows[current_row] if 0 <= current_row < len(visible_rows) else None
        dlg.accept()

    def _skip() -> None:
        holder["idx"] = None
        dlg.reject()

    def _poll_cancel() -> None:
        try:
            if callable(is_cancelled) and bool(is_cancelled()):
                holder["cancelled"] = True
                dlg.reject()
                return
        except Exception:
            pass

    cancel_timer = QTimer(dlg)
    cancel_timer.timeout.connect(_poll_cancel)
    cancel_timer.start(250)

    filter_entry.textChanged.connect(lambda _text: _refresh_list())
    listbox.itemDoubleClicked.connect(lambda _item: _choose())
    btn_choose.clicked.connect(_choose)
    btn_skip.clicked.connect(_skip)

    _refresh_list()
    filter_entry.setFocus()
    dlg.exec()
    cancel_timer.stop()

    if app_created:
        app.processEvents()

    if bool(holder.get("cancelled")):
        raise OrderImportCancelled("Order import cancelled by user request.")

    idx = holder.get("idx")
    if idx is None or idx < 0 or idx >= len(options):
        return None
    return options[idx][0]


def _create_manual_review_preview_pdf(order: ParsedOrder) -> Path | None:
    try:
        with tempfile.NamedTemporaryFile(prefix="damy_order_preview_", suffix=".pdf", delete=False) as tmp_pdf:
            preview_path = Path(tmp_pdf.name)
        _render_order_to_pdf(order, preview_path)
        return preview_path
    except Exception as exc:
        print(f"[WARN] Could not build manual-review PDF preview: {exc}")
        try:
            if 'preview_path' in locals() and preview_path.exists():
                preview_path.unlink()
        except Exception:
            pass
        return None


def _prompt_user_choose_pid_tie(
    *,
    pid: str,
    school_name: str,
    order_date: date,
    subject: str,
    preview_pdf_path: Path | None,
    is_cancelled=None,
    options: list[tuple[FolderCandidate, int, int, float, list[str]]],
) -> FolderCandidate | None:
    return _prompt_user_choose_folder_with_preview(
        window_title="Order Import: Choose Folder",
        header_text=(
            "Multiple folders matched this Picture Day ID. Review the PDF, search the folder list, "
            "and choose which existing folder should receive this order."
        ),
        subheader_text=(
            f"PID: {pid}    School: {school_name or '(unknown)'}    Date: {order_date:%Y-%m-%d}    "
            f"Subject: {subject or '(no subject)'}"
        ),
        preview_pdf_path=preview_pdf_path,
        choose_button_text="Use Selected Folder",
        is_cancelled=is_cancelled,
        options=options,
    )


def _prompt_user_choose_folder_without_pid(
    *,
    school_name: str,
    order_date: date,
    subject: str,
    preview_pdf_path: Path | None,
    is_cancelled=None,
    options: list[tuple[FolderCandidate, int, int, float, list[str]]],
) -> FolderCandidate | None:
    return _prompt_user_choose_folder_with_preview(
        window_title="Order Import: Choose Folder",
        header_text="Review the PDF, search the folder list, and choose which existing folder should receive this order.",
        subheader_text=(
            f"Status: Picture Day ID was not found in the email.    "
            f"School: {school_name or '(unknown)'}    Date: {order_date:%Y-%m-%d}    "
            f"Subject: {subject or '(no subject)'}"
        ),
        preview_pdf_path=preview_pdf_path,
        choose_button_text="Use Selected Folder",
        is_cancelled=is_cancelled,
        options=options,
    )


def _prompt_user_choose_folder_for_pid_mismatch(
    *,
    pid: str,
    school_name: str,
    order_date: date,
    subject: str,
    preview_pdf_path: Path | None,
    pid_auto_corrected: bool = False,
    pid_raw: str | None = None,
    is_cancelled=None,
    options: list[tuple[FolderCandidate, int, int, float, list[str]]],
) -> FolderCandidate | None:
    pid_line = f"Email PID: {pid}"
    if pid_auto_corrected and pid_raw:
        pid_line = f"Email PID Text: {pid_raw}    Corrected PID: {pid}"
    return _prompt_user_choose_folder_with_preview(
        window_title="Order Import: Choose Folder",
        header_text="Review the PDF, search the folder list, and choose which existing folder should receive this order.",
        subheader_text=(
            f"Status: Picture Day ID did not match an existing folder.    "
            f"{pid_line}    School: {school_name or '(unknown)'}    Date: {order_date:%Y-%m-%d}    "
            f"Subject: {subject or '(no subject)'}"
        ),
        preview_pdf_path=preview_pdf_path,
        choose_button_text="Use Selected Folder",
        is_cancelled=is_cancelled,
        options=options,
    )


def _strip_stage_prefix(folder_name: str) -> str:
    value = (folder_name or "").strip()
    for stage_def in STAGES:
        prefixes = tuple(getattr(stage_def, "prefixes", ()) or ())
        for prefix in prefixes:
            if value.startswith(prefix):
                return value[len(prefix):].strip()
            compact_prefix = re.sub(r"\.\s+", ".", prefix)
            if compact_prefix and value.startswith(compact_prefix):
                return value[len(compact_prefix):].strip()
    return value


def _extract_school_from_folder_name(folder_name: str) -> str:
    tail = _strip_stage_prefix(folder_name)
    tail = re.sub(r"^\d{6}\s*", "", tail).strip()
    tail = re.sub(r"\s+P\d{7,}(?:\+P\d{7,})*$", "", tail).strip()
    return MULTISPACE_RE.sub(" ", tail)


def _list_folder_candidates(base_dir: Path) -> list[FolderCandidate]:
    ignored_exact = {
        "cancel",
        "_workflow_log",
        "1. order form",
        "order form",
        "__pycache__",
    }
    candidates: list[FolderCandidate] = []
    for entry in os.scandir(base_dir):
        if not entry.is_dir():
            continue
        name = (entry.name or "").strip()
        lowered = name.lower()
        if not name or lowered in ignored_exact or name.startswith("."):
            continue
        pids = tuple(
            pid
            for pid in (_normalize_pid(token) for token in PID_RE.findall(name))
            if pid is not None
        )
        school_text = _extract_school_from_folder_name(name)
        school_norm = _normalize_school_name(school_text)
        candidates.append(
            FolderCandidate(
                name=name,
                path=Path(entry.path),
                pids=pids,
                school_text=school_text,
                school_norm=school_norm,
                stage=None,
                source="disk",
            )
        )
    return candidates


def _resolve_candidate_path_from_db_name(base_dir: Path, db_name: str) -> Path:
    raw = (db_name or "").strip()
    display = _strip_stage_prefix(raw).strip() or raw
    variants: list[str] = []
    for value in (raw, display):
        if value and value not in variants:
            variants.append(value)
    for value in variants:
        path = base_dir / value
        if path.exists():
            return path
    return base_dir / display


def _candidate_name_variants(name: str) -> list[str]:
    raw = (name or "").strip()
    display = _strip_stage_prefix(raw).strip() or raw
    variants: list[str] = []
    for value in (raw, display):
        if value and value not in variants:
            variants.append(value)
    return variants


def _find_existing_folder_path(root_dir: Path, folder_name_variants: list[str]) -> Path | None:
    for folder_name in folder_name_variants:
        direct = root_dir / folder_name
        if direct.is_dir():
            return direct
    for folder_name in folder_name_variants:
        in_cancel = root_dir / "cancel" / folder_name
        if in_cancel.is_dir():
            return in_cancel
    return None


def _ensure_db_candidate_available(
    candidate: FolderCandidate,
    *,
    base_dir: Path,
    source_base_dir: Path | None,
    pid: str,
) -> tuple[Path, bool]:
    if candidate.path.is_dir():
        return candidate.path, False

    variants = _candidate_name_variants(candidate.name)
    source_path = _find_existing_folder_path(source_base_dir, variants) if source_base_dir else None
    if source_path and source_path.is_dir():
        base_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_path, candidate.path)
        (candidate.path / pid).mkdir(parents=False, exist_ok=True)
        print(
            f"[COPY] copied_db_match_from_source "
            f"source={str(source_path)!r} target={str(candidate.path)!r}"
        )
        return candidate.path, True

    source_hint = str(source_base_dir) if source_base_dir else "None"
    raise RuntimeError(
        f"DB matched folder is missing in active base and source. "
        f"folder={candidate.name!r} target={str(candidate.path)!r} source_base={source_hint!r}"
    )


def _list_folder_candidates_from_db(db, base_dir: Path) -> list[FolderCandidate]:
    rows: list[tuple[int, str, int | None]] = []
    try:
        for item in db.list_by_stage(UPCOMING_STAGE, domain=PREPAID_WORKFLOW_DOMAIN):
            name = str(item.disk_name or "").strip()
            if not name:
                continue
            stage_value = int(getattr(item, "stage", UPCOMING_STAGE))
            rows.append((int(item.id), name, stage_value))
    except Exception as exc:
        print(f"[WARN] Could not read DB candidates; fallback to disk scan: {exc}")
        return _list_folder_candidates(base_dir)

    candidates: list[FolderCandidate] = []
    seen: set[str] = set()
    for item_id, disk_name, stage in rows:
        if disk_name in seen:
            continue
        seen.add(disk_name)
        school_text = _extract_school_from_folder_name(disk_name)
        school_norm = _normalize_school_name(school_text)
        pids = tuple(
            pid
            for pid in (_normalize_pid(token) for token in PID_RE.findall(disk_name))
            if pid is not None
        )
        path = _resolve_candidate_path_from_db_name(base_dir, disk_name)
        candidates.append(
            FolderCandidate(
                name=disk_name,
                path=path,
                pids=pids,
                school_text=school_text,
                school_norm=school_norm,
                item_id=item_id,
                stage=stage,
                source="db",
            )
        )
    return candidates


def _resolve_target_folder(
    base_dir: Path,
    pid: str,
    school_name: str,
    order_date: date,
    *,
    subject: str,
    preview_pdf_path: Path | None,
    pid_auto_corrected: bool = False,
    pid_raw: str | None = None,
    db=None,
    source_base_dir: Path | None = None,
    is_cancelled=None,
) -> tuple[Path, bool, bool, int | None, str]:
    if callable(is_cancelled) and bool(is_cancelled()):
        raise OrderImportCancelled("Order import cancelled by user request.")
    school_norm = _normalize_school_name(school_name)
    if not school_norm:
        raise RuntimeError("School Name is empty after normalization.")

    if db is not None:
        candidates = _list_folder_candidates_from_db(db, base_dir)
        print(f"[MATCH] candidate_source=db total={len(candidates)}")
    else:
        candidates = _list_folder_candidates(base_dir)
        print(f"[MATCH] candidate_source=disk total={len(candidates)}")
    pid_matches = [cand for cand in candidates if pid in cand.pids]

    if pid_matches:
        scored: list[tuple[FolderCandidate, int, int, float, list[str]]] = []
        for cand in pid_matches:
            score, common_count, ratio, common_tokens = _school_similarity(school_name, cand.school_text)
            scored.append((cand, score, common_count, ratio, common_tokens))
            print(
                f"[MATCH-SCORE] pid={pid} email_school={school_name!r} "
                f"candidate={cand.name!r} score={score} common={common_count} ratio={ratio:.2f} "
                f"tokens={common_tokens} stage={cand.stage} source={cand.source}"
            )

        if len(scored) == 1:
            chosen = scored[0][0]
            print(f"[MATCH] unique_pid_match_selected={chosen.name!r}")
            chosen_path, created_on_disk = _ensure_db_candidate_available(
                chosen,
                base_dir=base_dir,
                source_base_dir=source_base_dir,
                pid=pid,
            )
            # DB match, do not insert a new DB row.
            return chosen_path, created_on_disk, False, chosen.item_id, pid

        print(
            f"[MATCH] multi_pid_match_requires_user_choice pid={pid} school={school_name!r} "
            f"candidates={[row[0].name for row in scored]}"
        )
        sorted_rows = sorted(
            scored,
            key=lambda row: (-row[1], -row[2], -row[3], row[0].name.lower()),
        )
        chosen = _prompt_user_choose_pid_tie(
            pid=pid,
            school_name=school_name,
            order_date=order_date,
            subject=subject,
            preview_pdf_path=preview_pdf_path,
            is_cancelled=is_cancelled,
            options=sorted_rows,
        )
        if chosen is None:
            raise RuntimeError(
                f"Multiple PID matches require manual selection; user skipped. pid={pid} school='{school_name}'"
            )
        print(f"[MATCH] user_selected_folder={chosen.name!r}")
        chosen_path, created_on_disk = _ensure_db_candidate_available(
            chosen,
            base_dir=base_dir,
            source_base_dir=source_base_dir,
            pid=pid,
        )
        return chosen_path, created_on_disk, False, chosen.item_id, pid

    print(
        f"[MATCH] no_existing_pid_match_requires_user_choice parsed_pid={pid} "
        f"raw_pid={pid_raw!r} school={school_name!r} "
        f"reason={'corrected_pid_unmatched' if pid_auto_corrected else 'pid_unmatched'}"
    )
    scored = []
    for cand in candidates:
        score, common_count, ratio, common_tokens = _school_similarity(school_name, cand.school_text)
        scored.append((cand, score, common_count, ratio, common_tokens))
        print(
            f"[MATCH-SCORE] no_pid_match email_pid={pid} email_school={school_name!r} "
            f"candidate={cand.name!r} score={score} common={common_count} ratio={ratio:.2f} "
            f"tokens={common_tokens} stage={cand.stage} source={cand.source}"
        )

    sorted_rows = sorted(
        scored,
        key=lambda row: (-row[1], -row[2], -row[3], row[0].name.lower()),
    )
    chosen = _prompt_user_choose_folder_for_pid_mismatch(
        pid=pid,
        school_name=school_name,
        order_date=order_date,
        subject=subject,
        preview_pdf_path=preview_pdf_path,
        pid_auto_corrected=pid_auto_corrected,
        pid_raw=pid_raw,
        is_cancelled=is_cancelled,
        options=sorted_rows,
    )
    if chosen is None:
        raise RuntimeError(
            f"No existing DAMY folder matched PID and manual folder selection was skipped. "
            f"pid={pid} school='{school_name}'"
        )

    chosen_pid = chosen.pids[0] if chosen.pids else None
    if not chosen_pid:
        raise RuntimeError(
            f"Selected folder has no PID in its name. folder='{chosen.name}' "
            f"email_pid='{pid}' school='{school_name}'"
        )

    print(
        f"[MATCH] manual_pid_mismatch_selected folder={chosen.name!r} "
        f"email_pid={pid} resolved_pid={chosen_pid} school={school_name!r}"
    )
    chosen_path, created_on_disk = _ensure_db_candidate_available(
        chosen,
        base_dir=base_dir,
        source_base_dir=source_base_dir,
        pid=chosen_pid,
    )
    return chosen_path, created_on_disk, False, chosen.item_id, chosen_pid


def _resolve_target_folder_without_pid(
    base_dir: Path,
    school_name: str,
    order_date: date,
    *,
    subject: str,
    preview_pdf_path: Path | None,
    db=None,
    source_base_dir: Path | None = None,
    is_cancelled=None,
) -> tuple[Path, bool, bool, int | None, str]:
    if callable(is_cancelled) and bool(is_cancelled()):
        raise OrderImportCancelled("Order import cancelled by user request.")

    if db is not None:
        candidates = _list_folder_candidates_from_db(db, base_dir)
        print(f"[MATCH] manual_no_pid candidate_source=db total={len(candidates)}")
    else:
        candidates = _list_folder_candidates(base_dir)
        print(f"[MATCH] manual_no_pid candidate_source=disk total={len(candidates)}")

    scored: list[tuple[FolderCandidate, int, int, float, list[str]]] = []
    for cand in candidates:
        score, common_count, ratio, common_tokens = _school_similarity(school_name, cand.school_text)
        scored.append((cand, score, common_count, ratio, common_tokens))

    sorted_rows = sorted(
        scored,
        key=lambda row: (-row[1], -row[2], -row[3], row[0].name.lower()),
    )
    chosen = _prompt_user_choose_folder_without_pid(
        school_name=school_name,
        order_date=order_date,
        subject=subject,
        preview_pdf_path=preview_pdf_path,
        is_cancelled=is_cancelled,
        options=sorted_rows,
    )
    if chosen is None:
        raise RuntimeError(
            f"PID not found in email and manual folder selection was skipped. school='{school_name}'"
        )

    chosen_pid = chosen.pids[0] if chosen.pids else None
    if not chosen_pid:
        raise RuntimeError(
            f"Selected folder has no PID in its name. folder='{chosen.name}' school='{school_name}'"
        )

    print(
        f"[MATCH] manual_no_pid_selected folder={chosen.name!r} "
        f"resolved_pid={chosen_pid} school={school_name!r}"
    )
    chosen_path, created_on_disk = _ensure_db_candidate_available(
        chosen,
        base_dir=base_dir,
        source_base_dir=source_base_dir,
        pid=chosen_pid,
    )
    return chosen_path, created_on_disk, False, chosen.item_id, chosen_pid


def _parse_existing_order_pdf(entry: Path, normalized_pid: str) -> tuple[Path, date, int] | None:
    if not entry.is_file():
        return None
    m = ORDER_PDF_RE.match(entry.name)
    if not m:
        return None
    file_pid = _normalize_pid(m.group("pid")) or m.group("pid").upper()
    if file_pid != normalized_pid:
        return None
    yymmdd = m.group("date")
    try:
        file_date = datetime.strptime(yymmdd, "%y%m%d").date()
    except Exception:
        return None
    count = _to_int(m.group("count"), 0)
    return entry, file_date, count


def _find_existing_order_pdf(
    folder_path: Path,
    pid: str,
    *,
    preferred_pdf_path: str | Path | None = None,
) -> tuple[Path, date, int] | None:
    matches: list[tuple[Path, date, int]] = []
    normalized_pid = _normalize_pid(pid) or pid.upper()

    preferred_raw = str(preferred_pdf_path or "").strip().strip('"')
    if preferred_raw:
        preferred_entry = Path(preferred_raw).expanduser()
        preferred_match = _parse_existing_order_pdf(preferred_entry, normalized_pid)
        if preferred_match:
            return preferred_match
        if preferred_entry.exists():
            print(
                f"[PDF] preferred DB pdf_path ignored; not a matching order PDF for pid={normalized_pid}: "
                f"{preferred_entry}"
            )
        else:
            print(f"[PDF] preferred DB pdf_path missing; falling back to folder scan: {preferred_entry}")

    for entry in folder_path.iterdir():
        parsed = _parse_existing_order_pdf(entry, normalized_pid)
        if parsed:
            matches.append(parsed)
    if not matches:
        return None
    matches.sort(key=lambda row: (row[2], row[1], row[0].name.lower()))
    return matches[-1]


def _make_order_pdf_name(order_date: date, pid: str, count: int) -> str:
    return f"{order_date:%y%m%d} {pid} Ordered {count}.pdf"


def _strict_order_pdf_name(
    folder_path: Path,
    order_date: date,
    pid: str,
    count: int,
) -> str:
    """
    Returns the exact filename for the target cumulative count.
    If that filename already exists, raise instead of silently bumping count.
    """
    safe_count = max(1, int(count))
    name = _make_order_pdf_name(order_date, pid, safe_count)
    target = folder_path / name
    if target.exists():
        raise RuntimeError(
            "Target order PDF filename already exists; refusing to bump count automatically: "
            f"{target}"
        )
    return name


def _merge_pdfs(existing_pdf: Path, new_pdf: Path, output_pdf: Path) -> None:
    writer = PdfWriter()
    # Keep latest imported order at the front: new pages first, then existing pages.
    for source in (new_pdf, existing_pdf):
        reader = PdfReader(str(source))
        for page in reader.pages:
            if _is_browser_header_page(page):
                continue
            writer.add_page(page)
    if len(writer.pages) == 0:
        raise RuntimeError("Merged PDF ended up empty after header-page filtering.")
    with output_pdf.open("wb") as fh:
        writer.write(fh)


def _write_clean_order_pdf(source_pdf: Path, target_pdf: Path) -> Path:
    reader = PdfReader(str(source_pdf))
    writer = PdfWriter()
    for page in reader.pages:
        if _is_browser_header_page(page):
            continue
        writer.add_page(page)
    if len(writer.pages) == 0:
        shutil.copyfile(source_pdf, target_pdf)
    else:
        with target_pdf.open("wb") as fh:
            writer.write(fh)
    return target_pdf


def _looks_like_pdf_read_error(exc: Exception) -> bool:
    text = str(exc or "").strip().lower()
    if not text:
        return False
    markers = (
        "stream has ended unexpectedly",
        "eof marker not found",
        "startxref not found",
        "cannot read malformed pdf",
        "invalid pdf",
        "pdf read error",
        "pdfreaderror",
    )
    return any(marker in text for marker in markers)


def _pick_pdf_browser() -> str | None:
    env_browser = (os.environ.get("DAMY_PDF_BROWSER") or "").strip().strip('"')
    if env_browser and Path(env_browser).exists():
        return env_browser

    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if Path(path).exists():
            return path

    for cmd in ("google-chrome", "chrome", "chromium", "msedge"):
        resolved = shutil.which(cmd)
        if resolved:
            return resolved
    return None


def _inject_print_style(html_doc: str) -> str:
    doc = (html_doc or "").strip()
    if not doc:
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            + PRINT_STYLE_BLOCK
            + "</head><body></body></html>"
        )
    if re.search(r"(?is)<head\b", doc):
        return re.sub(r"(?is)</head\s*>", PRINT_STYLE_BLOCK + "\n</head>", doc, count=1)
    if re.search(r"(?is)<html\b", doc):
        return re.sub(
            r"(?is)<html\b([^>]*)>",
            r"<html\1><head><meta charset='utf-8'>"
            + PRINT_STYLE_BLOCK
            + "</head>",
            doc,
            count=1,
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        + PRINT_STYLE_BLOCK
        + "</head><body>"
        + doc
        + "</body></html>"
    )


def _build_print_html(order: ParsedOrder) -> str:
    if order.html_body.strip():
        body = order.html_body
        if re.search(r"(?is)<html\b", body):
            return _inject_print_style(body)
        return _inject_print_style(
            "<!doctype html><html><head><meta charset='utf-8'></head><body>"
            + body
            + "</body></html>"
        )
    escaped = html.escape(order.plain_body.strip() or order.subject.strip() or "(empty email)")
    return _inject_print_style(
        "<!doctype html><html><head><meta charset='utf-8'></head>"
        f"<body><pre style='white-space: pre-wrap'>{escaped}</pre></body></html>"
    )


def _run_headless_print(browser: str, html_uri: str, output_pdf: Path, profile_dir: Path) -> tuple[bool, str]:
    base_cmd = [
        browser,
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--allow-file-access-from-files",
        f"--user-data-dir={profile_dir}",
        f"--print-to-pdf={output_pdf}",
        "--no-pdf-header-footer",
    ]

    variants = [
        ["--headless=new"],
        ["--headless"],
    ]
    for variant in variants:
        cmd = base_cmd + variant + [html_uri]
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if cp.returncode == 0 and output_pdf.exists() and output_pdf.stat().st_size > 0:
            return True, ""
        err = (cp.stderr or cp.stdout or "").strip()
    return False, err


def _is_browser_header_page(page) -> bool:
    try:
        text = (page.extract_text() or "").strip().lower()
    except Exception:
        return False
    if not text:
        return False
    return ("file:///" in text) and ("new order #" in text)


def _page_text_content(page) -> str:
    try:
        return (page.extract_text() or "").strip()
    except Exception:
        return ""


def _is_blank_text_page(page) -> bool:
    return not bool(_page_text_content(page))


def _strip_blank_edge_pages(pdf_path: Path) -> None:
    try:
        reader = PdfReader(str(pdf_path))
    except Exception:
        return
    total = len(reader.pages)
    if total < 2:
        return

    start = 0
    end = total
    while start < end and _is_blank_text_page(reader.pages[start]):
        start += 1
    while end > start and _is_blank_text_page(reader.pages[end - 1]):
        end -= 1

    if start == 0 and end == total:
        return

    writer = PdfWriter()
    for page in reader.pages[start:end]:
        writer.add_page(page)
    if len(writer.pages) == 0:
        return
    with pdf_path.open("wb") as fh:
        writer.write(fh)


def _render_order_to_pdf(order: ParsedOrder, output_pdf: Path) -> None:
    browser = _pick_pdf_browser()
    if not browser:
        raise RuntimeError("No Chrome/Edge browser found for PDF printing.")

    with tempfile.TemporaryDirectory(prefix="damy_order_html_") as tmpdir:
        tmp_root = Path(tmpdir)
        html_path = tmp_root / "order_email.html"
        profile_dir = tmp_root / "profile"
        html_path.write_text(_build_print_html(order), encoding="utf-8")
        ok, err = _run_headless_print(browser, html_path.resolve().as_uri(), output_pdf, profile_dir)
        if not ok:
            raise RuntimeError(f"Headless browser print failed. {err}")
        _strip_blank_edge_pages(output_pdf)


def _append_or_create_order_pdf(
    folder_path: Path,
    pid: str,
    order_date: date,
    new_email_pdf: Path,
    *,
    preferred_existing_pdf_path: str | Path | None = None,
    new_order_package_count: int = 1,
) -> Path:
    add_count = max(1, int(new_order_package_count or 1))
    existing = _find_existing_order_pdf(folder_path, pid, preferred_pdf_path=preferred_existing_pdf_path)
    if not existing:
        name = _strict_order_pdf_name(folder_path, order_date, pid, add_count)
        target = folder_path / name
        return _write_clean_order_pdf(new_email_pdf, target)

    existing_pdf, existing_date, existing_count = existing
    final_date = max(order_date, existing_date)
    next_count = existing_count + add_count
    output_name = _strict_order_pdf_name(folder_path, final_date, pid, next_count)
    target = folder_path / output_name

    with tempfile.NamedTemporaryFile(
        prefix="damy_order_merge_",
        suffix=".pdf",
        dir=str(folder_path),
        delete=False,
    ) as tmp:
        tmp_output = Path(tmp.name)

    try:
        try:
            _merge_pdfs(existing_pdf, new_email_pdf, tmp_output)
        except Exception as exc:
            if existing_count <= 0 and _looks_like_pdf_read_error(exc):
                print(
                    f"[PDF] existing file is unreadable and count is 0; "
                    f"overwriting {existing_pdf.name!r} with a fresh PDF"
                )
                return _write_clean_order_pdf(new_email_pdf, existing_pdf)
            raise
        os.replace(str(tmp_output), str(target))
        if existing_pdf.resolve() != target.resolve():
            try:
                existing_pdf.unlink()
            except FileNotFoundError:
                pass
    finally:
        if tmp_output.exists():
            try:
                tmp_output.unlink()
            except FileNotFoundError:
                pass
    return target


def _count_order_packages_from_pdf(pdf_path: Path) -> int:
    """
    Counts package entries using the same PDF parsing path as QR Orders.
    Falls back to 1 if parsing fails or finds no package rows.
    """
    try:
        qr_module = importlib.import_module("folder_manager.qr_tags_v1.main")
        parse_orders_pdf = getattr(qr_module, "parse_orders_pdf", None)
        if not callable(parse_orders_pdf):
            return 1
        parsed = parse_orders_pdf(str(pdf_path)) or {}
        total = sum(len(items) for items in parsed.values())
        return max(1, int(total))
    except Exception as exc:
        print(f"[WARN] package-count parse failed for {pdf_path.name!r}; fallback=1 ({exc})")
        return 1


def _sync_new_folder_to_db(db, folder_name: str) -> int | None:
    if db is None:
        return None
    try:
        return int(
            db.upsert_into_domain(
                disk_name=folder_name,
                domain=PREPAID_WORKFLOW_DOMAIN,
                stage=UPCOMING_STAGE,
            )
        )
    except Exception as exc:
        print(f"[WARN] DB upsert failed for folder '{folder_name}': {exc}")
        return None


def _process_one_message(
    service,
    msg_id: str,
    base_dir: Path,
    source_base_dir: Path | None,
    test_label_id: str,
    imported_label_id: str,
    *,
    no_label_update: bool,
    is_cancelled=None,
    db=None,
) -> tuple[bool, str, str, str, int | None, AppliedAction]:
    if callable(is_cancelled) and bool(is_cancelled()):
        raise OrderImportCancelled("Order import cancelled by user request.")
    message_t0 = time.perf_counter()
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    payload = msg.get("payload", {}) or {}
    subject = _extract_header(payload, "Subject")
    from_header = _extract_header(payload, "From")
    header_date = _extract_header(payload, "Date")
    internal_date_ms = _to_int(msg.get("internalDate"), 0)
    print(
        f"[MAIL] id={msg_id} subject={subject!r} from={from_header!r} "
        f"header_date={header_date!r} internal_date={_format_internal_date_ms(internal_date_ms)}"
    )

    parsed: ParsedOrder | None = None
    try:
        parse_t0 = time.perf_counter()
        parsed = _parse_message_order(msg)
        parse_elapsed = time.perf_counter() - parse_t0
        if parsed.pid_auto_corrected:
            print(
                f"[PID] picture_day_id_corrected_for_matching raw={parsed.pid_raw!r} "
                f"corrected={parsed.pid} action=manual_choice_if_unmatched"
            )
        print(
            f"[PARSE] id={msg_id} pid={parsed.pid or '(missing)'} order_no={parsed.order_no} school={parsed.school_name!r} "
            f"order_date={parsed.order_date:%Y-%m-%d} elapsed={parse_elapsed:.2f}s"
        )

        if parsed.pid and db is not None and db.order_import_exists(
            parsed.pid,
            parsed.order_no,
            source=ORDER_IMPORT_SOURCE,
        ):
            print(
                f"[DUPLICATE] id={msg_id} pid={parsed.pid} order_no={parsed.order_no} "
                "action=skip_pdf_move_label"
            )
            label_moved = _move_message_to_imported_label(
                service,
                msg_id=msg_id,
                test_label_id=test_label_id,
                imported_label_id=imported_label_id,
                no_label_update=no_label_update,
            )
            action = AppliedAction(
                message_id=msg_id,
                pid=parsed.pid,
                order_no=parsed.order_no,
                folder_name=f"DUPLICATE {parsed.pid}",
                folder_path="",
                folder_action="duplicate_existing",
                item_id=None,
                db_inserted_item_id=None,
                label_moved=label_moved,
                pdf_output_path=None,
                previous_pdf_path=None,
                previous_pdf_backup_path=None,
                previous_db_pdf_path=None,
                order_history_recorded=False,
            )
            return True, f"Duplicate skipped for {parsed.pid} {parsed.order_no}", "", "duplicate_existing", None, action

        match_t0 = time.perf_counter()
        effective_pid = parsed.pid
        if effective_pid:
            preview_pdf_path = _create_manual_review_preview_pdf(parsed)
            try:
                target_folder, created, should_upsert, matched_item_id, effective_pid = _resolve_target_folder(
                    base_dir,
                    effective_pid,
                    parsed.school_name,
                    parsed.order_date,
                    subject=parsed.subject,
                    preview_pdf_path=preview_pdf_path,
                    pid_auto_corrected=parsed.pid_auto_corrected,
                    pid_raw=parsed.pid_raw,
                    db=db,
                    source_base_dir=source_base_dir,
                    is_cancelled=is_cancelled,
                )
            finally:
                try:
                    if preview_pdf_path is not None and preview_pdf_path.exists():
                        preview_pdf_path.unlink()
                except Exception:
                    pass
        else:
            preview_pdf_path = _create_manual_review_preview_pdf(parsed)
            try:
                target_folder, created, should_upsert, matched_item_id, effective_pid = _resolve_target_folder_without_pid(
                    base_dir,
                    parsed.school_name,
                    parsed.order_date,
                    subject=parsed.subject,
                    preview_pdf_path=preview_pdf_path,
                    db=db,
                    source_base_dir=source_base_dir,
                    is_cancelled=is_cancelled,
                )
            finally:
                try:
                    if preview_pdf_path is not None and preview_pdf_path.exists():
                        preview_pdf_path.unlink()
                except Exception:
                    pass
        match_elapsed = time.perf_counter() - match_t0
        print(
            f"[MATCH] id={msg_id} folder={target_folder.name!r} pid={effective_pid} "
            f"status={'CREATED' if created else 'EXISTING'} elapsed={match_elapsed:.2f}s"
        )

        if db is not None and db.order_import_exists(
            effective_pid,
            parsed.order_no,
            source=ORDER_IMPORT_SOURCE,
        ):
            print(
                f"[DUPLICATE] id={msg_id} pid={effective_pid} order_no={parsed.order_no} "
                "action=skip_pdf_move_label"
            )
            label_moved = _move_message_to_imported_label(
                service,
                msg_id=msg_id,
                test_label_id=test_label_id,
                imported_label_id=imported_label_id,
                no_label_update=no_label_update,
            )
            action = AppliedAction(
                message_id=msg_id,
                pid=effective_pid,
                order_no=parsed.order_no,
                folder_name=target_folder.name,
                folder_path=str(target_folder),
                folder_action="duplicate_existing",
                item_id=matched_item_id,
                db_inserted_item_id=None,
                label_moved=label_moved,
                pdf_output_path=None,
                previous_pdf_path=None,
                previous_pdf_backup_path=None,
                previous_db_pdf_path=None,
                order_history_recorded=False,
            )
            return True, f"Duplicate skipped for {target_folder.name} {parsed.order_no}", target_folder.name, "duplicate_existing", matched_item_id, action

        resolved_item_id = matched_item_id
        db_inserted_item_id: int | None = None
        if should_upsert:
            resolved_item_id = _sync_new_folder_to_db(db, target_folder.name)
            db_inserted_item_id = resolved_item_id
            if resolved_item_id is not None:
                print(f"[DB] id={msg_id} upserted_new_folder={target_folder.name!r} item_id={resolved_item_id}")
            else:
                print(f"[DB] id={msg_id} upsert skipped/unavailable for {target_folder.name!r}")

        previous_db_pdf_path: str | None = None
        if db is not None and resolved_item_id is not None:
            try:
                db_item = db.get_item_by_id(int(resolved_item_id))
                if db_item:
                    previous_db_pdf_path = db_item.pdf_path
            except Exception as exc:
                print(f"[WARN] DB read previous pdf_path failed for id={resolved_item_id}: {exc}")

        existing_before = _find_existing_order_pdf(
            target_folder,
            effective_pid,
            preferred_pdf_path=previous_db_pdf_path,
        )
        if existing_before:
            old_pdf, old_date, old_count = existing_before
            pdf_source = "folder_scan"
            preferred_raw = str(previous_db_pdf_path or "").strip().strip('"')
            if preferred_raw:
                try:
                    if Path(preferred_raw).expanduser().resolve() == old_pdf.resolve():
                        pdf_source = "db_path"
                except Exception:
                    pass
            print(
                f"[PDF] id={msg_id} existing={old_pdf.name!r} count={old_count} "
                f"date={old_date:%Y-%m-%d} source={pdf_source} action=APPEND"
            )
        else:
            print(f"[PDF] id={msg_id} existing=None action=CREATE")

        previous_pdf_path: str | None = None
        previous_pdf_backup_path: str | None = None
        order_history_recorded = False
        if existing_before:
            old_pdf = existing_before[0]
            previous_pdf_path = str(old_pdf)
            with tempfile.NamedTemporaryFile(prefix="damy_order_prev_", suffix=".pdf", delete=False) as tmp_prev:
                previous_pdf_backup_path = tmp_prev.name
            shutil.copy2(old_pdf, previous_pdf_backup_path)

        with tempfile.NamedTemporaryFile(prefix="damy_order_mail_", suffix=".pdf", delete=False) as tmp_pdf:
            temp_pdf_path = Path(tmp_pdf.name)

        try:
            if callable(is_cancelled) and bool(is_cancelled()):
                raise OrderImportCancelled("Order import cancelled by user request.")
            render_t0 = time.perf_counter()
            print(f"[PERF] id={msg_id} step=render_pdf start")
            _render_order_to_pdf(parsed, temp_pdf_path)
            render_elapsed = time.perf_counter() - render_t0
            print(f"[PERF] id={msg_id} step=render_pdf done elapsed={render_elapsed:.2f}s")
            package_count_for_message = _count_order_packages_from_pdf(temp_pdf_path)
            print(f"[PDF] id={msg_id} parsed_package_count={package_count_for_message}")

            merge_t0 = time.perf_counter()
            print(f"[PERF] id={msg_id} step=write_target_pdf start")
            final_pdf = _append_or_create_order_pdf(
                target_folder,
                effective_pid,
                parsed.order_date,
                temp_pdf_path,
                preferred_existing_pdf_path=previous_db_pdf_path,
                new_order_package_count=package_count_for_message,
            )
            merge_elapsed = time.perf_counter() - merge_t0
            print(
                f"[PDF] id={msg_id} output={final_pdf.name!r} "
                f"size={final_pdf.stat().st_size if final_pdf.exists() else 0} "
                f"elapsed={merge_elapsed:.2f}s"
            )
            if db is not None:
                resolved_for_pdf = resolved_item_id
                if resolved_for_pdf is None:
                    try:
                        resolved_row = db.get_item_by_disk_name(
                            target_folder.name,
                            domain=PREPAID_WORKFLOW_DOMAIN,
                            stage=UPCOMING_STAGE,
                        )
                        if resolved_row:
                            resolved_for_pdf = int(resolved_row.id)
                            previous_db_pdf_path = resolved_row.pdf_path
                            resolved_item_id = resolved_for_pdf
                    except Exception as exc:
                        print(f"[WARN] DB resolve by folder failed for {target_folder.name!r}: {exc}")
                if resolved_for_pdf is not None:
                    try:
                        db.set_pdf_path(int(resolved_for_pdf), str(final_pdf))
                    except Exception as exc:
                        raise RuntimeError(f"Failed to update DB pdf_path for item {resolved_for_pdf}: {exc}")
                    try:
                        db.record_order_import(
                            source=ORDER_IMPORT_SOURCE,
                            pid=effective_pid,
                            order_no=parsed.order_no,
                            message_id=msg_id,
                            item_id=int(resolved_for_pdf),
                        )
                        order_history_recorded = True
                    except Exception as exc:
                        raise RuntimeError(
                            f"Failed to record imported order number for PID {effective_pid} "
                            f"and order {parsed.order_no}: {exc}"
                        )
        finally:
            if temp_pdf_path.exists():
                try:
                    temp_pdf_path.unlink()
                except FileNotFoundError:
                    pass

        # Per-message label flow: only move labels after PDF write succeeds for this message.
        if callable(is_cancelled) and bool(is_cancelled()):
            raise OrderImportCancelled("Order import cancelled by user request.")
        label_moved = _move_message_to_imported_label(
            service,
            msg_id=msg_id,
            test_label_id=test_label_id,
            imported_label_id=imported_label_id,
            no_label_update=no_label_update,
        )

        total_elapsed = time.perf_counter() - message_t0
        print(f"[PERF] id={msg_id} step=message_total done elapsed={total_elapsed:.2f}s")

        if created and should_upsert:
            folder_action = "created_new"
        elif created and not should_upsert:
            folder_action = "copied_from_source"
        else:
            folder_action = "existing"

        action = AppliedAction(
            message_id=msg_id,
            pid=effective_pid,
            order_no=parsed.order_no,
            folder_name=target_folder.name,
            folder_path=str(target_folder),
            folder_action=folder_action,
            item_id=resolved_item_id,
            db_inserted_item_id=db_inserted_item_id,
            label_moved=label_moved,
            pdf_output_path=str(final_pdf),
            previous_pdf_path=previous_pdf_path,
            previous_pdf_backup_path=previous_pdf_backup_path,
            previous_db_pdf_path=previous_db_pdf_path,
            order_history_recorded=order_history_recorded,
        )
        return True, f"{target_folder.name} -> {final_pdf.name}", target_folder.name, folder_action, resolved_item_id, action
    except OrderImportCancelled:
        raise
    except Exception as exc:
        raise OrderImportMessageError(
            message_id=msg_id,
            reason="Gmail API error" if isinstance(exc, HttpError) else _classify_failure_reason(str(exc)),
            detail=str(exc),
            subject=subject,
            from_header=from_header,
            header_date=header_date,
            pid=parsed.pid if parsed is not None else None,
            order_no=parsed.order_no if parsed is not None else None,
            school_name=parsed.school_name if parsed is not None else "",
        ) from exc


def _rollback_applied_actions(
    actions: list[AppliedAction],
    *,
    service,
    test_label_id: str,
    imported_label_id: str,
    no_label_update: bool,
    db=None,
) -> list[str]:
    errors: list[str] = []
    for action in reversed(actions):
        try:
            if not no_label_update and action.label_moved and imported_label_id:
                try:
                    service.users().messages().modify(
                        userId="me",
                        id=action.message_id,
                        body={"addLabelIds": [test_label_id], "removeLabelIds": [imported_label_id]},
                    ).execute()
                    print(f"[ROLLBACK] label restored for id={action.message_id}")
                except Exception as exc:
                    errors.append(f"{action.message_id}: label rollback failed: {exc}")

            if action.folder_action in {"created_new", "copied_from_source"}:
                folder = Path(action.folder_path)
                if folder.exists():
                    shutil.rmtree(folder, ignore_errors=True)
            else:
                if action.pdf_output_path:
                    output_pdf = Path(action.pdf_output_path)
                    if output_pdf.exists():
                        output_pdf.unlink()
                if action.previous_pdf_backup_path and action.previous_pdf_path:
                    backup = Path(action.previous_pdf_backup_path)
                    previous = Path(action.previous_pdf_path)
                    if backup.exists():
                        previous.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(backup, previous)

            if db is not None:
                if action.order_history_recorded and action.pid and action.order_no:
                    try:
                        db.delete_order_import_record(action.pid, action.order_no, source=ORDER_IMPORT_SOURCE)
                    except Exception as exc:
                        errors.append(f"{action.message_id}: order history rollback failed: {exc}")
                if action.db_inserted_item_id is not None:
                    try:
                        db.delete_item(int(action.db_inserted_item_id))
                    except Exception as exc:
                        errors.append(f"{action.message_id}: DB rollback failed: {exc}")
                elif action.item_id is not None:
                    try:
                        db.set_pdf_path(int(action.item_id), action.previous_db_pdf_path)
                    except Exception as exc:
                        errors.append(f"{action.message_id}: DB pdf_path rollback failed: {exc}")
        except Exception as exc:
            errors.append(f"{action.message_id}: rollback failed: {exc}")
        finally:
            backup_path = (action.previous_pdf_backup_path or "").strip()
            if backup_path:
                try:
                    bp = Path(backup_path)
                    if bp.exists():
                        bp.unlink()
                except Exception:
                    pass
    return errors


def _classify_failure_reason(detail: str) -> str:
    text = (detail or "").strip().lower()
    if "gmail api error" in text:
        return "Gmail API error"
    if "stream has ended unexpectedly" in text or "eof marker not found" in text or "cannot read malformed pdf" in text:
        return "Existing PDF could not be read"
    if "order no. not found in email" in text:
        return "Order number not found in email"
    if "manual selection was skipped" in text or "manual selection; user skipped" in text:
        return "User skipped manual selection"
    if "pid not found in email" in text:
        return "PID not found in email"
    if "school name not found in email" in text:
        return "School Name not found in email"
    if "permission denied" in text:
        return "Permission denied when writing/copying files"
    if "headless browser print failed" in text:
        return "Headless browser PDF print failed"
    if "no chrome/edge browser found" in text:
        return "Chrome/Edge browser not found"
    if "db matched folder is missing in active base and source" in text:
        return "Matched folder missing in both active base and source"
    if "gmail label not found" in text:
        return "Required Gmail label not found"
    if "gmail label update failed" in text:
        return "Gmail label update failed"
    if "refusing to bump count automatically" in text or "target order pdf filename already exists" in text:
        return "Order PDF name conflict (manual resolution required)"
    return "Other error"


def run(args: argparse.Namespace) -> int:
    base_dir = Path(_resolve_base_dir(args.base_dir)).expanduser().resolve()
    if not base_dir.exists() or not base_dir.is_dir():
        raise NotADirectoryError(f"Base folder not found: {base_dir}")
    if not args.allow_non_damy_base and not _looks_like_damy_base_dir(base_dir):
        raise RuntimeError(
            f"Blocked by safety guard: base_dir is not DAMY ({base_dir}). "
            "Use --allow-non-damy-base only when you intentionally want non-DAMY writes."
        )
    source_base_dir = _resolve_source_base_dir(base_dir, args.source_base_dir)
    if source_base_dir:
        print(f"[INFO] Fallback source dir: {source_base_dir}")
    else:
        print("[INFO] Fallback source dir: None")

    token_path = _resolve_runtime_path(
        args.token_path,
        "DAMY_ORDER_IMPORT_TOKEN_PATH",
        "token.json",
        must_exist=False,
        preferred_path=ORDER_IMPORT_TOKEN_PATH,
    )
    credentials_path = _resolve_runtime_path(
        args.credentials_path,
        "DAMY_ORDER_IMPORT_CREDENTIALS_PATH",
        "credentials.json",
        must_exist=True,
        preferred_path=ORDER_IMPORT_CREDENTIALS_PATH,
    )
    if not credentials_path.exists():
        # Fallback to calendar credentials path when order-specific credential path is not present.
        credentials_path = Path(CALENDAR_CREDENTIALS_PATH).expanduser().resolve()

    service = _build_gmail_service(token_path, credentials_path)
    labels = _load_label_ids(service)

    test_label_key = _normalize_label_name(args.test_label)
    imported_label_key = _normalize_label_name(args.imported_label)
    test_label_id = labels.get(test_label_key)
    imported_label_id = labels.get(imported_label_key)
    if not test_label_id:
        raise RuntimeError(f"Gmail label not found: {args.test_label}")
    if not args.no_label_update and not imported_label_id:
        raise RuntimeError(f"Gmail label not found: {args.imported_label}")

    if args.label_window > 0:
        print(f"[INFO] Label window mode: first {args.label_window} messages from '{args.test_label}'.")
    message_ids = _list_message_ids_for_label(service, test_label_id, label_window=args.label_window)
    print(f"[INFO] Messages fetched for sorting: {len(message_ids)}")
    sort_rows = _load_message_sort_rows(service, message_ids)
    ordered_message_ids = [row.message_id for row in sort_rows]

    if args.max_messages > 0:
        ordered_message_ids = ordered_message_ids[: args.max_messages]

    is_cancelled, cancel_token_file = _build_cancel_checker(args.cancel_token_path)
    if cancel_token_file:
        print(f"[INFO] Cancel token path: {cancel_token_file}")

    db = _build_db_client()
    summary = {
        "total_in_label": len(message_ids),
        "queued": len(ordered_message_ids),
        "processed_ok": 0,
        "processed_failed": 0,
        "duplicates_skipped": 0,
        "cancelled": False,
        "rollback_applied": 0,
        "rollback_errors": [],
        "label_updates": 0,
        "no_label_update": bool(args.no_label_update),
        "touched_folders": [],
        "touched_item_ids": [],
        "created_folders": [],
        "created_item_ids": [],
        "copied_from_source_folders": [],
        "existing_folders": [],
        "errors": [],
        "failure_details": [],
    }
    touched_folders: set[str] = set()
    touched_item_ids: set[int] = set()
    created_folders: set[str] = set()
    created_item_ids: set[int] = set()
    copied_from_source_folders: set[str] = set()
    existing_folders: set[str] = set()
    applied_actions: list[AppliedAction] = []
    was_cancelled = False

    if not ordered_message_ids:
        print("[INFO] No messages found under target label.")
        print("__ORDER_IMPORT_SUMMARY__" + json.dumps(summary, ensure_ascii=False))
        return 0

    for idx, msg_id in enumerate(ordered_message_ids, start=1):
        print(f"[INFO] Processing {idx}/{len(ordered_message_ids)} message={msg_id}")
        if is_cancelled():
            was_cancelled = True
            print("[WARN] Cancel token detected. Stopping import and rolling back this run.")
            break
        try:
            ok, detail, folder_name, folder_action, item_id, action = _process_one_message(
                service,
                msg_id,
                base_dir,
                source_base_dir,
                test_label_id,
                imported_label_id or "",
                no_label_update=bool(args.no_label_update),
                is_cancelled=is_cancelled,
                db=db,
            )
            if ok:
                applied_actions.append(action)
                if folder_action == "duplicate_existing":
                    summary["duplicates_skipped"] += 1
                else:
                    summary["processed_ok"] += 1
                    touched_folders.add(folder_name)
                    if item_id is not None:
                        touched_item_ids.add(int(item_id))
                    if folder_action == "created_new":
                        created_folders.add(folder_name)
                        if item_id is not None:
                            created_item_ids.add(int(item_id))
                    elif folder_action == "copied_from_source":
                        copied_from_source_folders.add(folder_name)
                    else:
                        existing_folders.add(folder_name)
                if not args.no_label_update:
                    summary["label_updates"] += 1
                print(f"[OK] {detail}")
        except OrderImportCancelled:
            was_cancelled = True
            print("[WARN] Order import cancelled by user request. Rolling back this run.")
            break
        except OrderImportMessageError as exc:
            summary["processed_failed"] += 1
            err = f"{exc.message_id}: {exc.detail or exc.reason}"
            summary["errors"].append(err)
            summary["failure_details"].append(
                {
                    "message_id": exc.message_id,
                    "reason": exc.reason,
                    "detail": exc.detail,
                    "subject": exc.subject,
                    "from_header": exc.from_header,
                    "header_date": exc.header_date,
                    "pid": exc.pid,
                    "order_no": exc.order_no,
                    "school_name": exc.school_name,
                }
            )
            print(f"[ERROR] {err}")
        except HttpError as exc:
            summary["processed_failed"] += 1
            err = f"{msg_id}: Gmail API error {exc}"
            summary["errors"].append(err)
            summary["failure_details"].append(
                {
                    "message_id": msg_id,
                    "reason": "Gmail API error",
                    "detail": str(exc),
                    "subject": "",
                    "from_header": "",
                    "header_date": "",
                    "pid": None,
                    "order_no": None,
                    "school_name": "",
                }
            )
            print(f"[ERROR] {err}")
        except Exception as exc:
            summary["processed_failed"] += 1
            err = f"{msg_id}: {exc}"
            summary["errors"].append(err)
            summary["failure_details"].append(
                {
                    "message_id": msg_id,
                    "reason": _classify_failure_reason(str(exc)),
                    "detail": str(exc),
                    "subject": "",
                    "from_header": "",
                    "header_date": "",
                    "pid": None,
                    "order_no": None,
                    "school_name": "",
                }
            )
            print(f"[ERROR] {err}")

    if was_cancelled:
        rollback_errors = _rollback_applied_actions(
            applied_actions,
            service=service,
            test_label_id=test_label_id,
            imported_label_id=imported_label_id or "",
            no_label_update=bool(args.no_label_update),
            db=db,
        )
        summary["cancelled"] = True
        summary["rollback_applied"] = max(0, len(applied_actions) - len(rollback_errors))
        summary["rollback_errors"] = list(rollback_errors)
        touched_folders.clear()
        touched_item_ids.clear()
        created_folders.clear()
        created_item_ids.clear()
        copied_from_source_folders.clear()
        existing_folders.clear()
        summary["processed_ok"] = 0
        summary["duplicates_skipped"] = 0
        summary["label_updates"] = 0
    else:
        # Cleanup per-message backup files used for rollback safety.
        for action in applied_actions:
            backup_path = (action.previous_pdf_backup_path or "").strip()
            if not backup_path:
                continue
            try:
                bp = Path(backup_path)
                if bp.exists():
                    bp.unlink()
            except Exception:
                pass

    print(
        f"[DONE] ok={summary['processed_ok']} failed={summary['processed_failed']} "
        f"duplicates_skipped={summary['duplicates_skipped']} "
        f"labels_updated={summary['label_updates']} no_label_update={summary['no_label_update']}"
    )
    if summary["errors"]:
        for err in summary["errors"]:
            print(f"[FAIL] {err}")
    summary["touched_folders"] = sorted(touched_folders, key=str.lower)
    summary["touched_item_ids"] = sorted(touched_item_ids)
    summary["created_folders"] = sorted(created_folders, key=str.lower)
    summary["created_item_ids"] = sorted(created_item_ids)
    summary["copied_from_source_folders"] = sorted(copied_from_source_folders, key=str.lower)
    summary["existing_folders"] = sorted(existing_folders, key=str.lower)
    print("[SUMMARY] -------------------------------")
    print(f"[SUMMARY] Total fetched : {summary['total_in_label']}")
    print(f"[SUMMARY] Total queued  : {summary['queued']}")
    print(f"[SUMMARY] Success       : {summary['processed_ok']}")
    print(f"[SUMMARY] Failed        : {summary['processed_failed']}")
    print(f"[SUMMARY] Duplicates    : {summary['duplicates_skipped']}")
    print(f"[SUMMARY] New folders   : {len(summary['created_folders'])}")
    for idx, name in enumerate(summary["created_folders"], start=1):
        print(f"[SUMMARY][NEW {idx}] {name}")
    print(f"[SUMMARY] Copied from source into active base : {len(summary['copied_from_source_folders'])}")
    for idx, name in enumerate(summary["copied_from_source_folders"], start=1):
        print(f"[SUMMARY][COPIED {idx}] {name}")
    if summary["failure_details"]:
        print("[SUMMARY] Failure details:")
        for idx, item in enumerate(summary["failure_details"], start=1):
            message_id = str(item.get("message_id", "")).strip()
            reason = str(item.get("reason", "")).strip()
            detail = str(item.get("detail", "")).strip()
            print(f"[SUMMARY][FAIL {idx}] id={message_id} reason={reason} detail={detail}")
    else:
        print("[SUMMARY] Failure details: none")
    if summary["cancelled"]:
        print(
            f"[SUMMARY] Cancelled with rollback: "
            f"applied={summary['rollback_applied']} errors={len(summary['rollback_errors'])}"
        )
        for idx, item in enumerate(summary["rollback_errors"], start=1):
            print(f"[SUMMARY][ROLLBACK_FAIL {idx}] {item}")
    print("[SUMMARY] -------------------------------")
    print("__ORDER_IMPORT_SUMMARY__" + json.dumps(summary, ensure_ascii=False))
    if summary["cancelled"]:
        return EXIT_CODE_CANCELLED
    return 0


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    exit_code = run(args)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
