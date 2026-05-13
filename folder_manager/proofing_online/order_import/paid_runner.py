from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from googleapiclient.errors import HttpError

from folder_manager.config import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from folder_manager.db import DB, WORKFLOW_DOMAIN_PROOFING

from .config import ORDER_SOURCES
from .file_manager import (
    copy_paid_order_assets_to_orders,
    ensure_proofing_paid_order_folders,
    find_matching_subdir,
    find_orders_subdir,
)
from .gmail_client import (
    extract_picture_day_ids,
    get_gmail_service,
    get_header_value,
    get_html_from_payload,
    list_label_ids_by_name,
)
from .parsers import extract_class_number, extract_order_number, extract_photo_identifiers, sanitize_filename
from .pdf_utils import get_pdfkit_config
from .processing import (
    _class_descriptor_from_identifiers,
    _identifier_signature,
    _is_missing_class,
    _prepare_identifier_metadata,
)

EXIT_CODE_CANCELLED = 41
SUMMARY_MARKER = "__PHOTODECK_IMPORT_SUMMARY__"
SUBJECT_FILTER = "Your payment receipt"
ORDER_IMPORT_SOURCE = "photodeck_paid"
PICTURE_DAY_ID_RE = re.compile(r"\b([PH]\d{7,8})\b", re.IGNORECASE)


@dataclass(frozen=True)
class MessageSortRow:
    message_id: str
    internal_date_ms: int


@dataclass(frozen=True)
class JobCandidate:
    item_id: int
    disk_name: str
    folder_path: str
    pid: str
    stage: int
    proof_source_path: str = ""


@dataclass(frozen=True)
class StageRestore:
    item_id: int
    previous_stage: int


@dataclass
class AppliedAction:
    message_id: str
    pid: str
    order_no: str
    copied_paths: List[str]
    copied_assets: List[dict]
    stage_restores: List[StageRestore]
    label_moved: bool
    order_history_recorded: bool
    touched_disk_names: List[str]


class PaidOrderCancelled(RuntimeError):
    pass


