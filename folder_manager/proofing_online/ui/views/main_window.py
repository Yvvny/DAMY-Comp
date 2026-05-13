import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel,
    QSizePolicy, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QPlainTextEdit, QFileDialog, QInputDialog,
    QProgressBar, QDialog, QFrame, QSplitter, QAbstractItemView,
    QTableWidget, QTableWidgetItem, QHeaderView,
)
from PySide6.QtCore import Qt, QObject, QThread, Signal, QCoreApplication, QTimer, QSettings, QElapsedTimer, QEvent
from PySide6.QtGui import QBrush, QColor, QImage, QKeySequence
from googleapiclient.errors import HttpError

from ...order_import.config import ORDER_SOURCES
from ...order_import.exceptions import NoOrdersFoundError
from ...order_import.file_manager import (
    _background_from_proof_id,
    _child_name_from_proof_path,
    _portrait_background_token,
    find_matching_subdir,
    find_originals_subdir,
    find_proofs_subdir,
)
from ...order_import.gmail_client import (
    ensure_label_exists,
    get_gmail_service,
    list_label_ids_by_name,
    modify_message_labels,
    send_email_with_attachments,
)
from ...order_import.processing import process_picture_day
from ...order_import.utils import emit_status
from ..widgets.drag_list_widget import DragListWidget, ROLE_DB_ID
from folder_manager.config import DB_HOST, DB_NAME, DB_USER, DB_PASS, DB_PORT
from folder_manager.db import DB, WORKFLOW_DOMAIN_PROOFING, parse_contact_fields_from_note
from folder_manager.photodeck_upload.workflow import UserCancelled, run_stage3_bulk_upload, run_stage3_create_pdfs
from folder_manager.proofing_online.path_resolver import ProofingPathResolver, is_same_path, is_same_or_inside_path
from folder_manager.proofing_online.workflow_config import (
    PROOFING_DAYS,
    PROOFING_DAY_TO_STAGE,
    PROOFING_DAY_PREFIX_ALIASES,
    PROOFING_EDIT_STAGE,
    PROOFING_PRINT_STAGE,
    PROOFING_PACKAGE_STAGE,
    PROOFING_DELIVER_STAGE,
    detect_day_from_folder_name,
    extract_day_suffix,
    matches_day,
    stage_for_day,
)
from folder_manager.proofing_online.workflow_errors import DeveloperError, UserFacingError, friendly_parent_delivery_error, split_user_error
from folder_manager.proofing_online.workflow_services import (
    build_stage4_school_email_draft,
    build_stage3_upload_plan,
    build_stage2_sort_plan,
    execute_stage2_sort_plan,
    extract_school_name,
    first_email_from_text,
    infer_stage3_upload_id,
    parent_delivery_pdf_status,
    sanitize_folder_name,
    stage2_proof_output_name,
    stage2_source_conflicts_existing_output,
    stage2_sort_output_names,
    stage3_gallery_name_from_paths,
    summarize_stage3_pdf_result,
    summarize_stage3_upload_result,
)
from folder_manager.sms.child_info import prepare_child_info_assets
from folder_manager.sms.cloudflare_r2 import missing_r2_settings
from folder_manager.ui.child_mms_dialog import ChildMmsDialog
from folder_manager.ui.cloudflare_dialogs import load_r2_settings
from folder_manager.ui.drag_list_widget import ContactEditorDialog

PICTURE_DAY_ID_RE = re.compile(r'\b[PH]\d{7,8}\b', re.IGNORECASE)
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp', '.gif'}
ROLE_FLAG_I = Qt.UserRole + 2
ROLE_FLAG_G = Qt.UserRole + 3
PHOTODECK_IMPORT_EXIT_CANCELLED = 41
PHOTODECK_IMPORT_SUMMARY_MARKER = "__PHOTODECK_IMPORT_SUMMARY__"
DAYS = PROOFING_DAYS
DAY_TO_STAGE = PROOFING_DAY_TO_STAGE
DAY_PREFIX_ALIASES = PROOFING_DAY_PREFIX_ALIASES


