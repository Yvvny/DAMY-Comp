from collections import defaultdict
from datetime import datetime, time, timezone
from pathlib import Path
import argparse
import html
import json
import os
import re
import shutil
import sys
import tkinter as tk
from tkinter import messagebox

try:
    import win32com.client as win32_client
except Exception:
    win32_client = None

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    from folder_manager.config import (
        CALENDAR_CREDENTIALS_PATH,
        CALENDAR_TOKEN_PATH,
        DB_HOST,
        DB_NAME,
        DB_PASS,
        DB_PORT,
        DB_USER,
    )
    from folder_manager.db import (
        DB,
        WORKFLOW_DOMAIN_PREPAID,
        detect_stage_from_disk_name,
        normalize_display_name,
        parse_contact_fields_from_note,
        parse_job_code,
        parse_shoot_date_from_display,
    )
except Exception:
    DB = None
    WORKFLOW_DOMAIN_PREPAID = "prepaid"
    DB_HOST = DB_NAME = DB_PASS = DB_USER = None
    DB_PORT = 5432
    CALENDAR_TOKEN_PATH = os.path.join("folder_manager", "calendar_import_v3", "token.json")
    CALENDAR_CREDENTIALS_PATH = os.path.join("folder_manager", "calendar_import_v3", "credentials.json")

    def detect_stage_from_disk_name(_disk_name: str) -> int:
        return 1

    def normalize_display_name(disk_name: str) -> str:
        return disk_name

    def parse_job_code(_display_name: str) -> str | None:
        return None

    def parse_contact_fields_from_note(_note_payload: str | None):
        return None, None, None

    def parse_shoot_date_from_display(_display_name: str):
        return None


SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_BASE_PATH = r"T:\DAMY"
PREPAID_WORKFLOW_DOMAIN = str(WORKFLOW_DOMAIN_PREPAID or "prepaid").strip().lower() or "prepaid"
UPCOMING_STAGE = 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate upcoming DAMY folders from Google Calendar events."
    )
    parser.add_argument(
        "--calendar-import",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--txt-only",
        action="store_true",
        help="Deprecated compatibility flag. Calendar import now creates folders and note text only.",
    )
    parser.add_argument(
        "--run-cancellation",
        action="store_true",
        help="Run only cancellation/reschedule/ignore checks and skip folder import creation.",
    )
    parser.add_argument(
        "--cancellation-source-dir",
        default=None,
        help="Optional source folder for cancellation scan. Reads folder names from this source, but actions apply to --base-dir.",
    )
    parser.add_argument(
        "--base-dir",
        default=None,
        help="Override DAMY root directory. If omitted, uses DAMY_CALENDAR_BASE_DIR, DAMY_BASE_DIR, then T:\\DAMY.",
    )
    parser.add_argument(
        "--psd-dir",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--token-path",
        default=None,
        help="Override Google OAuth token.json path.",
    )
    parser.add_argument(
        "--credentials-path",
        default=None,
        help="Override Google OAuth credentials.json path.",
    )
    parser.add_argument(
        "--no-db-write",
        action="store_true",
        help="Skip DB row upsert and note sync.",
    )
    parser.add_argument(
        "--skip-cancellation",
        action="store_true",
        help="Skip cancellation dialog/actions.",
    )
    parser.add_argument(
        "--cancellation-report-json",
        action="store_true",
        help="Print cancellation report JSON and exit without showing dialogs/actions.",
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
        app_dir / "folder_manager" / "calendar_import_v3" / default_name,
        Path.cwd() / default_name,
        Path.cwd() / "folder_manager" / "calendar_import_v3" / default_name,
    ])

    if must_exist:
        for path in candidates:
            if path.exists():
                return path.resolve()

    fallback = candidates[0]
    return fallback.resolve()


def _resolve_base_path(base_dir_arg: str | None) -> str:
    if base_dir_arg:
        return base_dir_arg
    env_base = (os.environ.get("DAMY_CALENDAR_BASE_DIR") or os.environ.get("DAMY_BASE_DIR") or "").strip()
    return env_base or DEFAULT_BASE_PATH


def _build_db_client():
    if DB is None:
        print("[WARN] folder_manager DB modules unavailable; DB sync disabled.")
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


def _sync_db_note_for_folder(db, disk_name: str, note_text: str) -> None:
    display_name = normalize_display_name(disk_name)
    note_value = note_text.rstrip() or None
    contact_name, contact_email, contact_phone = parse_contact_fields_from_note(note_value)
    item_id = int(
        db.upsert_into_domain(
            disk_name=disk_name,
            domain=PREPAID_WORKFLOW_DOMAIN,
            step=None,
            stage=UPCOMING_STAGE,
        )
    )
    db.set_note(item_id, note_value)
    db.set_contact_if_empty(
        item_id,
        contact_name=contact_name,
        contact_email=contact_email,
        contact_phone=contact_phone,
    )