def _env_int(name: str, default: int = 0) -> int:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return int(default)
    try:
        return max(0, int(raw))
    except Exception:
        return int(default)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import PhotoDeck paid orders directly into proofing Edit.")
    parser.add_argument("--photodeck-paid-import", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--token-path", default=None, help="Override Gmail OAuth token path.")
    parser.add_argument("--credentials-path", default=None, help="Override Gmail OAuth credentials path.")
    parser.add_argument("--cancel-token-path", default=None, help="When this file appears, rollback this run and exit.")
    parser.add_argument("--upload-source-manifest", default=None, help="Stage 3 upload source manifest path.")
    parser.add_argument("--no-label-update", action="store_true", help="Do not modify Gmail labels.")
    parser.add_argument(
        "--max-messages",
        type=int,
        default=_env_int("DAMY_PHOTODECK_PAID_MAX_MESSAGES", 0),
        help="Optional cap for oldest matching messages to process.",
    )
    parser.add_argument(
        "--label-window",
        type=int,
        default=_env_int("DAMY_PHOTODECK_PAID_LABEL_WINDOW", 0),
        help="Only fetch first N matching label messages before sorting. 0 means all.",
    )
    parser.add_argument(
        "--fetch-workers",
        type=int,
        default=_env_int("DAMY_PHOTODECK_PAID_FETCH_WORKERS", 4),
        help="Number of workers for Gmail full-message fetch and local PID parsing.",
    )
    return parser.parse_args(argv)


def _log(message: str) -> None:
    print(str(message), flush=True)


def _build_cancel_checker(cancel_token_path: Optional[str]):
    token_file: Optional[Path] = None
    raw = str(cancel_token_path or "").strip()
    if raw:
        token_file = Path(raw).expanduser().resolve()

    def _is_cancelled() -> bool:
        return bool(token_file and token_file.exists())

    return _is_cancelled, token_file


def _to_int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _build_db_client() -> DB:
    host = os.environ.get("DAMY_DB_HOST", str(DB_HOST or "192.168.1.206"))
    name = os.environ.get("DAMY_DB_NAME", str(DB_NAME or "damy_workflow"))
    user = os.environ.get("DAMY_DB_USER", str(DB_USER or "damy_app"))
    password = os.environ.get("DAMY_DB_PASS", str(DB_PASS or "2357"))
    port = int(os.environ.get("DAMY_DB_PORT", str(DB_PORT or 5432)))
    return DB(host=host, dbname=name, user=user, password=password, port=port)


def _list_message_ids_for_label(
    service,
    label_id: str,
    *,
    gmail_query: str = "",
    label_window: int = 0,
) -> List[str]:
    results: List[str] = []
    page_token = None
    limit = max(0, int(label_window or 0))
    while True:
        page_size = 500
        if limit > 0:
            remaining = limit - len(results)
            if remaining <= 0:
                break
            page_size = max(1, min(500, remaining))
        kwargs = {
            "userId": "me",
            "labelIds": [label_id],
            "maxResults": page_size,
            "includeSpamTrash": False,
            "fields": "messages/id,nextPageToken,resultSizeEstimate",
        }
        if gmail_query:
            kwargs["q"] = gmail_query
        if page_token:
            kwargs["pageToken"] = page_token
        response = service.users().messages().list(**kwargs).execute()
        for message in response.get("messages", []):
            msg_id = str(message.get("id", "")).strip()
            if msg_id:
                results.append(msg_id)
                if limit > 0 and len(results) >= limit:
                    break
        if limit > 0 and len(results) >= limit:
            break
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return results


def _select_active_receipt_messages(
    service,
    message_ids: Sequence[str],
    job_map: Dict[str, List[JobCandidate]],
    *,
    fetch_workers: int = 4,
) -> Tuple[List[dict], Dict[str, int]]:
    active_pids = {str(pid or "").strip().upper() for pid in job_map if str(pid or "").strip()}
    stats = {
        "not_receipt_subject": 0,
        "no_picture_day_id": 0,
        "inactive_pid": 0,
        "fetch_failed": 0,
    }
    if not active_pids:
        stats["inactive_pid"] = len(list(message_ids or []))
        return [], stats

    worker_state = threading.local()

    def _worker_service():
        existing = getattr(worker_state, "service", None)
        if existing is None:
            existing = get_gmail_service()
            worker_state.service = existing
        return existing

    def _fetch_and_filter(msg_id: str) -> Tuple[Optional[MessageSortRow], Optional[dict], Optional[str]]:
        client = service if worker_count <= 1 else _worker_service()
        message = client.users().messages().get(
            userId="me",
            id=msg_id,
            format="full",
            fields="id,internalDate,payload",
        ).execute()
        subject = get_header_value(message, "Subject") or ""
        if SUBJECT_FILTER.lower() not in subject.lower():
            return None, None, "not_receipt_subject"

        pids = {str(pid or "").strip().upper() for pid in extract_picture_day_ids(message)}
        if not pids:
            return None, None, "no_picture_day_id"
        if not (pids & active_pids):
            return None, None, "inactive_pid"

        return (
            MessageSortRow(
                message_id=str(msg_id),
                internal_date_ms=_to_int(message.get("internalDate"), 0),
            ),
            message,
            None,
        )

    selected: List[Tuple[MessageSortRow, dict]] = []
    worker_count = max(1, min(8, int(fetch_workers or 1)))
    if worker_count <= 1 or len(message_ids) <= 1:
        for msg_id in message_ids:
            try:
                row, message, skip_reason = _fetch_and_filter(str(msg_id))
            except HttpError:
                raise
            except Exception:
                stats["fetch_failed"] += 1
                continue
            if skip_reason:
                stats[skip_reason] += 1
                continue
            if row is not None and message is not None:
                selected.append((row, message))
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(_fetch_and_filter, str(msg_id)): str(msg_id)
                for msg_id in message_ids
            }
            for future in as_completed(future_map):
                try:
                    row, message, skip_reason = future.result()
                except HttpError:
                    raise
                except Exception:
                    stats["fetch_failed"] += 1
                    continue
                if skip_reason:
                    stats[skip_reason] += 1
                    continue
                if row is not None and message is not None:
                    selected.append((row, message))

    selected.sort(key=lambda row: row[0].internal_date_ms)
    return [message for _, message in selected], stats


def _resolve_nested_stage_folder_path(root_dir: str, stage_folder_name: str, disk_name: str) -> Optional[str]:
    root = str(root_dir or "").strip()
    target_name = str(disk_name or "").strip()
    if not root or not target_name or not os.path.isdir(root):
        return None

    direct_stage = find_matching_subdir(root, stage_folder_name)
    if os.path.isdir(direct_stage):
        candidate = find_matching_subdir(direct_stage, target_name)
        if os.path.isdir(candidate):
            return candidate

    for entry in os.listdir(root):
        entry_path = os.path.join(root, entry)
        if not os.path.isdir(entry_path):
            continue
        stage_path = find_matching_subdir(entry_path, stage_folder_name)
        if not os.path.isdir(stage_path):
            continue
        candidate = find_matching_subdir(stage_path, target_name)
        if os.path.isdir(candidate):
            return candidate
    return None


