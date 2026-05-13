import os
import re
import shutil
import subprocess
import sys
import threading
import traceback
import json
from collections import defaultdict
from datetime import datetime
from email.utils import parseaddr
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel,
    QSizePolicy, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QPlainTextEdit, QFileDialog, QInputDialog,
    QProgressBar, QDialog, QFrame, QSplitter,
)
from PySide6.QtCore import Qt, QObject, QThread, Signal, QCoreApplication, QTimer, QSettings
from googleapiclient.errors import HttpError

from folder_manager.proofing_online.order_import.config import MAX_RESULTS_PER_QUERY, ORDER_SOURCES
from folder_manager.proofing_online.order_import.exceptions import NoOrdersFoundError
from folder_manager.proofing_online.order_import.file_manager import (
    find_matching_subdir,
    find_originals_subdir,
    find_proofs_subdir,
    get_pdf_metadata,
)
from folder_manager.proofing_online.order_import.gmail_client import (
    ensure_label_exists,
    extract_picture_day_ids,
    fetch_messages_by_label,
    get_gmail_service,
    get_header_value,
    get_message_debug_preview,
    list_label_ids_by_name,
    modify_message_labels,
    send_email_with_attachments,
)
from folder_manager.proofing_online.order_import.pdf_utils import combine_pdfs
from folder_manager.proofing_online.order_import.processing import process_picture_day
from folder_manager.proofing_online.order_import.utils import emit_status
from folder_manager.config import DB_HOST, DB_NAME, DB_USER, DB_PASS, DB_PORT
from folder_manager.db import DB, WORKFLOW_DOMAIN_YEARBOOK, parse_contact_fields_from_note
from folder_manager.photodeck_upload.workflow import UserCancelled, run_stage3_bulk_upload, run_stage3_create_pdfs
from folder_manager.proof_sorter import sort_folders
from folder_manager.sms.child_info import prepare_child_info_assets
from folder_manager.sms.cloudflare_r2 import missing_r2_settings
from folder_manager.ui.child_mms_dialog import ChildMmsDialog
from folder_manager.ui.cloudflare_dialogs import load_r2_settings
from folder_manager.ui.drag_list_widget import ContactEditorDialog
from ..widgets.drag_list_widget import DragListWidget, ROLE_DB_ID

DAYS = [
    "1. Proofs", "2. Sort", "3. Upload & PDFs",
    "4. School / Parent Delivery", "5. Finished", "6. Order Import", "7. Edit",
    "8. Print", "9. Package", "10. Deliver"
]
DAY_TO_STAGE = {day: idx + 1 for idx, day in enumerate(DAYS)}

DAY_PREFIX_ALIASES = {
    "2. Sort": ("2. Sort", "2. Photodeck Upload"),
    "3. Upload & PDFs": (
        "3. Upload & PDFs",
        "3. Photodeck Upload/PDF Packets",
        "3. Photodeck Upload",
        "3. PDF Packets",
        "3. Upload/PDF Packets",
    ),
}

PICTURE_DAY_ID_RE = re.compile(r'\b[PH]\d{7,8}\b', re.IGNORECASE)
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp', '.gif'}
ROLE_FLAG_I = Qt.UserRole + 2
ROLE_FLAG_G = Qt.UserRole + 3


def _normalize_token(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', value.lower())


def _sanitize_folder_name(value: str) -> str:
    sanitized = re.sub(r'[\\/:*?"<>|]', '_', value.strip())
    return sanitized or 'Folder'


def _stage2_proof_output_name(disk_name: str) -> str:
    name = str(disk_name or "").strip()
    pid_match = PICTURE_DAY_ID_RE.search(name)
    if not pid_match:
        return _sanitize_folder_name(f"{name} Proof")

    pid = pid_match.group(0).upper()
    prefix = name[:pid_match.start()].strip()
    date_part = ""
    school_part = prefix
    date_match = re.match(r"^(\d{6})\s*(.*)$", prefix)
    if date_match:
        date_part = date_match.group(1).strip()
        school_part = date_match.group(2).strip()

    parts = [part for part in (date_part, school_part, "Proof", pid) if part]
    return _sanitize_folder_name(" ".join(parts))


def _stage2_sort_output_names(disk_name: str) -> Set[str]:
    name = str(disk_name or "").strip()
    names = {_stage2_proof_output_name(name), "sorted"}
    pid_match = PICTURE_DAY_ID_RE.search(name)
    if not pid_match:
        names.add(_sanitize_folder_name(f"{name} Proofs"))
        return {item for item in names if item}

    pid = pid_match.group(0).upper()
    prefix = name[:pid_match.start()].strip()
    date_part = ""
    school_part = prefix
    date_match = re.match(r"^(\d{6})\s*(.*)$", prefix)
    if date_match:
        date_part = date_match.group(1).strip()
        school_part = date_match.group(2).strip()

    for label in ("Proof", "Proofs"):
        parts = [part for part in (date_part, school_part, label, pid) if part]
        names.add(_sanitize_folder_name(" ".join(parts)))
    return {item for item in names if item}


def _is_same_or_inside_path(path: str, parent: str) -> bool:
    try:
        child_path = os.path.normcase(os.path.abspath(path))
        parent_path = os.path.normcase(os.path.abspath(parent))
        return child_path == parent_path or os.path.commonpath([child_path, parent_path]) == parent_path
    except Exception:
        return False


def _is_same_path(left: str, right: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))
    except Exception:
        return False


def _remove_stage2_sort_outputs(stage2_folder: str, disk_name: str, *, except_path: str = "") -> List[str]:
    removed: List[str] = []
    for output_name in sorted(_stage2_sort_output_names(disk_name)):
        candidate = os.path.join(stage2_folder, output_name)
        if except_path and _is_same_path(candidate, except_path):
            continue
        if not os.path.isdir(candidate):
            continue
        shutil.rmtree(candidate)
        removed.append(candidate)
    return removed


def _day_prefixes(day: str) -> Tuple[str, ...]:
    return DAY_PREFIX_ALIASES.get(day, (day,))


def _matches_day(folder_name: str, day: str) -> bool:
    return any(prefix in folder_name for prefix in _day_prefixes(day))


def _extract_day_suffix(folder_name: str, day: str) -> str:
    for prefix in _day_prefixes(day):
        if prefix in folder_name:
            return folder_name.split(prefix, 1)[-1].strip()
    return folder_name.strip()


_STAGE_PREFIX_TEXT_RE = re.compile(r"^\s*\d+\s*[\.\-_) ]+\s*(.*)$")


def _strip_stage_prefix_text(value: str) -> str:
    text = str(value or "").strip()
    match = _STAGE_PREFIX_TEXT_RE.match(text)
    if not match:
        return text
    return str(match.group(1) or "").strip() or text


def _stage3_gallery_name_from_paths(work_root: str, folder_path: str, disk_name: str) -> str:
    work_name = os.path.basename(str(work_root or "").rstrip("/\\"))
    if work_name and work_name.lower() != "sorted":
        return _strip_stage_prefix_text(work_name)
    folder_name = os.path.basename(str(folder_path or "").rstrip("/\\"))
    if folder_name:
        return _strip_stage_prefix_text(folder_name)
    return _strip_stage_prefix_text(disk_name)


class SortActionCard(QFrame):
    clicked = Signal()

    def __init__(self, title: str = "Sort"):
        super().__init__()
        self.setObjectName("sortActionCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setFrameShape(QFrame.StyledPanel)
        self.setFrameShadow(QFrame.Raised)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        self.title_label = QLabel(title)
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setStyleSheet("QLabel { font-size: 17px; font-weight: 700; color: #eef3f9; }")

        self.status_label = QLabel("Ready")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("QLabel { font-size: 12px; font-weight: 700; color: #d9e6f7; }")

        self.hint_label = QLabel("Click to run sorter for this folder")
        self.hint_label.setAlignment(Qt.AlignCenter)
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("QLabel { font-size: 11px; color: #9fb0c4; }")

        layout.addWidget(self.title_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.hint_label)

        self.setStyleSheet(
            "QFrame#sortActionCard {"
            " background: #243249;"
            " border: 2px solid #f0a63a;"
            " border-radius: 12px;"
            "}"
            "QFrame#sortActionCard:hover { background: #2c3f5a; }"
            "QFrame#sortActionCard:pressed { background: #1f2d42; }"
        )

    def set_status(self, text: str) -> None:
        self.status_label.setText(str(text or "").strip() or "Ready")

    def set_title(self, text: str) -> None:
        self.title_label.setText(str(text or "").strip() or "Sort")

    def set_hint(self, text: str) -> None:
        self.hint_label.setText(str(text or "").strip() or "Click to run sorter for this folder")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class Stage3PdfAssetCard(QFrame):
    clicked = Signal()
    doubleClicked = Signal()
    clearRequested = Signal()
    fileDropped = Signal(str)

    def __init__(self, title: str = "PDF"):
        super().__init__()
        self.setObjectName("stage3PdfAssetCard")
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self.clicked.emit)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(6)

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 2)
        top_row.setSpacing(0)
        top_row.addStretch(1)

        self.clear_button = QPushButton("x")
        self.clear_button.setObjectName("stage3PdfAssetClearButton")
        self.clear_button.setCursor(Qt.PointingHandCursor)
        self.clear_button.setFixedSize(18, 18)
        self.clear_button.setVisible(False)
        self.clear_button.setToolTip("Clear saved PDF link")
        self.clear_button.clicked.connect(self._emit_clear_requested)
        top_row.addWidget(self.clear_button)
        layout.addLayout(top_row)

        self.title_label = QLabel(str(title or "PDF").strip() or "PDF")
        self.title_label.setObjectName("stage3PdfAssetTitle")
        self.title_label.setAlignment(Qt.AlignCenter)

        self.status_label = QLabel("Not linked")
        self.status_label.setObjectName("stage3PdfAssetStatus")
        self.status_label.setAlignment(Qt.AlignCenter)

        self.hint_label = QLabel("Drop a file/folder here\nor single-click to create\nDouble-click to choose")
        self.hint_label.setObjectName("stage3PdfAssetHint")
        self.hint_label.setAlignment(Qt.AlignCenter)
        self.hint_label.setWordWrap(True)

        self.meta_label = QLabel("")
        self.meta_label.setObjectName("stage3PdfAssetMeta")
        self.meta_label.setAlignment(Qt.AlignCenter)
        self.meta_label.setWordWrap(True)

        layout.addWidget(self.title_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.hint_label)
        layout.addWidget(self.meta_label)
        self._set_visual_state(linked=False)

    def _set_visual_state(self, *, linked: bool, drag_active: bool = False) -> None:
        border_color = "#e59b45" if linked else ("#7aaef0" if drag_active else "#4d5b6b")
        bg_color = "#253042" if linked else "#1b2330"
        if drag_active:
            bg_color = "#2a3950"
        border_style = "solid" if linked else "dashed"
        self.setStyleSheet(
            "QFrame#stage3PdfAssetCard {"
            f"background: {bg_color};"
            f"border: 2px {border_style} {border_color};"
            "border-radius: 12px; }"
            "QLabel#stage3PdfAssetTitle { color: #eef3f9; font-size: 13px; font-weight: 700; border: none; background: transparent; }"
            "QLabel#stage3PdfAssetStatus { color: #d9e6f7; font-size: 12px; font-weight: 600; border: none; background: transparent; }"
            "QLabel#stage3PdfAssetHint { color: #9fb0c4; font-size: 11px; border: none; background: transparent; }"
            "QLabel#stage3PdfAssetMeta { color: #7f93ad; font-size: 10px; border: none; background: transparent; }"
            "QPushButton#stage3PdfAssetClearButton {"
            " min-height: 18px; max-height: 18px; min-width: 18px; max-width: 18px;"
            " margin: 0; padding: 0 0 1px 0; border-radius: 9px;"
            " border: 1px solid #cf6a6a; background: #a84444;"
            " color: #fff6f6; font-size: 10px; font-weight: 700; text-align: center; }"
            "QPushButton#stage3PdfAssetClearButton:hover { background: #c45454; border-color: #e08383; }"
            "QPushButton#stage3PdfAssetClearButton:pressed { background: #8f3838; }"
        )

    def set_asset_state(
        self,
        *,
        linked: bool,
        file_name: str | None = None,
        meta_text: str | None = None,
        status_text: str | None = None,
        hint_text: str | None = None,
    ) -> None:
        self.status_label.setText(status_text or ("Linked" if linked else "Not linked"))
        if hint_text:
            self.hint_label.setText(hint_text)
        elif linked and file_name:
            self.hint_label.setText(f"{file_name}\nSingle-click to open\nDouble-click to choose")
        else:
            self.hint_label.setText("Drop a file/folder here\nor single-click to create\nDouble-click to choose")
        self.meta_label.setText(meta_text or "")
        self.clear_button.setVisible(bool(file_name))
        self._set_visual_state(linked=linked)

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self._click_timer.start(280)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            if self._click_timer.isActive():
                self._click_timer.stop()
            self.doubleClicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def dragEnterEvent(self, event):  # type: ignore[override]
        mime = event.mimeData()
        if mime and mime.hasUrls():
            urls = [u for u in mime.urls() if u.isLocalFile()]
            if urls:
                self._set_visual_state(linked=self.status_label.text() == "Linked", drag_active=True)
                event.acceptProposedAction()
                return
        event.ignore()

    def dragLeaveEvent(self, event):  # type: ignore[override]
        self._set_visual_state(linked=self.status_label.text() == "Linked", drag_active=False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):  # type: ignore[override]
        self._set_visual_state(linked=self.status_label.text() == "Linked", drag_active=False)
        mime = event.mimeData()
        if not mime or not mime.hasUrls():
            event.ignore()
            return
        urls = [u for u in mime.urls() if u.isLocalFile()]
        if not urls:
            event.ignore()
            return
        self.fileDropped.emit(urls[0].toLocalFile())
        event.acceptProposedAction()

    def _emit_clear_requested(self) -> None:
        if self._click_timer.isActive():
            self._click_timer.stop()
        self.clearRequested.emit()


class Stage4SendDialog(QDialog):
    def __init__(
        self,
        parent,
        *,
        to_email: str,
        subject: str,
        body_text: str,
        attachment_paths: List[str],
    ):
        super().__init__(parent)
        self.setWindowTitle("Send School Email")
        self.setModal(True)
        self.resize(880, 640)
        self.setMinimumSize(760, 520)
        self.setStyleSheet(
            "QDialog { background: #2b2b2b; color: #f4f7fb; }"
            "QLabel { color: #f4f7fb; }"
            "QLineEdit, QPlainTextEdit, QListWidget { background: #1f2329; color: #f4f7fb;"
            " border: 1px solid #3a424c; border-radius: 6px; padding: 6px; }"
            "QPushButton { min-height: 36px; padding: 6px 12px; border-radius: 8px;"
            " border: 1px solid #475364; background: #313844; color: #f4f7fb; }"
            "QPushButton:hover { background: #384354; }"
            "QPushButton#primaryAction { background: #2c6ad6; border-color: #4b84e3; font-weight: 700; }"
            "QPushButton#primaryAction:hover { background: #3a78e4; }"
        )

        layout = QVBoxLayout(self)

        title = QLabel("Send School Email")
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        layout.addWidget(title)

        subtitle = QLabel("Review the school email, adjust the message if needed, and send the proof PDFs.")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #c7d5e0;")
        layout.addWidget(subtitle)

        summary = QLabel(f"Ready to send {len(attachment_paths)} PDF file(s) to the school contact.")
        summary.setWordWrap(True)
        summary.setStyleSheet(
            "background-color: #1f2f44; border: 1px solid #335a8a; border-radius: 6px; padding: 8px 10px; color: #d9e8fb;"
        )
        layout.addWidget(summary)

        to_label = QLabel("Recipient email")
        self.to_edit = QLineEdit(self)
        self.to_edit.setText(str(to_email or "").strip())
        layout.addWidget(to_label)
        layout.addWidget(self.to_edit)

        subject_label = QLabel("Email subject")
        self.subject_edit = QLineEdit(self)
        self.subject_edit.setText(str(subject or "").strip())
        layout.addWidget(subject_label)
        layout.addWidget(self.subject_edit)

        body_label = QLabel("Email message")
        self.body_edit = QPlainTextEdit(self)
        self.body_edit.setPlainText(str(body_text or "").strip())
        self.body_edit.setMinimumHeight(180)
        layout.addWidget(body_label)
        layout.addWidget(self.body_edit, 1)

        attachment_title = QLabel(f"Attached PDFs ({len(attachment_paths)})")
        layout.addWidget(attachment_title)
        self.attachment_list = QListWidget(self)
        self.attachment_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.attachment_list.setMinimumHeight(180)
        for path in attachment_paths:
            item = QListWidgetItem(os.path.basename(path))
            item.setToolTip(path)
            self.attachment_list.addItem(item)
        layout.addWidget(self.attachment_list)

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        send_btn = QPushButton("Send School Email")
        send_btn.setObjectName("primaryAction")
        footer.addWidget(cancel_btn)
        footer.addWidget(send_btn)
        layout.addLayout(footer)
        cancel_btn.clicked.connect(self.reject)
        send_btn.clicked.connect(self.accept)

    def email_values(self) -> tuple[str, str, str]:
        return (
            (self.to_edit.text() or "").strip(),
            (self.subject_edit.text() or "").strip(),
            (self.body_edit.toPlainText() or "").strip(),
        )