def extract_data(text):
    data = {}
    booking_pattern = r"<b>(.*?)<\/b><br>(.*?)<br>"
    matches = re.findall(booking_pattern, text)
    if len(matches) == 0:
        booking_pattern = r"<b>(.*?)<\/b>\n(.*?)\n"
        matches = re.findall(booking_pattern, text)

    for match in matches:
        key = match[0].strip()
        value = match[1].strip()
        data[key] = value
    return data


def strip_html_for_text(text):
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


def impossible_char(folder_path):
    return bool(re.search(r"[\/:*?\"<>|]", folder_path))


def normalize_school_name(name):
    return re.sub(r"\s+", " ", name).strip().lower()


def _extract_school_base_text(text: str) -> str:
    value = (text or "").strip()
    if not value:
        return ""
    # Ignore Windows duplicate suffixes like "(1)" at end.
    value = re.sub(r"\s*\(\d+\)\s*$", "", value).strip()
    # Ignore Picture Day IDs when deriving date+school keys.
    value = re.sub(r"\bP\d{8,}\b", " ", value)
    value = value.replace("+", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _looks_like_calendar_folder_name(folder_name: str) -> bool:
    name = (folder_name or "").strip()
    if name.startswith("1.Upcoming") or name.startswith("1. Upcoming"):
        return True
    return bool(re.match(r"^\d{6}\b", name))


def parse_upcoming_folder_name(folder_name):
    normalized = (folder_name or "").strip()
    if normalized.startswith("1. Upcoming"):
        normalized = "1.Upcoming" + normalized[len("1. Upcoming"):]

    if normalized.startswith("1.Upcoming"):
        tail = normalized[len("1.Upcoming"):].strip()
    else:
        tail = normalized

    parts = tail.split()
    if not parts:
        return None
    yymmdd = parts[0].strip()
    if not re.fullmatch(r"\d{6}", yymmdd):
        return None

    rest = tail[len(yymmdd):].strip()
    if not rest:
        return None

    # Support folders with single PID, PID+PID, no PID suffix,
    # and duplicate suffixes like "(1)".
    school_name = _extract_school_base_text(rest)
    if not school_name:
        return None

    pids = _extract_pids(rest)
    picture_id = pids[0] if pids else None
    return yymmdd, school_name, picture_id


def _extract_calendar_key_from_db_name(name: str) -> tuple[str, str] | None:
    cleaned = normalize_display_name(name or "")
    parsed = parse_upcoming_folder_name(cleaned)
    if parsed:
        yymmdd, school_name, _ = parsed
        key = normalize_school_name(school_name)
        if key:
            return yymmdd, key
        return None

    # Fallback for rows that don't strictly match "YYMMDD School PID".
    m = re.match(r"^\s*(\d{6})\s+(.+)$", cleaned)
    if not m:
        return None
    yymmdd = m.group(1)
    fallback = _extract_school_base_text(m.group(2).strip())
    key = normalize_school_name(fallback)
    if key:
        return yymmdd, key
    return None


def load_existing_calendar_keys_from_db(db) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    if db is None:
        return keys

    try:
        rows = db.list_by_stage(UPCOMING_STAGE, domain=PREPAID_WORKFLOW_DOMAIN)
        for row in rows:
            source_name = row.display_name or row.disk_name or ""
            key = _extract_calendar_key_from_db_name(source_name)
            if key:
                keys.add(key)
    except Exception as exc:
        print(f"[WARN] Could not load existing DB date+school keys: {exc}")

    return keys


def load_existing_pids_from_db(db) -> set[str]:
    pids: set[str] = set()
    if db is None:
        return pids

    try:
        rows = db.list_by_stage(UPCOMING_STAGE, domain=PREPAID_WORKFLOW_DOMAIN)
        for row in rows:
            for token in re.findall(r"\bP\d{8,}\b", str(row.disk_name or "")):
                pids.add(token)
            for token in re.findall(r"\bP\d{8,}\b", str(row.display_name or "")):
                pids.add(token)
            pid = str(row.pid or "").strip()
            if pid:
                pids.add(pid)
    except Exception as exc:
        print(f"[WARN] Could not load existing DB PIDs: {exc}")

    return pids


def _ensure_unique_pid(candidate_pid: str, used_pids: set[str]) -> str:
    pid = (candidate_pid or "").strip()
    if not pid:
        return pid
    m = re.fullmatch(r"P(\d+)", pid)
    if not m:
        while pid in used_pids:
            pid = f"{pid}7"
        used_pids.add(pid)
        return pid

    base_numeric = int(m.group(1))
    width = max(8, len(m.group(1)))
    while pid in used_pids:
        base_numeric += 7
        pid = "P" + str(base_numeric).zfill(width)
    used_pids.add(pid)
    return pid


def get_upcoming_folder_rows(
    base_path: str,
    db=None,
    source_base_path: str | None = None,
) -> tuple[list[dict], list[str], int]:
    rows: list[dict] = []
    invalid_prefix_folders: list[str] = []
    candidate_count = 0

    db_names: list[str] | None = None
    if db is not None:
        try:
            db_names = [it.disk_name for it in db.list_by_stage(1)]
        except Exception as exc:
            print(f"[WARN] Could not read DB Upcoming column; fallback to folder scan: {exc}")
            db_names = None

    # Canonical source of truth: DB stage=1.
    # source_base_path is only used as an optional filesystem source path for copy-on-demand.
    if db_names is not None:
        for disk_name in db_names:
            candidate_count += 1
            parsed = parse_upcoming_folder_name(disk_name)
            if not parsed:
                invalid_prefix_folders.append(disk_name)
                continue

            yymmdd, school_name, _ = parsed
            school_key = normalize_school_name(school_name)
            source_path = os.path.join(source_base_path or base_path, disk_name)
            rows.append(
                {
                    "folder_name": disk_name,
                    "folder_path": os.path.join(base_path, disk_name),
                    "source_folder_path": source_path,
                    "yymmdd": yymmdd,
                    "school_name": school_name,
                    "school_key": school_key,
                }
            )
        rows.sort(key=lambda r: (r["yymmdd"], r["folder_name"].lower()))
        return rows, invalid_prefix_folders, candidate_count

    scan_base = source_base_path or base_path
    if not os.path.isdir(scan_base):
        return rows, invalid_prefix_folders, candidate_count

    if source_base_path:
        for entry in os.scandir(scan_base):
            if not entry.is_dir():
                continue
            if not _looks_like_calendar_folder_name(entry.name):
                continue

            candidate_count += 1
            parsed = parse_upcoming_folder_name(entry.name)
            if not parsed:
                invalid_prefix_folders.append(entry.name)
                continue

            yymmdd, school_name, _ = parsed
            school_key = normalize_school_name(school_name)
            rows.append(
                {
                    "folder_name": entry.name,
                    "folder_path": os.path.join(base_path, entry.name),
                    "source_folder_path": entry.path,
                    "yymmdd": yymmdd,
                    "school_name": school_name,
                    "school_key": school_key,
                }
            )
    else:
        for entry in os.scandir(base_path):
            if not entry.is_dir():
                continue
            if not _looks_like_calendar_folder_name(entry.name):
                continue

            candidate_count += 1
            parsed = parse_upcoming_folder_name(entry.name)
            if not parsed:
                invalid_prefix_folders.append(entry.name)
                continue

            yymmdd, school_name, _ = parsed
            school_key = normalize_school_name(school_name)
            rows.append(
                {
                    "folder_name": entry.name,
                    "folder_path": entry.path,
                    "source_folder_path": entry.path,
                    "yymmdd": yymmdd,
                    "school_name": school_name,
                    "school_key": school_key,
                }
            )

    rows.sort(key=lambda r: (r["yymmdd"], r["folder_name"].lower()))
    return rows, invalid_prefix_folders, candidate_count


def build_unique_folder_path(parent_dir, folder_name):
    candidate = os.path.join(parent_dir, folder_name)
    if not os.path.exists(candidate):
        return candidate

    i = 1
    while True:
        with_suffix = f"{folder_name} ({i})"
        candidate = os.path.join(parent_dir, with_suffix)
        if not os.path.exists(candidate):
            return candidate
        i += 1


def move_folder_to_cancel(base_path: str, folder_path: str) -> str:
    cancel_dir = os.path.join(base_path, "cancel")
    os.makedirs(cancel_dir, exist_ok=True)
    folder_name = os.path.basename(folder_path)
    target_path = build_unique_folder_path(cancel_dir, folder_name)
    os.rename(folder_path, target_path)
    return target_path


def ensure_target_folder_for_action(row: dict, base_path: str) -> str:
    folder_name = row.get("folder_name", "")
    target_path = row.get("folder_path") or os.path.join(base_path, folder_name)
    if os.path.isdir(target_path):
        return target_path

    source_path = row.get("source_folder_path")
    if source_path and os.path.isdir(source_path):
        os.makedirs(base_path, exist_ok=True)
        shutil.copytree(source_path, target_path)
        return target_path

    raise RuntimeError(f"Folder not found in target or source: {folder_name}")


def _extract_pids(folder_name: str) -> list[str]:
    return re.findall(r"\bP\d{8,}\b", folder_name or "")


def _strip_pid_suffix(folder_name: str) -> str:
    return re.sub(r"\s+P\d{8,}(?:\+P\d{8,})*$", "", folder_name or "").strip()


def _build_merged_folder_name(primary_folder_name: str, secondary_folder_name: str) -> str:
    base_name = _strip_pid_suffix(primary_folder_name)
    pids = list(dict.fromkeys(_extract_pids(primary_folder_name) + _extract_pids(secondary_folder_name)))
    if not pids:
        return base_name
    return f"{base_name} {'+'.join(pids)}".strip()


def _copy_item_with_merge(src_path: str, dst_dir: str) -> None:
    name = os.path.basename(src_path)
    dst_path = os.path.join(dst_dir, name)

    if not os.path.exists(dst_path):
        if os.path.isdir(src_path):
            shutil.copytree(src_path, dst_path)
        else:
            shutil.copy2(src_path, dst_path)
        return

    if os.path.isdir(src_path) and os.path.isdir(dst_path):
        _merge_folder_contents_copy(src_path, dst_path)
        return

    base, ext = os.path.splitext(name)
    i = 1
    while True:
        candidate = os.path.join(dst_dir, f"{base} ({i}){ext}")
        if not os.path.exists(candidate):
            if os.path.isdir(src_path):
                shutil.copytree(src_path, candidate)
            else:
                shutil.copy2(src_path, candidate)
            return
        i += 1


def _merge_folder_contents_copy(src_dir: str, dst_dir: str) -> None:
    for child in os.listdir(src_dir):
        _copy_item_with_merge(os.path.join(src_dir, child), dst_dir)


def merge_two_folders(base_path: str, primary_folder_path: str, secondary_folder_path: str) -> str:
    if not os.path.isdir(primary_folder_path) or not os.path.isdir(secondary_folder_path):
        raise RuntimeError("One or both selected folders do not exist.")
    if os.path.abspath(primary_folder_path) == os.path.abspath(secondary_folder_path):
        raise RuntimeError("Cannot merge a folder into itself.")

    # Merge by copy: keep source folder/files unchanged.
    _merge_folder_contents_copy(secondary_folder_path, primary_folder_path)

    primary_name = os.path.basename(primary_folder_path)
    secondary_name = os.path.basename(secondary_folder_path)
    merged_name = _build_merged_folder_name(primary_name, secondary_name)
    if not merged_name or merged_name == primary_name:
        return primary_folder_path

    desired_path = build_unique_folder_path(base_path, merged_name)
    if os.path.abspath(desired_path) != os.path.abspath(primary_folder_path):
        os.rename(primary_folder_path, desired_path)
        return desired_path
    return primary_folder_path


def ask_missing_school_action(
    parent,
    folder_name: str,
    school_name: str,
    yymmdd: str,
    base_path: str,
    reason_text: str | None = None,
) -> str:
    result = {"value": "ignore"}

    dlg = tk.Toplevel(parent)
    dlg.title("Missing School in Calendar")
    dlg.resizable(False, False)
    dlg.transient(parent)
    dlg.grab_set()
    dlg.attributes("-topmost", True)
    dlg.lift()
    dlg.focus_force()

    msg = (
        f"Folder: {folder_name}\n"
        f"School: {school_name}\n"
        f"Date: {yymmdd}\n"
        f"Base: {base_path}\n\n"
        f"{reason_text or 'No matching calendar event found.'}\n\n"
        "Choose action:"
    )
    tk.Label(dlg, text=msg, justify="left", padx=14, pady=12).pack(fill="both")

    row = tk.Frame(dlg, padx=10, pady=10)
    row.pack(fill="x")

    def _set(value: str) -> None:
        result["value"] = value
        dlg.destroy()

    tk.Button(row, text="Cancel", width=12, command=lambda: _set("cancel")).pack(side="left", padx=4)
    tk.Button(row, text="Reschedule", width=12, command=lambda: _set("reschedule")).pack(side="left", padx=4)
    tk.Button(row, text="Ignore", width=12, command=lambda: _set("ignore")).pack(side="left", padx=4)

    dlg.protocol("WM_DELETE_WINDOW", lambda: _set("ignore"))
    parent.wait_window(dlg)
    return result["value"]


def _parse_yymmdd_to_date(yymmdd: str):
    try:
        return datetime.strptime(str(yymmdd), "%y%m%d").date()
    except Exception:
        return None


def _resolve_missing_reason(yymmdd: str) -> tuple[str, str]:
    row_date = _parse_yymmdd_to_date(yymmdd)
    today_local = datetime.now().astimezone().date()
    if row_date and row_date < today_local:
        return "date_past", "Date is in the past"
    return "no_matching_calendar_event", "No matching calendar event found"


def _build_missing_rows(folder_rows: list[dict], calendar_event_keys: set[tuple[str, str]]) -> list[dict]:
    missing_rows: list[dict] = []
    for row in folder_rows:
        school_key = row.get("school_key")
        yymmdd = row.get("yymmdd")
        if not school_key:
            continue
        if (yymmdd, school_key) in calendar_event_keys:
            continue
        reason_code, reason_text = _resolve_missing_reason(str(yymmdd))
        enriched = dict(row)
        enriched["reason_code"] = reason_code
        enriched["reason_text"] = reason_text
        missing_rows.append(enriched)
    return missing_rows


def ask_reschedule_target_folder(
    parent,
    school_name: str,
    current_folder_name: str,
    folder_names: list[str],
) -> str | None:
    result: dict[str, str | None] = {"value": None}
    candidate_names = [name for name in folder_names if name != current_folder_name]
    if not candidate_names:
        return None

    dlg = tk.Toplevel(parent)
    dlg.title("Reschedule Merge")
    dlg.geometry("700x380")
    dlg.transient(parent)
    dlg.grab_set()
    dlg.attributes("-topmost", True)
    dlg.lift()
    dlg.focus_force()

    tk.Label(
        dlg,
        text=(
            f"School: {school_name}\n"
            f"Current folder: {current_folder_name}\n\n"
            "Select ONE target folder.\n"
            "Merge result name = target folder base + PID."
        ),
        justify="left",
        padx=12,
        pady=10,
    ).pack(anchor="w")

    listbox = tk.Listbox(dlg, selectmode=tk.SINGLE)
    for name in candidate_names:
        listbox.insert(tk.END, name)
    listbox.pack(fill="both", expand=True, padx=12, pady=(0, 10))

    row = tk.Frame(dlg, padx=10, pady=8)
    row.pack(fill="x")

    def _confirm() -> None:
        selected_idx = list(listbox.curselection())
        if len(selected_idx) != 1:
            messagebox.showwarning("Select One", "Please select exactly one target folder.", parent=dlg)
            return
        result["value"] = candidate_names[selected_idx[0]]
        dlg.destroy()

    def _ignore() -> None:
        result["value"] = None
        dlg.destroy()

    tk.Button(row, text="Merge Into Target", width=16, command=_confirm).pack(side="left", padx=4)
    tk.Button(row, text="Ignore", width=10, command=_ignore).pack(side="left", padx=4)

    dlg.protocol("WM_DELETE_WINDOW", _ignore)
    parent.wait_window(dlg)
    return result["value"]


def handle_missing_school_actions(
    base_path,
    calendar_event_keys: set[tuple[str, str]],
    db=None,
    source_base_path: str | None = None,
):
    folder_rows, invalid_prefix_folders, candidate_count = get_upcoming_folder_rows(
        base_path,
        db=db,
        source_base_path=source_base_path,
    )
    missing_rows = _build_missing_rows(folder_rows, calendar_event_keys)

    if not missing_rows:
        root = tk.Tk()
        root.title("Cancellation Check")
        root.geometry("1x1+0+0")
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()
        root.update()
        lines = [
            "No missing folder-event matches found.",
            f"Checked candidate folders: {candidate_count}",
            f"Valid folders used in check: {len(folder_rows)}",
            f"Calendar date+school keys: {len(calendar_event_keys)}",
        ]
        if invalid_prefix_folders:
            lines.append("")
            lines.append("Skipped (invalid format):")
            lines.extend(invalid_prefix_folders[:10])
            if len(invalid_prefix_folders) > 10:
                lines.append(f"...and {len(invalid_prefix_folders) - 10} more")
        messagebox.showinfo("Cancellation Check", "\n".join(lines), parent=root)
        root.destroy()
        return

    root = tk.Tk()
    root.title("Missing School Actions")
    root.geometry("1x1+0+0")
    root.attributes("-topmost", True)
    root.lift()
    root.focus_force()
    root.update()

    selectable_folder_map: dict[str, dict] = {}
    for row in folder_rows:
        selectable_folder_map[row["folder_name"]] = row

    selectable_folder_names = sorted(selectable_folder_map.keys(), key=str.lower)

    actions_taken = []
    for row in missing_rows:
        folder_name = row["folder_name"]
        school_name = row["school_name"]
        yymmdd = row["yymmdd"]
        reason_text = row.get("reason_text")

        action = ask_missing_school_action(
            root,
            folder_name,
            school_name,
            yymmdd,
            base_path,
            reason_text=reason_text,
        )

        if action == "ignore":
            actions_taken.append(f"Ignored: {folder_name}")
            continue

        if action == "reschedule":
            if len(selectable_folder_names) < 2:
                actions_taken.append("Reschedule skipped (need >=2 selectable folders).")
                continue

            target_name = ask_reschedule_target_folder(root, school_name, folder_name, selectable_folder_names)
            if not target_name:
                actions_taken.append(f"Reschedule ignored: {folder_name}")
                continue

            current_row = selectable_folder_map.get(folder_name)
            target_row = selectable_folder_map.get(target_name)
            if not current_row or not target_row:
                actions_taken.append(f"Reschedule failed (folder not found): {folder_name}")
                continue

            if db is None:
                actions_taken.append(f"Reschedule skipped (DB unavailable): {folder_name}")
                continue

            try:
                current_item = db.get_item_by_disk_name(
                    folder_name,
                    domain=PREPAID_WORKFLOW_DOMAIN,
                    stage=UPCOMING_STAGE,
                )
                target_item = db.get_item_by_disk_name(
                    target_name,
                    domain=PREPAID_WORKFLOW_DOMAIN,
                    stage=UPCOMING_STAGE,
                )
                if not current_item or not target_item:
                    actions_taken.append(f"Reschedule DB rows missing: {folder_name}")
                    continue

                merged_name_raw = _build_merged_folder_name(target_name, folder_name) or target_name
                merged_name = db.get_unique_disk_name(merged_name_raw, exclude_item_id=target_item.id)
                if merged_name != target_name:
                    db.update_disk_name(target_item.id, merged_name)

                db.delete_item(current_item.id)
                actions_taken.append(
                    f"Rescheduled (DB-only): {folder_name} -> {target_name} => {merged_name}"
                )

                selectable_folder_map.pop(folder_name, None)
                selectable_folder_map.pop(target_name, None)
                selectable_folder_map[merged_name] = {
                    "folder_name": merged_name,
                    "folder_path": os.path.join(base_path, merged_name),
                    "source_folder_path": os.path.join(base_path, merged_name),
                }
                selectable_folder_names = sorted(selectable_folder_map.keys(), key=str.lower)
            except Exception as exc:
                actions_taken.append(f"Reschedule DB merge failed: {folder_name} ({exc})")
            continue

        if db is None:
            actions_taken.append(f"Cancel skipped (DB unavailable): {folder_name}")
            continue

        try:
            current_item = db.get_item_by_disk_name(
                folder_name,
                domain=PREPAID_WORKFLOW_DOMAIN,
                stage=UPCOMING_STAGE,
            )
            if current_item:
                db.delete_item(current_item.id)
                actions_taken.append(f"Cancelled (DB-only): {folder_name}")
            else:
                actions_taken.append(f"Cancel skipped (DB row missing): {folder_name}")
            selectable_folder_map.pop(folder_name, None)
            selectable_folder_names = sorted(selectable_folder_map.keys(), key=str.lower)
        except Exception as exc:
            actions_taken.append(f"Cancel DB failed: {folder_name} ({exc})")

    root.destroy()

    if actions_taken:
        summary = "\n".join(actions_taken[:20])
        if len(actions_taken) > 20:
            summary += f"\n...and {len(actions_taken) - 20} more"
        print("Missing-school actions:")
        for action in actions_taken:
            print(action)
        root2 = tk.Tk()
        root2.withdraw()
        messagebox.showinfo("Missing-school actions complete", summary, parent=root2)
        root2.destroy()


def build_cancellation_report(
    base_path,
    calendar_event_keys: set[tuple[str, str]],
    db=None,
    source_base_path: str | None = None,
) -> dict:
    folder_rows, invalid_prefix_folders, candidate_count = get_upcoming_folder_rows(
        base_path,
        db=db,
        source_base_path=source_base_path,
    )
    missing_rows = _build_missing_rows(folder_rows, calendar_event_keys)
    return {
        "base_path": base_path,
        "prefixed_count": candidate_count,
        "candidate_count": candidate_count,
        "valid_rows_count": len(folder_rows),
        "calendar_keys_count": len(calendar_event_keys),
        "invalid_prefix_folders": invalid_prefix_folders,
        "folder_rows": folder_rows,
        "missing_rows": missing_rows,
    }


def DateChanger(mmddyy_or_more):
    s = str(mmddyy_or_more).strip()
    if len(s) < 6:
        raise ValueError(f"Too short for a date: '{s}'")
    mmddyy = s[:6]
    parsed_date = datetime.strptime(mmddyy, "%m%d%y")

    day = parsed_date.day
    if 4 <= day <= 20 or 24 <= day <= 30:
        day_str = f"{day}th"
    else:
        day_str = f"{day}st" if day == 1 else f"{day}nd" if day == 2 else f"{day}rd"

    return parsed_date.strftime("%b. ") + day_str + parsed_date.strftime(" %Y")


def form_creator(folder_path, folder_name, PID, mmddyy, school_name, psd_path):
    jpg_filename = folder_name.replace(" ", "-") + ".jpg"
    jpgfile = os.path.join(folder_path, jpg_filename)

    if win32_client is None:
        raise RuntimeError("Photoshop export requires pywin32 (win32com).")

    ps_app = win32_client.Dispatch("Photoshop.Application")
    ps_app.Open(psd_path)
    doc = ps_app.Application.ActiveDocument

    formatted_date = DateChanger(mmddyy)

    try:
        for i in range(1, 4):
            doc.ArtLayers[f"School Name {i}"].TextItem.Contents = school_name
            doc.ArtLayers[f"Date {i}"].TextItem.Contents = formatted_date

        for i in range(1, 3):
            doc.ArtLayers[f"PID {i}"].TextItem.Contents = PID

        options = win32_client.Dispatch("Photoshop.ExportOptionsSaveForWeb")
        options.Format = 6
        options.Quality = 100

        doc.Export(ExportIn=jpgfile, ExportAs=2, Options=options)
        print(f"Saved JPG: {jpgfile}")
    finally:
        try:
            doc.Close(2)
        except Exception:
            pass


def parse_event_start_date(event):
    start = event.get("start", {}) or {}
    if "dateTime" in start:
        raw = str(start["dateTime"]).strip().replace("Z", "+00:00")
        return datetime.fromisoformat(raw).date()
    return datetime.strptime(start["date"], "%Y-%m-%d").date()


def main(argv=None):
    args = parse_args(argv)

    base_path = _resolve_base_path(args.base_dir)
    cancellation_source_dir = (
        (args.cancellation_source_dir or "").strip()
        or (os.environ.get("DAMY_CANCELLATION_SOURCE_DIR") or "").strip()
        or None
    )
    # If source override points to the same path as base, treat it as disabled.
    # This preserves DB stage filtering for cancellation checks.
    if cancellation_source_dir:
        try:
            base_norm = os.path.normcase(os.path.abspath(base_path))
            source_norm = os.path.normcase(os.path.abspath(cancellation_source_dir))
            if source_norm == base_norm:
                cancellation_source_dir = None
        except Exception:
            pass
    token_path = _resolve_runtime_path(
        args.token_path,
        "DAMY_CALENDAR_TOKEN_PATH",
        "token.json",
        must_exist=False,
        preferred_path=CALENDAR_TOKEN_PATH,
    )
    credentials_path = _resolve_runtime_path(
        args.credentials_path,
        "DAMY_CALENDAR_CREDENTIALS_PATH",
        "credentials.json",
        must_exist=True,
        preferred_path=CALENDAR_CREDENTIALS_PATH,
    )

    db = None if args.no_db_write else _build_db_client()

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                try:
                    token_path.unlink()
                except OSError:
                    pass
                creds = None

        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)

        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, "w", encoding="utf-8") as token:
            token.write(creds.to_json())

    try:
        print("[INFO] Building Google Calendar service...")
        if cancellation_source_dir:
            print(
                "[INFO] Cancellation source override enabled: "
                f"{cancellation_source_dir} (actions apply to base path: {base_path})"
            )
        service = build("calendar", "v3", credentials=creds)
        local_now = datetime.now().astimezone()
        today_local = local_now.date()
        lookup_start_local = datetime.combine(today_local, time.min, tzinfo=local_now.tzinfo)
        time_min = lookup_start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        print(f"[INFO] Fetching events from {time_min} ...")
        events_result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=time_min,
                maxResults=1000,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        events = events_result.get("items", [])
        print(f"[INFO] Events fetched: {len(events)}")
        if not events and not args.run_cancellation:
            print("No upcoming events found.")
            return
        if not events and args.run_cancellation:
            print("[INFO] No upcoming events found; running cancellation checks with empty Gmail school list.")

        faulty_folder_paths = []
        grouped_by_date = defaultdict(list)
        calendar_event_keys: set[tuple[str, str]] = set()

        for event in events:
            description = event.get("description", "")
            data = extract_data(description)
            school_name = data.get("School Name", "")
            if not school_name:
                continue

            try:
                event_date = parse_event_start_date(event)
            except Exception as exc:
                print(f"[WARN] Skipping event with invalid start date: {exc}")
                continue

            mmddyy = event_date.strftime("%m%d%y")
            if event_date < today_local:
                continue

            yymmdd = event_date.strftime("%y%m%d")
            school_key = normalize_school_name(school_name)
            if school_key:
                # Cancellation check only compares against today+future events.
                calendar_event_keys.add((yymmdd, school_key))

            grouped_by_date[mmddyy].append((event, data, school_name, description))
        print(f"[INFO] Calendar date+school keys found: {len(calendar_event_keys)}")

        if args.cancellation_report_json:
            report = build_cancellation_report(
                base_path,
                calendar_event_keys,
                db=db,
                source_base_path=cancellation_source_dir,
            )
            print("__CANCELLATION_REPORT__" + json.dumps(report, ensure_ascii=False))
            return

        if not args.run_cancellation:
            existing_db_event_keys = load_existing_calendar_keys_from_db(db)
            existing_db_pids = load_existing_pids_from_db(db)
            if existing_db_event_keys:
                print(
                    f"[INFO] Existing DB date+school keys loaded: {len(existing_db_event_keys)} "
                    "(existing events will be skipped)."
                )
            if existing_db_pids:
                print(f"[INFO] Existing DB PIDs loaded: {len(existing_db_pids)}")

            for mmddyy, group in grouped_by_date.items():
                group.sort(key=lambda x: x[0].get("created", ""))

                for i, (event, data, school_name, description) in enumerate(group, start=1):
                    school_key = normalize_school_name(school_name)
                    yymmdd = datetime.strptime(mmddyy, "%m%d%y").strftime("%y%m%d")
                    event_key = (yymmdd, school_key) if school_key else None
                    if event_key and event_key in existing_db_event_keys:
                        print(
                            "[SKIP] Event already exists in DB (date+school): "
                            f"{yymmdd} {school_name.strip()}"
                        )
                        continue

                    raw_id = mmddyy + str(i)
                    numeric_id = int(raw_id) * 7
                    base_picture_id = "P" + str(numeric_id).zfill(8)
                    picture_id = (
                        _ensure_unique_pid(base_picture_id, existing_db_pids)
                        if db is not None
                        else base_picture_id
                    )
                    if picture_id != base_picture_id:
                        print(f"[INFO] PID conflict in DB: {base_picture_id} -> {picture_id}")

                    new_folder_name = yymmdd + " " + school_name.strip() + " " + str(picture_id)
                    folder_path = os.path.join(base_path, new_folder_name)

                    if impossible_char(new_folder_name):
                        faulty_folder_paths.append(new_folder_name)
                        continue

                    os.makedirs(os.path.join(folder_path, picture_id), exist_ok=True)

                    requests_text = os.path.join(folder_path, f"{new_folder_name}.txt")
                    clean_description = strip_html_for_text(description)
                    note_text = f"Description:\n{clean_description}\n"
                    with open(requests_text, "w", encoding="utf-8") as file:
                        file.write(note_text)

                    if db is not None:
                        try:
                            _sync_db_note_for_folder(db, new_folder_name, note_text)
                            if event_key:
                                existing_db_event_keys.add(event_key)
                        except Exception as exc:
                            print(f"[WARN] Could not sync DB note for {new_folder_name}: {exc}")

        # Run cancellation/reschedule prompt after import creation is complete.
        if not args.skip_cancellation:
            print("[INFO] Running cancellation checks...")
            handle_missing_school_actions(
                base_path,
                calendar_event_keys,
                db=db,
                source_base_path=cancellation_source_dir,
            )
            print("[INFO] Cancellation checks done.")
        else:
            print("[INFO] Cancellation checks skipped (--skip-cancellation).")

        if faulty_folder_paths:
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo("Faulty Character", "Check:\n" + "\n".join(faulty_folder_paths))

        if db is not None:
            print("[INFO] DB-first mode: import/cancellation update DB directly (no folder->DB sync).")

    except HttpError as error:
        print("An error occurred: %s" % error)


if __name__ == "__main__":
    main()