def _resolve_existing_folder_path(base_dir: str, disk_name: str, source_base_dir: str = "") -> Optional[str]:
    base_path = os.path.join(base_dir, disk_name)
    if os.path.isdir(base_path):
        return base_path
    cancel_path = os.path.join(base_dir, "cancel", disk_name)
    if os.path.isdir(cancel_path):
        return cancel_path
    nested_edit = _resolve_nested_stage_folder_path(base_dir, "3. Edit", disk_name)
    if nested_edit:
        return nested_edit
    cancel_nested_edit = _resolve_nested_stage_folder_path(os.path.join(base_dir, "cancel"), "3. Edit", disk_name)
    if cancel_nested_edit:
        return cancel_nested_edit
    source_dir = str(source_base_dir or "").strip()
    if not source_dir:
        return None
    source_path = os.path.join(source_dir, disk_name)
    if os.path.isdir(source_path):
        return source_path
    source_cancel = os.path.join(source_dir, "cancel", disk_name)
    if os.path.isdir(source_cancel):
        return source_cancel
    source_nested = _resolve_nested_stage_folder_path(source_dir, "3. Edit", disk_name)
    if source_nested:
        return source_nested
    source_cancel_nested = _resolve_nested_stage_folder_path(os.path.join(source_dir, "cancel"), "3. Edit", disk_name)
    if source_cancel_nested:
        return source_cancel_nested
    return None


def _load_upload_source_index(manifest_path: str = "") -> Dict[Tuple[str, str], str]:
    path = str(manifest_path or "").strip()
    if not path or not os.path.isfile(path):
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}

    index: Dict[Tuple[str, str], str] = {}
    for item in list(raw or []):
        disk_name = str((item or {}).get("disk_name") or "").strip().lower()
        picture_day_id = str((item or {}).get("picture_day_id") or "").strip().upper()
        source_path = str((item or {}).get("path") or "").strip()
        if not disk_name or not source_path or not os.path.isdir(source_path):
            continue
        index[(disk_name, picture_day_id)] = source_path
        index.setdefault((disk_name, ""), source_path)
    return index


def _upload_source_for_job(
    upload_sources: Dict[Tuple[str, str], str],
    disk_name: str,
    pid: str,
) -> str:
    key_name = str(disk_name or "").strip().lower()
    key_pid = str(pid or "").strip().upper()
    return upload_sources.get((key_name, key_pid), "") or upload_sources.get((key_name, ""), "")


def _build_job_index(db: DB, upload_sources: Optional[Dict[Tuple[str, str], str]] = None) -> Dict[str, List[JobCandidate]]:
    base_dir = str(db.get_base_dir(fallback=str(Path.cwd())) or "").strip()
    source_base_dir = str(os.environ.get("DAMY_SOURCE_BASE_DIR") or "").strip()
    rows = db.list_by_domain_stage(WORKFLOW_DOMAIN_PROOFING, 5)
    mapping: Dict[str, List[JobCandidate]] = {}
    source_index = upload_sources or {}
    for row in rows:
        disk_name = str(getattr(row, "disk_name", "") or "").strip()
        if not disk_name:
            continue
        row_pid = str(getattr(row, "pid", "") or "").strip().upper()
        if row_pid and PICTURE_DAY_ID_RE.fullmatch(row_pid):
            pid_values: set[str] = {row_pid}
        else:
            pid_values = {match.upper() for match in PICTURE_DAY_ID_RE.findall(disk_name)}
        if not pid_values:
            continue
        folder_path = _resolve_existing_folder_path(base_dir, disk_name, source_base_dir)
        if not folder_path:
            continue
        for pid in sorted(pid_values):
            mapping.setdefault(pid, []).append(
                JobCandidate(
                    item_id=int(row.id),
                    disk_name=disk_name,
                    folder_path=folder_path,
                    pid=pid,
                    stage=int(getattr(row, "stage", 0) or 0),
                    proof_source_path=_upload_source_for_job(source_index, disk_name, pid),
                )
            )
    return mapping