def _normalize_token(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', value.lower())


def _stage2_proof_output_name(disk_name: str) -> str:
    return stage2_proof_output_name(disk_name)


def _stage2_sort_output_names(disk_name: str) -> Set[str]:
    return stage2_sort_output_names(disk_name)


def _is_same_or_inside_path(path: str, parent: str) -> bool:
    return is_same_or_inside_path(path, parent)


def _is_same_path(left: str, right: str) -> bool:
    return is_same_path(left, right)


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
    return matches_day(folder_name, day)


def _extract_day_suffix(folder_name: str, day: str) -> str:
    return extract_day_suffix(folder_name, day)


_STAGE_PREFIX_TEXT_RE = re.compile(r"^\s*\d+\s*[\.\-_) ]+\s*(.*)$")


def _strip_stage_prefix_text(value: str) -> str:
    text = str(value or "").strip()
    match = _STAGE_PREFIX_TEXT_RE.match(text)
    if not match:
        return text
    return str(match.group(1) or "").strip() or text


def _stage3_gallery_name_from_paths(work_root: str, folder_path: str, disk_name: str) -> str:
    return stage3_gallery_name_from_paths(work_root, folder_path, disk_name)


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
    return detect_day_from_folder_name(folder_name)


def _stage_for_day(day: str) -> Optional[int]:
    return stage_for_day(day)

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
        workflow_domain: str = WORKFLOW_DOMAIN_PROOFING,
    ):
        super().__init__()
        self.base_dir = base_dir
        self.workflow_domain = (workflow_domain or WORKFLOW_DOMAIN_PROOFING).strip().lower() or WORKFLOW_DOMAIN_PROOFING
        self.db = db or DB(host=db_host, dbname=dbname, user=user, password=password, port=port)
        self.source_base_dir: Optional[str] = (os.environ.get("DAMY_SOURCE_BASE_DIR") or "").strip() or None
        self.path_resolver = ProofingPathResolver(
            self.base_dir,
            source_base_dir=self.source_base_dir or "",
            domain=self.workflow_domain,
        )
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
        self.workflow_asset_school_email_status_label: Optional[QLabel] = None
        self.workflow_asset_parent_delivery_button: Optional[QPushButton] = None
        self.workflow_asset_sort_section: Optional[QWidget] = None
        self.workflow_asset_stage3_section: Optional[QWidget] = None
        self.workflow_asset_stage4_section: Optional[QWidget] = None
        self.workflow_asset_stage3_splitter: Optional[QSplitter] = None
        self.workflow_asset_stage3_pdfs_path: str = ""
        self.workflow_asset_stage3_pdf_card: Optional[Stage3PdfAssetCard] = None
        self.photodeck_button: Optional[QPushButton] = None
        self.photodeck_status: Optional[QPlainTextEdit] = None
        self.photodeck_process = None
        self.photodeck_dialog: Optional[QDialog] = None
        self.photodeck_status_label: Optional[QLabel] = None
        self.photodeck_timer: Optional[QTimer] = None
        self.photodeck_elapsed: Optional[QElapsedTimer] = None
        self.photodeck_launching = False
        self.photodeck_log_path: Optional[str] = None
        self.photodeck_log_handle = None
        self.photodeck_cancel_token_path: Optional[str] = None
        self.photodeck_log_read_offset = 0
        self.photodeck_logs: List[str] = []
        self.photodeck_active_ids: List[str] = []
        self.photodeck_had_fatal_error = False
        self.send_to_edit_status: Optional[QTableWidget] = None
        self.send_to_edit_progress: Optional[QProgressBar] = None
        self.pending_send_to_edit_entries: List[Dict[str, str]] = self._load_paid_order_asset_entries()
        self.print_status: Optional[QListWidget] = None
        self.stage7_package_asset_status: Optional[QListWidget] = None
        self.stage7_class_photo_status_label: Optional[QLabel] = None
        self.print_paths: Dict[str, str] = {}
        self.pending_stage7_asset_entries: List[Dict[str, str]] = self._load_stage7_paid_asset_entries()
        self.stage7_class_photo_entries: List[Dict[str, str]] = self._load_stage7_class_photo_entries()
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
        if event.matches(QKeySequence.SelectAll):
            target = self.focusWidget()
            if target is self.send_to_edit_status and self.send_to_edit_status is not None:
                self.send_to_edit_status.selectAll()
                event.accept()
                return
            if target is self.stage7_package_asset_status and self.stage7_package_asset_status is not None:
                self.stage7_package_asset_status.selectAll()
                event.accept()
                return
        if event.key() == Qt.Key_F5:
            self.reload_ui()
        else:
            super().keyPressEvent(event)

    def _is_stage6_paid_asset_drag_source(self, source: object) -> bool:
        return self.send_to_edit_status is not None and source in {
            self.send_to_edit_status,
            self.send_to_edit_status.viewport(),
        }

    def _is_stage7_paid_asset_drag_source(self, source: object) -> bool:
        return self.stage7_package_asset_status is not None and source in {
            self.stage7_package_asset_status,
            self.stage7_package_asset_status.viewport(),
        }

    def _stage7_paid_asset_drop_targets(self) -> Set[object]:
        targets: Set[object] = set()
        for widget in (self.print_status, self.stage7_package_asset_status):
            if widget is None:
                continue
            targets.add(widget)
            targets.add(widget.viewport())
        return targets

    def _stage6_paid_asset_drop_targets(self) -> Set[object]:
        targets: Set[object] = set()
        for widget in (self.send_to_edit_status,):
            if widget is None:
                continue
            targets.add(widget)
            targets.add(widget.viewport())
        return targets

    def _paid_asset_drop_targets(self) -> Set[object]:
        return self._stage7_paid_asset_drop_targets() | self._stage6_paid_asset_drop_targets()

    def eventFilter(self, watched, event):  # type: ignore[override]
        if (
            watched in self._paid_asset_drop_targets()
            and event.type() in {QEvent.DragEnter, QEvent.DragMove, QEvent.Drop}
            and isinstance(event.source(), DragListWidget)
        ):
            event.ignore()
            return True
        if (
            self.send_to_edit_status is not None
            and watched in {self.send_to_edit_status, self.send_to_edit_status.viewport()}
            and event.type() in {QEvent.DragEnter, QEvent.DragMove, QEvent.Drop}
            and self._is_stage6_paid_asset_drag_source(event.source())
        ):
            event.ignore()
            return True
        if (
            watched in self._stage6_paid_asset_drop_targets()
            and event.type() in {QEvent.DragEnter, QEvent.DragMove, QEvent.Drop}
            and self._is_stage6_paid_asset_drag_source(event.source())
        ):
            event.ignore()
            return True
        if (
            watched in self._stage7_paid_asset_drop_targets()
            and event.type() in {QEvent.DragEnter, QEvent.DragMove, QEvent.Drop}
            and self._is_stage7_paid_asset_drag_source(event.source())
        ):
            event.ignore()
            return True
        if (
            self.print_status is not None
            and self.send_to_edit_status is not None
            and watched in self._stage7_paid_asset_drop_targets()
            and event.type() in {QEvent.DragEnter, QEvent.DragMove}
        ):
            if self._is_stage6_paid_asset_drag_source(event.source()):
                event.setDropAction(Qt.MoveAction)
                event.accept()
                return True
        if (
            self.print_status is not None
            and self.send_to_edit_status is not None
            and watched in self._stage7_paid_asset_drop_targets()
            and event.type() == QEvent.Drop
        ):
            if self._is_stage6_paid_asset_drag_source(event.source()):
                event.setDropAction(Qt.MoveAction)
                event.accept()
                self.move_selected_original_paid_assets_to_stage7()
                return True
        if (
            self.stage7_package_asset_status is not None
            and watched in self._stage6_paid_asset_drop_targets()
            and event.type() in {QEvent.DragEnter, QEvent.DragMove}
        ):
            if self._is_stage7_paid_asset_drag_source(event.source()) and self._selected_stage7_original_entries():
                event.setDropAction(Qt.MoveAction)
                event.accept()
                return True
        if (
            self.stage7_package_asset_status is not None
            and watched in self._stage6_paid_asset_drop_targets()
            and event.type() == QEvent.Drop
        ):
            if self._is_stage7_paid_asset_drag_source(event.source()) and self._selected_stage7_original_entries():
                event.setDropAction(Qt.MoveAction)
                event.accept()
                self.restore_selected_stage7_assets_to_stage6()
                return True
        if (
            self.send_to_edit_status is not None
            and watched in {self.send_to_edit_status, self.send_to_edit_status.viewport()}
            and event.type() == QEvent.KeyPress
        ):
            if event.matches(QKeySequence.SelectAll):
                self.send_to_edit_status.selectAll()
                return True
            if event.key() in {Qt.Key_Return, Qt.Key_Enter}:
                self.open_selected_send_to_edit_assets()
                return True
        if (
            self.stage7_package_asset_status is not None
            and watched in {self.stage7_package_asset_status, self.stage7_package_asset_status.viewport()}
            and event.type() == QEvent.KeyPress
        ):
            if event.matches(QKeySequence.SelectAll):
                self.stage7_package_asset_status.selectAll()
                return True
            if event.key() in {Qt.Key_Return, Qt.Key_Enter}:
                self.open_selected_stage7_assets()
                return True
        return super().eventFilter(watched, event)

    def _column_size_settings(self) -> QSettings:
        return QSettings("DAMYComp", "DAMYComp")

    def _column_splitter_size_key(self) -> str:
        domain = (self.workflow_domain or "proofing").strip().lower() or "proofing"
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
        self.send_to_edit_status = None
        self.send_to_edit_progress = None
        self.print_status = None
        self.stage7_package_asset_status = None
        self.stage7_class_photo_status_label = None
        self.print_paths = {}
        if not self.pending_send_to_edit_entries:
            self.pending_send_to_edit_entries = self._load_paid_order_asset_entries()
        if not self.pending_stage7_asset_entries:
            self.pending_stage7_asset_entries = self._load_stage7_paid_asset_entries()

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

                if day == "6. Edit":
                    self.photodeck_button = QPushButton("Import Paid Orders")
                    self.photodeck_button.clicked.connect(self.handle_photodeck_import_clicked)
                    column_layout.addWidget(self.photodeck_button)

                    self.send_to_edit_progress = QProgressBar()
                    self.send_to_edit_progress.setRange(0, 1)
                    self.send_to_edit_progress.setValue(1)
                    self.send_to_edit_progress.hide()
                    column_layout.addWidget(self.send_to_edit_progress)

                    self.send_to_edit_status = QTableWidget()
                    self.send_to_edit_status.setColumnCount(9)
                    self.send_to_edit_status.setHorizontalHeaderLabels(
                        ["Child", "Type", "ID", "Package", "Add-ons", "DE", "Background", "Qty", "File"]
                    )
                    self.send_to_edit_status.setSelectionBehavior(QAbstractItemView.SelectRows)
                    self.send_to_edit_status.setSelectionMode(QAbstractItemView.ExtendedSelection)
                    self.send_to_edit_status.setEditTriggers(QAbstractItemView.NoEditTriggers)
                    self.send_to_edit_status.setFocusPolicy(Qt.StrongFocus)
                    self.send_to_edit_status.setDragEnabled(True)
                    self.send_to_edit_status.setAcceptDrops(True)
                    self.send_to_edit_status.setDropIndicatorShown(True)
                    self.send_to_edit_status.setDragDropMode(QAbstractItemView.DragDrop)
                    self.send_to_edit_status.setDefaultDropAction(Qt.MoveAction)
                    self.send_to_edit_status.verticalHeader().hide()
                    self.send_to_edit_status.horizontalHeader().setStretchLastSection(True)
                    self.send_to_edit_status.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
                    self.send_to_edit_status.horizontalHeader().setSectionResizeMode(8, QHeaderView.Stretch)
                    self.send_to_edit_status.setAlternatingRowColors(True)
                    self.send_to_edit_status.installEventFilter(self)
                    self.send_to_edit_status.viewport().installEventFilter(self)
                    self.send_to_edit_status.itemDoubleClicked.connect(self.open_send_to_edit_folder)
                    self.send_to_edit_status.show()
                    column_layout.addWidget(self.send_to_edit_status)

                if day == "7. Print":
                    self.stage7_class_photo_status_label = QLabel("")
                    self.stage7_class_photo_status_label.setAlignment(Qt.AlignCenter)
                    self.stage7_class_photo_status_label.setStyleSheet(
                        "QLabel { color: #ffd166; font-weight: 600; padding: 2px 0; }"
                    )
                    self.stage7_class_photo_status_label.hide()
                    column_layout.addWidget(self.stage7_class_photo_status_label)

                    self.print_status = QListWidget()
                    self.print_status.setSelectionMode(QListWidget.SingleSelection)
                    self.print_status.setFocusPolicy(Qt.StrongFocus)
                    self.print_status.setAcceptDrops(True)
                    self.print_status.setDropIndicatorShown(True)
                    self.print_status.setDragDropMode(QAbstractItemView.DropOnly)
                    self.print_status.installEventFilter(self)
                    self.print_status.viewport().installEventFilter(self)
                    self.print_status.itemSelectionChanged.connect(self.refresh_stage7_package_asset_list)
                    self.print_status.show()
                    column_layout.addWidget(self.print_status)

                    self.stage7_package_asset_status = QListWidget()
                    self.stage7_package_asset_status.setSelectionMode(QListWidget.ExtendedSelection)
                    self.stage7_package_asset_status.setFocusPolicy(Qt.StrongFocus)
                    self.stage7_package_asset_status.setDragEnabled(True)
                    self.stage7_package_asset_status.setDragDropMode(QAbstractItemView.DragOnly)
                    self.stage7_package_asset_status.setDefaultDropAction(Qt.MoveAction)
                    self.stage7_package_asset_status.installEventFilter(self)
                    self.stage7_package_asset_status.viewport().installEventFilter(self)
                    self.stage7_package_asset_status.itemDoubleClicked.connect(self.open_print_folder)
                    self.stage7_package_asset_status.show()
                    column_layout.addWidget(self.stage7_package_asset_status)

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
                if day in {"6. Edit", "7. Print"}:
                    lw.setParent(column)
                    lw.hide()
                else:
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
        if self.pending_send_to_edit_entries and self.send_to_edit_status:
            self.set_send_to_edit_status_entries(self.pending_send_to_edit_entries)
            self.pending_send_to_edit_entries = []
        if self.print_status:
            self.set_stage7_paid_asset_entries(self.pending_stage7_asset_entries)
        self._reapply_current_search()

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

    def _reapply_current_search(self) -> None:
        search_widget = getattr(self, "search_input", None)
        text = search_widget.text() if search_widget is not None else ""
        if str(text or "").strip():
            self.perform_search(str(text or ""))

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._resize_workflow_asset_host()

    def is_busy(self) -> tuple[bool, str]:
        proc = getattr(self, "photodeck_process", None)
        if proc is not None:
            try:
                if proc.poll() is None:
                    return True, "PhotoDeck Import"
            except Exception:
                pass
        if getattr(self, "photodeck_launching", False):
            return True, "PhotoDeck Import"
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
            "QLabel#workflowStatusLabel { background: #172332; border: 1px solid #35516d; border-radius: 6px;"
            " padding: 6px 8px; color: #b8d8f6; font-size: 11px; }"
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

        school_email_status_label = QLabel("School email: Not sent")
        school_email_status_label.setObjectName("workflowStatusLabel")
        school_email_status_label.setWordWrap(True)
        school_group_layout.addWidget(school_email_status_label)

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
        self.workflow_asset_school_email_status_label = school_email_status_label
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
        school_email_status_label = self.workflow_asset_school_email_status_label
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
            or school_email_status_label is None
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
            disk_name = str(anchor_item.data(Qt.UserRole) or "").strip()
            row = self._get_or_create_row_for_disk_name(disk_name, stage=4) if disk_name else None
            school_email_status_label.setText(self._format_school_email_status(row))
            send_button.setText("Send School Email Again" if self._school_email_was_sent(row) else "Send School Email")
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
            self._show_workflow_error(
                title,
                what_happened="The folder does not exist.",
                checked=path,
                next_step="Refresh the board, then try opening the folder again.",
            )
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                opener = "xdg-open" if sys.platform.startswith("linux") else "open"
                subprocess.Popen([opener, path])
        except Exception as exc:
            user_error, technical_detail = self._split_user_error(exc)
            self._show_workflow_error(
                title,
                what_happened=f"The app could not open the folder: {user_error}",
                checked=path,
                next_step="Open the folder manually in File Explorer, or refresh the board and try again.",
                technical_detail=technical_detail,
            )

    def _resolve_nested_stage_folder_path(
        self,
        root_dir: str,
        stage_folder_name: str,
        disk_name: str,
    ) -> Optional[str]:
        return self.path_resolver.resolve_nested_stage_folder_path(root_dir, stage_folder_name, disk_name)

    def _resolve_existing_folder_path_for_open(self, disk_name: str) -> Optional[str]:
        return self.path_resolver.resolve_existing_folder_path_for_open(disk_name)

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
            self._show_workflow_error(
                action_title,
                what_happened="The selected stage-3 row has no disk name.",
                next_step="Refresh the board, then select the folder again.",
            )
            return None
        folder_path = self._resolve_existing_folder_path_for_open(disk_name)
        if not folder_path:
            self._show_workflow_error(
                action_title,
                what_happened="The selected folder does not exist in the active workspace.",
                checked=disk_name,
                next_step="Refresh the board or restore the folder, then try again.",
            )
            return None
        return disk_name, folder_path

    def _resolve_stage3_work_root(self, folder_path: str) -> str:
        return self.path_resolver.stage3_work_root(folder_path)

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
            self._show_workflow_error(
                action_title,
                what_happened="The selected stage-4 row has no disk name.",
                next_step="Refresh the board, then select the folder again.",
            )
            return None
        folder_path = self._resolve_existing_folder_path_for_open(disk_name)
        if not folder_path:
            self._show_workflow_error(
                action_title,
                what_happened="The selected folder does not exist in the active workspace.",
                checked=disk_name,
                next_step="Refresh the board or restore the folder, then try again.",
            )
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
            self._show_workflow_error(
                action_title,
                what_happened="The selected stage-2 row has no disk name.",
                next_step="Refresh the board, then select the folder again.",
            )
            return None
        folder_path = os.path.join(self.base_dir, disk_name)
        if not os.path.isdir(folder_path):
            self._show_workflow_error(
                action_title,
                what_happened="The selected folder does not exist in the active workspace.",
                checked=folder_path,
                next_step="Refresh the board or restore the folder, then try again.",
            )
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
            user_error, technical_detail = self._split_user_error(exc)
            self._show_workflow_error(
                action_title,
                what_happened="The action completed, but the app could not update this folder's stage in the database.",
                checked=name,
                next_step="Refresh the board. If the folder is still in the old stage, move it manually and try again.",
                technical_detail=technical_detail or user_error,
            )
            return False

    def _split_user_error(self, error: object) -> tuple[str, str]:
        return split_user_error(error)

    def _show_workflow_error(
        self,
        title: str,
        *,
        what_happened: str,
        checked: str = "",
        next_step: str = "",
        technical_detail: str = "",
    ) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(f"What happened:\n{str(what_happened or '').strip() or 'The action failed.'}")
        info_parts: List[str] = []
        if str(checked or "").strip():
            info_parts.append(f"Checked:\n{str(checked).strip()}")
        if str(next_step or "").strip():
            info_parts.append(f"Next step:\n{str(next_step).strip()}")
        if info_parts:
            box.setInformativeText("\n\n".join(info_parts))
        detail = str(technical_detail or "").strip()
        if detail:
            box.setDetailedText(detail)
        box.exec()

    def handle_workflow_asset_primary_clicked(self) -> None:
        day = str(self.workflow_asset_anchor_day or "").strip()
        if day == "2. Sort":
            self.handle_sort_clicked()
            return
        QMessageBox.information(self, "Workflow", "Select a folder in 2. Sort first.")

    def _infer_stage3_upload_id(self, disk_name: str) -> str:
        return infer_stage3_upload_id(disk_name)

    def _show_stage3_upload_checklist(
        self,
        *,
        folder: str,
        source_root: str = "",
        picture_day_id: str = "",
        gallery_name: str = "",
        uploaded_count: Optional[int] = None,
        pdfs_path: str = "",
        pdf_count: Optional[int] = None,
        parent_count: Optional[int] = None,
        gallery_uuid: str = "",
        upload_status: str = "pending",
        pdf_status: str = "pending",
        parent_status: str = "pending",
        move_status: str = "pending",
        needs_fix: Optional[List[str]] = None,
        warnings: Optional[List[str]] = None,
        technical_details: Optional[List[str]] = None,
    ) -> None:
        def _status_text(status: str) -> str:
            labels = {
                "done": "Done",
                "attention": "Needs attention",
                "pending": "Not started",
                "skipped": "Skipped",
            }
            return labels.get(status, "Not started")

        def _line(label: str, status: str, detail: str = "") -> str:
            suffix = f" ({detail})" if detail else ""
            return f"- {_status_text(status)}: {label}{suffix}"

        fix_lines = [str(item).strip() for item in (needs_fix or []) if str(item).strip()]
        warning_lines = [str(item).strip() for item in (warnings or []) if str(item).strip()]
        is_attention = bool(fix_lines) or any(
            status == "attention" for status in (upload_status, pdf_status, parent_status, move_status)
        )

        lines = ["Stage 3 needs attention" if is_attention else "Stage 3 finished"]
        lines.extend(["", "Completed:"])
        completed_count = 0
        if upload_status == "done":
            detail = f"{uploaded_count} file(s)" if uploaded_count is not None else ""
            lines.append(f"- PhotoDeck upload{f' ({detail})' if detail else ''}")
            completed_count += 1
        if pdf_status == "done":
            detail = f"{pdf_count} PDF(s)" if pdf_count is not None else ""
            lines.append(f"- PDF packet creation{f' ({detail})' if detail else ''}")
            completed_count += 1
        if parent_status == "done":
            detail = f"{parent_count} child row(s)" if parent_count is not None else ""
            lines.append(f"- Parent Delivery setup{f' ({detail})' if detail else ''}")
            completed_count += 1
        if move_status == "done":
            lines.append("- Moved to Stage 4")
            completed_count += 1
        if completed_count == 0:
            lines.append("- No step completed yet")

        remaining_lines = [
            _line("PhotoDeck upload", upload_status, f"{uploaded_count} file(s)" if uploaded_count is not None else ""),
            _line("PDF packet creation", pdf_status, f"{pdf_count} PDF(s)" if pdf_count is not None else ""),
            _line("Parent Delivery setup", parent_status, f"{parent_count} child row(s)" if parent_count is not None else ""),
            _line("Move to Stage 4", move_status),
        ]

        if fix_lines:
            lines.extend(["", "What needs fixing:", f"- {fix_lines[0]}"])
            if len(fix_lines) > 1:
                lines.extend(["", "Next step:"])
                lines.extend(f"- {item}" for item in fix_lines[1:])
        elif is_attention:
            lines.extend(["", "Next step:", "- Review the steps marked Needs attention, then run Stage 3 again."])
        else:
            lines.extend(["", "Next step:", "- Open Stage 4 Parent Delivery if you need to import parent contacts or send messages."])

        if warning_lines:
            lines.extend(["", "Warnings:"])
            lines.extend(f"- {item}" for item in warning_lines)

        detail_lines = ["Step status:"]
        detail_lines.extend(remaining_lines)
        detail_lines.append("")
        if folder:
            detail_lines.append(f"Folder: {folder}")
        if source_root:
            detail_lines.append(f"Source root: {source_root}")
        if picture_day_id:
            detail_lines.append(f"Picture Day ID: {picture_day_id}")
        if gallery_name:
            detail_lines.append(f"Gallery name: {gallery_name}")
        if pdfs_path:
            detail_lines.append(f"PDF folder: {pdfs_path}")
        if gallery_uuid:
            detail_lines.append(f"Gallery UUID: {gallery_uuid}")
        detail_lines_for_box = [line for line in detail_lines if str(line).strip()]
        detail_lines_for_box.extend(str(item).strip() for item in (technical_details or []) if str(item).strip())

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning if is_attention else QMessageBox.Icon.Information)
        box.setWindowTitle("Stage 3 Upload")
        box.setText("\n".join(lines))
        if detail_lines_for_box:
            box.setDetailedText("\n".join(detail_lines_for_box))
        box.exec()

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
            self._show_workflow_error(
                "Stage 3 Upload",
                what_happened="The selected upload folder does not exist.",
                checked=work_root,
                next_step="Choose an existing sorted proof folder, then run Stage 3 Upload again.",
            )
            return
        upload_plan = build_stage3_upload_plan(disk_name, folder_path, work_root)
        self._remember_photodeck_upload_source(disk_name, upload_plan.picture_day_id, upload_plan.work_root)

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
                    root_folder=upload_plan.work_root,
                    picture_day_id=upload_plan.picture_day_id,
                    gallery_name=upload_plan.gallery_name,
                    pricing_key="PRICING_PROFILE",
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
            user_error, technical_detail = self._split_user_error(exc)
            self._show_stage3_upload_checklist(
                folder=disk_name,
                source_root=upload_plan.work_root,
                picture_day_id=upload_plan.picture_day_id,
                gallery_name=upload_plan.gallery_name,
                upload_status="attention",
                pdf_status="pending",
                parent_status="pending",
                move_status="pending",
                needs_fix=[
                    f"PhotoDeck upload failed: {user_error}",
                    "Fix the upload issue, then run Stage 3 Upload again.",
                ],
                technical_details=[technical_detail] if technical_detail else [],
            )
            return

        upload_summary = summarize_stage3_upload_result(result, upload_plan)

        try:
            pdf_result = self._run_blocking_io_task(
                "Stage 3 Create PDFs",
                "Upload finished. Generating PDF packets...\nPlease wait.",
                lambda: run_stage3_create_pdfs(
                    root_folder=upload_plan.work_root,
                    pdf_output_root=folder_path,
                    gallery_name=upload_summary.gallery_name or upload_plan.gallery_name,
                ),
            )
        except Exception as exc:
            user_error, technical_detail = self._split_user_error(exc)
            self._show_stage3_upload_checklist(
                folder=disk_name,
                source_root=upload_summary.source_root,
                picture_day_id=upload_plan.picture_day_id,
                gallery_name=upload_summary.gallery_name or upload_plan.gallery_name,
                uploaded_count=upload_summary.uploaded_count,
                gallery_uuid=upload_summary.gallery_uuid,
                upload_status="done",
                pdf_status="attention",
                parent_status="pending",
                move_status="pending",
                needs_fix=[
                    f"PDF creation failed: {user_error}",
                    "Fix the PDF creation issue, then run Stage 3 Upload again.",
                ],
                technical_details=[technical_detail] if technical_detail else [],
            )
            return

        pdf_summary = summarize_stage3_pdf_result(pdf_result, folder_path)
        pdfs_path = pdf_summary.pdfs_path
        pdf_count = pdf_summary.pdf_count
        saved_link, save_error = self._save_stage3_pdfs_link(disk_name, pdfs_path)
        self.workflow_asset_stage3_pdfs_path = pdfs_path
        self._refresh_workflow_asset_popup()

        child_mms_result: Optional[Dict[str, object]] = None
        child_mms_error = ""
        child_mms_next_step = ""
        child_mms_technical_error = ""
        r2_settings = load_r2_settings()
        missing_r2 = missing_r2_settings(r2_settings)
        if missing_r2:
            child_mms_error = "Cloudflare R2 not configured: " + ", ".join(missing_r2)
            child_mms_next_step = "Open Cloudflare R2 settings, fill the missing values, then retry Parent Delivery."
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
                child_mms_error, child_mms_next_step = friendly_parent_delivery_error(exc)
                _user_error, technical_detail = self._split_user_error(exc)
                child_mms_technical_error = technical_detail or str(exc or "").strip()

        warnings: List[str] = []
        needs_fix: List[str] = []
        if not saved_link:
            warnings.append(f"PDF link not saved to DB: {save_error or 'Unknown error'}")
        parent_status = "pending"
        parent_count: Optional[int] = None
        if child_mms_result is not None:
            parent_status = "done"
            parent_count = int(child_mms_result.get("record_count", 0) or 0)
        elif child_mms_error:
            parent_status = "attention"
            needs_fix.append(f"Parent Delivery not prepared: {child_mms_error}")
            if child_mms_next_step:
                needs_fix.append(child_mms_next_step)
            else:
                needs_fix.append("Open Stage 4 Parent Delivery after fixing the issue.")
        moved_to_stage4 = False
        move_status = "pending"
        if upload_summary.uploaded_count > 0:
            moved_to_stage4 = self._advance_disk_name_to_stage(disk_name, 4, "Stage 3 Upload")
            if moved_to_stage4:
                move_status = "done"
            else:
                move_status = "attention"
                needs_fix.append("Upload and PDFs finished, but the app could not move this folder to Stage 4. Move it manually or refresh and try again.")
        self._show_stage3_upload_checklist(
            folder=disk_name,
            source_root=upload_summary.source_root,
            picture_day_id=upload_plan.picture_day_id,
            gallery_name=upload_summary.gallery_name,
            uploaded_count=upload_summary.uploaded_count,
            pdfs_path=pdfs_path,
            pdf_count=pdf_count,
            parent_count=parent_count,
            gallery_uuid=upload_summary.gallery_uuid,
            upload_status="done",
            pdf_status="done",
            parent_status=parent_status,
            move_status=move_status,
            needs_fix=needs_fix,
            warnings=warnings,
            technical_details=[child_mms_technical_error] if child_mms_technical_error else [],
        )
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
            user_error, technical_detail = self._split_user_error(exc)
            self._show_workflow_error(
                "Stage 3 Create PDFs",
                what_happened=f"PDF creation failed: {user_error}",
                checked=f"Source root:\n{work_root}\n\nOutput folder:\n{folder_path}",
                next_step="Fix the PDF creation issue, then run Stage 3 Create PDFs again.",
                technical_detail=technical_detail,
            )
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
        box = QMessageBox(self)
        box.setWindowTitle("Stage 3 Create PDFs")
        box.setIcon(QMessageBox.Icon.Information if saved_link else QMessageBox.Icon.Warning)
        if saved_link:
            box.setText(
                "\n".join(
                    [
                        "PDF packets created",
                        "",
                        "Completed:",
                        f"- Generated {pdf_count} PDF(s)",
                        "- Saved the PDF folder for Stage 4",
                        "",
                        "Next step:",
                        "- Open Stage 4 School / Parent Delivery.",
                    ]
                )
            )
        else:
            box.setText(
                "\n".join(
                    [
                        "PDF packets created, but the app could not save the PDF folder.",
                        "",
                        "Completed:",
                        f"- Generated {pdf_count} PDF(s)",
                        "",
                        "Needs attention:",
                        "- Stage 4 may not automatically find this PDF folder.",
                        "",
                        "Next step:",
                        "- Choose or drop the PDF folder in Stage 3, then retry Stage 4.",
                    ]
                )
            )
        details = [
            f"Folder: {disk_name}",
            f"Source root: {work_root}",
            f"PDF folder: {pdfs_path}",
            save_line,
        ]
        box.setDetailedText("\n".join(details))
        box.exec()

    def handle_email_school_complete_asset_clicked(self) -> None:
        resolved = self._resolve_stage4_anchor_target("School Email Complete")
        if resolved is None:
            return
        disk_name, _folder_path = resolved
        QMessageBox.information(
            self,
            "School Email Complete",
            f"Stage unchanged. This folder remains in Stage 4:\n{disk_name}",
        )

    def _extract_school_name(self, display_name: str) -> str:
        return extract_school_name(display_name)

    def _first_email_from_text(self, raw_text: str) -> str:
        return first_email_from_text(raw_text)

    def _collect_pdf_files_from_path(self, raw_path: str) -> tuple[str, List[str]]:
        return self.path_resolver.collect_pdf_files_from_path(raw_path)

    def _stage3_pdf_candidates(self, disk_name: str, folder_path: str) -> List[str]:
        return self.path_resolver.stage3_pdf_candidates(
            disk_name,
            folder_path,
            read_saved_link=self._read_stage3_pdfs_link,
        )

    def _collect_stage3_pdf_files(self, disk_name: str, folder_path: str) -> tuple[str, List[str]]:
        return self.path_resolver.collect_stage3_pdf_files(
            disk_name,
            folder_path,
            read_saved_link=self._read_stage3_pdfs_link,
        )

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
            self._show_workflow_error(
                "Stage 3 PDF Asset",
                what_happened="The app could not save the linked PDF path.",
                checked=picked_path,
                next_step="Try choosing the PDFs folder again. If it still fails, check the database connection.",
                technical_detail=save_error,
            )
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
            self._show_workflow_error(
                "Stage 3 PDF Asset",
                what_happened="The dropped PDF path does not exist.",
                checked=dropped_path,
                next_step="Drop an existing PDF file or PDFs folder.",
            )
            return
        saved_link, save_error = self._save_stage3_pdfs_link(disk_name, dropped_path)
        if not saved_link:
            self._show_workflow_error(
                "Stage 3 PDF Asset",
                what_happened="The app could not save the linked PDF path.",
                checked=dropped_path,
                next_step="Try dropping the PDFs folder again. If it still fails, check the database connection.",
                technical_detail=save_error,
            )
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
            user_error, technical_detail = self._split_user_error(exc)
            self._show_workflow_error(
                "Stage 3 PDF Asset",
                what_happened=f"The app could not clear the linked PDF path: {user_error}",
                checked=disk_name,
                next_step="Refresh the board, then try clearing the link again.",
                technical_detail=technical_detail,
            )
            return
        self.workflow_asset_stage3_pdfs_path = ""
        self._refresh_workflow_asset_popup()

    def handle_stage3_open_linked_pdfs_asset_clicked(self) -> None:
        linked_path = str(self.workflow_asset_stage3_pdfs_path or "").strip()
        if not linked_path:
            QMessageBox.information(self, "Stage 3 PDF Asset", "No linked PDF path.")
            return
        if not os.path.exists(linked_path):
            self._show_workflow_error(
                "Stage 3 PDF Asset",
                what_happened="The saved linked PDF path does not exist.",
                checked=linked_path,
                next_step="Choose or drop the current PDFs folder again.",
            )
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(linked_path)  # type: ignore[attr-defined]
            else:
                opener = "xdg-open" if sys.platform.startswith("linux") else "open"
                subprocess.Popen([opener, linked_path])
        except Exception as exc:
            user_error, technical_detail = self._split_user_error(exc)
            self._show_workflow_error(
                "Stage 3 PDF Asset",
                what_happened=f"The app could not open the linked PDF path: {user_error}",
                checked=linked_path,
                next_step="Open the folder manually in File Explorer, or choose the current PDFs folder again.",
                technical_detail=technical_detail,
            )

    def _school_contact_context(self, row: object, disk_name: str) -> tuple[str, str, str, str]:
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
        return school_name, current_name, current_email, current_phone

    def _school_email_status(self, row: Optional[object]) -> tuple[Optional[object], str]:
        if row is None:
            return None, ""
        try:
            return self.db.get_school_email_status(int(row.id))
        except Exception:
            return None, ""

    def _school_email_was_sent(self, row: Optional[object]) -> bool:
        sent_at, _recipient = self._school_email_status(row)
        return sent_at is not None

    def _format_school_email_status(self, row: Optional[object]) -> str:
        sent_at, recipient = self._school_email_status(row)
        if sent_at is None:
            return "School email: Not sent"

        sent_text = ""
        try:
            sent_text = sent_at.strftime("%Y-%m-%d %I:%M %p")
        except Exception:
            sent_text = str(sent_at or "").strip()
        if recipient:
            return f"School email: Sent to {recipient}\n{sent_text}"
        return f"School email: Sent\n{sent_text}"

    def _open_school_contact_editor(
        self,
        row: object,
        disk_name: str,
        *,
        focus_email: bool = False,
        show_success: bool = True,
    ) -> Optional[object]:
        school_name, current_name, current_email, current_phone = self._school_contact_context(row, disk_name)
        dialog = ContactEditorDialog(
            self,
            school_name=school_name,
            contact_name=current_name,
            contact_email=current_email,
            contact_phone=current_phone,
        )
        if focus_email and hasattr(dialog, "email_edit"):
            dialog.email_edit.setFocus()
            dialog.email_edit.selectAll()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        new_name, new_email, new_phone = dialog.contact_values()
        previous_email = self._first_email_from_text(current_email)
        next_email = self._first_email_from_text(new_email or "")
        try:
            self.db.set_contact(
                int(row.id),
                contact_name=new_name,
                contact_email=new_email,
                contact_phone=new_phone,
            )
            if previous_email != next_email:
                self.db.clear_school_email_sent(int(row.id))
        except Exception as exc:
            user_error, technical_detail = self._split_user_error(exc)
            self._show_workflow_error(
                "School Contact",
                what_happened=f"The app could not save the school contact: {user_error}",
                checked=disk_name,
                next_step="Check the database connection, then save the contact again.",
                technical_detail=technical_detail,
            )
            return None
        if show_success:
            QMessageBox.information(self, "School Contact", f"Contact updated for:\n{disk_name}")
        self._refresh_workflow_asset_popup()
        try:
            return self.db.get_item_by_id(int(row.id)) or row
        except Exception:
            return row

    def handle_email_info_asset_clicked(self) -> None:
        resolved = self._resolve_stage4_anchor_target("School Contact")
        if resolved is None:
            return
        disk_name, _folder_path = resolved
        row = self._get_or_create_row_for_disk_name(disk_name, stage=4)
        if row is None:
            self._show_workflow_error(
                "School Contact",
                what_happened="The app could not load this folder's database row.",
                checked=disk_name,
                next_step="Refresh the board, then try School Contact again.",
            )
            return
        self._open_school_contact_editor(row, disk_name)

    def handle_send_asset_clicked(self) -> None:
        resolved = self._resolve_stage4_anchor_target("Send School Email")
        if resolved is None:
            return
        disk_name, folder_path = resolved

        row = self._get_or_create_row_for_disk_name(disk_name, stage=4)
        if row is None:
            self._show_workflow_error(
                "Send School Email",
                what_happened="The app could not load this folder's database row.",
                checked=disk_name,
                next_step="Refresh the board, then try Send School Email again.",
            )
            return

        display_name = (getattr(row, "display_name", None) or "").strip()
        if not display_name and self.workflow_asset_anchor_item is not None:
            display_name = str(self.workflow_asset_anchor_item.text() or "").strip()
        draft = build_stage4_school_email_draft(
            row=row,
            disk_name=disk_name,
            folder_path=folder_path,
            display_name=display_name,
            collect_stage3_pdf_files=self._collect_stage3_pdf_files,
            parse_note_fields=parse_contact_fields_from_note,
        )
        if not draft.to_email:
            updated_row = self._open_school_contact_editor(
                row,
                disk_name,
                focus_email=True,
                show_success=False,
            )
            if updated_row is None:
                return
            row = updated_row
            draft = build_stage4_school_email_draft(
                row=row,
                disk_name=disk_name,
                folder_path=folder_path,
                display_name=display_name,
                collect_stage3_pdf_files=self._collect_stage3_pdf_files,
                parse_note_fields=parse_contact_fields_from_note,
            )
            if not draft.to_email:
                self._show_workflow_error(
                    "Send School Email",
                    what_happened="School contact still has no valid email.",
                    checked=disk_name,
                    next_step="Open School Contact and enter a valid school email before sending the proof packet.",
                )
                return

        if not draft.pdf_files:
            self._show_workflow_error(
                "Send School Email",
                what_happened="School email needs the Stage 3 PDF packet, but no PDF files were found.",
                checked=draft.pdf_root,
                next_step="Run Stage 3 Upload & PDFs again, then retry Send School Email.",
            )
            return

        dialog = Stage4SendDialog(
            self,
            to_email=draft.to_email,
            subject=draft.subject,
            body_text=draft.body_text,
            attachment_paths=list(draft.pdf_files),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        to_value, subject_value, body_value = dialog.email_values()
        recipient = self._first_email_from_text(to_value)
        if not recipient:
            self._show_workflow_error(
                "Send School Email",
                what_happened="The recipient email in 'To' is not valid.",
                checked=to_value,
                next_step="Enter one valid recipient email, then send again.",
            )
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
                    attachment_paths=list(draft.pdf_files),
                ),
            )
        except Exception as exc:
            user_error, technical_detail = self._split_user_error(exc)
            self._show_workflow_error(
                "Send School Email",
                what_happened=f"Email send failed: {user_error}",
                checked=f"Recipient:\n{recipient}\n\nPDF folder:\n{draft.pdf_root}",
                next_step="Check Gmail authorization, internet connection, and attachment files, then try sending again.",
                technical_detail=technical_detail,
            )
            return

        status_warning = ""
        try:
            self.db.set_school_email_sent(int(row.id), recipient)
        except Exception as exc:
            user_error, technical_detail = self._split_user_error(exc)
            status_warning = f"School email was sent, but the app could not save the sent status: {user_error}"
            self._show_workflow_error(
                "Send School Email",
                what_happened=status_warning,
                checked=f"Recipient:\n{recipient}",
                next_step="Refresh the board. If the status still says Not sent, send status was not saved.",
                technical_detail=technical_detail,
            )

        summary_lines = [
            f"To: {recipient}",
            f"Attachments: {len(draft.pdf_files)} PDF(s)",
            f"PDF folder: {draft.pdf_root}",
            "School email status: Sent",
            "Stage unchanged: folder remains in Stage 4.",
        ]
        if status_warning:
            summary_lines.append(status_warning)
        if isinstance(send_result, dict):
            msg_id = str(send_result.get("id") or "").strip()
            if msg_id:
                summary_lines.append(f"Gmail message id: {msg_id}")
        QMessageBox.information(self, "Send School Email", "\n".join(summary_lines))
        self._refresh_workflow_asset_popup()

    def _ensure_parent_delivery_pdfs_ready(self, folder_path: str) -> bool:
        ready, pdf_root, _pdf_count = parent_delivery_pdf_status(folder_path, self._collect_pdf_files_from_path)
        if ready:
            return True

        self._show_workflow_error(
            "Parent Delivery Needs PDFs",
            what_happened="Parent Delivery needs the Stage 3 PDFs before it can build parent rows.",
            checked=pdf_root,
            next_step="Run Stage 3 Upload & PDFs again, then retry Parent Delivery.",
        )
        return False

    def _open_parent_delivery_dialog(self, *, start_mode: str = "info") -> None:
        resolved = self._resolve_stage4_anchor_target("Parent Delivery")
        if resolved is None:
            return
        disk_name, folder_path = resolved
        if not os.path.isdir(folder_path):
            self._show_workflow_error(
                "Parent Delivery",
                what_happened="The selected folder does not exist.",
                checked=folder_path,
                next_step="Refresh the board, then select the folder again.",
            )
            return
        row = self._get_or_create_row_for_disk_name(disk_name, stage=4)
        if not self._ensure_parent_delivery_pdfs_ready(folder_path):
            return
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
        self._show_workflow_error(
            title,
            what_happened=message.split("\n", 1)[0],
            next_step="Check the database connection, then refresh.",
            technical_detail=message,
        )

    def _build_rows_by_stage_cache(self) -> Dict[int, List]:
        grouped: Dict[int, List] = {}
        try:
            rows = self.db.list_by_domain(self.workflow_domain)
        except Exception:
            self._show_db_warning_once(
                "Workflow DB",
                "Could not read Proofing rows from DB.\n"
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

    def handle_photodeck_import_clicked(self):
        proc = self.photodeck_process
        if proc is not None:
            try:
                if proc.poll() is None:
                    QMessageBox.information(self, "PhotoDeck Import", "An import is already running.")
                    return
            except Exception:
                pass
        if self.photodeck_launching:
            QMessageBox.information(self, "PhotoDeck Import", "An import is already running.")
            return

        self.photodeck_active_ids = []
        self.photodeck_logs = []
        self.photodeck_had_fatal_error = False

        if self.photodeck_status:
            self.photodeck_status.clear()
            self.photodeck_status.show()
        if self.photodeck_button:
            self.photodeck_button.setEnabled(False)

        label_name = ORDER_SOURCES['photodeck'].get('gmail_label') or "PHOTODECK PAID ORDER"
        self.append_photodeck_status(
            f"Starting PhotoDeck import from Gmail label '{label_name}' using subject 'Your payment receipt'."
        )

        is_frozen = bool(getattr(sys, "frozen", False))
        root_dir = Path(__file__).resolve().parents[4]
        if is_frozen:
            root_dir = Path(sys.executable).resolve().parent

        self.photodeck_launching = True
        process_started = False
        try:
            if is_frozen:
                cmd = [
                    sys.executable,
                    "--photodeck-paid-import",
                ]
            else:
                cmd = [
                    sys.executable,
                    "-m",
                    "folder_manager.proofing_online.order_import.paid_runner",
                ]

            cancel_tmp = tempfile.NamedTemporaryFile(prefix="photodeck_import_cancel_", suffix=".token", delete=False)
            self.photodeck_cancel_token_path = cancel_tmp.name
            cancel_tmp.close()
            try:
                os.remove(self.photodeck_cancel_token_path)
            except Exception:
                pass
            cmd.extend(["--cancel-token-path", self.photodeck_cancel_token_path])
            cmd.extend(["--upload-source-manifest", self._photodeck_upload_sources_manifest_path()])

            log_tmp = tempfile.NamedTemporaryFile(prefix="photodeck_import_", suffix=".log", delete=False)
            self.photodeck_log_path = log_tmp.name
            log_tmp.close()
            self.photodeck_log_read_offset = 0
            self.photodeck_log_handle = open(self.photodeck_log_path, "w", encoding="utf-8")

            proc = subprocess.Popen(
                cmd,
                cwd=str(root_dir),
                stdout=self.photodeck_log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            process_started = True
            self.photodeck_process = proc
            self._show_photodeck_import_dialog(proc.pid or 0)
            self._start_photodeck_import_monitor()
        except Exception as exc:
            self.photodeck_had_fatal_error = True
            self.append_photodeck_status(f"Failed to start PhotoDeck import: {exc}")
            self._show_workflow_error(
                "PhotoDeck Import",
                what_happened="PhotoDeck Import could not start.",
                next_step="Close any running import window, then try again. If it still fails, open Details and send the message to support.",
                technical_detail=str(exc),
            )
        finally:
            self.photodeck_launching = False
            if not process_started:
                self._cleanup_photodeck_runtime_artifacts(remove_log=True)
                if self.photodeck_button:
                    self.photodeck_button.setEnabled(True)

    def append_photodeck_status(self, message: str):
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

    def handle_photodeck_fatal(self, message: str):
        self.photodeck_had_fatal_error = True
        self.append_photodeck_status(message)
        self._show_workflow_error(
            "PhotoDeck Import",
            what_happened="PhotoDeck Import could not continue.",
            checked=message,
            next_step="Fix the listed issue, then run PhotoDeck Import again.",
        )

    def handle_photodeck_missing(self, message: str):
        if not message:
            return
        self.append_photodeck_status(message)
        self._show_workflow_error(
            "PhotoDeck Import",
            what_happened="Some PhotoDeck import requirements are missing.",
            checked=message,
            next_step="Fix the listed requirements, then run PhotoDeck Import again.",
        )

    def handle_photodeck_finished(self):
        self.append_photodeck_status("PhotoDeck import complete.")

    def _show_photodeck_import_dialog(self, _pid: int):
        if self.photodeck_dialog and self.photodeck_dialog.isVisible():
            try:
                self.photodeck_dialog.raise_()
                self.photodeck_dialog.activateWindow()
            except Exception:
                pass
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("PhotoDeck Import Running")
        dlg.setModal(False)
        dlg.setMinimumWidth(380)

        layout = QVBoxLayout(dlg)
        status = QLabel("PhotoDeck Import is running...\nElapsed: 0s")
        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setTextVisible(False)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self._request_photodeck_cancel)

        layout.addWidget(status)
        layout.addWidget(bar)
        layout.addWidget(cancel_btn)
        dlg.show()

        self.photodeck_dialog = dlg
        self.photodeck_status_label = status
        self.photodeck_elapsed = QElapsedTimer()
        self.photodeck_elapsed.start()

    def _request_photodeck_cancel(self) -> None:
        token_path = (self.photodeck_cancel_token_path or "").strip()
        if not token_path:
            return
        try:
            Path(token_path).write_text("cancel", encoding="utf-8")
            self.append_photodeck_status("Cancel requested. Waiting for rollback...")
        except Exception as exc:
            self.append_photodeck_status(f"Failed to request cancel: {exc}")

    def _start_photodeck_import_monitor(self) -> None:
        if self.photodeck_timer is None:
            self.photodeck_timer = QTimer(self)
            self.photodeck_timer.timeout.connect(self._poll_photodeck_import_process_safe)
        self.photodeck_timer.start(500)

    def _poll_photodeck_import_process_safe(self) -> None:
        try:
            self._poll_photodeck_import_process()
        except Exception as exc:
            self.photodeck_had_fatal_error = True
            self.append_photodeck_status(f"PhotoDeck import monitor crashed: {exc}")
            if self.photodeck_timer:
                self.photodeck_timer.stop()
            if self.photodeck_dialog:
                self.photodeck_dialog.close()
            if self.photodeck_log_handle:
                try:
                    self.photodeck_log_handle.close()
                except Exception:
                    pass
                self.photodeck_log_handle = None
            self.photodeck_process = None
            self.photodeck_dialog = None
            self.photodeck_status_label = None
            self.photodeck_elapsed = None
            self._cleanup_photodeck_runtime_artifacts(remove_log=False)
            if self.photodeck_button:
                self.photodeck_button.setEnabled(True)
            self._show_workflow_error(
                "PhotoDeck Import",
                what_happened="PhotoDeck Import monitor stopped unexpectedly.",
                next_step="Run PhotoDeck Import again. If it fails again, open Details and send the message to support.",
                technical_detail=str(exc),
            )

    def _append_new_photodeck_log_lines(self) -> None:
        path = (self.photodeck_log_path or "").strip()
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                handle.seek(self.photodeck_log_read_offset)
                text = handle.read()
                self.photodeck_log_read_offset = handle.tell()
        except Exception:
            return
        if not text:
            return
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(PHOTODECK_IMPORT_SUMMARY_MARKER):
                continue
            self.append_photodeck_status(line)

    def _poll_photodeck_import_process(self) -> None:
        proc = self.photodeck_process
        if not proc:
            return

        if self.photodeck_status_label and self.photodeck_elapsed:
            seconds = int(self.photodeck_elapsed.elapsed() / 1000)
            self.photodeck_status_label.setText(
                f"PhotoDeck Import is running...\nElapsed: {seconds}s"
            )

        self._append_new_photodeck_log_lines()
        exit_code = proc.poll()
        if exit_code is None:
            return

        if self.photodeck_timer:
            self.photodeck_timer.stop()
        if self.photodeck_dialog:
            self.photodeck_dialog.close()
        if self.photodeck_log_handle:
            try:
                self.photodeck_log_handle.close()
            except Exception:
                pass
            self.photodeck_log_handle = None

        self.photodeck_process = None
        self.photodeck_dialog = None
        self.photodeck_status_label = None
        self.photodeck_elapsed = None

        self._append_new_photodeck_log_lines()
        log_path = (self.photodeck_log_path or "").strip()
        summary = self._parse_photodeck_import_summary_from_log()

        if exit_code == 0 and summary:
            self._merge_paid_order_assets_from_summary(summary)

        self.reload_ui()
        if self.photodeck_button:
            self.photodeck_button.setEnabled(True)

        if exit_code == 0:
            self.append_photodeck_status("PhotoDeck import complete.")
            QMessageBox.information(
                self,
                "PhotoDeck Import",
                self._format_photodeck_import_completion_message(summary),
            )
            failure_details = list((summary or {}).get("failure_details") or [])
            if failure_details:
                self._show_photodeck_details_dialog(
                    "PhotoDeck Import Failure Details",
                    self._format_photodeck_import_failure_details(failure_details),
                )
        elif exit_code == PHOTODECK_IMPORT_EXIT_CANCELLED or bool((summary or {}).get("cancelled", False)):
            self.append_photodeck_status("PhotoDeck import cancelled.")
            rollback_applied = int((summary or {}).get("rollback_applied", 0))
            rollback_errors = list((summary or {}).get("rollback_errors") or [])
            box = QMessageBox(self)
            box.setWindowTitle("PhotoDeck Import")
            box.setIcon(QMessageBox.Icon.Information if not rollback_errors else QMessageBox.Icon.Warning)
            box.setText(
                "\n".join(
                    [
                        "PhotoDeck import cancelled",
                        "",
                        "Completed:",
                        "- This run was rolled back",
                        f"- Rolled back items: {rollback_applied}",
                        "",
                        "Needs review:",
                        f"- Rollback errors: {len(rollback_errors)}",
                        "",
                        "Next step:",
                        "- Run PhotoDeck Import again when ready.",
                    ]
                )
            )
            details = [f"Log: {log_path or '(none)'}"]
            recent_log = self._recent_photodeck_log_text()
            if recent_log:
                details.extend(["", "Recent log:", recent_log])
            box.setDetailedText("\n".join(details))
            box.exec()
            if rollback_errors:
                self._show_photodeck_details_dialog(
                    "PhotoDeck Import Rollback Errors",
                    "\n".join(str(item) for item in rollback_errors[:200]),
                )
        else:
            self.append_photodeck_status("PhotoDeck import failed.")
            detail_parts = [f"Exit code: {exit_code}", f"Log: {log_path or '(none)'}"]
            recent_log = self._recent_photodeck_log_text()
            if recent_log:
                detail_parts.extend(["", "Recent log:", recent_log])
            self._show_workflow_error(
                "PhotoDeck Import",
                what_happened="PhotoDeck Import stopped before it could finish.",
                next_step="Open Details, fix the listed issue, then run PhotoDeck Import again.",
                technical_detail="\n".join(detail_parts),
            )

        self._cleanup_photodeck_runtime_artifacts(remove_log=False)

    def _cleanup_photodeck_runtime_artifacts(self, *, remove_log: bool) -> None:
        if self.photodeck_log_handle:
            try:
                self.photodeck_log_handle.close()
            except Exception:
                pass
            self.photodeck_log_handle = None
        if self.photodeck_cancel_token_path:
            try:
                os.remove(self.photodeck_cancel_token_path)
            except Exception:
                pass
            self.photodeck_cancel_token_path = None
        if remove_log and self.photodeck_log_path:
            try:
                os.remove(self.photodeck_log_path)
            except Exception:
                pass
            self.photodeck_log_path = None
        self.photodeck_log_read_offset = 0

    def _parse_photodeck_import_summary_from_log(self) -> dict | None:
        path = (self.photodeck_log_path or "").strip()
        if not path:
            return None
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return None

        parsed_summary: dict | None = None
        for line in reversed(text.splitlines()):
            if line.startswith(PHOTODECK_IMPORT_SUMMARY_MARKER):
                raw = line[len(PHOTODECK_IMPORT_SUMMARY_MARKER):]
                try:
                    parsed_summary = json.loads(raw)
                except Exception:
                    parsed_summary = None
                break
        return parsed_summary

    def _recent_photodeck_log_text(self, *, max_lines: int = 160) -> str:
        path = (self.photodeck_log_path or "").strip()
        if not path or not os.path.exists(path):
            return ""
        try:
            lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            return ""
        kept = [line for line in lines if not line.startswith(PHOTODECK_IMPORT_SUMMARY_MARKER)]
        return "\n".join(kept[-max_lines:]).strip()

    def _format_photodeck_import_completion_message(self, summary: dict | None) -> str:
        ok_count = int((summary or {}).get("processed_ok", 0))
        fail_count = int((summary or {}).get("processed_failed", 0))
        skipped_count = int((summary or {}).get("skipped_kept", 0))
        duplicate_count = int((summary or {}).get("duplicates_skipped", 0))
        moved_count = int((summary or {}).get("label_updates", 0))
        copied_count = len(list((summary or {}).get("copied_assets") or []))
        touched_count = len(list((summary or {}).get("touched_folders") or []))
        return (
            "PhotoDeck import complete\n\n"
            "Completed:\n"
            f"- Imported orders: {ok_count}\n"
            f"- Copied images: {copied_count}\n"
            f"- Updated Finished jobs: {touched_count}\n"
            f"- Moved emails to Imported: {moved_count}\n\n"
            "Needs review:\n"
            f"- Kept in source label: {skipped_count}\n"
            f"- Errors: {fail_count}\n"
            f"- Already imported orders skipped: {duplicate_count}\n\n"
            "Next step:\n"
            "- Review Stage 6 Edit."
        )

    def _photodeck_upload_sources_manifest_path(self) -> str:
        return os.path.join(self._runtime_workspace_root(), "photodeck_upload_sources.json")

    def _load_photodeck_upload_sources(self) -> List[Dict[str, str]]:
        manifest_path = self._photodeck_upload_sources_manifest_path()
        if not os.path.isfile(manifest_path):
            return []
        try:
            raw = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        except Exception:
            return []
        entries: List[Dict[str, str]] = []
        for item in list(raw or []):
            disk_name = str((item or {}).get("disk_name") or "").strip()
            picture_day_id = str((item or {}).get("picture_day_id") or "").strip().upper()
            path = str((item or {}).get("path") or "").strip()
            if not disk_name or not path or not os.path.isdir(path):
                continue
            entries.append({"disk_name": disk_name, "picture_day_id": picture_day_id, "path": path})
        return entries

    def _remember_photodeck_upload_source(self, disk_name: str, picture_day_id: str, path: str) -> None:
        disk_name = str(disk_name or "").strip()
        picture_day_id = str(picture_day_id or "").strip().upper()
        path = str(path or "").strip()
        if not disk_name or not path or not os.path.isdir(path):
            return
        entries = self._load_photodeck_upload_sources()
        key = (disk_name.lower(), picture_day_id)
        updated = [
            item for item in entries
            if (str(item.get("disk_name") or "").strip().lower(), str(item.get("picture_day_id") or "").strip().upper()) != key
        ]
        updated.append({"disk_name": disk_name, "picture_day_id": picture_day_id, "path": path})
        manifest_path = self._photodeck_upload_sources_manifest_path()
        os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
        Path(manifest_path).write_text(
            json.dumps(updated, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_paid_order_asset_entries(self) -> List[Dict[str, str]]:
        try:
            return self.db.list_proofing_paid_order_assets(statuses=["stage6"])
        except Exception:
            return []

    def _load_stage7_paid_asset_entries(self) -> List[Dict[str, str]]:
        try:
            return self.db.list_proofing_paid_order_assets(statuses=["stage7"], asset_type="original")
        except Exception:
            return []

    def _load_stage7_class_photo_entries(self) -> List[Dict[str, str]]:
        try:
            entries = self.db.list_proofing_paid_order_assets(statuses=["stage7"], asset_type="class_photo")
        except Exception:
            return []
        cleaned: List[Dict[str, str]] = []
        for entry in entries:
            path = str((entry or {}).get("path") or "").strip()
            if not path or not os.path.exists(path):
                continue
            class_key = str((entry or {}).get("original_id") or "").strip()
            if not class_key:
                class_key = self._class_key_from_child_name(str((entry or {}).get("label") or ""))
            job_folder = self._job_folder_for_class_photo_path(path)
            class_photos_folder = os.path.dirname(path)
            cleaned.append(
                {
                    **dict(entry or {}),
                    "asset_type": "class_photo",
                    "class_key": class_key,
                    "label": os.path.basename(path),
                    "path": path,
                    "source_path": path,
                    "job_folder": job_folder,
                    "class_photos_folder": class_photos_folder,
                    "quantity": int((entry or {}).get("quantity") or 1),
                }
            )
        return cleaned

    def _merge_paid_order_assets_from_summary(self, summary: dict | None) -> None:
        self.pending_send_to_edit_entries = self._load_paid_order_asset_entries()
        self.pending_stage7_asset_entries = self._load_stage7_paid_asset_entries()

    def _format_photodeck_import_failure_details(self, details: List[dict]) -> str:
        blocks: List[str] = []
        for item in details:
            reason = str(item.get("reason", "")).strip() or "The email could not be imported."
            detail = str(item.get("detail", "")).strip() or reason
            pid = str(item.get("pid", "") or "").strip()
            order_no = str(item.get("order_no", "") or "").strip()
            subject = str(item.get("subject", "") or "").strip()
            message_id = str(item.get("message_id", "") or "").strip()
            from_header = str(item.get("from_header", "") or "").strip()
            header_date = str(item.get("header_date", "") or "").strip()
            next_step = str(item.get("next_step", "") or "").strip()

            lines = [
                f"Reason: {reason}",
                f"What happened: {detail}",
            ]
            if next_step:
                lines.append(f"Next step: {next_step}")
            if pid:
                lines.append(f"Picture Day ID: {pid}")
            if order_no:
                lines.append(f"Order No.: {order_no}")
            if subject:
                lines.append(f"Subject: {subject}")
            if from_header:
                lines.append(f"From: {from_header}")
            if header_date:
                lines.append(f"Date: {header_date}")
            if message_id:
                lines.append(f"Message ID: {message_id}")
            blocks.append("\n".join(lines))
        return "\n\n" + ("\n\n" + ("-" * 52) + "\n\n").join(blocks) if blocks else ""

    def _show_photodeck_details_dialog(self, title: str, text: str) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumSize(720, 420)
        layout = QVBoxLayout(dlg)
        box = QPlainTextEdit()
        box.setReadOnly(True)
        box.setPlainText(text.strip() or "No details available.")
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(box)
        layout.addWidget(close_btn)
        dlg.exec()

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
            self._show_workflow_error(
                "2. Sort",
                what_happened="The selected source folder does not exist.",
                checked=source_folder,
                next_step="Choose an existing original source folder, then run Sort again.",
            )
            return

        plan = build_stage2_sort_plan(disk_name, folder_path, source_folder)
        if stage2_source_conflicts_existing_output(plan):
            self._show_workflow_error(
                "2. Sort",
                what_happened="The selected source is already inside an old sorted output.",
                checked=source_folder,
                next_step="Choose the original unsorted source folder, then run Sort again.",
            )
            return
        if _is_same_path(source_folder, folder_path) and plan.existing_output_paths:
            self._show_workflow_error(
                "2. Sort",
                what_happened="The whole job folder was selected, but it already contains an old sorted output.",
                checked=folder_path,
                next_step="Choose the original source folder inside this job. Do not choose the whole job folder.",
            )
            return

        old_output_text = "Existing output will be replaced." if plan.existing_output_paths else "No existing output found."
        source_text = "Source will be deleted after success." if plan.source_will_be_deleted else "Source will be kept."
        confirm = QMessageBox.question(
            self,
            "Confirm 2. Sort",
            "Sort will build the new output first. Old files change only after that succeeds.\n\n"
            f"Source:\n{plan.source_folder}\n\n"
            f"Output:\n{plan.output_folder}\n\n"
            f"{old_output_text}\n"
            f"{source_text}\n\n"
            "Continue?",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if confirm != QMessageBox.Yes:
            return

        try:
            def run_stage2_sort_task():
                return execute_stage2_sort_plan(plan)

            results = self._run_blocking_io_task(
                "2. Sort",
                "Sorting proofs into a temporary output first...\nPlease wait.",
                run_stage2_sort_task,
            )
        except Exception as exc:
            user_error, technical_detail = self._split_user_error(exc)
            self._show_workflow_error(
                "2. Sort",
                what_happened=f"Sorting failed: {user_error}",
                checked=f"Source folder:\n{plan.source_folder}\n\nTemporary output:\n{plan.temp_output_folder}\n\nFinal output:\n{plan.output_folder}",
                next_step="Fix the source/output folder issue, then run Sort again. Old output and source were not changed unless the success summary said they were replaced.",
                technical_detail=technical_detail,
            )
            return
        sort_run = results
        results = list(sort_run.sort_results)
        removed_outputs = list(sort_run.replaced_outputs)
        source_removed = bool(sort_run.source_removed)
        cleanup_warnings = list(sort_run.cleanup_warnings)

        total_students = sum(int(getattr(row, "student_count", 0) or 0) for row in results)
        total_files = sum(int(getattr(row, "file_count", 0) or 0) for row in results)
        total_moved = sum(int(getattr(row, "moved_count", 0) or 0) for row in results)
        skipped = [row for row in results if getattr(row, "skipped_reason", None)]
        summary_lines = [
            f"Processed folders: {len(results)}",
            f"Source folder: {plan.source_folder}",
            f"Output folder: {plan.output_folder}",
            f"Student buckets: {total_students}",
            f"Matched image files: {total_files}",
            f"Sorted files: {total_moved}",
        ]
        if removed_outputs:
            summary_lines.append(f"Replaced old output folders: {len(removed_outputs)}")
        if source_removed:
            summary_lines.append("Deleted source folder after new output succeeded.")
        if cleanup_warnings:
            summary_lines.append("Cleanup warnings:")
            summary_lines.extend(f"- {warning}" for warning in cleanup_warnings[:5])
        if skipped:
            summary_lines.append(f"Folders without images: {len(skipped)}")
        moved_to_stage3 = False
        if total_files > 0 and total_moved > 0:
            moved_to_stage3 = self._advance_disk_name_to_stage(disk_name, 3, "2. Sort")
            if moved_to_stage3:
                summary_lines.append("Auto moved to stage 3 (Upload & PDFs).")
        detail_lines = list(summary_lines)
        user_lines = [
            "Sort finished",
            "",
            "Completed:",
            f"- Student folders: {total_students}",
            f"- Images sorted: {total_moved} of {total_files}",
            "- New output created successfully",
        ]
        if removed_outputs:
            user_lines.append(f"- Replaced {len(removed_outputs)} old output folder(s)")
        if source_removed:
            user_lines.append("- Deleted the old source folder after the new output succeeded")
        if skipped:
            user_lines.append(f"- {len(skipped)} folder(s) had no images")
        if cleanup_warnings:
            user_lines.extend(["", "Needs attention:"])
            user_lines.extend(f"- {warning}" for warning in cleanup_warnings[:3])
        user_lines.extend(["", "Next step:"])
        if moved_to_stage3:
            user_lines.append("- Continue with Stage 3 Upload & PDFs.")
        else:
            user_lines.append("- Move this job to Stage 3 when you are ready to upload.")

        box = QMessageBox(self)
        box.setWindowTitle("2. Sort")
        box.setIcon(QMessageBox.Icon.Warning if cleanup_warnings else QMessageBox.Icon.Information)
        box.setText("\n".join(user_lines))
        box.setDetailedText("\n".join(detail_lines))
        box.exec()
        if moved_to_stage3:
            self.reload_ui()

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
                user_error, technical_detail = self._split_user_error(exc)
                self._show_workflow_error(
                    "Move to Edit",
                    what_happened=f"Failed while preparing Send to Edit: {user_error}",
                    checked=target_folder_path,
                    next_step="Check that the runtime folder is accessible, then try Move to Edit again.",
                    technical_detail=technical_detail,
                )
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
                            stage=PROOFING_EDIT_STAGE,
                        )
                        self._mark_item_moved(marker_id)
                    else:
                        marker_id = int(
                            self.db.upsert_into_domain(
                                disk_name=folder_name,
                                domain=self.workflow_domain,
                                stage=PROOFING_EDIT_STAGE,
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
            self.pending_send_to_edit_entries = [{"label": status_label, "path": target_folder_path}]

            self.reload_ui()
        finally:
            self._finish_progress(self.send_to_edit_progress)

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
            if isinstance(worker.error, (UserFacingError, DeveloperError)):
                raise worker.error
            error_detail = str(getattr(worker.error, "detail", "") or "").strip()
            if error_detail and worker.traceback_text:
                error_detail = f"{error_detail}\n\n{worker.traceback_text}"
            raise DeveloperError(str(worker.error), error_detail or worker.traceback_text)
        return worker.result

    def _sync_db_after_folder_rename(self, old_name: str, new_name: str, *, target_stage: Optional[int]) -> None:
        old_key = (old_name or "").strip()
        new_key = (new_name or "").strip()
        if not old_key or not new_key:
            return
        try:
            existing = self.db.get_item_by_disk_name(old_key, domain=self.workflow_domain)
            if existing is not None:
                marker_id = int(existing.id)
                self.db.update_disk_name(marker_id, new_key)
                if target_stage:
                    self.db.update_domain_stage(
                        marker_id,
                        domain=self.workflow_domain,
                        stage=int(target_stage),
                    )
                    self._mark_item_moved(marker_id)
                else:
                    self.updated_item_ids.add(marker_id)
                return
            # If old row is missing, ensure at least the new name is represented.
            marker_id = int(
                self.db.upsert_into_domain(
                    disk_name=new_key,
                    domain=self.workflow_domain,
                    stage=int(target_stage or 1),
                )
            )
            if target_stage:
                self._mark_item_moved(marker_id)
            else:
                self.newly_added_item_ids.add(marker_id)
        except Exception:
            self._show_db_warning_once(
                "Workflow DB",
                "Folder rename completed on disk, but DB sync failed.\n"
                f"{old_key}\n→ {new_key}",
            )

    def _list_edit_candidate_folders(self) -> List[str]:
        try:
            stage_rows = self.db.list_by_domain_stage(self.workflow_domain, 5)
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
        domain_key = re.sub(r'[^a-z0-9_-]+', '_', str(self.workflow_domain or "proofing").strip().lower())
        domain_key = domain_key or "proofing"
        runtime_dir = str(os.environ.get("DAMY_RUNTIME_DIR") or "").strip()
        if runtime_dir:
            return os.path.join(runtime_dir, domain_key)
        share_root = str(os.environ.get("DAMY_SHARE_ROOT") or "").strip()
        if share_root:
            return os.path.join(share_root, "runtime", domain_key)
        if bool(getattr(sys, "frozen", False)):
            return os.path.join(os.path.dirname(sys.executable), "runtime", domain_key)
        return os.path.join(Path(__file__).resolve().parents[4], "_workflow_runtime", domain_key)

    def _runtime_send_to_edit_dir(self) -> str:
        return os.path.join(self._runtime_workspace_root(), "send_to_edit")

    def _paid_order_asset_ui_label(self, entry: Dict[str, object]) -> str:
        asset_type_key = str((entry or {}).get("asset_type") or "").strip().lower()
        if asset_type_key in {"class_photo", "missing_class_photo"}:
            class_key = str((entry or {}).get("class_key") or "").strip()
            if asset_type_key == "missing_class_photo":
                reason = str((entry or {}).get("missing_reason") or "Missing").strip()
                return " | ".join(part for part in [f"ACTION REQUIRED: {reason} Class Photo", class_key, "double-click to choose"] if part)
            filename = os.path.basename(str((entry or {}).get("path") or "").strip())
            return " | ".join(part for part in ["Class Photos", class_key, filename] if part)
        asset_type = str((entry or {}).get("asset_type") or "").strip().title()
        disk_name = str((entry or {}).get("disk_name") or "").strip()
        original_id = self._paid_order_asset_display_id(entry)
        proof_id = str((entry or {}).get("proof_id") or "").strip()
        filename = os.path.basename(str((entry or {}).get("path") or "").strip())
        display_label = str((entry or {}).get("label") or "").strip()
        display_filename = display_label or filename
        package_name = self._package_folder_name_from_value((entry or {}).get("package"))
        background = self._clean_paid_order_part((entry or {}).get("background"))
        addon_names = self._addon_folder_names_from_entry(entry)
        quantity = self._coerce_paid_quantity((entry or {}).get("quantity"))

        parts = [part for part in [disk_name, asset_type, original_id] if part]
        if asset_type.lower() == "proof" and proof_id:
            parts.append(proof_id)
        if package_name:
            parts.append(package_name)
        if background:
            parts.append(background)
        if addon_names:
            parts.append("Add-ons: " + ", ".join(addon_names))
        parts.append(f"Qty {quantity}")
        if display_filename:
            parts.append(display_filename)
        return " | ".join(parts)

    def _paid_order_asset_display_id(self, entry: Dict[str, object]) -> str:
        return str((entry or {}).get("original_id") or "").strip()

    def _paid_order_asset_group_key(self, entry: Dict[str, object]) -> Tuple[str, str, str, str]:
        return (
            str((entry or {}).get("workflow_item_id") or (entry or {}).get("workflow_run_id") or ""),
            str((entry or {}).get("order_no") or ""),
            str((entry or {}).get("original_id") or ""),
            str((entry or {}).get("proof_id") or ""),
        )

    def _child_name_from_paid_entry(self, entry: Dict[str, object]) -> str:
        child_name = str((entry or {}).get("child_name") or "").strip()
        if child_name:
            return child_name
        label_child = self._child_name_from_paid_label((entry or {}).get("label"))
        if label_child:
            return label_child
        for key in ("source_path", "path"):
            path = str((entry or {}).get(key) or "").strip()
            if not path:
                continue
            inferred = _child_name_from_proof_path(path, "")
            if inferred and inferred.lower() not in {"orders", "order pdfs", "break pages", "original", "originals", "proof", "proofs"}:
                return inferred
        return ""

    def _child_name_from_paid_label(self, value: object) -> str:
        label = str(value or "").strip()
        if not label:
            return ""
        for part in [part.strip() for part in label.split(" - ") if part.strip()]:
            if "[" in part and "]" in part:
                return part
        match = re.search(r"([^|\n\r]*\[[^\]]+\])", label)
        return match.group(1).strip() if match else ""

    def _paid_order_asset_background_text(self, entry: Dict[str, object]) -> str:
        background = _portrait_background_token((entry or {}).get("background"))
        if not background:
            background = _portrait_background_token(_background_from_proof_id((entry or {}).get("proof_id")))
        if not background:
            path = str((entry or {}).get("path") or "").strip()
            background = _portrait_background_token(_background_from_proof_id(os.path.basename(path)))
        return background

    def _digital_editing_text_from_entry(self, entry: Dict[str, object]) -> str:
        addons_value = (entry or {}).get("addons") or []
        raw_entries: List[object]
        if isinstance(addons_value, dict):
            raw_entries = [addons_value]
        elif isinstance(addons_value, tuple) and len(addons_value) >= 2 and not isinstance(addons_value[0], (list, tuple, dict)):
            raw_entries = [addons_value]
        elif isinstance(addons_value, list):
            raw_entries = list(addons_value)
        else:
            raw_entries = []
        for addon in raw_entries:
            if isinstance(addon, dict):
                label = addon.get("label")
                value = addon.get("value")
            elif isinstance(addon, (list, tuple)) and len(addon) >= 2:
                label, value = addon[0], addon[1]
            else:
                continue
            if "digital editing" not in self._clean_paid_order_part(label).lower():
                continue
            value_clean = self._clean_paid_order_part(value)
            if value_clean.lower() in {"as is", "asis"}:
                return ""
            return f"DE_{value_clean}" if value_clean else ""
        return ""

    def _paid_order_asset_table_values(self, entry: Dict[str, object], *, child_name_override: str = "") -> List[str]:
        asset_type = str((entry or {}).get("asset_type") or "").strip().title()
        child_name = str(child_name_override or "").strip() or self._child_name_from_paid_entry(entry)
        display_id = self._paid_order_asset_display_id(entry)
        package_name = self._package_folder_name_from_value((entry or {}).get("package"))
        addon_names = ", ".join(self._addon_folder_names_from_entry(entry))
        digital_editing = self._digital_editing_text_from_entry(entry)
        background = self._paid_order_asset_background_text(entry)
        quantity = str(self._coerce_paid_quantity((entry or {}).get("quantity")))
        filename = os.path.basename(str((entry or {}).get("path") or "").strip())
        return [child_name, asset_type, display_id, package_name, addon_names, digital_editing, background, quantity, filename]

    def set_send_to_edit_status_entries(self, entries: List[Dict[str, str]]) -> None:
        if not self.send_to_edit_status:
            return
        self.send_to_edit_status.setRowCount(0)
        cleaned: List[Dict[str, str]] = []
        for entry in entries:
            path = str((entry or {}).get("path") or "").strip()
            if not path or not os.path.exists(path):
                continue
            cleaned_entry = dict(entry or {})
            label = self._paid_order_asset_ui_label(cleaned_entry)
            cleaned_entry["label"] = label
            cleaned_entry["path"] = path
            cleaned.append(cleaned_entry)
        child_lookup: Dict[Tuple[str, str, str, str], str] = {}
        for cleaned_entry in cleaned:
            child_name = self._child_name_from_paid_entry(cleaned_entry)
            if child_name:
                child_lookup[self._paid_order_asset_group_key(cleaned_entry)] = child_name
        for cleaned_entry in cleaned:
            path = str((cleaned_entry or {}).get("path") or "").strip()
            label = str((cleaned_entry or {}).get("label") or "").strip()
            row = self.send_to_edit_status.rowCount()
            self.send_to_edit_status.insertRow(row)
            child_name = child_lookup.get(self._paid_order_asset_group_key(cleaned_entry), "")
            stored_entry = dict(cleaned_entry)
            if child_name:
                stored_entry["child_name"] = child_name
            for col, value in enumerate(self._paid_order_asset_table_values(cleaned_entry, child_name_override=child_name)):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, path)
                item.setData(Qt.UserRole + 20, stored_entry)
                item.setToolTip(label)
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled)
                self.send_to_edit_status.setItem(row, col, item)
        self.send_to_edit_status.show()
        self._reapply_current_search()

    def set_stage7_paid_asset_entries(self, entries: List[Dict[str, str]]) -> None:
        if not self.print_status:
            return
        self.print_status.clear()
        cleaned = [
            dict(entry or {})
            for entry in entries
            if str((entry or {}).get("path") or "").strip() and os.path.exists(str((entry or {}).get("path") or "").strip())
            and str((entry or {}).get("asset_type") or "").strip().lower() not in {"class_photo", "missing_class_photo", "break_page"}
        ]
        self._add_class_photos_for_paid_entries(cleaned)
        active_class_keys = {
            (
                self._class_photo_dedup_key(self._job_folder_for_paid_entry(entry)),
                self._class_key_from_child_name(self._child_name_from_paid_entry(entry)).lower(),
            )
            for entry in cleaned
            if str((entry or {}).get("asset_type") or "").strip().lower() == "original"
            and self._class_key_from_child_name(self._child_name_from_paid_entry(entry))
        }
        class_entries: List[Dict[str, str]] = []
        seen_class_entries: Set[Tuple[str, str, str]] = set()
        for entry in getattr(self, "stage7_class_photo_entries", []):
            entry_type = str((entry or {}).get("asset_type") or "").strip().lower()
            path = str((entry or {}).get("path") or "").strip()
            if entry_type == "missing_class_photo":
                path_key = ""
            elif path and os.path.exists(path):
                path_key = self._class_photo_dedup_key(path)
            else:
                continue
            class_key = (
                self._class_photo_dedup_key(str((entry or {}).get("job_folder") or "")),
                str((entry or {}).get("class_key") or "").strip().lower(),
            )
            if class_key not in active_class_keys:
                continue
            dedup_key = (class_key[0], class_key[1], path_key)
            if dedup_key in seen_class_entries:
                continue
            seen_class_entries.add(dedup_key)
            class_entries.append(dict(entry))
        stale_class_photo_ids: List[int] = []
        for entry in getattr(self, "stage7_class_photo_entries", []):
            if str((entry or {}).get("asset_type") or "").strip().lower() != "class_photo":
                continue
            class_key = (
                self._class_photo_dedup_key(str((entry or {}).get("job_folder") or "")),
                str((entry or {}).get("class_key") or "").strip().lower(),
            )
            if class_key in active_class_keys:
                continue
            try:
                stale_class_photo_ids.append(int((entry or {}).get("id")))
            except Exception:
                continue
        if stale_class_photo_ids:
            try:
                self.db.archive_proofing_paid_assets_by_ids(stale_class_photo_ids)
            except Exception:
                pass
        self.stage7_class_photo_entries = class_entries
        combined = cleaned + class_entries
        self.pending_stage7_asset_entries = combined
        groups: Dict[str, List[Dict[str, object]]] = {}
        for entry in combined:
            for folder_name, _multiplier in self._package_order_lines_for_entry(entry):
                groups.setdefault(folder_name, []).append(entry)
        for folder_name in sorted(groups, key=lambda value: value.lower()):
            count = sum(self._coerce_paid_quantity(entry.get("quantity")) for entry in groups[folder_name])
            item = QListWidgetItem(f"{folder_name} ({count})")
            item.setData(Qt.UserRole, folder_name)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.print_status.addItem(item)
        if self.print_status.count() > 0:
            self.print_status.setCurrentRow(0)
        self.refresh_stage7_package_asset_list()
        self.print_paths = {self._paid_order_asset_ui_label(entry): str(entry.get("path") or "") for entry in combined}
        self._update_stage7_class_photo_status(class_entries)
        self.print_status.show()
        self._reapply_current_search()

    def _update_stage7_class_photo_status(self, class_entries: List[Dict[str, object]]) -> None:
        label = self.stage7_class_photo_status_label
        if label is None:
            return
        missing_entries = [
            entry for entry in class_entries
            if str((entry or {}).get("asset_type") or "").strip().lower() == "missing_class_photo"
        ]
        if not missing_entries:
            label.hide()
            label.setText("")
            return
        missing_count = sum(
            1
            for entry in missing_entries
            if str((entry or {}).get("missing_reason") or "").strip().lower() != "ambiguous"
        )
        ambiguous_count = len(missing_entries) - missing_count
        parts: List[str] = []
        if missing_count:
            parts.append(f"{missing_count} missing")
        if ambiguous_count:
            parts.append(f"{ambiguous_count} ambiguous")
        label.setText("Class Photos: " + ", ".join(parts))
        label.show()

    def _stage7_school_prefix_from_entry(self, entry: Dict[str, object]) -> str:
        if str((entry or {}).get("asset_type") or "").strip().lower() in {"class_photo", "missing_class_photo"}:
            return str((entry or {}).get("class_key") or "").strip() or "Class Photos"
        filename = os.path.basename(str((entry or {}).get("path") or "").strip())
        stem, _ext = os.path.splitext(filename)
        tokens = [token for token in stem.split("_") if token]
        if len(tokens) >= 2:
            return f"{tokens[0]}_{tokens[1]}"
        original_id = str((entry or {}).get("original_id") or "").strip()
        tokens = [token for token in original_id.split("_") if token]
        if len(tokens) >= 2:
            return f"{tokens[0]}_{tokens[1]}"
        return "Unknown"

    def _stage7_break_page_path(self, entry: Dict[str, object], package_folder: str, school_prefix: str) -> str:
        source_path = str((entry or {}).get("path") or "").strip()
        orders_folder = os.path.dirname(source_path) if source_path else self._runtime_workspace_root()
        break_root = os.path.join(orders_folder, "Break Pages", sanitize_folder_name(package_folder))
        os.makedirs(break_root, exist_ok=True)
        return os.path.join(break_root, sanitize_folder_name(f"BREAK PAGE - {school_prefix}") + ".jpg")

    def _ensure_stage7_break_page(self, entry: Dict[str, object], package_folder: str, school_prefix: str) -> str:
        path = self._stage7_break_page_path(entry, package_folder, school_prefix)
        if os.path.exists(path):
            return path
        image = QImage(4912, 7360, QImage.Format_RGB32)
        image.fill(QColor("white"))
        image.save(path, "JPG", 95)
        return path

    def refresh_stage7_package_asset_list(self) -> None:
        if not self.stage7_package_asset_status:
            return
        self.stage7_package_asset_status.clear()
        selected_folder = ""
        if self.print_status and self.print_status.currentItem() is not None:
            selected_folder = str(self.print_status.currentItem().data(Qt.UserRole) or "").strip()
        if not selected_folder:
            return
        grouped: Dict[str, List[Dict[str, object]]] = {}
        for entry in self.pending_stage7_asset_entries:
            folders = [folder_name for folder_name, _multiplier in self._package_order_lines_for_entry(entry)]
            if selected_folder not in folders:
                continue
            grouped.setdefault(self._stage7_school_prefix_from_entry(entry), []).append(entry)

        for school_prefix in sorted(grouped, key=lambda value: value.lower()):
            group_entries = sorted(
                grouped[school_prefix],
                key=lambda item: os.path.basename(str(item.get("path") or "")).lower(),
            )
            if group_entries and selected_folder != "Class Photos":
                break_path = self._ensure_stage7_break_page(group_entries[0], selected_folder, school_prefix)
                break_entry = {
                    "label": f"BREAK PAGE - {school_prefix}",
                    "path": break_path,
                    "asset_type": "break_page",
                    "school_prefix": school_prefix,
                    "package_folder": selected_folder,
                }
                break_item = QListWidgetItem(str(break_entry["label"]))
                break_item.setData(Qt.UserRole, break_path)
                break_item.setData(Qt.UserRole + 20, break_entry)
                break_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.stage7_package_asset_status.addItem(break_item)

            for entry in group_entries:
                label = self._paid_order_asset_ui_label(entry)
                path = str((entry or {}).get("path") or "").strip()
                asset_type_key = str((entry or {}).get("asset_type") or "").strip().lower()
                if asset_type_key != "missing_class_photo" and (not path or not os.path.exists(path)):
                    continue
                cleaned_entry = dict(entry or {})
                cleaned_entry["label"] = label
                cleaned_entry["path"] = path
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, path)
                item.setData(Qt.UserRole + 20, cleaned_entry)
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled)
                if asset_type_key == "missing_class_photo":
                    item.setToolTip("Double-click to choose the class photo file.")
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    item.setForeground(QBrush(QColor("#ffd166")))
                    item.setBackground(QBrush(QColor("#4a3320")))
                self.stage7_package_asset_status.addItem(item)
        self._reapply_current_search()

    def _send_to_edit_entry_from_item(self, item) -> Dict[str, str]:
        path = str(item.data(Qt.UserRole) or "").strip()
        label = str(item.text() or "").strip()
        stored = item.data(Qt.UserRole + 20)
        entry = dict(stored or {}) if isinstance(stored, dict) else {}
        entry["label"] = label
        entry["path"] = path
        return entry

    def _selected_send_to_edit_items(self) -> List[QTableWidgetItem]:
        if not self.send_to_edit_status:
            return []
        selected = [item for item in self.send_to_edit_status.selectedItems() if item is not None]
        if not selected and self.send_to_edit_status.currentItem() is not None:
            selected = [self.send_to_edit_status.currentItem()]
        by_row: Dict[int, QTableWidgetItem] = {}
        for item in selected:
            row = item.row()
            if row not in by_row:
                first = self.send_to_edit_status.item(row, 0)
                by_row[row] = first if first is not None else item
        return [by_row[row] for row in sorted(by_row)]

    def _paid_asset_display_name(self, entry: Dict[str, str]) -> str:
        path = str((entry or {}).get("path") or "").strip()
        label = str((entry or {}).get("label") or "").strip()
        name = os.path.basename(path) if path else label
        if not name:
            name = label
        return re.sub(r"\s+\[[^\]]+\]\s*$", "", name).strip()

    def _is_original_paid_asset(self, entry: Dict[str, str]) -> bool:
        asset_type = str((entry or {}).get("asset_type") or "").strip().lower()
        if asset_type:
            return asset_type == "original"
        name = self._paid_asset_display_name(entry).lower()
        return "original" in name and "proof" not in name

    def _coerce_paid_quantity(self, value: object) -> int:
        if value is None:
            return 1
        if isinstance(value, (int, float)):
            return max(1, int(value))
        match = re.search(r"\d+", str(value))
        if not match:
            return 1
        return max(1, int(match.group(0)))

    def _clean_paid_order_part(self, value: object) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        text = re.sub(r"[.\s]+$", "", text)
        if text.lower() in {"", "none", "n/a", "na", "no add-ons", "no addons", "no add ons", "no add on"}:
            return ""
        return text

    def _package_folder_name_from_value(self, value: object) -> str:
        cleaned = self._clean_paid_order_part(value)
        if not cleaned:
            return ""
        if cleaned.lower() in {"no package", "no packages"}:
            return ""
        cleaned = re.sub(r"(?i)^package\s+", "", cleaned).strip()
        compact = re.sub(r"[^A-Za-z0-9]+", "", cleaned).upper()
        if not compact:
            return ""
        if compact.startswith("P") and len(compact) > 1:
            return sanitize_folder_name(compact)
        return sanitize_folder_name(f"P{compact}")

    def _addon_order_line_from_value(self, value: object, label: object = "") -> Optional[Tuple[str, int]]:
        value_clean = self._clean_paid_order_part(value)
        label_clean = self._clean_paid_order_part(label)
        if not value_clean:
            return None
        if value_clean.lower() in {"as is", "asis"}:
            return None
        item_name = value_clean

        if not label_clean:
            folder_name = item_name
        else:
            label_lower = label_clean.lower()
            if label_lower in {"add-ons", "add ons", "add-on", "addons"}:
                folder_name = item_name
            elif "digital editing" in label_lower:
                return None
            elif label_lower == value_clean.lower():
                folder_name = item_name
            else:
                folder_name = f"{label_clean} {item_name}"
        clean_folder = sanitize_folder_name(folder_name)
        if not clean_folder:
            return None
        return clean_folder, 1

    def _addon_folder_name_from_value(self, value: object, label: object = "") -> str:
        line = self._addon_order_line_from_value(value, label)
        return line[0] if line else ""

    def _addon_order_lines_from_entry(self, entry: Dict[str, object]) -> List[Tuple[str, int]]:
        addons_value = (entry or {}).get("addons") or []
        raw_entries: List[object]
        if isinstance(addons_value, dict):
            raw_entries = [addons_value]
        elif isinstance(addons_value, tuple) and len(addons_value) >= 2 and not isinstance(addons_value[0], (list, tuple, dict)):
            raw_entries = [addons_value]
        elif isinstance(addons_value, list):
            raw_entries = list(addons_value)
        else:
            raw_entries = []

        lines: List[Tuple[str, int]] = []
        for addon in raw_entries:
            if isinstance(addon, dict):
                line = self._addon_order_line_from_value(addon.get("value"), addon.get("label"))
            elif isinstance(addon, (list, tuple)) and len(addon) >= 2:
                line = self._addon_order_line_from_value(addon[1], addon[0])
            else:
                line = self._addon_order_line_from_value(addon)
            if line:
                lines.append(line)
        return lines

    def _addon_folder_names_from_entry(self, entry: Dict[str, object]) -> List[str]:
        return [folder_name for folder_name, _multiplier in self._addon_order_lines_from_entry(entry)]

    def _fallback_order_lines_from_filename(self, entry: Dict[str, object]) -> List[Tuple[str, int]]:
        name = self._paid_asset_display_name(entry)
        stem, _ = os.path.splitext(name)
        stem = re.sub(r"\s+Original\s*$", "", stem, flags=re.IGNORECASE).strip()
        parts = [part.strip() for part in stem.split(" - ") if part.strip()]
        detail_parts = parts[1:] if len(parts) > 1 else []
        if detail_parts and "+" in detail_parts[0]:
            package_part, addon_part = [part.strip() for part in detail_parts[0].split("+", 1)]
            detail_parts = [package_part, addon_part, *detail_parts[1:]]

        lines: List[Tuple[str, int]] = []
        if detail_parts:
            package_name = self._package_folder_name_from_value(detail_parts[0])
            if package_name:
                lines.append((package_name, 1))
                detail_parts = detail_parts[1:]
        for addon in detail_parts:
            line = self._addon_order_line_from_value(addon)
            if line:
                lines.append(line)
        return lines

    def _class_key_from_child_name(self, child_name: str) -> str:
        match = re.search(r"\[([^\]]+)\]", str(child_name or ""))
        return self._clean_paid_order_part(match.group(1)) if match else ""

    def _job_folder_for_paid_entry(self, entry: Dict[str, object]) -> str:
        path = str((entry or {}).get("path") or "").strip()
        if not path:
            return ""
        parent = os.path.dirname(path)
        if os.path.basename(parent).lower() == "orders":
            return os.path.dirname(parent)
        grandparent = os.path.dirname(parent)
        if os.path.basename(grandparent).lower() == "orders":
            return os.path.dirname(grandparent)
        return grandparent

    def _class_photos_folder_for_paid_entry(self, entry: Dict[str, object]) -> str:
        job_folder = self._job_folder_for_paid_entry(entry)
        if not job_folder:
            return ""
        folder = find_matching_subdir(job_folder, "Class Photos")
        return folder if os.path.isdir(folder) else ""

    def _job_folder_for_class_photo_path(self, path: str) -> str:
        folder = os.path.dirname(str(path or "").strip())
        if os.path.basename(folder).lower() == "class photos":
            return os.path.dirname(folder)
        return os.path.dirname(folder)

    def _class_photo_dedup_key(self, path: str) -> str:
        return os.path.normcase(os.path.abspath(str(path or "").strip())).lower()

    def _find_class_photo_candidates(self, class_photos_folder: str, class_key: str) -> List[str]:
        if not class_photos_folder or not os.path.isdir(class_photos_folder) or not class_key:
            return []
        target = _normalize_token(class_key)
        candidates: List[str] = []
        for filename in sorted(os.listdir(class_photos_folder), key=lambda value: value.lower()):
            path = os.path.join(class_photos_folder, filename)
            if not os.path.isfile(path):
                continue
            if os.path.splitext(filename)[1].lower() not in IMAGE_EXTENSIONS:
                continue
            stem = os.path.splitext(filename)[0]
            if target and target in _normalize_token(stem):
                candidates.append(path)
        return candidates

    def _choose_class_photo_file(self, class_photos_folder: str, class_key: str) -> str:
        start_folder = class_photos_folder if os.path.isdir(class_photos_folder) else ""
        picked_file, _ = QFileDialog.getOpenFileName(
            self,
            f"Select Class Photo for {class_key}",
            start_folder,
            "Images (*.jpg *.jpeg *.png *.tif *.tiff *.bmp *.gif);;All Files (*.*)",
        )
        return str(picked_file or "").strip()

    def _persist_stage7_class_photo_entry(self, entry: Dict[str, object], source_entry: Dict[str, object]) -> None:
        path = str((entry or {}).get("path") or "").strip()
        class_key = str((entry or {}).get("class_key") or "").strip()
        if not path or not class_key:
            return
        asset = {
            "workflow_run_id": (source_entry or {}).get("workflow_run_id") or (source_entry or {}).get("workflow_item_id"),
            "workflow_item_id": (source_entry or {}).get("workflow_item_id") or (source_entry or {}).get("workflow_run_id"),
            "disk_name": str((source_entry or {}).get("disk_name") or "").strip(),
            "pid": str((source_entry or {}).get("pid") or "").strip(),
            "order_no": f"CLASS_PHOTO|{class_key}",
            "message_id": str((source_entry or {}).get("message_id") or "").strip(),
            "asset_type": "class_photo",
            "original_id": class_key,
            "proof_id": "",
            "path": path,
            "source_path": path,
            "order_pdf_path": "",
            "label": self._paid_order_asset_ui_label(entry),
            "package": "Class Photos",
            "addons": [],
            "background": "",
            "quantity": 1,
            "asset_status": "stage7",
        }
        self.db.record_proofing_paid_order_assets([asset])

    def _stage7_source_entry_for_class_key(self, job_folder: str, class_key: str) -> Dict[str, object]:
        target = (
            self._class_photo_dedup_key(job_folder),
            str(class_key or "").strip().lower(),
        )
        for entry in self._load_stage7_paid_asset_entries():
            if str((entry or {}).get("asset_type") or "").strip().lower() != "original":
                continue
            entry_key = (
                self._class_photo_dedup_key(self._job_folder_for_paid_entry(entry)),
                self._class_key_from_child_name(self._child_name_from_paid_entry(entry)).lower(),
            )
            if entry_key == target:
                return dict(entry or {})
        return {}

    def _add_class_photos_for_paid_entries(self, entries: List[Dict[str, object]]) -> None:
        existing = {
            self._class_photo_dedup_key(str((entry or {}).get("path") or ""))
            for entry in getattr(self, "stage7_class_photo_entries", [])
            if str((entry or {}).get("asset_type") or "").strip().lower() == "class_photo"
            and str((entry or {}).get("path") or "").strip()
            and os.path.exists(str((entry or {}).get("path") or "").strip())
        }
        existing_class_keys = {
            (
                self._class_photo_dedup_key(str((entry or {}).get("job_folder") or "")),
                str((entry or {}).get("class_key") or "").strip().lower(),
            )
            for entry in getattr(self, "stage7_class_photo_entries", [])
            if (
                str((entry or {}).get("asset_type") or "").strip().lower() == "missing_class_photo"
                or (
                    str((entry or {}).get("asset_type") or "").strip().lower() == "class_photo"
                    and str((entry or {}).get("path") or "").strip()
                    and os.path.exists(str((entry or {}).get("path") or "").strip())
                )
            )
        }
        prompted_keys: Set[Tuple[str, str]] = set()
        for entry in entries:
            if str((entry or {}).get("asset_type") or "").strip().lower() != "original":
                continue
            child_name = self._child_name_from_paid_entry(entry)
            class_key = self._class_key_from_child_name(child_name)
            if not class_key:
                continue
            job_folder = self._job_folder_for_paid_entry(entry)
            class_group_key = (self._class_photo_dedup_key(job_folder), class_key.lower())
            if class_group_key in existing_class_keys:
                continue
            class_photos_folder = self._class_photos_folder_for_paid_entry(entry)
            prompt_key = (self._class_photo_dedup_key(class_photos_folder), class_key.lower())
            candidates = self._find_class_photo_candidates(class_photos_folder, class_key)
            path = candidates[0] if len(candidates) == 1 else ""
            if not path and prompt_key not in prompted_keys:
                prompted_keys.add(prompt_key)
                reason = "Ambiguous" if len(candidates) > 1 else "Missing"
                self.stage7_class_photo_entries.append(
                    {
                        "asset_type": "missing_class_photo",
                        "class_key": class_key,
                        "child_name": child_name,
                        "label": f"{reason} Class Photo: {class_key}",
                        "path": "",
                        "source_path": "",
                        "job_folder": job_folder,
                        "class_photos_folder": class_photos_folder,
                        "missing_reason": reason,
                        "quantity": 1,
                    }
                )
                existing_class_keys.add(class_group_key)
                continue
            if not path or not os.path.exists(path):
                continue
            dedup_key = self._class_photo_dedup_key(path)
            if dedup_key in existing:
                continue
            existing.add(dedup_key)
            existing_class_keys.add(class_group_key)
            class_entry = {
                "asset_type": "class_photo",
                "class_key": class_key,
                "child_name": child_name,
                "label": os.path.basename(path),
                "path": path,
                "source_path": path,
                "job_folder": job_folder,
                "class_photos_folder": class_photos_folder,
                "quantity": 1,
            }
            self.stage7_class_photo_entries.append(class_entry)
            try:
                self._persist_stage7_class_photo_entry(class_entry, entry)
            except Exception as exc:  # pylint: disable=broad-except
                QMessageBox.warning(
                    self,
                    "Class Photos",
                    f"Class photo was added to this computer, but could not be saved to the shared database.\n\n{exc}",
                )

    def _package_order_lines_for_entry(self, entry: Dict[str, object]) -> List[Tuple[str, int]]:
        if str((entry or {}).get("asset_type") or "").strip().lower() in {"class_photo", "missing_class_photo"}:
            return [("Class Photos", 1)]
        lines: List[Tuple[str, int]] = []
        package_name = self._package_folder_name_from_value((entry or {}).get("package"))
        if package_name:
            lines.append((package_name, 1))
        lines.extend(self._addon_order_lines_from_entry(entry))
        if not lines:
            lines = self._fallback_order_lines_from_filename(entry)
        if not lines:
            lines = [("Unsorted", 1)]

        deduped: List[Tuple[str, int]] = []
        seen: Set[str] = set()
        for line, multiplier in lines:
            clean = sanitize_folder_name(str(line or "").strip()) or "Unsorted"
            key = clean.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append((clean, max(1, int(multiplier or 1))))
        return deduped or [("Unsorted", 1)]

    def move_selected_original_paid_assets_to_stage7(self) -> None:
        if not self.send_to_edit_status:
            return
        selected_entries = [
            self._send_to_edit_entry_from_item(item)
            for item in self._selected_send_to_edit_items()
            if item is not None
        ]
        original_entries = [entry for entry in selected_entries if self._is_original_paid_asset(entry)]
        if not original_entries and selected_entries:
            selected_keys = {self._paid_order_asset_group_key(entry) for entry in selected_entries}
            original_entries = [
                dict(entry or {})
                for entry in self.pending_send_to_edit_entries
                if self._is_original_paid_asset(entry)
                and self._paid_order_asset_group_key(entry) in selected_keys
            ]
        if not original_entries:
            QMessageBox.information(
                self,
                "Move Original to Stage 7",
                "Select one or more paid order images in Stage 6, then try again.",
            )
            return

        errors: List[str] = []
        for entry in original_entries:
            try:
                self.db.set_proofing_paid_asset_group_status(
                    workflow_item_id=entry.get("workflow_item_id"),
                    order_no=str(entry.get("order_no") or ""),
                    original_id=str(entry.get("original_id") or ""),
                    proof_id=str(entry.get("proof_id") or ""),
                    status="stage7",
                )
            except Exception as exc:  # pylint: disable=broad-except
                errors.append(f"{self._paid_order_asset_ui_label(entry)}: {exc}")

        if errors:
            QMessageBox.warning(
                self,
                "Move Original to Stage 7",
                "Some items could not be moved.\n\n" + "\n".join(errors[:8]),
            )
            return
        self._add_class_photos_for_paid_entries(original_entries)
        self.pending_send_to_edit_entries = self._load_paid_order_asset_entries()
        self.pending_stage7_asset_entries = self._load_stage7_paid_asset_entries()
        self.set_send_to_edit_status_entries(self.pending_send_to_edit_entries)
        self.set_stage7_paid_asset_entries(self.pending_stage7_asset_entries)

    def _selected_stage7_original_entries(self) -> List[Dict[str, str]]:
        if not self.stage7_package_asset_status:
            return []
        items = [item for item in self.stage7_package_asset_status.selectedItems() if item is not None]
        if not items and self.stage7_package_asset_status.currentItem() is not None:
            items = [self.stage7_package_asset_status.currentItem()]

        entries: List[Dict[str, str]] = []
        seen: Set[Tuple[str, str, str, str]] = set()
        for item in items:
            entry = self._send_to_edit_entry_from_item(item)
            if str((entry or {}).get("asset_type") or "").strip().lower() != "original":
                continue
            key = (
                str((entry or {}).get("workflow_item_id") or ""),
                str((entry or {}).get("order_no") or ""),
                str((entry or {}).get("original_id") or ""),
                str((entry or {}).get("proof_id") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
        return entries

    def restore_selected_stage7_assets_to_stage6(self) -> bool:
        original_entries = self._selected_stage7_original_entries()
        if not original_entries:
            return False
        errors: List[str] = []
        for entry in original_entries:
            try:
                self.db.set_proofing_paid_asset_group_status(
                    workflow_item_id=entry.get("workflow_item_id"),
                    order_no=str(entry.get("order_no") or ""),
                    original_id=str(entry.get("original_id") or ""),
                    proof_id=str(entry.get("proof_id") or ""),
                    status="stage6",
                )
            except Exception as exc:  # pylint: disable=broad-except
                errors.append(f"{self._paid_order_asset_ui_label(entry)}: {exc}")
        if errors:
            QMessageBox.warning(
                self,
                "Restore to Stage 6",
                "Some items could not be restored.\n\n" + "\n".join(errors[:8]),
            )
            return False
        self.pending_send_to_edit_entries = self._load_paid_order_asset_entries()
        self.pending_stage7_asset_entries = self._load_stage7_paid_asset_entries()
        self.set_send_to_edit_status_entries(self.pending_send_to_edit_entries)
        self.set_stage7_paid_asset_entries(self.pending_stage7_asset_entries)
        return True

    def _open_with_windows_app_picker(self, path: str) -> None:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
            return
        opener = "xdg-open" if sys.platform.startswith("linux") else "open"
        subprocess.Popen([opener, path])

    def open_selected_send_to_edit_assets(self) -> None:
        if not self.send_to_edit_status:
            return
        items = self._selected_send_to_edit_items()
        if not items:
            return
        if len(items) > 1:
            confirm = QMessageBox.question(
                self,
                "Open Paid Order Assets",
                f"Open {len(items)} selected asset(s)?",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if confirm != QMessageBox.Yes:
                return
        for item in items:
            self.open_send_to_edit_folder(item)

    def open_send_to_edit_folder(self, item: QListWidgetItem):
        if not item:
            return
        path = item.data(Qt.UserRole)
        if not path or not os.path.exists(path):
            QMessageBox.warning(
                self,
                "Paid Order Assets",
                "Could not locate the selected asset.",
            )
            return
        try:
            self._open_with_windows_app_picker(path)
        except Exception as exc:  # pylint: disable=broad-except
            QMessageBox.warning(
                self,
                "Paid Order Assets",
                f"Unable to open asset:\n{path}\n{exc}",
            )

    def open_selected_stage7_assets(self) -> None:
        if not self.stage7_package_asset_status:
            return
        items = [item for item in self.stage7_package_asset_status.selectedItems() if item is not None]
        if not items and self.stage7_package_asset_status.currentItem() is not None:
            items = [self.stage7_package_asset_status.currentItem()]
        if not items:
            return
        if len(items) > 1:
            confirm = QMessageBox.question(
                self,
                "Stage 7 Print Assets",
                f"Open {len(items)} selected asset(s)?",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if confirm != QMessageBox.Yes:
                return
        for item in items:
            self.open_print_folder(item)

    def open_print_folder(self, item: QListWidgetItem):
        if not item:
            return
        entry = self._send_to_edit_entry_from_item(item)
        if str((entry or {}).get("asset_type") or "").strip().lower() == "missing_class_photo":
            self._resolve_missing_class_photo_entry(entry)
            return
        path = item.data(Qt.UserRole)
        if not path:
            path = self.print_paths.get(item.text().strip(), '')
        if not path or not os.path.exists(path):
            QMessageBox.warning(
                self,
                "Stage 7 Print Assets",
                "Could not locate the selected Print asset.",
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
                "Stage 7 Print Assets",
                f"Unable to open asset:\n{path}\n{exc}",
            )

    def _resolve_missing_class_photo_entry(self, entry: Dict[str, object]) -> None:
        class_key = str((entry or {}).get("class_key") or "").strip()
        class_photos_folder = str((entry or {}).get("class_photos_folder") or "").strip()
        if not class_photos_folder:
            class_photos_folder = os.path.join(str((entry or {}).get("job_folder") or "").strip(), "Class Photos")
        path = self._choose_class_photo_file(class_photos_folder, class_key)
        if not path or not os.path.exists(path):
            return
        dedup_key = self._class_photo_dedup_key(path)
        for existing in self.stage7_class_photo_entries:
            if (
                str((existing or {}).get("asset_type") or "").strip().lower() == "class_photo"
                and self._class_photo_dedup_key(str((existing or {}).get("path") or "")) == dedup_key
            ):
                QMessageBox.information(
                    self,
                    "Class Photos",
                    "This class photo is already in Stage 7.",
                )
                return
        target_key = (
            self._class_photo_dedup_key(str((entry or {}).get("job_folder") or "")),
            class_key.lower(),
        )
        updated: List[Dict[str, str]] = []
        replaced = False
        for existing in self.stage7_class_photo_entries:
            existing_key = (
                self._class_photo_dedup_key(str((existing or {}).get("job_folder") or "")),
                str((existing or {}).get("class_key") or "").strip().lower(),
            )
            if not replaced and existing_key == target_key:
                resolved = dict(existing)
                resolved.update(
                    {
                        "asset_type": "class_photo",
                        "label": os.path.basename(path),
                        "path": path,
                        "source_path": path,
                        "missing_reason": "",
                    }
                )
                updated.append(resolved)
                try:
                    source_entry = self._stage7_source_entry_for_class_key(
                        str((resolved or {}).get("job_folder") or ""),
                        class_key,
                    )
                    self._persist_stage7_class_photo_entry(resolved, source_entry)
                except Exception as exc:  # pylint: disable=broad-except
                    QMessageBox.warning(
                        self,
                        "Class Photos",
                        f"Class photo was selected, but could not be saved to the shared database.\n\n{exc}",
                    )
                replaced = True
            else:
                updated.append(existing)
        self.stage7_class_photo_entries = updated
        self.set_stage7_paid_asset_entries(self._load_stage7_paid_asset_entries())

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
            user_error, technical_detail = self._split_user_error(exc)
            self._show_workflow_error(
                "Edit Flags",
                what_happened=f"Failed to update I/G flags in DB: {user_error}",
                checked=str(item_id),
                next_step="Check the database connection, then try changing the flags again.",
                technical_detail=technical_detail,
            )
            return
        self._set_item_flag_state(row_item, has_i=new_i, has_g=new_g)
        self.sync_ig_checkboxes()

    def _item_search_text(self, item: QListWidgetItem) -> str:
        parts = [str(item.text() or "")]
        for role_offset in (2, 20):
            value = item.data(Qt.UserRole + role_offset)
            if not value:
                continue
            if isinstance(value, (dict, list, tuple)):
                try:
                    parts.append(json.dumps(value, ensure_ascii=False, default=str))
                except Exception:
                    parts.append(str(value))
            else:
                parts.append(str(value))
        return " ".join(parts).lower()

    def _filter_search_list_widget(self, lw: Optional[QListWidget], text: str) -> bool:
        if lw is None:
            return False
        any_visible = False
        for i in range(lw.count()):
            item = lw.item(i)
            match = not text or text in self._item_search_text(item)
            item.setHidden(not match)
            if match:
                any_visible = True
        return any_visible

    def _table_row_search_text(self, table: QTableWidget, row: int) -> str:
        parts: List[str] = []
        for col in range(table.columnCount()):
            item = table.item(row, col)
            if item is None:
                continue
            parts.append(self._item_search_text(item))
        return " ".join(parts).lower()

    def _filter_search_table_widget(self, table: Optional[QTableWidget], text: str) -> bool:
        if table is None:
            return False
        any_visible = False
        for row in range(table.rowCount()):
            match = not text or text in self._table_row_search_text(table, row)
            table.setRowHidden(row, not match)
            if match:
                any_visible = True
        return any_visible

    def _filter_search_extra_widget(self, widget, text: str) -> bool:
        if isinstance(widget, QTableWidget):
            return self._filter_search_table_widget(widget, text)
        return self._filter_search_list_widget(widget, text)

    def _extra_search_lists_for_column(self, col_widget: QWidget) -> List[object]:
        lists: List[object] = []
        for widget in (
            self.send_to_edit_status,
            self.print_status,
            self.stage7_package_asset_status,
        ):
            if widget is not None and (widget.parent() is col_widget or col_widget.isAncestorOf(widget)):
                lists.append(widget)
        return lists

    def _column_button_text_matches(self, col_widget: QWidget, text: str) -> bool:
        if not text:
            return False
        for button in col_widget.findChildren(QPushButton):
            if text in str(button.text() or "").lower():
                return True
        return False

    def perform_search(self, text: str):
        text = text.strip().lower()
        for col_widget, lw in self.column_widgets:
            any_visible = False
            if hasattr(self, "edit_widgets") and lw == self.edit_widgets[0]:
                lw_edit, lw_i, lw_g = self.edit_widgets
                for i in range(lw_edit.count()):
                    item = lw_edit.item(i)
                    match = not text or text in self._item_search_text(item)
                    item.setHidden(not match)
                    lw_i.item(i).setHidden(not match)
                    lw_g.item(i).setHidden(not match)
                    if match:
                        any_visible = True
                col_widget.setVisible(any_visible or not text)
            else:
                any_visible = self._filter_search_list_widget(lw, text)
                for extra_lw in self._extra_search_lists_for_column(col_widget):
                    any_visible = self._filter_search_extra_widget(extra_lw, text) or any_visible
                if self._column_button_text_matches(col_widget, text):
                    any_visible = True
                col_widget.setVisible(any_visible or not text)