class PhotoDeckImportWorker(QObject):
    progress = Signal(str)
    fatal_error = Signal(str)
    missing_requirements = Signal(str)
    finished = Signal()

    def __init__(self, base_dir: str):
        super().__init__()
        self.base_dir = base_dir
        self.completed_ids: List[str] = []

    def run(self):
        self.completed_ids = []

        try:
            emit_status("Initializing Gmail service...", self.progress.emit)
            service = get_gmail_service()
            emit_status("Gmail service ready.", self.progress.emit)
        except Exception as exc:  # pylint: disable=broad-except
            self.fatal_error.emit(f"Failed to initialize Gmail service: {exc}")
            self.finished.emit()
            return

        source_settings = ORDER_SOURCES['photodeck']
        from_address = source_settings['from_address']
        label_name = source_settings.get('gmail_label')
        imported_label_name = source_settings.get('gmail_imported_label')

        if not label_name:
            self.fatal_error.emit("PhotoDeck Gmail label is not configured.")
            self.finished.emit()
            return

        label_map = list_label_ids_by_name(service)
        label_id = label_map.get(label_name)
        if not label_id:
            self.fatal_error.emit(f'Gmail label "{label_name}" was not found.')
            self.finished.emit()
            return

        imported_label_id = None
        if imported_label_name:
            imported_label_id = label_map.get(imported_label_name)
            if not imported_label_id:
                imported_label_id = ensure_label_exists(service, imported_label_name)
            if not imported_label_id:
                emit_status(
                    f'Unable to ensure label "{imported_label_name}". Imported emails will remain in "{label_name}".',
                    self.progress.emit,
                )

        emit_status(f'Scanning label "{label_name}" for PhotoDeck orders...', self.progress.emit)
        messages = fetch_messages_by_label(service, label_id, MAX_RESULTS_PER_QUERY)
        emit_status(
            f'Found {len(messages)} message(s) in label "{label_name}".',
            self.progress.emit,
        )

        if not messages:
            self.missing_requirements.emit(f'No emails found under label "{label_name}".')
            self.finished.emit()
            return

        id_to_messages: Dict[str, List[str]] = defaultdict(list)
        message_to_ids: Dict[str, Set[str]] = {}
        message_records: Dict[str, dict] = {}

        for message in messages:
            msg_id = message.get('id')
            if not msg_id:
                continue

            message_records[msg_id] = message
            from_header = get_header_value(message, "From") or ""
            _, actual_address = parseaddr(from_header)
            actual_address = (actual_address or "").lower()
            if actual_address != from_address.lower():
                emit_status(
                    f"Skipping message {msg_id}: unexpected sender '{actual_address}'",
                    self.progress.emit,
                )
                continue

            picture_day_ids = extract_picture_day_ids(message)
            if not picture_day_ids:
                subject = get_header_value(message, "Subject") or "(no subject)"
                emit_status(
                    f"Message {msg_id} '{subject}' does not include a Picture Day ID; skipping.",
                    self.progress.emit,
                )
                debug_preview = get_message_debug_preview(message)
                emit_status(
                    f"    Subject: {debug_preview['subject']}",
                    self.progress.emit,
                )
                emit_status(
                    f"    Snippet: {debug_preview['snippet']}",
                    self.progress.emit,
                )
                emit_status(
                    f"    Plain text preview: {debug_preview['plain_text']}",
                    self.progress.emit,
                )
                emit_status(
                    f"    HTML text preview: {debug_preview['html_text']}",
                    self.progress.emit,
                )
                continue

            subject = get_header_value(message, "Subject") or "(no subject)"
            emit_status(
                f"Message {msg_id}: detected Picture Day ID(s) {', '.join(picture_day_ids)} from '{subject}'",
                self.progress.emit,
            )

            message_to_ids[msg_id] = set(picture_day_ids)
            for picture_day_id in picture_day_ids:
                id_to_messages[picture_day_id].append(msg_id)

        if not id_to_messages:
            self.missing_requirements.emit(
                f'No PhotoDeck emails from "{from_address}" contained a valid Picture Day ID.'
            )
            self.finished.emit()
            return

        missing_requirements: List[str] = []
        processable_ids: List[str] = []

        for picture_day_id in sorted(id_to_messages.keys()):
            finished_folder = self._find_finished_folder(picture_day_id)
            if not finished_folder:
                missing_requirements.append(
                    f'{picture_day_id}: No "5. Finished" folder found under {self.base_dir}.'
                )
                emit_status(
                    f'{picture_day_id}: Missing "5. Finished" folder in {self.base_dir}.',
                    self.progress.emit,
                )
                continue

            originals_path = find_originals_subdir(finished_folder)
            proofs_path = find_proofs_subdir(finished_folder)
            missing_parts: List[str] = []
            if not os.path.isdir(originals_path):
                missing_parts.append("original source folder")
            if not os.path.isdir(proofs_path):
                missing_parts.append("proof output folder")

            if missing_parts:
                folder_name = os.path.basename(finished_folder)
                missing_requirements.append(
                    f"{picture_day_id}: Missing {', '.join(missing_parts)} in {folder_name}."
                )
                emit_status(
                    f"{picture_day_id}: Missing {', '.join(missing_parts)} in folder {folder_name}.",
                    self.progress.emit,
                )
                continue

            emit_status(
                f'{picture_day_id}: Prerequisites satisfied using folder "{os.path.basename(finished_folder)}".',
                self.progress.emit,
            )
            processable_ids.append(picture_day_id)

        if missing_requirements:
            issues_text = "Issues detected:\n" + "\n".join(f"- {issue}" for issue in missing_requirements)
            self.missing_requirements.emit(issues_text)

        if not processable_ids:
            emit_status("No Picture Day IDs were ready for import.", self.progress.emit)
            self.finished.emit()
            return

        successful_ids: Set[str] = set()

        for picture_day_id in processable_ids:
            try:
                process_picture_day(
                    service,
                    picture_day_id,
                    "photodeck",
                    progress_callback=self.progress.emit,
                )
                successful_ids.add(picture_day_id)
                self.completed_ids.append(picture_day_id)
                emit_status(f"{picture_day_id}: Processing complete!", self.progress.emit)
            except NoOrdersFoundError as error:
                emit_status(str(error), self.progress.emit)
            except HttpError as error:
                emit_status(f"{picture_day_id}: Gmail API error - {error}", self.progress.emit)
            except Exception as error:  # pylint: disable=broad-except
                emit_status(f"{picture_day_id}: Unexpected error - {error}", self.progress.emit)

        self.completed_ids = sorted(set(self.completed_ids))

        if successful_ids and imported_label_id:
            for msg_id, ids in message_to_ids.items():
                if not ids or not ids.issubset(successful_ids):
                    continue
                modify_message_labels(
                    service,
                    msg_id,
                    add_label_ids=[imported_label_id],
                    remove_label_ids=[label_id],
                )
                subject = get_header_value(message_records.get(msg_id, {}), "Subject") or "(no subject)"
                emit_status(
                    f"{', '.join(sorted(ids))}: Updated email '{subject}' to label '{imported_label_name}'.",
                    self.progress.emit,
                )
        elif successful_ids and not imported_label_id:
            emit_status(
                "Imported label ID unavailable; completed emails will remain under the original label.",
                self.progress.emit,
            )

        self.finished.emit()

    def _find_finished_folder(self, picture_day_id: str) -> Optional[str]:
        if not os.path.isdir(self.base_dir):
            return None

        normalized_id = picture_day_id.upper()
        candidates: List[str] = []

        for entry in os.listdir(self.base_dir):
            full_path = os.path.join(self.base_dir, entry)
            if not os.path.isdir(full_path):
                continue
            entry_upper = entry.upper()
            if "5. FINISHED" in entry_upper and normalized_id in entry_upper:
                candidates.append(full_path)

        if not candidates:
            return None

        candidates.sort(key=lambda path: natural_sort_key(os.path.basename(path)))
        return candidates[0]

    @staticmethod
    def _existing_subdir(base_folder: str, names: List[str]) -> Optional[str]:
        for name in names:
            path = find_matching_subdir(base_folder, name)
            if os.path.isdir(path):
                return path
        return None


class _IoTaskThread(QThread):
    def __init__(self, task: Callable[[], object]):
        super().__init__()
        self._task = task
        self.result: object = None
        self.error: Exception | None = None
        self.traceback_text: str = ""

    def run(self) -> None:  # type: ignore[override]
        try:
            self.result = self._task()
        except Exception as exc:  # pylint: disable=broad-except
            self.error = exc
            self.traceback_text = traceback.format_exc()

def natural_sort_key(text):
    return [int(s) if s.isdigit() else s.lower() for s in re.split(r'(\d+)', text)]


def _detect_day_from_folder_name(folder_name: str) -> Optional[str]:
    for day in DAYS:
        if _matches_day(folder_name, day):
            return day
    return None


def _stage_for_day(day: str) -> Optional[int]:
    try:
        return int(DAY_TO_STAGE.get(day, 0)) or None
    except Exception:
        return None