def _resolve_pid_for_message(message: dict, job_map: Dict[str, List[JobCandidate]]) -> Tuple[Optional[str], str]:
    picture_day_ids = extract_picture_day_ids(message)
    if not picture_day_ids:
        return None, "No Picture Day ID found in email; kept in source label."
    if len(picture_day_ids) == 1:
        return picture_day_ids[0], ""
    matching = [pid for pid in picture_day_ids if pid in job_map]
    if len(matching) == 1:
        return matching[0], ""
    return None, f"Multiple Picture Day IDs found in email ({', '.join(sorted(picture_day_ids))}); kept in source label."


def _prepare_identifiers_for_message(html_content: str, picture_day_id: str, subject: str, order_no: str) -> List[dict]:
    class_number = extract_class_number(html_content)
    identifiers = extract_photo_identifiers(html_content, picture_day_id)
    for identifier in identifiers:
        identifier["order_number"] = order_no
        _prepare_identifier_metadata(identifier, class_number, subject)
    if identifiers:
        class_override = _class_descriptor_from_identifiers(identifiers)
        if class_override and _is_missing_class(class_number):
            class_number = class_override
    _ = class_number

    unique_identifiers: List[dict] = []
    seen_signatures = set()
    for identifier in identifiers:
        signature = _identifier_signature(identifier)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        unique_identifiers.append(identifier)
    return unique_identifiers


def _render_order_pdf_to_orders(
    html_content: str,
    *,
    job_folder: str,
    order_no: str,
) -> Tuple[str, bool]:
    orders_folder = find_orders_subdir(job_folder)
    pdf_folder = find_matching_subdir(orders_folder, "Order PDFS")
    os.makedirs(pdf_folder, exist_ok=True)
    filename = sanitize_filename(f"{order_no} Order") + ".pdf"
    pdf_path = os.path.join(pdf_folder, filename)
    if os.path.exists(pdf_path):
        _log(f"[PDF] Reused existing {pdf_path}")
        return pdf_path, False

    try:
        import pdfkit  # type: ignore
    except Exception as exc:
        raise ModuleNotFoundError(
            "Missing PDF component: pdfkit is not installed."
        ) from exc

    pdfkit.from_string(html_content, pdf_path, configuration=get_pdfkit_config())
    _log(f"[PDF] Saved order PDF to {pdf_path}")
    return pdf_path, True


def _failure_detail_for_exception(exc: Exception) -> Dict[str, str]:
    message = str(exc or "").strip()
    lower = message.lower()
    if isinstance(exc, ModuleNotFoundError) and "pdfkit" in lower:
        return {
            "reason": "Missing PDF component",
            "detail": "The app could not create the Order PDF because pdfkit is missing.",
            "next_step": "Install/update requirements, rebuild the Proofing EXE, then run Import Paid Orders again.",
        }
    if "wkhtmltopdf executable not found" in lower:
        return {
            "reason": "Missing PDF renderer",
            "detail": message,
            "next_step": "Install wkhtmltopdf at the configured path or update WKHTMLTOPDF_PATH, then run Import Paid Orders again.",
        }
    return {
        "reason": "Unexpected error",
        "detail": message or exc.__class__.__name__,
        "next_step": "Check the log details, fix the problem, then run Import Paid Orders again.",
    }


def _move_message_to_imported_label(
    service,
    *,
    message_id: str,
    source_label_id: str,
    imported_label_id: str,
    no_label_update: bool,
) -> bool:
    if no_label_update:
        _log(f"[LABEL] id={message_id} no_label_update=True (unchanged)")
        return False
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": [imported_label_id], "removeLabelIds": [source_label_id]},
    ).execute()
    _log(
        f"[LABEL] id={message_id} removed_source_label={source_label_id} added_imported_label={imported_label_id}"
    )
    return True


def _rollback_applied_actions(
    actions: List[AppliedAction],
    *,
    service,
    source_label_id: str,
    imported_label_id: str,
    no_label_update: bool,
    db: DB,
) -> List[str]:
    errors: List[str] = []
    for action in reversed(actions):
        if not no_label_update and action.label_moved and imported_label_id:
            try:
                service.users().messages().modify(
                    userId="me",
                    id=action.message_id,
                    body={"addLabelIds": [source_label_id], "removeLabelIds": [imported_label_id]},
                ).execute()
                _log(f"[ROLLBACK] label restored for id={action.message_id}")
            except Exception as exc:
                errors.append(f"{action.message_id}: label rollback failed: {exc}")
        if action.order_history_recorded:
            try:
                db.delete_order_import_record(action.pid, action.order_no, source=ORDER_IMPORT_SOURCE)
            except Exception as exc:
                errors.append(f"{action.message_id}: order history rollback failed: {exc}")
        else:
            try:
                db.delete_proofing_paid_assets_for_order(action.pid, action.order_no)
            except Exception as exc:
                errors.append(f"{action.message_id}: paid asset rollback failed: {exc}")
        for stage_restore in reversed(action.stage_restores):
            try:
                db.update_domain_stage(
                    stage_restore.item_id,
                    domain=WORKFLOW_DOMAIN_PROOFING,
                    stage=stage_restore.previous_stage,
                )
            except Exception as exc:
                errors.append(f"{action.message_id}: stage rollback failed for item {stage_restore.item_id}: {exc}")
        for copied_path in reversed(action.copied_paths):
            try:
                if os.path.isfile(copied_path):
                    os.remove(copied_path)
            except Exception as exc:
                errors.append(f"{action.message_id}: file rollback failed for {copied_path}: {exc}")
    return errors