class DragDropFolders(QWidget):
    def __init__(
        self,
        base_dir: str,
        *,
        db: Optional[DB] = None,
        db_host: str = DB_HOST,
        dbname: str = DB_NAME,
        user: str = DB_USER,
        password: str = DB_PASS,
        port: int = DB_PORT,
        workflow_domain: str = WORKFLOW_DOMAIN_YEARBOOK,
    ):
        super().__init__()
        self.base_dir = base_dir
        self.workflow_domain = (workflow_domain or WORKFLOW_DOMAIN_YEARBOOK).strip().lower() or WORKFLOW_DOMAIN_YEARBOOK
        self.db = db or DB(host=db_host, dbname=dbname, user=user, password=password, port=port)
        self.source_base_dir: Optional[str] = (os.environ.get("DAMY_SOURCE_BASE_DIR") or "").strip() or None
        self.no_fs_mutation = (os.environ.get("DAMY_NO_FS_MUTATION") or "0").strip().lower() in {"1", "true", "yes", "on"}
        self._db_warning_shown = False
        self.setWindowTitle("7-Column Folder Manager")
        self.resize(1000, 520)
        self.expanded_column = None
        self.list_widgets_by_day: Dict[str, QListWidget] = {}
        self.workflow_asset_popup: Optional[QFrame] = None
        self.workflow_asset_host_item: Optional[QListWidgetItem] = None
        self.workflow_asset_host_widget: Optional[QWidget] = None
        self.workflow_asset_anchor_list: Optional[QListWidget] = None
        self.workflow_asset_anchor_item: Optional[QListWidgetItem] = None
        self.workflow_asset_anchor_day: str = ""
        self.workflow_asset_title: Optional[QLabel] = None
        self.workflow_asset_hint: Optional[QLabel] = None
        self.workflow_asset_sort_card: Optional[SortActionCard] = None
        self.workflow_asset_bulk_button: Optional[QPushButton] = None
        self.workflow_asset_email_info_button: Optional[QPushButton] = None
        self.workflow_asset_send_button: Optional[QPushButton] = None
        self.workflow_asset_parent_delivery_button: Optional[QPushButton] = None
        self.workflow_asset_sort_section: Optional[QWidget] = None
        self.workflow_asset_stage3_section: Optional[QWidget] = None
        self.workflow_asset_stage4_section: Optional[QWidget] = None
        self.workflow_asset_stage3_splitter: Optional[QSplitter] = None
        self.workflow_asset_stage3_pdfs_path: str = ""
        self.workflow_asset_stage3_pdf_card: Optional[Stage3PdfAssetCard] = None
        self.photodeck_button: Optional[QPushButton] = None
        self.photodeck_status: Optional[QPlainTextEdit] = None
        self.photodeck_thread: Optional[QThread] = None
        self.photodeck_worker: Optional[PhotoDeckImportWorker] = None
        self.photodeck_logs: List[str] = []
        self.photodeck_active_ids: List[str] = []
        self.photodeck_had_fatal_error = False
        self.yearbook_import_process: Optional[subprocess.Popen] = None
        self.yearbook_import_timer = QTimer(self)
        self.yearbook_import_timer.setInterval(500)
        self.yearbook_import_timer.timeout.connect(self._poll_yearbook_import_process)
        self.send_to_edit_button: Optional[QPushButton] = None
        self.send_to_edit_status: Optional[QListWidget] = None
        self.send_to_edit_progress: Optional[QProgressBar] = None
        self.returned_from_edit_button: Optional[QPushButton] = None
        self.send_to_edit_paths: Dict[str, str] = {}
        self.pending_send_to_edit_entry: Optional[Tuple[str, str]] = self._build_existing_send_to_edit_entry()
        self.print_button: Optional[QPushButton] = None
        self.print_status: Optional[QListWidget] = None
        self.print_paths: Dict[str, str] = {}
        self.pending_print_entry: Optional[Tuple[str, str]] = self._build_existing_print_entry()
        self.move_to_package_button: Optional[QPushButton] = None
        self.print_progress: Optional[QProgressBar] = None
        self.package_progress: Optional[QProgressBar] = None
        self._embedded_mode = False
        self._rows_by_stage_cache: Dict[int, List] = {}
        self._reload_in_progress = False
        self._reload_pending = False
        self.column_splitter: Optional[QSplitter] = None
        self._restoring_column_sizes = False
        self._sync_ig_timer = QTimer(self)
        self._sync_ig_timer.setSingleShot(True)
        self._sync_ig_timer.timeout.connect(self.sync_ig_checkboxes)
        self.newly_added_item_ids: Set[int] = set()
        self.updated_item_ids: Set[int] = set()
        self.moved_item_ids: Set[int] = set()

        self.main_layout = QVBoxLayout(self)
        self.setLayout(self.main_layout)
        self.initialize_ui()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_F5:
            self.reload_ui()
        else:
            super().keyPressEvent(event)

    def _column_size_settings(self) -> QSettings:
        return QSettings("DAMYComp", "DAMYComp")

    def _column_splitter_size_key(self) -> str:
        domain = (self.workflow_domain or "yearbook").strip().lower() or "yearbook"
        return f"ui/{domain}_column_splitter_sizes_v2"

    def _save_column_splitter_sizes(self) -> None:
        splitter = self.column_splitter
        if splitter is None or self._restoring_column_sizes or self.expanded_column is not None:
            return
        sizes = [int(size) for size in splitter.sizes()]
        if not sizes or any(size <= 0 for size in sizes):
            return
        self._column_size_settings().setValue(self._column_splitter_size_key(), json.dumps(sizes))

    def _restore_column_splitter_sizes(self) -> None:
        splitter = self.column_splitter
        if splitter is None:
            return
        raw_value = self._column_size_settings().value(self._column_splitter_size_key(), "")
        if not raw_value:
            return
        try:
            parsed = json.loads(str(raw_value))
            sizes = [max(80, int(value)) for value in parsed]
        except Exception:
            return
        if len(sizes) != splitter.count():
            return
        self._restoring_column_sizes = True
        try:
            splitter.setSizes(sizes)
        finally:
            self._restoring_column_sizes = False

    def initialize_ui(self):
        while self.main_layout.count():
            item = self.main_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.setParent(None)
                widget.deleteLater()

        self.list_widgets_by_day = {}
        self._hide_workflow_asset_popup()
        self.photodeck_button = None
        self.photodeck_status = None
        self.send_to_edit_button = None
        self.send_to_edit_status = None
        self.send_to_edit_progress = None
        self.returned_from_edit_button = None
        self.send_to_edit_paths = {}
        self.print_button = None
        self.print_status = None
        self.print_progress = None
        self.package_progress = None
        self.print_paths = {}
        if not self.pending_send_to_edit_entry:
            self.pending_send_to_edit_entry = self._build_existing_send_to_edit_entry()
        if not self.pending_print_entry:
            self.pending_print_entry = self._build_existing_print_entry()
        self.move_to_package_button = None

        self.total_label = QLabel("Total: 0 folders")
        self.total_label.setAlignment(Qt.AlignCenter)
        self.main_layout.addWidget(self.total_label)

        self.search_input = QLineEdit(placeholderText="Filter folders as you type...")
        self.search_input.textChanged.connect(self.perform_search)
        if self._embedded_mode:
            self.search_input.hide()
        self.main_layout.addWidget(self.search_input)

        self.container = QWidget()
        self.container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.layout = QVBoxLayout(self.container)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.column_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.column_splitter.setChildrenCollapsible(False)
        self.column_splitter.setHandleWidth(10)
        self.column_splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.column_splitter.splitterMoved.connect(
            lambda _pos, _index: self._save_column_splitter_sizes()
        )
        self.layout.addWidget(self.column_splitter)
        self.column_widgets = []
        self._rows_by_stage_cache = self._build_rows_by_stage_cache()

        for day in DAYS:
            day_stage = _stage_for_day(day)
            entries = self._collect_day_entries(day)
            item_count = len(entries)

            if day == "3. Edit":
                edit_column = QWidget()
                edit_layout = QVBoxLayout(edit_column)
                edit_layout.setContentsMargins(0, 0, 0, 0)

                label = QLabel(f"{day} ({item_count} items)")
                label.setAlignment(Qt.AlignCenter)
                label.mousePressEvent = self.make_toggle_column_visibility(edit_column)
                edit_layout.addWidget(label)

                lw_edit = DragListWidget(self.base_dir, day)
                lw_edit.set_db(self.db)
                lw_edit.set_workflow_domain(self.workflow_domain)
                if day_stage is not None:
                    lw_edit.set_workflow_stage(day_stage)
                lw_i = QListWidget()
                lw_g = QListWidget()
                self.list_widgets_by_day[day] = lw_edit

                lw_i.setFixedWidth(50)
                lw_g.setFixedWidth(50)

                for entry, display, item_id, in_progress_by, has_i, has_g in entries:
                    lw_edit.add_entry(entry, display, item_id=item_id, in_progress_by=in_progress_by)
                    row_item = lw_edit.item(lw_edit.count() - 1)
                    if row_item is not None:
                        self._apply_row_markers(lw_edit, row_item, item_id)
                        self._set_item_flag_state(row_item, has_i=bool(has_i), has_g=bool(has_g))

                    for lw_box, label_text, checked in [(lw_i, "I", has_i), (lw_g, "G", has_g)]:
                        item = QListWidgetItem("✅" if checked else label_text)
                        item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                        item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
                        lw_box.addItem(item)

                lw_i.itemChanged.connect(self.handle_i_checkbox)
                lw_g.itemChanged.connect(self.handle_g_checkbox)

                row_widget = QWidget()
                row_layout = QHBoxLayout(row_widget)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.addWidget(lw_edit)
                row_layout.addWidget(lw_i)
                row_layout.addWidget(lw_g)

                edit_layout.addWidget(row_widget)
                if self.column_splitter is not None:
                    self.column_splitter.addWidget(edit_column)

                self.column_widgets.append((edit_column, lw_edit))
                self.edit_widgets = (lw_edit, lw_i, lw_g)
                lw_edit.itemRemovedFromEdit.connect(self.remove_edit_checkboxes)
                lw_edit.folderDropped.connect(self._schedule_sync_ig_checkboxes)
                lw_edit.folderDroppedDetailed.connect(self.on_folder_dropped)
                lw_edit.uiReloadRequested.connect(self.reload_ui)

            else:
                column = QWidget()
                column_layout = QVBoxLayout(column)
                column_layout.setContentsMargins(0, 0, 0, 0)

                label = QLabel(f"{day} ({item_count} items)")
                label.setAlignment(Qt.AlignCenter)
                label.mousePressEvent = self.make_toggle_column_visibility(column)
                column_layout.addWidget(label)

                if day == "6. Order Import":
                    self.photodeck_button = QPushButton("Import Paid Orders")
                    self.photodeck_button.clicked.connect(self.handle_yearbook_import_clicked)
                    column_layout.addWidget(self.photodeck_button)
                    if self.yearbook_import_process and self.yearbook_import_process.poll() is None:
                        self.photodeck_button.setEnabled(False)

                if day == "7. Edit":
                    self.send_to_edit_button = QPushButton("Move to Edit")
                    self.send_to_edit_button.clicked.connect(self.handle_move_to_edit_clicked)
                    column_layout.addWidget(self.send_to_edit_button)

                    self.send_to_edit_progress = QProgressBar()
                    self.send_to_edit_progress.setRange(0, 1)
                    self.send_to_edit_progress.setValue(1)
                    self.send_to_edit_progress.hide()
                    column_layout.addWidget(self.send_to_edit_progress)

                    self.send_to_edit_status = QListWidget()
                    self.send_to_edit_status.setSelectionMode(QListWidget.SingleSelection)
                    self.send_to_edit_status.itemDoubleClicked.connect(self.open_send_to_edit_folder)
                    self.send_to_edit_status.hide()
                    column_layout.addWidget(self.send_to_edit_status)

                    self.returned_from_edit_button = QPushButton("Returned From Edit")
                    self.returned_from_edit_button.clicked.connect(self.handle_returned_from_edit_clicked)
                    column_layout.addWidget(self.returned_from_edit_button)

                if day == "8. Print":
                    self.print_button = QPushButton("Prepare Print Folders")
                    self.print_button.clicked.connect(self.handle_print_clicked)
                    column_layout.addWidget(self.print_button)

                    self.print_progress = QProgressBar()
                    self.print_progress.setRange(0, 1)
                    self.print_progress.setValue(1)
                    self.print_progress.hide()
                    column_layout.addWidget(self.print_progress)

                    self.print_status = QListWidget()
                    self.print_status.setSelectionMode(QListWidget.SingleSelection)
                    self.print_status.itemDoubleClicked.connect(self.open_print_folder)
                    self.print_status.hide()
                    column_layout.addWidget(self.print_status)

                if day == "9. Package":
                    self.move_to_package_button = QPushButton("Move to Package")
                    self.move_to_package_button.clicked.connect(self.handle_move_to_package_clicked)
                    column_layout.addWidget(self.move_to_package_button)

                    self.package_progress = QProgressBar()
                    self.package_progress.setRange(0, 1)
                    self.package_progress.setValue(1)
                    self.package_progress.hide()
                    column_layout.addWidget(self.package_progress)

                lw = DragListWidget(self.base_dir, day)
                lw.set_db(self.db)
                lw.set_workflow_domain(self.workflow_domain)
                if day_stage is not None:
                    lw.set_workflow_stage(day_stage)
                self.list_widgets_by_day[day] = lw

                for entry, display, item_id, in_progress_by, _flag_i, _flag_g in entries:
                    lw.add_entry(entry, display, item_id=item_id, in_progress_by=in_progress_by)
                    row_item = lw.item(lw.count() - 1)
                    if row_item is not None:
                        self._apply_row_markers(lw, row_item, item_id)

                lw.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
                column_layout.addWidget(lw)
                if self.column_splitter is not None:
                    self.column_splitter.addWidget(column)

                self.column_widgets.append((column, lw))
                lw.folderDroppedDetailed.connect(self.on_folder_dropped)
                lw.uiReloadRequested.connect(self.reload_ui)
                if day in {"2. Sort", "3. Upload & PDFs", "4. School / Parent Delivery"}:
                    lw.itemClicked.connect(lambda item, source=lw: self._show_workflow_asset_popup_for_item(source, item))
                    lw.itemSelectionChanged.connect(lambda source=lw: self._on_workflow_asset_selection_changed(source))

        self.main_layout.addWidget(self.container, 1)
        QTimer.singleShot(0, self._restore_column_splitter_sizes)
        total_items = sum(lw.count() for _, lw in self.column_widgets)
        self.total_label.setText(f"Total: {total_items} folders")
        if self.pending_send_to_edit_entry and self.send_to_edit_status:
            label, path = self.pending_send_to_edit_entry
            self.append_send_to_edit_status(label, path)
            self.pending_send_to_edit_entry = None
        if self.pending_print_entry and self.print_status:
            label, path = self.pending_print_entry
            self.append_print_status(label, path)
            self.pending_print_entry = None

    def make_toggle_column_visibility(self, selected_column):
        def toggle(event):
            if self.expanded_column == selected_column:
                for col, _ in self.column_widgets:
                    col.show()
                self.expanded_column = None
            else:
                for col, _ in self.column_widgets:
                    col.setVisible(col == selected_column)
                self.expanded_column = selected_column
        return toggle

    def reload_ui(self):
        if self._reload_in_progress:
            self._reload_pending = True
            return
        self._reload_in_progress = True
        try:
            self.initialize_ui()
        finally:
            self._reload_in_progress = False
        if self._reload_pending:
            self._reload_pending = False
            QTimer.singleShot(0, self.reload_ui)

    def _apply_row_markers(self, lw: DragListWidget, item: QListWidgetItem, item_id: Optional[int]) -> None:
        try:
            marker_id = int(item_id if item_id is not None else item.data(ROLE_DB_ID))
        except Exception:
            return
        lw.apply_new_import_style(item, marker_id in self.newly_added_item_ids)
        lw.apply_moved_style(item, marker_id in self.moved_item_ids)
        lw.apply_updated_style(item, marker_id in self.updated_item_ids)

    def _mark_item_moved(self, item_id: int) -> None:
        marker_id = int(item_id)
        self.moved_item_ids.add(marker_id)
        # Keep marker behavior aligned with Prepaid:
        # move highlight wins over "new" and "updated" for the same row.
        self.updated_item_ids.discard(marker_id)
        self.newly_added_item_ids.discard(marker_id)

    def ingest_external_markers(
        self,
        *,
        new_ids: Optional[Set[int]] = None,
        updated_ids: Optional[Set[int]] = None,
        moved_ids: Optional[Set[int]] = None,
    ) -> None:
        if new_ids:
            self.newly_added_item_ids |= {int(v) for v in new_ids}
        if updated_ids:
            self.updated_item_ids |= {int(v) for v in updated_ids}
        if moved_ids:
            moved_set = {int(v) for v in moved_ids}
            self.moved_item_ids |= moved_set
            self.updated_item_ids -= moved_set
            self.newly_added_item_ids -= moved_set

    def on_folder_dropped(self, item_id: int, old_stage: int, new_stage: int, disk_name: str) -> None:
        _ = old_stage, new_stage, disk_name
        try:
            marker_id = int(item_id)
        except Exception:
            return
        self._mark_item_moved(marker_id)

    def set_embedded_mode(self, enabled: bool = True) -> None:
        self._embedded_mode = bool(enabled)
        search_widget = getattr(self, "search_input", None)
        if search_widget is not None:
            search_widget.setVisible(not self._embedded_mode)

    def set_external_search_text(self, text: str) -> None:
        search_widget = getattr(self, "search_input", None)
        if search_widget is not None:
            previous = search_widget.blockSignals(True)
            try:
                search_widget.setText(str(text or ""))
            finally:
                search_widget.blockSignals(previous)
        self.perform_search(str(text or ""))

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._resize_workflow_asset_host()

    def is_busy(self) -> tuple[bool, str]:
        process = getattr(self, "yearbook_import_process", None)
        try:
            if process is not None and process.poll() is None:
                return True, "YearBook Import"
        except Exception:
            pass
        return False, ""

    def _clear_other_workflow_selections(self, *, except_widget: Optional[QListWidget] = None) -> None:
        for day in ("2. Sort", "3. Upload & PDFs", "4. School / Parent Delivery"):
            widget = self.list_widgets_by_day.get(day)
            if widget is None or widget is except_widget:
                continue
            widget.blockSignals(True)
            try:
                widget.clearSelection()
                widget.setCurrentRow(-1)
            finally:
                widget.blockSignals(False)

    def _clear_workflow_asset_host(self) -> None:
        host_list = self.workflow_asset_anchor_list
        host_item = self.workflow_asset_host_item
        host_widget = self.workflow_asset_host_widget
        panel = self.workflow_asset_popup

        if host_list is not None and host_item is not None:
            try:
                host_list.removeItemWidget(host_item)
            except Exception:
                pass
            try:
                row = host_list.row(host_item)
            except Exception:
                row = -1
            if row >= 0:
                taken = host_list.takeItem(row)
                del taken

        if panel is not None and host_widget is not None and panel.parentWidget() is host_widget:
            try:
                panel.setParent(None)
            except Exception:
                pass

        if host_widget is not None:
            host_widget.setParent(None)
            host_widget.deleteLater()

        self.workflow_asset_host_item = None
        self.workflow_asset_host_widget = None

    def _resize_workflow_asset_host(self) -> None:
        host_list = self.workflow_asset_anchor_list
        host_item = self.workflow_asset_host_item
        host_widget = self.workflow_asset_host_widget
        panel = self.workflow_asset_popup
        if host_list is None or host_item is None or host_widget is None or panel is None:
            return
        available_width = max(340, host_list.viewport().width() - 24)
        panel.setMaximumWidth(available_width)
        panel.adjustSize()
        host_widget.adjustSize()
        host_item.setSizeHint(host_widget.sizeHint())

    def _ensure_workflow_asset_popup(self) -> None:
        if self.workflow_asset_popup is not None:
            return

        panel = QFrame()
        panel.setObjectName("workflowAssetPopup")
        panel.setFrameShape(QFrame.Shape.NoFrame)
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        panel.setStyleSheet(
            "QFrame#workflowAssetPopup {"
            " background: #1c2430; border: 1px solid #3c4d64; border-radius: 12px; }"
            "QLabel#workflowAssetTitle { color: #f4f8fd; font-size: 14px; font-weight: 700; }"
            "QLabel#workflowAssetHint { color: #9fb0c4; font-size: 12px; }"
            "QFrame#workflowStage3Panel { background: #243244; border: 1px solid #4a607c; border-radius: 10px; }"
            "QFrame#workflowStage4Panel { background: #243244; border: 1px solid #4a607c; border-radius: 10px; }"
            "QFrame#workflowAssetGroupPanel { background: #202b38; border: 1px solid #42546b; border-radius: 10px; }"
            "QLabel#workflowAssetGroupLabel { color: #edf4fb; font-size: 12px; font-weight: 700; }"
            "QLabel#workflowAssetGroupHint { color: #9fb0c4; font-size: 11px; }"
            "QPushButton { min-height: 36px; padding: 6px 10px; border-radius: 10px;"
            " border: 1px solid #3d5470; background: #263344; color: #f4f7fb; text-align: center; }"
            "QPushButton:hover { background: #2f4158; }"
            "QPushButton#workflowPrimaryAction { background: #2c6ad6; border-color: #4b84e3; font-weight: 700; }"
            "QPushButton#workflowPrimaryAction:hover { background: #3a78e4; }"
            "QPushButton#workflowSecondaryAction { background: #2b3849; border-color: #51667f; }"
        )

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        title = QLabel("Workflow Actions")
        title.setObjectName("workflowAssetTitle")
        title.setWordWrap(True)
        hint = QLabel("Click a folder in 2. Sort, 3. Upload & PDFs, or 4. School / Parent Delivery to show actions.")
        hint.setObjectName("workflowAssetHint")
        hint.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(hint)

        sort_section = QWidget()
        sort_layout = QVBoxLayout(sort_section)
        sort_layout.setContentsMargins(0, 0, 0, 0)
        sort_layout.setSpacing(8)
        sort_card = SortActionCard("Sort")
        sort_card.set_status("Not linked")
        sort_card.set_hint("Click to choose the source folder and run sorting.")
        sort_card.clicked.connect(self.handle_workflow_asset_primary_clicked)
        sort_layout.addWidget(sort_card)
        layout.addWidget(sort_section)

        stage3_section = QWidget()
        stage3_layout = QVBoxLayout(stage3_section)
        stage3_layout.setContentsMargins(0, 0, 0, 0)
        stage3_layout.setSpacing(8)
        stage3_panel = QFrame()
        stage3_panel.setObjectName("workflowStage3Panel")
        stage3_panel_layout = QVBoxLayout(stage3_panel)
        stage3_panel_layout.setContentsMargins(12, 12, 12, 12)
        stage3_panel_layout.setSpacing(12)

        stage3_label = QLabel("Main action")
        stage3_label.setObjectName("workflowAssetGroupLabel")
        stage3_panel_layout.addWidget(stage3_label)

        bulk_button = QPushButton("Upload")
        bulk_button.setObjectName("workflowPrimaryAction")
        bulk_button.clicked.connect(self.handle_photodeck_upload_asset_clicked)
        bulk_button.setMinimumHeight(44)

        stage3_hint = QLabel("Uploads to PhotoDeck, creates PDFs, and prepares delivery files for Stage 4.")
        stage3_hint.setObjectName("workflowAssetGroupHint")
        stage3_hint.setWordWrap(True)

        stage3_pdf_card = None
        # PDF card is kept for later, but hidden for now. Upload now creates PDFs automatically.
        # stage3_pdf_card = Stage3PdfAssetCard("PDF")
        # stage3_pdf_card.clicked.connect(self.handle_stage3_pdf_asset_clicked)
        # stage3_pdf_card.doubleClicked.connect(self.handle_stage3_pdf_asset_double_clicked)
        # stage3_pdf_card.clearRequested.connect(self.handle_stage3_pdf_asset_clear_clicked)
        # stage3_pdf_card.fileDropped.connect(self.handle_stage3_pdf_asset_dropped)

        stage3_panel_layout.addWidget(bulk_button)
        stage3_panel_layout.addWidget(stage3_hint)
        # stage3_panel_layout.addWidget(stage3_pdf_card)
        stage3_layout.addWidget(stage3_panel)
        layout.addWidget(stage3_section)

        stage4_section = QWidget()
        stage4_layout = QVBoxLayout(stage4_section)
        stage4_layout.setContentsMargins(0, 0, 0, 0)
        stage4_layout.setSpacing(8)
        stage4_panel = QFrame()
        stage4_panel.setObjectName("workflowStage4Panel")
        stage4_panel_layout = QVBoxLayout(stage4_panel)
        stage4_panel_layout.setContentsMargins(12, 12, 12, 12)
        stage4_panel_layout.setSpacing(10)

        school_group = QFrame()
        school_group.setObjectName("workflowAssetGroupPanel")
        school_group_layout = QVBoxLayout(school_group)
        school_group_layout.setContentsMargins(10, 10, 10, 10)
        school_group_layout.setSpacing(8)

        school_delivery_label = QLabel("School Delivery")
        school_delivery_label.setObjectName("workflowAssetGroupLabel")
        school_group_layout.addWidget(school_delivery_label)

        school_delivery_hint = QLabel("Save the school contact, then send the proof PDF packet.")
        school_delivery_hint.setObjectName("workflowAssetGroupHint")
        school_delivery_hint.setWordWrap(True)
        school_group_layout.addWidget(school_delivery_hint)

        email_info_button = QPushButton("School Contact")
        email_info_button.setObjectName("workflowSecondaryAction")
        email_info_button.setMinimumHeight(40)
        email_info_button.clicked.connect(self.handle_email_info_asset_clicked)

        send_button = QPushButton("Send School Email")
        send_button.setObjectName("workflowPrimaryAction")
        send_button.setMinimumHeight(40)
        send_button.clicked.connect(self.handle_send_asset_clicked)
        school_group_layout.addWidget(email_info_button)
        school_group_layout.addWidget(send_button)

        parent_group = QFrame()
        parent_group.setObjectName("workflowAssetGroupPanel")
        parent_group_layout = QVBoxLayout(parent_group)
        parent_group_layout.setContentsMargins(10, 10, 10, 10)
        parent_group_layout.setSpacing(8)

        parent_delivery_label = QLabel("Parent Delivery")
        parent_delivery_label.setObjectName("workflowAssetGroupLabel")
        parent_group_layout.addWidget(parent_delivery_label)

        parent_delivery_hint = QLabel("Review parent contacts, then send MMS previews or email with each child's PDF attached.")
        parent_delivery_hint.setObjectName("workflowAssetGroupHint")
        parent_delivery_hint.setWordWrap(True)
        parent_group_layout.addWidget(parent_delivery_hint)

        parent_delivery_button = QPushButton("Parent Delivery")
        parent_delivery_button.setObjectName("workflowPrimaryAction")
        parent_delivery_button.setMinimumHeight(40)
        parent_delivery_button.clicked.connect(self.handle_parent_delivery_asset_clicked)

        parent_group_layout.addWidget(parent_delivery_button)
        stage4_panel_layout.addWidget(school_group)
        stage4_panel_layout.addWidget(parent_group)
        stage4_layout.addWidget(stage4_panel)
        layout.addWidget(stage4_section)

        panel.hide()
        self.workflow_asset_popup = panel
        self.workflow_asset_title = title
        self.workflow_asset_hint = hint
        self.workflow_asset_sort_card = sort_card
        self.workflow_asset_bulk_button = bulk_button
        self.workflow_asset_email_info_button = email_info_button
        self.workflow_asset_send_button = send_button
        self.workflow_asset_parent_delivery_button = parent_delivery_button
        self.workflow_asset_sort_section = sort_section
        self.workflow_asset_stage3_section = stage3_section
        self.workflow_asset_stage4_section = stage4_section
        self.workflow_asset_stage3_splitter = None
        self.workflow_asset_stage3_pdf_card = stage3_pdf_card

    def _selected_items_for_day(self, day: str) -> List[QListWidgetItem]:
        widget = self.list_widgets_by_day.get(day)
        if widget is None:
            return []
        return [item for item in widget.selectedItems() if item is not None]

    def _summarize_selected_items(self, items: List[QListWidgetItem]) -> str:
        if not items:
            return "No folder selected"
        first = items[0]
        base = str(first.text() or "").strip() or str(first.data(Qt.UserRole) or "").strip() or "No folder selected"
        if len(items) == 1:
            return base
        return f"{base} (+{len(items) - 1} more)"

    def _refresh_workflow_asset_popup(self) -> None:
        panel = self.workflow_asset_popup
        if panel is None:
            return
        title = self.workflow_asset_title
        hint = self.workflow_asset_hint
        sort_card = self.workflow_asset_sort_card
        bulk_button = self.workflow_asset_bulk_button
        stage3_pdf_card = self.workflow_asset_stage3_pdf_card
        email_info_button = self.workflow_asset_email_info_button
        send_button = self.workflow_asset_send_button
        parent_delivery_button = self.workflow_asset_parent_delivery_button
        sort_section = self.workflow_asset_sort_section
        stage3_section = self.workflow_asset_stage3_section
        stage4_section = self.workflow_asset_stage4_section
        source = self.workflow_asset_anchor_list
        day = self.workflow_asset_anchor_day
        if (
            title is None
            or hint is None
            or sort_card is None
            or bulk_button is None
            or email_info_button is None
            or send_button is None
            or parent_delivery_button is None
            or sort_section is None
            or stage3_section is None
            or stage4_section is None
            or source is None
        ):
            return

        anchor_item = self.workflow_asset_anchor_item
        if anchor_item is None:
            self._hide_workflow_asset_popup()
            return
        if source.row(anchor_item) < 0:
            self._hide_workflow_asset_popup()
            return

        summary = self._summarize_selected_items([anchor_item])
        if day == "2. Sort":
            sort_section.show()
            stage3_section.hide()
            stage4_section.hide()
            title.setText(f"Sort Assets\n{summary}")
            hint.setText("Choose the source folder to sort. The app rebuilds the final sorted Proof folder and replaces the old output.")
            sort_card.set_title("Sort")
            sort_card.set_status("Ready")
            sort_card.set_hint("Choose the source folder and create a fresh sorted Proof output.")
        elif day == "4. School / Parent Delivery":
            sort_section.hide()
            stage3_section.hide()
            stage4_section.show()
            title.setText(f"School / Parent Delivery\n{summary}")
            hint.setText(
                "Use School Delivery for the school PDF packet. Use Parent Delivery for contact import, review, MMS, and email."
            )
            email_info_button.setText("School Contact")
            send_button.setText("Send School Email")
            parent_delivery_button.setText("Parent Delivery")
        else:
            sort_section.hide()
            stage3_section.show()
            stage4_section.hide()
            disk_name = str(anchor_item.data(Qt.UserRole) or "").strip()
            folder_path = self._resolve_existing_folder_path_for_open(disk_name) if disk_name else None
            work_root = self._resolve_stage3_work_root(folder_path) if folder_path else ""
            saved_pdfs_path = self._read_stage3_pdfs_link(disk_name)
            if saved_pdfs_path:
                self.workflow_asset_stage3_pdfs_path = saved_pdfs_path
            else:
                self.workflow_asset_stage3_pdfs_path = ""
            title.setText(f"Upload & PDFs\n{summary}")
            hint.setText(
                "Main action: upload to PhotoDeck. The app also creates PDFs and prepares delivery files for Stage 4 automatically."
            )
            bulk_button.setText("Upload to PhotoDeck")
            if stage3_pdf_card is not None:
                self._refresh_stage3_pdf_asset_card()

    def _show_workflow_asset_popup_for_item(self, source: QListWidget, item: QListWidgetItem) -> None:
        if source is None or item is None:
            self._hide_workflow_asset_popup()
            return
        day = str(getattr(source, "day_name", "") or "").strip()
        if day not in {"2. Sort", "3. Upload & PDFs", "4. School / Parent Delivery"}:
            self._hide_workflow_asset_popup()
            return

        panel = self.workflow_asset_popup
        if (
            panel is not None
            and panel.isVisible()
            and source is self.workflow_asset_anchor_list
            and day == self.workflow_asset_anchor_day
        ):
            anchor_item = self.workflow_asset_anchor_item
            same_item = item is anchor_item
            if not same_item and anchor_item is not None:
                item_disk = str(item.data(Qt.UserRole) or "").strip()
                anchor_disk = str(anchor_item.data(Qt.UserRole) or "").strip()
                same_item = bool(item_disk and anchor_disk and item_disk == anchor_disk)
            if same_item:
                self._hide_workflow_asset_popup(clear_selection=True)
                return

        self._ensure_workflow_asset_popup()
        panel = self.workflow_asset_popup
        if panel is None:
            return
        self._clear_other_workflow_selections(except_widget=source)
        source.blockSignals(True)
        try:
            source.clearSelection()
            item.setSelected(True)
            source.setCurrentItem(item)
        finally:
            source.blockSignals(False)

        self.workflow_asset_anchor_list = source
        self.workflow_asset_anchor_item = item
        self.workflow_asset_anchor_day = day
        self._refresh_workflow_asset_popup()
        self._clear_workflow_asset_host()

        host_widget = QWidget()
        host_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        host_layout = QHBoxLayout(host_widget)
        host_layout.setContentsMargins(10, 6, 10, 8)
        host_layout.setSpacing(0)
        panel.setParent(host_widget)
        panel.show()
        host_layout.addWidget(panel, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        host_layout.addStretch(1)

        popup_item = QListWidgetItem()
        popup_item.setFlags(Qt.ItemFlag.NoItemFlags)
        source.insertItem(source.row(item) + 1, popup_item)
        source.setItemWidget(popup_item, host_widget)

        self.workflow_asset_host_item = popup_item
        self.workflow_asset_host_widget = host_widget
        self._resize_workflow_asset_host()

    def _on_workflow_asset_selection_changed(self, source: QListWidget) -> None:
        if source is None:
            return
        selected = [item for item in source.selectedItems() if item is not None]
        if not selected:
            if source is self.workflow_asset_anchor_list:
                panel = self.workflow_asset_popup
                # QListWidget clears selection briefly before itemClicked fires.
                # Keep popup state until click handler decides open/toggle.
                if panel is not None and panel.isVisible():
                    return
                self._hide_workflow_asset_popup()
            return
        if source is not self.workflow_asset_anchor_list:
            return
        if self.workflow_asset_popup is None or not self.workflow_asset_popup.isVisible():
            return
        self._resize_workflow_asset_host()

    def _hide_workflow_asset_popup(self, *, clear_selection: bool = False) -> None:
        panel = self.workflow_asset_popup
        anchor_list = self.workflow_asset_anchor_list
        self._clear_workflow_asset_host()
        if panel is not None:
            panel.hide()
        self.workflow_asset_anchor_list = None
        self.workflow_asset_anchor_item = None
        self.workflow_asset_anchor_day = ""
        self.workflow_asset_stage3_pdfs_path = ""
        if clear_selection and anchor_list is not None:
            anchor_list.blockSignals(True)
            try:
                anchor_list.clearSelection()
                anchor_list.setCurrentRow(-1)
            finally:
                anchor_list.blockSignals(False)

    def _open_path_in_explorer(self, path: str, *, title: str) -> None:
        if not path or not os.path.isdir(path):
            QMessageBox.warning(self, title, f"Folder does not exist:\n{path}")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                opener = "xdg-open" if sys.platform.startswith("linux") else "open"
                subprocess.Popen([opener, path])
        except Exception as exc:
            QMessageBox.warning(self, title, f"Unable to open folder:\n{path}\n{exc}")

    def _resolve_nested_stage_folder_path(
        self,
        root_dir: str,
        stage_folder_name: str,
        disk_name: str,
    ) -> Optional[str]:
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

    def _resolve_existing_folder_path_for_open(self, disk_name: str) -> Optional[str]:
        base_path = os.path.join(self.base_dir, disk_name)
        if os.path.isdir(base_path):
            return base_path
        cancel_path = os.path.join(self.base_dir, "cancel", disk_name)
        if os.path.isdir(cancel_path):
            return cancel_path
        nested_edit = self._resolve_nested_stage_folder_path(self.base_dir, "3. Edit", disk_name)
        if nested_edit:
            return nested_edit
        cancel_nested_edit = self._resolve_nested_stage_folder_path(
            os.path.join(self.base_dir, "cancel"),
            "3. Edit",
            disk_name,
        )
        if cancel_nested_edit:
            return cancel_nested_edit
        source_dir = (self.source_base_dir or "").strip()
        if source_dir:
            source_path = os.path.join(source_dir, disk_name)
            if os.path.isdir(source_path):
                return source_path
            source_cancel = os.path.join(source_dir, "cancel", disk_name)
            if os.path.isdir(source_cancel):
                return source_cancel
            source_nested_edit = self._resolve_nested_stage_folder_path(source_dir, "3. Edit", disk_name)
            if source_nested_edit:
                return source_nested_edit
            source_cancel_nested_edit = self._resolve_nested_stage_folder_path(
                os.path.join(source_dir, "cancel"),
                "3. Edit",
                disk_name,
            )
            if source_cancel_nested_edit:
                return source_cancel_nested_edit
        return None

    def _resolve_stage3_anchor_target(self, action_title: str) -> Optional[Tuple[str, str]]:
        if self.workflow_asset_anchor_day != "3. Upload & PDFs":
            QMessageBox.information(
                self,
                action_title,
                "Click a folder in '3. Upload & PDFs' first.",
            )
            return None
        anchor_item = self.workflow_asset_anchor_item
        if anchor_item is None:
            QMessageBox.information(
                self,
                action_title,
                "No stage-3 folder is currently selected.",
            )
            return None
        disk_name = str(anchor_item.data(Qt.UserRole) or "").strip()
        if not disk_name:
            QMessageBox.warning(self, action_title, "Selected stage-3 folder has no disk name.")
            return None
        folder_path = self._resolve_existing_folder_path_for_open(disk_name)
        if not folder_path:
            QMessageBox.warning(self, action_title, f"Folder does not exist:\n{disk_name}")
            return None
        return disk_name, folder_path

    def _resolve_stage3_work_root(self, folder_path: str) -> str:
        root = str(folder_path or "").strip()
        if not root:
            return root
        sorted_path = os.path.join(root, "sorted")
        if os.path.isdir(sorted_path):
            try:
                with os.scandir(sorted_path) as it:
                    for entry in it:
                        if entry.is_dir() or entry.is_file():
                            return sorted_path
            except OSError:
                pass
        return root

    def _resolve_stage4_anchor_target(self, action_title: str) -> Optional[Tuple[str, str]]:
        if self.workflow_asset_anchor_day != "4. School / Parent Delivery":
            QMessageBox.information(
                self,
                action_title,
                "Click a folder in '4. School / Parent Delivery' first.",
            )
            return None
        anchor_item = self.workflow_asset_anchor_item
        if anchor_item is None:
            QMessageBox.information(
                self,
                action_title,
                "No stage-4 folder is currently selected.",
            )
            return None
        disk_name = str(anchor_item.data(Qt.UserRole) or "").strip()
        if not disk_name:
            QMessageBox.warning(self, action_title, "Selected stage-4 folder has no disk name.")
            return None
        folder_path = self._resolve_existing_folder_path_for_open(disk_name)
        if not folder_path:
            QMessageBox.warning(self, action_title, f"Folder does not exist:\n{disk_name}")
            return None
        return disk_name, folder_path

    def _resolve_stage2_anchor_target(self, action_title: str) -> Optional[Tuple[str, str]]:
        if self.workflow_asset_anchor_day != "2. Sort":
            QMessageBox.information(
                self,
                action_title,
                "Click a folder in '2. Sort' first.",
            )
            return None
        anchor_item = self.workflow_asset_anchor_item
        if anchor_item is None:
            QMessageBox.information(
                self,
                action_title,
                "No stage-2 folder is currently selected.",
            )
            return None
        disk_name = str(anchor_item.data(Qt.UserRole) or "").strip()
        if not disk_name:
            QMessageBox.warning(self, action_title, "Selected stage-2 folder has no disk name.")
            return None
        folder_path = os.path.join(self.base_dir, disk_name)
        if not os.path.isdir(folder_path):
            QMessageBox.warning(self, action_title, f"Folder does not exist:\n{folder_path}")
            return None
        return disk_name, folder_path

    def _advance_disk_name_to_stage(self, disk_name: str, target_stage: int, action_title: str) -> bool:
        name = str(disk_name or "").strip()
        if not name:
            return False
        try:
            row = self.db.get_item_by_disk_name(name, domain=self.workflow_domain)
            if row is not None:
                marker_id = int(row.id)
                self.db.update_domain_stage(
                    marker_id,
                    domain=self.workflow_domain,
                    stage=int(target_stage),
                )
            else:
                marker_id = int(
                    self.db.upsert_into_domain(
                        disk_name=name,
                        domain=self.workflow_domain,
                        stage=int(target_stage),
                    )
                )
            self._mark_item_moved(marker_id)
            return True
        except Exception as exc:
            QMessageBox.warning(
                self,
                action_title,
                f"Completed, but failed to move DB stage:\n{name}\n\n{exc}",
            )
            return False

    def handle_workflow_asset_primary_clicked(self) -> None:
        day = str(self.workflow_asset_anchor_day or "").strip()
        if day == "2. Sort":
            self.handle_sort_clicked()
            return
        QMessageBox.information(self, "Workflow", "Select a folder in 2. Sort first.")

    def _infer_stage3_upload_id(self, disk_name: str) -> str:
        token = str(disk_name or "").strip()
        pid_match = re.search(r"\b[PH]\d{7,8}\b", token, re.IGNORECASE)
        if pid_match:
            return pid_match.group(0).upper()
        date_match = re.match(r"^\s*(\d{6,8})\b", token)
        if date_match:
            return date_match.group(1)
        return datetime.now().strftime("%y%m%d")

    def handle_photodeck_upload_asset_clicked(self) -> None:
        if not self._ensure_fs_mutation_allowed("Stage 3 Upload"):
            return
        resolved = self._resolve_stage3_anchor_target("Stage 3 Upload")
        if resolved is None:
            return
        disk_name, folder_path = resolved
        default_upload_root = self._resolve_stage3_work_root(folder_path)
        work_root = QFileDialog.getExistingDirectory(
            self,
            "Choose Folder to Upload",
            default_upload_root or folder_path,
        )
        work_root = str(work_root or "").strip()
        if not work_root:
            return
        if not os.path.isdir(work_root):
            QMessageBox.warning(self, "Stage 3 Upload", f"Selected folder does not exist:\n{work_root}")
            return
        picture_day_id = self._infer_stage3_upload_id(disk_name)
        stage3_gallery_name = _stage3_gallery_name_from_paths(work_root, folder_path, disk_name)

        def _prompt_existing_gallery(_folder_name: str, _existing_uuid: str, _error_text: Optional[str] = None) -> str:
            # Upload is executed in worker thread; use deterministic behavior.
            return "use_existing"

        def _prompt_new_gallery_name(default_name: str, _error_text: Optional[str] = None) -> Optional[str]:
            # If a new name is required, append a timestamp to avoid collisions.
            return f"{default_name}-{datetime.now().strftime('%H%M%S')}"

        try:
            cancel_event = threading.Event()
            result = self._run_blocking_io_task(
                "Stage 3 Upload",
                "Running Bulk Upload workflow...\nPlease wait.",
                lambda: run_stage3_bulk_upload(
                    root_folder=work_root,
                    picture_day_id=picture_day_id,
                    gallery_name=stage3_gallery_name,
                    pricing_key="YEARBOOK_PRICING_PROFILE",
                    max_workers=4,
                    on_existing_gallery=_prompt_existing_gallery,
                    on_new_gallery_name=_prompt_new_gallery_name,
                    cancel_event=cancel_event,
                ),
                cancel_event=cancel_event,
            )
        except UserCancelled as exc:
            QMessageBox.information(self, "Stage 3 Upload", str(exc) or "Upload cancelled.")
            return
        except Exception as exc:
            QMessageBox.warning(self, "Stage 3 Upload", f"Upload failed:\n{exc}")
            return

        result_map = result if isinstance(result, dict) else {}
        source_root = str(result_map.get("source_root") or work_root)
        uploaded_count = int(result_map.get("scheduled_file_count") or 0)
        gallery_name = str(result_map.get("gallery_name") or disk_name).strip()
        gallery_uuid = str(result_map.get("gallery_uuid") or "").strip()

        try:
            pdf_result = self._run_blocking_io_task(
                "Stage 3 Create PDFs",
                "Upload finished. Generating PDF packets...\nPlease wait.",
                lambda: run_stage3_create_pdfs(
                    root_folder=work_root,
                    pdf_output_root=folder_path,
                    gallery_name=gallery_name or stage3_gallery_name,
                ),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Stage 3 Upload", f"Upload finished, but Create PDFs failed:\n{exc}")
            return

        pdf_result_map = pdf_result if isinstance(pdf_result, dict) else {}
        pdfs_path = str(pdf_result_map.get("pdfs_root") or os.path.join(folder_path, "PDFs"))
        pdf_count = int(pdf_result_map.get("pdf_count") or 0)
        saved_link, save_error = self._save_stage3_pdfs_link(disk_name, pdfs_path)
        self.workflow_asset_stage3_pdfs_path = pdfs_path
        self._refresh_workflow_asset_popup()

        child_mms_result: Optional[Dict[str, object]] = None
        child_mms_error = ""
        r2_settings = load_r2_settings()
        missing_r2 = missing_r2_settings(r2_settings)
        if missing_r2:
            child_mms_error = "Cloudflare R2 not configured: " + ", ".join(missing_r2)
        else:
            try:
                parent_row = self._get_or_create_row_for_disk_name(disk_name, stage=3)
                prepared = self._run_blocking_io_task(
                    "Prepare Parent Delivery",
                    "Generating parent contact rows and publishing cloud preview links...\nPlease wait.",
                    lambda: prepare_child_info_assets(
                        folder_path,
                        r2_settings,
                        db=self.db,
                        workflow_item_id=int(getattr(parent_row, "id", 0) or 0) if parent_row is not None else None,
                        disk_name=disk_name,
                    ),
                )
                child_mms_result = prepared if isinstance(prepared, dict) else {}
            except Exception as exc:
                child_mms_error = str(exc)

        summary_lines = [
            f"Folder: {disk_name}",
            f"Source root: {source_root}",
            f"Picture Day ID: {picture_day_id}",
            f"Gallery name: {gallery_name}",
            f"Files queued for upload: {uploaded_count}",
            f"PDF folder: {pdfs_path}",
            f"Generated PDFs: {pdf_count}",
        ]
        if saved_link:
            summary_lines.append("PDF link saved to DB.")
        else:
            summary_lines.append(f"PDF link not saved: {save_error or 'Unknown error'}")
        if child_mms_result is not None:
            summary_lines.append(
                f"Parent Delivery prepared with {child_mms_result.get('record_count', 0)} child row(s) and cloud preview links."
            )
        elif child_mms_error:
            summary_lines.append(f"Parent Delivery not prepared: {child_mms_error}")
        if gallery_uuid:
            summary_lines.append(f"Gallery UUID: {gallery_uuid}")
        moved_to_stage4 = False
        if uploaded_count > 0:
            moved_to_stage4 = self._advance_disk_name_to_stage(disk_name, 4, "Stage 3 Upload")
            if moved_to_stage4:
                summary_lines.append("Auto moved to stage 4 (School / Parent Delivery).")
        QMessageBox.information(self, "Stage 3 Upload", "\n".join(summary_lines))
        if moved_to_stage4:
            self.reload_ui()

    def handle_stage3_create_pdfs_asset_clicked(self) -> None:
        if not self._ensure_fs_mutation_allowed("Stage 3 Create PDFs"):
            return
        resolved = self._resolve_stage3_anchor_target("Stage 3 Create PDFs")
        if resolved is None:
            return
        disk_name, folder_path = resolved
        work_root = self._resolve_stage3_work_root(folder_path)
        stage3_gallery_name = _stage3_gallery_name_from_paths(work_root, folder_path, disk_name)
        try:
            result = self._run_blocking_io_task(
                "Stage 3 Create PDFs",
                "Generating PDF packets...\nPlease wait.",
                lambda: run_stage3_create_pdfs(
                    root_folder=work_root,
                    pdf_output_root=folder_path,
                    gallery_name=stage3_gallery_name,
                ),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Stage 3 Create PDFs", f"Create PDFs failed:\n{exc}")
            return

        result_map = result if isinstance(result, dict) else {}
        pdfs_path = str(result_map.get("pdfs_root") or os.path.join(folder_path, "PDFs"))
        pdf_count = int(result_map.get("pdf_count") or 0)
        saved_link, save_error = self._save_stage3_pdfs_link(disk_name, pdfs_path)
        self.workflow_asset_stage3_pdfs_path = pdfs_path
        self._refresh_workflow_asset_popup()
        save_line = "PDF link saved to DB."
        if not saved_link:
            save_line = f"PDF link not saved: {save_error or 'Unknown error'}"
        QMessageBox.information(
            self,
            "Stage 3 Create PDFs",
            "\n".join(
                [
                    f"Folder: {disk_name}",
                    f"Source root: {work_root}",
                    f"PDF folder: {pdfs_path}",
                    f"Generated PDFs: {pdf_count}",
                    save_line,
                ]
            ),
        )

    def handle_email_school_complete_asset_clicked(self) -> None:
        resolved = self._resolve_stage4_anchor_target("School Email Complete")
        if resolved is None:
            return
        disk_name, _folder_path = resolved
        moved = self._advance_disk_name_to_stage(disk_name, 5, "School Email Complete")
        if moved:
            QMessageBox.information(
                self,
                "School Email Complete",
                f"Moved to stage 5 (Finished):\n{disk_name}",
            )
            self.reload_ui()

    def _extract_school_name(self, display_name: str) -> str:
        text = str(display_name or "").strip()
        text = re.sub(r"^\d{6}\s+", "", text)
        text = re.sub(r"\s+P\d{8,}\b.*$", "", text).strip()
        return text or str(display_name or "").strip()

    def _first_email_from_text(self, raw_text: str) -> str:
        text = str(raw_text or "").strip()
        if not text:
            return ""
        match = re.search(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", text, flags=re.IGNORECASE)
        return (match.group(0).strip() if match else "").strip()

    def _collect_stage3_pdf_files(self, folder_path: str) -> tuple[str, List[str]]:
        primary_pdf_root = os.path.join(folder_path, "PDFs")
        work_root = self._resolve_stage3_work_root(folder_path)
        fallback_pdf_root = os.path.join(work_root, "PDFs")
        if os.path.isdir(primary_pdf_root):
            pdf_root = primary_pdf_root
        elif os.path.isdir(fallback_pdf_root):
            pdf_root = fallback_pdf_root
        else:
            return primary_pdf_root, []
        pdf_files: List[str] = []
        for root, _dirs, files in os.walk(pdf_root):
            if "Parent Delivery Assets" in {part for part in os.path.normpath(root).split(os.sep)}:
                continue
            for file_name in files:
                if file_name.lower().endswith(".pdf"):
                    pdf_files.append(os.path.join(root, file_name))
        pdf_files.sort(key=lambda p: natural_sort_key(os.path.basename(p)))
        return pdf_root, pdf_files

    def _get_or_create_row_for_disk_name(self, disk_name: str, *, stage: int) -> Optional[object]:
        try:
            row = self.db.get_item_by_disk_name(disk_name, domain=self.workflow_domain)
            if row is not None:
                return row
            marker_id = int(
                self.db.upsert_into_domain(
                    disk_name=disk_name,
                    domain=self.workflow_domain,
                    stage=int(stage),
                )
            )
            return self.db.get_item_by_id(marker_id)
        except Exception:
            return None

    def _save_stage3_pdfs_link(self, disk_name: str, pdfs_path: str) -> tuple[bool, str]:
        cleaned_path = str(pdfs_path or "").strip()
        if not cleaned_path:
            return False, "PDF folder path is empty."
        try:
            row = self._get_or_create_row_for_disk_name(disk_name, stage=3)
            if row is None:
                return False, "Could not resolve DB row."
            self.db.set_pdf_path(int(row.id), cleaned_path)
            return True, ""
        except Exception as exc:
            return False, str(exc)

    def _read_stage3_pdfs_link(self, disk_name: str) -> str:
        key = str(disk_name or "").strip()
        if not key:
            return ""
        try:
            row = self.db.get_item_by_disk_name(key, domain=self.workflow_domain)
            if row is None:
                return ""
            return str(getattr(row, "pdf_path", "") or "").strip()
        except Exception:
            return ""

    def _format_stage3_asset_mtime(self, path: str) -> str:
        try:
            ts = datetime.fromtimestamp(os.path.getmtime(path))
            return ts.strftime("%Y-%m-%d %I:%M %p")
        except Exception:
            return ""

    def _refresh_stage3_pdf_asset_card(self) -> None:
        card = self.workflow_asset_stage3_pdf_card
        if card is None:
            return
        linked_path = str(self.workflow_asset_stage3_pdfs_path or "").strip()
        if not linked_path:
            card.set_asset_state(
                linked=False,
                status_text="Not linked",
                hint_text="Drop a file/folder here\nor single-click to create\nDouble-click to choose",
            )
            return

        display_name = os.path.basename(linked_path.rstrip("\\/")) or linked_path
        if not os.path.exists(linked_path):
            card.set_asset_state(
                linked=False,
                file_name=display_name,
                status_text="Saved path missing",
                hint_text=f"{display_name}\nDouble-click to choose",
                meta_text=f"Path: {linked_path}",
            )
            return

        updated = self._format_stage3_asset_mtime(linked_path)
        meta_parts = []
        if updated:
            meta_parts.append(f"Last updated: {updated}")
        meta_parts.append(f"Path: {linked_path}")
        card.set_asset_state(
            linked=True,
            file_name=display_name,
            status_text="Linked",
            hint_text=f"{display_name}\nSingle-click to open\nDouble-click to choose",
            meta_text="\n".join(meta_parts),
        )

    def _choose_stage3_pdf_asset_path(self, base_folder: str) -> str:
        start_path = str(self.workflow_asset_stage3_pdfs_path or "").strip()
        if not start_path:
            start_path = os.path.join(base_folder, "PDFs")
        if not os.path.exists(start_path):
            start_path = base_folder

        picked_dir = QFileDialog.getExistingDirectory(self, "Select PDFs Folder", start_path)
        if picked_dir:
            return str(picked_dir).strip()

        picked_file, _ = QFileDialog.getOpenFileName(
            self,
            "Select PDF File",
            start_path,
            "PDF files (*.pdf);;All files (*.*)",
        )
        return str(picked_file or "").strip()

    def handle_stage3_pdf_asset_clicked(self) -> None:
        linked_path = str(self.workflow_asset_stage3_pdfs_path or "").strip()
        if linked_path and os.path.exists(linked_path):
            self.handle_stage3_open_linked_pdfs_asset_clicked()
            return
        # Keep PDF behavior merged with Create PDFs: generate and save path when not linked.
        self.handle_stage3_create_pdfs_asset_clicked()

    def handle_stage3_pdf_asset_double_clicked(self) -> None:
        resolved = self._resolve_stage3_anchor_target("Stage 3 PDF Asset")
        if resolved is None:
            return
        disk_name, folder_path = resolved
        picked_path = self._choose_stage3_pdf_asset_path(folder_path)
        if not picked_path:
            return
        saved_link, save_error = self._save_stage3_pdfs_link(disk_name, picked_path)
        if not saved_link:
            QMessageBox.warning(self, "Stage 3 PDF Asset", f"Could not save linked path:\n{save_error or 'Unknown error'}")
            return
        self.workflow_asset_stage3_pdfs_path = picked_path
        self._refresh_workflow_asset_popup()

    def handle_stage3_pdf_asset_dropped(self, raw_path: str) -> None:
        resolved = self._resolve_stage3_anchor_target("Stage 3 PDF Asset")
        if resolved is None:
            return
        disk_name, _folder_path = resolved
        dropped_path = str(raw_path or "").strip()
        if not dropped_path:
            return
        if not os.path.exists(dropped_path):
            QMessageBox.warning(self, "Stage 3 PDF Asset", f"Dropped path does not exist:\n{dropped_path}")
            return
        saved_link, save_error = self._save_stage3_pdfs_link(disk_name, dropped_path)
        if not saved_link:
            QMessageBox.warning(self, "Stage 3 PDF Asset", f"Could not save linked path:\n{save_error or 'Unknown error'}")
            return
        self.workflow_asset_stage3_pdfs_path = dropped_path
        self._refresh_workflow_asset_popup()

    def handle_stage3_pdf_asset_clear_clicked(self) -> None:
        resolved = self._resolve_stage3_anchor_target("Stage 3 PDF Asset")
        if resolved is None:
            return
        disk_name, _folder_path = resolved
        try:
            row = self._get_or_create_row_for_disk_name(disk_name, stage=3)
            if row is None:
                raise RuntimeError("Could not resolve DB row.")
            self.db.set_pdf_path(int(row.id), None)
        except Exception as exc:
            QMessageBox.warning(self, "Stage 3 PDF Asset", f"Could not clear linked path:\n{exc}")
            return
        self.workflow_asset_stage3_pdfs_path = ""
        self._refresh_workflow_asset_popup()

    def handle_stage3_open_linked_pdfs_asset_clicked(self) -> None:
        linked_path = str(self.workflow_asset_stage3_pdfs_path or "").strip()
        if not linked_path:
            QMessageBox.information(self, "Stage 3 PDF Asset", "No linked PDF path.")
            return
        if not os.path.exists(linked_path):
            QMessageBox.warning(self, "Stage 3 PDF Asset", f"Linked path does not exist:\n{linked_path}")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(linked_path)  # type: ignore[attr-defined]
            else:
                opener = "xdg-open" if sys.platform.startswith("linux") else "open"
                subprocess.Popen([opener, linked_path])
        except Exception as exc:
            QMessageBox.warning(self, "Stage 3 PDF Asset", f"Unable to open linked path:\n{linked_path}\n{exc}")

    def handle_email_info_asset_clicked(self) -> None:
        resolved = self._resolve_stage4_anchor_target("School Contact")
        if resolved is None:
            return
        disk_name, _folder_path = resolved
        row = self._get_or_create_row_for_disk_name(disk_name, stage=4)
        if row is None:
            QMessageBox.warning(self, "School Contact", f"Could not load DB row:\n{disk_name}")
            return

        current_name = (getattr(row, "contact_name", None) or "").strip()
        current_email = (getattr(row, "contact_email", None) or "").strip()
        current_phone = (getattr(row, "contact_phone", None) or "").strip()
        if not (current_name or current_email or current_phone):
            parsed_name, parsed_email, parsed_phone = parse_contact_fields_from_note(getattr(row, "note", None))
            current_name = current_name or str(parsed_name or "")
            current_email = current_email or str(parsed_email or "")
            current_phone = current_phone or str(parsed_phone or "")

        display_name = (getattr(row, "display_name", None) or "").strip()
        if not display_name and self.workflow_asset_anchor_item is not None:
            display_name = str(self.workflow_asset_anchor_item.text() or "").strip()
        school_name = self._extract_school_name(display_name or disk_name)

        dialog = ContactEditorDialog(
            self,
            school_name=school_name,
            contact_name=current_name,
            contact_email=current_email,
            contact_phone=current_phone,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_name, new_email, new_phone = dialog.contact_values()
        try:
            self.db.set_contact(
                int(row.id),
                contact_name=new_name,
                contact_email=new_email,
                contact_phone=new_phone,
            )
        except Exception as exc:
            QMessageBox.warning(self, "School Contact", f"Could not save contact:\n{exc}")
            return
        QMessageBox.information(self, "School Contact", f"Contact updated for:\n{disk_name}")

    def handle_send_asset_clicked(self) -> None:
        resolved = self._resolve_stage4_anchor_target("Send School Email")
        if resolved is None:
            return
        disk_name, folder_path = resolved

        row = self._get_or_create_row_for_disk_name(disk_name, stage=4)
        if row is None:
            QMessageBox.warning(self, "Send School Email", f"Could not load DB row:\n{disk_name}")
            return

        contact_email = (getattr(row, "contact_email", None) or "").strip()
        if not contact_email:
            _parsed_name, parsed_email, _parsed_phone = parse_contact_fields_from_note(getattr(row, "note", None))
            contact_email = str(parsed_email or "").strip()
        to_email = self._first_email_from_text(contact_email)
        if not to_email:
            QMessageBox.warning(
                self,
                "Send School Email",
                "No contact email found for this folder.\nUse School Contact to set the contact email first.",
            )
            return

        pdf_root, pdf_files = self._collect_stage3_pdf_files(folder_path)
        if not pdf_files:
            QMessageBox.warning(
                self,
                "Send School Email",
                f"No PDF files found in:\n{pdf_root}",
            )
            return

        display_name = (getattr(row, "display_name", None) or "").strip()
        if not display_name and self.workflow_asset_anchor_item is not None:
            display_name = str(self.workflow_asset_anchor_item.text() or "").strip()
        school_name = self._extract_school_name(display_name or disk_name)
        default_subject = f"{school_name} - Proofs"
        default_body = (
            f"Hello {school_name},\n\n"
            "Attached are your proof PDFs.\n\n"
            "Thank you."
        )

        dialog = Stage4SendDialog(
            self,
            to_email=to_email,
            subject=default_subject,
            body_text=default_body,
            attachment_paths=pdf_files,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        to_value, subject_value, body_value = dialog.email_values()
        recipient = self._first_email_from_text(to_value)
        if not recipient:
            QMessageBox.warning(self, "Send School Email", "Please enter a valid recipient email in 'To'.")
            return

        try:
            send_result = self._run_blocking_io_task(
                "Send School Email",
                "Sending email with PDF attachments...\nPlease wait.",
                lambda: send_email_with_attachments(
                    get_gmail_service(),
                    to=recipient,
                    subject=subject_value,
                    body_text=body_value,
                    attachment_paths=pdf_files,
                ),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Send School Email", f"Email send failed:\n{exc}")
            return

        moved_to_stage5 = self._advance_disk_name_to_stage(disk_name, 5, "Send School Email")
        summary_lines = [
            f"To: {recipient}",
            f"Attachments: {len(pdf_files)} PDF(s)",
            f"PDF folder: {pdf_root}",
        ]
        if isinstance(send_result, dict):
            msg_id = str(send_result.get("id") or "").strip()
            if msg_id:
                summary_lines.append(f"Gmail message id: {msg_id}")
        if moved_to_stage5:
            summary_lines.append("Moved to stage 5 (Finished).")
        QMessageBox.information(self, "Send School Email", "\n".join(summary_lines))
        if moved_to_stage5:
            self.reload_ui()

    def _open_parent_delivery_dialog(self, *, start_mode: str = "info") -> None:
        resolved = self._resolve_stage4_anchor_target("Parent Delivery")
        if resolved is None:
            return
        disk_name, folder_path = resolved
        if not os.path.isdir(folder_path):
            QMessageBox.warning(self, "Parent Delivery", f"Folder does not exist:\n{folder_path}")
            return
        row = self._get_or_create_row_for_disk_name(disk_name, stage=4)
        dialog = ChildMmsDialog(
            self,
            job_folder=folder_path,
            disk_name=disk_name,
            db=self.db,
            workflow_item_id=int(getattr(row, "id", 0) or 0) if row is not None else None,
            start_mode=start_mode,
            task_runner=lambda title, message, task: self._run_blocking_io_task(title, message, task),
        )
        dialog.exec()

    def handle_parent_delivery_asset_clicked(self) -> None:
        self._open_parent_delivery_dialog(start_mode="info")

    def handle_child_info_asset_clicked(self) -> None:
        self._open_parent_delivery_dialog(start_mode="info")

    def handle_mms_child_asset_clicked(self) -> None:
        self._open_parent_delivery_dialog(start_mode="send")

    def _show_db_warning_once(self, title: str, message: str) -> None:
        if self._db_warning_shown:
            return
        self._db_warning_shown = True
        QMessageBox.warning(self, title, message)

    def _build_rows_by_stage_cache(self) -> Dict[int, List]:
        grouped: Dict[int, List] = {}
        try:
            rows = self.db.list_by_domain(self.workflow_domain)
        except Exception:
            self._show_db_warning_once(
                "Workflow DB",
                "Could not read Yearbook rows from DB.\n"
                "Please check the DB connection, then refresh.",
            )
            return grouped
        for row in rows:
            try:
                stage_value = int(getattr(row, "stage", 0) or 0)
            except Exception:
                continue
            grouped.setdefault(stage_value, []).append(row)
        return grouped

    def _collect_day_entries(
        self,
        day: str,
    ) -> List[Tuple[str, str, Optional[int], Optional[str], bool, bool]]:
        day_stage = _stage_for_day(day)
        if day_stage is None:
            return []
        rows = list(self._rows_by_stage_cache.get(int(day_stage), []))
        entries: List[Tuple[str, str, Optional[int], Optional[str], bool, bool]] = []
        for row in rows:
            disk_name = str(row.disk_name or "")
            if not disk_name:
                continue
            extracted = _extract_day_suffix(disk_name, day)
            if extracted and extracted != disk_name.strip():
                display = extracted
            else:
                display = (row.display_name or "").strip() or disk_name
            entries.append(
                (
                    disk_name,
                    display,
                    int(row.id),
                    row.in_progress_by,
                    bool(getattr(row, "flag_i", False)),
                    bool(getattr(row, "flag_g", False)),
                )
            )
        return entries

    def _ensure_fs_mutation_allowed(self, action_name: str) -> bool:
        if not self.no_fs_mutation:
            return True
        QMessageBox.information(
            self,
            action_name,
            "DAMY_NO_FS_MUTATION is enabled.\n"
            "Disable that env var to allow file/folder changes.",
        )
        return False

    def collect_finished_picture_day_ids(self) -> List[str]:
        finished_widget = self.list_widgets_by_day.get("5. Finished")
        if not finished_widget:
            return []
        seen = set()
        picture_day_ids: List[str] = []
        for index in range(finished_widget.count()):
            item = finished_widget.item(index)
            if not item:
                continue
            folder_name = item.data(Qt.UserRole) or ""
            for match in PICTURE_DAY_ID_RE.findall(folder_name):
                normalized = match.upper()
                if normalized not in seen:
                    seen.add(normalized)
                    picture_day_ids.append(normalized)
        picture_day_ids.sort()
        return picture_day_ids

    def handle_yearbook_import_clicked(self):
        if self.yearbook_import_process and self.yearbook_import_process.poll() is None:
            QMessageBox.information(self, "Yearbook Import", "An import is already running.")
            return

        startup = self._resolve_yearbook_import_startup()
        if startup is None:
            QMessageBox.warning(
                self,
                "Yearbook Import",
                "No Yearbook import startup was found.\n\n"
                "Expected one of:\n"
                "tools\\yearbook\\YearbookImportCli.exe\n"
                "tools\\yearbook\\YearbookImport.exe\n"
                "..\\YearbookImport\\run_ui.py\n"
                "..\\YearbookImport\\run_cli.py",
            )
            return

        cmd, cwd = startup
        self.photodeck_logs = []
        if self.photodeck_status:
            self.photodeck_status.clear()
            self.photodeck_status.show()
        if self.photodeck_button:
            self.photodeck_button.setEnabled(False)

        self.append_yearbook_status("Starting Yearbook import...")

        env = os.environ.copy()
        env.setdefault("YEARBOOK_IMPORT_ROOT", r"T:\DAMY YEARBOOK")
        if not env.get("YEARBOOK_IMPORT_DATA_DIR"):
            credentials_fallback = Path(__file__).resolve().parents[4] / "folder_manager" / "calendar_import_v3"
            if (credentials_fallback / "credentials.json").is_file():
                env["YEARBOOK_IMPORT_DATA_DIR"] = str(credentials_fallback)
        try:
            self.yearbook_import_process = subprocess.Popen(cmd, cwd=cwd, env=env)
        except Exception as exc:
            self.yearbook_import_process = None
            if self.photodeck_button:
                self.photodeck_button.setEnabled(True)
            QMessageBox.critical(self, "Yearbook Import", f"Failed to start import:\n{exc}")
            return

        self.append_yearbook_status(f"Launch command: {' '.join(cmd)}")
        self.yearbook_import_timer.start()

    def _resolve_yearbook_import_startup(self) -> Optional[Tuple[List[str], str]]:
        if getattr(sys, "frozen", False):
            app_root = Path(sys.executable).resolve().parent
        else:
            app_root = Path(__file__).resolve().parents[4]

        exe_candidates = [
            app_root / "tools" / "yearbook" / "YearbookImportCli.exe",
            app_root / "tools" / "yearbook" / "YearbookImport.exe",
        ]
        for exe_path in exe_candidates:
            if exe_path.is_file():
                return [str(exe_path)], str(exe_path.parent)

        if not getattr(sys, "frozen", False):
            workspace_root = Path(__file__).resolve().parents[5]
            yearbook_root = workspace_root / "YearbookImport"
            run_cli = yearbook_root / "run_cli.py"
            run_ui = yearbook_root / "run_ui.py"
            preference = (os.environ.get("DAMY_YEARBOOK_STARTUP") or "").strip().lower()
            if preference in {"cli", "run_cli"}:
                ordered_candidates = [run_cli, run_ui]
            else:
                ordered_candidates = [run_ui, run_cli]
            for script_path in ordered_candidates:
                if script_path.is_file():
                    python_exe = Path(sys.executable)
                    if script_path.name.lower() == "run_ui.py":
                        pythonw_exe = python_exe.with_name("pythonw.exe")
                        if pythonw_exe.is_file():
                            python_exe = pythonw_exe
                    return [str(python_exe), str(script_path)], str(yearbook_root)

        return None

    def append_yearbook_status(self, message: str):
        if not message:
            return
        self.photodeck_logs.append(message)
        if len(self.photodeck_logs) > 500:
            self.photodeck_logs = self.photodeck_logs[-500:]
        if self.photodeck_status:
            if not self.photodeck_status.isVisible():
                self.photodeck_status.show()
            self.photodeck_status.appendPlainText(message)
            scrollbar = self.photodeck_status.verticalScrollBar()
            if scrollbar:
                scrollbar.setValue(scrollbar.maximum())

    def _poll_yearbook_import_process(self):
        process = self.yearbook_import_process
        if process is None:
            self.yearbook_import_timer.stop()
            return
        exit_code = process.poll()
        if exit_code is None:
            return

        self.yearbook_import_timer.stop()
        self.yearbook_import_process = None
        if self.photodeck_button:
            self.photodeck_button.setEnabled(True)

        if exit_code == 0:
            self.append_yearbook_status("Yearbook import complete.")
            QMessageBox.information(self, "Yearbook Import", "Yearbook import complete.")
            self.reload_ui()
            return

        self.append_yearbook_status(f"Yearbook import exited with code {exit_code}.")
        QMessageBox.warning(
            self,
            "Yearbook Import",
            f"Yearbook import exited with code {exit_code}.",
        )

    def handle_move_to_edit_clicked(self):
        confirmation = QMessageBox.question(
            self,
            "Move to Edit",
            "Have you already sent the previous orders to edit before moving another set?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirmation != QMessageBox.Yes:
            QMessageBox.information(
                self,
                "Move to Edit",
                "Send the current orders to edit before moving a new set.",
            )
            return

        password, ok = QInputDialog.getText(
            self,
            "Move to Edit",
            "Enter password to confirm:",
            QLineEdit.Password,
        )
        if not ok:
            return

        if password.strip().upper() != "LIZA":
            QMessageBox.warning(
                self,
                "Move to Edit",
                "Incorrect password. Move to Edit cancelled.",
            )
            return

        self._start_progress(self.send_to_edit_progress)
        try:
            source_widget = self.list_widgets_by_day.get("6. Order Import")
            if not source_widget:
                QMessageBox.information(
                    self,
                    "Move to Edit",
                    "No '6. Order Import' column found.",
                )
                return

            folder_infos: List[Tuple[str, str]] = []
            for i in range(source_widget.count()):
                item = source_widget.item(i)
                if not item:
                    continue
                folder_name = item.data(Qt.UserRole)
                if not folder_name:
                    continue
                folder_path = self._resolve_existing_folder_path_for_open(str(folder_name))
                if folder_path:
                    folder_infos.append((str(folder_name), folder_path))

            if not folder_infos:
                QMessageBox.information(
                    self,
                    "Move to Edit",
                    "No order folders found to move.",
                )
                return

            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
            target_folder_name = "Send To Edit"
            target_folder_path = self._runtime_send_to_edit_dir()
            try:
                def _copy_to_edit_runtime() -> int:
                    total_files_local = 0
                    os.makedirs(target_folder_path, exist_ok=True)
                    for existing_entry in os.listdir(target_folder_path):
                        existing_path = os.path.join(target_folder_path, existing_entry)
                        if os.path.isfile(existing_path):
                            _, ext = os.path.splitext(existing_entry)
                            if ext.lower() in IMAGE_EXTENSIONS:
                                os.remove(existing_path)
                        elif os.path.isdir(existing_path):
                            shutil.rmtree(existing_path)

                    for folder_name, folder_path in folder_infos:
                        for entry in os.listdir(folder_path):
                            entry_path = os.path.join(folder_path, entry)
                            if not os.path.isfile(entry_path):
                                continue
                            _, ext = os.path.splitext(entry)
                            if ext.lower() not in IMAGE_EXTENSIONS:
                                continue
                            shutil.copy2(entry_path, os.path.join(target_folder_path, entry))
                            total_files_local += 1
                    return total_files_local

                total_files = int(
                    self._run_blocking_io_task(
                        "Move to Edit",
                        "Preparing Send To Edit folder...\nPlease wait.",
                        _copy_to_edit_runtime,
                    )
                    or 0
                )
            except Exception as exc:
                QMessageBox.warning(self, "Move to Edit", f"Failed while preparing Send to Edit:\n{exc}")
                return

            errors: List[str] = []
            for folder_name, _ in folder_infos:
                try:
                    db_item = self.db.get_item_by_disk_name(folder_name, domain=self.workflow_domain)
                    if db_item is not None:
                        marker_id = int(db_item.id)
                        self.db.update_domain_stage(
                            marker_id,
                            domain=self.workflow_domain,
                            stage=7,
                        )
                        self._mark_item_moved(marker_id)
                    else:
                        marker_id = int(
                            self.db.upsert_into_domain(
                                disk_name=folder_name,
                                domain=self.workflow_domain,
                                stage=7,
                            )
                        )
                        self._mark_item_moved(marker_id)
                except Exception as exc:  # pylint: disable=broad-except
                    errors.append(f"{folder_name}: {exc}")

            if errors:
                QMessageBox.warning(
                    self,
                    "Move to Edit",
                    "Some rows could not be moved to stage 7:\n" + "\n".join(errors[:15]),
                )

            file_word = "image" if total_files == 1 else "images"
            status_label = f"{target_folder_name} ({timestamp} • {total_files} {file_word})"
            self.pending_send_to_edit_entry = (status_label, target_folder_path)

            self.reload_ui()
        finally:
            self._finish_progress(self.send_to_edit_progress)

    def handle_returned_from_edit_clicked(self):
        directory = QFileDialog.getExistingDirectory(
            self,
            "Select Edited Images Folder",
            self.base_dir,
        )
        if not directory:
            return

        edit_index = self._build_edit_folder_match_index()
        try:
            def _copy_returned_from_edit() -> Tuple[Dict[str, List[str]], List[str], int]:
                processed_local: Dict[str, List[str]] = {}
                missing_local: List[str] = []
                total_copied_local = 0
                entries = os.listdir(directory)
                for entry in entries:
                    source_path = os.path.join(directory, entry)
                    if not os.path.isfile(source_path):
                        continue
                    base_name, ext = os.path.splitext(entry)
                    if ext.lower() not in IMAGE_EXTENSIONS:
                        continue

                    target_folder = self._find_edit_folder_for_base_with_index(base_name, edit_index)
                    if not target_folder:
                        missing_local.append(entry)
                        continue

                    dest_folder_path = os.path.join(self.base_dir, target_folder)
                    os.makedirs(dest_folder_path, exist_ok=True)

                    edited_name = f"{base_name} Edited{ext}"
                    dest_path = os.path.join(dest_folder_path, edited_name)
                    if os.path.exists(dest_path):
                        os.remove(dest_path)
                    shutil.copy2(source_path, dest_path)
                    processed_local.setdefault(target_folder, []).append(edited_name)
                    total_copied_local += 1
                return processed_local, missing_local, total_copied_local

            processed, missing, total_copied = self._run_blocking_io_task(
                "Returned From Edit",
                "Copying edited files into DAMY folders...\nPlease wait.",
                _copy_returned_from_edit,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Returned From Edit", f"Failed to process edited files:\n{exc}")
            return

        moved_stage_folders: List[str] = []
        for folder_name in processed:
            try:
                db_item = self.db.get_item_by_disk_name(folder_name, domain=self.workflow_domain)
                if db_item is not None:
                    marker_id = int(db_item.id)
                    self.db.update_domain_stage(
                        marker_id,
                        domain=self.workflow_domain,
                        stage=8,
                    )
                    self._mark_item_moved(marker_id)
                    moved_stage_folders.append(folder_name)
                else:
                    marker_id = int(
                        self.db.upsert_into_domain(
                            disk_name=folder_name,
                            domain=self.workflow_domain,
                            stage=8,
                        )
                    )
                    self._mark_item_moved(marker_id)
                    moved_stage_folders.append(folder_name)
            except Exception as exc:  # pylint: disable=broad-except
                QMessageBox.warning(
                    self,
                    "Returned From Edit",
                    f"Failed to move DB stage for {folder_name}:\n{exc}",
                )
                return

        summary_lines: List[str] = []
        if total_copied:
            summary_lines.append(f"Copied {total_copied} edited image(s) into {len(processed)} folder(s).")
        if moved_stage_folders:
            summary_lines.append(
                "Moved to stage 8 (Print): " + ", ".join(moved_stage_folders)
            )
        if missing:
            summary_lines.append(
                "No matching folder for: " + ", ".join(sorted(missing))
            )

        if summary_lines:
            QMessageBox.information(self, "Returned From Edit", "\n".join(summary_lines))
        else:
            QMessageBox.information(self, "Returned From Edit", "No matching edited images were processed.")

        self.pending_send_to_edit_entry = self._build_existing_send_to_edit_entry()
        self.reload_ui()

    def handle_sort_clicked(self):
        if not self._ensure_fs_mutation_allowed("2. Sort"):
            return

        resolved = self._resolve_stage2_anchor_target("2. Sort")
        if resolved is None:
            return
        disk_name, folder_path = resolved

        source_folder = QFileDialog.getExistingDirectory(
            self,
            "Choose Folder to Sort",
            folder_path,
        )
        source_folder = str(source_folder or "").strip()
        if not source_folder:
            return
        if not os.path.isdir(source_folder):
            QMessageBox.warning(self, "2. Sort", f"Selected folder does not exist:\n{source_folder}")
            return

        output_name = _stage2_proof_output_name(disk_name)
        output_folder = os.path.join(folder_path, output_name)
        use_temp_output = _is_same_or_inside_path(source_folder, output_folder)
        temp_output_folder = os.path.join(folder_path, f".{output_name}.sorting_tmp")

        try:
            def run_stage2_sort_task():
                sort_output_folder = temp_output_folder if use_temp_output else output_folder
                cleanup_except = output_folder if use_temp_output else source_folder
                if use_temp_output and os.path.isdir(temp_output_folder):
                    shutil.rmtree(temp_output_folder)
                removed_outputs = _remove_stage2_sort_outputs(folder_path, disk_name, except_path=cleanup_except)
                sort_results = sort_folders(
                    [source_folder],
                    output_folder=sort_output_folder,
                    replace_output=True,
                    generic_password=os.getenv("DAMY_PROOFING_GENERIC_PASSWORD", ""),
                    make_covers=True,
                    copy_files=False,
                    dry_run=False,
                    auto_password=False,
                )
                moved = sum(int(getattr(row, "moved_count", 0) or 0) for row in sort_results)
                source_removed = False
                if moved > 0 and not _is_same_path(source_folder, folder_path) and os.path.isdir(source_folder):
                    shutil.rmtree(source_folder)
                    source_removed = True
                if moved > 0 and use_temp_output:
                    if os.path.isdir(output_folder):
                        shutil.rmtree(output_folder)
                    if os.path.isdir(temp_output_folder):
                        shutil.move(temp_output_folder, output_folder)
                return sort_results, removed_outputs, source_removed

            results = self._run_blocking_io_task(
                "2. Sort",
                "Sorting proofs and replacing the Proof output folder...\nPlease wait.",
                run_stage2_sort_task,
            )
        except Exception as exc:
            QMessageBox.warning(self, "2. Sort", f"Sorting failed:\n{exc}")
            return
        results, removed_outputs, source_removed = results

        total_students = sum(int(getattr(row, "student_count", 0) or 0) for row in results)
        total_files = sum(int(getattr(row, "file_count", 0) or 0) for row in results)
        total_moved = sum(int(getattr(row, "moved_count", 0) or 0) for row in results)
        skipped = [row for row in results if getattr(row, "skipped_reason", None)]
        summary_lines = [
            f"Processed folders: {len(results)}",
            f"Source folder: {source_folder}",
            f"Output folder: {output_folder}",
            f"Student buckets: {total_students}",
            f"Matched image files: {total_files}",
            f"Moved files: {total_moved}",
        ]
        if removed_outputs:
            summary_lines.append(f"Deleted old output folders: {len(removed_outputs)}")
        if source_removed:
            summary_lines.append("Deleted original source folder.")
        if skipped:
            summary_lines.append(f"Folders without images: {len(skipped)}")
        moved_to_stage3 = False
        if total_files > 0 and total_moved > 0:
            moved_to_stage3 = self._advance_disk_name_to_stage(disk_name, 3, "2. Sort")
            if moved_to_stage3:
                summary_lines.append("Auto moved to stage 3 (Upload & PDFs).")
        QMessageBox.information(self, "2. Sort", "\n".join(summary_lines))
        if moved_to_stage3:
            self.reload_ui()

    def handle_print_clicked(self):
        self._start_progress(self.print_progress)
        try:
            source_widget = self.list_widgets_by_day.get("8. Print")
            if not source_widget:
                QMessageBox.information(
                    self,
                    "Prepare Print",
                    "No '8. Print' column found.",
                )
                return

            folder_infos: List[Tuple[str, str]] = []
            for i in range(source_widget.count()):
                item = source_widget.item(i)
                if not item:
                    continue
                folder_name = item.data(Qt.UserRole)
                if not folder_name:
                    continue
                folder_path = self._resolve_existing_folder_path_for_open(str(folder_name))
                if folder_path:
                    folder_infos.append((str(folder_name), folder_path))

            if not folder_infos:
                QMessageBox.information(
                    self,
                    "Prepare Print",
                    "No print folders available to process.",
                )
                return

            target_root_path = self._runtime_print_dir()
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
            try:
                def _prepare_print_runtime() -> Tuple[int, int, Dict[str, int], Optional[str], Optional[str]]:
                    if os.path.exists(target_root_path):
                        shutil.rmtree(target_root_path)
                    os.makedirs(target_root_path, exist_ok=True)

                    processed_images_local = 0
                    pb_copies_local = 0
                    package_counts_local: Dict[str, int] = {}
                    pdf_entries: List[Tuple[Tuple, Tuple, str, str]] = []

                    for folder_name, folder_path in folder_infos:
                        try:
                            entries = os.listdir(folder_path)
                        except OSError:
                            continue

                        for entry in entries:
                            source_path = os.path.join(folder_path, entry)
                            if not os.path.isfile(source_path):
                                continue
                            base_name, ext = os.path.splitext(entry)
                            lower_ext = ext.lower()
                            if lower_ext in {'.pdf'}:
                                metadata = get_pdf_metadata(source_path)
                                package_key = metadata.get('package_sort_key')
                                if not isinstance(package_key, tuple):
                                    package_key = (999, '', metadata.get('package_sort_key') or entry)
                                sequence_key = metadata.get('sequence_sort_key')
                                if not isinstance(sequence_key, tuple):
                                    sequence_key = (10**12, metadata.get('sequence_sort_key') or entry)
                                original_id = metadata.get('original_id') or entry
                                pdf_entries.append((package_key, sequence_key, original_id, source_path))
                                continue

                            if lower_ext not in IMAGE_EXTENSIONS:
                                continue
                            if not base_name.lower().endswith(' edited'):
                                continue

                            processed_images_local += 1

                            if re.search(r'\bPB\b', base_name, re.IGNORECASE):
                                pb_folder = os.path.join(target_root_path, 'PB')
                                os.makedirs(pb_folder, exist_ok=True)
                                pb_dest = os.path.join(pb_folder, entry)
                                if os.path.exists(pb_dest):
                                    os.remove(pb_dest)
                                shutil.copy2(source_path, pb_dest)
                                pb_copies_local += 1

                            for package in self._extract_plus_packages(base_name):
                                folder_label = _sanitize_folder_name(package)
                                if not folder_label:
                                    continue
                                package_folder = os.path.join(target_root_path, folder_label)
                                os.makedirs(package_folder, exist_ok=True)
                                package_dest = os.path.join(package_folder, entry)
                                if os.path.exists(package_dest):
                                    os.remove(package_dest)
                                shutil.copy2(source_path, package_dest)
                                package_counts_local[folder_label] = package_counts_local.get(folder_label, 0) + 1

                    combined_pdf_name: Optional[str] = None
                    combine_error: Optional[str] = None
                    if pdf_entries:
                        pdf_entries.sort(key=lambda item: (item[0], item[1], item[2]))
                        combined_pdf_name = f"{timestamp} Combined Orders.pdf"
                        combined_pdf_path = os.path.join(target_root_path, combined_pdf_name)
                        ordered_paths = [entry[-1] for entry in pdf_entries]
                        try:
                            combine_pdfs(ordered_paths, combined_pdf_path)
                        except Exception as exc:  # pylint: disable=broad-except
                            combine_error = str(exc)

                    return (
                        processed_images_local,
                        pb_copies_local,
                        package_counts_local,
                        combined_pdf_name,
                        combine_error,
                    )

                (
                    processed_images,
                    pb_copies,
                    package_counts,
                    combined_pdf_name,
                    combine_error,
                ) = self._run_blocking_io_task(
                    "Prepare Print",
                    "Preparing print folders and package outputs...\nPlease wait.",
                    _prepare_print_runtime,
                )
            except Exception as exc:
                QMessageBox.warning(self, "Prepare Print", f"Failed while preparing print folders:\n{exc}")
                return

            file_word = "image" if processed_images == 1 else "images"
            pb_word = "copy" if pb_copies == 1 else "copies"
            summary_lines = [f"Processed {processed_images} edited {file_word}."]
            if pb_copies:
                summary_lines.append(f"PB {pb_copies} {pb_word} created.")
            if package_counts:
                package_summaries = ", ".join(
                    f"{name} ({count})" for name, count in sorted(package_counts.items())
                )
                summary_lines.append(f"Packages: {package_summaries}.")

            if combined_pdf_name and not combine_error:
                summary_lines.append(f"Combined PDF created: {combined_pdf_name}")
            elif combine_error:
                summary_lines.append(f"Failed to combine PDFs: {combine_error}")

            QMessageBox.information(self, "Prepare Print", "\n".join(summary_lines))

            status_label = f"Print ({timestamp} • {processed_images} {file_word})"
            self.pending_print_entry = (status_label, target_root_path)
            self.reload_ui()
        finally:
            self._finish_progress(self.print_progress)

    def handle_move_to_package_clicked(self):
        self._start_progress(self.package_progress)
        try:
            source_widget = self.list_widgets_by_day.get("8. Print")
            if not source_widget:
                QMessageBox.information(
                    self,
                    "Move to Package",
                    "No '8. Print' column found.",
                )
                return

            moved_any = False
            errors: List[str] = []

            for i in range(source_widget.count()):
                item = source_widget.item(i)
                if not item:
                    continue
                folder_name = item.data(Qt.UserRole)
                if not folder_name:
                    continue
                folder_path = self._resolve_existing_folder_path_for_open(str(folder_name))
                if not folder_path:
                    continue

                try:
                    db_item = self.db.get_item_by_disk_name(folder_name, domain=self.workflow_domain)
                    if db_item is not None:
                        marker_id = int(db_item.id)
                        self.db.update_domain_stage(
                            marker_id,
                            domain=self.workflow_domain,
                            stage=9,
                        )
                        self._mark_item_moved(marker_id)
                    else:
                        marker_id = int(
                            self.db.upsert_into_domain(
                                disk_name=folder_name,
                                domain=self.workflow_domain,
                                stage=9,
                            )
                        )
                        self._mark_item_moved(marker_id)
                    moved_any = True
                except Exception as exc:  # pylint: disable=broad-except
                    errors.append(f"{folder_name}: {exc}")

            if errors:
                QMessageBox.warning(
                    self,
                    "Move to Package",
                    "Some folders could not be moved to stage 9:\n" + "\n".join(errors),
                )
            elif moved_any:
                QMessageBox.information(
                    self,
                    "Move to Package",
                    "All print folders were moved to stage 9 (Package).",
                )
            else:
                QMessageBox.information(
                    self,
                    "Move to Package",
                    "No folders were moved.",
                )

            self.reload_ui()
        finally:
            self._finish_progress(self.package_progress)

    def _start_progress(self, bar: Optional[QProgressBar]) -> None:
        if not bar:
            return
        bar.show()
        bar.setRange(0, 0)
        bar.setValue(0)
        QCoreApplication.processEvents()

    def _finish_progress(self, bar: Optional[QProgressBar]) -> None:
        if not bar:
            return
        bar.setRange(0, 1)
        bar.setValue(1)
        bar.hide()
        QCoreApplication.processEvents()

    def _run_blocking_io_task(
        self,
        title: str,
        message: str,
        task: Callable[[], object],
        *,
        cancel_event: Optional[threading.Event] = None,
        cancel_text: str = "Stop",
    ) -> object:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setModal(True)
        dialog.setMinimumWidth(420)
        dialog.setWindowFlag(Qt.WindowCloseButtonHint, False)
        layout = QVBoxLayout(dialog)
        label = QLabel(message)
        label.setWordWrap(True)
        progress = QProgressBar()
        progress.setRange(0, 0)
        progress.setTextVisible(False)
        layout.addWidget(label)
        layout.addWidget(progress)
        if cancel_event is not None:
            stop_button = QPushButton(cancel_text)
            layout.addWidget(stop_button)

            def request_stop() -> None:
                cancel_event.set()
                stop_button.setEnabled(False)
                label.setText(f"{message}\n\nStopping after current upload finishes...")

            stop_button.clicked.connect(request_stop)

        worker = _IoTaskThread(task)
        worker.finished.connect(dialog.accept)
        worker.start()
        try:
            dialog.exec()
        finally:
            worker.wait()

        if worker.error is not None:
            if isinstance(worker.error, UserCancelled):
                raise worker.error
            detail = str(worker.error)
            if worker.traceback_text:
                detail = f"{detail}\n\n{worker.traceback_text}"
            raise RuntimeError(detail)
        return worker.result

    def _list_edit_candidate_folders(self) -> List[str]:
        try:
            stage_rows = self.db.list_by_domain_stage(self.workflow_domain, 7)
        except Exception:
            stage_rows = []
        candidates: List[str] = [str(getattr(row, "disk_name", "") or "").strip() for row in stage_rows]
        candidates = [name for name in candidates if name]
        if candidates:
            return candidates
        try:
            return [
                entry
                for entry in os.listdir(self.base_dir)
                if os.path.isdir(os.path.join(self.base_dir, entry))
            ]
        except OSError:
            return []

    def _build_edit_folder_match_index(self) -> List[Tuple[str, str]]:
        index: List[Tuple[str, str]] = []
        for entry in self._list_edit_candidate_folders():
            entry_path = os.path.join(self.base_dir, entry)
            if not os.path.isdir(entry_path):
                continue
            suffix = entry.strip()
            for day in DAYS:
                extracted = _extract_day_suffix(entry, day)
                if extracted and extracted != entry:
                    suffix = extracted
                    break
            suffix_norm = _normalize_token(suffix)
            if suffix_norm:
                index.append((entry, suffix_norm))
        return index

    def _find_edit_folder_for_base_with_index(
        self,
        base_name: str,
        index: List[Tuple[str, str]],
    ) -> Optional[str]:
        if not base_name:
            return None

        candidate_norms = {_normalize_token(base_name)}
        candidate_norms.add(_normalize_token(base_name.split(' - ', 1)[0]))
        space_split = base_name.split()[0] if base_name.split() else ''
        candidate_norms.add(_normalize_token(space_split))
        first_token_match = re.match(r'\s*([A-Za-z0-9_]+)', base_name)
        if first_token_match:
            candidate_norms.add(_normalize_token(first_token_match.group(1)))
        candidate_norms.discard('')
        if not candidate_norms:
            return None

        best_match: Optional[str] = None
        fallback_match: Optional[str] = None
        for entry, suffix_norm in index:
            for candidate_norm in candidate_norms:
                if suffix_norm == candidate_norm:
                    return entry
                if candidate_norm.startswith(suffix_norm) or suffix_norm.startswith(candidate_norm):
                    return entry
                if fallback_match is None and (
                    candidate_norm in suffix_norm or suffix_norm in candidate_norm
                ):
                    fallback_match = entry
            if best_match is None and any(
                suffix_norm.startswith(candidate_norm) for candidate_norm in candidate_norms
            ):
                best_match = entry
        return best_match or fallback_match

    def _find_edit_folder_for_base(self, base_name: str) -> Optional[str]:
        return self._find_edit_folder_for_base_with_index(base_name, self._build_edit_folder_match_index())

    def _runtime_workspace_root(self) -> str:
        domain_key = re.sub(r'[^a-z0-9_-]+', '_', str(self.workflow_domain or "yearbook").strip().lower())
        domain_key = domain_key or "yearbook"
        return os.path.join(self.base_dir, "_workflow_runtime", domain_key)

    def _runtime_send_to_edit_dir(self) -> str:
        return os.path.join(self._runtime_workspace_root(), "send_to_edit")

    def _runtime_print_dir(self) -> str:
        return os.path.join(self._runtime_workspace_root(), "print")

    def _build_existing_send_to_edit_entry(self) -> Optional[Tuple[str, str]]:
        folder_path = self._runtime_send_to_edit_dir()
        if not os.path.isdir(folder_path):
            return None
        try:
            entries = os.listdir(folder_path)
        except OSError:
            return None

        total_images = 0
        latest_mtime: Optional[float] = None

        for entry in entries:
            entry_path = os.path.join(folder_path, entry)
            if not os.path.isfile(entry_path):
                continue
            _, ext = os.path.splitext(entry)
            if ext.lower() not in IMAGE_EXTENSIONS:
                continue
            total_images += 1
            try:
                entry_mtime = os.path.getmtime(entry_path)
            except OSError:
                continue
            if latest_mtime is None or entry_mtime > latest_mtime:
                latest_mtime = entry_mtime

        if latest_mtime is None:
            try:
                latest_mtime = os.path.getmtime(folder_path)
            except OSError:
                latest_mtime = datetime.now().timestamp()

        timestamp = datetime.fromtimestamp(latest_mtime).strftime("%Y-%m-%d_%H-%M")
        file_word = "image" if total_images == 1 else "images"
        label = f"Send To Edit ({timestamp} • {total_images} {file_word})"
        return (label, folder_path)

    def _extract_plus_packages(self, base_name: str) -> List[str]:
        packages: List[str] = []
        if '+' not in base_name:
            return packages
        segments = base_name.split('+')[1:]
        for segment in segments:
            candidate = segment.strip()
            if not candidate:
                continue
            if ' -' in candidate:
                candidate = candidate.split(' -', 1)[0].strip()
            if candidate:
                packages.append(candidate)
        return packages

    def _build_existing_print_entry(self) -> Optional[Tuple[str, str]]:
        folder_path = self._runtime_print_dir()
        if not os.path.isdir(folder_path):
            return None

        total_images = 0
        latest_mtime: Optional[float] = None

        for current_dir, _, files in os.walk(folder_path):
            for filename in files:
                _, ext = os.path.splitext(filename)
                if ext.lower() not in IMAGE_EXTENSIONS:
                    continue
                total_images += 1
                file_path = os.path.join(current_dir, filename)
                try:
                    entry_mtime = os.path.getmtime(file_path)
                except OSError:
                    continue
                if latest_mtime is None or entry_mtime > latest_mtime:
                    latest_mtime = entry_mtime

        if latest_mtime is None:
            try:
                latest_mtime = os.path.getmtime(folder_path)
            except OSError:
                latest_mtime = datetime.now().timestamp()

        timestamp = datetime.fromtimestamp(latest_mtime).strftime("%Y-%m-%d_%H-%M")
        file_word = "image" if total_images == 1 else "images"
        label = f"Print ({timestamp} • {total_images} {file_word})"
        return (label, folder_path)

    def append_send_to_edit_status(self, label: str, path: str):
        if not label or not path:
            return
        if self.send_to_edit_status:
            self.send_to_edit_status.show()
            self.send_to_edit_status.clear()
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, path)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.send_to_edit_status.addItem(item)
        self.send_to_edit_paths = {label: path}

    def open_send_to_edit_folder(self, item: QListWidgetItem):
        if not item:
            return
        path = item.data(Qt.UserRole)
        if not path:
            path = self.send_to_edit_paths.get(item.text().strip(), '')
        if not path or not os.path.exists(path):
            QMessageBox.warning(
                self,
                "Move to Edit",
                "Could not locate the selected Send to Edit folder.",
            )
            return
        try:
            if sys.platform.startswith('win'):
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                opener = 'xdg-open' if sys.platform.startswith('linux') else 'open'
                subprocess.Popen([opener, path])
        except Exception as exc:  # pylint: disable=broad-except
            QMessageBox.warning(
                self,
                "Move to Edit",
                f"Unable to open folder:\n{path}\n{exc}",
            )

    def append_print_status(self, label: str, path: str):
        if not label or not path:
            return
        if self.print_status:
            self.print_status.show()
            self.print_status.clear()
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, path)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.print_status.addItem(item)
        self.print_paths = {label: path}

    def open_print_folder(self, item: QListWidgetItem):
        if not item:
            return
        path = item.data(Qt.UserRole)
        if not path:
            path = self.print_paths.get(item.text().strip(), '')
        if not path or not os.path.exists(path):
            QMessageBox.warning(
                self,
                "Prepare Print",
                "Could not locate the selected Print folder.",
            )
            return
        try:
            if sys.platform.startswith('win'):
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                opener = 'xdg-open' if sys.platform.startswith('linux') else 'open'
                subprocess.Popen([opener, path])
        except Exception as exc:  # pylint: disable:broad-except
            QMessageBox.warning(
                self,
                "Prepare Print",
                f"Unable to open folder:\n{path}\n{exc}",
            )

    def _set_item_flag_state(self, item: QListWidgetItem, *, has_i: bool, has_g: bool) -> None:
        item.setData(ROLE_FLAG_I, bool(has_i))
        item.setData(ROLE_FLAG_G, bool(has_g))

    def _read_item_flag_state(self, item: QListWidgetItem) -> Tuple[bool, bool]:
        has_i_raw = item.data(ROLE_FLAG_I)
        has_g_raw = item.data(ROLE_FLAG_G)
        if has_i_raw is not None and has_g_raw is not None:
            return bool(has_i_raw), bool(has_g_raw)
        item_id = item.data(Qt.UserRole + 1)
        if item_id is None:
            return False, False
        try:
            db_item = self.db.get_item_by_id(int(item_id))
            has_i = bool(getattr(db_item, "flag_i", False)) if db_item is not None else False
            has_g = bool(getattr(db_item, "flag_g", False)) if db_item is not None else False
            self._set_item_flag_state(item, has_i=has_i, has_g=has_g)
            return has_i, has_g
        except Exception:
            return False, False

    def _schedule_sync_ig_checkboxes(self) -> None:
        if self._sync_ig_timer.isActive():
            self._sync_ig_timer.stop()
        self._sync_ig_timer.start(0)

    def sync_ig_checkboxes(self):
        if not hasattr(self, "edit_widgets"):
            return
        lw_edit, lw_i, lw_g = self.edit_widgets
        for i in range(lw_edit.count()):
            item = lw_edit.item(i)
            if item is None:
                continue
            has_i, has_g = self._read_item_flag_state(item)
            if i >= lw_i.count() or i >= lw_g.count():
                continue
            lw_i.blockSignals(True)
            lw_g.blockSignals(True)
            lw_i_item = lw_i.item(i)
            lw_g_item = lw_g.item(i)
            if lw_i_item:
                lw_i_item.setCheckState(Qt.Checked if has_i else Qt.Unchecked)
                lw_i_item.setText("✅" if has_i else "I")
            if lw_g_item:
                lw_g_item.setCheckState(Qt.Checked if has_g else Qt.Unchecked)
                lw_g_item.setText("✅" if has_g else "G")
            lw_i.blockSignals(False)
            lw_g.blockSignals(False)

    def handle_i_checkbox(self, item):
        self.on_checkbox_change(item, "I")

    def handle_g_checkbox(self, item):
        self.on_checkbox_change(item, "G")

    def remove_edit_checkboxes(self, item_id: int):
        lw_edit, lw_i, lw_g = self.edit_widgets
        for i in range(lw_edit.count()):
            item = lw_edit.item(i)
            raw = item.data(Qt.UserRole + 1)
            try:
                row_item_id = int(raw)
            except Exception:
                continue
            if int(item_id) == row_item_id:
                lw_edit.takeItem(i)
                lw_i.takeItem(i)
                lw_g.takeItem(i)
                return

    def on_checkbox_change(self, item, label):
        if not hasattr(self, "edit_widgets"):
            return
        lw_edit, lw_i, lw_g = self.edit_widgets
        index = lw_i.row(item) if self.sender() == lw_i else lw_g.row(item)
        if index >= lw_edit.count():
            return
        item.setText("✅" if item.checkState() == Qt.Checked else label)
        row_item = lw_edit.item(index)
        if row_item is None:
            return
        item_id = row_item.data(Qt.UserRole + 1)
        if item_id is None:
            return
        new_i = bool(lw_i.item(index) and lw_i.item(index).checkState() == Qt.Checked)
        new_g = bool(lw_g.item(index) and lw_g.item(index).checkState() == Qt.Checked)
        try:
            self.db.update_flags(int(item_id), flag_i=new_i, flag_g=new_g)
        except Exception as exc:
            QMessageBox.warning(self, "Edit Flags", f"Failed to update I/G flags in DB:\n{exc}")
            return
        self._set_item_flag_state(row_item, has_i=new_i, has_g=new_g)
        self.sync_ig_checkboxes()

    def perform_search(self, text: str):
        text = text.strip().lower()
        for col_widget, lw in self.column_widgets:
            any_visible = False
            if hasattr(self, "edit_widgets") and lw == self.edit_widgets[0]:
                lw_edit, lw_i, lw_g = self.edit_widgets
                for i in range(lw_edit.count()):
                    item = lw_edit.item(i)
                    match = not text or text in item.text().lower()
                    item.setHidden(not match)
                    lw_i.item(i).setHidden(not match)
                    lw_g.item(i).setHidden(not match)
                    if match:
                        any_visible = True
                col_widget.setVisible(any_visible or not text)
            else:
                for i in range(lw.count()):
                    item = lw.item(i)
                    match = not text or text in item.text().lower()
                    item.setHidden(not match)
                    if match:
                        any_visible = True
                col_widget.setVisible(any_visible or not text)