def _process_one_message(
    service,
    msg_id: str,
    job_map: Dict[str, List[JobCandidate]],
    source_label_id: str,
    imported_label_id: str,
    *,
    no_label_update: bool,
    is_cancelled,
    db: DB,
    message: Optional[dict] = None,
) -> Tuple[str, str, Optional[AppliedAction]]:
    if callable(is_cancelled) and bool(is_cancelled()):
        raise PaidOrderCancelled("PhotoDeck paid-order import cancelled by user request.")

    if message is None:
        message = service.users().messages().get(
            userId="me",
            id=msg_id,
            format="full",
            fields="id,internalDate,labelIds,payload",
        ).execute()
    payload = message.get("payload", {}) or {}
    subject = get_header_value(message, "Subject") or ""
    from_header = get_header_value(message, "From") or ""
    header_date = get_header_value(message, "Date") or ""
    subject_lower = subject.lower()
    if SUBJECT_FILTER.lower() not in subject_lower:
        return "skipped", f"{msg_id}: subject mismatch; kept in source label.", None

    html_content = get_html_from_payload(payload)
    if not html_content:
        raise RuntimeError(f"{msg_id}: HTML order content was not found in the email.")

    pid, pid_skip_reason = _resolve_pid_for_message(message, job_map)
    if not pid:
        return "skipped", f"{msg_id}: {pid_skip_reason}", None

    order_no = str(extract_order_number(html_content) or "").strip()
    if not order_no:
        return "skipped", f"{msg_id}: order number not found; kept in source label.", None

    _log(f"[PARSE] id={msg_id} pid={pid} order_no={order_no} subject={subject!r} from={from_header!r} date={header_date!r}")

    if db.order_import_exists(pid, order_no, source=ORDER_IMPORT_SOURCE):
        label_moved = _move_message_to_imported_label(
            service,
            message_id=msg_id,
            source_label_id=source_label_id,
            imported_label_id=imported_label_id,
            no_label_update=no_label_update,
        )
        action = AppliedAction(
            message_id=msg_id,
            pid=pid,
            order_no=order_no,
            copied_paths=[],
            copied_assets=[],
            stage_restores=[],
            label_moved=label_moved,
            order_history_recorded=False,
            touched_disk_names=[],
        )
        return "duplicate", f"{msg_id}: duplicate {pid} {order_no}; moved to imported label.", action

    candidates = list(job_map.get(pid, []))
    if not candidates:
        return "skipped", f"{msg_id}: no existing proofing job matched PID {pid}; kept in source label.", None
    if len(candidates) > 1:
        names = ", ".join(sorted(candidate.disk_name for candidate in candidates))
        return "skipped", f"{msg_id}: multiple proofing jobs matched PID {pid} ({names}); kept in source label.", None
    candidate = candidates[0]

    identifiers = _prepare_identifiers_for_message(html_content, pid, subject, order_no)
    if not identifiers:
        return "skipped", f"{msg_id}: no photo identifiers found for PID {pid}; kept in source label.", None

    copied_assets: List[dict] = []
    copied_paths: List[str] = []
    stage_restores: List[StageRestore] = []
    order_history_recorded = False
    label_moved = False
    try:
        ensure_proofing_paid_order_folders(candidate.folder_path)
        order_pdf_path, pdf_created = _render_order_pdf_to_orders(
            html_content,
            job_folder=candidate.folder_path,
            order_no=order_no,
        )
        if pdf_created:
            copied_paths.append(order_pdf_path)

        copied_assets = copy_paid_order_assets_to_orders(
            candidate.folder_path,
            identifiers,
            pid,
            order_no,
            progress_callback=_log,
            proofs_folder_override=candidate.proof_source_path,
            order_pdf_path=order_pdf_path,
        )
        if not copied_assets:
            return "skipped", f"{msg_id}: no edit assets were copied for PID {pid}; kept in source label.", None
        copied_paths.extend(
            str(entry.get("path") or "").strip()
            for entry in copied_assets
            if bool(entry.get("created")) and str(entry.get("path") or "").strip()
        )
        enriched_assets: List[dict] = []
        for entry in copied_assets:
            path = str(entry.get("path") or "").strip()
            label = str(entry.get("label") or os.path.basename(path) or path).strip()
            enriched_assets.append(
                {
                    **entry,
                    "workflow_item_id": candidate.item_id,
                    "path": path,
                    "label": label,
                    "disk_name": candidate.disk_name,
                    "pid": pid,
                    "message_id": msg_id,
                    "order_no": order_no,
                    "asset_status": "stage6",
                }
            )
        copied_assets = enriched_assets
        db.record_proofing_paid_order_import(
            copied_assets,
            source=ORDER_IMPORT_SOURCE,
            pid=pid,
            order_no=order_no,
            message_id=msg_id,
            item_id=candidate.item_id,
        )
        order_history_recorded = True

        if callable(is_cancelled) and bool(is_cancelled()):
            raise PaidOrderCancelled("PhotoDeck paid-order import cancelled by user request.")

        label_moved = _move_message_to_imported_label(
            service,
            message_id=msg_id,
            source_label_id=source_label_id,
            imported_label_id=imported_label_id,
            no_label_update=no_label_update,
        )
        action = AppliedAction(
            message_id=msg_id,
            pid=pid,
            order_no=order_no,
            copied_paths=list(copied_paths),
            copied_assets=list(copied_assets),
            stage_restores=list(stage_restores),
            label_moved=label_moved,
            order_history_recorded=order_history_recorded,
            touched_disk_names=[candidate.disk_name],
        )
        return "imported", f"{msg_id}: imported {len(copied_assets)} asset(s) into {candidate.disk_name}.", action
    except PaidOrderCancelled:
        for copied_path in reversed(copied_paths):
            try:
                if os.path.isfile(copied_path):
                    os.remove(copied_path)
            except OSError:
                pass
        for stage_restore in reversed(stage_restores):
            try:
                db.update_domain_stage(
                    stage_restore.item_id,
                    domain=WORKFLOW_DOMAIN_PROOFING,
                    stage=stage_restore.previous_stage,
                )
            except Exception as exc:
                _log(f"[ROLLBACK-WARN] item_id={stage_restore.item_id} stage restore failed: {exc}")
        if order_history_recorded:
            try:
                db.delete_order_import_record(pid, order_no, source=ORDER_IMPORT_SOURCE)
            except Exception as exc:
                _log(f"[ROLLBACK-WARN] pid={pid} order_no={order_no} history delete failed: {exc}")
        else:
            try:
                db.delete_proofing_paid_assets_for_order(pid, order_no)
            except Exception as exc:
                _log(f"[ROLLBACK-WARN] pid={pid} order_no={order_no} paid asset delete failed: {exc}")
        raise
    except Exception:
        for copied_path in reversed(copied_paths):
            try:
                if os.path.isfile(copied_path):
                    os.remove(copied_path)
            except OSError:
                pass
        for stage_restore in reversed(stage_restores):
            try:
                db.update_domain_stage(
                    stage_restore.item_id,
                    domain=WORKFLOW_DOMAIN_PROOFING,
                    stage=stage_restore.previous_stage,
                )
            except Exception as exc:
                _log(f"[ROLLBACK-WARN] item_id={stage_restore.item_id} stage restore failed: {exc}")
        if order_history_recorded:
            try:
                db.delete_order_import_record(pid, order_no, source=ORDER_IMPORT_SOURCE)
            except Exception as exc:
                _log(f"[ROLLBACK-WARN] pid={pid} order_no={order_no} history delete failed: {exc}")
        else:
            try:
                db.delete_proofing_paid_assets_for_order(pid, order_no)
            except Exception as exc:
                _log(f"[ROLLBACK-WARN] pid={pid} order_no={order_no} paid asset delete failed: {exc}")
        raise


def run(args: argparse.Namespace) -> int:
    run_started = time.perf_counter()
    if args.token_path:
        os.environ["DAMY_ORDER_IMPORT_TOKEN_PATH"] = str(args.token_path)
    if args.credentials_path:
        os.environ["DAMY_ORDER_IMPORT_CREDENTIALS_PATH"] = str(args.credentials_path)

    setup_started = time.perf_counter()
    db = _build_db_client()
    service = get_gmail_service()
    upload_sources = _load_upload_source_index(args.upload_source_manifest)
    job_map = _build_job_index(db, upload_sources)
    _log(
        f"[TIMING] setup={time.perf_counter() - setup_started:.2f}s "
        f"active_pids={len(job_map)}"
    )

    source_settings = ORDER_SOURCES["photodeck"]
    source_label_name = str(source_settings.get("gmail_label") or "").strip()
    imported_label_name = str(source_settings.get("gmail_imported_label") or "").strip()
    if not source_label_name:
        raise RuntimeError("PhotoDeck paid-order Gmail label is not configured.")

    label_started = time.perf_counter()
    label_map = list_label_ids_by_name(service)
    _log(f"[TIMING] gmail_labels={time.perf_counter() - label_started:.2f}s")
    source_label_id = label_map.get(source_label_name)
    if not source_label_id:
        raise RuntimeError(f"Gmail label not found: {source_label_name}")

    imported_label_id = ""
    if imported_label_name:
        imported_label_id = label_map.get(imported_label_name) or ""
        if not args.no_label_update and not imported_label_id:
            response = service.users().labels().create(
                userId="me",
                body={
                    "name": imported_label_name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            ).execute()
            imported_label_id = str(response.get("id") or "").strip()
        if not args.no_label_update and not imported_label_id:
            raise RuntimeError(f"Gmail label not found: {imported_label_name}")

    is_cancelled, cancel_token_file = _build_cancel_checker(args.cancel_token_path)
    if cancel_token_file:
        _log(f"[INFO] Cancel token path: {cancel_token_file}")

    list_started = time.perf_counter()
    gmail_query = SUBJECT_FILTER
    _log(f"[INFO] Gmail query: {gmail_query!r} in label {source_label_name!r}")
    message_ids = _list_message_ids_for_label(
        service,
        source_label_id,
        gmail_query=gmail_query,
        label_window=args.label_window,
    )
    _log(
        f"[INFO] Receipt messages fetched for local parse: {len(message_ids)} "
        f"(label_window={max(0, int(args.label_window or 0))})"
    )
    _log(f"[TIMING] gmail_list={time.perf_counter() - list_started:.2f}s")
    parse_started = time.perf_counter()
    fetch_workers = max(1, min(8, int(args.fetch_workers or 1)))
    _log(f"[INFO] Gmail full-message fetch workers: {fetch_workers}")
    ordered_messages, local_filter_stats = _select_active_receipt_messages(
        service,
        message_ids,
        job_map,
        fetch_workers=fetch_workers,
    )
    if args.max_messages > 0:
        ordered_messages = ordered_messages[: args.max_messages]
    _log(
        f"[INFO] Active receipt messages queued after local parse: {len(ordered_messages)} "
        f"(ignored_not_receipt={local_filter_stats['not_receipt_subject']} "
        f"ignored_no_pid={local_filter_stats['no_picture_day_id']} "
        f"ignored_inactive_pid={local_filter_stats['inactive_pid']} "
        f"fetch_failed={local_filter_stats['fetch_failed']})"
    )
    _log(
        f"[TIMING] gmail_local_parse={time.perf_counter() - parse_started:.2f}s "
        f"queued={len(ordered_messages)} max_messages={max(0, int(args.max_messages or 0))}"
    )

    summary = {
        "total_in_label": len(message_ids),
        "queued": len(ordered_messages),
        "ignored_not_receipt": int(local_filter_stats.get("not_receipt_subject", 0)),
        "ignored_no_pid": int(local_filter_stats.get("no_picture_day_id", 0)),
        "ignored_inactive_pid": int(local_filter_stats.get("inactive_pid", 0)),
        "candidate_fetch_failed": int(local_filter_stats.get("fetch_failed", 0)),
        "processed_ok": 0,
        "processed_failed": 0,
        "duplicates_skipped": 0,
        "skipped_kept": 0,
        "cancelled": False,
        "rollback_applied": 0,
        "rollback_errors": [],
        "label_updates": 0,
        "no_label_update": bool(args.no_label_update),
        "touched_folders": [],
        "copied_assets": [],
        "updated_item_ids": [],
        "errors": [],
        "failure_details": [],
        "skipped_details": [],
    }

    touched_folders: set[str] = set()
    copied_assets: List[dict] = []
    updated_item_ids: set[int] = set()
    applied_actions: List[AppliedAction] = []
    was_cancelled = False

    if not ordered_messages:
        _log("[INFO] No matching receipt messages found under target label.")
        _log(SUMMARY_MARKER + json.dumps(summary, ensure_ascii=False))
        return 0

    for idx, message in enumerate(ordered_messages, start=1):
        msg_id = str((message or {}).get("id") or "").strip()
        _log(f"[INFO] Processing {idx}/{len(ordered_messages)} message={msg_id}")
        if is_cancelled():
            was_cancelled = True
            _log("[WARN] Cancel token detected. Rolling back this run.")
            break
        try:
            status, detail, action = _process_one_message(
                service,
                msg_id,
                job_map,
                source_label_id,
                imported_label_id,
                no_label_update=bool(args.no_label_update),
                is_cancelled=is_cancelled,
                db=db,
                message=message,
            )
            if status == "imported" and action is not None:
                applied_actions.append(action)
                summary["processed_ok"] += 1
                if action.label_moved:
                    summary["label_updates"] += 1
                touched_folders.update(action.touched_disk_names)
                copied_assets.extend(action.copied_assets)
                updated_item_ids.update(stage_restore.item_id for stage_restore in action.stage_restores)
                _log(f"[OK] {detail}")
            elif status == "duplicate" and action is not None:
                applied_actions.append(action)
                summary["duplicates_skipped"] += 1
                if action.label_moved:
                    summary["label_updates"] += 1
                _log(f"[OK] {detail}")
            else:
                summary["skipped_kept"] += 1
                summary["skipped_details"].append(detail)
                _log(f"[SKIP] {detail}")
        except PaidOrderCancelled:
            was_cancelled = True
            _log("[WARN] PhotoDeck paid-order import cancelled by user request. Rolling back this run.")
            break
        except HttpError as exc:
            summary["processed_failed"] += 1
            err = f"{msg_id}: Gmail API error {exc}"
            summary["errors"].append(err)
            summary["failure_details"].append(
                {
                    "message_id": msg_id,
                    "reason": "Gmail API error",
                    "detail": str(exc),
                }
            )
            _log(f"[ERROR] {err}")
        except Exception as exc:
            summary["processed_failed"] += 1
            err = f"{msg_id}: {exc}"
            summary["errors"].append(err)
            failure_detail = _failure_detail_for_exception(exc)
            summary["failure_details"].append(
                {
                    "message_id": msg_id,
                    "reason": failure_detail["reason"],
                    "detail": failure_detail["detail"],
                    "next_step": failure_detail["next_step"],
                }
            )
            _log(f"[ERROR] {err}")

    if was_cancelled:
        rollback_errors = _rollback_applied_actions(
            applied_actions,
            service=service,
            source_label_id=source_label_id,
            imported_label_id=imported_label_id,
            no_label_update=bool(args.no_label_update),
            db=db,
        )
        summary["cancelled"] = True
        summary["rollback_applied"] = len(applied_actions)
        summary["rollback_errors"] = list(rollback_errors)
        summary["processed_ok"] = 0
        summary["duplicates_skipped"] = 0
        summary["label_updates"] = 0
        summary["touched_folders"] = []
        summary["copied_assets"] = []
        summary["updated_item_ids"] = []
    else:
        summary["touched_folders"] = sorted(touched_folders)
        summary["copied_assets"] = copied_assets
        summary["updated_item_ids"] = sorted(updated_item_ids)

    _log(
        f"[DONE] ok={summary['processed_ok']} skipped={summary['skipped_kept']} "
        f"errors={summary['processed_failed']} duplicates={summary['duplicates_skipped']} "
        f"moved={summary['label_updates']} cancelled={summary['cancelled']} "
        f"elapsed={time.perf_counter() - run_started:.2f}s"
    )
    _log(SUMMARY_MARKER + json.dumps(summary, ensure_ascii=False))
    return EXIT_CODE_CANCELLED if was_cancelled else 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except PaidOrderCancelled:
        _log("[WARN] PhotoDeck paid-order import cancelled.")
        return EXIT_CODE_CANCELLED
    except Exception as exc:
        _log(f"[FATAL] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
